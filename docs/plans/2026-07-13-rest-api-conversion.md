# 纯 REST API 转型实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 nbdpsy-mcp 的 24 个 MCP 工具全部改为 REST API,新增 `GET /api/manifest` 自描述接口,彻底删除 FastMCP/marketplace。

**Architecture:** 业务全在 services 层,本次只换入口皮:`app/tools/*` 六组工具平移为 `app/http/*` FastAPI router;路由与 manifest 元数据经 `app/http/__init__.py` 注册表聚合;防漂移测试钉死 manifest 与实际路由全等。Task 1 打地基先合并,Task 2-5 并行(各自独立文件,注册表各加一行),Task 6 串行收口删 MCP。

**Tech Stack:** FastAPI、SQLAlchemy 2.0 async + aiosqlite、httpx ASGITransport(测试)、pytest(asyncio_mode=auto)。

**Spec:** `docs/design/2026-07-13-rest-api-conversion-design.md`(端点映射/错误契约/manifest 结构以 spec 为准)

## Global Constraints

- 解释器一律 `/home/roots/nbdpsy-mcp/.venv/bin/python`(worktree 里也用主仓这个,worktree 无 venv)。跑测试:`/home/roots/nbdpsy-mcp/.venv/bin/python -m pytest tests/ -x -q`(cwd = 各自 worktree 根)。
- 注释/docstring/commit 全中文;禁 emoji;commit 格式 `type(scope): 中文描述`;**禁 `git add -A`/`git add .`,显式列文件**。
- **语义平移不重设计**:返回体键名、异步契约、RBAC、截断规则与原 MCP 工具逐一对齐(原工具代码在 `app/tools/`,平移时照抄逻辑;Task 6 之前不删)。
- 所有"××不存在"错误一律抛 `app.core.errors.NotFoundError`(→404);其余入参非法用裸 `ValueError`(→400)。
- 端点函数取身份用 `operator = current_operator()`(ContextVar,中间件已注入),DB 用 `async with get_session() as session`——与 `app/http/accounts_rest.py` 现有模式一致,不用 FastAPI Depends。
- 每个新 router 模块必须导出 `router`(APIRouter,无 prefix,全路径写装饰器)与 `MANIFEST_ENTRIES`(list[dict],键:`method/path/summary/admin_only/params/returns/errors/notes`,path 用 FastAPI 花括号字面量如 `/api/accounts/{account_id}`),并接进 `app/http/__init__.py` 注册表——漏接会被防漂移测试逮住。
- MANIFEST_ENTRIES 的 `notes` 是给 agent 的上手要点:把原 MCP 工具 docstring 里的契约(轮询节奏/易踩坑/静默截断)浓缩进去,不许丢信息。

---

### Task 1: 地基——错误契约 + 路由注册表 + manifest + 测试公共件(先行合并)

**Files:**
- Create: `app/core/errors.py`、`app/http/system.py`、`app/http/manifest.py`、`tests/rest_helpers.py`、`tests/test_manifest.py`
- Modify: `app/http/__init__.py`(当前为空/仅包声明)、`app/server.py`、`app/http/accounts_rest.py`(只加 MANIFEST_ENTRIES)、`app/http/cookies_import.py`(同)、`tests/test_accounts_rest.py`(改用公共件)
- Test: `tests/test_manifest.py`

**Interfaces(Produces,后续 Task 全依赖):**
- `app.core.errors.NotFoundError(ValueError)` — "不存在"专用异常,server 映 404
- `tests.rest_helpers`:`ADMIN_KEY: str`、`bearer(key) -> dict`(Authorization 头)、`rest_client(tmp_path, monkeypatch, root_key=ADMIN_KEY)`(asynccontextmanager,yield AsyncClient;隔离库+真 lifespan)、`seed_account(name, user_id, cookies=...) -> int`、`make_operator(apikey_plain) -> int`、`get_root_admin() -> Operator`
- `app/http/__init__.py`:`ALL_ROUTERS: list[APIRouter]` 与 `ALL_MANIFEST_ENTRIES: list[dict]`;后续 Task 在此各加一行 import + 一行 router + 一行 entries 展开
- MANIFEST_ENTRIES 模块级约定(见 Global Constraints)

- [ ] **Step 1: 写失败测试 `tests/test_manifest.py`**

先建 `tests/rest_helpers.py`:把 `tests/test_accounts_rest.py` 里现成的本地 helper **原样提炼**(import 区照抄该文件顶部):`isolated_client → rest_client`、`_seed_account → seed_account`、`_make_operator → make_operator`、`_admin_operator → get_root_admin`,外加:

```python
ADMIN_KEY = "test-root-admin-key"

def bearer(key: str) -> dict:
    """构造 Authorization 头。"""
    return {"Authorization": f"Bearer {key}"}
```

`tests/test_accounts_rest.py` 删本地副本改 import 公共件(行为不变)。然后写 `tests/test_manifest.py`(4 个测试,完整代码):

```python
"""GET /api/manifest 自描述接口 + 防漂移测试。"""

from fastapi.routing import APIRoute

import app.core.db as db_module
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


async def test_manifest_requires_apikey(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/manifest")
        assert r.status_code == 401


async def test_manifest_admin_sections_and_caller(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/manifest", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["service"] == "nbdpsy-api"
        assert data["caller"]["role"] == "admin"
        for key in ("version", "base_url", "auth", "workflows",
                    "constraints", "error_contract", "endpoints"):
            assert data[key], f"manifest 缺 {key}"
        for e in data["endpoints"]:
            assert e["method"] and e["path"].startswith("/api/") and e["summary"]


async def test_manifest_operator_account_count_narrowed(tmp_path, monkeypatch):
    # 库里 2 个号,operator 只授权 1 个 → caller.account_count == 1
    async with rest_client(tmp_path, monkeypatch) as client:
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        await seed_account("号二", "u-2", _COOKIES)
        op_key = "op-key-manifest"
        op_id = await make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc1, None)
            await s.commit()
        r = await client.get("/api/manifest", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        caller = r.json()["caller"]
        assert caller["role"] == "operator"
        assert caller["account_count"] == 1


def test_manifest_covers_all_api_routes():
    """防漂移:manifest 声明的端点集合与实际注册的 /api/* 路由双向全等。"""
    from app.http import ALL_MANIFEST_ENTRIES
    from app.server import create_app

    app = create_app()
    actual = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    declared = {(e["method"], e["path"]) for e in ALL_MANIFEST_ENTRIES}
    assert actual == declared, (
        f"manifest 漏写: {sorted(actual - declared)}; 多写: {sorted(declared - actual)}"
    )
```

