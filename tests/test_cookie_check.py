"""异步 cookie 检测服务(app.services.cookie_check)单测,不起真浏览器。

覆盖本次修复三点:
- 共享锁:cookie 检测与发布调度器用**同一把** per-account 锁(app.browser.account_locks
  的进程级单例)——断言二者对同号 get 到同一个 Lock 对象;并断言同号检测会在发布持有的
  锁上串行等待(避免 SyncClient.start() 的 kill_orphans 互杀)。
- 台账驱逐:超龄终态条目在读/写路径被清理,_registry 不无界增长。
- 抛异常终态:check_login_once **抛异常**(而非返回 error)时,_run_check 底部 except 把
  台账落成 error 终态,不让轮询方死等 checking。
"""

import asyncio
from datetime import datetime, timedelta

from app.browser.account_locks import account_locks
from app.publish.scheduler import PublishScheduler
from app.services import cookie_check


# 用不易与其它测试撞车的高位 account_id,避免共享单例里跨 event loop 复用同一把锁。
_ACC_IDENTITY = 90200
_ACC_SERIALIZE = 90201


def _seed_entry(check_id: str, account_id: int, status: str, created_at) -> None:
    """直接往台账塞一条条目,供驱逐/终态断言用。"""
    cookie_check._registry[check_id] = {
        "status": status,
        "account_id": account_id,
        "user_info": None,
        "reason": None,
        "created_at": created_at,
    }


async def _clean_state():
    """清空共享锁与台账,隔离各测试(共享单例跨测试/跨 event loop 会泄漏锁对象)。"""
    account_locks._locks.clear()
    cookie_check._registry.clear()
    cookie_check._tasks.clear()


# ---------------- 共享 per-account 锁 ----------------


async def test_check_and_publish_share_same_lock_object(db_factory):
    """cookie 检测与发布调度器对同号 get 到**同一个** Lock 对象(共享进程级单例)。"""
    await _clean_state()
    # 生产 server.py 用共享单例装配 scheduler,此处复刻该 wiring
    scheduler = PublishScheduler(db_factory, account_locks=account_locks)

    # cookie_check 直接引用同一个模块级单例
    assert cookie_check.account_locks is account_locks
    # 同号两条路径 get 到的是同一把锁,不同号不同锁
    assert scheduler._account_locks.get(_ACC_IDENTITY) is account_locks.get(_ACC_IDENTITY)
    assert account_locks.get(_ACC_IDENTITY) is not account_locks.get(_ACC_IDENTITY + 1)


async def test_check_serializes_behind_publish_held_lock(monkeypatch):
    """发布侧持有该号锁时,同号检测在同一把锁上等待,不并发进入浏览器检测。"""
    await _clean_state()
    entered = {"v": False}

    def fake_check_login_once(account_id, cookies):
        entered["v"] = True
        # 返回 error 终态 → _run_check 走"不写回 DB"分支,测试不碰生产库
        return {"status": "error", "user_info": None, "reason": "stub"}

    monkeypatch.setattr(
        cookie_check.sync_client, "check_login_once", fake_check_login_once
    )

    lock = account_locks.get(_ACC_SERIALIZE)  # 发布侧会用的同一把锁
    await lock.acquire()  # 模拟"发布正在进行,持有该号锁"
    try:
        cookie_check.start_check(_ACC_SERIALIZE, [])
        await asyncio.sleep(0.05)
        # 被同一把锁挡住,后台检测未进入 check_login_once
        assert entered["v"] is False
    finally:
        lock.release()

    # 释放后检测拿到锁继续,进入检测
    for _ in range(200):
        if entered["v"]:
            break
        await asyncio.sleep(0.01)
    assert entered["v"] is True
    # 等后台任务收尾,避免 pending task 警告污染其它测试
    pending = list(cookie_check._tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await _clean_state()


# ---------------- 台账驱逐(M1)----------------


async def test_stale_terminal_entries_are_evicted():
    """超龄的终态条目在读/写路径被驱逐;进行中(checking)与未超龄的保留。"""
    await _clean_state()
    now = datetime.utcnow()
    old = now - timedelta(hours=2)  # 超 _ENTRY_TTL(1h)

    _seed_entry("old-valid", 1, "valid", old)  # 超龄终态 → 应驱逐
    _seed_entry("old-error", 2, "error", old)  # 超龄终态 → 应驱逐
    _seed_entry("old-checking", 3, "checking", old)  # 超龄但进行中 → 保留
    _seed_entry("fresh-valid", 4, "valid", now)  # 未超龄终态 → 保留

    # 读路径触发驱逐
    cookie_check.get_check("whatever")

    assert "old-valid" not in cookie_check._registry
    assert "old-error" not in cookie_check._registry
    assert "old-checking" in cookie_check._registry
    assert "fresh-valid" in cookie_check._registry
    await _clean_state()


# ---------------- 抛异常终态(M3)----------------


async def test_run_check_exception_lands_error_entry(monkeypatch):
    """check_login_once 抛异常(非返回 error):_run_check 兜底 except 落 error 终态。"""
    await _clean_state()
    account_id = 90300
    check_id = "exc-check"
    _seed_entry(check_id, account_id, "checking", datetime.utcnow())

    def boom(account_id, cookies):
        raise RuntimeError("检测炸了")

    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", boom)

    await cookie_check._run_check(check_id, account_id, [])

    entry = cookie_check._registry[check_id]
    assert entry["status"] == "error"  # 不卡 checking
    assert entry["reason"] is not None and "检测炸了" in entry["reason"]
    await _clean_state()
