"""解析与匿名性判定单元测试（纯函数，无需网络）。"""
from proxypool.fetcher import parse_line
from proxypool.validator import (
    judge_anonymity,
    normalize_ip,
    parse_origin_list,
)


class TestNormalizeIp:
    def test_valid_ipv4(self):
        assert normalize_ip("8.8.8.8") == "8.8.8.8"
        assert normalize_ip("  1.2.3.4 ") == "1.2.3.4"

    def test_rejects_unspecified_and_broadcast(self):
        assert normalize_ip("0.0.0.0") is None
        assert normalize_ip("0.1.2.3") is None

    def test_rejects_garbage(self):
        assert normalize_ip("") is None
        assert normalize_ip("999.1.1.1") is None
        assert normalize_ip("abc") is None
        assert normalize_ip("1.2.3") is None

    def test_accepts_ipv6(self):
        assert normalize_ip("2001:db8::1") == "2001:db8::1"
        assert normalize_ip("::1") is None  # 回环拒绝


class TestParseLine:
    def test_plain_ip_port(self):
        assert parse_line("1.2.3.4:8080", "http") == ("http", "1.2.3.4", 8080)

    def test_with_scheme(self):
        assert parse_line("socks5://1.2.3.4:1080", "http") == ("socks5", "1.2.3.4", 1080)
        assert parse_line("http://1.2.3.4:80", "socks5") == ("http", "1.2.3.4", 80)

    def test_scheme_alias(self):
        assert parse_line("socks5h://1.2.3.4:1080", "http") == ("socks5", "1.2.3.4", 1080)
        assert parse_line("socks4a://1.2.3.4:1080", "http") == ("socks4", "1.2.3.4", 1080)

    def test_trailing_country_field(self):
        # hideip.me 格式: ip:port:Country
        assert parse_line("203.174.15.83:8080:Indonesia", "http") == ("http", "203.174.15.83", 8080)

    def test_crlf_and_comment(self):
        assert parse_line("1.2.3.4:8080\r", "http") == ("http", "1.2.3.4", 8080)
        assert parse_line("# 1.2.3.4:8080", "http") is None
        assert parse_line("", "http") is None

    def test_invalid_port(self):
        assert parse_line("1.2.3.4:0", "http") is None
        assert parse_line("1.2.3.4:70000", "http") is None
        assert parse_line("1.2.3.4", "http") is None

    def test_mixed_defaults_to_http(self):
        assert parse_line("1.2.3.4:80", "mixed") == ("http", "1.2.3.4", 80)

    def test_rejects_bad_ip(self):
        assert parse_line("0.0.0.0:80", "http") is None


class TestParseOrigin:
    def test_single(self):
        assert parse_origin_list("1.2.3.4") == ["1.2.3.4"]

    def test_multi(self):
        assert parse_origin_list("1.2.3.4, 5.6.7.8") == ["1.2.3.4", "5.6.7.8"]

    def test_with_port(self):
        assert parse_origin_list("1.2.3.4:8080") == ["1.2.3.4"]

    def test_ipv6_bracket(self):
        assert parse_origin_list("[2001:db8::1]:443") == ["2001:db8::1"]

    def test_empty(self):
        assert parse_origin_list("") == []


class TestAnonymity:
    REAL = "9.9.9.9"

    def test_elite(self):
        """出口就是代理自己，无泄漏头 → 高匿。"""
        anon, leaks = judge_anonymity(["1.2.3.4"], {}, "1.2.3.4", self.REAL)
        assert anon == "elite"
        assert leaks == []

    def test_transparent_when_real_ip_in_origin(self):
        """origin 链里出现本机真实 IP → 透明（实测 httpbin 会返回 "real,proxy"）。"""
        anon, _ = judge_anonymity(["9.9.9.9", "1.2.3.4"], {}, "1.2.3.4", self.REAL)
        assert anon == "transparent"

    def test_transparent_when_xff_present(self):
        anon, leaks = judge_anonymity(["1.2.3.4"], {"x-forwarded-for": "9.9.9.9"}, "1.2.3.4", self.REAL)
        assert anon == "transparent"
        assert "x-forwarded-for" in leaks

    def test_anonymous_when_via_header(self):
        anon, leaks = judge_anonymity(["1.2.3.4"], {"via": "1.1 squid"}, "1.2.3.4", self.REAL)
        assert anon == "anonymous"
        assert "via" in leaks

    def test_anonymous_when_exit_differs(self):
        """出口 IP 与代理 IP 不同（多级跳转）但没有泄漏 → 匿名。"""
        anon, _ = judge_anonymity(["5.6.7.8"], {}, "1.2.3.4", self.REAL)
        assert anon == "anonymous"

    def test_proxy_connection_alone_is_not_downgrade(self):
        """proxy-connection 只是协商头，不应把 elite 降级。"""
        anon, _ = judge_anonymity(["1.2.3.4"], {"proxy-connection": "close"}, "1.2.3.4", self.REAL)
        assert anon == "elite"
