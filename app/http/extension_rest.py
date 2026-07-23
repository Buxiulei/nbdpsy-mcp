"""GET /api/extension —— 把插件包信息递给操作者(远程登录闭环起点)。

远程 agent 调此端点,拿到下载地址 + 版本 + 安装步骤 + apikey 引导语,即可指导操作者
把"装好即用"的 chrome 插件装上并连回本服务。

关键:库里只存 apikey 的 hash,拿不到明文,故本端点不返回明文 apikey,只返回引导语——
让操作者填"连接本服务的同一把 apikey";忘了就由管理员 rotate_operator_apikey 重置。

平移自 app/tools/extension.py 的 get_extension_download 工具(常量 + 逻辑原样搬)。
"""

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from app import __version__
from app.core.config import settings

router = APIRouter()

# 插件 manifest.json 路径:extension_version 的单一事实源(与打进 zip 的是同一份)。
_MANIFEST_PATH = Path(__file__).resolve().parent.parent.parent / (
    "chrome-extension/manifest.json"
)


def _read_extension_version() -> str:
    """从 chrome-extension/manifest.json 读插件真实版本(如 2.1.2)。

    历史混淆:本端点的 ``version`` 字段是 **server 版本**(app.__version__,v0.6.x),
    不是插件版本——排障判断"运营装的插件是否旧版"必须比对插件版本。读不到时返回
    "unknown"(不抛错,不因插件目录缺失拖垮登录闭环起点)。
    """
    try:
        return str(json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))["version"])
    except Exception:
        return "unknown"

# apikey 引导语:不回传明文(库内只存 hash),引导操作者复用连接本服务的同一把 key。
_APIKEY_HINT = (
    "在插件里填入你连接本服务的同一把 apikey(创建/轮换 operator 时一次性显示的那串);"
    "忘了就让管理员用 rotate_operator_apikey 重置后重新下发。"
)

# 中文安装步骤:下载 → 解压 → 开发者模式加载 → 填配置 → 无痕模式需手动勾选启用。
_INSTALL_STEPS = [
    "下载插件包:点击 download_url 下载 extension.zip。",
    "解压 extension.zip 到一个固定目录(不要放临时目录,重装后目录还在才不用重加载)。",
    "打开 chrome://extensions,右上角开启「开发者模式」。",
    "点「加载已解压的扩展程序」,选中上一步解压出来的目录。",
    "打开插件弹窗,填入 serverUrl(本服务地址)与 apikey(见 apikey_hint)。",
    "若要在无痕模式使用,进插件详情页勾选「在无痕模式下启用」。",
]

MANIFEST_ENTRIES = [{
    "method": "GET", "path": "/api/extension",
    "summary": "返回 chrome 插件包下载地址、版本、安装步骤、apikey 引导语与服务端当前时间",
    "admin_only": False, "params": {},
    "returns": "{download_url, version, extension_version, apikey_hint, install_steps, server_time}",
    "errors": "",
    "notes": "登录闭环起点:记下 server_time 作为 /api/login/poll 的 since 起点;"
             "download_url 免鉴权可直接递给操作者。注意:version=服务端版本(v0.6.x),"
             "extension_version=插件真实版本(如 2.1.2)——判断运营插件是否旧版看后者,"
             "与 chrome://extensions 页面显示的版本号比对。",
}]


@router.get("/api/extension")
async def get_extension_endpoint() -> dict:
    """返回 chrome 插件包下载地址、版本、安装步骤、apikey 引导语与服务端当前时间。

    download_url 指向白名单放行的 /downloads/extension.zip(无需 apikey 即可下载);
    apikey_hint 是引导语而非明文 key(库内只存 hash,无法回取)。
    server_time 是服务端当前时间(naive UTC 的 ISO 串):**拿它做 /api/login/poll?since=...
    的起点**——发插件给操作者扫码登录前记下 server_time,登录发起后用它当基准轮询
    /api/login/poll,直到检测到新号/该号登录时间刷新(避免用客户端本地时钟错判早/晚)。
    """
    # 带 zip mtime 做 cache-buster：每次重打包链接即变，绕开 CDN 边缘缓存拿最新包。
    zip_path = Path(settings.DATA_DIR) / "extension.zip"
    buster = int(zip_path.stat().st_mtime) if zip_path.is_file() else 0
    return {
        "download_url": f"{settings.PUBLIC_BASE_URL}/downloads/extension.zip?t={buster}",
        "version": __version__,                       # 服务端版本(历史字段,语义保持不变)
        "extension_version": _read_extension_version(),  # 插件真实版本(排障比对用这个)
        "apikey_hint": _APIKEY_HINT,
        "install_steps": _INSTALL_STEPS,
        "server_time": datetime.utcnow().isoformat(),
    }
