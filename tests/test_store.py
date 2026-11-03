"""状态机测试：用真实 SQLite 库跑，不 mock 数据库。

这些是核心业务规则，直接决定池子里放的是活代理还是僵尸：
- 首次通过必须立即入池（否则首轮结束池子为空）
- 连续失败到阈值要下线，再到阈值要拉黑
- 已下线的代理不能因一次失败就被退避逻辑提前拉回
- 死代理在冷却期内不能被重复挑选（否则浪费每轮预算）
"""
import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from proxypool.config import Config, PolicyConfig
from proxypool.models import Proxy, create_all, utcnow
from proxypool.store import ALIVE, BANNED, CANDIDATE, DEAD, NEW, Store
from proxypool.validator import ProbeResult


def ok_result(latency=500, anon="elite", https=False):
    r = ProbeResult()
    r.ok = True
    r.latency_ms = latency
    r.anonymity = anon
    r.exit_ip = "1.2.3.4"
    r.supports_https = https
    return r


def fail_result(err="timeout"):
    r = ProbeResult()
    r.ok = False
    r.error = err
    r.stage = "connect"
    r.latency_ms = 8000
    return r


@pytest.fixture
async def store(tmp_path):
    cfg = Config()
    cfg.database.url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    cfg.policy = PolicyConfig(
        promote_consecutive_ok=1,
        kill_consecutive_fail=3,
        ban_consecutive_fail=6,
        dead_cooldown=1800,
        score_up=12,
        score_down=30,
        score_max=100,
        score_floor=0,
    )
    cfg.scheduler.interval = 300
    engine = create_async_engine(cfg.db_url)
    await create_all(engine)
    sf = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    s = Store(sf, cfg)
    yield s
    await engine.dispose()


async def _one(store, protocol="http", ip="1.2.3.4", port=8080):
    await store.upsert_many([(protocol, ip, port)], "test")
    async with store.sf() as s:
        from sqlalchemy import select
        p = (await s.execute(select(Proxy))).scalars().first()
        return {"id": p.id, "protocol": p.protocol, "ip": p.ip, "port": p.port, "status": p.status}


class TestPromotion:
    async def test_first_success_promotes_immediately(self, store):
        """首次通过即入池 —— 这是修复过的缺陷：原来要求连续 2 次，导致首轮池子为空。"""
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        counts = await store.counts_by_status()
        assert counts[ALIVE] == 1
        assert counts[NEW] == 0

    async def test_promoted_proxy_servable(self, store):
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        batch = await store.get_batch(10)
        assert len(batch) == 1
        assert batch[0]["ip"] == "1.2.3.4"
        assert batch[0]["anonymity"] == "elite"

    async def test_metrics_recorded(self, store):
        row = await _one(store)
        await store.apply_results([(row, ok_result(latency=321))])
        batch = await store.get_batch(1)
        assert batch[0]["latency_ms"] == 321
        assert batch[0]["score"] == 62  # 50 + 12


