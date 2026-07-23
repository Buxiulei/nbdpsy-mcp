"""创作中心笔记删除的进程级内存台账:后台起浏览器按标题删笔记。

对齐 note_export 的 ephemeral 台账设计(进程内存 registry + 后台 asyncio 任务 →
asyncio.to_thread 跑同步浏览器 + AccountLocks 串行 + TTL 驱逐),细节注释见
``note_export``,此处不重复。差异点:

- 删除是**不可逆**破坏性操作:浏览器层(app.browser.note_delete)有确认弹窗文案
  必须含「删除」的防误点闸,服务层不再重复校验;
- 结果字段为 ``deleted``(实际删除数)与 ``remaining``(剩余同题卡数),
  同题多篇(重复发布)用 ``count`` 一次会话删多篇。
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_delete import NoteDeleteError, delete_notes_by_title
from app.browser.sync_client import SyncClient
from app.core.db import get_session
from app.models.note_deletion import NoteDeletion

_TERMINAL_STATUSES = ("done", "error")
_ENTRY_TTL = timedelta(hours=1)

# deletion_id -> {"status","account_id","title","deleted","remaining","reason","created_at"}
_registry: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def _evict_stale() -> None:
    """驱逐超龄终态条目(running 不动),防 _registry 无界增长。"""
    cutoff = datetime.utcnow() - _ENTRY_TTL
    stale = [
        deletion_id
        for deletion_id, entry in _registry.items()
        if entry["status"] in _TERMINAL_STATUSES and entry["created_at"] <= cutoff
    ]
    for deletion_id in stale:
        _registry.pop(deletion_id, None)


def start_delete(
    account_id: int, cookies: list[dict], title: str, count: int = 1
) -> str:
    """登记 running 台账并起后台删除任务,立即返回 deletion_id。"""
    _evict_stale()
    deletion_id = uuid.uuid4().hex
    _registry[deletion_id] = {
        "status": "running",
        "account_id": account_id,
        "title": title,
        "deleted": 0,
        "remaining": None,
        "reason": None,
        "created_at": datetime.utcnow(),
    }
    task = asyncio.create_task(_run_delete(deletion_id, account_id, cookies, title, count))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return deletion_id


def get_delete(deletion_id: str) -> dict | None:
    """按 deletion_id 取台账条目;不存在返回 None。"""
    _evict_stale()
    return _registry.get(deletion_id)


def _delete_sync(account_id: int, cookies: list[dict], title: str, count: int) -> dict:
    """同一线程内:建 SyncClient → start → 按标题删除 → stop 收尾(finally 防泄漏)。"""
    # 删除要在笔记管理页悬停/点击真实卡片,保留图片渲染(block_images 会缺封面影响布局)
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteDeleteError(f"browser_start_failed: {start.get('error')}")
        return delete_notes_by_title(client.page, account_id, title, count)
    finally:
        client.stop()


async def _run_delete(
    deletion_id: str, account_id: int, cookies: list[dict], title: str, count: int
) -> None:
    """后台删除:落 running DB 行 → 持号锁串行 → 浏览器删除 → 终态双写;异常落 error 不上抛。

    DB 台账(skills 侧反馈 2026-07-23):删除不可逆,内存台账重启即丢会让"删了没删"
    永久不可查。running 行在此起步时写入;server 若在删除中途重启,该行永远停在
    running —— 由读方(get_delete_persisted)译成 unknown 语义,不误报也不丢历史。
    """
    try:
        await _db_insert_running(deletion_id, account_id, title)
    except Exception as exc:  # DB 台账写失败不拦删除主流程(内存台账仍在)
        logger.warning(f"删除台账 DB 写入失败(不拦主流程) deletion_id={deletion_id}: {exc}")
    try:
        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(
                    _delete_sync, account_id, cookies, title, count
                )
        await _finalize(deletion_id, "done",
                        deleted=result["deleted"], remaining=result["remaining"],
                        reason=None)
    except NoteDeleteError as exc:
        logger.warning(
            f"笔记删除失败 deletion_id={deletion_id} account_id={account_id} "
            f"title={title!r} reason={exc.reason}"
        )
        await _finalize(deletion_id, "error", deleted=0, remaining=None,
                        reason=exc.reason)
    except Exception as exc:  # 兜底:任务异常也要落终态,别让台账永远 running
        logger.exception(
            f"笔记删除任务异常 deletion_id={deletion_id} account_id={account_id}"
        )
        await _finalize(deletion_id, "error", deleted=0, remaining=None,
                        reason=f"删除任务异常:{exc}")


async def _db_insert_running(deletion_id: str, account_id: int, title: str) -> None:
    """向 DB 台账落一条 running 行(后台任务起步时)。"""
    async with get_session() as session:
        session.add(NoteDeletion(
            id=deletion_id, account_id=account_id, title=title,
            status="running", deleted=0,
        ))
        await session.commit()


async def _finalize(
    deletion_id: str, status: str, deleted: int, remaining: int | None,
    reason: str | None,
) -> None:
    """终态双写:内存台账 + DB 台账(DB 失败只告警,内存结果仍可轮询到)。"""
    _update_entry(deletion_id, status, deleted=deleted, remaining=remaining,
                  reason=reason)
    try:
        async with get_session() as session:
            row = await session.get(NoteDeletion, deletion_id)
            if row is not None:
                row.status = status
                row.deleted = deleted
                row.remaining = remaining
                row.reason = reason
                await session.commit()
    except Exception as exc:
        logger.warning(f"删除台账 DB 终态更新失败 deletion_id={deletion_id}: {exc}")


async def get_delete_persisted(deletion_id: str) -> dict | None:
    """内存台账 miss 后的 DB 回退读(REST 层用)。

    - 终态行(done/error)原样返回 —— 重启后"删了没删"仍可查(盲区闭合);
    - running 行且内存无此条 = 任务被重启打断,结果**未知**:译成
      status="unknown" + reason 说明,绝不冒充 running(它永远不会完成)。
    """
    async with get_session() as session:
        row = await session.get(NoteDeletion, deletion_id)
    if row is None:
        return None
    entry = {
        "status": row.status,
        "account_id": row.account_id,
        "title": row.title,
        "deleted": row.deleted,
        "remaining": row.remaining,
        "reason": row.reason,
    }
    if row.status == "running":
        # 内存台账没有而 DB 停在 running:server 重启打断,结果未知
        entry["status"] = "unknown"
        entry["reason"] = (
            "server 重启中断了删除任务,是否已删结果未知:请到创作中心笔记管理页"
            "人工核对该标题笔记数量后再决定是否重新发起"
        )
    return entry


def _update_entry(
    deletion_id: str, status: str, deleted: int, remaining: int | None,
    reason: str | None,
) -> None:
    """把删除结果更新进台账条目(条目已被同步移除时静默跳过)。"""
    entry = _registry.get(deletion_id)
    if entry is not None:
        entry["status"] = status
        entry["deleted"] = deleted
        entry["remaining"] = remaining
        entry["reason"] = reason
