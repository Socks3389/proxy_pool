"""实测不同并发下的真实吞吐，确定 5 分钟内能覆盖全量的并发值。

理论线性推算不可信：免费代理大量是 timeout，受操作系统 fd 上限、
上游带宽、尤其运营商对异常连接的限速影响，吞吐会饱和。
"""
import asyncio
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxypool.config import load_config
from proxypool.validator import Validator


async def bench(concurrency: int, sample_size: int = 2000) -> dict:
    cfg = load_config()
    cfg.validator.concurrency = concurrency
    cfg.validator.timeout = 8.0
    cfg.validator.attempts = 1

    # 出口 IP 探测出来用于匿名性判定，不能写死（每台机器不同）
    v = Validator(cfg.validator)
    try:
        v.real_ip = await v.detect_real_ip()
    except Exception:
        v.real_ip = None

    # 用配置里的库路径，不写死相对路径（从别的目录调用时也能跑）
    db_path = cfg.db_url.split("///", 1)[-1] if cfg.db_url.startswith("sqlite") \
        else "data/proxypool.db"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "select protocol, ip, port from proxies where status in ('new','candidate') "
        "order by random() limit ?", (sample_size,)
    ).fetchall()
    conn.close()

    sem = asyncio.Semaphore(concurrency)

    async def one(proto, ip, port):
        async with sem:
            return await v.probe(proto, ip, port, use_https=False)

    t0 = time.perf_counter()
    results = await asyncio.gather(*[one(*r) for r in rows], return_exceptions=True)
    elapsed = time.perf_counter() - t0

    ok = sum(1 for r in results if not isinstance(r, Exception) and r.ok)
    timeouts = sum(1 for r in results if not isinstance(r, Exception) and "timeout" in (r.error or ""))
    rate = len(rows) / elapsed
    return {
        "concurrency": concurrency,
        "sample": len(rows),
        "elapsed_s": round(elapsed, 1),
        "rate_per_s": round(rate, 1),
        "ok": ok,
        "ok_rate_pct": round(ok / len(rows) * 100, 1),
        "timeout_pct": round(timeouts / len(rows) * 100, 1),
        "capacity_280s": int(rate * 280),
    }


async def main():
    print(f"{'并发':>6} {'样本':>6} {'耗时s':>7} {'条/秒':>7} {'存活%':>6} {'超时%':>6} {'280s容量':>9}")
    out = []
    for conc in [200, 400, 600, 800]:
        r = await bench(conc)
        out.append(r)
        print(f"{r['concurrency']:>6} {r['sample']:>6} {r['elapsed_s']:>7} "
              f"{r['rate_per_s']:>7} {r['ok_rate_pct']:>6} {r['timeout_pct']:>6} "
              f"{r['capacity_280s']:>9}")
        await asyncio.sleep(3)  # 让上游限速恢复，避免互相干扰

    print("\n=== 结论 ===")
    total = 24791
    for r in out:
        rounds = total / max(1, r["capacity_280s"])
        print(f"并发 {r['concurrency']:>3}: {rounds:.1f} 轮覆盖全量 ({rounds*5:.0f} 分钟)")
    best = max(out, key=lambda x: x["rate_per_s"])
    print(f"\n最佳: 并发 {best['concurrency']} → {best['rate_per_s']} 条/秒")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