class TestFailure:
    async def test_single_failure_keeps_candidate(self, store):
        """一次失败只是候选，不应直接淘汰。"""
        row = await _one(store)
        await store.apply_results([(row, fail_result())])
        counts = await store.counts_by_status()
        assert counts[CANDIDATE] == 1
        assert counts[DEAD] == 0

    async def test_consecutive_failures_kill(self, store):
        row = await _one(store)
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        counts = await store.counts_by_status()
        assert counts[DEAD] == 1

    async def test_enough_failures_ban(self, store):
        row = await _one(store)
        for _ in range(6):
            await store.apply_results([(row, fail_result())])
        counts = await store.counts_by_status()
        assert counts[BANNED] == 1

    async def test_alive_proxy_failing_once_stays_alive_not_candidate(self, store):
        """已入池的代理偶发一次失败，应保持 alive（由连续失败计数决定），不能立刻踢出候选。"""
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        await store.apply_results([(row, fail_result())])
        counts = await store.counts_by_status()
        assert counts[ALIVE] == 1

    async def test_recovery_after_dead(self, store):
        """下线后若再次通过，应能重新入池。"""
        row = await _one(store)
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        assert (await store.counts_by_status())[DEAD] == 1
        await store.apply_results([(row, ok_result())])
        counts = await store.counts_by_status()
        assert counts[ALIVE] == 1
        assert counts[DEAD] == 0

    async def test_target_throttled_does_not_count_as_proxy_failure(self, store):
        """回归：目标站 429/409 是目标站的错，不能把代理判死。

        httpbin.org 在高并发下会返回 429/409，
        旧逻辑一律当成代理失败，连续 2 次就把可用代理淘汰了。
        """
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        assert (await store.counts_by_status())[ALIVE] == 1

        for _ in range(5):
            throttled = ProbeResult()
            throttled.ok = False
            throttled.error = "status_429"
            throttled.stage = "read"
            throttled.target_throttled = True
            stats = await store.apply_results([(row, throttled)])
            assert stats["skipped"] == 1
            assert stats["failed"] == 0

        # 代理必须仍然是 alive，且失败计数没涨
        counts = await store.counts_by_status()
        assert counts[ALIVE] == 1, "代理被目标站限流误杀了"
        assert counts[DEAD] == 0
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            assert p.consecutive_fail == 0


class TestPicking:
    async def test_dead_not_picked_within_cooldown(self, store):
        """冷却期内的死代理不该被重复挑走，否则每轮预算都被浪费。"""
        row = await _one(store)
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        picked = await store.pick_for_check(100)
        assert all(p["id"] != row["id"] for p in picked)

    async def test_dead_never_picked_again(self, store):
        """dead 不再被挑去重测。

        实测依据：跑 3 小时后 3 万条 dead 全部冷却到期，每轮回来吃掉 20% 预算，
        而存活率 <2%。旧行为（冷却后重测）导致库从 2.2 万膨胀到 3.7 万条。
        正确做法：判死后不再重测，交给 cleanup 的 dead_purged 删掉。
        """
        row = await _one(store)
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        assert (await store.counts_by_status())[DEAD] == 1

        # 即便把 next_check 拨到过去，也不该再被挑中
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            p.next_check_at = utcnow() - dt.timedelta(seconds=9999)
            await s.commit()

        picked = await store.pick_for_check(100)
        assert all(p["id"] != row["id"] for p in picked), "dead 被重新挑去重测了"

    async def test_candidate_not_picked_before_backoff(self, store):
        """抖动代理要退避，不能在下一轮立刻又占用预算。"""
        row = await _one(store)
        await store.apply_results([(row, fail_result())])
        picked = await store.pick_for_check(100)
        assert all(p["id"] != row["id"] for p in picked)

    async def test_alive_picked_when_due(self, store):
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            p.next_check_at = utcnow() - dt.timedelta(seconds=5)
            await s.commit()
        picked = await store.pick_for_check(100)
        assert any(p["id"] == row["id"] for p in picked)

    async def test_new_entries_picked(self, store):
        await store.upsert_many([("http", "1.1.1.1", 80), ("socks5", "2.2.2.2", 1080)], "t")
        picked = await store.pick_for_check(100)
        assert len(picked) == 2

    async def test_candidate_gets_quota_despite_huge_new_backlog(self, store):
        """回归测试：new 堆积时不能让 candidate 饿死。

        真实踩过的缺陷：new 优先且配额不封顶时，2 万条 new 永远抽不完，
        已入池/候选的代理一次都轮不到，consecutive_fail 永远停在 1，
        淘汰机制（连续失败 3 次下线）完全失效。
        """
        # 1000 条 new
        await store.upsert_many(
            [("http", f"10.0.{i // 250}.{i % 250 + 1}", 80) for i in range(1000)], "t"
        )
        # 1 条已失败一次的 candidate
        await store.upsert_many([("http", "9.9.9.9", 8080)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "9.9.9.9"))).scalars().first()
            cand = {"id": p.id, "protocol": p.protocol, "ip": p.ip, "port": p.port, "status": p.status}
        await store.apply_results([(cand, fail_result())])
        # 拨到退避期结束
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "9.9.9.9"))).scalars().first()
            p.next_check_at = utcnow() - dt.timedelta(seconds=1)
            await s.commit()

        picked = await store.pick_for_check(100)
        assert any(r["id"] == cand["id"] for r in picked), "candidate 被 new 饿死了"
        # new 也要继续被抽到，不能反过来饿死 new
        assert sum(1 for r in picked if r["status"] == NEW) > 0

    async def test_pick_has_no_duplicates(self, store):
        """四个查询段之间不能重复挑同一个代理。"""
        await store.upsert_many([("http", f"8.8.8.{i}", 80) for i in range(1, 60)], "t")
        picked = await store.pick_for_check(100)
        ids = [r["id"] for r in picked]
        assert len(ids) == len(set(ids))

    async def test_hot_only_excludes_new_entries(self, store):
        """hot_only 精检轮必须只动热池，不能把预算花在没验证过的 new 上。

        这是"每 5 分钟全量复检热池"的核心保证：热池约 1500 条，
        混进 new 就永远验不完。
        """
        await store.upsert_many([("http", f"7.7.7.{i}", 80) for i in range(1, 200)], "t")
        await store.upsert_many([("http", "5.5.5.5", 9999)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "5.5.5.5"))).scalars().first()
            row = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                   "port": p.port, "status": p.status}
        await store.apply_results([(row, ok_result())])
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "5.5.5.5"))).scalars().first()
            p.next_check_at = utcnow() - dt.timedelta(seconds=1)
            await s.commit()

        picked = await store.pick_for_check(500, hot_only=True)
        assert all(r["status"] != NEW for r in picked), "精检轮混入了未验证的 new"
        assert any(r["ip"] == "5.5.5.5" for r in picked), "热池代理未被挑中"

    async def test_hot_pool_size(self, store):
        await store.upsert_many([("http", f"6.6.6.{i}", 80) for i in range(1, 10)], "t")
        await store.upsert_many([("http", "4.4.4.4", 9998)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "4.4.4.4"))).scalars().first()
            row = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                   "port": p.port, "status": p.status}
        await store.apply_results([(row, ok_result())])
        assert await store.hot_pool_size() == 1  # 只有 alive 那条，new 不算热池


