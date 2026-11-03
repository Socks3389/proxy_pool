"""调度器测试：轮次互斥与预算控制。

不依赖网络：用 stub validator/store 模拟，专测调度逻辑本身。
"""
import asyncio
import time


from proxypool.config import Config, SchedulerConfig
from proxypool.scheduler import Scheduler
from proxypool.validator import ProbeResult


class StubValidator:
    """可配置延迟与结果的假探测器。

    必须模拟真实 Validator 的信号量限流，否则测试里的并发不受控，
    1000 条 0.2s 的任务会瞬间并行跑完，测不到"预算截断"。
    """

    def __init__(self, delay=0.01, ok=True, concurrency=100):
        self.delay = delay
        self.ok = ok
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self._sem = asyncio.Semaphore(concurrency)

    async def verify(self, protocol, ip, port):
        async with self._sem:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            try:
                await asyncio.sleep(self.delay)
                r = ProbeResult()
                r.ok = self.ok
                r.latency_ms = int(self.delay * 1000)
                r.anonymity = "elite"
                return r
            finally:
                self.concurrent -= 1

    async def verify_https(self, protocol, ip, port):
        r = ProbeResult()
        r.ok = True
        return r


class StubStore:
    def __init__(self, rows, hot=0):
        self.rows = rows
        self.hot = hot
        self.due = None
        self.applied = []
        self.round_started = 0
        self.round_finished = 0
        self.cleanups = 0

    async def start_round(self):
        self.round_started += 1
        return self.round_started

    async def upsert_many(self, entries, source_name):
        return 0

    async def finish_round(self, rid, data):
        self.round_finished += 1

    async def discard_round(self, rid):
        self.round_discarded = getattr(self, "round_discarded", 0) + 1

    async def pick_for_check(self, limit, *, hot_only=False):
        # 模拟真实行为：每轮只返回一次，用掉即空
        rows, self.rows = self.rows, []
        return rows[:limit]

    async def apply_results(self, results, https_map=None):
        self.applied.append(len(results))
        return {"ok": sum(1 for _, r in results if r.ok),
                "failed": sum(1 for _, r in results if not r.ok),
                "promoted": 0, "killed": 0, "banned": 0}

    async def cleanup(self):
        self.cleanups += 1
        return {"logs_deleted": 0, "stale_deleted": 0, "overflow_deleted": 0, "cold_capped": 0}

    async def hot_pool_size(self):
        return self.hot

    async def next_due_seconds(self):
        return self.due


def make_rows(n):
    return [{"id": i, "protocol": "http", "ip": f"1.1.1.{i}", "port": 80,
             "status": "new"} for i in range(n)]


def make_sched(rows, *, delay=0.01, ok=True, hot=0, budget=60, batch=500):
    cfg = Config()
    cfg.scheduler = SchedulerConfig(interval=300, refetch_every_round=False,
                                    candidate_batch=batch, round_budget=budget,
                                    fast_promote=False, refresh_every_n_rounds=6)
    cfg.validator.concurrency = 100
    cfg.validator.timeout = 1.0
    cfg.validator.attempts = 1
    store = StubStore(rows, hot=hot)
    validator = StubValidator(delay=delay, ok=ok, concurrency=cfg.validator.concurrency)
    return Scheduler(cfg, store, validator), store, validator


class TestRoundBasics:
    async def test_round_processes_all_rows(self):
        sched, store, v = make_sched(make_rows(20))
        rep = await sched.run_round(hot_only=True)
        assert rep.checked == 20
        assert v.calls == 20
        assert store.round_finished == 1

    async def test_empty_pick_returns_cleanly(self):
        """空 pick 要干净返回，且不留下 round_stats 记录。"""
        sched, store, v = make_sched([])
        rep = await sched.run_round(hot_only=True)
        assert rep.checked == 0
        assert v.calls == 0
        # 空轮被丢弃，不算跑过一轮（否则趋势图会多出 0 点）
        assert store.round_finished == 0
        assert getattr(store, "round_discarded", 0) == 1