注:`seed_account`/`make_operator`/`grant_access` 的确切签名以提炼源(test_accounts_rest.py)为准,若与上面示例有出入,以源码为准调整测试。

- [ ] **Step 2: 跑测试确认失败**

`/home/roots/nbdpsy-mcp/.venv/bin/python -m pytest tests/test_manifest.py -x -q` → 预期 FAIL(`/api/manifest` 404 / import 错误)。

- [ ] **Step 3: 实现**

3a. `app/core/errors.py`:

```python
"""对外 REST 错误契约的专用异常。"""


class NotFoundError(ValueError):
    """资源不存在(账号/任务/运营者/check_id)→ HTTP 404。

    继承 ValueError:未升级的旧调用方(按 ValueError 捕获/断言)行为不变;
    Starlette handler 查找按异常类精确优先,NotFoundError 走 404,裸 ValueError 走 400。
    """
```

3b. `app/http/system.py`:whoami 从 server.py 平移过来:

```python
"""system 分组 REST:whoami 身份探针(/healthz 留在 server.py,走免鉴权白名单)。"""

from fastapi import APIRouter

from app.auth.context import current_operator

router = APIRouter()

MANIFEST_ENTRIES = [{
    "method": "GET", "path": "/api/whoami",
    "summary": "返回当前 apikey 对应的运营者身份(轻量验 key)",
    "admin_only": False, "params": {},
    "returns": "{name, role}",
    "errors": "401=apikey 缺失/无效/停用",
    "notes": "完整上手信息用 GET /api/manifest。",
}]


@router.get("/api/whoami")
async def whoami() -> dict:
    """当前 apikey 的身份(中间件已鉴权,ContextVar 必有运营者)。"""
    op = current_operator()
    return {"name": op.name, "role": op.role}
```

3c. `app/http/manifest.py`——叙事常量平移自 server.py 的 MCP_INSTRUCTIONS(工具名全部换成 REST 路径):

```python
"""GET /api/manifest —— 服务自描述:agent 带 apikey 调这一个接口即获全部上手信息。

组成:服务叙事(workflows/constraints,平移自原 MCP_INSTRUCTIONS)+ 错误契约 +
全部端点元数据(聚合各 router 模块的 MANIFEST_ENTRIES)+ caller 身份与权限摘要。
端点集合与实际路由的一致性由 tests/test_manifest.py 防漂移测试钉死。
"""

from fastapi import APIRouter
from sqlalchemy import func, select

from app import __version__
from app.auth.context import current_operator
from app.auth.guards import visible_account_ids
from app.core.config import settings
from app.core.db import get_session
from app.models.xhs_account import XhsAccount

router = APIRouter()

_DESCRIPTION = (
    "nbdpsy-api:小红书运营能力后台(自动发布 / 多账号管理 / cookie 管理 / 远程登录)。"
    "消费方是运营侧 agent:先读本 manifest,再按 endpoints 直接调 REST。"
)

_AUTH = {
    "scheme": "每个 /api/* 请求带请求头 Authorization: Bearer <apikey>(或 X-API-Key: <apikey>)",
    "whitelist": "/healthz 与 /downloads/* 免鉴权",
    "errors": "401 见 error_contract;403=越权(动了没授权的账号或非 admin 调管理端点)",
}

_WORKFLOWS = [
    "接入自检:GET /healthz(通)→ GET /api/manifest(200 即 key 有效,响应含你的身份与可操作账号数)。",
    "远程登录(没有登录接口,登录靠人 + chrome 插件):GET /api/extension 拿 server_time + 插件下载地址 + "
    "安装步骤,把插件递给操作者装好扫码;之后每 ~10s GET /api/login/poll?since=<server_time> 直到 "
    "done=true——登新号不传 account_id,重登旧号传 account_id;建议设 5-10 分钟超时。",
    "cookie 活性:POST /api/accounts/{id}/cookie-checks 发起(202 回 check_id),每 2-5s "
    "GET /api/cookie-checks/{check_id} 轮询到 valid/invalid/captcha/error;error 是基础设施失败,"
    "不代表 cookie 失效。别用它探登录进度——等登录用 /api/login/poll。",
    "发布:POST /api/publish-jobs(202 回 job_id)→ 每 5-10s GET /api/publish-jobs/{job_id} 轮询到 "
    "published/failed;publishing 常态 1-3 分钟,失败自动重试最多 3 次(退避约 2/10/30 分钟),"
    "单条任务最长约 40 分钟才落 failed。同一账号的发布自动串行。",
    "典型编排:manifest → GET /api/accounts →(操作者用插件登录,login/poll 收口)→ cookie-checks 验活 "
    "→ publish-jobs → 轮询终态。",
]

_CONSTRAINTS = [
    "发布仅支持图文:图片 ≥1 且 ≤18 张(越界立即 400);不支持视频。",
    "标题按显示长度截断 ≤20、正文截断 ≤900、话题去重后截断 ≤10——长度类均静默硬截断不报错,请自行控长。",
    "schedule_time 定时发布务必带时区偏移(如 2026-01-01T09:00:00+08:00);不带偏移按 UTC 解释,会早/晚 8 小时。",
    "图片三形态:http(s) URL 字符串 / data URI 字符串 / {b64, ext} 对象;服务端自行下载/解码。",
    "RBAC:非 admin 只能看到/操作被授权的账号;admin 全见。",
]

_ERROR_CONTRACT = {
    "400": '{"error": ...} 入参非法(图片张数越界、status 枚举错、since 非 ISO8601 等)',
    "401": '{"detail": ...} apikey 缺失/无效/运营者被停用(中间件层,注意键是 detail)',
    "403": '{"error": ...} 越权(没授权的账号 / 非 admin 调管理端点)',
    "404": '{"error": ...} 资源不存在(账号/任务/运营者/check_id)',
    "422": '{"detail": [...]} 请求体不符合 schema(FastAPI 校验)',
    "500": '{"error": ...} 未预期异常,联系管理员查日志',
}

MANIFEST_ENTRIES = [{
    "method": "GET", "path": "/api/manifest",
    "summary": "本自描述接口:服务叙事 + 全部端点元数据 + caller 身份",
    "admin_only": False, "params": {},
    "returns": "{service, version, description, base_url, auth, caller, workflows, constraints, error_contract, endpoints}",
    "errors": "401=apikey 缺失/无效/停用",
    "notes": "接入后第一站,一次拿全上手信息。",
}]


@router.get("/api/manifest")
async def manifest() -> dict:
    """服务自描述 + caller 身份(须鉴权:验 key 与上手一步完成)。"""
    # 延迟导入聚合表,避免与 app.http 包 __init__ 循环导入。
    from app.http import ALL_MANIFEST_ENTRIES

    op = current_operator()
    async with get_session() as session:
        ids = await visible_account_ids(op, session)
        if ids is None:  # admin:全量账号数
            account_count = (
                await session.execute(select(func.count()).select_from(XhsAccount))
            ).scalar_one()
        else:
            account_count = len(ids)
    return {
        "service": "nbdpsy-api",
        "version": __version__,
        "description": _DESCRIPTION,
        "base_url": settings.PUBLIC_BASE_URL,
        "auth": _AUTH,
        "caller": {
            "operator_id": op.id,
            "name": op.name,
            "role": op.role,
            "account_count": account_count,
        },
        "workflows": _WORKFLOWS,
        "constraints": _CONSTRAINTS,
        "error_contract": _ERROR_CONTRACT,
        "endpoints": ALL_MANIFEST_ENTRIES,
    }
```