class TestOrdering:
    async def test_https_capable_ranked_first(self, store):
        """回归：取代理时支持 HTTPS 的必须排前面。

        纯 score 排序会把"score=100 但只支持 HTTP"的代理排到最前，
        用 HTTPS 的爬虫拿到 10 个全废（实测默认取 10 个可用率仅 10%）。
        """
        # 一个低分但支持 HTTPS 的
        await store.upsert_many([
            ("http", "1.1.1.1", 80),    # 会被标成支持 HTTPS
            ("http", "2.2.2.2", 80),    # 只支持 HTTP，但 score 更高
        ], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p1 = (await s.execute(select(Proxy).where(Proxy.ip == "1.1.1.1"))).scalars().first()
            p2 = (await s.execute(select(Proxy).where(Proxy.ip == "2.2.2.2"))).scalars().first()
            row1 = {"id": p1.id, "protocol": p1.protocol, "ip": p1.ip, "port": p1.port, "status": p1.status}
            row2 = {"id": p2.id, "protocol": p2.protocol, "ip": p2.ip, "port": p2.port, "status": p2.status}

        def https_ok():
            r = ProbeResult()
            r.ok = True
            return r

        # p1 通过 + HTTPS 支持；p2 通过 3 次但不支持 HTTPS（score 更高）
        await store.apply_results([(row1, ok_result())], {row1["id"]: https_ok()})
        for _ in range(3):
            await store.apply_results([(row2, ok_result())])

        batch = await store.get_batch(10)
        assert batch[0]["ip"] == "1.1.1.1", (
            f"支持 HTTPS 的没排第一: {[(b['ip'], b['supports_https'], b['score']) for b in batch]}"
        )
        assert batch[0]["supports_https"] is True


class TestHttpsTracking:
    async def test_https_success_rate_tracked(self, store):
        """HTTPS 能力要记通过率，不能只记布尔：实测同一代理 TLS 通过率波动很大。"""
        row = await _one(store)

        def https_res(ok):
            r = ProbeResult()
            r.ok = ok
            return r

        await store.apply_results([(row, ok_result())], {row["id"]: https_res(True)})
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            assert p.https_try_count == 1
            assert p.https_ok_count == 1
            assert p.supports_https is True

        batch = await store.get_batch(1)
        assert batch[0]["https_success_rate"] == 1.0

    async def test_https_flag_revoked_on_low_rate(self, store):
        """连续 3 次里只成功 0-1 次要撤销 HTTPS 标记，避免给爬虫不可靠承诺。"""
        row = await _one(store)

        def https_res(ok):
            r = ProbeResult()
            r.ok = ok
            return r

        await store.apply_results([(row, ok_result())], {row["id"]: https_res(True)})
        for _ in range(3):
            await store.apply_results([(row, ok_result())], {row["id"]: https_res(False)})
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            assert p.https_try_count == 4
            assert p.https_ok_count == 1
            assert p.supports_https is False


class TestDeadLifecycle:
    async def test_dead_proxy_with_many_fails_gets_banned(self, store):
        """回归：dead 状态下继续失败，到阈值必须 ban，不能无限循环。

        实测：跑 3 小时后 dead 堆到 35738 条，全部冷却到期，
        每轮回来消耗预算，永远在 dead↔candidate 之间打转。
        """
        row = await _one(store)
        # 先连续失败到 dead（fixture 阈值 3）
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        assert (await store.counts_by_status())[DEAD] == 1

        # dead 状态下继续失败到 ban 阈值（6）：再失败 3 次
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        counts = await store.counts_by_status()
        assert counts[BANNED] == 1, f"dead 代理没被 ban，仍在循环: {counts}"
        assert counts[DEAD] == 0

    async def test_old_dead_proxies_purged_from_db(self, store):
        """回归：连续失败多、久未检查的 dead 必须真删，不能只留在库里。"""
        await store.upsert_many([("http", "6.6.6.6", 9997)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "6.6.6.6"))).scalars().first()
            row = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                   "port": p.port, "status": p.status}
        # fixture 的 kill_consecutive_fail=3，失败 3 次即进 dead
        for _ in range(3):
            await store.apply_results([(row, fail_result())])
        assert (await store.counts_by_status())[DEAD] == 1

        store.cfg.cleanup.dead_purge_min_fails = 3
        store.cfg.cleanup.dead_purge_seconds = 60
        # 把 last_checked 拨到过去
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "6.6.6.6"))).scalars().first()
            p.last_checked_at = utcnow() - dt.timedelta(seconds=120)
            await s.commit()

        out = await store.cleanup()
        assert out["dead_purged"] >= 1, "超龄 dead 没被清掉"
        async with store.sf() as s:
            from sqlalchemy import select
            left = (await s.execute(
                select(Proxy).where(Proxy.ip == "6.6.6.6"))).scalars().first()
            assert left is None, "dead 代理仍在库中"

    async def test_tombstone_blocks_refetch(self, store):
        """回归：判死删掉的代理必须被墓碑拦住，不能重新灌回库。

        实测：purge 删掉 13809 条 dead 后，下一轮抓取把它们全当"新"重新入库，
        形成「验死→删除→重抓→再验」死循环，白烧 CPU 和带宽。
        """
        await store.upsert_many([("http", "7.7.7.7", 9995)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "7.7.7.7"))).scalars().first()
            row = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                   "port": p.port, "status": p.status}
        for _ in range(3):  # fixture: kill_consecutive_fail=3
            await store.apply_results([(row, fail_result())])

        store.cfg.cleanup.dead_purge_min_fails = 2
        store.cfg.cleanup.dead_purge_seconds = 0
        out = await store.cleanup()
        assert out["dead_purged"] >= 1
        assert await store.total() == 0, "dead 应已删除"

        # 源重新吐回来 → 必须被墓碑拦住
        added = await store.upsert_many([("http", "7.7.7.7", 9995)], "t")
        assert added == 0, f"墓碑没拦住，重新入库了 {added} 条"
        assert await store.total() == 0

    async def test_tombstone_expires_and_allows_refetch(self, store):
        """墓碑过期后允许重新抓取（避免永久拉黑误杀）。"""
        await store.upsert_many([("http", "8.8.8.8", 9994)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "8.8.8.8"))).scalars().first()
            row = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                   "port": p.port, "status": p.status}
        for _ in range(3):  # fixture: kill_consecutive_fail=3
            await store.apply_results([(row, fail_result())])
        store.cfg.cleanup.dead_purge_min_fails = 2
        store.cfg.cleanup.dead_purge_seconds = 0
        await store.cleanup()

        # 把墓碑时间拨到过期
        store.cfg.cleanup.tombstone_seconds = 0
        await store.cleanup()  # 清过期墓碑
        added = await store.upsert_many([("http", "8.8.8.8", 9994)], "t")
        assert added == 1, "墓碑过期后应该允许重新抓取"

    async def test_cleanup_returns_wal_key(self, store):
        """cleanup 返回结构必须稳定（上层会读 dead_purged / banned_purged）。"""
        out = await store.cleanup()
        for key in ("logs_deleted", "stale_deleted", "overflow_deleted",
                    "cold_capped", "dead_purged", "banned_purged"):
            assert key in out, f"cleanup 缺字段 {key}"

    async def test_old_banned_proxies_purged(self, store):
        """banned 超过保留期必须删，否则一天累积上千条白占库。"""
        await store.upsert_many([("http", "5.5.5.5", 9996)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "5.5.5.5"))).scalars().first()
            row = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                   "port": p.port, "status": p.status}
        # 失败到 ban（fixture: ban=6）
        for _ in range(6):
            await store.apply_results([(row, fail_result())])
        assert (await store.counts_by_status())[BANNED] == 1

        store.cfg.cleanup.banned_retention_seconds = 60
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.ip == "5.5.5.5"))).scalars().first()
            p.last_checked_at = utcnow() - dt.timedelta(seconds=120)
            await s.commit()

        out = await store.cleanup()
        assert out["banned_purged"] >= 1, "超龄 banned 没被清掉"


