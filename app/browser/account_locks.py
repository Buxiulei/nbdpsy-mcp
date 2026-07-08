"""进程级共享的按账号互斥锁:发布链与 cookie 检测共用同一把 per-account 锁。

同一账号的浏览器操作(发布 / cookie 检测)共用一套 profile 目录
``DATA_DIR/browser/account_{id}``;``SyncClient.start()`` 启动时会 ``kill_orphans`` 按 argv
精确杀该 profile 的所有 camoufox 进程。若发布与检测各持一把**独立**锁,同号两条路径可能同时
打开浏览器 → 后到者的 ``kill_orphans`` 会**误杀正在发帖的另一条链**(发布杀检测 / 检测杀发布),
造成发布中断 / Firefox 单写锁 profile 损坏。

故两条路径必须**共用同一把锁**,使同一账号的浏览器操作串行。单进程单事件循环下,模块级单例
``account_locks`` 的同一把 ``asyncio.Lock`` 在发布调度器与 cookie 检测后台任务之间生效。
"""

import asyncio


class AccountLocks:
    """按 account_id 惰性分配 ``asyncio.Lock``;同一 account_id 恒返回同一把锁。

    同号并发浏览器操作(发布 / 检测)会互相踩 profile、``kill_orphans`` 误杀,必须串行;
    不同号各自一把锁互不阻塞。
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, account_id: int) -> asyncio.Lock:
        """取该账号的锁;首次访问时惰性创建并缓存。"""
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock


# 进程级共享单例:发布调度器(经 server 装配)与 cookie 检测后台任务共用同一实例,
# 保证同号"发布 / 检测"两条浏览器链串行,避免 kill_orphans 互相误杀。
account_locks = AccountLocks()
