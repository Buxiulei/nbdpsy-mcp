# 浏览器并发闸 + 释放 + SQLite WAL 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps 用 checkbox。

**Goal:** 全局浏览器并发闸(超出排队不崩)+ 保证释放/孤儿回收(防内存泄露)+ SQLite WAL,支撑 20+ 运营。

**Architecture:** browser_gate 信号量(BROWSER_CONCURRENCY=6)套住 publish/cookie-check/note-export 三入口的 camoufox 启动;browser_reaper 周期清崩溃孤儿;SyncClient 加 block_images/block_webgl 瘦身;db.py sqlite WAL+busy_timeout。

**Spec:** `docs/design/2026-07-15-browser-concurrency-design.md`(接口/错误契约以 spec 为准)

## Global Constraints

- 解释器 `/home/roots/nbdpsy-server/.venv/bin/python`;测试 `source .../activate && python -m pytest tests/ -q`(cwd=worktree 根)。
- 全中文注释无 emoji;commit `type(scope): 描述`;显式列文件,禁 git add -A。
- 已核实:三入口都是 `async with account_locks.get(account_id): await asyncio.to_thread(<浏览器活>)`——`_publish_runner`(scheduler.py ~97)、`_run_check`(cookie_check.py)、`_run_export`(note_export.py:114)。gate 套在 **account_lock 内、to_thread 外**:`async with account_locks.get(id): async with browser_slot(): await to_thread(...)`。
- account_locks:`AccountLocks.get(id)->asyncio.Lock`(同号同锁),`_locks: dict[int,Lock]`;判在跑用 `lock.locked()`。
- profile_guard:`kill_orphans(profile_dir)` 扫 `/proc/<pid>/cmdline` + `_argv_targets_profile(argv, profile_dir)` 精确匹配;`profile_dir(account_id)` 返绝对路径。reaper 复用这些。
- SyncClient:`__init__(account_id, cookies, headless=True)`;`start()` 里 `launch_options(headless=..., block_webrtc=True, ...)`。
- db.py:模块级 `engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)`。
- 单 uvicorn worker = 单事件循环(信号量绑该 loop 安全)。

---

### Task 1: browser_gate 并发闸 + 三入口套闸 + 瘦身(核心,先行)

**Files:**
- Create: `app/browser/browser_gate.py`、`tests/test_browser_gate.py`
- Modify: `app/core/config.py`(BROWSER_CONCURRENCY)、`app/browser/sync_client.py`(block_images 参数 + block_webgl)、`app/publish/scheduler.py`、`app/services/cookie_check.py`、`app/services/note_export.py`(套闸 + 传 block_images)
- Test: `tests/test_browser_gate.py` + 现有三入口测试仍绿

**Interfaces(Produces):**
```python
# app/browser/browser_gate.py
def _get_semaphore() -> asyncio.Semaphore   # 懒建,按 settings.BROWSER_CONCURRENCY,进程级单例
@asynccontextmanager
async def browser_slot():                    # acquire 一个名额,满则排队;出作用域(含异常)release
```

- [ ] **Step 1: 写失败测试 `tests/test_browser_gate.py`**
```python
# 完整用例:
# 1. test_gate_caps_concurrency:BROWSER_CONCURRENCY=2(monkeypatch settings + 重置模块单例),
#    起 5 个 async 任务各 `async with browser_slot(): 记录当前在闸内计数 + await sleep(0.05)`,
#    断言峰值 in-flight ≤2、5 个全完成(排队不丢)
# 2. test_gate_releases_on_exception:闸内抛异常 → 名额归还(异常后再连续 acquire N 次不阻塞)
# 3. test_singleton:_get_semaphore() 两次返回同一对象
```
（BROWSER_CONCURRENCY monkeypatch:改 settings 后需重置 browser_gate 的模块级缓存单例,提供 `_reset_for_test()` 或用可注入的建法;测试用固定小并发验峰值。）

- [ ] **Step 2: 确认失败** → FAIL。

- [ ] **Step 3: 实现**

