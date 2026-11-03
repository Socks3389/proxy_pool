"""API 层测试：重点是别让接口副作用污染长期运行的配置。

不跑真实 lifespan —— 那会连数据库、探测出口 IP、启动调度器，
既慢又会把测试用的 fake 覆盖掉。这里只挂路由。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from proxypool import api
from proxypool.config import Config
from proxypool.scheduler import RoundReport


class FakeStore:
    def __init__(self, items=None):
        self.items = items or []

    async def get_batch(self, n, **kw):
        # 真实实现在 n<=0 时返回全部，桩要跟着对齐，
        # 否则 items[:0] 永远是空列表，测出来的行为是假的。
        if n <= 0:
            return list(self.items)
        return self.items[:n]


class FakeScheduler:
    running = False

    def __init__(self):
        self.calls = []

    async def run_round(self, **kw):
        self.calls.append(kw)
        return RoundReport(round_id=1)


@pytest.fixture
def client():
    cfg = Config()
    sched = FakeScheduler()

    api.state.cfg = cfg
    api.state.scheduler = sched          # type: ignore[assignment]
    api.state.store = FakeStore()        # type: ignore[assignment]

    bare = FastAPI()
    kept = tuple(r for r in api.app.routes
                 if getattr(r, "path", "").startswith(("/get", "/health", "/api", "/report")))
    bare.routes.extend(kept)

    with TestClient(bare) as c:
        yield c, cfg, sched


class TestTrigger:
    def test_trigger_does_not_mutate_config(self, client):
        """POST /api/trigger?fetch=0 不能改掉长期配置。

        曾经直接写 cfg.scheduler.refetch_every_round，
        调一次之后所有定时轮都不再抓源，直到重启服务。
        """
        c, cfg, _sched = client
        before = cfg.scheduler.refetch_every_round

        r = c.post("/api/trigger", params={"fetch": 0})
        assert r.status_code == 200
        assert cfg.scheduler.refetch_every_round == before, "配置被永久改写了"

    def test_fetch_flag_maps_to_hot_only(self, client):
        """fetch 只作用于这一次触发，且转成 hot_only 传给调度器。"""
        _c, cfg, sched = client
        before = cfg.scheduler.refetch_every_round

        asyncio.run(api._run_once(fetch=False))
        asyncio.run(api._run_once(fetch=True))
        assert sched.calls[0]["hot_only"] is True
        assert sched.calls[1]["hot_only"] is False
        assert cfg.scheduler.refetch_every_round == before

    def test_running_scheduler_rejects(self, client):
        c, _cfg, sched = client
        sched.running = True
        assert c.post("/api/trigger").json()["ok"] is False


class TestGetProxy:
    def test_single_proxy_returns_list_shape(self, client):
        """n=1 也要返回 {count, proxies}，别让客户端写两套分支。"""
        c, _cfg, _sched = client
        api.state.store = FakeStore([                     # type: ignore[assignment]
            {"ip": "1.2.3.4", "port": 8080, "url": "http://1.2.3.4:8080"}
        ])
        data = c.get("/get", params={"n": 1}).json()
        assert data["count"] == 1
        assert isinstance(data["proxies"], list)
        assert data["proxies"][0]["ip"] == "1.2.3.4"

    def test_text_format_is_plain(self, client):
        """text 格式必须是纯文本，不能带 JSON 引号和转义。"""
        c, _cfg, _sched = client
        api.state.store = FakeStore([                     # type: ignore[assignment]
            {"ip": "1.2.3.4", "port": 8080},
            {"ip": "5.6.7.8", "port": 3128},
        ])
        r = c.get("/get", params={"format": "text", "n": 2})
        assert r.text == "1.2.3.4:8080\n5.6.7.8:3128"
        assert r.headers["content-type"].startswith("text/plain")

    def test_protocol_path_returns_text(self, client):
        """路径形式的 /get/socks5 也要返回纯文本。

        曾经 500：内部转发到 get_proxy 时漏传 https_rate，
        而它的默认值是 Query(...) 对象不是 None，比较时 TypeError。
        """
        c, _cfg, _sched = client
        api.state.store = FakeStore([                     # type: ignore[assignment]
            {"ip": "1.2.3.4", "port": 1080}
        ])
        r = c.get("/get/socks5")
        assert r.status_code == 200
        assert r.text == "1.2.3.4:1080"
        assert r.headers["content-type"].startswith("text/plain")

    def test_protocol_path_rejects_unknown(self, client):
        c, _cfg, _sched = client
        assert c.get("/get/ftp").status_code == 400

    def test_empty_pool_returns_503(self, client):
        c, _cfg, _sched = client
        api.state.store = FakeStore([])                   # type: ignore[assignment]
        assert c.get("/get").status_code == 503


class TestReportBadProxies:
    """爬虫上报失效代理，提前排入下一轮复检。"""

    def test_report_accepted(self, client):
        c, _cfg, _sched = client

        async def fake_report(targets, reason="client_reported"):
            return {"matched": len(targets), "queued": len(targets)}

        api.state.store = type("S", (), {
            "report_failures": staticmethod(fake_report),
        })()
        r = c.post("/report", json={"proxies": ["1.2.3.4:8080", "5.6.7.8:3128"]})
        assert r.status_code == 200
        assert r.json()["matched"] == 2

    def test_rejects_empty_list(self, client):
        c, _cfg, _sched = client
        assert c.post("/report", json={"proxies": []}).status_code == 400

    def test_rejects_too_many(self, client):
        c, _cfg, _sched = client
        many = [f"1.2.3.{i}:8080" for i in range(300)]
        assert c.post("/report", json={"proxies": many}).status_code == 400

    def test_skips_malformed_entries(self, client):
        c, _cfg, _sched = client
        seen = {}

        async def fake_report(targets, reason="client_reported"):
            seen["targets"] = targets
            return {"matched": len(targets), "queued": len(targets)}

        api.state.store = type("S", (), {
            "report_failures": staticmethod(fake_report),
        })()
        r = c.post("/report", json={
            "proxies": ["1.2.3.4:8080", "bad", "5.6.7.8", "9.9.9.9:70000"],
        })
        assert r.status_code == 200
        assert seen["targets"] == [("1.2.3.4", 8080)]


class TestSplitHostPort:
    """地址拆分要兼容 IPv6，别用 rpartition 想当然。"""

    def test_basic(self):
        assert api._split_host_port("1.2.3.4:8080") == ("1.2.3.4", 8080)

    def test_ipv6_bracketed(self):
        assert api._split_host_port("[::1]:8080") == ("::1", 8080)

    def test_ipv6_full(self):
        assert api._split_host_port("[2001:db8::1]:3128") == ("2001:db8::1", 3128)

    def test_no_port_returns_none(self):
        assert api._split_host_port("1.2.3.4") is None

    def test_bare_ipv6_returns_none(self):
        assert api._split_host_port("2001:db8::1") is None

    def test_bad_port(self):
        assert api._split_host_port("1.2.3.4:99999") is None
        assert api._split_host_port("1.2.3.4:abc") is None

    def test_empty(self):
        assert api._split_host_port("") is None
        assert api._split_host_port("   ") is None


class TestUnlimitedN:
    """n 不设上限：要多少给多少，能拿多少由池子决定。

    用户明确要求过：最大数量从 100 调到无上限，但不能超过实际可用数量。
    """

    def test_n_zero_returns_all(self, client):
        """n=0 表示全部可用。"""
        c, _cfg, _sched = client
        api.state.store = FakeStore([                     # type: ignore[assignment]
            {"ip": f"10.0.0.{i}", "port": 8080, "url": f"http://10.0.0.{i}:8080"}
            for i in range(1, 4)
        ])
        data = c.get("/get", params={"n": 0}).json()
        assert data["count"] == 3

    def test_n_above_100_allowed(self, client):
        """n=250 不该再被 100 截断。"""
        c, _cfg, _sched = client
        api.state.store = FakeStore([                     # type: ignore[assignment]
            {"ip": f"10.1.{i // 250}.{i % 250}", "port": 8080,
             "url": f"http://10.1.{i // 250}.{i % 250}:8080"}
            for i in range(250)
        ])
        data = c.get("/get", params={"n": 250}).json()
        assert data["count"] == 250, f"n=250 应返回 250 条，实际 {data['count']}"

    def test_huge_n_capped_by_pool_size(self, client):
        """n 远超可用数时返回全部，不报错也不补空。"""
        c, _cfg, _sched = client
        api.state.store = FakeStore([                     # type: ignore[assignment]
            {"ip": "10.2.0.1", "port": 8080, "url": "http://10.2.0.1:8080"},
            {"ip": "10.2.0.2", "port": 8080, "url": "http://10.2.0.2:8080"},
        ])
        data = c.get("/get", params={"n": 999999}).json()
        assert data["count"] == 2

    def test_negative_n_rejected(self, client):
        """负数是不合法的，FastAPI 应该挡下来。"""
        c, _cfg, _sched = client
        assert c.get("/get", params={"n": -1}).status_code == 422
