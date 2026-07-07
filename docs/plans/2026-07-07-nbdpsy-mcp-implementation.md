# nbdpsy-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把小红书「自动发布 / 多账号管理 / cookie 管理 / 远程登录(插件)」四类核心能力做成一个 apikey 鉴权、带 RBAC 的纯 MCP 后台服务，远程 agent 经 Streamable HTTP 调用。

**Architecture:** 单进程 FastAPI + FastMCP(Streamable HTTP)，SQLite + SQLAlchemy(async) + Alembic；apikey 中间件把 Operator 挂进 ContextVar，各工具据此鉴权；发布走 asyncio 队列 + per-account 互斥 + DB 状态机(替代 celery)，sync Camoufox 在独立线程发笔记；登录交给 chrome 插件把 cookie 推回 `/api/cookies/import`。

**Tech Stack:** Python 3.11+、fastmcp、fastapi、uvicorn、sqlalchemy、aiosqlite、alembic、camoufox==0.4.11、playwright、httpx、Pillow、cryptography、pydantic-settings、loguru、pytest、pytest-asyncio。

## Global Constraints

- **抽取来源仓库(只读)**：`/home/roots/小红书运营工具`。所有"从旧仓抽取/移植"任务必须直接读该绝对路径下的源文件，不得凭空写。
- **目标仓库**：`/home/roots/nbdpsy-mcp`（已 push，root commit 只有设计文档）。
- **语言**：注释、commit、文档全部简体中文。
- **前端 UI 无 emoji**（本项目无前端，但工具返回文本也不用 emoji）。
- **Camoufox 版本写死 `camoufox==0.4.11`**（旧仓漏写踩过坑）。
- **SECRET_KEY 的 Fernet 派生必须与旧仓 `backend/app/core/security.py` 字节一致**（保留未来迁移旧 cookie 的可能；换 key 会让存量 cookie 解不出且静默返回空串）。
- **cookie 每个小红书账号库里唯一一行**（共享语义），import 是 upsert。
- **同一小红书账号禁止并发发布**（Firefox 单写锁），用进程内 per-account 锁。
- **sync Camoufox 的 start/publish/stop 必须同一线程**，不能进 asyncio event loop。
- **授权是二元**：`operator_account_access` 有记录=可用，无=不可用；admin 隐式全量。
- **测试命令**：新仓自带 venv，`/home/roots/nbdpsy-mcp/.venv/bin/pytest`；异步测试用 `pytest-asyncio`。
- **commit 结尾附**：
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JhaxEMJ3msQss7QzgTrBxq
  ```
- **每个 commit 显式列文件，禁 `git add -A`/`git add .`**。

---

## File Structure

```
nbdpsy-mcp/
  app/
    __init__.py
    server.py               # FastAPI + FastMCP 挂载 + 中间件 + lifespan
    core/
      config.py             # pydantic-settings Settings
      security.py           # Fernet 加解密 + apikey 生成/hash
      db.py                 # async engine/session/Base/init_db
    models/
      __init__.py
      operator.py           # Operator, OperatorAccountAccess
      xhs_account.py        # XhsAccount
      publish_job.py        # PublishJob
    auth/
      context.py            # ContextVar[Operator] + current_operator()
      middleware.py         # apikey 解析中间件
      guards.py             # require_admin / assert_account_access / visible_account_ids
      bootstrap.py          # ROOT_ADMIN_APIKEY 引导 admin
    services/
      operator_service.py   # Operator/Access CRUD
      cookie_service.py     # sameSite 规范化 + upsert 共享 cookie + import/get
      account_service.py    # XhsAccount CRUD (scoped)
    browser/
      profile_guard.py      # 锁清理/杀孤儿/删 cookies.sqlite/proxy None pop
      fingerprint.py        # 移植 fingerprint_factory + schemas
      login_detector.py     # 移植 DETECT_LOGIN_JS
      text_formatter.py     # 移植 format_for_xiaohongshu 等
      sync_human_actions.py # 移植拟人化操作
      sync_client.py        # 精简 xhs_playwright_client: start/check_login/publish_note/stop
      atomic_tasks.py       # 移植 step1-7
      images.py             # materialize_images: URL/base64 -> 临时文件
    publish/
      queue.py              # AccountLocks + asyncio.Queue + worker
      scheduler.py          # 扫表调度协程 + 启动恢复 + 重试
    tools/
      __init__.py           # register_all(mcp)
      system.py             # health
      admin.py              # operator/access 管理工具
      accounts.py           # 账号工具
      cookies.py            # cookie 工具 (import/check/get)
      publish.py            # 发布工具
      extension.py          # get_extension_download
    http/
      cookies_import.py     # POST /api/cookies/import
      downloads.py          # GET /downloads/extension.zip
  chrome-extension/         # 从旧仓移植 + apikey 化
  scripts/
    xvfb.sh                 # Xvfb :99 启停
    pack_extension.sh       # 打包 extension.zip
    run.sh                  # alembic upgrade + uvicorn
  alembic/                  # 迁移
  tests/
  requirements.txt
  .env.example
  README.md
