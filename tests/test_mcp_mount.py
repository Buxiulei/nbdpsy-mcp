"""MCP 挂载回归钉:server 把薄 facade(app/mcp_facade.py 的 mcp)重挂到 /mcp。

三条验证(与 Task 4 brief 对齐):
1. create_app 后 app.routes 里有 /mcp 这个 mount(挂载点存在)。
2. POST /mcp/ 不带 apikey → 401(apikey 中间件先于 MCP 传输层拦截,未认证不放行)。
3. 带 admin key + initialize JSON-RPC → 状态非 421(host_origin_protection=False 生效,
   经反代/隧道进来的公网 Host 不再被 MCP 传输层判 Misdirected Request)。

第 3 条必须在测试体内 `async with app.router.lifespan_context(app)`:combine_lifespans
组合后,lifespan 才会启动 MCP session manager 的 task group,否则 /mcp 请求会因
task group 未启动而报错。rest_client 内部已用 lifespan_context 包裹,直接复用。
"""

import json

from starlette.routing import Mount

from app.server import create_app
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

# Streamable HTTP 传输要求客户端同时接受 JSON 与 SSE,否则握手不成立。
_MCP_ACCEPT = "application/json, text/event-stream"


def test_mcp_mounted():
    """create_app 装出的 app 里存在 /mcp 这个子应用挂载点。"""
    app = create_app()
    mounts = [
        r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp"
    ]
    assert mounts, "app.routes 里未找到 /mcp mount"


async def test_mcp_requires_apikey(tmp_path, monkeypatch):
    """POST /mcp/ 不带 apikey → 401(中间件先跑,MCP 传输层拿不到无鉴权请求)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/mcp/",
            headers={"Accept": _MCP_ACCEPT},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert r.status_code == 401


async def test_mcp_initialize_not_421(tmp_path, monkeypatch):
    """带 admin key + initialize JSON-RPC → 200(非 421,host_origin_protection=False 生效)。

    421 是 MCP 传输层对公网 Host 的 DNS-rebinding 防护判定;关掉后握手应正常返回 200
    并带 mcp-session-id。这里用 rest_client(内部 async with lifespan_context)保证 MCP
    session manager 的 task group 已启动。
    """
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/mcp/",
            headers={**bearer(ADMIN_KEY), "Accept": _MCP_ACCEPT},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        assert r.status_code != 421, f"host_origin_protection 未关:{r.status_code}"
        assert r.status_code == 200
        assert r.headers.get("mcp-session-id"), "initialize 未返回 mcp-session-id"
        # 顺带确认握手体是合法 JSON-RPC 结果(SSE 流里第一段 data: 即 result)。
        payload = next(
            (
                json.loads(line[len("data:") :].strip())
                for line in r.text.splitlines()
                if line.startswith("data:")
            ),
            None,
        )
        assert payload is not None and "result" in payload