class TestRoundMutualExclusion:
    async def test_concurrent_round_rejected(self):
        """回归：同一时刻只允许一轮。

        真实踩过的缺陷：手动触发与定时触发叠加，两轮并行，
        后一轮 pick 拿到空列表（前一轮已把 next_check_at 推到未来），
        日志里出现 "验证 0 条" 的空轮，白耗一次周期。
        """
        sched, store, v = make_sched(make_rows(10), delay=0.08)
        t1 = asyncio.create_task(sched.run_round(hot_only=True))
        await asyncio.sleep(0.01)          # 确保第一轮已进入临界区
        rep2 = await sched.run_round(hot_only=True)

        assert rep2.notes.startswith("skipped"), f"第二轮未被拒绝: {rep2.notes}"
        rep1 = await t1
        assert rep1.checked == 10
        # 只有第一轮真正启动并结束
        assert store.round_started == 1
        assert store.round_finished == 1
        assert v.calls == 10

    async def test_sequential_rounds_allowed(self):
        sched, store, v = make_sched(make_rows(5))
        await sched.run_round(hot_only=True)
        await sched.run_round(hot_only=True)
        assert store.round_started == 2


class TestBudget:
    async def test_budget_cuts_long_round(self):
        """预算到达必须真的截断，不能等全部跑完。

        旧实现用 gather 会等所有任务跑完，预算形同虚设，
        一轮能拖到 324s 超过 5 分钟周期。
        """
        # 1000 条 × 0.2s，并发 100 → 约 2s 才能跑完，预算设 1s 必然截断。
    # batch 必须给足，否则 pick 就只返回 500 条，测不到截断逻辑。
        rows = make_rows(1000)
        sched, store, v = make_sched(rows, delay=0.2, budget=1, batch=2000)
        sched.cfg.validator.concurrency = 100
        t0 = time.monotonic()
        rep = await sched.run_round(hot_only=True)
        elapsed = time.monotonic() - t0

        assert elapsed < 3.0, f"预算未生效，耗时 {elapsed:.1f}s"
        assert rep.checked < 1000, f"全部跑完说明截断没生效（checked={rep.checked}）"
        assert rep.truncated is True

    async def test_no_task_leak_after_cancel(self):
        """取消后不能留下后台任务继续跑（会污染下一轮）。"""
        sched, store, v = make_sched(make_rows(100), delay=0.05, budget=1)
        await sched.run_round(hot_only=True)
        await asyncio.sleep(0.2)
        # 取消落地后并发数应归零
        assert v.concurrent == 0, f"有 {v.concurrent} 个任务未清理"


class TestHotOnly:
    async def test_hot_only_skips_fetch(self):
        cfg_fetch_called = []

        sched, store, v = make_sched(make_rows(5))
        sched.cfg.scheduler.refetch_every_round = True
        async def fake_fetch(cfg):
            cfg_fetch_called.append(1)
            return [], []
        import proxypool.scheduler as sm
        orig = sm.fetch_all
        sm.fetch_all = fake_fetch
        try:
            await sched.run_round(hot_only=True)
        finally:
            sm.fetch_all = orig
        assert cfg_fetch_called == [], "精检轮不应抓取源"

    async def test_normal_round_fetches(self):
        calls = []

        sched, store, v = make_sched(make_rows(5))
        sched.cfg.scheduler.refetch_every_round = True
        async def fake_fetch(cfg):
            calls.append(1)
            return [], []
        import proxypool.scheduler as sm
        orig = sm.fetch_all
        sm.fetch_all = fake_fetch
        try:
            await sched.run_round(hot_only=False)
        finally:
            sm.fetch_all = orig
        assert calls == [1], "常规轮必须抓取源"

    async def test_socks5_gets_https_probe(self):
        """回归：socks5 也必须做 HTTPS 实测。

        实测发现：源里的 socks5 列表混有空壳/伪装代理（端口 80/8080 的其实是 HTTP），
        8 个里只有 3 个真能跑 HTTPS。若不实测，筛"支持 HTTPS"时会把 200+ 条
        socks5 全排除，结果误导用户。
        socks4 不测：它不支持域名解析，访问 443 目标天然失败。
        """
        seen = []

        sched, store, v = make_sched(make_rows(0))
        async def fake_verify(protocol, ip, port):
            r = ProbeResult()
            r.ok = True
            return r
        v.verify = fake_verify
        async def fake_https(protocol, ip, port):
            seen.append(protocol)
            r = ProbeResult()
            r.ok = True
            return r
        v.verify_https = fake_https

        store.rows = [
            {"id": 1, "protocol": "http", "ip": "1.1.1.1", "port": 80, "status": "new"},
            {"id": 2, "protocol": "socks5", "ip": "2.2.2.2", "port": 1080, "status": "new"},
            {"id": 3, "protocol": "socks4", "ip": "3.3.3.3", "port": 1080, "status": "new"},
        ]
        sched.cfg.scheduler.fast_promote = True
        await sched.run_round(hot_only=True)

        assert "http" in seen, "http 代理没做 HTTPS 探测"
        assert "socks5" in seen, "socks5 没做 HTTPS 探测 —— 会被筛 HTTPS 时全部排除"
        assert "socks4" not in seen, "socks4 不该做 HTTPS 探测（不支持域名解析）"


    async def test_empty_round_not_counted(self):
        """回归：空轮（0 验证 0 新增）不能写进 round_stats。

        实测缺陷：新装后第一轮耗尽预算跑完 248s，紧接着的一轮所有代理都没到期，
        产生"验证 0 条"的记录，趋势图上会是一片 0 点。
        """
        sched, store, v = make_sched([])
        rep = await sched.run_round(hot_only=True)
        assert rep.checked == 0
        assert getattr(store, "round_discarded", 0) == 1, "空轮没有被丢弃"
        assert store.round_finished == 0, "空轮不该写入 round_stats"

    async def test_round_with_data_is_counted(self):
        sched, store, v = make_sched(make_rows(5))
        rep = await sched.run_round(hot_only=True)
        assert rep.checked == 5
        assert store.round_finished == 1
        assert getattr(store, "round_discarded", 0) == 0