3d. `app/http/accounts_rest.py` 与 `app/http/cookies_import.py` 各加模块级 MANIFEST_ENTRIES(**不动端点逻辑**):

accounts_rest.py(2 条):
```python
MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/accounts",
        "summary": "列出 caller 可见的小红书账号(operator 只见被授权的,admin 全见)",
        "admin_only": False, "params": {},
        "returns": "{accounts: [{id, name, nickname, user_id, red_id, avatar, status, cookie_status, last_check_at, last_login_at, created_at}]}",
        "errors": "",
        "notes": "刻意不含 cookie 明文;cookie_status/last_check_at 可做廉价活性预检。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}/cookies",
        "summary": "解密回读某号 cookie(受授权限制)",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": "{account_id, cookies: [cookie 对象]}",
        "errors": "403=无该号授权",
        "notes": "用于把 cookie 注入自己的浏览器等程序化场景。",
    },
]
```

cookies_import.py(1 条):
```python
MANIFEST_ENTRIES = [{
    "method": "POST", "path": "/api/cookies/import",
    "summary": "灌入某号 cookie(upsert 唯一账号行)",
    "admin_only": False,
    "params": {"account_name": "body,str", "cookies": "body,list[cookie 对象]",
               "user_info": "body,dict|None(user_id/nickname/red_id/avatar)"},
    "returns": "{account_id, created}",
    "errors": "422=缺字段",
    "notes": "正常远程登录由 chrome 插件登录后自动推本端点,多数情况不用手调;"
             "user_info.user_id 是 upsert 去重键;首次导入新号自动给导入者建授权。",
}]
```

3e. `app/http/__init__.py` 注册表:

```python
"""REST 路由注册表:server.py 统一 include;manifest 聚合各模块端点元数据。

新增 router 模块的接线口就在这里:import 模块 → ALL_ROUTERS 加 router →
ALL_MANIFEST_ENTRIES 拼其 MANIFEST_ENTRIES(/api/* 之外的路由如 downloads 不进 manifest)。
漏接会被 tests/test_manifest.py 防漂移测试逮住。
"""

from app.http import accounts_rest, cookies_import, downloads, manifest, system

ALL_ROUTERS = [
    system.router,
    manifest.router,
    accounts_rest.router,
    cookies_import.router,
    downloads.router,
]

ALL_MANIFEST_ENTRIES = [
    *system.MANIFEST_ENTRIES,
    *manifest.MANIFEST_ENTRIES,
    *accounts_rest.MANIFEST_ENTRIES,
    *cookies_import.MANIFEST_ENTRIES,
]
```

3f. `app/server.py`:
- 删内联 `/api/whoami` 端点与三个单独 `include_router` 调用及其 import,改为:
  ```python
  from app.http import ALL_ROUTERS
  ...
  for r in ALL_ROUTERS:
      app.include_router(r)
  ```
- 加两个 handler(放在现有 AccessDenied handler 之后):
  ```python
  from app.core.errors import NotFoundError

  @app.exception_handler(NotFoundError)
  async def _handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
      """资源不存在 → 404 JSON。"""
      return JSONResponse({"error": str(exc)}, status_code=404)

  @app.exception_handler(ValueError)
  async def _handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
      """入参非法 → 400 JSON(NotFoundError 是其子类但按精确类优先走 404)。"""
      return JSONResponse({"error": str(exc)}, status_code=400)
  ```
