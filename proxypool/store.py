"""入库与状态机。

负责四件事：upsert 抓到的代理、按验证结果推进状态、挑本轮要验的、清理过期数据。

批量写用 INSERT ... ON CONFLICT DO UPDATE，不走「逐条 select 再 insert」。
后者 1.6 万条要 40 秒以上。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Sequence

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import CheckLog, Proxy, RoundStat, Tombstone, utcnow
from .validator import ProbeResult

log = logging.getLogger("proxypool.store")

ALIVE = "alive"
CANDIDATE = "candidate"
NEW = "new"
DEAD = "dead"
BANNED = "banned"
ALL_STATUS = (NEW, CANDIDATE, ALIVE, DEAD, BANNED)


class Store:
    def __init__(self, session_factory, cfg):
        self.sf = session_factory
        self.cfg = cfg
        self._is_sqlite = cfg.db_url.startswith("sqlite")

    # ---------------- 写入 ----------------

    async def upsert_many(self, entries: Sequence[tuple[str, str, int]], source_name: str) -> int:
        """批量插入新代理，返回真正新增的条数；已存在的只把 source_count 加一。

        不能用 RETURNING 统计新增：SQLite 的 ON CONFLICT DO UPDATE ... RETURNING
        会把被更新的行也返回，重复条目就被算成新增。

        插入前先按墓碑过滤。purge 删掉 dead 之后源会把同一批地址再吐回来，
        不拦就会出现「验死 → 删除 → 重抓 → 再验」的死循环。
        """
        if not entries:
            return 0
        rows = [
            {"protocol": p, "ip": ip, "port": port,
             "status": NEW, "score": 50, "first_source": source_name}
            for p, ip, port in entries
        ]
        inserted = 0
        skipped_tomb = 0
        async with self.sf() as s:
            # 先按墓碑过滤
            dead_cut = utcnow() - dt.timedelta(seconds=self.cfg.cleanup.tombstone_seconds)
            tomb: set[tuple[str, str, int]] = set()
            keys_all = [(r["protocol"], r["ip"], r["port"]) for r in rows]
            tm = 800
            for j in range(0, len(keys_all), tm):
                batch = keys_all[j:j + tm]
                found = await s.execute(
                    select(Tombstone.protocol, Tombstone.ip, Tombstone.port)
                    .where(Tombstone.protocol.in_({k[0] for k in batch}))
                    .where(Tombstone.ip.in_({k[1] for k in batch}))
                    .where(Tombstone.port.in_({k[2] for k in batch}))
                    .where(Tombstone.died_at >= dead_cut)
                )
                bset = set(batch)
                for proto, ip, port in found:
                    if (proto, ip, port) in bset:
                        tomb.add((proto, ip, port))
            if tomb:
                before = len(rows)
                rows = [r for r in rows
                        if (r["protocol"], r["ip"], r["port"]) not in tomb]
                skipped_tomb = before - len(rows)

            chunk = 2000 if self._is_sqlite else 5000
            for i in range(0, len(rows), chunk):
                part = rows[i:i + chunk]
                keys = [(r["protocol"], r["ip"], r["port"]) for r in part]
                # 先查已存在，再插入，差值即新增数
                existing: set[tuple[str, str, int]] = set()
                mm = 800
                for j in range(0, len(keys), mm):
                    batch = keys[j:j + mm]
                    bset = set(batch)
                    found = await s.execute(
                        select(Proxy.protocol, Proxy.ip, Proxy.port)
                        .where(Proxy.protocol.in_({k[0] for k in batch}))
                        .where(Proxy.ip.in_({k[1] for k in batch}))
                        .where(Proxy.port.in_({k[2] for k in batch}))
                    )
                    for proto, ip, port in found:
                        if (proto, ip, port) in bset:
                            existing.add((proto, ip, port))
                new_count = sum(1 for k in keys if k not in existing)

                if self._is_sqlite:
                    stmt = sqlite_insert(Proxy).values(part)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["protocol", "ip", "port"],
                        set_={"source_count": Proxy.__table__.c.source_count + 1},
                    )
                else:
                    stmt = pg_insert(Proxy).values(part)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["protocol", "ip", "port"],
                        set_={"source_count": Proxy.__table__.c.source_count + 1},
                    )
                await s.execute(stmt)
                inserted += new_count
            await s.commit()
        log.debug("upsert %d 条 → 新增 %d，墓碑跳过 %d", len(rows) + skipped_tomb, inserted, skipped_tomb)
        return inserted

    async def pick_for_check(self, limit: int, *, hot_only: bool = False) -> list[dict]:
        """挑本轮要验证的代理，按价值排序。

        优先级：
        1. alive 且没测过 HTTPS 的 —— 补上能力标记
        2. alive 到期复验
        3. candidate 里失败次数少的 —— 有复活希望
        4. new —— 扩充池子，仅常规轮

        几个配额是有原因的：
        - candidate 失败 2 次以上的排后面。否则上万条准僵尸会吃光预算，
          新鲜的 candidate 永远轮不到。
        - new 的额度要封顶。2 万条 new 抽不完，会把 alive 的复验预算挤成 0，
          淘汰机制就失效了。
        - hot_only=True 只挑 alive 和 candidate，给 5 分钟精检轮用。
        """
        now = utcnow()
        out: list[dict] = []
        seen: set[int] = set()

        def _add(rows):
            for r in rows:
                d = dict(r._mapping)
                if d["id"] not in seen:
                    seen.add(d["id"])
                    out.append(d)

        async with self.sf() as s:
            # 1) alive 但没测过 HTTPS 的，优先补测。
            # 漏了这批，"只要 HTTPS"筛出来的结果就不可信。
            # socks5 也要测：它本来就能隧穿，不实测就标不上支持，
            # 筛 HTTPS 时会把两百多条 socks5 全排除掉。
            quota_untested = int(limit * 0.25)
            rows = await s.execute(
                select(Proxy.id, Proxy.protocol, Proxy.ip, Proxy.port, Proxy.status)
                .where(Proxy.status == ALIVE)
                .where(Proxy.protocol.in_(("http", "https", "socks5")))
                .where(Proxy.https_try_count == 0)
                .order_by(Proxy.score.desc())
                .limit(quota_untested)
            )
            _add(rows)

            # 2) alive 到期复验：精检轮占 60%，常规轮占 45%
            quota_alive = int(limit * (0.60 if hot_only else 0.45))
            rows = await s.execute(
                select(Proxy.id, Proxy.protocol, Proxy.ip, Proxy.port, Proxy.status)
                .where(Proxy.status == ALIVE)
                .where((Proxy.next_check_at.is_(None)) | (Proxy.next_check_at <= now))
                .order_by(Proxy.score.desc(), Proxy.last_checked_at.asc().nullsfirst())
                .limit(quota_alive)
            )
            _add(rows)

            # 3) candidate：只挑失败次数少的（有复活希望），按失败次数升序
            remain = limit - len(out)
            if remain > 0:
                rows = await s.execute(
                    select(Proxy.id, Proxy.protocol, Proxy.ip, Proxy.port, Proxy.status)
                    .where(Proxy.status == CANDIDATE)
                    .where((Proxy.next_check_at.is_(None)) | (Proxy.next_check_at <= now))
                    .where(Proxy.consecutive_fail <= 1)
                    .order_by(Proxy.consecutive_fail.asc(), Proxy.score.desc())
                    .limit(min(remain, int(limit * 0.40)))
                )
                _add(rows)

            if not hot_only:
                # 4) new 随机抽样：填充剩余
                remain = limit - len(out)
                if remain > 0:
                    rows = await s.execute(
                        select(Proxy.id, Proxy.protocol, Proxy.ip, Proxy.port, Proxy.status)
                        .where(Proxy.status == NEW)
                        .order_by(func.random())
                        .limit(remain)
                    )
                    _add(rows)
            # dead 不参与重测。给它们留 20% 配额的话，
            # 几万条死地址每轮吃掉几百条预算，而复活率不到 2%。
            # dead 只在到达 ban 阈值前短暂存在，之后由 cleanup 收走。
        return out

    async def hot_pool_size(self) -> int:
        """热池规模（alive + candidate），用于判断能否 5 分钟全量覆盖。"""
        async with self.sf() as s:
            return (await s.execute(
                select(func.count()).select_from(Proxy).where(Proxy.status.in_((ALIVE, CANDIDATE)))
            )).scalar_one()

    async def next_due_seconds(self) -> float | None:
        """距离最近一个待验证代理到期还有多少秒，没有则返回 None。

        调度器用它避开空轮：扩充轮跑 247s 时，新晋升代理的 next_check_at
        是那轮开始 +300s，下一轮只等 53s 就开始，到点时没有任何代理到期，
        于是白跑一个验证 0 条的空轮。
        """
        now = utcnow()
        async with self.sf() as s:
            nxt = (await s.execute(
                select(func.min(Proxy.next_check_at))
                .where(Proxy.status.in_((ALIVE, CANDIDATE, NEW, DEAD)))
                .where(Proxy.next_check_at.is_not(None))
                .where(Proxy.next_check_at > now)
            )).scalar()
        if nxt is None:
            return None
        return max(0.0, (nxt - now).total_seconds())

    async def apply_results(self, results: list[tuple[dict, ProbeResult]],
                            https_results: dict[int, ProbeResult] | None = None) -> dict:
        """把验证结果写回库存，推进状态机。返回统计。"""
        pol = self.cfg.policy
        now = utcnow()
        https_results = https_results or {}
        stats = {"promoted": 0, "killed": 0, "banned": 0, "ok": 0, "failed": 0, "skipped": 0}
        logs: list[dict] = []

        async with self.sf() as s:
            # 一次性拉出涉及的行，避免 N 次查询
            ids = [row["id"] for row, _ in results]
            if not ids:
                return stats
            proxy_map: dict[int, Proxy] = {}
            for i in range(0, len(ids), 5000):
                chunk = ids[i:i + 5000]
                rows = await s.execute(select(Proxy).where(Proxy.id.in_(chunk)))
                for p in rows.scalars():
                    proxy_map[p.id] = p

            for row, res in results:
                p = proxy_map.get(row["id"])
                if p is None:
                    continue
                was_status = p.status
                p.last_checked_at = now

                if res.ok:
                    stats["ok"] += 1
                    p.ok_count += 1
                    p.consecutive_ok += 1
                    p.consecutive_fail = 0
                    p.latency_ms = res.latency_ms
                    # 指数移动平均，兼顾近期性与稳定性
                    p.latency_avg_ms = (
                        res.latency_ms if p.latency_avg_ms is None
                        else int(p.latency_avg_ms * 0.7 + res.latency_ms * 0.3)
                    )
                    p.score = min(pol.score_max, p.score + pol.score_up)
                    p.last_ok_at = now
                    p.last_error = None
                    if res.anonymity:
                        p.anonymity = res.anonymity
                    if res.exit_ip:
                        p.exit_ip = res.exit_ip[:45]
                    if res.leak_headers:
                        p.leak_headers = json.dumps(res.leak_headers[:8])
                    else:
                        p.leak_headers = None

                    hr = https_results.get(p.id)
                    if hr is not None:
                        p.https_try_count += 1
                        if hr.ok:
                            p.https_ok_count += 1
                            p.supports_https = True
                            # HTTPS 能力稀缺（HTTP 代理里只有 16% 支持 CONNECT），
                            # 额外加分，否则纯 HTTP 代理会靠低延迟挤到排序前排。
                            p.score = min(pol.score_max, p.score + 6)
                        # 通过率低于 1/3 时撤销 HTTPS 标记，避免给爬虫一个不可靠的承诺
                        elif p.https_try_count >= 3 and p.https_ok_count * 3 < p.https_try_count:
                            p.supports_https = False

                    if p.consecutive_ok >= pol.promote_consecutive_ok:
                        if p.status != ALIVE:
                            stats["promoted"] += 1
                            p.status = ALIVE
                        # 下次检查：按延迟分档，快代理更频繁复检
                        p.next_check_at = now + dt.timedelta(
                            seconds=self.cfg.scheduler.interval
                        )
                else:
                    # 目标站限流/抽风：不是代理的问题，不推进状态机也不扣分，
                    # 只把下次检查推后一点，避免同一代理被反复打。
                    if res.target_throttled:
                        stats["skipped"] = stats.get("skipped", 0) + 1
                        p.next_check_at = now + dt.timedelta(seconds=120)
                        logs.append({
                            "proxy_id": p.id, "ok": False,
                            "latency_ms": res.latency_ms or None,
                            "error": "target_throttled",
                            "anonymity": None, "supports_https": bool(p.supports_https),
                            "checked_at": now,
                        })
                        continue

                    stats["failed"] += 1
                    p.fail_count += 1
                    p.consecutive_fail += 1
                    p.consecutive_ok = 0
                    p.score = max(pol.score_floor, p.score - pol.score_down)
                    p.last_error = (res.errors_short() or "unknown")[:120]
                    p.latency_ms = None

                    if p.consecutive_fail >= pol.ban_consecutive_fail:
                        if p.status != BANNED:
                            stats["banned"] += 1
                            p.status = BANNED
                        # banned 不再复检，next_check_at 无意义，留空。
                        p.next_check_at = None
                    elif p.consecutive_fail >= pol.kill_consecutive_fail:
                        if p.status != DEAD:
                            stats["killed"] += 1
                            p.status = DEAD
                        p.next_check_at = now + dt.timedelta(seconds=pol.dead_cooldown)
                    elif was_status in (DEAD, BANNED):
                        # 已下线的代理本次仍失败：继续尊重原冷却期。
                        # 但反复失败（dead 状态下已经是第 N 次）要加速出局，
                        # 否则几条死代理每次冷却到期都回来消耗预算，
                        # 在 dead 和 candidate 之间无限循环。
                        if p.consecutive_fail >= pol.ban_consecutive_fail:
                            if p.status != BANNED:
                                stats["banned"] += 1
                                p.status = BANNED
                            p.next_check_at = None
                        elif was_status == DEAD:
                            p.next_check_at = now + dt.timedelta(seconds=pol.dead_cooldown)
                        else:
                            p.next_check_at = None
                    else:
                        p.status = CANDIDATE if was_status in (NEW, CANDIDATE) else was_status
                        # 抖动代理：退避复验，避免每轮都浪费预算在它身上
                        backoff = min(900, 60 * p.consecutive_fail)
                        p.next_check_at = now + dt.timedelta(seconds=backoff)

                logs.append({
                    "proxy_id": p.id,
                    "ok": res.ok,
                    "latency_ms": res.latency_ms or None,
                    "error": (res.errors_short() or None) if not res.ok else None,
                    "anonymity": res.anonymity,
                    "supports_https": bool(p.supports_https),
                    "checked_at": now,
                })

            if logs:
                chunk = 2000 if self._is_sqlite else 5000
                for i in range(0, len(logs), chunk):
                    await s.execute(insert(CheckLog), logs[i:i + chunk])
            await s.commit()
        return stats

    # ---------------- 查询 ----------------

    async def counts_by_status(self) -> dict[str, int]:
        async with self.sf() as s:
            rows = await s.execute(
                select(Proxy.status, func.count()).group_by(Proxy.status)
            )
            out = {k: 0 for k in ALL_STATUS}
            for status, n in rows:
                out[status] = n
            return out

    async def counts_by_protocol(self, status: str | None = None) -> dict[str, int]:
        async with self.sf() as s:
            q = select(Proxy.protocol, func.count()).group_by(Proxy.protocol)
            if status:
                q = q.where(Proxy.status == status)
            rows = await s.execute(q)
            return {k: n for k, n in rows}

    async def total(self) -> int:
        async with self.sf() as s:
            return (await s.execute(select(func.count()).select_from(Proxy))).scalar_one()

    async def get_batch(self, n: int, *, protocol: str | None = None,
                        anonymity: str | None = None, https_only: bool = False,
                        max_latency: int | None = None,
                        min_score: int | None = None,
                        min_https_rate: float | None = None,
                        exclude_transparent: bool = True) -> list[dict]:
        """取一批可用代理，按 HTTPS 能力和 score 排序。

        min_https_rate：只返回 HTTPS 实测通过率 ≥ 该值的代理（0-1）。
        比 supports_https 布尔标记可靠：同一个代理的 TLS 通过率波动很大，
        一次成功就标 true 太激进，全成功才算又太保守。

        exclude_transparent：默认不返回透明代理。透明代理会把请求头里的
        真实 IP 透给目标站，爬虫拿它等于没挂代理，还会暴露自己。真要的话
        显式传 anonymity="transparent" 或把这个开关关掉。
        """
        async with self.sf() as s:
            q = select(Proxy).where(Proxy.status == ALIVE)
            if protocol:
                q = q.where(Proxy.protocol == protocol)
            if anonymity:
                q = q.where(Proxy.anonymity == anonymity)
            elif exclude_transparent:
                q = q.where(Proxy.anonymity != "transparent")
            if https_only:
                q = q.where(Proxy.supports_https.is_(True))
            if max_latency:
                q = q.where(Proxy.latency_avg_ms <= max_latency)
            if min_score is not None:
                q = q.where(Proxy.score >= min_score)
            if min_https_rate is not None and min_https_rate > 0:
                # 用整数比较避免浮点：ok/try >= rate  <=>  ok*100 >= rate*100*try
                q = q.where(Proxy.https_try_count > 0)
                q = q.where(
                    Proxy.https_ok_count * 100 >= int(min_https_rate * 100) * Proxy.https_try_count
                )
            q = q.order_by(
                # HTTPS 能力排在 score 前面。只按 score 排的话，
                # 满分但只支持 HTTP 的代理会占据前排，
                # 用 HTTPS 抓取的爬虫拿十个全废。
                Proxy.supports_https.desc(),
                Proxy.score.desc(),
                Proxy.latency_avg_ms.asc().nullslast(),
            ).limit(n)
            rows = await s.execute(q)
            out = []
            for p in rows.scalars():
                rate = None
                if p.https_try_count:
                    rate = round(p.https_ok_count / p.https_try_count, 2)
                out.append({
                    "protocol": p.protocol,
                    "ip": p.ip,
                    "port": p.port,
                    "url": p.url,
                    "anonymity": p.anonymity,
                    "supports_https": p.supports_https,
                    "https_success_rate": rate,
                    "latency_ms": p.latency_avg_ms,
                    "score": p.score,
                    "last_ok_at": p.last_ok_at.isoformat() if p.last_ok_at else None,
                })
            return out

    async def list_proxies(self, *, page: int = 1, size: int = 50, status: str | None = None,
                           protocol: str | None = None, anonymity: str | None = None,
                           search: str | None = None, order: str = "score",
                           https_only: bool = False) -> tuple[list[dict], int]:
        async with self.sf() as s:
            conds = []
            if status:
                conds.append(Proxy.status == status)
            if protocol:
                conds.append(Proxy.protocol == protocol)
            if anonymity:
                conds.append(Proxy.anonymity == anonymity)
            if https_only:
                conds.append(Proxy.supports_https.is_(True))
            if search:
                like = f"%{search}%"
                conds.append(Proxy.ip.like(like))
            base = select(Proxy)
            cnt = select(func.count()).select_from(Proxy)
            for c in conds:
                base = base.where(c)
                cnt = cnt.where(c)
            total = (await s.execute(cnt)).scalar_one()
            size = max(1, min(size, 200))
            page = max(1, page)
            order_col = {
                "score": Proxy.score.desc(),
                "latency": Proxy.latency_avg_ms.asc().nullslast(),
                "recent": Proxy.last_ok_at.desc().nullslast(),
                "created": Proxy.created_at.desc(),
            }.get(order, Proxy.score.desc())
            rows = await s.execute(
                base.order_by(order_col).offset((page - 1) * size).limit(size)
            )
            items = [
                {
                    "id": p.id, "protocol": p.protocol, "ip": p.ip, "port": p.port,
                    "status": p.status, "score": p.score,
                    "latency_ms": p.latency_avg_ms,
                    "anonymity": p.anonymity, "supports_https": p.supports_https,
                    "ok_count": p.ok_count, "fail_count": p.fail_count,
                    "consecutive_fail": p.consecutive_fail,
                    "exit_ip": p.exit_ip, "last_error": p.last_error,
                    "first_source": p.first_source, "source_count": p.source_count,
                    "last_ok_at": p.last_ok_at.isoformat() if p.last_ok_at else None,
                    "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in rows.scalars()
            ]
            return items, total

    async def recent_rounds(self, limit: int = 30) -> list[dict]:
        async with self.sf() as s:
            rows = await s.execute(
                select(RoundStat).order_by(RoundStat.started_at.desc()).limit(limit)
            )
            return [
                {
                    "id": r.id,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "duration_ms": r.duration_ms,
                    "fetched_sources": r.fetched_sources,
                    "fetched_raw": r.fetched_raw,
                    "fetched_new": r.fetched_new,
                    "checked": r.checked, "ok": r.ok, "failed": r.failed,
                    "promoted": r.promoted, "killed": r.killed,
                    "banned": r.banned, "pruned": r.pruned,
                    "notes": r.notes,
                }
                for r in rows.scalars()
            ]

    async def error_summary(self, limit: int = 12) -> list[dict]:
        """最近 6 小时失败原因聚合，用于排障看板。"""
        since = utcnow() - dt.timedelta(hours=6)
        async with self.sf() as s:
            rows = await s.execute(
                select(CheckLog.error, func.count().label("n"))
                .where(CheckLog.checked_at >= since)
                .where(CheckLog.ok.is_(False))
                .where(CheckLog.error.is_not(None))
                .group_by(CheckLog.error)
                .order_by(func.count().desc())
                .limit(limit)
            )
            return [{"error": e, "count": n} for e, n in rows]

    async def trend(self, limit: int = 60) -> dict:
        """趋势数据：每轮的验证量/存活/晋升/淘汰 + 池子规模的当前快照。

        面板画图靠这个。用 round_stats 而不是 check_logs 聚合，
        因为前者一轮一行、体积小，且已经带了各项汇总。
        """
        async with self.sf() as s:
            rows = await s.execute(
                select(RoundStat)
                .where(RoundStat.checked > 0)
                .order_by(RoundStat.started_at.desc())
                .limit(limit)
            )
            items = list(rows.scalars())[::-1]  # 时间正序，方便画图
            points = [
                {
                    "t": r.started_at.isoformat() if r.started_at else None,
                    "checked": r.checked,
                    "ok": r.ok,
                    "failed": r.failed,
                    "promoted": r.promoted,
                    "killed": r.killed,
                    "banned": r.banned,
                    "new": r.fetched_new,
                    "duration_s": round((r.duration_ms or 0) / 1000, 1),
                }
                for r in items
            ]
        # 池子状态分布（堆叠图用）
        counts = await self.counts_by_status()
        return {"points": points, "status_counts": counts}

    async def latency_histogram(self, buckets: int = 8) -> dict:
        """可用代理的延迟分布，画柱状图。

        分桶按对数感觉走（<200/500/1s/2s/3s/5s/8s/>8s），
        线性分桶在免费代理这种长尾分布上会全挤在最后一格。
        """
        edges = [0, 200, 500, 1000, 2000, 3000, 5000, 8000, 10**9]
        labels = ["<200ms", "0.2-0.5s", "0.5-1s", "1-2s", "2-3s", "3-5s", "5-8s", ">8s"]
        async with self.sf() as s:
            rows = await s.execute(
                select(Proxy.latency_avg_ms)
                .where(Proxy.status == ALIVE)
                .where(Proxy.latency_avg_ms.is_not(None))
            )
            vals = [v for (v,) in rows if v is not None]
        counts = [0] * (len(edges) - 1)
        for v in vals:
            for i in range(len(edges) - 1):
                if edges[i] <= v < edges[i + 1]:
                    counts[i] += 1
                    break
        return {
            "labels": labels[:buckets],
            "counts": counts[:buckets],
            "total": len(vals),
            "avg": round(sum(vals) / len(vals)) if vals else None,
            "p50": sorted(vals)[len(vals) // 2] if vals else None,
        }

    async def anonymity_breakdown(self) -> dict[str, int]:
        async with self.sf() as s:
            rows = await s.execute(
                select(Proxy.anonymity, func.count())
                .where(Proxy.status == ALIVE)
                .group_by(Proxy.anonymity)
            )
            return {(k or "unknown"): n for k, n in rows}

    # ---------------- 清理 ----------------

    async def cleanup(self) -> dict:
        cl = self.cfg.cleanup
        now = utcnow()
        out = {"logs_deleted": 0, "stale_deleted": 0, "overflow_deleted": 0,
               "cold_capped": 0, "dead_purged": 0, "banned_purged": 0, "tomb_cleared": 0}
        async with self.sf() as s:
            cutoff = now - dt.timedelta(days=cl.log_retention_days)
            r = await s.execute(delete(CheckLog).where(CheckLog.checked_at < cutoff))
            out["logs_deleted"] = r.rowcount or 0

            # 从没成功过、抓来很久的垃圾
            stale_cut = now - dt.timedelta(hours=cl.stale_hours)
            r = await s.execute(
                delete(Proxy)
                .where(Proxy.status.in_((NEW, CANDIDATE)))
                .where(Proxy.ok_count == 0)
                .where(Proxy.created_at < stale_cut)
            )
            out["stale_deleted"] = r.rowcount or 0

            # dead 真清：先把身份写进墓碑，再从 proxies 删。
            # 只删不记会导到源重新吐回来重验（实测一次 13809 条死循环）。
            dead_cut = now - dt.timedelta(seconds=cl.dead_purge_seconds)
            dying = (await s.execute(
                select(Proxy.protocol, Proxy.ip, Proxy.port, Proxy.consecutive_fail)
                .where(Proxy.status == DEAD)
                .where(Proxy.consecutive_fail >= cl.dead_purge_min_fails)
                .where(Proxy.last_checked_at < dead_cut)
            )).fetchall()
            if dying:
                tomb_rows = [
                    {"protocol": p, "ip": ip, "port": port, "fail_count": fc, "died_at": now}
                    for p, ip, port, fc in dying
                ]
                tchunk = 2000 if self._is_sqlite else 5000
                for i in range(0, len(tomb_rows), tchunk):
                    part = tomb_rows[i:i + tchunk]
                    if self._is_sqlite:
                        st = sqlite_insert(Tombstone).values(part)
                        st = st.on_conflict_do_update(
                            index_elements=["protocol", "ip", "port"],
                            set_={"died_at": now, "fail_count": Tombstone.__table__.c.fail_count + 1},
                        )
                    else:
                        st = pg_insert(Tombstone).values(part)
                        st = st.on_conflict_do_update(
                            index_elements=["protocol", "ip", "port"],
                            set_={"died_at": now, "fail_count": Tombstone.__table__.c.fail_count + 1},
                        )
                    await s.execute(st)
                r = await s.execute(
                    delete(Proxy)
                    .where(Proxy.status == DEAD)
                    .where(Proxy.consecutive_fail >= cl.dead_purge_min_fails)
                    .where(Proxy.last_checked_at < dead_cut)
                )
                out["dead_purged"] = r.rowcount or 0

            # 过期墓碑清掉（超过墓碑有效期的，允许重新抓取重验）
            tomb_cut = now - dt.timedelta(seconds=cl.tombstone_seconds)
            r = await s.execute(delete(Tombstone).where(Tombstone.died_at < tomb_cut))
            out["tomb_cleared"] = r.rowcount or 0

            # banned 不复检，超过保留期直接删。实测一天攒上千条。
            ban_cut = now - dt.timedelta(seconds=cl.banned_retention_seconds)
            r = await s.execute(
                delete(Proxy)
                .where(Proxy.status == BANNED)
                .where(Proxy.last_checked_at < ban_cut)
            )
            out["banned_purged"] = r.rowcount or 0

            # 冷池上限：超过就淘汰最差的。
            # 只淘汰 NEW，不碰 CANDIDATE —— candidate 是验证过、只失败一次的，
            # 比从没活过的 new 值钱。混在一起淘汰会把 candidate 清成 0。
            cold = (await s.execute(
                select(func.count()).select_from(Proxy).where(Proxy.status == NEW)
            )).scalar_one()
            if cold > cl.cold_pool_limit:
                excess = cold - cl.cold_pool_limit
                sub = (
                    select(Proxy.id)
                    .where(Proxy.status == NEW)
                    .order_by(Proxy.last_checked_at.asc().nullsfirst(), Proxy.id.asc())
                    .limit(excess)
                )
                ids = [i for (i,) in (await s.execute(sub)).fetchall()]
                if ids:
                    await s.execute(delete(CheckLog).where(CheckLog.proxy_id.in_(ids)))
                    r = await s.execute(delete(Proxy).where(Proxy.id.in_(ids)))
                    out["cold_capped"] = r.rowcount or 0

            # 行数保护兜底
            total = (await s.execute(select(func.count()).select_from(Proxy))).scalar_one()
            if total > cl.max_rows:
                excess = min(total - cl.max_rows, 20000)
                sub = (
                    select(Proxy.id)
                    .where(Proxy.status.in_((BANNED, DEAD, NEW, CANDIDATE)))
                    .order_by(Proxy.score.asc(), Proxy.last_checked_at.asc().nullsfirst())
                    .limit(excess)
                )
                ids = [i for (i,) in (await s.execute(sub)).fetchall()]
                if ids:
                    r = await s.execute(delete(Proxy).where(Proxy.id.in_(ids)))
                    out["overflow_deleted"] = r.rowcount or 0
            await s.commit()

            # WAL checkpoint，空闲页多时再做 VACUUM。
            # SQLite 删行只标记空闲页、不缩文件，删三万行后文件还是 29MB。
            # 空闲页超过 20% 才 VACUUM，太频繁会拖慢整轮。
            if self._is_sqlite:
                try:
                    if cl.wal_checkpoint:
                        await s.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                        await s.commit()
                    free_pages = (await s.execute(text("PRAGMA freelist_count"))).scalar() or 0
                    total_pages = (await s.execute(text("PRAGMA page_count"))).scalar() or 1
                    if free_pages > 500 and free_pages * 100 // total_pages >= 20:
                        log.info("VACUUM：%d/%d 页空闲（%d%%），回收磁盘",
                                 free_pages, total_pages, free_pages * 100 // total_pages)
                        await s.execute(text("VACUUM"))
                        out["vacuumed"] = True
                except Exception as e:  # noqa: BLE001 - 维护失败不该影响清理
                    log.warning("WAL checkpoint / VACUUM 失败: %s", e)
        return out

    # ---------------- 轮次 ----------------

    async def start_round(self) -> int:
        async with self.sf() as s:
            rs = RoundStat(started_at=utcnow())
            s.add(rs)
            await s.commit()
            return rs.id

    async def finish_round(self, round_id: int, data: dict) -> None:
        async with self.sf() as s:
            await s.execute(
                update(RoundStat).where(RoundStat.id == round_id).values(**data)
            )
            await s.commit()

    async def discard_round(self, round_id: int) -> None:
        """丢弃空轮记录。

        一条都没验的轮次留在 round_stats 里，趋势图会多出一串 0 点，
        看着像系统在空转。
        """
        async with self.sf() as s:
            await s.execute(delete(RoundStat).where(RoundStat.id == round_id))
            await s.commit()
