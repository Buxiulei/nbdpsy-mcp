# claude.ai 接入实施计划(图片上传 + 薄 MCP facade)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps 用 checkbox。

**Goal:** 让 claude.ai 网页/App 能发小红书:A 图片上传端点+上传页(图变 URL),B 薄 MCP facade(唯一官方通道,转发 REST)。

**Architecture:** A = UploadBatch 表 + upload_service + REST 端点 + /upload 页 + 白名单;B = mcp_facade.py(7 个薄工具 httpx 自转发本机 REST,apikey 从 MCP 请求头透传)+ server.py 重挂 /mcp。REST 是唯一真源,facade 零业务逻辑。

**Spec:** `docs/design/2026-07-15-claudeai-enablement-design.md`(契约/工具清单/错误契约以 spec 为准)

## Global Constraints

- 解释器 `/home/roots/nbdpsy-server/.venv/bin/python`;测试 `source .../activate && python -m pytest tests/ -q`(cwd=worktree 根)。**worktree 共用主仓 venv**(Pillow 12.3.0 / fastmcp 3.4.3 已在)。
- 注释/docstring/commit 全中文;禁 emoji(含 /upload 页,用内联 SVG);commit `type(scope): 描述`;**禁 git add -A,显式列文件**。
- 已核实事实:`materialize_images` 已支持 http URL(发布链零改);publish 端点 body = `{account_id,title,content,images,topics,schedule_time}`(facade 的 image_urls→images);`get_http_headers(include={"authorization","x-api-key"})` 才拿得到 apikey(默认剔除);旧 MCP 挂载见 `git show d3c1dc7^:app/server.py`。
- 端点鉴权/RBAC 沿用:`operator=current_operator()`、`async with get_session()`、`assert_account_access`;不存在抛 `app.core.errors.NotFoundError`(404)、越权 AccessDenied(403)、入参非法裸 ValueError(400)。
- 中间件白名单三路径:`/upload`(exact,页)、`/uploads/`(前缀,取图)免鉴权;`/api/uploads/*`(POST 上传/list)**走鉴权**(有 /api 前缀,天然不在白名单)。
- 模型:`from app.core.db import Base`,SQLAlchemy 2.0,注册进 app/models/__init__.py;alembic 迁移在 worktree 根 `alembic revision --autogenerate` 后核对再 `upgrade head`;**部署时 alembic upgrade 必须先于 restart**(lifespan create_all 会抢建表)。

---

### Task 1: A 基础——UploadBatch 模型 + 迁移 + upload_service + 白名单(可与 Task 3 并行)

**Files:**
- Create: `app/models/upload_batch.py`、`app/services/upload_service.py`、`tests/test_upload_service.py`、`alembic/versions/<新>_upload_batches.py`
- Modify: `app/models/__init__.py`(注册)、`app/auth/middleware.py`(加白名单)、`app/core/config.py`(可选:UPLOAD_MAX_MB/UPLOAD_TTL_DAYS,带默认)

**Interfaces(Produces):**
```python
class UploadBatch(Base):  # __tablename__="upload_batches"; batch_id UNIQUE; operator_id/file_count/created_at/expires_at
async def save_images(session, operator, files: list[tuple[str, bytes]], now) -> dict  # {batch_id, urls, expires_at}
async def list_batches(session, operator) -> list[dict]           # 该 operator 未过期批次
async def sweep_expired(session, now) -> int                       # 删过期批次目录+行,返回删除数
```

- [ ] **Step 1: 写失败测试 `tests/test_upload_service.py`**(参照 test_note_metrics_service 的 db fixture)

用例(每个完整实现;用 Pillow 造真 PNG bytes):
```
1. test_save_images_writes_pageorder_and_row:2 张真图 → 落盘 01/02.<ext>、返回 urls 顺序=入参顺序、插 UploadBatch 行 file_count=2 expires_at=now+7天
2. test_save_rejects_non_image:非图片 bytes(b"notimage")→ Pillow verify 失败 → 抛 ValueError,不落半批(目录不残留)
3. test_save_rejects_too_many / too_large:>18 张 / 单张 >UPLOAD_MAX_MB → ValueError
4. test_sweep_expired_removes_dir_and_row:造一个 expires_at<now 的批次(落个文件)→ sweep 删目录+行返回1;未过期不动
5. test_list_batches_only_own_unexpired:operator A 两批(一过期)→ list 只返未过期;B 看不到 A 的
```
（DATA_DIR 用 monkeypatch 指到 tmp_path;now 由测试注入固定值。）

- [ ] **Step 2: 确认失败** `pytest tests/test_upload_service.py -q` → FAIL。

- [ ] **Step 3: 实现**

3a. `app/models/upload_batch.py`:UploadBatch(参照 publish_job.py 风格);batch_id 唯一;`app/models/__init__.py` 注册。

