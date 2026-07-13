"""笔记指标模型:创作中心 Excel 导出的最新快照 + 每日趋势两表。

按 (account_id, title, publish_time) 三元组定位一条笔记——小红书创作中心导出无 note_id /
封面 URL,故以该三元组作业务唯一键。publish_time 存 Excel 原文字符串(如
"2026年05月22日10时59分14秒"),不强解析、原样落库。11 指标列 int 默认 0 / float 默认 0.0。

- NoteMetric:每账号每笔记仅一行,每次导出 upsert 覆盖成最新;额外 updated_at。
- NoteMetricDaily:每账号每笔记每天一行,当天重导覆盖、跨天加行;额外 snapshot_date。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NoteMetric(Base):
    """某账号某笔记的最新指标快照;(account_id, title, publish_time) 全局唯一。"""

    __tablename__ = "note_metrics"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "title", "publish_time", name="uq_note_metric"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    title: Mapped[str] = mapped_column()
    # Excel 原文发布时间字符串,不强解析
    publish_time: Mapped[str] = mapped_column()
    # 9 个整数指标
    likes: Mapped[int] = mapped_column(default=0)
    collects: Mapped[int] = mapped_column(default=0)
    comments: Mapped[int] = mapped_column(default=0)
    danmu: Mapped[int] = mapped_column(default=0)
    shares: Mapped[int] = mapped_column(default=0)
    reposts: Mapped[int] = mapped_column(default=0)
    follows: Mapped[int] = mapped_column(default=0)
    exposure: Mapped[int] = mapped_column(default=0)
    views: Mapped[int] = mapped_column(default=0)
    # 2 个浮点指标:封面点击率(%)、人均观看时长(秒)
    cover_ctr: Mapped[float] = mapped_column(default=0.0)
    avg_view_duration: Mapped[float] = mapped_column(default=0.0)
    # 最近一次 upsert 时刻(由调用方注入,便于测试)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


class NoteMetricDaily(Base):
    """某账号某笔记某天的指标行;(account_id, title, publish_time, snapshot_date) 唯一。"""

    __tablename__ = "note_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "title",
            "publish_time",
            "snapshot_date",
            name="uq_note_metric_daily",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    title: Mapped[str] = mapped_column()
    # Excel 原文发布时间字符串,不强解析
    publish_time: Mapped[str] = mapped_column()
    # 导出日 "YYYY-MM-DD"(调用方注入);字符串字典序即时序,便于升序趋势
    snapshot_date: Mapped[str] = mapped_column()
    # 9 个整数指标
    likes: Mapped[int] = mapped_column(default=0)
    collects: Mapped[int] = mapped_column(default=0)
    comments: Mapped[int] = mapped_column(default=0)
    danmu: Mapped[int] = mapped_column(default=0)
    shares: Mapped[int] = mapped_column(default=0)
    reposts: Mapped[int] = mapped_column(default=0)
    follows: Mapped[int] = mapped_column(default=0)
    exposure: Mapped[int] = mapped_column(default=0)
    views: Mapped[int] = mapped_column(default=0)
    # 2 个浮点指标:封面点击率(%)、人均观看时长(秒)
    cover_ctr: Mapped[float] = mapped_column(default=0.0)
    avg_view_duration: Mapped[float] = mapped_column(default=0.0)