```

---

# Phase 0 — 地基（阻塞全部后续，串行先行）

### Task 0.1：仓库骨架 + 依赖 + 配置

**Files:**
- Create: `requirements.txt`, `.env.example`, `pytest.ini`, `app/__init__.py`, `app/core/__init__.py`, `app/core/config.py`, `tests/__init__.py`, `tests/conftest.py`
- Create venv: `/home/roots/nbdpsy-mcp/.venv`

**Interfaces:**
- Produces: `app.core.config.settings` (单例 `Settings` 实例)，字段见下。

**Settings 字段（全部带默认值）：** `APP_NAME="nbdpsy-mcp"`, `DEBUG=False`, `LOG_LEVEL="INFO"`, `LOG_FILE="logs/app.log"`, `API_HOST="0.0.0.0"`, `API_PORT=8848`, `PUBLIC_BASE_URL="http://127.0.0.1:8848"`, `DATABASE_URL="sqlite+aiosqlite:///./data/nbdpsy.db"`, `SECRET_KEY="change-me-32bytes-minimum-secret-key"`, `ROOT_ADMIN_APIKEY=""`, `DATA_DIR="./data"`, `UPLOAD_DIR="./data/uploads"`, `XVFB_DISPLAY=":99"`, `PUBLISH_CONCURRENCY=2`, `PUBLISH_RETRY_SCHEDULE="120,600,1800"`, `PUBLISH_JOB_TIMEOUT=600`, `COOKIE_CHECK_INTERVAL=0`, `DEBUG_SCREENSHOTS_ENABLED=False`。

- [ ] **Step 1: 建 venv 与依赖清单**

`requirements.txt`：
```
fastmcp>=2.0
fastapi>=0.110
uvicorn[standard]>=0.27
sqlalchemy>=2.0
aiosqlite>=0.19
alembic>=1.13
camoufox==0.4.11
playwright>=1.48
httpx>=0.27
Pillow>=10.0
cryptography>=42.0
pydantic-settings>=2.1
loguru>=0.7
pytest>=8.0
pytest-asyncio>=0.23
```
Run:
```bash
cd /home/roots/nbdpsy-mcp && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
```
Expected: 安装成功（camoufox 浏览器体二进制先不 fetch，单测不需要）。

- [ ] **Step 2: 写 config.py**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    APP_NAME: str = "nbdpsy-mcp"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/app.log"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8848
    PUBLIC_BASE_URL: str = "http://127.0.0.1:8848"
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/nbdpsy.db"
    SECRET_KEY: str = "change-me-32bytes-minimum-secret-key"
    ROOT_ADMIN_APIKEY: str = ""
    DATA_DIR: str = "./data"
    UPLOAD_DIR: str = "./data/uploads"
    XVFB_DISPLAY: str = ":99"
    PUBLISH_CONCURRENCY: int = 2
    PUBLISH_RETRY_SCHEDULE: str = "120,600,1800"
    PUBLISH_JOB_TIMEOUT: int = 600
    COOKIE_CHECK_INTERVAL: int = 0
    DEBUG_SCREENSHOTS_ENABLED: bool = False

    @property
    def retry_delays(self) -> list[int]:
        return [int(x) for x in self.PUBLISH_RETRY_SCHEDULE.split(",") if x.strip()]

settings = Settings()
```

- [ ] **Step 3: 写测试**

`tests/test_config.py`:
```python
from app.core.config import settings

def test_defaults_present():
    assert settings.APP_NAME == "nbdpsy-mcp"
    assert settings.PUBLISH_CONCURRENCY >= 1
    assert settings.retry_delays == [120, 600, 1800]
```
`pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
markers =
    slow: 需真小红书账号/浏览器的端到端用例
```

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/pytest tests/test_config.py -v` → Expected: PASS

- [ ] **Step 5: `.env.example`（覆盖所有字段，真实值用占位）+ commit**

`.env.example` 每字段一行。Commit：
```bash
git add requirements.txt .env.example pytest.ini app/__init__.py app/core/__init__.py app/core/config.py tests/__init__.py tests/test_config.py
git commit -m "chore(core): 仓库骨架 + pydantic-settings 配置 + venv"
```

---

### Task 0.2：core/security（Fernet + apikey）

**Files:**
- Create: `app/core/security.py`, `tests/test_security.py`
- Read source: `/home/roots/小红书运营工具/backend/app/core/security.py`（复制 Fernet 派生方式）

**Interfaces:**
- Produces: `encrypt_cookies(plaintext:str)->str`, `decrypt_cookies(ciphertext:str)->str`(失败返 "")，`generate_apikey()->str`, `hash_apikey(key:str)->str`, `verify_apikey(key:str, hashed:str)->bool`。

- [ ] **Step 1: 读旧仓 security.py，确认 Fernet key 派生**（取 SECRET_KEY 前 32 字节 → base64.urlsafe_b64encode）。移植同款派生到新文件。

- [ ] **Step 2: 写测试**

```python
from app.core import security

def test_cookie_roundtrip():
    ct = security.encrypt_cookies('[{"name":"a","value":"1"}]')
    assert security.decrypt_cookies(ct) == '[{"name":"a","value":"1"}]'

def test_decrypt_bad_returns_empty():
    assert security.decrypt_cookies("not-a-token") == ""

def test_apikey_hash_verify():
    k = security.generate_apikey()
    h = security.hash_apikey(k)
    assert security.verify_apikey(k, h)
    assert not security.verify_apikey("wrong", h)
```

- [ ] **Step 3: 实现**

```python
import base64, hashlib, secrets
from cryptography.fernet import Fernet
from loguru import logger
from app.core.config import settings

def _fernet() -> Fernet:
    raw = settings.SECRET_KEY.encode("utf-8")[:32].ljust(32, b"0")
    return Fernet(base64.urlsafe_b64encode(raw))