3b. `app/services/upload_service.py`:
- `save_images`:校验张数(1-18)、逐张 Pillow `Image.open(BytesIO)`+`.verify()` 得真实 format→ext,单张 ≤UPLOAD_MAX_MB;batch_id=`secrets.token_urlsafe(12)`;落盘 `Path(settings.DATA_DIR)/"uploads"/batch_id/f"{i:02d}.{ext}"`(i 从 1);落盘失败清理已写文件;插 UploadBatch 行;末尾 `await sweep_expired(session, now)`(懒清理);urls 用 `settings.PUBLIC_BASE_URL`+`/uploads/{batch_id}/{NN}.{ext}` 拼;返回 dict。
- `list_batches`/`sweep_expired`:见接口。sweep 用 shutil.rmtree(ignore_errors) 删目录。

3c. `app/core/config.py`:加 `UPLOAD_MAX_MB: int = 10`、`UPLOAD_TTL_DAYS: int = 7`(带默认)。

3d. `app/auth/middleware.py`:白名单加 `/upload`(exact,进 `_WHITELIST_EXACT`)+ `/uploads` 前缀(仿 `_DOWNLOADS_ROOT` 加 `_UPLOADS_ROOT="/uploads"`,`_is_whitelisted` 补 `path==_UPLOADS_ROOT or path.startswith(_UPLOADS_ROOT+"/")`)。

- [ ] **Step 4: 跑测试通过** + alembic 迁移(核对含 upload_batches + UNIQUE(batch_id),upgrade head 验证)+ 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/models/upload_batch.py app/models/__init__.py app/services/upload_service.py \
  app/core/config.py app/auth/middleware.py tests/test_upload_service.py alembic/versions/<新>
git commit -m "feat(uploads): UploadBatch 表 + upload_service(落盘/懒清理/归属)+ 白名单"
```

---

### Task 2: A 端点 + /upload 页(串行,Task 1 合并后)

**Files:**
- Create: `app/http/uploads_rest.py`、`tests/test_uploads_rest.py`
- Modify: `app/http/__init__.py`(接线)

**Interfaces:** Consumes Task 1 `save_images`/`list_batches`。

- [ ] **Step 1: 写失败测试 `tests/test_uploads_rest.py`**(用 rest_helpers;httpx multipart)

用例:POST /api/uploads/images multipart 2 真图 + admin key → 200 {batch_id,urls,expires_at};无 key 401;非图片 400;>18 张 400。GET /uploads/{batch}/01.<ext> 免 key → 200 + 正确 content-type;不存在 404。GET /upload 免 key → 200 text/html。GET /api/uploads 带 key → 列自己批次。

- [ ] **Step 2: 确认失败** → FAIL。

- [ ] **Step 3: 实现 `app/http/uploads_rest.py`**
- `POST /api/uploads/images`:`files: list[UploadFile] = File(...)`;`operator=current_operator()`;读每个 `await f.read()` + filename → `save_images(session, operator, [(name,bytes)...], datetime.now(UTC))` → 返回 dict。
- `GET /uploads/{batch_id}/{name}`:白名单免鉴权;拼 `DATA_DIR/uploads/{batch_id}/{name}`,`is_file()` 否则 404;`FileResponse`(media_type 由扩展名推断)。**防路径穿越**:name 只允许 `^\d{2}\.(png|jpe?g|webp)$`,batch_id 只允许 token_urlsafe 字符集,否则 404。
- `GET /upload`:白名单;返回 `HTMLResponse(内联 HTML)`——apikey 输入框(password)+ 拖拽/选图 → fetch POST /api/uploads/images(Authorization: Bearer)→ 展示 batch_id+urls+复制按钮。禁 emoji,图标用内联 SVG。
- `GET /api/uploads`:`list_batches` 返 `{batches:[...]}`。
- MANIFEST_ENTRIES:POST /api/uploads/images 与 GET /api/uploads 各一条(GET /uploads/{} 与 /upload 是免鉴权静态,**不进 manifest**——manifest 只列 /api/* 鉴权端点);接进 `app/http/__init__.py`。

- [ ] **Step 4: 跑测试通过**(含 tests/test_manifest.py 防漂移仍绿)+ 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/http/uploads_rest.py app/http/__init__.py tests/test_uploads_rest.py
git commit -m "feat(uploads): 上传/取图 REST 端点 + /upload 上传页"
```

---

### Task 3: B facade 工具(可与 Task 1 并行,新文件)

**Files:**
- Create: `app/mcp_facade.py`、`tests/test_mcp_facade.py`
- Modify: `requirements.txt`(重加 fastmcp)

**Interfaces(Produces):** `mcp`(FastMCP 实例,注册 7 工具);`app/server.py`(Task 4)将 import 它。

- [ ] **Step 1: 写失败测试 `tests/test_mcp_facade.py`**

monkeypatch facade 内部的 httpx 转发函数(或用 respx 拦本机 REST),不真起服务。用例:
```
1. test_publish_note_forwards_image_urls:调 mcp.call_tool("publish_note", {...image_urls:["u1"]}) → 转发到 POST /api/publish-jobs 且 body images==["u1"];apikey 头透传
2. test_whoami/list_accounts_forward:转发到对的 GET 路径,原样回 JSON
3. test_rest_error_passthrough:REST 返 403 {"error":...} → 工具结果带回该错误(不吞)
4. test_apikey_missing:无 authorization 头(get_http_headers 空)→ 工具返回未认证错误
5. test_check_cookie_polls:monkeypatch 起检返 check_id + 轮询返 checking→valid → 工具回 valid;超时路径回 {status:checking,check_id}
```
（用 fastmcp 的 `mcp.call_tool(name, args)`;apikey 头用 monkeypatch get_http_headers 返 {"authorization":"Bearer k"} 模拟。）