- **本 Task 不动** FastMCP/MCP_INSTRUCTIONS/combine_lifespans//mcp 挂载(Task 6 删)。

- [ ] **Step 4: 跑测试通过**

`/home/roots/nbdpsy-mcp/.venv/bin/python -m pytest tests/test_manifest.py tests/test_accounts_rest.py tests/test_auth_middleware.py -q` → 全 PASS(whoami 迁移后 test_auth_middleware 里的 whoami 用例必须仍绿)。

- [ ] **Step 5: 全量回归 + 提交**

`/home/roots/nbdpsy-mcp/.venv/bin/python -m pytest tests/ -q` 全绿后:

```bash
git add app/core/errors.py app/http/system.py app/http/manifest.py app/http/__init__.py \
  app/http/accounts_rest.py app/http/cookies_import.py app/server.py \
  tests/rest_helpers.py tests/test_manifest.py tests/test_accounts_rest.py
git commit -m "feat(rest): manifest 自描述接口 + 路由注册表 + NotFoundError 错误契约(地基)"
```

---

### Task 2: admin 分组 REST(8 端点)

**Files:**
- Create: `app/http/admin_rest.py`、`tests/test_admin_rest.py`
- Modify: `app/http/__init__.py`(接线 3 行)、`app/services/operator_service.py`(仅"不存在"raise 改 NotFoundError)
- 参照平移源:`app/tools/admin.py`(常量 `_APIKEY_NOTE` 原样搬)、`tests/test_admin_tools.py`(场景矩阵)

**Interfaces:**
- Consumes:Task 1 的 `NotFoundError`、`rest_helpers`、注册表约定
- Produces:8 个 `/api/operators*` 端点(返回键与原工具全等,见下)

- [ ] **Step 1: 写失败测试 `tests/test_admin_rest.py`**

平移 `tests/test_admin_tools.py` 三个用例的断言矩阵 + REST 特有用例。完整用例清单(每个都要写,断言精确到 HTTP code 与返回键):

```python
"""admin 分组 REST 测试:仅 admin 可调 / apikey 生命周期 / 授权往返。"""

from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

# 8 端点 (method, path 构造器) 清单,用于逐一打非 admin 拦截
_ADMIN_CALLS = [
    ("POST", "/api/operators", {"name": "x"}),
    ("GET", "/api/operators", None),
    ("PATCH", "/api/operators/1", {"enabled": False}),
    ("DELETE", "/api/operators/1", None),
    ("POST", "/api/operators/1/rotate-apikey", None),
    ("POST", "/api/operators/1/grants", {"xhs_account_id": 1}),
    ("DELETE", "/api/operators/1/grants/1", None),
    ("GET", "/api/operators/1/grants", None),
]


async def test_all_admin_endpoints_block_non_admin(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "plain-operator-key"
        await make_operator(op_key)
        for method, path, body in _ADMIN_CALLS:
            r = await client.request(method, path, json=body, headers=bearer(op_key))
            assert r.status_code == 403, f"{method} {path} 应 403,得 {r.status_code}"
            assert "需要管理员权限" in r.json()["error"]
```

其余用例(各自完整实现,模式同上):
- `test_create_operator_returns_plaintext_and_new_key_works`:admin POST /api/operators {"name":"alice"} → 200,返回 `{id,name,role=="operator",enabled==True,apikey,note}`;紧接着用返回的 apikey GET /api/whoami → 200 且 name=="alice"。
- `test_create_operator_missing_name_422`:POST 空 body → 422。
- `test_list_operators_contains_root_and_created`:GET → operators 列表含 root 与新建者,每项键 `{id,name,role,enabled,created_at}`。
- `test_update_operator_disable_then_key_rejected`:建 operator(带 key)→ PATCH enabled=false → 200;旧 key 调 /api/whoami → 401(停用生效)。
- `test_update_operator_unknown_id_404`:PATCH /api/operators/9999 → 404,body 键 `error`。
- `test_rotate_apikey_old_dies_new_works`:rotate → 200 返回新 apikey;旧 key 401、新 key 200。
- `test_grant_list_revoke_roundtrip`:seed_account + 建 operator → POST grants {xhs_account_id} → 200 `{id,operator_id,xhs_account_id}`;GET grants → `{operator_id, xhs_account_ids:[acc]}`;该 operator key 能 GET /api/accounts/{acc};DELETE grants → `{revoked:True}`;再 GET grants 空列表,operator 访问该号 403。
- `test_delete_operator`:DELETE → 200 `{deleted:id}`;其 key 401。

- [ ] **Step 2: 跑测试确认失败**

`pytest tests/test_admin_rest.py -x -q` → FAIL(404 路由不存在)。

- [ ] **Step 3: 实现 `app/http/admin_rest.py`**

平移 `app/tools/admin.py` 的 8 个工具体(每端点首行 `require_admin(current_operator())`,service 调用与返回 dict **逐键照抄**)。骨架:

```python
"""admin 分组 REST(仅 admin 角色):运营者 CRUD / apikey 轮换 / 账号授权。"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.context import current_operator
from app.auth.guards import require_admin
from app.core.db import get_session
from app.services import operator_service

router = APIRouter()

# _APIKEY_NOTE 从 app/tools/admin.py 原样搬过来


class OperatorCreateRequest(BaseModel):
    name: str
    role: Literal["operator", "admin"] = "operator"


class OperatorUpdateRequest(BaseModel):
    name: str | None = None
    role: Literal["operator", "admin"] | None = None
    enabled: bool | None = None


class GrantRequest(BaseModel):
    xhs_account_id: int


@router.post("/api/operators")
async def create_operator_endpoint(payload: OperatorCreateRequest) -> dict:
    """建运营者,返回一次性明文 apikey(库里只存 hash,不可回读)。"""
    require_admin(current_operator())
    async with get_session() as session:
        op, apikey = await operator_service.create_operator(
            session, payload.name, role=payload.role
        )
        return {"id": op.id, "name": op.name, "role": op.role,
                "enabled": op.enabled, "apikey": apikey, "note": _APIKEY_NOTE}
```

