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
                 if getattr(r, "path", "").startswith(("/get", "/health", "/api")))
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