- [ ] **Step 2: 确认失败** → FAIL。

- [ ] **Step 3: 实现 `app/mcp_facade.py`**
- `mcp = FastMCP("nbdpsy")`;内部 helper `_forward(method, path, *, json=None, params=None) -> dict`:
  `headers = get_http_headers(include={"authorization","x-api-key"})`;取 apikey 头(无 → 返回/抛未认证);
  `async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.API_PORT}") as c: r = await c.request(method, path, headers={转发 apikey}, json=json, params=params)`;
  非 2xx → 返回 `{"error": r.json().get("error") or r.json().get("detail") or r.text}`;2xx → `r.json()`。
- 7 个 `@mcp.tool`(whoami/list_accounts/publish_note/get_publish_status/list_publish_jobs/check_cookie/get_extension_info),每个薄调 `_forward`。publish_note 把 image_urls 映射为 body 的 images;description 写异步语义 + 写操作确认 + 图片只收 URL。check_cookie 内部轮询(起检拿 check_id → 每 ~3s 轮 GET cookie-checks 到终态,>250s 回 {status:checking,check_id})。
- `requirements.txt` 重加 `fastmcp>=3.4,<4`。

- [ ] **Step 4: 跑测试通过** + 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/mcp_facade.py tests/test_mcp_facade.py requirements.txt
git commit -m "feat(mcp): 薄 MCP facade——7 工具 httpx 自转发本机 REST(apikey 头透传)"
```

---

### Task 4: B 挂载 + 回归(串行,Task 3 合并后)

**Files:**
- Modify: `app/server.py`(combine_lifespans + mount /mcp)
- Create: `tests/test_mcp_mount.py`

**Interfaces:** Consumes Task 3 `mcp`。

- [ ] **Step 1: 写失败测试 `tests/test_mcp_mount.py`**（参照 rest_helpers 的 rest_client + 旧 test_auth_middleware 的 /mcp lifespan 用法）

```
1. test_mcp_mounted:create_app 后 app.routes 有 /mcp mount
2. test_mcp_requires_apikey:POST /mcp/ 无 key → 401(中间件)
3. test_mcp_initialize_not_421:带 admin key + initialize JSON-RPC → 状态非 421(host_origin_protection=False 生效);须在测试体内 `async with app.router.lifespan_context(app)`(combine 后 lifespan 初始化 session manager)
```

- [ ] **Step 2: 确认失败** → FAIL。

- [ ] **Step 3: 实现 `app/server.py`**（参照 `git show d3c1dc7^:app/server.py` 的挂载方式）
- 顶部 import:`from fastmcp.utilities.lifespan import combine_lifespans`、`from app.mcp_facade import mcp`。
- `mcp_app = mcp.http_app(path="/", host_origin_protection=False)`(在 create_app 内、app 构造前)。
- `app = FastAPI(title="nbdpsy-api", lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan))`。
- 末尾(include_router 循环后):`app.mount("/mcp", mcp_app)`。
- 模块 docstring 补一句"重挂薄 MCP facade(/mcp,Streamable HTTP,给 claude.ai);业务仍在 REST"。

- [ ] **Step 4: 跑测试通过** + 全量不回归(现有 test_mcp_removed 若还在——它断言 /mcp 404,本 task 重加 /mcp 会让它失败:**删除或改写** test_mcp_removed 里"/mcp 返回 404"那条,保留"app/ 无 fastmcp 直连业务逻辑"若适用;facade 是薄转发,grep fastmcp 会命中 mcp_facade,该测试语义已过时,删相应用例)。

- [ ] **Step 5: 提交**
```bash
git add app/server.py tests/test_mcp_mount.py tests/test_mcp_removed.py
git commit -m "feat(mcp): server 重挂 /mcp facade(combine_lifespans + host_origin_protection=False)"
```

---

## 合并与部署(lead)

1. Task 1、3 从 main 并行 → 各自 review → merge。
2. Task 2(依赖1)、Task 4(依赖3)从新 main 并行 → review → merge(文件不相扰:T2=uploads_rest/__init__,T4=server.py)。
3. 全并后:`alembic upgrade head`(建 upload_batches,**先于 restart**)+ `pip install -r requirements`(fastmcp 已在)+ restart。
4. 冒烟:`GET /upload` 返 HTML;`POST /api/uploads/images`(带 key,传图)→ urls;`GET /uploads/{batch}/01.png` 免 key 200;`POST /mcp/` 无 key 401、带 key initialize 非 421。
5. **静态部署项(非代码,记 DEPLOY + 告知用户)**:去 Anthropic 申请 static_headers 连接器 beta;确认 mcp.nbdpsy.com 有 A 记录(IPv4,Anthropic 出口仅 IPv4);隧道不 3xx 跳 host。
