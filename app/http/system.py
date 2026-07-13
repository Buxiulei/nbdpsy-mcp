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
