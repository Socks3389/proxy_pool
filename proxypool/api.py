"""HTTP API：对外取代理 + 对内管理。

对外接口（给爬虫业务用）：
    GET /get                  随机取 1 个可用代理
    GET /get?n=10             取一批
    GET /get?protocol=socks5&anonymity=elite&https=1&max_latency=3000
    GET /get?format=text      纯 ip:port 文本，方便直接喂给爬虫

对内接口（管理台用）：
    GET  /api/stats           池子总览
    GET  /api/proxies         分页列表
    GET  /api/rounds          最近轮次
    GET  /api/errors          失败原因聚合
    POST /api/trigger         立即跑一轮
    POST /api/trigger-check   只验证不抓取
    POST /api/proxies/delete  批量删除
    GET  /api/config          当前生效配置（脱敏）
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import Config, load_config
from .models import Proxy, create_all
from .scheduler import Scheduler
from .store import ALIVE, Store
from .validator import Validator

log = logging.getLogger("proxypool.api")
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


class AppState:
    cfg: Config
    engine: object
    sf: async_sessionmaker[AsyncSession]
    store: Store
    validator: Validator
    scheduler: Scheduler
    real_ip: str | None = None


state = AppState()


def get_cfg() -> Config:
    return state.cfg


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """管理接口鉴权。配置为空时不校验（只建议本机监听时留空）。"""
    token = state.cfg.api.admin_token
    if token and x_admin_token != token:
        raise HTTPException(status_code=401, detail="invalid admin token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    state.cfg = cfg
    (BASE_DIR / "data").mkdir(exist_ok=True)

    # 抬 fd 上限：并发 400 需要 ~2600 个 fd，默认 soft limit 只有 4096 且被其他占用
    try:
        from .__main__ import ensure_fd_limit
        ensure_fd_limit(cfg.validator.concurrency)
    except Exception:
        log.warning("fd 上限检查失败，可能影响高并发探测")

    engine = create_async_engine(
        cfg.db_url, echo=cfg.database.echo, future=True,
        pool_pre_ping=True,
        **({"pool_size": cfg.database.pool_size, "max_overflow": 10}
           if not cfg.db_url.startswith("sqlite") else {}),
    )
    state.engine = engine
    await create_all(engine)
    state.sf = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    state.store = Store(state.sf, cfg)

    validator = Validator(cfg.validator, real_ip=None)
    state.validator = validator
    # 出口 IP 是匿名性判定的基准，启动时先测出来
    try:
        state.real_ip = await validator.detect_real_ip()
        validator.real_ip = state.real_ip
        log.info("本机出口 IP: %s", state.real_ip)
    except Exception:
        log.warning("出口 IP 探测失败，匿名性判定将退化为仅看 header")

    state.scheduler = Scheduler(cfg, state.store, validator)
    await state.scheduler.start()

    yield

    await state.scheduler.stop()
    await engine.dispose()


app = FastAPI(title="ProxyPool", version="1.0.0", lifespan=lifespan)


# ---------------- 对外取代理 ----------------

async def _fetch_proxies(
    n: int,
    protocol: str | None = None,
    anonymity: str | None = None,
    https_only: bool = False,
    max_latency: int | None = None,
    min_score: int | None = None,
    https_rate: float | None = None,
    allow_transparent: bool = False,
) -> list[dict]:
    """两个取代理路由共用的取数逻辑。

    抽出来是为了避免路由互相调用 —— 直接调带 Query(...) 默认值的路由函数
    不会经过 FastAPI 的参数解析，漏传的参数会保持 Query 对象，
    后续比较直接 TypeError。

    n 没有上限，池子里有多少符合条件就返回多少。调用方要多少给多少，
    真正能拿到多少由池子规模和筛选条件决定。
    """
    items = await state.store.get_batch(
        n, protocol=protocol, anonymity=anonymity,
        https_only=https_only, max_latency=max_latency, min_score=min_score,
        min_https_rate=https_rate,
        exclude_transparent=not allow_transparent,
    )
    if not items:
        raise HTTPException(status_code=503, detail="池中暂无可用代理")
    return items


@app.get("/get")
async def get_proxy(
    n: int = Query(1, ge=0, description="取几个；0 表示全部可用的"),
    protocol: str | None = Query(None, description="http/https/socks4/socks5"),
    anonymity: str | None = Query(None, description="elite/anonymous/transparent"),
    https: int = Query(0, ge=0, le=1, description="只要支持 HTTPS 的"),
    max_latency: int | None = Query(None, ge=1, description="延迟上限 ms"),
    min_score: int | None = Query(None, ge=0, le=100, description="最低评分"),
    https_rate: float | None = Query(None, ge=0, le=1, description="HTTPS 实测通过率下限 0-1"),
    allow_transparent: int = Query(0, ge=0, le=1, description="允许返回透明代理（会暴露真实 IP）"),
    format: str = Query("json", description="json|text|list"),
):
    items = await _fetch_proxies(
        n, protocol=protocol, anonymity=anonymity, https_only=bool(https),
        max_latency=max_latency, min_score=min_score, https_rate=https_rate,
        allow_transparent=bool(allow_transparent),
    )

    if format == "text":
        return PlainTextResponse("\n".join(f"{i['ip']}:{i['port']}" for i in items))
    if format == "list":
        return JSONResponse([i["url"] for i in items])
    # 统一结构：n=1 也返回数组，客户端不用写两套分支
    return {"count": len(items), "proxies": items}


@app.get("/get/{protocol}", response_class=PlainTextResponse)
async def get_proxy_by_protocol(
    protocol: str,
    n: int = Query(1, ge=0, description="取几个；0 表示全部可用的"),
    https: int = Query(0, ge=0, le=1),
):
    if protocol not in ("http", "https", "socks4", "socks5"):
        raise HTTPException(status_code=400, detail="protocol 非法")
    items = await _fetch_proxies(n, protocol=protocol, https_only=bool(https))
    return "\n".join(f"{i['ip']}:{i['port']}" for i in items)


@app.get("/health")
async def health():
    counts = await state.store.counts_by_status()
    return {"status": "ok", "alive": counts.get(ALIVE, 0), "total": sum(counts.values())}


@app.post("/report")
async def report_bad_proxies(
    payload: dict = Body(..., description='{"proxies": ["ip:port", ...], "reason": "timeout"}'),
):
    """爬虫上报失效的代理，让它们立刻进入下一轮复检。

    爬虫比定时轮询更早知道哪个代理挂了。上报后不直接判死，
    只是把复检时间提前，验不过自然会被状态机淘汰 —— 这样爬虫误报
    也不会冤枉好代理。
    """
    raw = payload.get("proxies") or []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="proxies 必须是数组")
    if len(raw) > 200:
        raise HTTPException(status_code=400, detail="一次最多上报 200 个")

    targets: list[tuple[str, int]] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        parsed = _split_host_port(item)
        if parsed is not None:
            targets.append(parsed)
    if not targets:
        raise HTTPException(status_code=400, detail="没有解析出合法的 ip:port")

    reason = str(payload.get("reason") or "client_reported")[:40]
    return await state.store.report_failures(targets, reason=reason)


def _split_host_port(value: str) -> tuple[str, int] | None:
    """拆 ip:port，兼容 IPv6 的 [::1]:8080 写法。

    直接 rpartition(":") 对 IPv6 会把地址切碎，
    而且没有端口时返回空 host —— 必须显式判一次。
    """
    value = value.strip()
    if not value:
        return None
    if value.startswith("["):                 # [IPv6]:port
        end = value.find("]")
        if end < 0:
            return None
        port_s = value[end + 1:].lstrip(":")
        if not port_s.isdigit():
            return None
        return value[1:end], int(port_s)
    if value.count(":") != 1:                  # 无端口或裸 IPv6，都不可用
        return None
    host, _, port_s = value.partition(":")
    if not host or not port_s.isdigit():
        return None
    port = int(port_s)
    if not (1 <= port <= 65535):
        return None
    return host, port


# ---------------- 管理接口 ----------------

@app.get("/api/stats", dependencies=[Depends(require_admin)])
async def api_stats():
    store = state.store
    counts = await store.counts_by_status()
    protos = await store.counts_by_protocol(status=ALIVE)
    anon = await store.anonymity_breakdown()
    https_alive = await _count_alive_https()
    total = sum(counts.values())
    alive = counts.get(ALIVE, 0)
    return {
        "status_counts": counts,
        "total": total,
        "alive": alive,
        "alive_rate": round(alive / total * 100, 2) if total else 0.0,
        "alive_by_protocol": protos,
        "alive_by_anonymity": anon,
        "alive_https_capable": https_alive,
        "real_ip": state.real_ip,
        "scheduler": state.scheduler.status(),
    }


async def _count_alive_https() -> int:
    async with state.sf() as s:
        return (await s.execute(
            select(func.count()).select_from(Proxy)
            .where(Proxy.status == ALIVE, Proxy.supports_https.is_(True))
        )).scalar_one()


@app.get("/api/proxies", dependencies=[Depends(require_admin)])
async def api_proxies(
    page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
    status: str | None = None, protocol: str | None = None,
    anonymity: str | None = None, search: str | None = None,
    order: str = Query("score"),
    https: int = Query(0, ge=0, le=1),
):
    items, total = await state.store.list_proxies(
        page=page, size=size, status=status, protocol=protocol,
        anonymity=anonymity, search=search, order=order, https_only=bool(https),
    )
    return {"items": items, "total": total, "page": page, "size": size}


@app.get("/api/rounds", dependencies=[Depends(require_admin)])
async def api_rounds(limit: int = Query(20, ge=1, le=100)):
    return {"rounds": await state.store.recent_rounds(limit)}


@app.get("/api/errors", dependencies=[Depends(require_admin)])
async def api_errors():
    return {"errors": await state.store.error_summary()}


@app.get("/api/trend", dependencies=[Depends(require_admin)])
async def api_trend(limit: int = Query(60, ge=5, le=300)):
    """轮次趋势 + 延迟分布，控制台画图用。"""
    return await state.store.trend(limit)


@app.get("/api/latency", dependencies=[Depends(require_admin)])
async def api_latency():
    return await state.store.latency_histogram()


@app.post("/api/trigger", dependencies=[Depends(require_admin)])
async def api_trigger(fetch: int = Query(1, ge=0, le=1)):
    if state.scheduler.running:
        return {"ok": False, "detail": "已有轮次在跑"}
    # 只影响这一次触发，不改动 cfg。
    # 曾经直接写 state.cfg.scheduler.refetch_every_round，
    # 调一次 ?fetch=0 之后所有定时轮都不再抓源，直到重启。
    asyncio.create_task(_run_once(bool(fetch)))
    return {"ok": True, "detail": "已触发"}


async def _run_once(fetch: bool = True):
    try:
        await state.scheduler.run_round(hot_only=not fetch)
    except Exception:
        log.exception("手动触发轮次失败")


@app.post("/api/proxies/delete", dependencies=[Depends(require_admin)])
async def api_delete_proxies(ids: list[int] = Body(..., embed=True)):
    if not ids:
        return {"deleted": 0}
    async with state.sf() as s:
        # 显式删 check_logs，不依赖外键级联（SQLite 默认不启用 FK）
        from .models import CheckLog
        await s.execute(delete(CheckLog).where(CheckLog.proxy_id.in_(ids)))
        r = await s.execute(delete(Proxy).where(Proxy.id.in_(ids)))
        await s.commit()
        return {"deleted": r.rowcount or 0}


@app.get("/api/usage", dependencies=[Depends(require_admin)])
async def api_usage():
    """取用侧统计：池子被用得怎么样，不只是池子里有多少。"""
    return await state.store.usage_stats()


@app.get("/api/config", dependencies=[Depends(require_admin)])
async def api_config():
    c = state.cfg
    return {
        "database": {"url": _mask_dsn(c.db_url)},
        "validator": {"concurrency": c.validator.concurrency, "timeout": c.validator.timeout,
                      "attempts": c.validator.attempts},
        "policy": c.policy.__dict__,
        "scheduler": {"interval": c.scheduler.interval,
                      "candidate_batch": c.scheduler.candidate_batch,
                      "round_budget": c.scheduler.round_budget},
        "cleanup": c.cleanup.__dict__,
        "sources": [{"name": s.name, "protocol": s.protocol, "enabled": s.enabled}
                    for s in c.fetcher.sources],
    }


def _mask_dsn(dsn: str) -> str:
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)


# ---------------- 管理页面 ----------------

if TEMPLATE_DIR.exists():
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {"real_ip": state.real_ip})
