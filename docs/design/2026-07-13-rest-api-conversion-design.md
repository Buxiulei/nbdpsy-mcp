# nbdpsy-mcp → 纯 REST API 转型设计

**日期**:2026-07-13
**决策**:彻底删除 MCP(FastMCP 依赖、`/mcp` 端点、plugin marketplace),全部能力改为 REST API,
新增单一自描述接口 `GET /api/manifest` 让 agent 一次拿到全部能力与标准。鉴权体系不变。

**背景**:MCP 架构被判定落伍;本服务的消费方是"另一台机器上的 agent",它完全可以直接调 HTTP API。
当前库里 0 个真实账号、无人经 MCP 接入生产,切换成本最低点就是现在。

---

## 1. 总体架构

- **不动**:apikey 鉴权中间件(`ApiKeyMiddleware`,白名单 `/healthz`、`/downloads`)、RBAC guards
  (`assert_account_access` / `visible_account_ids` / `require_admin`)、Fernet 加密、
  浏览器栈(Camoufox / profile_guard / atomic_tasks)、发布调度器、cookie 巡检、chrome 插件
  (插件本来就走 REST `/api/cookies/import` + `/api/accounts`)。
- **平移**:`app/tools/` 六组 24 个 MCP 工具的入口逻辑 → `app/http/` 各分组 FastAPI router。
  业务都在 services 层,工具层是薄壳,平移即换皮。
- **删除**:`app/tools/` 全目录、fastmcp 依赖(pyproject)、`server.py` 里 FastMCP 实例 /
  `MCP_INSTRUCTIONS` / `combine_lifespans` / `/mcp` 挂载、`.claude-plugin/` + `plugins/`
  (marketplace 整体废弃)。
- **新增**:`GET /api/manifest` 自描述接口 + 防漂移测试。

## 2. 端点映射(24 工具 → REST)

规则:资源名词复数;动作类用 POST 子资源;已有 5 个 REST 端点原样复用。

```
GET  /healthz                                   探活(免鉴权,已有)
GET  /api/manifest                              自描述(新;须鉴权,附 caller 身份)
GET  /api/whoami                                已有

# admin(仅 admin 角色,operator 调用 → 403)
POST   /api/operators                           create_operator(返回一次性明文 apikey)
GET    /api/operators                           list_operators
PATCH  /api/operators/{operator_id}             update_operator(name/enabled/role)
DELETE /api/operators/{operator_id}             delete_operator
POST   /api/operators/{operator_id}/rotate-apikey   rotate_operator_apikey(旧 key 立即失效)
POST   /api/operators/{operator_id}/grants      grant_account_access,body {xhs_account_id}
DELETE /api/operators/{operator_id}/grants/{xhs_account_id}   revoke_account_access
GET    /api/operators/{operator_id}/grants      list_operator_grants

# accounts(RBAC 收窄,admin 全见)
GET    /api/accounts                            已有(list_accounts)
GET    /api/accounts/{account_id}               get_account
PATCH  /api/accounts/{account_id}               update_account(name)
DELETE /api/accounts/{account_id}               delete_account
GET    /api/login/poll?since=<ISO8601>&account_id=<可选>   poll_login(登录闭环轮询)

# cookies
POST   /api/cookies/import                      已有(插件推送口 + 程序化注入,同一端点)
GET    /api/accounts/{account_id}/cookies       已有(get_cookies 解密回读)
POST   /api/accounts/{account_id}/cookie-checks check_cookies → 202 {check_id, status:"checking"}
GET    /api/cookie-checks/{check_id}            get_cookie_check(checking/valid/invalid/captcha/error)

# publish
POST   /api/publish-jobs                        publish_note → 202 {job_id, status:"pending"}
GET    /api/publish-jobs/{job_id}               get_publish_status
GET    /api/publish-jobs?account_id=&status=&limit=   list_publish_jobs
POST   /api/publish-jobs/{job_id}/cancel        cancel_publish_job → {ok} 或 {ok:false, status}

# extension
GET    /api/extension                           get_extension_download(server_time+下载 URL+安装步骤)
GET    /downloads/*                             已有(插件包下载,免鉴权)
```

**语义原样保留**(不是重新设计,是换入口):

- 发布/巡检的异步契约(投递即回 id,轮询到终态)、重试退避、同号串行;
- 图片三形态(URL / data URI / `{b64, ext}`)、1–18 张校验、标题≤20/正文≤900/话题≤10 静默截断;
- schedule_time 时区规则(不带偏移按 UTC);
- poll_login 的 since/account_id 判据;
- import_cookies 的 user_id upsert 去重 + 新号自动给导入者建 access;
- check 的 error 态不写回 cookie_status。

## 3. 错误契约(统一)

响应体一律 `{"error": "<中文原因>"}`:

| HTTP | 场景 | 来源 |
|---|---|---|
| 400 | 入参非法(图片张数越界、status 枚举错、cookies_json 格式错、since 非 ISO8601 等) | `ValueError` |
| 401 | 无/错 apikey、operator 被停用 | `AuthError`(中间件已有) |
| 403 | 越权访问账号 / 非 admin 调管理端点 | `AccessDenied`(已有) |
| 404 | 资源不存在(账号/任务/运营者/check_id) | 原工具的"不存在"ValueError **升级为真 404**(新 `NotFoundError` 或 router 内显式判) |
| 500 | 未预期异常 | 不泄栈,日志留全 |