class TestUpsert:
    async def test_dedup_on_identity(self, store):
        n1 = await store.upsert_many([("http", "1.1.1.1", 80)], "a")
        n2 = await store.upsert_many([("http", "1.1.1.1", 80)], "b")
        assert n1 == 1
        assert n2 == 0
        assert await store.total() == 1

    async def test_same_ip_different_protocol_is_distinct(self, store):
        await store.upsert_many([("http", "1.1.1.1", 80), ("socks5", "1.1.1.1", 80)], "a")
        assert await store.total() == 2

    async def test_source_count_increments(self, store):
        await store.upsert_many([("http", "1.1.1.1", 80)], "a")
        await store.upsert_many([("http", "1.1.1.1", 80)], "b")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            assert p.source_count == 2


class TestCleanup:
    async def test_stale_never_ok_deleted(self, store):
        await store.upsert_many([("http", "1.1.1.1", 80)], "t")
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            p.created_at = utcnow() - dt.timedelta(hours=72)
            await s.commit()
        out = await store.cleanup()
        assert out["stale_deleted"] == 1
        assert await store.total() == 0

    async def test_alive_never_deleted_by_stale_rule(self, store):
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy))).scalars().first()
            p.created_at = utcnow() - dt.timedelta(hours=72)
            await s.commit()
        await store.cleanup()
        assert await store.total() == 1

    async def test_cold_cap_only_prunes_new_not_candidate(self, store):
        """回归：冷池超限只能删 NEW，不能删 CANDIDATE。

        真实踩过的缺陷：把 candidate 一起算进淘汰范围，
        导致 candidate 被清成 0 —— 而那批是"验证过、只失败 1 次"的，
        价值高于从没活过的 new，删掉等于把辛苦验出来的结果扔了。
        """
        store.cfg.cleanup.cold_pool_limit = 5
        # 20 条 new
        await store.upsert_many([("http", f"9.9.9.{i}", 80) for i in range(1, 21)], "t")
        # 3 条 candidate（失败一次）
        await store.upsert_many([("http", f"3.3.3.{i}", 80) for i in range(1, 4)], "t")
        for i in range(1, 4):
            async with store.sf() as s:
                from sqlalchemy import select
                p = (await s.execute(select(Proxy).where(Proxy.ip == f"3.3.3.{i}"))).scalars().first()
                r = {"id": p.id, "protocol": p.protocol, "ip": p.ip,
                     "port": p.port, "status": p.status}
            await store.apply_results([(r, fail_result())])
        assert (await store.counts_by_status())[CANDIDATE] == 3

        out = await store.cleanup()
        assert out["cold_capped"] > 0
        counts = await store.counts_by_status()
        assert counts[NEW] <= 5 + 20, f"new 未裁剪: {counts[NEW]}"
        assert counts[CANDIDATE] == 3, f"candidate 被误删了: {counts[CANDIDATE]}"

    async def test_old_logs_deleted(self, store):
        row = await _one(store)
        await store.apply_results([(row, ok_result())])
        async with store.sf() as s:
            from sqlalchemy import update
            from proxypool.models import CheckLog
            await s.execute(update(CheckLog).values(checked_at=utcnow() - dt.timedelta(days=10)))
            await s.commit()
        out = await store.cleanup()
        assert out["logs_deleted"] == 1


