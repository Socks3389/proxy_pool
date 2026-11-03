"""代理源抓取与解析。

设计要点：
- 一个源失败不影响整体（这是免费源的日常，实测 6 个源里常有 1-2 个 5xx）
- 解析统一走正则 + 协议归一化，能吃掉纯 ip:port、scheme://ip:port、ip:port:COUNTRY 三种格式
- 单源条数上限保护，防止异常大响应拖垮内存
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from .config import SourceConfig
from .validator import normalize_ip

log = logging.getLogger("proxypool.fetcher")

# 匹配 ip:port 或 scheme://ip:port，允许尾部多余字段（:COUNTRY / #comment 等）
_LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks4a?|socks5h?)://)?"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{1,5})",
    re.IGNORECASE,
)
_MIXED_SCHEME_ALIAS = {"socks4a": "socks4", "socks5h": "socks5", "https": "https"}


@dataclass
class FetchResult:
    source: str
    ok: bool
    raw_lines: int = 0
    parsed: int = 0
    error: str | None = None
    elapsed_ms: int = 0
    entries: list[tuple[str, str, int]] = field(default_factory=list)


def parse_line(line: str, default_protocol: str) -> tuple[str, str, int] | None:
    """解析一行代理条目，返回 (protocol, ip, port)。"""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None
    m = _LINE_RE.match(line)
    if not m:
        return None
    ip = normalize_ip(m.group("ip"))
    if not ip:
        return None
    try:
        port = int(m.group("port"))
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None

    scheme = (m.group("scheme") or "").lower()
    scheme = _MIXED_SCHEME_ALIAS.get(scheme, scheme)

    if scheme in ("http", "https", "socks4", "socks5"):
        proto = scheme
    else:
        proto = default_protocol if default_protocol != "mixed" else "http"
    if proto == "mixed":
        proto = "http"
    return proto, ip, port


async def fetch_source(client: httpx.AsyncClient, src: SourceConfig,
                       timeout: int, retries: int, max_entries: int) -> FetchResult:
    last_err: str | None = None
    for attempt in range(retries + 1):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            r = await client.get(src.url, timeout=timeout)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return FetchResult(src.name, False, error=last_err,
                                   elapsed_ms=int((loop.time() - t0) * 1000))
            text = r.text
            raw_lines = 0
            entries: list[tuple[str, str, int]] = []
            seen: set[tuple[str, str, int]] = set()
            for line in text.splitlines():
                raw_lines += 1
                if len(entries) >= max_entries:
                    break
                parsed = parse_line(line, src.protocol)
                if parsed and parsed not in seen:
                    seen.add(parsed)
                    entries.append(parsed)
            return FetchResult(
                source=src.name, ok=True, raw_lines=raw_lines, parsed=len(entries),
                entries=entries, elapsed_ms=int((loop.time() - t0) * 1000),
            )
        except Exception as e:  # noqa: BLE001 - 单源异常不能影响整轮
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return FetchResult(src.name, False, error=last_err,
                               elapsed_ms=int((loop.time() - t0) * 1000))
    return FetchResult(src.name, False, error=last_err or "unknown")


async def fetch_all(cfg) -> tuple[list[FetchResult], list[tuple[str, str, int]]]:
    """并发抓取所有启用的源，返回 (逐源结果, 去重后条目)。"""
    enabled = [s for s in cfg.fetcher.sources if s.enabled]
    sem = asyncio.Semaphore(cfg.fetcher.concurrency)

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "ProxyPool/1.0 (+https://github.com)"},
        limits=httpx.Limits(max_connections=cfg.fetcher.concurrency * 2),
    ) as client:

        async def run(src: SourceConfig) -> FetchResult:
            async with sem:
                limit = src.max_per_source or cfg.fetcher.max_per_source
                return await fetch_source(
                    client, src, cfg.fetcher.timeout, cfg.fetcher.retries, limit,
                )

        results = await asyncio.gather(*[run(s) for s in enabled])

    merged: dict[tuple[str, str, int], None] = {}
    for r in results:
        if r.ok:
            for e in r.entries:
                merged[e] = None
    ok_cnt = sum(1 for r in results if r.ok)
    log.info(
        "抓取完成：%d/%d 源成功，原始 %d 条，去重 %d 条",
        ok_cnt, len(results), sum(r.parsed for r in results), len(merged),
    )
    for r in results:
        if not r.ok:
            log.warning("源失败 %s: %s", r.source, r.error)
    return results, list(merged.keys())
