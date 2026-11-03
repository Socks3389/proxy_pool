"""代理探测引擎：自建 asyncio raw socket，不用 httpx。

为什么不用 httpx/requests：它们通过代理时不保证遵守 timeout，
一批 400 条探测能挂住 5 分钟不返回，整轮调度跟着卡死。
raw socket 配 asyncio.wait_for 才能硬超时。

两级探测：
- 明文（http://httpbin.org/get）：拿 origin 和请求头判匿名性，成本最低
- HTTPS（CONNECT + TLS）：只有约 16% 的 HTTP 代理支持 CONNECT，
  不支持的打标降级成仅 HTTP 可用
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Literal

Anon = Literal["elite", "anonymous", "transparent"]
STAGE_CONNECT = "connect"
STAGE_HANDSHAKE = "handshake"
STAGE_TLS = "tls"
STAGE_WRITE = "write"
STAGE_READ = "read"

LEAK_HEADERS = (
    "via",
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "client-ip",
    "x-client-ip",
    "x-proxy-id",
    "proxy-agent",
    "proxy-connection",
    "x-proxyip",
)

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_MAX_HEAD = 65536
_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


@dataclass
class ProbeResult:
    ok: bool = False
    latency_ms: int = 0
    exit_ip: str | None = None
    anonymity: str | None = None
    supports_https: bool = False
    leak_headers: list[str] = field(default_factory=list)
    error: str | None = None
    stage: str = ""
    # 目标站限流或抽风，不是代理的问题，不计入代理的失败次数
    target_throttled: bool = False

    def errors_short(self) -> str:
        """错误码统一格式：错误类型@发生阶段，便于聚合统计。"""
        if not self.error:
            return ""
        return f"{self.error}@{self.stage or STAGE_CONNECT}"


def normalize_ip(raw: str) -> str | None:
    """校验并规范化 IP，拒绝 0.0.0.0 / 广播 / 保留段。"""
    raw = (raw or "").strip()
    if not raw or len(raw) > 45:
        return None
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if addr.version == 4:
        if addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_reserved:
            return None
        # 0.x.y.z 整段无效
        if str(addr).startswith("0."):
            return None
    else:
        if addr.is_unspecified or addr.is_multicast or addr.is_loopback:
            return None
    return str(addr)


def parse_origin_list(value: str) -> list[str]:
    """httpbin 的 origin 可能是 "ip1, ip2"，也可能带端口。"""
    out: list[str] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("["):           # [IPv6]:port
            end = chunk.find("]")
            out.append(chunk[1:end] if end > 0 else chunk)
        elif ":" in chunk and chunk.count(":") == 1:
            out.append(chunk.split(":", 1)[0])
        else:
            out.append(chunk)
    return [x for x in out if x]


def judge_anonymity(
    origin_ips: list[str],
    headers: dict[str, str],
    proxy_ip: str,
    real_ip: str | None,
) -> tuple[Anon, list[str]]:
    """匿名性分级。

    elite       出口只有代理自己一个 IP，无泄漏头
    anonymous   出口是别的 IP（多级跳转），或有代理特征头，但没暴露我们的真实 IP
    transparent 出口链或请求头里出现了我们的真实 IP
    """
    leaks = [h for h in LEAK_HEADERS if h in headers]
    joined = " ".join(origin_ips) + " " + " ".join(headers.get(h, "") for h in leaks)

    if real_ip and real_ip in joined:
        return "transparent", leaks
    if any(h in headers for h in ("x-forwarded-for", "x-real-ip", "client-ip", "x-client-ip", "forwarded")):
        return "transparent", leaks
    if leaks:
        # proxy-connection 只是协商用，不算匿名性降级
        if set(leaks) - {"proxy-connection"}:
            return "anonymous", leaks
    if origin_ips and len(origin_ips) == 1 and origin_ips[0] == proxy_ip:
        return "elite", leaks
    if origin_ips:
        return "anonymous", leaks
    return "anonymous", leaks


async def _read_http_response(reader: asyncio.StreamReader) -> tuple[dict[str, str], bytes, str]:
    """读一个完整 HTTP/1.x 响应，返回 (headers, body, status_line)。"""
    raw = await reader.readuntil(b"\r\n\r\n")
    head = raw.decode("latin-1", "replace")
    lines = head.split("\r\n")
    status_line = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    body = b""
    cl = headers.get("content-length")
    if cl and cl.isdigit():
        n = min(int(cl), _MAX_HEAD)
        if n:
            try:
                body = await reader.readexactly(n)
            except asyncio.IncompleteReadError as e:
                body = e.partial
    elif "chunked" in headers.get("transfer-encoding", "").lower():
        buf = bytearray()
        while len(buf) < _MAX_HEAD:
            line = await reader.readline()
            if not line:
                break
            try:
                size = int(line.strip().split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            buf += await reader.readexactly(size)
            await reader.readline()
        body = bytes(buf)
    else:
        try:
            body = await reader.read(_MAX_HEAD)
        except Exception:
            body = b""
    return headers, body[:_MAX_HEAD], status_line


def _status_code(status_line: str) -> int:
    parts = status_line.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


def _make_ssl_ctx() -> ssl.SSLContext:
    """探测用的 TLS 上下文，不校验证书。

    免费代理大量存在 TLS 中间人（实测常见 self-signed certificate in
    certificate chain），校验证书会让绝大多数可用代理被判死。
    这里只判断「能不能建立 TLS 并取回数据」，不判断链路可信不可信。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


