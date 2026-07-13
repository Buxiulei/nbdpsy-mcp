"""笔记指标服务层:导出行 upsert(最新快照 + 每日趋势)+ RBAC 收窄的读取。

约定(与 account_service 一致):纯业务逻辑,用调用方传入的 AsyncSession——只 add/query/commit,
不自开引擎/事务边界。
- upsert_notes:SQLite 兼容的"先 select 唯一键、有则 update 无则 insert",不用 dialect-specific
  upsert。每行同时维护 NoteMetric(最新快照,updated_at=传入 now)与 NoteMetricDaily
  (按含 snapshot_date 的唯一键:当天覆盖、跨天加行);返回处理条数。
- list_notes / note_trend:经 assert_account_access(admin 全见,operator 仅授权号,无权抛
  AccessDenied),读最新快照 / 某笔记的 daily 升序序列。
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import assert_account_access
from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.operator import Operator

# 11 指标列:每次 upsert 从导出行覆盖到两表(缺列时保留模型默认 int 0 / float 0.0)。
_METRIC_FIELDS = (
    "likes",
    "collects",
    "comments",
    "danmu",
    "shares",
    "reposts",
    "follows",
    "exposure",
    "views",
    "cover_ctr",
    "avg_view_duration",
)


def _apply_metrics(obj, row: dict) -> None:
    """把导出行里出现的指标字段覆盖到 ORM 对象;缺失字段不动(留模型默认或旧值)。"""
    for field in _METRIC_FIELDS:
        if field in row:
            setattr(obj, field, row[field])


def _note_view(m: NoteMetric) -> dict:
    """把最新快照序列化为对外视图(account_id/title/publish_time + 11 指标 + updated_at)。"""
    view = {
        "account_id": m.account_id,
        "title": m.title,
        "publish_time": m.publish_time,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    for field in _METRIC_FIELDS:
        view[field] = getattr(m, field)
    return view


def _daily_view(d: NoteMetricDaily) -> dict:
    """把每日趋势行序列化为对外视图(含 snapshot_date + 11 指标)。"""
    view = {
        "account_id": d.account_id,
        "title": d.title,
        "publish_time": d.publish_time,
        "snapshot_date": d.snapshot_date,
    }
    for field in _METRIC_FIELDS:
        view[field] = getattr(d, field)
    return view


async def upsert_notes(
    session: AsyncSession,
    account_id: int,
    rows: list[dict],
    snapshot_date: str,
    now: datetime,
) -> int:
    """按唯一键 upsert 每行到 NoteMetric(最新快照)与 NoteMetricDaily(当天),返回处理条数。

    每行须含 title / publish_time;指标字段按 _METRIC_FIELDS 覆盖(缺列保留旧值/默认)。
    NoteMetric 唯一键 (account_id, title, publish_time)——重复导出覆盖成最新,updated_at=now;
    NoteMetricDaily 唯一键含 snapshot_date——当天重导覆盖、跨天加行。
    """
    for row in rows:
        title = row["title"]
        publish_time = row["publish_time"]

        # 最新快照:有则更新、无则插入
        snapshot = (
            await session.execute(
                select(NoteMetric).where(
                    NoteMetric.account_id == account_id,
                    NoteMetric.title == title,
                    NoteMetric.publish_time == publish_time,
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            snapshot = NoteMetric(
                account_id=account_id, title=title, publish_time=publish_time
            )
            session.add(snapshot)
        _apply_metrics(snapshot, row)
        snapshot.updated_at = now

        # 当天趋势行:同 snapshot_date 覆盖、跨天加行
        daily = (
            await session.execute(
                select(NoteMetricDaily).where(
                    NoteMetricDaily.account_id == account_id,
                    NoteMetricDaily.title == title,
                    NoteMetricDaily.publish_time == publish_time,
                    NoteMetricDaily.snapshot_date == snapshot_date,
                )
            )
        ).scalar_one_or_none()
        if daily is None:
            daily = NoteMetricDaily(
                account_id=account_id,
                title=title,
                publish_time=publish_time,
                snapshot_date=snapshot_date,
            )
            session.add(daily)
        _apply_metrics(daily, row)

    await session.commit()
    return len(rows)


async def list_notes(
    session: AsyncSession, operator: Operator, account_id: int
) -> list[dict]:
    """RBAC 收窄后按 id 升序读该号最新快照;operator 无授权抛 AccessDenied。"""
    await assert_account_access(operator, account_id, session)
    rows = (
        await session.execute(
            select(NoteMetric)
            .where(NoteMetric.account_id == account_id)
            .order_by(NoteMetric.id)
        )
    ).scalars().all()
    return [_note_view(m) for m in rows]


async def note_trend(
    session: AsyncSession,
    operator: Operator,
    account_id: int,
    title: str,
    publish_time: str,
) -> list[dict]:
    """RBAC 收窄后读某笔记的 daily 序列,按 snapshot_date 升序;无授权抛 AccessDenied。"""
    await assert_account_access(operator, account_id, session)
    rows = (
        await session.execute(
            select(NoteMetricDaily)
            .where(
                NoteMetricDaily.account_id == account_id,
                NoteMetricDaily.title == title,
                NoteMetricDaily.publish_time == publish_time,
            )
            .order_by(NoteMetricDaily.snapshot_date)
        )
    ).scalars().all()
    return [_daily_view(d) for d in rows]
