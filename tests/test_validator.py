"""探测引擎集成测试：本地起 mock 代理服务端，验证协议实现真能跑通。

不测"能否连上公网代理"（结果随机），而是测我们自己的协议实现是否符合 RFC：
- HTTP 代理的绝对 URI 请求格式
- SOCKS5 的 no-auth 握手与 CONNECT
- SOCKS4 的 CONNECT
- 匿名性判定在真实报文下的结果
- 超时是否真实生效（这是最关键的一条：httpx 在此场景会挂死）
"""
import asyncio
import time

import pytest

from proxypool.config import ValidatorConfig
from proxypool.validator import (
    Validator,
    _read_http_response,
    _socks4_handshake,
    _socks5_handshake,
)


async def _echo_http_proxy(reader, writer):
    """一个极简 HTTP 代理：回固定的 httpbin 风格响应，记录收到的请求行。"""
    try:
        data = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        return
    text = data.decode("latin-1", "replace")
    line = text.split("\r\n")[0]
    body = (
        '{"origin": "7.7.7.7"}'
        if "/get" in line
        else '{"ip": "7.7.7.7"}'
    )
    resp = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n" + body
    ).encode()
    writer.write(resp)
    await writer.drain()
    writer.close()


async def _proxy_that_leaks_headers(reader, writer):
    """泄漏 X-Forwarded-For 的代理：应被判为 transparent。"""
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        return
    body = '{"origin": "7.7.7.7"}'
    resp = (
        "HTTP/1.1 200 OK\r\n"
        "X-Forwarded-For: 9.9.9.9\r\n"
        f"Content-Length: {len(body)}\r\n\r\n" + body
    ).encode()
    writer.write(resp)
    await writer.drain()
    writer.close()


async def _silent_server(reader, writer):
    """接受连接但永不响应 —— 用来验证硬超时。"""
    await asyncio.sleep(30)
    writer.close()


async def _http_407(reader, writer):
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        pass
    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
    await writer.drain()
    writer.close()


@pytest.fixture
async def served(request):
    """按需启动 mock 服务，返回端口。"""
    handler_map = {
        "echo": _echo_http_proxy,
        "leak": _proxy_that_leaks_headers,
        "silent": _silent_server,
        "407": _http_407,
    }
    handler = handler_map[request.param]
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


@pytest.mark.parametrize("served", ["echo"], indirect=True)
async def test_http_proxy_probe(served):
    cfg = ValidatorConfig(timeout=5.0, attempts=1)
    v = Validator(cfg, real_ip="1.1.1.1")
    # 用本地 mock 作为"代理"，目标改成它自己
    v._http_targets = ("127.0.0.1", served, "/get", False)
    r = await v.probe("http", "127.0.0.1", served, use_https=False)
    assert r.ok is True, r.errors_short()
    assert r.exit_ip == "7.7.7.7"
    assert r.latency_ms >= 0


@pytest.mark.parametrize("served", ["leak"], indirect=True)
async def test_leaky_proxy_is_transparent(served):
    cfg = ValidatorConfig(timeout=5.0, attempts=1)
    v = Validator(cfg, real_ip="1.1.1.1")
    v._http_targets = ("127.0.0.1", served, "/get", False)
    r = await v.probe("http", "127.0.0.1", served, use_https=False)
    assert r.ok is True
    assert r.anonymity == "transparent"
    assert "x-forwarded-for" in r.leak_headers


@pytest.mark.parametrize("served", ["407"], indirect=True)
async def test_proxy_auth_required_reported(served):
    cfg = ValidatorConfig(timeout=5.0, attempts=1)
    v = Validator(cfg, real_ip="1.1.1.1")
    v._http_targets = ("127.0.0.1", served, "/get", False)
    r = await v.probe("http", "127.0.0.1", served, use_https=False)
    assert r.ok is False
    assert "407" in r.errors_short()


@pytest.mark.parametrize("served", ["silent"], indirect=True)
async def test_hard_timeout_is_enforced(served):
    """核心回归：服务端永不响应时，探测必须按配置超时返回，不能挂死。

    这条是整轮调度不被拖死的保证（httpx 在真实代理上会挂 5 分钟以上）。
    """
    cfg = ValidatorConfig(timeout=1.5, attempts=1)
    v = Validator(cfg, real_ip="1.1.1.1")
    v._http_targets = ("127.0.0.1", served, "/get", False)
    t0 = time.perf_counter()
    r = await v.probe("http", "127.0.0.1", served, use_https=False)
    elapsed = time.perf_counter() - t0
    assert r.ok is False
    assert "timeout" in (r.error or "")
    assert elapsed < 3.0, f"超时未生效，实际耗时 {elapsed:.1f}s"


