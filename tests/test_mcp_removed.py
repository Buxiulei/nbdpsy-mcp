"""MCP 端点回归钉:/mcp 目前尚未挂载(Task 4 重挂薄 facade 后本用例由 Task 4 处理)。

注:原 test_no_fastmcp_import_in_app 已删除——薄 MCP facade(app/mcp_facade.py)必然 import
fastmcp,"app/ 不引用 fastmcp"的旧断言在重加 facade 后失效。facade 是薄转发(非旧的 MCP
直连业务逻辑),与 d3c1dc7 删除的那套语义不同。
"""

from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


async def test_mcp_endpoint_gone(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/mcp/", headers=bearer(ADMIN_KEY), json={})
        assert r.status_code == 404