async def _socks5_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                            host: str, port: int) -> None:
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    ver_auth, method = await reader.readexactly(2)
    if ver_auth != 0x05 or method != 0x00:
        raise OSError(f"socks5_auth_{method}")

    hb = host.encode()
    writer.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big"))
    await writer.drain()
    head = await reader.readexactly(4)
    if head[0] != 0x05:
        raise OSError("socks5_bad_version")
    if head[1] != 0x00:
        raise OSError(f"socks5_connect_{head[1]}")
    atyp = head[3]
    if atyp == 0x01:
        await reader.readexactly(6)
    elif atyp == 0x03:
        ln = (await reader.readexactly(1))[0]
        await reader.readexactly(ln + 2)
    elif atyp == 0x04:
        await reader.readexactly(18)
    else:
        raise OSError("socks5_bad_atyp")


async def _socks4_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                            host: str, port: int) -> None:
    if not _IPV4_RE.match(host):
        raise OSError("socks4_needs_ipv4")
    ipb = bytes(int(x) for x in host.split("."))
    writer.write(b"\x04\x01" + port.to_bytes(2, "big") + ipb + b"\x00")
    await writer.drain()
    resp = await reader.readexactly(8)
    if resp[1] != 0x5A:
        raise OSError(f"socks4_rejected_{resp[1]}")


