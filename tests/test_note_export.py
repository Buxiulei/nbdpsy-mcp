"""异步笔记导出服务(app.services.note_export)单测,不起真浏览器。

照 test_cookie_check.py 的 ephemeral 台账 + monkeypatch 浏览器边界模式。覆盖:
- start_export 立即返 export_id,台账初始 running;
- 成功流:monkeypatch export_notes 返假行 + upsert_notes → 轮询到 done + note_count;
- 失败流:monkeypatch export_notes 抛 CreatorExportError → 台账 error + reason,不崩、不落库;
- 台账驱逐:超龄终态条目在读/写路径被清理,_registry 不无界增长。

monkeypatch 点全打在 note_export 内部调的 SyncClient.start / export_notes / upsert_notes,
不真起浏览器;SyncClient.stop 对全 None 属性天然安全,无需 patch。
"""

import asyncio
from datetime import datetime, timedelta

from app.browser.account_locks import account_locks
from app.services import note_export


# 用不易与其它测试撞车的高位 account_id,避免共享单例里跨 event loop 复用同一把锁。
_ACC_RUNNING = 91000
_ACC_SUCCESS = 91001
_ACC_ERROR = 91002


def _seed_entry(export_id: str, account_id: int, status: str, created_at) -> None:
    """直接往台账塞一条条目,供驱逐断言用。"""
    note_export._registry[export_id] = {
        "status": status,
        "account_id": account_id,
        "note_count": 0,
        "reason": None,
        "created_at": created_at,
    }


async def _clean_state() -> None:
    """清空共享锁与台账,隔离各测试(共享单例跨测试/跨 event loop 会泄漏锁对象)。"""
    account_locks._locks.clear()
    note_export._registry.clear()
    note_export._tasks.clear()


async def _drain_and_clean() -> None:
    """等后台任务收尾再清状态,避免 pending task 警告污染其它测试。"""
    pending = list(note_export._tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await _clean_state()


async def _poll_terminal(export_id: str, tries: int = 300) -> dict:
    """小超时轮询台账到终态(done/error);超时即断言失败。"""
    for _ in range(tries):
        entry = note_export.get_export(export_id)
        if entry and entry["status"] in ("done", "error"):
            return entry
        await asyncio.sleep(0.01)
    raise AssertionError(f"后台导出任务未在超时内进入终态 export_id={export_id}")


def _stub_browser_boundary(monkeypatch) -> None:
    """把 note_export 内部的浏览器启动打桩成 no-op(不起真 camoufox)。"""
    # start 返成功但不建真 page(page 留 None);export_notes 由各测试单独 patch。
    monkeypatch.setattr(note_export.SyncClient, "start", lambda self: {"success": True})


# ---------------- start_export 立即返 running ----------------


async def test_start_returns_export_id_and_running(monkeypatch):
    """start_export 立即返 export_id,台账初始 running(后台任务尚未获得执行机会)。"""
    await _clean_state()
    _stub_browser_boundary(monkeypatch)
    monkeypatch.setattr(note_export, "export_notes", lambda page, acc, d, ts: [])

    async def fake_upsert(session, acc, rows, snapshot_date, now):
        return len(rows)

    monkeypatch.setattr(note_export, "upsert_notes", fake_upsert)

    export_id = note_export.start_export(_ACC_RUNNING, [])
    # 无 await:create_task 已排程但事件循环尚未回收控制权,状态必为 running。
    entry = note_export.get_export(export_id)
    assert isinstance(export_id, str) and export_id
    assert entry is not None
    assert entry["status"] == "running"
    assert entry["account_id"] == _ACC_RUNNING

    await _drain_and_clean()


# ---------------- 成功流:落库 + done + note_count ----------------


async def test_success_flow_stores_and_done(monkeypatch):
    """monkeypatch export_notes 返假行 + upsert → 轮询到 done,note_count 为处理条数。"""
    await _clean_state()
    _stub_browser_boundary(monkeypatch)

    fake_rows = [
        {"title": "笔记A", "publish_time": "t1", "likes": 5},
        {"title": "笔记B", "publish_time": "t2", "likes": 9},
    ]
    monkeypatch.setattr(note_export, "export_notes", lambda page, acc, d, ts: fake_rows)

    captured = {}

    async def fake_upsert(session, acc, rows, snapshot_date, now):
        captured["account_id"] = acc
        captured["rows"] = rows
        captured["snapshot_date"] = snapshot_date
        return len(rows)

    monkeypatch.setattr(note_export, "upsert_notes", fake_upsert)

    export_id = note_export.start_export(_ACC_SUCCESS, [])
    entry = await _poll_terminal(export_id)

    assert entry["status"] == "done"
    assert entry["note_count"] == 2
    assert entry["reason"] is None
    # 落库拿到的是导出器返回的行,account_id 与 snapshot_date 由 service 层生成注入。
    assert captured["account_id"] == _ACC_SUCCESS
    assert captured["rows"] == fake_rows
    assert captured["snapshot_date"]  # 形如 2026-07-13,非空

    await _drain_and_clean()


# ---------------- 失败流:error 台账 + reason,不崩、不落库 ----------------


async def test_export_error_lands_error_entry(monkeypatch):
    """export_notes 抛 CreatorExportError → 台账 error + reason,后台 loop 不崩、不写半截数据。"""
    await _clean_state()
    _stub_browser_boundary(monkeypatch)

    def boom(page, acc, download_dir, ts):
        raise note_export.CreatorExportError("need_manual_login")

    monkeypatch.setattr(note_export, "export_notes", boom)

    upsert_called = {"v": False}

    async def fake_upsert(session, acc, rows, snapshot_date, now):
        upsert_called["v"] = True
        return len(rows)

    monkeypatch.setattr(note_export, "upsert_notes", fake_upsert)

    export_id = note_export.start_export(_ACC_ERROR, [])
    entry = await _poll_terminal(export_id)

    assert entry["status"] == "error"
    assert entry["reason"] is not None and "need_manual_login" in entry["reason"]
    assert entry["note_count"] == 0
    # 导出失败绝不落库(不写半截数据)。
    assert upsert_called["v"] is False

    await _drain_and_clean()


# ---------------- 台账驱逐 ----------------


async def test_stale_terminal_evicted():
    """超龄的终态条目在读/写路径被驱逐;进行中(running)与未超龄的保留。"""
    await _clean_state()
    now = datetime.utcnow()
    old = now - timedelta(hours=2)  # 超 _ENTRY_TTL(1h)

    _seed_entry("old-done", 1, "done", old)  # 超龄终态 → 应驱逐
    _seed_entry("old-error", 2, "error", old)  # 超龄终态 → 应驱逐
    _seed_entry("old-running", 3, "running", old)  # 超龄但进行中 → 保留
    _seed_entry("fresh-done", 4, "done", now)  # 未超龄终态 → 保留

    # 读路径触发驱逐
    note_export.get_export("whatever")

    assert "old-done" not in note_export._registry
    assert "old-error" not in note_export._registry
    assert "old-running" in note_export._registry
    assert "fresh-done" in note_export._registry

    await _clean_state()