class TestBannedFate:
    async def test_banned_has_no_next_check(self, store):
        """banned 不再复检，next_check_at 应留空。

        曾经给它设 ban_cooldown（7 天），但 pick_for_check 根本不挑 banned，
        这个值没有任何作用，只会误导后来人以为它会复活。
        """
        row = await _one(store)
        for _ in range(6):                       # ban_consecutive_fail=6
            await store.apply_results([(row, fail_result())])
        async with store.sf() as s:
            from sqlalchemy import select
            p = (await s.execute(select(Proxy).where(Proxy.id == row["id"]))).scalars().one()
            assert p.status == BANNED
            assert p.next_check_at is None, "banned 不该有下次检查时间"

    async def test_banned_never_picked(self, store):
        """banned 永远不该被 pick_for_check 挑走。"""
        row = await _one(store)
        for _ in range(6):
            await store.apply_results([(row, fail_result())])
        picked = await store.pick_for_check(1000)
        assert not [r for r in picked if r["status"] == BANNED]


class TestTransparentFilter:
    async def test_transparent_excluded_by_default(self, store):
        """默认不返回透明代理，它们会把真实 IP 透给目标站。"""
        row = await _one(store)
        r = ok_result()
        r.anonymity = "transparent"
        await store.apply_results([(row, r)])

        got = await store.get_batch(10)
        assert not [p for p in got if p["anonymity"] == "transparent"]

    async def test_transparent_returned_when_asked(self, store):
        row = await _one(store)
        r = ok_result()
        r.anonymity = "transparent"
        await store.apply_results([(row, r)])

        got = await store.get_batch(10, anonymity="transparent")
        assert [p for p in got if p["anonymity"] == "transparent"]

    async def test_explicit_switch_allows_transparent(self, store):
        row = await _one(store)
        r = ok_result()
        r.anonymity = "transparent"
        await store.apply_results([(row, r)])

        got = await store.get_batch(10, exclude_transparent=False)
        assert [p for p in got if p["anonymity"] == "transparent"]