class Validator:
    """代理验证器。所有网络操作都在 wait_for 硬超时内完成。"""

    def __init__(self, cfg, real_ip: str | None = None):
        self.cfg = cfg
        self.real_ip = real_ip
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._ssl = _make_ssl_ctx()
        self._http_targets = self._parse_target(cfg.check_url_http)
        self._https_targets = self._parse_target(cfg.check_url_https)
        self._https_nonce = self._probe_nonce(self._https_targets[2])

    @staticmethod
    def _parse_target(url: str) -> tuple[str, int, str, bool]:
        from urllib.parse import urlsplit

        sp = urlsplit(url)
        if not sp.hostname:
            raise ValueError(f"检查地址无效: {url}")
        is_tls = (sp.scheme or "http").lower() == "https"
        port = sp.port or (443 if is_tls else 80)
        path = sp.path or "/"
        if sp.query:
            path += "?" + sp.query
        if not is_tls and "://" in url and url.split("://")[0].lower() not in ("http", ""):
            raise ValueError(f"明文检查地址必须是 http://，实际 {url}")
        return sp.hostname, port, path, is_tls

    @staticmethod
    def _probe_nonce(path: str) -> str:
        """从探测地址的 query 里取一个必须出现在响应体中的串。

        HTTPS 探测不能只看状态码：实测有代理 CONNECT 任何目标都返回自己的 IP
        （一个合法的 JSON），只看 200 会把这种假代理判成支持 HTTPS。
        用探测地址里的唯一参数做呼应校验，回显了才算通。
        """
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(path).query)
        for key in ("probe", "token", "nonce"):
            if q.get(key):
                return q[key][0]
        return ""

    async def probe(self, protocol: str, ip: str, port: int,
                    *, use_https: bool = False, read_body: bool = True) -> ProbeResult:
        """单次探测。use_https=True 走 CONNECT/TLS，否则走明文。"""
        async with self._sem:
            return await self._probe_inner(protocol, ip, port, use_https, read_body)

    async def _probe_inner(self, protocol: str, ip: str, port: int,
                           use_https: bool, read_body: bool) -> ProbeResult:
        res = ProbeResult()
        target = self._https_targets if use_https else self._http_targets
        host, tport, tpath, _tls_target = target
        t0 = time.perf_counter()

        async def run() -> None:
            reader: asyncio.StreamReader | None = None
            writer: asyncio.StreamWriter | None = None
            try:
                res.stage = STAGE_CONNECT
                reader, writer = await asyncio.open_connection(ip, port, limit=_MAX_HEAD)

                if protocol == "socks5":
                    res.stage = STAGE_HANDSHAKE
                    await _socks5_handshake(reader, writer, host, tport)
                elif protocol == "socks4":
                    res.stage = STAGE_HANDSHAKE
                    await _socks4_handshake(reader, writer, host, tport)

                is_http_proxy = protocol in ("http", "https")

                if use_https:
                    if is_http_proxy:
                        # 先 CONNECT 建立隧道
                        res.stage = STAGE_TLS
                        writer.write(
                            f"CONNECT {host}:{tport} HTTP/1.1\r\nHost: {host}:{tport}\r\n\r\n".encode()
                        )
                        await writer.drain()
                        head = await reader.readuntil(b"\r\n\r\n")
                        code = _status_code(head.decode("latin-1", "replace").split("\r\n")[0])
                        if code != 200:
                            res.error = f"connect_{code or 'bad'}"
                            return
                    res.stage = STAGE_TLS
                    await writer.start_tls(self._ssl, server_hostname=host)

                res.stage = STAGE_WRITE
                if is_http_proxy and not use_https:
                    req = (
                        f"GET http://{host}{tpath} HTTP/1.1\r\n"
                        f"Host: {host}\r\n"
                        f"User-Agent: {_DEFAULT_UA}\r\n"
                        "Accept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
                    )
                else:
                    req = (
                        f"GET {tpath} HTTP/1.1\r\n"
                        f"Host: {host}\r\n"
                        f"User-Agent: {_DEFAULT_UA}\r\n"
                        "Accept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
                    )
                writer.write(req.encode())
                await writer.drain()

                res.stage = STAGE_READ
                headers, _head_bytes, status_line = await _read_http_response(reader)
                code = _status_code(status_line)
                if code != 200:
                    res.error = f"status_{code or 'bad'}"
                    # 429/409/5xx 是目标站自己限流，不算代理的错。
                    # httpbin.org 在高并发下会返回 429/409，不区分就会误判可用代理。
                    res.target_throttled = code in (429, 409, 500, 502, 503, 504)
                    return

                body = b""
                if read_body:
                    body = _head_bytes

                text = body.decode("utf-8", "replace")
                if use_https:
                    # 有 nonce 时必须回显，否则视为假代理。
                    # 只看状态码会被「CONNECT 啥都返回自己 IP」的代理骗过。
                    if self._https_nonce and self._https_nonce not in text:
                        res.error = "no_echo"
                        return
                    m = re.search(r'"ip"\s*:\s*"([0-9a-fA-F:.]+)"', text)
                    if not m:
                        m = re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text)
                    if m:
                        res.exit_ip = normalize_ip(m.group(1)) or m.group(1)
                    res.supports_https = True
                else:
                    m = re.search(r'"origin"\s*:\s*"([^"]+)"', text)
                    origin_ips: list[str] = []
                    if m:
                        origin_ips = parse_origin_list(m.group(1))
                        res.exit_ip = ",".join(origin_ips) if len(origin_ips) != 1 else origin_ips[0]
                    res.anonymity, res.leak_headers = judge_anonymity(
                        origin_ips, headers, ip, self.real_ip
                    )
                res.ok = True
            finally:
                res.stage = "done" if res.ok else res.stage
                if writer is not None:
                    try:
                        writer.close()
                        await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(run(), timeout=self.cfg.timeout)
        except TimeoutError:
            res.error = res.error or "timeout"
        except asyncio.IncompleteReadError:
            res.error = "short_read"
        except ConnectionResetError:
            res.error = "conn_reset"
        except ConnectionRefusedError:
            res.error = "conn_refused"
        except ssl.SSLError:
            res.error = "ssl_error"
        except OSError as e:
            res.error = f"os_{e.errno}" if e.errno else "os_error"
        except Exception as e:  # noqa: BLE001 - 探测层必须兜住一切，否则一条坏代理能拖死整轮
            res.error = type(e).__name__.lower()[:24]
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res

    async def verify(self, protocol: str, ip: str, port: int) -> ProbeResult:
        """完整验证：明文判定存活和匿名性，HTTPS 能力由二级探测补。

        同一轮内重试 attempts 次，能救回抖动的代理。
        """
        attempts = max(1, self.cfg.attempts)
        last = ProbeResult(error="no_attempt")
        for i in range(attempts):
            last = await self.probe(protocol, ip, port, use_https=False)
            if last.ok:
                return last
            if i + 1 < attempts:
                await asyncio.sleep(self.cfg.retry_delay)
        return last

    async def verify_https(self, protocol: str, ip: str, port: int) -> ProbeResult:
        """只探 HTTPS 能力（TLS 握手 + 一次真实 HTTPS 请求）。"""
        return await self.probe(protocol, ip, port, use_https=True)

    async def detect_real_ip(self) -> str | None:
        """探测本机出口 IP，用于匿名性判定的对照基准。"""
        for url in ("https://api.ipify.org?format=json", "http://api.ipify.org?format=json"):
            try:
                from urllib.parse import urlsplit

                sp = urlsplit(url)
                tls = sp.scheme == "https"
                port = sp.port or (443 if tls else 80)
                path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(sp.hostname, port,
                                            ssl=_make_ssl_ctx() if tls else None,
                                            server_hostname=sp.hostname if tls else None),
                    timeout=8,
                )
                try:
                    writer.write(
                        f"GET {path} HTTP/1.1\r\nHost: {sp.hostname}\r\nUser-Agent: proxypool\r\nConnection: close\r\n\r\n".encode()
                    )
                    await writer.drain()
                    _h, body, _s = await asyncio.wait_for(_read_http_response(reader), timeout=8)
                    m = re.search(r'"ip"\s*:\s*"([0-9a-fA-F:.]+)"', body.decode("utf-8", "replace"))
                    if m:
                        return normalize_ip(m.group(1))
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass
            except Exception:
                continue
        return None
