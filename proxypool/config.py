"""ProxyPool 配置加载。

设计要点：
- 配置全部来自 config.yaml，代码里不写死任何阈值
- 启动时校验必填项，缺配置立即失败（fail fast），不带错配置跑
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
    # 单源条数上限（0 = 用全局 fetcher.max_per_source）。
    # 大源（如 openproxy 有 6 万条、存活率仅 4%）会淹没小源的高质量数据，
    # 给它限流才能保留源头多样性。
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
    check_url_http: str = "http://httpbin.org/get"
    check_url_https: str = "https://api.ipify.org?format=json"
    attempts: int = 2
    retry_delay: float = 0.4


@dataclass
class PolicyConfig:
    promote_consecutive_ok: int = 1
    kill_consecutive_fail: int = 2
    ban_consecutive_fail: int = 8
    dead_cooldown: int = 0
    ban_cooldown: int = 604800
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
    # 冷池上限（new + candidate）。超过时按 score 淘汰最差的。
    # 实测依据：免费源每轮能抓 2 万条，但单次存活率只有 11-18%，
    # 其中绝大多数是从没活过的死地址。全部保留会让每轮预算被死数据吃掉，
    # 活着的那批反而轮不到复检。上限控制在有希望的范围内才跑得动。
    cold_pool_limit: int = 15000
    # dead 真清阈值：连续失败达 N 次、且最后一次检查已过 N 秒，直接从库删除。
    # 不留着"冷却重测"，因为实测会造成无限循环（3 万条死代理反复回炉，库无限膨胀）。
    dead_purge_min_fails: int = 2
    dead_purge_seconds: int = 60
    # banned 的保留时长：ban_cooldown 是"不再重测"，不是"永久保留"。
    # 实测一天会累积上千条，超过此期限直接删库。
    banned_retention_seconds: int = 86400
    # 墓碑有效期：判死的代理在这段时间内不再被重新抓取入库。
    # 防止「失败→判死→删除→重新抓取→再验证」的死循环（实测一次重新灌入 13809 条）。
    # 6 小时是折中：免费代理极少在 6 小时内复活，同时不至于永久拉黑误杀。
    tombstone_seconds: int = 21600
    # 每轮清理后做一次 WAL checkpoint（仅 sqlite），避免 WAL 无限增长
    wal_checkpoint: bool = True


@dataclass
class ApiConfig:
    # 默认 0.0.0.0：127.0.0.1 会让局域网/其他机器连不上，
    # 这是踩过的坑（用户拿到地址却打不开）。
    host: str = "0.0.0.0"
    port: int = 8088
    admin_token: str = ""
    max_batch: int = 100


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
