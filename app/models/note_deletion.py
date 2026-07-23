"""笔记删除台账模型:删除是不可逆操作,终态必须可追溯(skills 侧反馈 2026-07-23)。

内存台账(note_delete 服务的 _registry)重启即丢——纯内存时代,轮询中 server 重启会让
"删了没删"永久不可查,只能人工去创作中心核对;而删重复恰恰高发于当天(数据看板
no_data 盲区,连导出对账都不可用)。故落一张小表镜像台账:

- running 行在后台任务起步时写入,终态(done/error)时更新;
- server 重启后,内存台账丢失但 DB 行还在:终态行照常可查(盲区闭合);
  卡在 running 的行 = 任务被重启打断,结果未知,由读方(REST 层)译成 unknown 语义。
删除量极低(人工触发),不设 TTL 清理。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NoteDeletion(Base):
    """一次按标题删除任务的持久台账;主键即对外的 deletion_id(uuid hex)。"""

    __tablename__ = "note_deletions"

    id: Mapped[str] = mapped_column(primary_key=True)  # deletion_id(uuid hex)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    title: Mapped[str] = mapped_column()
    # running / done / error(重启遗孤的 running 由读方译成 unknown,库内不改写)
    status: Mapped[str] = mapped_column(default="running")
    deleted: Mapped[int] = mapped_column(default=0)
    remaining: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
