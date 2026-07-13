"""GET /api/manifest 自描述接口 + 防漂移测试。"""

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
    """防漂移:manifest 声明的端点集合与实际注册的 /api/* 路由双向全等。

    FastAPI 0.139 起 include_router 变为惰性挂载(app.routes 里是私有
    _IncludedRouter 节点而非平铺的 APIRoute),故经公开的 app.openapi()
    展平取实际端点集合——同时天然覆盖 server.py 内联注册的端点。
    """
    from app.http import ALL_MANIFEST_ENTRIES
    from app.server import create_app

    _HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    app = create_app()
    actual = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in ops
        if method.upper() in _HTTP_METHODS
    }
    declared = {(e["method"], e["path"]) for e in ALL_MANIFEST_ENTRIES}
    assert actual == declared, (
        f"manifest 漏写: {sorted(actual - declared)}; 多写: {sorted(declared - actual)}"
    )