def encrypt_cookies(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")

def decrypt_cookies(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.warning(f"cookie 解密失败(返回空串): {e}")
        return ""

def generate_apikey() -> str:
    return secrets.token_urlsafe(32)

def hash_apikey(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def verify_apikey(key: str, hashed: str) -> bool:
    return secrets.compare_digest(hash_apikey(key), hashed)
```
> 注意：若旧仓派生方式不同(如 SHA256 后 base64)，以旧仓为准替换 `_fernet()`，Step 2 的 roundtrip 仍应过。

- [ ] **Step 4: 跑测试** → `.venv/bin/pytest tests/test_security.py -v` → PASS

- [ ] **Step 5: commit** `feat(core): Fernet cookie 加解密 + apikey 生成/校验`

---

### Task 0.3：core/db

**Files:** Create `app/core/db.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `Base`(DeclarativeBase), `engine`, `async_session`(sessionmaker), `get_session()`(async 上下文), `init_db()`(建表)。

- [ ] **Step 1: 测试**
```python
import pytest
from app.core.db import init_db, get_session
from sqlalchemy import text

@pytest.mark.asyncio
async def test_init_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    # 重新加载 settings/engine 见实现说明
    await init_db()
    async with get_session() as s:
        assert (await s.execute(text("select 1"))).scalar() == 1
```

- [ ] **Step 2: 实现**
```python
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

class Base(DeclarativeBase): ...

engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@asynccontextmanager
async def get_session():
    async with async_session() as s:
        yield s

async def init_db():
    import app.models  # 确保模型注册
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```
> 测试为隔离数据库，用独立 engine fixture（见 conftest）；生产用 alembic（Task 0.4）。conftest 提供 `db` fixture：为每个测试建临时 sqlite + `Base.metadata.create_all`。

- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(core): async SQLAlchemy engine/session/init_db`

---

### Task 0.4：models + Alembic 初始迁移

**Files:** Create `app/models/__init__.py`, `app/models/operator.py`, `app/models/xhs_account.py`, `app/models/publish_job.py`, `alembic.ini`, `alembic/env.py`, `tests/test_models.py`

**Interfaces (Produces — 后续任务全依赖这些字段名/类型):**
```
Operator(id:int pk, name:str, apikey_hash:str unique, role:str['admin'|'operator'],
         enabled:bool=True, created_at:datetime, created_by:int|None)
OperatorAccountAccess(id:int pk, operator_id:int fk, xhs_account_id:int fk,
         granted_by:int|None, created_at:datetime; UNIQUE(operator_id,xhs_account_id))
XhsAccount(id:int pk, name:str, nickname:str|None, user_id:str|None, red_id:str|None,
         avatar:str|None, status:str='unknown', cookie_status:str='unknown',
         last_check_at:datetime|None, login_cookies:str|None(加密), last_login_at:datetime|None,
         created_at:datetime)
PublishJob(id:int pk, account_id:int fk, title:str, content:str, images_json:str,
         topics_json:str, schedule_time:datetime|None, status:str='pending',
         started_at:datetime|None, note_id:str|None, note_url:str|None, error:str|None,
         retries:int=0, next_retry_at:datetime|None, created_by:int|None, created_at:datetime)
```
`status` 取值：`pending|publishing|published|failed|canceled`。

- [ ] **Step 1: 测试**（建表 + 唯一约束 + 默认值）
```python
import pytest
from app.models import Operator, OperatorAccountAccess, XhsAccount, PublishJob

@pytest.mark.asyncio
async def test_create_models(db):
    op = Operator(name="a", apikey_hash="h", role="admin")
    db.add(op); await db.commit()
    assert op.id and op.enabled is True
    acc = XhsAccount(name="号1"); db.add(acc); await db.commit()
    assert acc.status == "unknown"
    job = PublishJob(account_id=acc.id, title="t", content="c", images_json="[]", topics_json="[]")
    db.add(job); await db.commit()
    assert job.status == "pending" and job.retries == 0
```

- [ ] **Step 2: 实现 4 个模型**（SQLAlchemy 2.0 `Mapped`/`mapped_column`；`created_at` 用 `server_default=func.now()` 或 Python 默认；`OperatorAccountAccess` 加 `UniqueConstraint`）。`app/models/__init__.py` 导出全部并被 `Base` 感知。

- [ ] **Step 3: 跑测试** → PASS

- [ ] **Step 4: Alembic 初始化 + autogenerate**
```bash
.venv/bin/alembic init alembic   # 然后改 env.py 指向 app.core.db.Base 与 settings.DATABASE_URL(同步 url)
.venv/bin/alembic revision --autogenerate -m "初始表: operators/access/xhs_accounts/publish_jobs"
```
> env.py 里 sqlite 迁移用同步驱动 url（把 `+aiosqlite` 去掉）。

- [ ] **Step 5: commit** `feat(models): 4 张核心表 + alembic 初始迁移`

---

### Task 0.5：server 骨架 + FastMCP 挂载 + health

**Files:** Create `app/server.py`, `app/tools/__init__.py`, `app/tools/system.py`, `tests/test_server_health.py`
- Read（版本核对）：用 context7 查 `fastmcp` 当前版本的「Streamable HTTP + 与 FastAPI/Starlette 共挂 + 从 HTTP 请求取 header」的正确 API。

**Interfaces:**
- Produces: `app.server.create_app()->FastAPI`（挂 FastMCP 于 `/mcp`，注册工具）；`register_all(mcp)`。

- [ ] **Step 1: 用 context7 核对 fastmcp API**，确认：如何创建 `FastMCP` 实例、如何拿到 Streamable HTTP ASGI app、如何挂到 FastAPI(`app.mount("/mcp", mcp_app)` 或等价)、如何在工具内读取 HTTP headers（用于 apikey）。把结论写进 `app/server.py` 顶部注释。

- [ ] **Step 2: 测试(health 可无鉴权)**
```python
import pytest
from httpx import AsyncClient, ASGITransport
from app.server import create_app

@pytest.mark.asyncio
async def test_app_boots():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True
```

- [ ] **Step 3: 实现 server.py**：`create_app()` 建 FastAPI + lifespan(暂只 `init_db()`)；FastMCP 实例 + `register_all(mcp)`；挂 `/mcp`；另加一个明文 `/healthz` REST 便于探活。`app/tools/system.py` 定义 MCP `health` 工具(返回 `{ok, version}`)。`register_all` 汇总注册（此刻只 system）。

- [ ] **Step 4: 跑测试** → PASS

- [ ] **Step 5: commit** `feat(server): FastAPI+FastMCP 骨架 + health`

---

# Phase 1 — RBAC（依赖 Phase 0，可与 Phase 3 并行）

### Task 1.1：apikey 中间件 + Operator 上下文 + 引导 admin

**Files:** Create `app/auth/context.py`, `app/auth/middleware.py`, `app/auth/bootstrap.py`, `tests/test_auth_middleware.py`; Modify `app/server.py`(装中间件 + lifespan 调 bootstrap)

**Interfaces:**
- Produces: `current_operator()->Operator`(读 ContextVar，未认证抛 `AuthError`)；`ApiKeyMiddleware`；`bootstrap_admin()`(启动确保 ROOT_ADMIN_APIKEY 对应 admin)。

- [ ] **Step 1: 测试**
```python
# 未带 key -> /mcp 401；带非法 key -> 401；带 ROOT_ADMIN_APIKEY -> current_operator 为 admin
```
（用 ASGITransport 打 `/mcp` 初始化请求或一个受保护的 REST 探针 `/api/whoami` 便于测中间件；`/healthz` 与 `/downloads` 白名单放行。）

- [ ] **Step 2: 实现 context.py**（`ContextVar[Operator|None]` + `set_current_operator` + `current_operator()`）。

- [ ] **Step 3: 实现 middleware.py**：Starlette `BaseHTTPMiddleware`，白名单路径(`/healthz`,`/downloads`)直接放行；否则取 `Authorization: Bearer`，`hash_apikey` 查 `Operator.apikey_hash` 且 `enabled` → set ContextVar；查不到返回 401 JSON。

- [ ] **Step 4: 实现 bootstrap.py**：若 `ROOT_ADMIN_APIKEY` 非空 → upsert 一个 `role=admin` Operator(name="root")，`apikey_hash=hash_apikey(...)`；若为空则 `generate_apikey()` 建 admin 并 `logger.warning` 打印明文一次。

- [ ] **Step 5: 装配 server.py**：`app.add_middleware(ApiKeyMiddleware)`；lifespan 里 `await init_db(); await bootstrap_admin()`。

- [ ] **Step 6: 跑测试** → PASS

- [ ] **Step 7: commit** `feat(auth): apikey 中间件 + Operator 上下文 + 引导 admin`

---

### Task 1.2：guards（鉴权助手）

**Files:** Create `app/auth/guards.py`, `tests/test_guards.py`

**Interfaces:**
- Produces: `require_admin(op:Operator)`(非 admin 抛 `PermissionError`)；`async assert_account_access(op, account_id, session)`(admin 放行；否则查 access 行，无则抛)；`async visible_account_ids(op, session)->list[int]|None`(admin 返 None 表示全部)。

- [ ] **Step 1: 测试**（admin 通吃；operator 仅其 access 行；无 access 抛错）
- [ ] **Step 2: 实现 guards.py**
- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(auth): admin/account-access 鉴权助手`

---

### Task 1.3：operator_service + 管理员工具

**Files:** Create `app/services/operator_service.py`, `app/tools/admin.py`, `tests/test_operator_service.py`, `tests/test_admin_tools.py`; Modify `app/tools/__init__.py`(注册 admin)

**Interfaces (Produces):**
```
operator_service.create_operator(session, name, role='operator') -> (Operator, apikey_plain)
  .list_operators(session) -> list[Operator]
  .update_operator(session, id, *, role=None, enabled=None, name=None) -> Operator
  .delete_operator(session, id) -> None
  .rotate_apikey(session, id) -> apikey_plain
  .grant_access(session, operator_id, xhs_account_id, granted_by) -> OperatorAccountAccess
  .revoke_access(session, operator_id, xhs_account_id) -> None
  .list_grants(session, operator_id) -> list[int]
```
MCP 工具(admin only，内部先 `require_admin(current_operator())`)：`create_operator/list_operators/update_operator/delete_operator/rotate_operator_apikey/grant_account_access/revoke_account_access/list_operator_grants`。

- [ ] **Step 1: service 测试**（create 返回明文 key 且库存 hash；rotate 变更 hash；grant 幂等(唯一约束)；delete 级联清 access）
- [ ] **Step 2: 实现 operator_service.py**
- [ ] **Step 3: service 测试过** → PASS
- [ ] **Step 4: admin 工具测试**（非 admin 调用抛 PermissionError；admin 正常）
- [ ] **Step 5: 实现 admin.py 工具** + 注册
- [ ] **Step 6: 工具测试过** → PASS
- [ ] **Step 7: commit** `feat(rbac): operator/access 服务 + 管理员 MCP 工具`

---

# Phase 2 — 账号 + cookie（依赖 Phase 0/1）

### Task 2.1：cookie_service（sameSite 规范化 + 共享 upsert）

**Files:** Create `app/services/cookie_service.py`, `tests/test_cookie_service.py`
- Read source: `/home/roots/小红书运营工具/backend/app/api/endpoints/cookies_import.py`（sameSite 规范化 + user_info 字段）、`backend/app/utils/xhs_utils.py`

**Interfaces (Produces):**
```
cookie_service.normalize_cookies(raw:list[dict]) -> list[dict]   # sameSite 'unspecified'/小写 -> 'Lax'/首字母大写; 缺 sameSite 补 'Lax'
  .import_cookies(session, operator, account_name, cookies:list[dict], user_info:dict|None)
       -> (XhsAccount, created:bool)   # upsert 唯一行; 加密存 login_cookies; 新号给 operator 建 access
  .get_cookies(session, operator, account_id) -> list[dict]      # 解密; 先 assert_account_access
```

- [ ] **Step 1: 测试**
```python
def test_normalize_samesite():
    out = cookie_service.normalize_cookies([{"name":"a","value":"1","sameSite":"unspecified"}])
    assert out[0]["sameSite"] == "Lax"

@pytest.mark.asyncio
async def test_import_creates_account_and_access(db):
    op = ...operator...
    acc, created = await cookie_service.import_cookies(db, op, "号1",
        [{"name":"a1","value":"x","domain":".xiaohongshu.com","sameSite":"lax"}], {"nickname":"N","user_id":"u1"})
    assert created and acc.nickname == "N"
    # 二次 import 同名/同 user_id -> 更新同一行不新建
    acc2, created2 = await cookie_service.import_cookies(db, op, "号1", [...], {"user_id":"u1"})
    assert not created2 and acc2.id == acc.id
```
> upsert 主键：优先按 `user_info.user_id` 匹配既有号，否则按 `account_name`。

- [ ] **Step 2: 实现 cookie_service.py**（normalize 参考旧仓；import upsert + 加密 + 新号建 access + 回写 user_info/last_login_at；get 先 `assert_account_access` 再 `decrypt_cookies`）
- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(cookie): sameSite 规范化 + 共享 cookie upsert + import/get`

---

### Task 2.2：账号工具 + cookie 工具(import/get) + HTTP import 端点

**Files:** Create `app/services/account_service.py`, `app/tools/accounts.py`, `app/tools/cookies.py`, `app/http/cookies_import.py`, `tests/test_account_tools.py`, `tests/test_cookies_import_http.py`; Modify `app/tools/__init__.py`, `app/server.py`(挂 HTTP 路由)

**Interfaces (Produces):**
```
account_service.list_accounts(session, operator) -> list[XhsAccount]   # visible_account_ids 过滤
  .get_account / .update_account / .delete_account (均 assert_account_access)
MCP: list_accounts/get_account/update_account/delete_account, import_cookies(account_name,cookies_json,user_info?), get_cookies(account_id)
HTTP: POST /api/cookies/import  (body 同 import_cookies; apikey 中间件已鉴权; 调 cookie_service.import_cookies)
```
> `check_cookies` 工具留到 Task 3.7（依赖 sync_client）。

- [ ] **Step 1: 账号工具测试**（operator 只见被 grant 的号；admin 全见；update/delete 越权抛错）
- [ ] **Step 2: 实现 account_service + accounts.py 工具**
- [ ] **Step 3: 账号工具测试过** → PASS
- [ ] **Step 4: cookies 工具测试**（import_cookies 工具解析 cookies_json 字符串 -> list；get_cookies 受 access 限制）
- [ ] **Step 5: 实现 cookies.py(import/get) + http/cookies_import.py + server 挂载**
- [ ] **Step 6: HTTP import 测试**（带 apikey POST /api/cookies/import 建号成功；无 apikey 401）
- [ ] **Step 7: commit** `feat(account): 账号工具 + cookie import/get 工具 + 插件推送端点`

---

# Phase 3 — 浏览器 + 发布（依赖 Phase 0；重移植，可与 Phase 1 并行）

> 移植任务的"代码"= 读旧仓源文件 + 按 keep/strip/adapt 清单裁剪。每个任务给出可验证测试。移植时**逐行保留 §Global 与坑清单相关逻辑**。

### Task 3.1：profile_guard + fingerprint

**Files:** Create `app/browser/profile_guard.py`, `app/browser/fingerprint.py`, `tests/test_profile_guard.py`, `tests/test_fingerprint.py`
- Read source: `/home/roots/小红书运营工具/backend/app/utils/camoufox_helper.py`（锁清理/杀孤儿/删 cookies.sqlite/proxy None pop），`backend/app/services/smart_browser/fingerprint_factory.py` + `smart_browser/schemas.py` + `smart_browser/data/*.json`

**Interfaces (Produces):**
```
profile_guard.profile_dir(account_id:int) -> Path            # 统一为 DATA_DIR/browser/account_{id}
  .clean_locks(profile_dir)                                   # 删 lock/.parentlock
  .delete_cookies_db(profile_dir)                             # 删 cookies.sqlite
  .kill_orphans(profile_dir)                                  # argv 精确匹配杀 camoufox-bin（防 account_2/account_20 误杀）
  .sanitize_launch_options(opts:dict) -> dict                 # proxy=None 则 pop
fingerprint.get_fingerprint(account_id:int) -> BrowserFingerprint  # 持久化 profile_dir/fingerprint.json
```

- [ ] **Step 1: profile_guard 测试**
```python
def test_kill_orphans_prefix_safe(monkeypatch):
    # 构造伪 /proc argv: account_2 与 account_20，确认只匹配精确 account_2 目录
def test_sanitize_pops_none_proxy():
    assert "proxy" not in profile_guard.sanitize_launch_options({"proxy": None, "a": 1})
def test_clean_locks(tmp_path):
    (tmp_path/"lock").write_text("x"); (tmp_path/".parentlock").write_text("x")
    profile_guard.clean_locks(tmp_path)
    assert not (tmp_path/"lock").exists()
```
- [ ] **Step 2: 移植实现 profile_guard.py**（从 camoufox_helper 抽相关函数，路径改统一目录）
- [ ] **Step 3: profile_guard 测试过** → PASS
- [ ] **Step 4: fingerprint 测试**（同 account_id 两次调用返回一致指纹；写 fingerprint.json）
- [ ] **Step 5: 移植 fingerprint.py + schemas（BrowserFingerprint）+ data/*.json**
- [ ] **Step 6: fingerprint 测试过** → PASS
- [ ] **Step 7: commit** `feat(browser): profile_guard 锁清理/杀孤儿 + 稳定指纹`

---

### Task 3.2：login_detector + text_formatter + sync_human_actions

**Files:** Create `app/browser/login_detector.py`, `app/browser/text_formatter.py`, `app/browser/sync_human_actions.py`, `tests/test_text_formatter.py`
- Read source: `backend/app/utils/login_detector.py`, `backend/app/utils/text_formatter.py`, `backend/app/services/smart_browser/sync_human_actions.py`

**Interfaces (Produces):**
```
login_detector.DETECT_LOGIN_JS: str
text_formatter.format_for_xiaohongshu(text:str)->str; .get_display_length(text:str)->int; .truncate_by_display(text:str, max_width:int)->str
sync_human_actions.SyncHumanActions(page)  # type/click/move 拟人化 API（沿用旧签名）
```

- [ ] **Step 1: text_formatter 测试**（emoji 宽=2；截断到显示宽 20；Markdown 清理）
```python
def test_display_width_emoji():
    assert text_formatter.get_display_length("a😀") == 3
def test_truncate_title():
    assert text_formatter.get_display_length(text_formatter.truncate_by_display("很长的标题"*10, 20)) <= 20
```
- [ ] **Step 2: 移植三文件**（sync_human_actions 只依赖 playwright sync + logger；login_detector 是常量 JS；text_formatter 纯函数）
- [ ] **Step 3: text_formatter 测试过** → PASS（sync_human_actions/login_detector 无账号不单测，靠 e2e 覆盖）
- [ ] **Step 4: commit** `feat(browser): 登录检测JS + 文本格式化 + 拟人化操作`

---

### Task 3.3：sync_client + atomic_tasks（发布落地层）

**Files:** Create `app/browser/sync_client.py`, `app/browser/atomic_tasks.py`, `app/browser/images.py`, `tests/test_images.py`
- Read source: `backend/app/services/xhs_playwright_client.py`（精简）、`backend/app/services/xhs_publish_atomic_tasks.py`（step1-7）、`backend/app/services/xhs_playwright_manager.py`（线程封装 pattern，简化内联）、`backend/app/services/publish_service.py`（`localize_external_image` 参考）

**keep/strip:**
- `sync_client`：留 `__init__/start/check_login/publish_note/stop`；**cookie 由参数注入**（不再 SessionLocal 读 DB）；SmartLocator 兜底降级为直接失败（删 lazy import 分支）；start 用 profile_guard 清锁/删 cookies.sqlite/统一 profile 目录/注入指纹/注入 cookie/开 explore/`login_detector` 判定。互动方法(comment/like/collect/文字封面)全删。
- `atomic_tasks`：留 step1–7 + 私有 helpers；删 orchestrator 专用尾部 async 函数(add_mention/add_topic_tag/check_risk_control)与 @提及 step5b(除非用户要)。
- **必带坑**（§6.4）：成功页 3 秒立即收口、closed Shadow DOM 像素定位、创作中心先切图文 tab、话题 ≤10 精确匹配回删、note_id 可空、正文 900 截断。

**Interfaces (Produces):**
```
images.materialize_images(images:list[str|dict], workdir:Path) -> list[Path]  # http->下载; base64(data uri 或 {b64,ext})->解码
sync_client.publish_once(account_id:int, cookies:list[dict], title:str, content:str,
        image_paths:list[str], topics:list[str]) -> PublishResult
sync_client.check_login_once(account_id:int, cookies:list[dict]) -> {status:'valid'|'invalid'|'captcha', user_info:dict|None}
PublishResult = {success:bool, note_id:str, note_url:str, error:str|None, need_manual_login:bool}
```
> `publish_once/check_login_once` 内部建 client→start→操作→stop，全部同一线程，供上层 `to_thread` 调用。

- [ ] **Step 1: images 测试**
```python
@pytest.mark.asyncio
async def test_materialize_base64(tmp_path):
    p = images.materialize_images([{"b64": "<1x1png base64>", "ext": "png"}], tmp_path)
    assert p[0].exists()
# http 用 monkeypatch httpx 返回假图字节
```
- [ ] **Step 2: 实现 images.py** → 测试过
- [ ] **Step 3: 移植 sync_client.py**（cookie 注入参数化、profile_guard 接入、SmartLocator 降级）
- [ ] **Step 4: 移植 atomic_tasks.py**（step1-7 + 坑）
- [ ] **Step 5: 导入自检**：`.venv/bin/python -c "import app.browser.sync_client, app.browser.atomic_tasks"` 无 ImportError（无账号不跑真发布）
- [ ] **Step 6: commit** `feat(publish): sync Camoufox 发布客户端 + step1-7 落地 + 图片物料化`

---

### Task 3.4：发布队列 + 调度器（去 celery 状态机）

**Files:** Create `app/publish/queue.py`, `app/publish/scheduler.py`, `tests/test_publish_scheduler.py`

**Interfaces (Produces):**
```
queue.AccountLocks  # get(account_id)->asyncio.Lock
queue.PublishQueue(concurrency:int)
  .submit(job_id:int)                 # 放入内存队列(立即发路径)
  .start(runner:Callable)/.stop()     # 起/停 worker 协程
scheduler.PublishScheduler(session_factory, publish_runner)
  .scan_once() -> list[int]           # 选 pending 且(schedule_time 空或到期) 且 next_retry_at 空或到期
  .recover_stale()                    # publishing 且 started_at 超 PUBLISH_JOB_TIMEOUT -> 复位
  .mark_publishing(job_id)->bool      # UPDATE...WHERE status='pending' 原子占用; 返回是否占到
  .finish(job_id, result)             # 成功->published(note_id/url); 失败->重试排期或 failed
  .start()/.stop()                    # lifespan 循环: recover -> 周期 scan -> submit
publish_runner(job_id)  # 载 job+account+cookie -> per-account 锁 -> to_thread(sync_client.publish_once) -> finish
```
状态机：`pending→publishing(started_at)→published|failed`；重试用 `retries`+`next_retry_at`(retry_delays)；耗尽→failed。

- [ ] **Step 1: 测试(纯 DB 状态机，runner 用假函数不碰浏览器)**
```python
@pytest.mark.asyncio
async def test_state_machine_success(db_factory):
    # 建 pending job -> mark_publishing 占用成功 -> finish(success) -> status=published, note_id 写入
@pytest.mark.asyncio
async def test_retry_then_fail(db_factory):
    # finish(fail) 三次 -> retries 递增 next_retry_at 排期 -> 第4次 failed
@pytest.mark.asyncio
async def test_recover_stale(db_factory):
    # publishing 且 started_at 很旧 -> recover_stale -> 回 pending
@pytest.mark.asyncio
async def test_double_submit_dedup(db_factory):
    # 两次 mark_publishing 只一次返回 True
```
- [ ] **Step 2: 实现 queue.py + scheduler.py**（`publish_runner` 注入，测试用假 runner）
- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(publish): asyncio 队列 + DB 状态机 + 启动恢复 + 重试(替代 celery)`

---

### Task 3.5：发布工具 + check_cookies 工具

**Files:** Create `app/tools/publish.py`, `tests/test_publish_tools.py`; Modify `app/tools/cookies.py`(加 check_cookies), `app/tools/__init__.py`, `app/server.py`(lifespan 起 scheduler/queue)

**Interfaces (Produces):**
```
MCP publish_note(account_id, title, content, images:list, topics:list, schedule_time?) -> {job_id, status:'queued'}
  get_publish_status(job_id) -> {status, note_id?, note_url?, error?, retries}
  list_publish_jobs(account_id?, status?) -> [...]
  cancel_publish_job(job_id) -> {ok}   # 仅 queued/pending
  check_cookies(account_id) -> {status, user_info?}   # to_thread(sync_client.check_login_once) + 写回状态
```
鉴权：publish/list/status/cancel 均 `assert_account_access`；job 归属校验（operator 只见自己有 access 的账号的 job）。

- [ ] **Step 1: 测试(runner/browser mock)**：`publish_note` 建 pending job 返 job_id；越权 account 抛错；`get_publish_status` 读 DB；`cancel` 只对 pending 生效；`list_publish_jobs` 按 access 过滤。`check_cookies` monkeypatch `sync_client.check_login_once` 返回 valid，断言写回 `cookie_status`。
- [ ] **Step 2: 实现 publish.py + check_cookies；server lifespan 起 `PublishQueue`/`PublishScheduler`**（注入真 `publish_runner`；测试环境可关调度）
- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(publish): 发布/状态/取消工具 + check_cookies`

---

# Phase 4 — 插件（依赖 Phase 1 apikey + Phase 2 import 端点）

### Task 4.1：chrome-extension 移植 + apikey 化

**Files:** Create `chrome-extension/*`（从旧仓拷贝后改）, `tests/test_extension_manifest.py`
- Read source: `/home/roots/小红书运营工具/chrome-extension/`（manifest.json, background/service-worker.js, popup/*, content/*, lib/api.js）

**adapt:**
- `serverUrl` 默认/配置指向 `PUBLIC_BASE_URL`。
- 鉴权 header 从 JWT `Bearer <token>` 改为 Operator apikey；popup 增加 apikey 输入并存 `chrome.storage`。
- 推送端点统一 `POST /api/cookies/import`（去掉 save-cookies/create-with-cookies 二分，全走 upsert）。
- 保留：隐身窗口开小红书、跨 cookieStore 采集、`webRequest.onHeadersReceived` 补抓 httpOnly、user_info 采集。
- manifest host_permissions 保留 `*.xiaohongshu.com` + 配置的 MCP 主机。

- [ ] **Step 1: 测试**（`test_extension_manifest.py`：解析 manifest.json 断言 MV3、含 cookies/webRequest 权限、含 xiaohongshu host）
- [ ] **Step 2: 移植 + 改造扩展文件**（apikey 化、端点统一）
- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(extension): 移植 chrome 插件 + apikey 化 + 统一 import 端点`

---

### Task 4.2：下载端点 + 打包脚本 + get_extension_download 工具

**Files:** Create `app/http/downloads.py`, `app/tools/extension.py`, `scripts/pack_extension.sh`, `tests/test_extension_download.py`; Modify `app/server.py`, `app/tools/__init__.py`

**Interfaces (Produces):**
```
HTTP GET /downloads/extension.zip -> 返回打包 zip（白名单放行，无需 apikey）
MCP get_extension_download() -> {download_url, version, apikey, install_steps}
```
`download_url = f"{PUBLIC_BASE_URL}/downloads/extension.zip"`；`apikey` = 当前 caller 自己的明文 key？**注意**：库里只有 hash，明文无法回取。因此 `apikey` 字段返回**引导语**（"用你连接本服务的同一把 apikey 填入插件"）而非明文；admin 若要发 key 走 `create_operator/rotate` 时的一次性明文。install_steps 为中文步骤数组。

- [ ] **Step 1: 测试**：`pack_extension.sh` 产出 `data/extension.zip`；GET /downloads/extension.zip 返回 200 + `application/zip`；`get_extension_download` 返回正确 URL 与步骤。
- [ ] **Step 2: 实现 downloads.py(FileResponse) + pack_extension.sh + extension.py 工具**（`__init__` 注册；server 挂 `/downloads`，中间件白名单已含 `/downloads`）
- [ ] **Step 3: 跑测试** → PASS
- [ ] **Step 4: commit** `feat(extension): 下载端点 + 打包脚本 + get_extension_download 工具`

---

# Phase 5 — 集成与交付（依赖全部）

### Task 5.1：整合 lifespan + 部署脚本 + README + e2e

**Files:** Modify `app/server.py`(lifespan 全量：init_db→bootstrap_admin→起 queue/scheduler→可选 cookie_check 循环)；Create `scripts/xvfb.sh`, `scripts/run.sh`, `README.md`, `tests/e2e/test_smoke.py`(marked `slow`), `.env.example`(补全)

- [ ] **Step 1: 补全 lifespan**：按顺序 `init_db → bootstrap_admin → PublishQueue.start → PublishScheduler.start`；`COOKIE_CHECK_INTERVAL>0` 时起周期检测协程；shutdown 优雅停。
- [ ] **Step 2: 部署脚本**：`xvfb.sh`(启停 `Xvfb :99 -screen 0 1920x1080x24`)、`run.sh`(`alembic upgrade head` + `uvicorn app.server:create_app --factory`)。
- [ ] **Step 3: 全量单测**：`.venv/bin/pytest -m "not slow" -v` 全绿。
- [ ] **Step 4: e2e 冒烟(手动，需真号，标 `@pytest.mark.slow`)**：`import_cookies → check_cookies → publish_note → get_publish_status` 与 `create_operator → grant_account_access → 该 operator 视野受限` 各一条；含清理。**默认不在 CI 跑**。
- [ ] **Step 5: README**（架构、启动、apikey/插件配置、工具清单、部署、坑）。
- [ ] **Step 6: commit** `feat(integration): lifespan 整合 + 部署脚本 + e2e 冒烟 + README`

---

## 工作流依赖图（供 Workflow 并行编排）

```
Phase 0 (串行: 0.1→0.2→0.3→0.4→0.5)  ── 阻塞全部
      │
      ├── Phase 1 (1.1→1.2→1.3)                 ┐
      ├── Phase 3 (3.1→3.2→3.3→3.4)             ┤ 1 与 3 可并行
      │                                          │
      ▼                                          │
Phase 2 (2.1→2.2)  依赖 0+1                       │
      │                                          │
      ▼                                          ▼
Phase 3.5 (发布工具+check_cookies) 依赖 3.4 + 2.x + sync_client
Phase 4 (4.1→4.2) 依赖 1 + 2.2
      │
      ▼
Phase 5 (5.1) 依赖全部
```

- 每组独立 git worktree + 分支 `feature/<phase>`；PR 回 `nbdpsy-mcp` main。
- 合并前跑该组单测 + `pytest -m "not slow"`；e2e 手动。

---

## Self-Review（对照 spec）

- **spec §3 RBAC** → Task 1.1/1.2/1.3、guards、bootstrap 覆盖；二元 grant 与新号自导入在 2.1/1.3。✓
- **spec §4 工具面** → 账号(2.2)、cookie import/get(2.2)+check(3.5)、发布(3.5)、get_extension_download(4.2)、管理员(1.3)、health(0.5)。共 21 工具全覆盖。✓
- **spec §5 登录/共享 cookie** → cookie_service upsert 唯一行(2.1)、HTTP import(2.2)、插件(4.1)。✓
- **spec §6 发布** → 3.3 落地层+坑、3.4 队列/状态机、3.5 工具。✓
- **spec §7 cookie 检测** → check_cookies(3.5)、可选周期(5.1)。✓
- **spec §8 浏览器地基** → profile_guard/fingerprint(3.1)、Xvfb 脚本(5.1)、版本写死(0.1)。✓
- **spec §9 数据模型 4 表** → Task 0.4。✓
- **spec §10 配置** → 0.1 全字段 + .env.example(0.1/5.1)。✓
- **spec §11 目录** → File Structure 一致。✓
- **placeholder 扫描**：无 TBD/TODO；移植任务以 keep/strip/adapt + 测试锚定，非占位。✓
- **类型一致性**：`PublishResult`/`status` 取值/`import_cookies` 返回 `(XhsAccount, bool)`/`visible_account_ids` 返 `None`=全部，跨任务一致。✓
- **已知待核对**（非阻塞，实现时用 context7 查）：Task 0.5 fastmcp Streamable HTTP 挂载与取 header 的确切 API；Task 0.2 旧仓 Fernet 派生细节。