实现:在 `app/auth/context.py` 旁新增专用异常 `NotFoundError`,app 级挂
`ValueError → 400` 与 `NotFoundError → 404` 两个 handler;router/services 里所有
"××不存在"一律改抛 `NotFoundError`(全仓统一,不允许一部分走 400)。

## 4. `GET /api/manifest` 自描述接口

**目标**:另一个 agent 带 apikey 调这一个接口,即获得上手所需的全部信息,无需读任何外部文档。

**须鉴权**:顺带完成 key 验证,并在响应里返回 caller 身份与可操作账号摘要——一步顶三步
(验 key + whoami + 读文档)。

响应结构(JSON):

```jsonc
{
  "service": "nbdpsy-api",
  "version": "<语义版本>",
  "description": "<一段话:小红书运营能力后台(自动发布/多账号/cookie/远程登录)>",
  "base_url": "https://mcp.nbdpsy.com",
  "auth": { "scheme": "Authorization: Bearer <apikey> 或 X-API-Key", "errors": "401=key 错/停用, 403=越权" },
  "caller": { "operator_id": 1, "name": "...", "role": "admin|operator", "account_count": N },
  "workflows": [ "<典型编排叙事:接入自检 → 登录闭环 → cookie 巡检 → 发布轮询,即原 MCP_INSTRUCTIONS 的使用要点/硬约束/登录闭环协议,逐条平移>" ],
  "constraints": [ "<发布硬约束速览:仅图文/1-18 张/截断规则/时区规则>" ],
  "endpoints": [
    {
      "method": "POST", "path": "/api/publish-jobs",
      "summary": "<一句话>",
      "admin_only": false,
      "params": { "<body/query/path 参数名>": "<类型 + 语义 + 约束>" },
      "returns": "<返回体说明>",
      "errors": "<该端点特有错误>",
      "notes": "<异步契约/轮询节奏/易踩坑,即原工具 docstring 的浓缩>"
    }
    // ... 全部 /api/* 端点
  ]
}
```

**防漂移**(核心质量闸):manifest 的 endpoints 集合是手工策展的(为了叙事质量),但用测试
钉死一致性——遍历 FastAPI 实际注册的 `/api/*` 路由(method+path),与 manifest 声明的集合
做**双向全等断言**:漏写、多写、改路径没同步,测试直接红。

## 5. 删除与文档重写

- 删 `app/tools/`、fastmcp 依赖、`.claude-plugin/`、`plugins/`;`server.py` 去掉
  FastMCP/combine_lifespans(lifespan 直接给 FastAPI)。
- `MCP_INSTRUCTIONS` 的内容**不丢**:全部迁进 manifest 的 workflows/constraints/endpoints.notes。
- README 重写接入章节:不再有"安装 MCP/插件 marketplace",改为
  「拿 apikey → `curl /healthz` → `GET /api/manifest` → 照 manifest 干活」。
- `docs/onboarding/operator-config-package.md` 重写:配置包 = base URL + apikey +
  "第一步调 `GET /api/manifest`",Claude Code / 任意 HTTP agent 通吃,零客户端安装。
  管理侧流程(create_operator → grant → 填模板)改为对应 REST 调用。
- 服务名对外表述从"MCP 后台"改为"API 后台"(仓库名 nbdpsy-mcp 不改,域名不改)。

## 6. 测试策略

- 现有 24 个工具的测试(`test_*_tools.py` 等)**平移为 httpx ASGI REST 测试**:同样的场景矩阵
  (鉴权/越权/入参校验/异步轮询/RBAC 收窄),断言从"工具返回 dict"改为"HTTP code + JSON body"。
- 新增:manifest 防漂移测试、manifest 内容测试(caller 字段随身份变化、operator 调 admin 端点 403)。
- 收口判据:全套测试绿 + 公网真机验证(healthz / manifest 200 / 无 key 401 / operator 越权 403 /
  `/mcp/` 返回 404)。

## 7. 实施方式(多 agent workflow)

subagent-driven-development,每 task 独立 worktree + opus review + fix:

- **并行批**(互不相扰的 router 文件,各自带测试平移):
  1. manifest 基础设施 + 错误 handler(400/404)+ system 组
  2. admin router(8 端点)
  3. accounts + login poll router(在既有 accounts_rest.py 上扩)
  4. cookies router(check 异步对)
  5. publish router(4 端点)
- **串行收口**:删 MCP/marketplace、server.py 精简、manifest endpoints 汇总 + 防漂移测试、
  README/onboarding 重写。
- 部署:merge → systemd restart → 公网验证清单(见 §6)。

## 8. 明确不做(YAGNI)

- 不加 `/api/v1` 路径版本(manifest 里带 version 字段即可,消费方是自家 agent);
- 不做 OpenAPI 增强/SDK 生成;
- 不改鉴权模型、不加新能力;
- 不动 chrome 插件(接口全兼容)。