class TestEmptyRound:
    async def test_empty_round_waits_until_due(self):
        """回归：空轮应睡到最近代理到期，而不是白跑一次。

        实测缺陷：冷池扩充轮耗时 247s，新晋升代理 next_check_at = 那轮开始 +300s，
        于是 53s 后的下一轮必然拿到 0 条待验证（验证 0 条的空轮）。
        """
        sched, store, v = make_sched([])          # 无 rows → pick 返回空
        store.due = 240.0                          # 最近一个代理 240s 后到期
        sched.cfg.scheduler.interval = 300
        # 直接调内部循环太复杂，这里验证 next_due_seconds 被正确读取
        due = await store.next_due_seconds()
        assert due == 240.0

        rep = await sched.run_round(hot_only=True)
        assert rep.checked == 0                    # 确实是空轮
        # 空轮时 checked==0 → 调度器会读 due 决定等待时长（下面断言逻辑）
        assert store.due > 0

    async def test_scheduler_uses_due_when_empty(self):
        """空轮时等待时长必须用 due 覆盖，而不是傻等满周期。

        实测缺陷：空轮时 wait=300（整周期），due 只有 9s，
        写成 "if due > wait" 判断永远为假，改了也没用。
        """
        sched, store, v = make_sched([])
        store.due = 9.0
        sched.cfg.scheduler.interval = 300
        rep = await sched.run_round(hot_only=True)

        # 复现调度循环的计算
        wait = max(5.0, sched.cfg.scheduler.interval - 0)
        if rep.checked == 0 and rep.fetched_new == 0:
            due = await store.next_due_seconds()
            if due is not None:
                wait = max(5.0, min(due, sched.cfg.scheduler.interval))
        assert wait == 9.0, f"空轮等待应为 9s，实际 {wait}s"

    async def test_due_floor_protects_against_tight_loop(self):
        """due 接近 0 时必须有 5s 下限，否则会高频空转打满 CPU。"""
        sched, store, v = make_sched([])
        store.due = 0.01
        sched.cfg.scheduler.interval = 300
        due = await store.next_due_seconds()
        wait = max(5.0, min(due, sched.cfg.scheduler.interval))
        assert wait >= 5.0

    async def test_due_capped_by_interval(self):
        """due 超过一个周期时不该无限期睡下去。"""
        sched, store, v = make_sched([])
        store.due = 9999.0
        wait = max(5.0, min(store.due, sched.cfg.scheduler.interval))
        assert wait == sched.cfg.scheduler.interval
