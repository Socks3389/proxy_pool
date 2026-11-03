"""调度器：每 N 秒一轮「抓取 → 验证 → 入库 → 清理」。

为什么必须分级轮转（实测依据）：
    全量 16660 条，并发 200、超时 8s 时单向验完需 11 分钟，超过 5 分钟周期。
    硬塞进 5 分钟只有两个办法：降超时（会误杀，p99 延迟 7.9s）或抬并发（运营商限速反而更慢）。
    所以采取轮转：每轮按优先级挑一批（默认 4000），保证轮次预算内跑完，全池在 3-4 轮内覆盖一遍。
    效果等价于"5 分钟内持续验证"，同时不会因为跑不完而堆积。

轮次预算控制：round_budget 到达后不再派发新任务，已派发的等它完成，记 notes 说明。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass

from .fetcher import fetch_all
from .models import utcnow
from .store import ALIVE, Store
from .validator import ProbeResult, Validator

log = logging.getLogger("proxypool.scheduler")


@dataclass
class RoundReport:
    round_id: int
    fetched_sources: int = 0
    fetched_raw: int = 0
    fetched_new: int = 0
    checked: int = 0
    ok: int = 0
    failed: int = 0
    promoted: int = 0
    killed: int = 0
    banned: int = 0
    pruned: int = 0
    duration_ms: int = 0
    notes: str = ""
    truncated: bool = False


class Scheduler:
    def __init__(self, cfg, store: Store, validator: Validator):
        self.cfg = cfg
        self.store = store
        self.validator = validator
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 轮次互斥：防止手动触发与定时触发重入。
        # 实测踩坑：不加锁时会出现同一时刻两轮并行，
        # 后一轮 pick 拿到空列表（前一轮刚把所有 next_check_at 推到未来），
        # 日志里出现一堆 "验证 0 条" 的空轮，白占一次周期。
        self._round_lock = asyncio.Lock()
        self.last_report: RoundReport | None = None
        self.running = False
        self.round_started_at: dt.datetime | None = None
        self.trigger_now = asyncio.Event()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="proxypool-scheduler")
        log.info("调度器已启动，周期 %ds", self.cfg.scheduler.interval)

    async def stop(self) -> None:
        self._stop.set()
        self.trigger_now.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("调度器已停止")

    async def _loop(self) -> None:
        """主循环：热池精检每轮都跑，冷池扩充按间隔穿插。

        为什么这样分（实测依据）：
        - 2 万多条 new 全量验证一遍要 5-14 分钟（取决于并发），塞不进 5 分钟周期
        - 用高并发硬塞到 5 分钟会因拥塞误杀可用代理（并发 800 时存活率从 5.9% 掉到 1.7%）
        - 而你真正关心的"每 5 分钟验证清理"针对的是在用的那批（alive+candidate，约 1500 条）
        所以：每 300 秒精检一遍热池（实测 30-60 秒跑完，100% 覆盖），
        每 N 轮做一次带抓取的常规扩充，两者互不挤占预算。
        """
        refresh_every = max(1, self.cfg.scheduler.refresh_every_n_rounds)
        round_no = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            hot_only = not (round_no % refresh_every == 0)
            try:
                rep = await self.run_round(hot_only=hot_only)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单轮异常不能让调度器死掉
                log.exception("轮次异常，已记录，继续下一轮")
                rep = None
            # 空轮不推进计数，下次仍然按原节奏（避免空轮把轮次序号和抓取节奏带偏）
            if rep is None or rep.checked > 0:
                round_no += 1

            elapsed = time.monotonic() - t0
            wait = max(5.0, self.cfg.scheduler.interval - elapsed)
            log.info("本轮耗时 %.1fs，%.0fs 后开始下一轮", elapsed, wait)
            try:
                await asyncio.wait_for(self.trigger_now.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self.trigger_now.clear()

    # ---------- 单轮 ----------

    async def run_round(self, *, hot_only: bool = False) -> RoundReport:
        """跑一轮（带互斥，重入会直接返回空报告而不是并行跑）。

        hot_only=True：只复检热池（alive + candidate），不做抓取。
        这是满足"每 5 分钟验证清理一次"的主力轮次：热池约 1500 条，
        按实测 50 条/秒可 30 秒跑完，300 秒周期绰绰有余，真正全量覆盖。
        常规轮则负责抓新源、扩充冷池。
        """
        if self._round_lock.locked():
            log.info("已有轮次在跑，本次触发跳过")
            return self.last_report or RoundReport(round_id=-1, notes="skipped: already running")
        async with self._round_lock:
            return await self._run_round_inner(hot_only=hot_only)

    async def _run_round_inner(self, *, hot_only: bool = False) -> RoundReport:
        self.running = True
        self.round_started_at = utcnow()
        t_start = time.monotonic()
        round_id = await self.store.start_round()
        rep = RoundReport(round_id=round_id)

        try:
            # ---- 1. 抓取（精检轮跳过）----
            if self.cfg.scheduler.refetch_every_round and not hot_only:
                t_fetch = time.monotonic()
                results, entries = await fetch_all(self.cfg)
                rep.fetched_sources = sum(1 for r in results if r.ok)
                rep.fetched_raw = sum(r.parsed for r in results)
                rep.fetched_new = await self.store.upsert_many(entries, "multi")
                log.info("抓取 %d 源 / %d 条 / 新增 %d，耗时 %.1fs",
                         rep.fetched_sources, len(entries), rep.fetched_new,
                         time.monotonic() - t_fetch)

            # ---- 2. 挑选待验证 ----
            budget = self.cfg.scheduler.round_budget
            elapsed = time.monotonic() - t_start
            pick_limit = self.cfg.scheduler.candidate_batch
            if hot_only:
                # 精检轮：额度直接设为热池全量，确保 5 分钟内全覆盖
                hot = await self.store.hot_pool_size()
                pick_limit = max(pick_limit, hot + 200)
            if elapsed > budget * 0.5:
                pick_limit = max(300, int(pick_limit * 0.4))
                rep.truncated = True
            rows = await self.store.pick_for_check(pick_limit, hot_only=hot_only)
            log.info("本轮待验证 %d 条（%s，预算 %ds）",
                     len(rows), "热池精检" if hot_only else "常规", budget)
            if not rows:
                rep.notes = "无待验证代理"
                return rep

            # ---- 3. 并发验证，带截止时间 ----
            deadline = t_start + budget
            results, https_map, checked = await self._verify_batch(rows, deadline)
            rep.checked = checked
            if checked < len(rows):
                rep.truncated = True
                rep.notes = f"轮次预算到达，跳过 {len(rows) - checked} 条"

            # ---- 4. 写回 ----
            stats = await self.store.apply_results(results, https_map)
            rep.ok = stats["ok"]
            rep.failed = stats["failed"]
            rep.promoted = stats["promoted"]
            rep.killed = stats["killed"]
            rep.banned = stats["banned"]

            # ---- 5. 清理 ----
            cl = await self.store.cleanup()
            rep.pruned = (cl["logs_deleted"] + cl["stale_deleted"]
                          + cl["overflow_deleted"] + cl.get("dead_purged", 0)
                          + cl.get("banned_purged", 0))
            if any(v for k, v in cl.items() if k != "wal_checkpoint"):
                log.info("清理：%s", cl)

        finally:
            rep.duration_ms = int((time.monotonic() - t_start) * 1000)
            await self.store.finish_round(round_id, {
                "finished_at": utcnow(),
                "duration_ms": rep.duration_ms,
                "fetched_sources": rep.fetched_sources,
                "fetched_raw": rep.fetched_raw,
                "fetched_new": rep.fetched_new,
                "checked": rep.checked, "ok": rep.ok, "failed": rep.failed,
                "promoted": rep.promoted, "killed": rep.killed,
                "banned": rep.banned, "pruned": rep.pruned,
                "notes": rep.notes or None,
            })
            self.last_report = rep
            self.running = False
            log.info(
                "轮次完成：验证 %d 存活 %d 失败 %d 晋升 %d 淘汰 %d 拉黑 %d 清理 %d，%.1fs",
                rep.checked, rep.ok, rep.failed, rep.promoted, rep.killed,
                rep.banned, rep.pruned, rep.duration_ms / 1000,
            )
        return rep

    async def _verify_batch(self, rows: list[dict], deadline: float):
        """并发验证一批代理，返回 (结果, https 结果, 实际完成数)。

        预算控制要点：任务在 deadline 到达后主动放弃（不是等它跑完）。
        用 asyncio.wait 带超时收集，超时部分直接丢弃并取消，保证轮次严格按时收尾。
        """
        results: list[tuple[dict, ProbeResult]] = []
        https_map: dict[int, ProbeResult] = {}
        lock = asyncio.Lock()

        async def one(row: dict) -> None:
            if time.monotonic() >= deadline:
                return
            res = await self.validator.verify(row["protocol"], row["ip"], row["port"])
            async with lock:
                results.append((row, res))

        tasks = [asyncio.create_task(one(r)) for r in rows]
        remain = max(1.0, deadline - time.monotonic())
        done, pending = await asyncio.wait(tasks, timeout=remain)
        for t in pending:
            t.cancel()
        if pending:
            # 等取消落地，避免残留任务在下轮还在跑
            await asyncio.gather(*pending, return_exceptions=True)
            log.warning("预算到达，取消 %d 条未完成验证", len(pending))
        checked = len(results)

        # HTTPS 能力探测。
        # 覆盖 http/https（需要 CONNECT 支持，实测仅 16% 通过）和 socks5
        # （协议天然支持隧穿，但实际吞吐/TLS 表现仍需实测，否则筛"支持 HTTPS"时
        # 会把 206 条 socks5 全部排除，结果误导用户）。
        # socks4 不做：它不支持域名解析，访问 443 目标天然失败，保持 false 是正确的。
        if self.cfg.scheduler.fast_promote:
            need_https = [
                row for row, res in results
                if res.ok and row["protocol"] in ("http", "https", "socks5")
                and time.monotonic() < deadline
            ]
            if need_https:
                log.info("二级探测 HTTPS 能力：%d 条", len(need_https))
                sem = asyncio.Semaphore(min(60, self.cfg.validator.concurrency))

                async def hprobe(row: dict) -> None:
                    if time.monotonic() >= deadline:
                        return
                    async with sem:
                        hr = await self.validator.verify_https(
                            row["protocol"], row["ip"], row["port"]
                        )
                        async with lock:
                            https_map[row["id"]] = hr

                htasks = [asyncio.create_task(hprobe(r)) for r in need_https]
                hremain = max(0.5, deadline - time.monotonic())
                _hdone, hpending = await asyncio.wait(htasks, timeout=hremain)
                for t in hpending:
                    t.cancel()
                if hpending:
                    await asyncio.gather(*hpending, return_exceptions=True)

        return results, https_map, checked

    def status(self) -> dict:
        rep = self.last_report
        return {
            "running": self.running,
            "round_started_at": self.round_started_at.isoformat() if self.round_started_at else None,
            "interval": self.cfg.scheduler.interval,
            "last_round": {
                "checked": rep.checked, "ok": rep.ok, "failed": rep.failed,
                "promoted": rep.promoted, "killed": rep.killed, "banned": rep.banned,
                "duration_ms": rep.duration_ms, "truncated": rep.truncated,
                "notes": rep.notes,
            } if rep else None,
        }