3a. `app/browser/browser_gate.py`:模块级 `_sem: asyncio.Semaphore | None = None`;`_get_semaphore()` 懒建 `asyncio.Semaphore(settings.BROWSER_CONCURRENCY)`(首次在 async 上下文调 → 绑运行 loop);`browser_slot()` = `async with _get_semaphore():`。加 `_reset_for_test()` 置 None(测试用)。

3b. `app/core/config.py` 加 `BROWSER_CONCURRENCY: int = 6`。

3c. `app/browser/sync_client.py`:`__init__` 加 `block_images: bool = False`,存 `self.block_images`;`start()` 的 `launch_options(...)` 加 `block_webgl=True` + `block_images=self.block_images`。

3d. 三入口套闸 + 传 block_images:
- `scheduler.py` `_publish_runner`:`async with account_locks.get(account_id): async with browser_slot(): await asyncio.to_thread(...)`;publish 的 SyncClient 构造保持 `block_images=False`(默认,不改)。
- `cookie_check.py` `_run_check`:`async with account_locks.get(account_id): async with browser_slot(): await asyncio.to_thread(...)`;其调的 `check_login_once`/SyncClient 传 `block_images=True`(只读)。
- `note_export.py` `_run_export`:同上套闸;`_export_sync` 里 `SyncClient(account_id, cookies, block_images=True)`。
- import:三处 `from app.browser.browser_gate import browser_slot`。

- [ ] **Step 4: 跑测试通过** + 全量不回归(现有 publish/cookie/note-export 测试;monkeypatch to_thread 的测试仍绿——套闸不改变单操作行为,只加并发上限)。

- [ ] **Step 5: 提交**
```bash
git add app/browser/browser_gate.py app/core/config.py app/browser/sync_client.py \
  app/publish/scheduler.py app/services/cookie_check.py app/services/note_export.py tests/test_browser_gate.py
git commit -m "feat(browser): 全局浏览器并发闸(BROWSER_CONCURRENCY=6)+ 三入口套闸 + block_images/webgl 瘦身"
```

---

### Task 2: 孤儿回收 reaper(串行,Task 1 合并后——config.py 重叠)

**Files:**
- Create: `app/browser/browser_reaper.py`、`tests/test_browser_reaper.py`
- Modify: `app/core/config.py`(BROWSER_REAP_*)、`app/server.py`(lifespan 起 reaper)

**Interfaces:**
- Consumes:`profile_guard`(/proc 枚举 + `_argv_targets_profile` + `profile_dir`)、`account_locks`
- Produces:`class BrowserReaper`(类比 `app/browser/cookie_checker.py::CookieChecker` 的 start/stop 周期任务);`reap_once() -> int`(杀掉的孤儿数)

- [ ] **Step 1: 写失败测试 `tests/test_browser_reaper.py`**
```python
# monkeypatch 进程枚举(返假 pid→(是否camoufox, profile 里的 account_id, 存活秒数))+ 假 kill(记录被杀 pid)
# 1. test_reap_kills_stale_ownerless:camoufox 进程,account 锁未持有 + 存活>REAP_AGE → 被杀
# 2. test_reap_skips_locked:account 锁被持有(有在跑操作)→ 不杀(即便超龄)
# 3. test_reap_skips_young:未超 REAP_AGE → 不杀
# 4. test_reap_skips_non_camoufox:非 camoufox 进程 → 不碰
# 5. test_reap_once_exception_safe:枚举/kill 抛异常 → reap_once 不冒泡(记 log 返已杀数)
```

- [ ] **Step 2: 确认失败** → FAIL。

