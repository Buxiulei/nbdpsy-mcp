"""删除台账持久化测试(skills 侧反馈 2026-07-23:不可逆操作终态必须可追溯)。

隔离手法同 test_notes_rest.py:rest_client 真 lifespan 隔离库;不真起浏览器——
直接操纵 note_delete 服务的 DB 写入函数与内存台账,模拟"server 重启后内存台账丢失"。

覆盖:
- _db_insert_running + _finalize 双写后,清空内存台账(模拟重启),GET 仍能查到终态。
- DB 行停在 running 且内存无此条(重启打断)→ GET 返回 status=unknown + 指引 reason。
- 双写正常路径:内存命中时 GET 走内存(与 DB 一致)。
- 越权 operator 查他号 deletion → 403(DB 回退路径同样受 RBAC 收窄)。
"""

from app.services import note_delete
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


async def test_terminal_state_survives_restart(tmp_path, monkeypatch):
    """终态双写后清空内存台账(模拟重启),GET 回退 DB 仍返回 done 终态。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("删A", "uDA", _COOKIES)
        did = "del-persist-01"
        await note_delete._db_insert_running(did, acc, "某标题")
        await note_delete._finalize(did, "done", deleted=2, remaining=1, reason=None)

        note_delete._registry.clear()  # 模拟 server 重启:内存台账清零

        r = await c.get(f"/api/note-deletions/{did}", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "done"
        assert body["deleted"] == 2
        assert body["remaining"] == 1


async def test_interrupted_running_reports_unknown(tmp_path, monkeypatch):
    """DB 行停在 running 且内存无此条(重启打断)→ status=unknown + 人工核对指引。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("删B", "uDB", _COOKIES)
        did = "del-orphan-01"
        await note_delete._db_insert_running(did, acc, "被打断的标题")
        note_delete._registry.clear()  # 内存无此条 → running 行即重启遗孤

        r = await c.get(f"/api/note-deletions/{did}", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "unknown"
        assert "人工核对" in body["reason"]


async def test_memory_hit_path_unchanged(tmp_path, monkeypatch):
    """内存命中时走内存台账(热路径),行为与落库前一致。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("删C", "uDC", _COOKIES)
        did = "del-mem-01"
        await note_delete._db_insert_running(did, acc, "标题C")
        # 手工放一条内存台账(running),模拟任务进行中
        from datetime import datetime
        note_delete._registry[did] = {
            "status": "running", "account_id": acc, "title": "标题C",
            "deleted": 0, "remaining": None, "reason": None,
            "created_at": datetime.utcnow(),
        }
        r = await c.get(f"/api/note-deletions/{did}", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200
        assert r.json()["status"] == "running"  # 内存热路径,不被 DB 回退译成 unknown
        note_delete._registry.pop(did, None)


async def test_db_fallback_rbac_403(tmp_path, monkeypatch):
    """DB 回退路径同样受 RBAC 收窄:无该号授权的 operator 查 → 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("删D", "uDD", _COOKIES)
        did = "del-rbac-01"
        await note_delete._db_insert_running(did, acc, "标题D")
        await note_delete._finalize(did, "done", deleted=1, remaining=0, reason=None)
        note_delete._registry.clear()

        op_key = "op-del-ledger-b-01"
        await make_operator(op_key)  # 未授权 acc
        r = await c.get(f"/api/note-deletions/{did}", headers=bearer(op_key))
        assert r.status_code == 403
