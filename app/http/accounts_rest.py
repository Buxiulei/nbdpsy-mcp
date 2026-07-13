"""GET /api/accounts + GET /api/accounts/{id}/cookies —— 插件"我的账号"列表 + 注入用解密 cookie。

两端点均不在中间件白名单(白名单仅 /healthz、/downloads)→ 走 apikey 中间件校验后,端点内
current_operator() 即当前运营者;RBAC 复用服务层:
- list_accounts 本就按 visible_account_ids 收窄(admin 全见,operator 仅其被 grant 的号);
- get_cookies 内部 assert_account_access,无权抛 AccessDenied → server.py 的全局 handler 映 403。

/api/accounts 返回体复用 account_service.account_view(与 accounts 分组 MCP 工具同一视图,
**不含 login_cookies**);/api/accounts/{id}/cookies 返回解密 cookie,专供插件注入无痕窗口。
"""

from fastapi import APIRouter

from app.auth.context import current_operator
from app.core.db import get_session
from app.services import account_service, cookie_service

router = APIRouter()

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


@router.get("/api/accounts")
async def list_accounts_endpoint() -> dict:
    """列出当前运营者可见的小红书账号(admin 全见;不含 cookie),供插件"我的账号"列表渲染。"""
    operator = current_operator()
    async with get_session() as session:
        accounts = await account_service.list_accounts(session, operator)
        return {"accounts": [account_service.account_view(a) for a in accounts]}


@router.get("/api/accounts/{account_id}/cookies")
async def get_account_cookies_endpoint(account_id: int) -> dict:
    """取某号解密 cookie 供插件注入无痕窗口;无 access → AccessDenied(全局 handler 映 403)。"""
    operator = current_operator()
    async with get_session() as session:
        cookies = await cookie_service.get_cookies(session, operator, account_id)
        return {"account_id": account_id, "cookies": cookies}