其余 7 个端点同模式:GET /api/operators(键 `{operators:[{id,name,role,enabled,created_at}]}`)、PATCH /api/operators/{operator_id}(→ `{id,name,role,enabled}`)、DELETE(→ `{deleted}`)、POST .../rotate-apikey(→ `{id,apikey,note}`)、POST .../grants(`grant_access(session, operator_id, payload.xhs_account_id, granted_by=admin.id)` → `{id,operator_id,xhs_account_id}`)、DELETE .../grants/{xhs_account_id}(→ `{operator_id,xhs_account_id,revoked:True}`)、GET .../grants(→ `{operator_id,xhs_account_ids}`)。

`app/services/operator_service.py`:`update_operator`/`rotate_apikey` 里"运营者 … 不存在"的 `raise ValueError` 改 `raise NotFoundError`(`from app.core.errors import NotFoundError`),其余不动。

MANIFEST_ENTRIES:8 条,全部 `"admin_only": True`;create/rotate 的 notes 必须写"apikey 仅此一次显示";grants 的 notes 写"授权是二元的:有 grant 即可全功能操作该号"。

`app/http/__init__.py`:import 加 `admin_rest`,ALL_ROUTERS 加 `admin_rest.router`,ALL_MANIFEST_ENTRIES 拼 `*admin_rest.MANIFEST_ENTRIES`。

- [ ] **Step 4: 跑测试通过**

`pytest tests/test_admin_rest.py tests/test_manifest.py tests/test_operator_service.py -q` → PASS(防漂移测试验证 8 条 entries 已接)。

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add app/http/admin_rest.py app/http/__init__.py app/services/operator_service.py tests/test_admin_rest.py
git commit -m "feat(rest): admin 分组 8 端点——运营者 CRUD/apikey 轮换/账号授权"
```

---

### Task 3: accounts + 登录闭环 + extension REST

**Files:**
- Modify: `app/http/accounts_rest.py`(加 4 端点 + 4 条 entries)、`app/services/account_service.py`(仅"不存在"raise 改 NotFoundError)、`app/http/__init__.py`(接线 extension_rest)、`tests/test_accounts_rest.py`(扩测试)
- Create: `app/http/extension_rest.py`、`tests/test_extension_rest.py`
- 参照平移源:`app/tools/accounts.py`(`_parse_since` + poll_login 整体搬)、`app/tools/extension.py`(常量 `_APIKEY_HINT`/`_INSTALL_STEPS` 原样搬)、`tests/test_account_tools.py` + `tests/test_extension_download.py` 的工具用例(场景矩阵)

**Interfaces:**
- Consumes:Task 1 全部
- Produces:`GET/PATCH/DELETE /api/accounts/{account_id}`、`GET /api/login/poll`、`GET /api/extension`

- [ ] **Step 1: 写失败测试**

`tests/test_accounts_rest.py` 追加用例,平移 `tests/test_account_tools.py` 矩阵(REST 形态,每个完整实现):
- `test_get_account_view_and_denied`:授权号 GET → 200 account_view 键全(且无 login_cookies);未授权号 → 403;不存在 → 404。
- `test_update_account_name_happy_and_denied`:PATCH {"name":"新名"} → 200 name 变;未授权 → 403。
- `test_update_account_rejects_extra_fields`:PATCH {"status":"x"} → 422(Pydantic 模型只收 name;这是与工具 ValueError 语义的差异,收严可接受)。
- `test_delete_account_happy_and_denied`:DELETE → `{deleted:id}` 后 GET → 404;未授权 → 403。
- `test_poll_login_new_account_done`:seed 前记 since=utcnow iso;seed_account 后 GET /api/login/poll?since=... → `{done:True, accounts:[...]}`。
- `test_poll_login_by_account_id`:对既有号,since 晚于 last_login_at → done False;把 last_login_at 刷新到 since 之后 → done True,键 `account`。
- `test_poll_login_rbac_narrowed`:operator 无授权 → done False accounts 空;admin 全见。
- `test_poll_login_bad_since_400`:since="garbage" → 400。
- `test_poll_login_unknown_account_404`。
- `test_parse_since_normalizes_to_naive_utc`:单测直接 import `_parse_since`(平移自 test_account_tools 同名用例)。

`tests/test_extension_rest.py`(平移 test_extension_download.py 的 3 个工具用例为 REST):
- `test_extension_requires_apikey`:无 key → 401。
- `test_extension_returns_download_info`:admin key → 200,键 `{download_url, version, apikey_hint, install_steps, server_time}`;download_url 含 `/downloads/extension.zip?t=`;install_steps 非空 list;server_time 可被 `datetime.fromisoformat` 解析;apikey_hint 不含任何明文 key。

- [ ] **Step 2: 确认失败** `pytest tests/test_accounts_rest.py tests/test_extension_rest.py -x -q` → FAIL。

- [ ] **Step 3: 实现**

3a. `app/http/accounts_rest.py` 加(逻辑照抄 `app/tools/accounts.py` 对应工具体,含 `_parse_since` 整函数搬入本文件):

```python
class AccountUpdateRequest(BaseModel):
    """账号可改字段白名单(目前仅 name);extra 字段直接 422。"""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None


@router.get("/api/accounts/{account_id}")
async def get_account_endpoint(account_id: int) -> dict:
    operator = current_operator()
    async with get_session() as session:
        account = await account_service.get_account(session, operator, account_id)
        return account_service.account_view(account)