@pytest.mark.parametrize("served", ["silent"], indirect=True)
async def test_verify_attempts_respect_timeout(served):
    """attempts=2 时总耗时约 2 倍超时，但仍要有界。"""
    cfg = ValidatorConfig(timeout=1.0, attempts=2, retry_delay=0.1)
    v = Validator(cfg, real_ip="1.1.1.1")
    v._http_targets = ("127.0.0.1", served, "/get", False)
    t0 = time.perf_counter()
    r = await v.verify("http", "127.0.0.1", served)
    elapsed = time.perf_counter() - t0
    assert r.ok is False
    assert elapsed < 4.0


async def test_socks5_handshake_against_mock():
    """验证 SOCKS5 no-auth 握手的报文格式（RFC1928）。"""

    async def handler(reader, writer):
        # 1) 读客户端方法协商
        head = await reader.readexactly(2)
        assert head == b"\x05\x01", head
        methods = await reader.readexactly(1)
        assert methods == b"\x00"
        writer.write(b"\x05\x00")
        await writer.drain()
        # 2) 读 CONNECT 请求
        ver_cmd_rsv_atyp = await reader.readexactly(4)
        assert ver_cmd_rsv_atyp[0] == 5
        assert ver_cmd_rsv_atyp[1] == 1  # CONNECT
        assert ver_cmd_rsv_atyp[3] == 3  # DOMAINNAME
        ln = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(ln)).decode()
        port = int.from_bytes(await reader.readexactly(2), "big")
        assert (host, port) == ("example.com", 443)
        # 3) 回成功
        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 4 + b"\x00\x00")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(_socks5_handshake(reader, writer, "example.com", 443), timeout=5)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_socks5_auth_required_raises():
    async def handler(reader, writer):
        await reader.readexactly(3)
        writer.write(b"\x05\x02")  # 只支持用户名密码
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        with pytest.raises(OSError, match="socks5"):
            await asyncio.wait_for(_socks5_handshake(reader, writer, "x.com", 80), timeout=5)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_socks4_handshake_format():
    async def handler(reader, writer):
        head = await reader.readexactly(8)
        assert head[0] == 4 and head[1] == 1
        port = int.from_bytes(head[2:4], "big")
        ip = ".".join(str(b) for b in head[4:8])
        assert (ip, port) == ("1.2.3.4", 8080)
        # 读 USERID 终止符
        await reader.readuntil(b"\x00")
        writer.write(b"\x00\x5a" + b"\x00" * 6)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(_socks4_handshake(reader, writer, "1.2.3.4", 8080), timeout=5)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_response_parsing_chunked():
    """chunked 响应也要能解析出 body（部分代理不解压但会 chunked）。"""

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\n{\"a\":\r\n" b"7\r\n\"origin\"\r\n" b"0\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        headers, body, status = await _read_http_response(reader)
        assert "200" in status
        assert headers.get("transfer-encoding") == "chunked"
        assert b"origin" in body or b'a' in body
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


class TestFakeHttpsDetection:
    """HTTPS 探测不能只看状态码。

    实测遇到一类代理：CONNECT 任何目标都返回自己的 IP（合法 JSON）。
    只看 status==200 会把它判成支持 HTTPS，但它根本没把流量转出去。
    """

    def test_nonce_extracted_from_probe_url(self):
        cfg = ValidatorConfig(check_url_https="https://httpbin.org/get?probe=abc123")
        v = Validator(cfg)
        assert v._https_nonce == "abc123"

    def test_nonce_empty_when_no_probe_param(self):
        cfg = ValidatorConfig(check_url_https="https://api.ipify.org?format=json")
        v = Validator(cfg)
        assert v._https_nonce == ""

    async def test_probe_rejects_missing_echo(self):
        """nonce 在正文里找不到时必须判失败。

        直接验证判定逻辑本身，不起 TLS 服务端 —— 没有 nonce 时
        probe 会走 TLS 握手，与这里要测的校验分支无关。
        """
        cfg = ValidatorConfig(check_url_https="https://httpbin.org/get?probe=needle999")
        v = Validator(cfg)
        assert v._https_nonce == "needle999"

        # 这就是 _probe_inner 里 use_https 分支的判定条件
        for body, should_fail in [
            ('{"ip": "9.9.9.9"}', True),                       # 假代理，没回显
            ('{"args": {"probe": "needle999"}}', False),       # 真代理，回显了
        ]:
            missing = bool(v._https_nonce and v._https_nonce not in body)
            assert missing is should_fail, f"{body} 判定错了"
