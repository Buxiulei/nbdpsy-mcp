"""note_metrics_service 服务层单测:导出行 upsert(最新快照 + 每日趋势)+ RBAC 读取。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。核心断言:
- upsert 首次:NoteMetric 一行值对、NoteMetricDaily 一行含 snapshot_date。
- 同 (account_id,title,publish_time) 二次 upsert:快照仍一行且指标更新、updated_at 用传入 now。
- daily 同 snapshot_date 覆盖(一行)、跨天加行(两行)。
- list_notes RBAC:operator 仅见授权号、无授权抛 AccessDenied;admin 全见。
- note_trend 按 snapshot_date 升序返回。
"""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AccessDenied
from app.models import (
    NoteMetric,
    NoteMetricDaily,
    Operator,
    OperatorAccountAccess,
    XhsAccount,
)
from app.services import note_metrics_service as svc

# 三元组唯一键里固定的发布时间(Excel 原文,不强解析)。
_PUB = "2026年05月22日10时59分14秒"


async def _make_account(db: AsyncSession, name: str = "号1") -> XhsAccount:
    """造一个小红书账号并提交,返回对象(供 upsert/RBAC 引用真实 id)。"""
    acc = XhsAccount(name=name)
    db.add(acc)
    await db.commit()
    return acc


async def _make_operator(
    db: AsyncSession, role: str = "operator", name: str = "op"
) -> Operator:
    """造一个启用中的运营者(apikey_hash 占位,单测不走鉴权中间件)。"""
    op = Operator(name=name, role=role, apikey_hash=f"h-{name}", enabled=True)
    db.add(op)
    await db.commit()
    return op


async def _grant(db: AsyncSession, op: Operator, acc: XhsAccount) -> None:
    """给 operator 授权某账号。"""
    db.add(OperatorAccountAccess(operator_id=op.id, xhs_account_id=acc.id))
    await db.commit()


def _row(title: str = "笔记A", **overrides) -> dict:
    """构造一条导出行(11 指标齐全);overrides 覆写个别字段。"""
    row = {
        "title": title,
        "publish_time": _PUB,
        "likes": 10,
        "collects": 2,
        "comments": 1,
        "danmu": 0,
        "shares": 3,
        "reposts": 0,
        "follows": 5,
        "exposure": 100,
        "views": 80,
        "cover_ctr": 12.5,
        "avg_view_duration": 6.2,
    }
    row.update(overrides)
    return row


async def test_upsert_inserts_snapshot_and_daily(db: AsyncSession):
    """upsert 1 行 → NoteMetric 一行值对、NoteMetricDaily 一行含 snapshot_date。"""
    acc = await _make_account(db)
    now = datetime(2026, 7, 13, 12, 0, 0)
    n = await svc.upsert_notes(db, acc.id, [_row()], "2026-07-13", now)
    assert n == 1

    snaps = (await db.execute(select(NoteMetric))).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].account_id == acc.id
    assert snaps[0].publish_time == _PUB  # 原样存字符串
    assert snaps[0].likes == 10
    assert snaps[0].cover_ctr == 12.5
    assert snaps[0].updated_at == now

    dailies = (await db.execute(select(NoteMetricDaily))).scalars().all()
    assert len(dailies) == 1
    assert dailies[0].snapshot_date == "2026-07-13"
    assert dailies[0].likes == 10


async def test_upsert_same_note_updates_snapshot(db: AsyncSession):
    """同唯一键二次 upsert(likes 变)→ 快照仍一行且更新;updated_at 用传入 now。"""
    acc = await _make_account(db)
    now1 = datetime(2026, 7, 13, 12, 0, 0)
    await svc.upsert_notes(db, acc.id, [_row(likes=10)], "2026-07-13", now1)
    now2 = datetime(2026, 7, 14, 9, 30, 0)
    await svc.upsert_notes(db, acc.id, [_row(likes=99)], "2026-07-14", now2)

    snaps = (await db.execute(select(NoteMetric))).scalars().all()
    assert len(snaps) == 1  # 未新增行
    assert snaps[0].likes == 99  # 指标更新
    assert snaps[0].updated_at == now2  # 用传入 now


async def test_daily_same_date_overwrites_diff_date_appends(db: AsyncSession):
    """daily 同 snapshot_date 覆盖(一行)、不同 snapshot_date 加行(两行)。"""
    acc = await _make_account(db)
    now = datetime(2026, 7, 13, 12, 0, 0)
    await svc.upsert_notes(db, acc.id, [_row(likes=10)], "2026-07-13", now)
    # 当天重导:覆盖
    await svc.upsert_notes(db, acc.id, [_row(likes=20)], "2026-07-13", now)
    dailies = (
        await db.execute(
            select(NoteMetricDaily).order_by(NoteMetricDaily.snapshot_date)
        )
    ).scalars().all()
    assert len(dailies) == 1
    assert dailies[0].likes == 20  # 覆盖成最新

    # 跨天:加行
    await svc.upsert_notes(db, acc.id, [_row(likes=30)], "2026-07-14", now)
    dailies = (
        await db.execute(
            select(NoteMetricDaily).order_by(NoteMetricDaily.snapshot_date)
        )
    ).scalars().all()
    assert len(dailies) == 2
    assert [d.snapshot_date for d in dailies] == ["2026-07-13", "2026-07-14"]
    assert dailies[1].likes == 30


async def test_list_notes_rbac(db: AsyncSession):
    """operator 仅见授权号快照、无授权抛 AccessDenied;admin 全见。"""
    acc1 = await _make_account(db, "号1")
    acc2 = await _make_account(db, "号2")
    now = datetime(2026, 7, 13, 12, 0, 0)
    await svc.upsert_notes(db, acc1.id, [_row(title="A")], "2026-07-13", now)
    await svc.upsert_notes(db, acc2.id, [_row(title="B")], "2026-07-13", now)

    admin = await _make_operator(db, role="admin", name="admin")
    op = await _make_operator(db, role="operator", name="op")
    await _grant(db, op, acc1)

    # admin 全见
    assert len(await svc.list_notes(db, admin, acc1.id)) == 1
    assert len(await svc.list_notes(db, admin, acc2.id)) == 1

    # operator 见授权号
    got = await svc.list_notes(db, op, acc1.id)
    assert [g["title"] for g in got] == ["A"]

    # operator 无授权号 → AccessDenied
    with pytest.raises(AccessDenied):
        await svc.list_notes(db, op, acc2.id)


async def test_note_trend_ascending(db: AsyncSession):
    """某笔记多天 daily(乱序注入)→ note_trend 按 snapshot_date 升序返回。"""
    acc = await _make_account(db)
    admin = await _make_operator(db, role="admin", name="admin")
    now = datetime(2026, 7, 13, 12, 0, 0)
    # 乱序注入三天
    await svc.upsert_notes(db, acc.id, [_row(title="A", likes=3)], "2026-07-14", now)
    await svc.upsert_notes(db, acc.id, [_row(title="A", likes=1)], "2026-07-12", now)
    await svc.upsert_notes(db, acc.id, [_row(title="A", likes=2)], "2026-07-13", now)

    trend = await svc.note_trend(db, admin, acc.id, "A", _PUB)
    assert [t["snapshot_date"] for t in trend] == [
        "2026-07-12",
        "2026-07-13",
        "2026-07-14",
    ]
    assert [t["likes"] for t in trend] == [1, 2, 3]
