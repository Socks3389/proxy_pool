"""端到端真实验收：从 /get 取代理，实际用 httpx 访问目标站点，验证整条链路可用。

这是最关键的验收：池子里的代理必须能被真实 HTTP 客户端用起来。
"""
import sys
import time

import httpx

BASE = "http://127.0.0.1:8088"
TARGETS = [
    "http://httpbin.org/ip",
    "https://api.ipify.org?format=json",
]


def fetch(n=20, **kw):
    q = {"n": n, **kw}
    r = httpx.get(f"{BASE}/get", params=q, timeout=15)
    r.raise_for_status()
    d = r.json()
    return d.get("proxies", []) if isinstance(d, dict) else [d]


def test_proxy(p, target):
    url = f"{p['protocol']}://{p['ip']}:{p['port']}"
    t0 = time.perf_counter()
    try:
        with httpx.Client(proxy=url, timeout=12, verify=False) as c:
            r = c.get(target)
            ms = int((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                return False, ms, f"HTTP {r.status_code}"
            body = r.text[:200]
            # 确认出口 IP 不是本机
            leak_local = "43.247.132.71" in body
            return (not leak_local), ms, body[:80].replace("\n", " ")
    except Exception as e:
        return False, int((time.perf_counter() - t0) * 1000), f"{type(e).__name__}: {str(e)[:50]}"


def main():
    print("=== 从 API 取 20 个代理（默认筛选）===")
    try:
        items = fetch(20)
    except Exception as e:
        print("取代理失败:", e)
        return 1
    print(f"取到 {len(items)} 个\n")

    for target in TARGETS:
        ok = 0
        print(f"--- 目标 {target} ---")
        for p in items:
            good, ms, info = test_proxy(p, target)
            mark = "OK  " if good else "FAIL"
            if good:
                ok += 1
            print(f"  {mark} {p['protocol']:6s} {p['ip']}:{p['port']:<6} "
                  f"池内{p['latency_ms'] or 0:>5}ms 实测{ms:>5}ms  {info}")
        print(f"  通过 {ok}/{len(items)}\n")

    print("=== 取高匿 + 支持 HTTPS 的代理 ===")
    try:
        elite = fetch(10, anonymity="elite", https=1)
        print(f"取到 {len(elite)} 个")
        for p in elite:
            good, ms, info = test_proxy(p, TARGETS[1])
            print(f"  {'OK  ' if good else 'FAIL'} {p['protocol']:6s} {p['ip']}:{p['port']:<6} {ms:>5}ms {info}")
    except Exception as e:
        print("  无满足条件代理:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
