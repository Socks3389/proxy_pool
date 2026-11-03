"""数据模型与建表。

代理状态机：
    new      刚抓到，还没验证过
    candidate 验证失败但没到淘汰线，等待复验
    alive    当前可用
    dead     连续失败已下线，冷却后回 candidate
    banned   反复失败已拉黑，长期冷却
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """统一返回 naive UTC。

    为什么不用 timezone-aware：
    SQLite 的 DateTime 列不保存时区，写入时把 aware 对象变成 naive 存储，
    读回来还是 naive。若查询条件用 aware 而库里是 naive，
    SQLAlchemy 就算不报错，区间比较也会错（实测墓碑过滤因此完全失效）。
    全库统一 naive UTC 是 SQLite 项目最省事的做法；
    换 PostgreSQL 时该列是 timestamptz，需要改回 aware。
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Proxy(Base):
    """代理主表。一个 (protocol, ip, port) 唯一。"""

    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)  # http/https/socks4/socks5
    ip: Mapped[str] = mapped_column(String(45), nullable=False)       # 支持 IPv6
    port: Mapped[int] = mapped_column(Integer, nullable=False)

    # 状态机
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="new", server_default="new")
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")

    # 验证统计
    ok_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consecutive_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consecutive_fail: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # 性能指标
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_avg_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 能力标记（只有约 16% 的 HTTP 代理支持 CONNECT，单独记）
    anonymity: Mapped[str | None] = mapped_column(String(12), nullable=True)   # elite/anonymous/transparent
    supports_https: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # HTTPS 能力不能只记布尔值：实测同一代理的 TLS 通过率波动很大（部分按 SNI 过滤），
    # 用成功/尝试次数算出真实通过率，取用时才能按可靠性筛选
    https_ok_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    https_try_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    exit_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    leak_headers: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON 数组字符串
    last_error: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # 来源追踪
    first_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # 取用统计：被爬虫取走多少次、爬虫上报失效多少次。
    # 用来判断池子的真实利用率 —— 有些代理验证通过但取走后从没成功过，
    # 这类光看验证数据是看不出来的。
    served_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    client_fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # 时间戳
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    checks: Mapped[list[CheckLog]] = relationship(back_populates="proxy", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("protocol", "ip", "port", name="uq_proxy_identity"),
        Index("ix_proxy_pick", "status", "next_check_at"),
        Index("ix_proxy_score", "status", "score"),
        Index("ix_proxy_last_ok", "last_ok_at"),
        Index("ix_proxy_https", "status", "supports_https"),
    )

    @property
    def url(self) -> str:
        return f"{self.protocol}://{self.ip}:{self.port}"


class CheckLog(Base):
    """每次验证一条流水，用于趋势分析与清理。"""

    __tablename__ = "check_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proxy_id: Mapped[int] = mapped_column(ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(120), nullable=True)
    anonymity: Mapped[str | None] = mapped_column(String(12), nullable=True)
    supports_https: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    proxy: Mapped[Proxy] = relationship(back_populates="checks")

    __table_args__ = (
        Index("ix_checklog_proxy_time", "proxy_id", "checked_at"),
        Index("ix_checklog_time", "checked_at"),
    )


class RoundStat(Base):
    """每轮调度汇总，用于看板趋势和故障排查。"""

    __tablename__ = "round_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    fetched_sources: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fetched_raw: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fetched_new: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    checked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    promoted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    killed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    banned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pruned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_round_started", "started_at"),)


class Tombstone(Base):
    """墓碑表：记录已判死的代理，防止清理后被源重新抓回来反复验证。

    purge 删掉 dead 之后，下一轮抓取会把同一批地址当新的重新入库
    （实测新增 13809 条全是刚删的），形成
    「验证失败 → 判死 → 删除 → 重新抓取 → 再验证」的死循环，白烧 CPU 和带宽。
    墓碑表只存身份+死亡时间，体积远小于完整行，也不参与业务查询。
    """

    __tablename__ = "tombstones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    died_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("protocol", "ip", "port", name="uq_tombstone_identity"),
        Index("ix_tombstone_died", "died_at"),
    )


async def create_all(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # SQLite 并发写优化：WAL 模式让读写不互相阻塞
        if engine.url.get_backend_name() == "sqlite":
            for pragma in (
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA busy_timeout=15000",
                "PRAGMA cache_size=-32000",
            ):
                await conn.execute(text(pragma))
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(sync_conn) -> None:
    """给已有表补上模型里新增的列。

    create_all 只建表不改表，升级后老库会缺列，查询直接报 no such column。
    这里按模型定义对比实际列，缺什么补什么，避免为此引入 Alembic。
    只支持加列，改类型和删列仍需手工处理。
    """
    from sqlalchemy import inspect

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl_type = col.type.compile(dialect=sync_conn.dialect)
            default = ""
            if col.server_default is not None:
                arg = getattr(col.server_default, "arg", None)
                if arg is not None:
                    default = f" DEFAULT {arg}"
            sync_conn.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}'
            )