```

PATCH:`fields = {k: v for k, v in payload.model_dump().items() if v is not None}` 后 `update_account(session, operator, account_id, **fields)` → account_view。DELETE → `{"deleted": account_id}`。

`GET /api/login/poll`:签名 `async def poll_login_endpoint(since: str, account_id: int | None = None) -> dict`,函数体与 `app/tools/accounts.py::poll_login` 逐行对齐(visible_account_ids 收窄 / or 条件 / account_id 分支),"账号 … 不存在"改抛 NotFoundError。

`app/services/account_service.py`:`get_account`/`update_account` 里"账号 … 不存在"的 ValueError 改 NotFoundError;update 的字段白名单 ValueError **保持裸 ValueError**(400)。

3b. `app/http/extension_rest.py`:平移 `app/tools/extension.py` 全部(常量 + 逻辑),端点 `GET /api/extension`,返回键与工具全等 `{download_url, version, apikey_hint, install_steps, server_time}`。MANIFEST_ENTRIES 1 条,notes 写清:"登录闭环起点:记下 server_time 作为 /api/login/poll 的 since 起点;download_url 免鉴权可直接递给操作者"。

3c. accounts_rest.py 的 MANIFEST_ENTRIES 追加 4 条(GET/PATCH/DELETE /api/accounts/{account_id}、GET /api/login/poll);poll 的 notes 必须写轮询协议(~10s 间隔、登新号不传 account_id、重登旧号传、5-10 分钟超时)。`app/http/__init__.py` 接线 extension_rest(3 行)。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_accounts_rest.py tests/test_extension_rest.py tests/test_manifest.py -q` → PASS。

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add app/http/accounts_rest.py app/http/extension_rest.py app/http/__init__.py \
  app/services/account_service.py tests/test_accounts_rest.py tests/test_extension_rest.py
git commit -m "feat(rest): accounts CRUD + /api/login/poll 登录闭环 + /api/extension"
```

---

### Task 4: cookie 活性巡检 REST(异步对)

**Files:**
- Create: `app/http/cookies_rest.py`、`tests/test_cookie_checks_rest.py`
- Modify: `app/http/__init__.py`(接线 3 行)
- 参照平移源:`app/tools/cookies.py`(check_cookies / get_cookie_check / `_decrypt_account_cookies` 整体搬)、`tests/test_publish_tools.py` 里 5 个 cookie-check 用例(场景矩阵与 monkeypatch 点)

**Interfaces:**
- Consumes:Task 1 全部;`app.services.cookie_check.start_check(account_id, cookies) -> str` 与 `get_check(check_id) -> dict|None`(现有,不动)
- Produces:`POST /api/accounts/{account_id}/cookie-checks`(202)、`GET /api/cookie-checks/{check_id}`

- [ ] **Step 1: 写失败测试 `tests/test_cookie_checks_rest.py`**

平移 `tests/test_publish_tools.py` 的 cookie-check 用例(browser 检测的 monkeypatch 点**照旧测试原样搬**——它 patch 的是 `app.services.cookie_check` 内部的检测函数,不是工具层):
- `test_start_check_returns_202_check_id_then_valid`:授权号 POST cookie-checks → 202 `{check_id, status:"checking"}`;(monkeypatch 检测函数返回 valid)轮询 GET → 200 `{status:"valid", user_info:{...}}`。
- `test_start_check_denied_without_access` → 403。
- `test_start_check_unknown_account_404`。
- `test_check_error_state_carries_reason`:patch 检测函数抛异常 → 轮询到 `{status:"error", reason:...}`。
- `test_get_check_unknown_id_404`。
- `test_get_check_denied_cross_operator`:A 发起,B(无该号授权)查 → 403。

- [ ] **Step 2: 确认失败** `pytest tests/test_cookie_checks_rest.py -x -q` → FAIL。

- [ ] **Step 3: 实现 `app/http/cookies_rest.py`**

```python
"""cookie 活性巡检 REST(异步对):发起检测(202)+ 轮询结果。"""

import json

from fastapi import APIRouter

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import cookie_check

router = APIRouter()


@router.post("/api/accounts/{account_id}/cookie-checks", status_code=202)
async def start_cookie_check_endpoint(account_id: int) -> dict:
    """异步发起该号 cookie 活性检测,立即返回 check_id(检测 20-40s,不阻塞)。"""
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        cookies = _decrypt_account_cookies(account)
    check_id = cookie_check.start_check(account_id, cookies)
    return {"check_id": check_id, "status": "checking"}


@router.get("/api/cookie-checks/{check_id}")
async def get_cookie_check_endpoint(check_id: str) -> dict:
    """轮询检测结果:checking / valid / invalid / captcha / error(error≠cookie 失效)。"""
    entry = cookie_check.get_check(check_id)
    if entry is None:
        raise NotFoundError(f"check_id {check_id} 不存在或已过期")
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, entry["account_id"], session)
    result: dict = {"status": entry["status"]}
    if entry.get("user_info") is not None:
        result["user_info"] = entry["user_info"]
    if entry.get("reason") is not None:
        result["reason"] = entry["reason"]
    return result


