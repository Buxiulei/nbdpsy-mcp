"""发布内存队列(替代 celery 的立即发路径)。

- ``AccountLocks``:每账号互斥锁,现已迁到 ``app.browser.account_locks`` 做成**进程级共享
  单例**(发布与 cookie 检测共用同一把锁,避免同号浏览器操作互相 kill_orphans 误杀);此处
  仅 re-export 供 ``from app.publish.queue import AccountLocks`` 的旧引用继续可用。
- ``PublishQueue``:内存 ``asyncio.Queue`` + concurrency 个 worker 协程;``submit`` 立即
  入队,worker 取 job_id 调注入的 runner。单个 job 异常被捕获记录,不拖垮 worker。
"""

import asyncio
from typing import Awaitable, Callable

from loguru import logger

# per-account 锁类已迁到 app.browser.account_locks(与 cookie 检测共用进程级单例);
# 这里 re-export 保持 `from app.publish.queue import AccountLocks` 的向后兼容。
from app.browser.account_locks import AccountLocks  # noqa: F401


class PublishQueue:
    """内存发布队列:concurrency 个 worker 协程消费 ``submit`` 进来的 job_id。"""

    def __init__(self, concurrency: int) -> None:
        # 至少 1 个 worker,防止 concurrency 配 0 时队列永不被消费
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._runner: Callable[[int], Awaitable] | None = None

    def submit(self, job_id: int) -> None:
        """把 job_id 放入内存队列(非阻塞)。"""
        self._queue.put_nowait(job_id)

    def start(self, runner: Callable[[int], Awaitable]) -> None:
        """起 concurrency 个 worker 协程,循环取 job_id 调 runner;已启动则忽略重复调用。"""
        if self._workers:
            return
        self._runner = runner
        for _ in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._worker()))

    async def _worker(self) -> None:
        """worker 主循环:阻塞取 job_id → 调 runner;runner 异常只记录不退出。"""
        while True:
            job_id = await self._queue.get()
            try:
                await self._runner(job_id)
            except Exception:
                logger.exception("发布 worker 处理 job {} 异常", job_id)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """优雅停:取消所有 worker 协程并等待其退出。"""
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