- [ ] **Step 3: 实现**
3a. `app/browser/browser_reaper.py`:`reap_once()` 扫 `/proc`(复用 profile_guard 的枚举风格),对每个 camoufox 进程:从 argv 提取 account_id(匹配 `data/browser/account_{id}` profile 路径)→ 若 `account_locks.get(id).locked()` 为 False **且** 进程存活 > `settings.BROWSER_REAP_AGE` → SIGKILL;全程 try/except 单进程失败不中断,返回杀掉数。`BrowserReaper` 类:`start()` 起 `asyncio.create_task` 周期跑 `reap_once`(间隔 `BROWSER_REAP_INTERVAL`),`stop()` 取消;整体 try/except 不崩 loop。把可测的纯判定(是否该杀:is_camoufox + not locked + age>threshold)抽成小函数便于单测。
3b. `app/core/config.py` 加 `BROWSER_REAP_INTERVAL: int = 300`、`BROWSER_REAP_AGE: int = 900`。
3c. `app/server.py` app_lifespan:`if settings.BROWSER_REAP_INTERVAL > 0: reaper = BrowserReaper(...); reaper.start()`;finally `await reaper.stop()`(类比现有 cookie_checker 的起停)。

- [ ] **Step 4: 跑测试通过** + 全量不回归(lifespan 改动别破坏 scheduler/cookie_checker 启停)。

- [ ] **Step 5: 提交**
```bash
git add app/browser/browser_reaper.py app/core/config.py app/server.py tests/test_browser_reaper.py
git commit -m "feat(browser): 孤儿 camoufox 周期回收 reaper——杀无主超龄进程防内存泄露"
```

---

### Task 3: SQLite WAL + busy_timeout(可与 Task 2 并行,仅 config.py 重叠)

**Files:**
- Modify: `app/core/db.py`、`app/core/config.py`(SQLITE_BUSY_TIMEOUT)
- Test: `tests/test_db_wal.py`(新)

**Interfaces:** Consumes `settings.DATABASE_URL`(sqlite 判断)、`SQLITE_BUSY_TIMEOUT`。

- [ ] **Step 1: 写失败测试 `tests/test_db_wal.py`**
```python
# 1. test_sqlite_engine_wal_and_timeout:用 sqlite URL 建 engine(复用 db.py 的建法)→ 连接后
#    PRAGMA journal_mode == "wal";busy_timeout PRAGMA == SQLITE_BUSY_TIMEOUT*1000(或 connect_args timeout 生效)
# 2. test_non_sqlite_skips_pragma:DATABASE_URL 为 postgres 形态时,建 engine 不套 sqlite pragma
#    (不真连 pg;验建 engine 的分支逻辑:is_sqlite 判断 + connect_args/event 只在 sqlite 挂)
```

- [ ] **Step 2: 确认失败** → FAIL。

- [ ] **Step 3: 实现 `app/core/db.py`**
- 判 `_is_sqlite = settings.DATABASE_URL.startswith("sqlite")`。
- sqlite 时:`create_async_engine(DATABASE_URL, echo=False, future=True, connect_args={"timeout": settings.SQLITE_BUSY_TIMEOUT})`;并挂 `event.listens_for(engine.sync_engine, "connect")` 回调执行 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=<ms>`。非 sqlite:原样 `create_async_engine(DATABASE_URL, echo=False, future=True)`(不传 sqlite-only 的 connect_args/pragma)。
- `app/core/config.py` 加 `SQLITE_BUSY_TIMEOUT: int = 30`(秒)。

- [ ] **Step 4: 跑测试通过** + 全量不回归(现有所有测试用 sqlite,WAL 不应破坏——反而更稳)。

- [ ] **Step 5: 提交**
```bash
git add app/core/db.py app/core/config.py tests/test_db_wal.py
git commit -m "feat(db): SQLite WAL + busy_timeout——并发写从报错变排队(非 sqlite 自动跳过)"
```

---

## 合并与部署(lead)

1. Task 1 先 review + merge(核心)。
2. Task 2、3 从新 main 并行 → review → merge;**config.py 会冲突(各加不同字段),lead 手工合**(三方保留所有新字段);其余文件不相扰(T2=reaper/server,T3=db)。
3. 全并后:全量绿;`systemctl restart nbdpsy-server`(应用并发闸/reaper/WAL;无新迁移)。
4. 冒烟:并发触发多个 cookie-check(不同号)→ 观察 camoufox 进程峰值 ≤ BROWSER_CONCURRENCY;`PRAGMA journal_mode` == wal;reaper 日志正常。