# _decrypt_account_cookies 从 app/tools/cookies.py 原样搬入
```

MANIFEST_ENTRIES 2 条:POST 的 notes 写"检测约 20-40s,别对同号重复发起;每 2-5s 轮询";GET 的 notes 写五态含义,特别是"error 是基础设施失败不代表 cookie 失效,别据此让人重登"、"check_id 进程重启即丢,404 时重新发起"。`app/http/__init__.py` 接线。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_cookie_checks_rest.py tests/test_cookie_check.py tests/test_manifest.py -q` → PASS。

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add app/http/cookies_rest.py app/http/__init__.py tests/test_cookie_checks_rest.py
git commit -m "feat(rest): cookie 活性巡检异步对——发起 202 + 轮询五态"
```

---

### Task 5: publish 分组 REST(4 端点)

**Files:**
- Create: `app/http/publish_rest.py`、`tests/test_publish_rest.py`、`tests/test_publish_runner.py`
- Modify: `app/http/__init__.py`(接线 3 行)
- 参照平移源:`app/tools/publish.py`(`_parse_schedule_time`/`_job_view`/`_JOB_STATUSES`/`_MAX_IMAGES` 全搬)、`tests/test_publish_tools.py`(发布用例矩阵 + runner 用例)

**Interfaces:**
- Consumes:Task 1 全部;`app.publish.runtime.get_active_scheduler()`(现有)
- Produces:`POST /api/publish-jobs`(202)、`GET /api/publish-jobs/{job_id}`、`GET /api/publish-jobs`、`POST /api/publish-jobs/{job_id}/cancel`

- [ ] **Step 1: 写失败测试 `tests/test_publish_rest.py`**

平移 `tests/test_publish_tools.py` 的发布用例矩阵(scheduler 的 stub/monkeypatch 方式照旧测试原样;rest_client 跑真 lifespan 会起真调度器,旧测试若用 `set_active_scheduler(stub)`,在 client 起来后再 set 覆盖即可):
- `test_publish_note_creates_pending_and_enqueues`:POST → 202 `{job_id, status:"pending"}`,stub scheduler 收到 submit(job_id)。
- `test_publish_scheduled_not_enqueued`:带 `schedule_time="2030-01-01T09:00:00+08:00"` → 202 且 stub 未收 submit;DB 里 schedule_time 为 naive UTC `2030-01-01T01:00:00`。
- `test_parse_schedule_time_tzaware_naive_none`:单测 `_parse_schedule_time` 三态(平移原用例)。
- `test_publish_denied_without_access` → 403。
- `test_publish_rejects_empty_images` / `test_publish_rejects_19_images` → 400,error 文案含"图片"。
- `test_get_status_reads_and_denied`:job 建后 GET → 200 键 `{job_id,account_id,title,status,note_id,note_url,error,retries,schedule_time,next_retry_at,created_at}`;他人 → 403;9999 → 404。
- `test_list_jobs_access_filter`:operator 只见授权号的 job;admin 全见。
- `test_list_jobs_status_filter_and_bad_status_400`:`?status=pending` 生效;`?status=xx` → 400。
- `test_list_jobs_limit`:`?limit=1` 只回最新 1 条。
- `test_cancel_only_pending`:pending → `{ok:True}` 且状态 canceled;再取消 → `{ok:False, status:"canceled"}`;9999 → 404。

`tests/test_publish_runner.py`:把 `test_publish_tools.py::test_runner_materializes_images_and_cleans_workdir` **原样整体搬入**(runner 层测试与工具层无关,为 Task 6 删旧文件预挪)。

- [ ] **Step 2: 确认失败** `pytest tests/test_publish_rest.py -x -q` → FAIL。

- [ ] **Step 3: 实现 `app/http/publish_rest.py`**

```python
"""publish 分组 REST:建发布任务(202)/ 查状态 / 列任务 / 取消。"""

import json

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.publish_job import PublishJob
from app.publish.runtime import get_active_scheduler
from sqlalchemy import select

# _JOB_STATUSES / _MAX_IMAGES / _parse_schedule_time / _job_view
# 从 app/tools/publish.py 原样搬入(逐行照抄)

router = APIRouter()


class PublishNoteRequest(BaseModel):
    account_id: int
    title: str
    content: str
    images: list
    topics: list[str] = []
    schedule_time: str | None = None


@router.post("/api/publish-jobs", status_code=202)
async def publish_note_endpoint(payload: PublishNoteRequest) -> dict:
    """发布图文笔记(异步入队):函数体与 app/tools/publish.py::publish_note 逐行对齐。"""
    ...


@router.get("/api/publish-jobs/{job_id}")
async def get_publish_status_endpoint(job_id: int) -> dict:
    """job 不存在 → NotFoundError(404);越权 → 403;返回 _job_view。"""
    ...


@router.get("/api/publish-jobs")
async def list_publish_jobs_endpoint(
    account_id: int | None = None, status: str | None = None, limit: int = 50
) -> dict:
    """与 list_publish_jobs 工具逐行对齐;status 非法 → 裸 ValueError(400)。"""
    ...


@router.post("/api/publish-jobs/{job_id}/cancel")
async def cancel_publish_job_endpoint(job_id: int) -> dict:
    """仅 pending 可取消;job 不存在 → 404。"""
    ...
```

(`...` 处 = 把 `app/tools/publish.py` 对应工具函数体原样搬入,仅两处改动:①"发布任务 … 不存在"的 ValueError 改 NotFoundError;②入参从工具签名改为 payload/query。)

MANIFEST_ENTRIES 4 条,POST 的 params 写全六字段与图片三形态,notes 写异步契约(5-10s 轮询、1-3 分钟常态、重试 3 次退避 2/10/30 分钟、同号串行、静默截断规则);GET status 的 notes 写状态枚举五态与 next_retry_at 语义。`app/http/__init__.py` 接线。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_publish_rest.py tests/test_publish_runner.py tests/test_publish_scheduler.py tests/test_manifest.py -q` → PASS。

- [ ] **Step 5: 全量回归 + 提交**

```bash
git add app/http/publish_rest.py app/http/__init__.py tests/test_publish_rest.py tests/test_publish_runner.py
git commit -m "feat(rest): publish 分组 4 端点——异步建任务/查状态/列表/取消"
```

---

### Task 6: 收口——删 MCP 全家 + 文档重写(串行,2-5 全部合并后)

**Files:**
- Delete: `app/tools/`(整目录)、`.claude-plugin/`、`plugins/`、`tests/test_admin_tools.py`、`tests/test_account_tools.py`、`tests/test_publish_tools.py`
- Modify: `app/server.py`、`requirements.txt`、`app/__init__.py`(版本 0.1.0→0.2.0)、`CHANGELOG.md`(无则建)、`README.md`、`docs/onboarding/operator-config-package.md`、`docs/DEPLOY.md`、`tests/test_auth_middleware.py`、`tests/test_extension_download.py`、`tests/test_server_health.py`(如引用 MCP)
- Create: `tests/test_mcp_removed.py`

