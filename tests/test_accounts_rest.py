"""GET /api/accounts + GET /api/accounts/{id}/cookies 端点测试:插件"我的账号"列表 + 注入用解密 cookie。

隔离手法与 test_cookies_import_http 一致:patch app.core.db 模块级 engine/async_session 指向
tmp sqlite,patch settings.ROOT_ADMIN_APIKEY,用真实 lifespan 驱动 init_db + bootstrap_admin
(root admin 拿到明文 apikey 做 Bearer 头)。

覆盖(brief 必测):
- GET /api/accounts 带 apikey → 200 且返回该运营者可见的号(admin 全见);无 apikey → 401。
- 造 operator + 两个号只 grant 一个 → 该 operator 的 /api/accounts 只见被 grant 的号(RBAC)。
- GET /api/accounts/{id}/cookies 有 access → 200 返回解密 cookies;无 access → 403;无 apikey → 401。
- 账号列表返回体绝不含 login_cookies(明文/密文)。
"""

import app.core.db as db_module
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    make_operator as _make_operator,
    rest_client as isolated_client,
    seed_account as _seed_account,
)


# ---------------- GET /api/accounts ----------------


async def test_list_accounts_admin_sees_all(tmp_path, monkeypatch):
    """带合法 apikey(admin)GET /api/accounts → 200,全见已入库的号;返回体不含 cookie。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        await _seed_account("号A", "uA", [{"name": "a1", "value": "x"}])
        await _seed_account("号B", "uB", [{"name": "a1", "value": "y"}])

        r = await c.get(
            "/api/accounts", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert r.status_code == 200, r.text
        accounts = r.json()["accounts"]
        names = {a["name"] for a in accounts}
        assert names == {"号A", "号B"}
        # 列表视图绝不含 login_cookies(明文/密文)
        assert all("login_cookies" not in a for a in accounts)


async def test_list_accounts_without_apikey_401(tmp_path, monkeypatch):
    """无 apikey GET /api/accounts → 401(中间件挡,不进业务层)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        r = await c.get("/api/accounts")
        assert r.status_code == 401


async def test_list_accounts_invalid_apikey_401(tmp_path, monkeypatch):
    """带一个库里不存在的 Bearer token GET /api/accounts → 401
    (覆盖中间件"apikey 存在但查不到 operator"分支,即 op is None,与缺失 apikey 是不同分支)。
    """
    async with isolated_client(tmp_path, monkeypatch) as c:
        r = await c.get(
            "/api/accounts",
            headers={"Authorization": "Bearer this-apikey-does-not-exist-in-db"},
        )
        assert r.status_code == 401


async def test_list_accounts_operator_sees_only_granted(tmp_path, monkeypatch):
    """非 admin operator 只见被 grant 的号(RBAC 收窄)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc1 = await _seed_account("号1", "u1", [{"name": "a1", "value": "x"}])
        await _seed_account("号2", "u2", [{"name": "a1", "value": "y"}])

        op_key = "operator-plain-key-rest-scope-01"
        op_id = await _make_operator(op_key)
        # 只授权 acc1
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc1, op_id)

        r = await c.get(
            "/api/accounts", headers={"Authorization": f"Bearer {op_key}"}
        )
        assert r.status_code == 200, r.text
        got = {a["id"] for a in r.json()["accounts"]}
        assert got == {acc1}


# ---------------- GET /api/accounts/{id}/cookies ----------------


async def test_get_cookies_with_access_returns_decrypted(tmp_path, monkeypatch):
    """admin 有 access:GET /api/accounts/{id}/cookies → 200 返回解密 cookies。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account(
            "号C", "uC", [{"name": "a1", "value": "秘", "sameSite": "lax"}]
        )
        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == acc
        cookies = body["cookies"]
        assert cookies[0]["name"] == "a1"
        assert cookies[0]["value"] == "秘"


async def test_get_cookies_operator_with_access_returns_decrypted(
    tmp_path, monkeypatch
):
    """operator 有 access:自己的 apikey GET /api/accounts/{id}/cookies → 200 返回解密 cookies
    (现有测试只覆盖 admin 正向 + operator 负向,这里补 operator 正向)。
    """
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account(
            "号F", "uF", [{"name": "a1", "value": "秘F", "sameSite": "lax"}]
        )
        op_key = "operator-plain-key-rest-access-ok-01"
        op_id = await _make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc, op_id)

        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {op_key}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == acc
        cookies = body["cookies"]
        assert cookies[0]["name"] == "a1"
        assert cookies[0]["value"] == "秘F"


async def test_get_cookies_without_access_403(tmp_path, monkeypatch):
    """无 access 的 operator GET /api/accounts/{id}/cookies → 403(AccessDenied 映射)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号D", "uD", [{"name": "a1", "value": "x"}])
        op_key = "operator-plain-key-rest-noaccess-1"
        await _make_operator(op_key)  # 不授权任何号

        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {op_key}"},
        )
        assert r.status_code == 403


async def test_get_cookies_without_apikey_401(tmp_path, monkeypatch):
    """无 apikey GET /api/accounts/{id}/cookies → 401(中间件挡)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号E", "uE", [{"name": "a1", "value": "x"}])
        r = await c.get(f"/api/accounts/{acc}/cookies")
        assert r.status_code == 401
