"""配置加载。

全部来自 config.yaml，代码里不写死阈值。启动时校验必填项，
配置错了直接抛异常，不带着错配置跑。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "config.yaml"


@dataclass
class SourceConfig:
    name: str
    url: str
    protocol: str = "http"
    enabled: bool = True
    # 单源条数上限，0 表示用全局 fetcher.max_per_source。
    # 大源会淹掉小源的高质量数据，限流才能保住源头多样性。
    max_per_source: int = 0


@dataclass
class FetcherConfig:
    concurrency: int = 20
    timeout: int = 25
    retries: int = 2
    max_per_source: int = 50000
    sources: list[SourceConfig] = field(default_factory=list)


@dataclass
class ValidatorConfig:
    concurrency: int = 200
    timeout: float = 8.0
    # 连接阶段超时，默认与全局一致。单独设短能提速约 8%，
    # 但会砍掉 6-10% 的慢链路代理，实测那批代理 81% 真能用，不划算。
    connect_timeout: float = 8.0
    check_url_http: str = "http://httpbin.org/get"
    check_url_https: str = "https://httpbin.org/get?probe=proxypool"
    attempts: int = 2
    retry_delay: float = 0.4


@dataclass
class PolicyConfig:
    promote_consecutive_ok: int = 1
    kill_consecutive_fail: int = 2
    ban_consecutive_fail: int = 8
    dead_cooldown: int = 0
    score_up: int = 2
    score_down: int = 30
    score_max: int = 100
    score_floor: int = 0


@dataclass
class SchedulerConfig:
    interval: int = 300
    refetch_every_round: bool = True
    candidate_batch: int = 12000
    round_budget: int = 280
    fast_promote: bool = True
    # 每隔几轮做一次带抓取的常规扩充，其余轮次只精检热池。
    # 6 轮 × 300 秒 = 30 分钟刷新一次源；热池则每 5 分钟 100% 复检。
    refresh_every_n_rounds: int = 6


@dataclass
class CleanupConfig:
    log_retention_days: int = 3
    stale_hours: int = 48
    max_rows: int = 200000
    # 冷池上限（new + candidate），超过就淘汰最差的。
    # 每轮能抓两万条，但单次存活率只有 11-18%，其余全是从没活过的地址。
    # 全留着会让死数据吃掉每轮预算，活着的反而轮不到复检。
    cold_pool_limit: int = 15000
    # dead 清理阈值：连续失败达 N 次且最后一次检查已过 N 秒，直接删。
    # 不做冷却重测，那会造成死循环（几万条死代理反复回炉，库只涨不跌）。
    dead_purge_min_fails: int = 2
    dead_purge_seconds: int = 60
    # banned 的保留时长：banned 不复检，这个值就是它从拉到删的寿命。
    # 实测一天会累积上千条，超过此期限直接删库。
    banned_retention_seconds: int = 86400
    # 墓碑有效期：判死的代理在这段时间内不再重新入库，
    # 防止「验死→删除→重抓→再验」的死循环。
    # 6 小时是折中：免费代理很少在 6 小时内复活，也不至于永久误杀。
    tombstone_seconds: int = 21600
    # 每轮清理后做一次 WAL checkpoint（仅 sqlite），避免 WAL 无限增长
    wal_checkpoint: bool = True


@dataclass
class ApiConfig:
    # 默认 0.0.0.0，绑 127.0.0.1 的话局域网和其他机器都连不上。
    host: str = "0.0.0.0"
    port: int = 8088
    admin_token: str = ""


@dataclass
class DatabaseConfig:
    url: str = "sqlite+aiosqlite:///./data/proxypool.db"
    echo: bool = False
    pool_size: int = 10


@dataclass
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    fetcher: FetcherConfig = field(default_factory=FetcherConfig)
    validator: ValidatorConfig = field(default_factory=ValidatorConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    api: ApiConfig = field(default_factory=ApiConfig)

    @property
    def db_url(self) -> str:
        """相对路径的 sqlite 转成基于项目根的绝对路径，避免受 cwd 影响。"""
        url = self.database.url
        if url.startswith("sqlite+aiosqlite:///./"):
            rel = url.replace("sqlite+aiosqlite:///./", "")
            return f"sqlite+aiosqlite:///{BASE_DIR / rel}"
        return url


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    sec = raw.get(name) or {}
    if not isinstance(sec, dict):
        raise ValueError(f"配置节 {name} 必须是映射，实际是 {type(sec).__name__}")
    return sec


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("PROXYPOOL_CONFIG") or DEFAULT_CONFIG)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是映射")

    sources_raw = _section(raw, "fetcher").get("sources") or []
    sources: list[SourceConfig] = []
    for s in sources_raw:
        if not isinstance(s, dict) or "url" not in s:
            raise ValueError(f"fetcher.sources 条目缺 url: {s!r}")
        proto = str(s.get("protocol", "http")).lower()
        if proto not in ("http", "https", "socks4", "socks5", "mixed"):
            raise ValueError(f"不支持的协议 {proto}（源 {s.get('name')}）")
        sources.append(
            SourceConfig(
                name=str(s.get("name") or s["url"])[:64],
                url=str(s["url"]),
                protocol=proto,
                enabled=bool(s.get("enabled", True)),
                max_per_source=int(s.get("max_per_source") or 0),
            )
        )
    enabled_sources = [s for s in sources if s.enabled]
    if not enabled_sources:
        raise ValueError("fetcher.sources 没有任何启用项，无法抓取")

    # URL 去重：同一 URL 配成两个源会白抓一遍，是配置笔误的典型症状
    seen_urls: dict[str, str] = {}
    for s in enabled_sources:
        if s.url in seen_urls:
            raise ValueError(
                f"fetcher.sources 里 {s.name} 和 {seen_urls[s.url]} 是同一个 URL，"
                f"请删掉其中一个或改成 enabled: false"
            )
        seen_urls[s.url] = s.name

    cfg = Config(
        database=DatabaseConfig(**_section(raw, "database")),
        fetcher=FetcherConfig(**{**_section(raw, "fetcher"), "sources": sources}),
        validator=ValidatorConfig(**_section(raw, "validator")),
        policy=PolicyConfig(**_section(raw, "policy")),
        scheduler=SchedulerConfig(**_section(raw, "scheduler")),
        cleanup=CleanupConfig(**_section(raw, "cleanup")),
        api=ApiConfig(**_section(raw, "api")),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.validator.concurrency < 1:
        raise ValueError("validator.concurrency 必须 >= 1")
    if cfg.validator.timeout <= 0:
        raise ValueError("validator.timeout 必须 > 0")
    if cfg.scheduler.interval < 30:
        raise ValueError("scheduler.interval 不能小于 30 秒")
    if cfg.policy.promote_consecutive_ok < 1:
        raise ValueError("policy.promote_consecutive_ok 必须 >= 1")
    if cfg.policy.kill_consecutive_fail < 1:
        raise ValueError("policy.kill_consecutive_fail 必须 >= 1")
    if cfg.policy.ban_consecutive_fail <= cfg.policy.kill_consecutive_fail:
        raise ValueError("policy.ban_consecutive_fail 必须大于 kill_consecutive_fail")