**Interfaces:**
- Consumes:Task 1-5 全部端点已就位
- Produces:无 MCP 的最终形态

- [ ] **Step 1: 写失败测试 `tests/test_mcp_removed.py`**

```python
"""MCP 已删除的回归钉:/mcp 不再存在,fastmcp 不再被 app 引用。"""

import subprocess
import sys

from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


async def test_mcp_endpoint_gone(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/mcp/", headers=bearer(ADMIN_KEY), json={})
        assert r.status_code == 404


def test_no_fastmcp_import_in_app():
    """app/ 源码里不允许再出现 fastmcp 引用(文档/历史除外)。"""
    out = subprocess.run(
        ["grep", "-ri", "fastmcp", "app/"], capture_output=True, text=True
    )
    assert out.stdout == "", f"app/ 仍引用 fastmcp:\n{out.stdout}"
```

- [ ] **Step 2: 确认失败** `pytest tests/test_mcp_removed.py -x -q` → FAIL(/mcp 仍在)。

- [ ] **Step 3: 实施删除**

3a. `app/server.py` 重写头部与装配(保留:assert_secret_key_configured、lifespan 全部内容、ApiKeyMiddleware、四个异常 handler、/healthz、ALL_ROUTERS 循环):
- 删 import:`FastMCP`、`combine_lifespans`、`register_all`
- 删 `MCP_INSTRUCTIONS` 常量、`mcp = FastMCP(...)`、`register_all(mcp)`、`mcp_app = mcp.http_app(...)`、`app.mount("/mcp", mcp_app)`
- `FastAPI(title="nbdpsy-api", lifespan=app_lifespan)`(不再 combine)
- 模块 docstring 里的 fastmcp API 结论笔记整段删除,换成一句"纯 REST 装配:路由见 app/http 注册表,自描述见 GET /api/manifest"

3b. `rm -r app/tools/`;`requirements.txt` 删 `fastmcp>=3.4,<4` 行;`rm -r .claude-plugin/ plugins/`。

3c. 测试清理:
- 删 `tests/test_admin_tools.py`、`tests/test_account_tools.py`、`tests/test_publish_tools.py`(REST 版已在 Task 2/3/5 全覆盖;runner 用例已挪 test_publish_runner.py)
- `grep -rn "fastmcp\|mcp\." tests/` 逐个处理:`test_auth_middleware.py` 的 `test_contextvar_propagation_into_mcp_tool` 删除(ContextVar 穿透 REST 已被 whoami/accounts 用例覆盖);`test_extension_download.py` 的 3 个 `test_tool_*` 用例删除(REST 版在 test_extension_rest.py);`test_server_health.py` 若引用 mcp 同理改/删。

3d. 版本与变更:`app/__init__.py` `__version__ = "0.2.0"`;`CHANGELOG.md` 追加 0.2.0 条目(纯 REST 转型、manifest、MCP 删除、破坏性说明:MCP 客户端接入方式作废)。

3e. 文档重写:
- `README.md`:「MCP 工具清单」(145-208 行)改「API 端点清单」(按 manifest 六组列 method+path+一句话,注明"完整契约以 GET /api/manifest 为准");「安装/接入 MCP」(210-309 行)整章替换为「接入(三步)」:①拿 apikey ②`curl https://mcp.nbdpsy.com/healthz` ③`curl -H "Authorization: Bearer <key>" https://mcp.nbdpsy.com/api/manifest`,agent 读 manifest 即可干活;删 marketplace 段;「架构总览」「插件配置」里的 MCP 表述改 API。
- `docs/onboarding/operator-config-package.md`:重写为 REST 版——后台生成流程改为调 REST(`POST /api/operators`、`POST /api/operators/{id}/grants`,curl 示例带 admin key 占位);配置包模板改为:给 Claude 的指令 = base URL + `{{APIKEY}}` + 自检(healthz→manifest→`GET /api/accounts`)+ "干活前先读 manifest 的 workflows/constraints/endpoints";删 marketplace/claude mcp add 全部内容;连通性判据表删 421 行、加 404(路径拼错)。
- `docs/DEPLOY.md`:`grep -n "mcp/" docs/DEPLOY.md`,把 MCP initialize 探针的验证命令换成 manifest 探针(`curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" https://mcp.nbdpsy.com/api/manifest` 期望 200)。

- [ ] **Step 4: 跑测试通过** `pytest tests/ -q` → 全绿(全套)。

- [ ] **Step 5: 提交**

```bash
git add -u app/ tests/ requirements.txt README.md CHANGELOG.md docs/onboarding/operator-config-package.md docs/DEPLOY.md
git add tests/test_mcp_removed.py
git rm -r --cached .claude-plugin plugins 2>/dev/null || true
git commit -m "feat(rest)!: 删除 MCP 全家——纯 REST 收口,v0.2.0(BREAKING: MCP 接入作废,改 GET /api/manifest)"
```

(`git add -u` 仅限已跟踪文件的删除/修改收录,配合显式列新文件;不使用 `git add -A`。)

---

## 合并与部署顺序(lead 执行)

1. Task 1 分支先 review + merge 进 main。
2. Task 2-5 从新 main 拉分支**并行**实施;合并时 `app/http/__init__.py` 的一行式冲突由 lead 手工顺序合。
3. Task 6 在 2-5 全并后从 main 拉分支,串行实施。
4. 全并后:主仓 `git pull` → `sudo systemctl restart nbdpsy-mcp` → 公网验证:
   - `curl -s https://mcp.nbdpsy.com/healthz` → `{"ok":true}`
   - `GET /api/manifest` 带 admin key → 200,endpoints ≥ 22 条
   - 无 key → 401;operator key 调 `GET /api/operators` → 403
   - `POST https://mcp.nbdpsy.com/mcp/` → 404
