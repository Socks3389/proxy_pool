"""命令行入口。

用法：
    python -m proxypool serve                 # 起 API + 调度器
    python -m proxypool run [--rounds N]      # 只跑调度器，不起 HTTP
    python -m proxypool fetch                 # 只抓取，写出到 stdout/文件
    python -m proxypool check URL             # 验证单个代理
    python -m proxypool initdb                # 只建表
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import load_config
from .models import create_all
from .store import Store
from .validator import Validator


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def ensure_fd_limit(concurrency: int) -> None:
    """确保文件描述符上限够用，不够就自动提高到硬上限。

    实测坑：Linux 默认 soft limit 4096，并发 400 时每个探测占 2-3 个 fd
    （socket + TLS），会直接撞上限报 EMFILE，表现为大量莫名的 OSError。
    必须在启动时抬到硬上限，而不是等出错再查。
    """
    try:
        import resource
    except ImportError:
        return
    need = concurrency * 4 + 1024
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < need:
        target = min(hard, need) if hard != resource.RLIM_INFINITY else need
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            logging.getLogger("proxypool").warning(
                "fd 上限从 %d 提升到 %d（并发 %d 需要 ≥%d）", soft, target, concurrency, need
            )
        except (ValueError, OSError) as e:
            logging.getLogger("proxypool").error(
                "fd 上限提升失败 (%s)，并发 %d 可能触发 EMFILE。"
                "请手动执行 ulimit -n %d 或用 systemd LimitNOFILE=%d",
                e, concurrency, need, need,
            )


def _make_engine(cfg):
    return create_async_engine(
        cfg.db_url, echo=cfg.database.echo, future=True, pool_pre_ping=True,
        **({"pool_size": cfg.database.pool_size, "max_overflow": 10}
           if not cfg.db_url.startswith("sqlite") else {}),
    )


async def cmd_initdb(args) -> int:
    from pathlib import Path

    cfg = load_config(args.config)
    if cfg.db_url.startswith("sqlite"):
        db_path = Path(cfg.db_url.split("///", 1)[-1])
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = _make_engine(cfg)
    await create_all(engine)
    await engine.dispose()
    print(f"数据库已初始化: {cfg.db_url}")
    return 0


async def cmd_fetch(args) -> int:
    from .fetcher import fetch_all

    cfg = load_config(args.config)
    results, entries = await fetch_all(cfg)
    print(f"\n{'源':<22} {'状态':<8} {'解析':>7} {'耗时':>8}  备注")
    for r in results:
        print(f"{r.source:<22} {'OK' if r.ok else 'FAIL':<8} {r.parsed:>7} "
              f"{r.elapsed_ms:>6}ms  {r.error or ''}")
    print(f"\n去重后合计: {len(entries)} 条")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for p, ip, port in sorted(entries):
                f.write(f"{p}://{ip}:{port}\n")
        print(f"已写入 {args.out}")
    else:
        for p, ip, port in sorted(entries)[:args.limit]:
            print(f"{p}://{ip}:{port}")
    return 0


async def cmd_check(args) -> int:
    cfg = load_config(args.config)
    v = Validator(cfg.validator)
    real = await v.detect_real_ip()
    v.real_ip = real
    print(f"本机出口 IP: {real}")
    target = args.proxy
    if "://" not in target:
        # 猜测协议：先试 http，再试 socks5
        protocols = ["http", "socks5", "socks4"]
        hostport = target
    else:
        proto, hostport = target.split("://", 1)
        protocols = [proto]
    host, _, port = hostport.rpartition(":")
    for proto in protocols:
        r = await v.verify(proto, host, int(port))
        print(f"\n[{proto}] {host}:{port}")
        print(f"  存活: {r.ok}  延迟: {r.latency_ms}ms  阶段: {r.stage}")
        if r.ok:
            print(f"  出口IP: {r.exit_ip}  匿名性: {r.anonymity}  泄漏头: {r.leak_headers}")
            hr = await v.verify_https(proto, host, int(port))
            print(f"  HTTPS: {hr.ok} ({hr.latency_ms}ms) {hr.error or ''}")
            return 0
        print(f"  失败原因: {r.errors_short()}")
    return 1


async def cmd_run(args) -> int:
    from .scheduler import Scheduler

    cfg = load_config(args.config)
    if args.interval:
        cfg.scheduler.interval = args.interval
    engine = _make_engine(cfg)
    await create_all(engine)
    sf = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    store = Store(sf, cfg)
    v = Validator(cfg.validator)
    real = await v.detect_real_ip()
    v.real_ip = real
    logging.getLogger("proxypool").info("出口 IP %s", real)
    sch = Scheduler(cfg, store, v)

    if args.rounds:
        for i in range(args.rounds):
            rep = await sch.run_round()
            print(f"轮 {i+1}: 验证 {rep.checked} 存活 {rep.ok} 晋升 {rep.promoted} "
                  f"耗时 {rep.duration_ms}ms")
    else:
        await sch.start()
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await sch.stop()
    await engine.dispose()
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    cfg = load_config(args.config)
    host = args.host or cfg.api.host
    port = args.port or cfg.api.port
    uvicorn.run("proxypool.api:app", host=host, port=port,
                log_level=args.log_level.lower(), access_log=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="proxypool", description="免费代理池系统")
    p.add_argument("--config", help="配置文件路径", default=None)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动 API + 调度器")
    s.add_argument("--host")
    s.add_argument("--port", type=int)

    r = sub.add_parser("run", help="只跑调度器")
    r.add_argument("--rounds", type=int, default=0, help="跑 N 轮后退出，0=常驻")
    r.add_argument("--interval", type=int, help="覆盖周期秒数")

    f = sub.add_parser("fetch", help="只抓取代理源")
    f.add_argument("--out", help="输出文件")
    f.add_argument("--limit", type=int, default=20)

    c = sub.add_parser("check", help="验证单个代理")
    c.add_argument("proxy", help="ip:port 或 scheme://ip:port")

    sub.add_parser("initdb", help="初始化数据库")

    args = p.parse_args(argv)
    setup_logging(args.log_level)

    # 所有入口都先保证 fd 上限，避免探到一半 EMFILE
    try:
        _cfg_for_fd = load_config(args.config)
        ensure_fd_limit(_cfg_for_fd.validator.concurrency)
    except Exception:
        pass

    if args.cmd == "serve":
        return cmd_serve(args)
    handler = {
        "initdb": cmd_initdb, "fetch": cmd_fetch,
        "check": cmd_check, "run": cmd_run,
    }[args.cmd]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
