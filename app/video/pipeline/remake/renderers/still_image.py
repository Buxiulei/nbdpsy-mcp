"""卡片段渲染：品牌 HTML 模板 → Playwright 截图 → ffmpeg 图转视频段（spec §6）。

模板占位符用 __NAME__ + str.replace（CSS 花括号与 str.format 冲突）；
全片右下角 logo 统一由 muxer 水印层叠加，卡片模板不再烘 logo（防双 logo 重影）。
"""
import asyncio
import html as html_escape
import time
from pathlib import Path

from app.video.pipeline.muxer import _run_ffmpeg
from app.video.pipeline.remake import style

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_FADE_S = 0.5


def _fill_template(scene: dict) -> str:
    content = scene.get("content") or {}
    name = "title_card.html" if scene.get("type") == "title_card" else "text_card.html"
    tpl = (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return (tpl
            .replace("__CARD_BG__", style.CARD_BG)
            .replace("__GOLD__", style.GOLD)
            .replace("__BURGUNDY__", style.BURGUNDY)
            .replace("__CARD_TEXT__", style.CARD_TEXT)
            .replace("__FONT__", style.FONT_FAMILY)
            .replace("__TITLE__", html_escape.escape(content.get("title", "")))
            .replace("__BODY__", html_escape.escape(content.get("body", ""))))


async def _screenshot(html: str, out_png: Path, deadline: float | None = None) -> Path:
    """headless chromium 截 1920x1080（复用宿主装好的 playwright chromium）。

    平移适配：源用 app/utils/playwright_guard 的 guarded_chromium/guarded_step 护栏；宿主
    nbdpsy-server 无该公共件（源里它服务 4 个截图点，本宿主仅本处一个消费方，整体搬入
    100+ 行强杀机器属过度设计）。故就地实现同语义的有界护栏：

    playwright 的 start()/launch()/new_page() 一个 timeout 参数都没有，是「起浏览器→截图→
    关掉」链路上仅有的能永久挂死的 await（挂死不抛异常、except 接不住，会把整个 job 拖死）。
    用「总 deadline + 每步取剩余」把这些步骤逐个有界，无论卡在哪一步墙钟都硬封顶在 timeout_s。
    整个浏览器阶段共享 BROWSER_SHOT_TIMEOUT 预算，调用方给了 deadline 就取两者较小值。

    playwright 异步 API 本身非阻塞（全 awaitable），无需 to_thread（scheduler 非阻塞红线）。
    """
    from playwright.async_api import async_playwright

    from app.core.config import settings

    timeout_s = float(getattr(settings, "BROWSER_SHOT_TIMEOUT", 20))
    if deadline is not None:
        timeout_s = min(timeout_s, deadline - time.monotonic())
    dl = time.monotonic() + max(0.0, timeout_s)

    async def _step(coro, name: str):
        """按剩余总预算 await 一步，超时统一转成带步骤名的 TimeoutError（源 guarded_step 同语义）。"""
        remaining = max(0.0, dl - time.monotonic())
        try:
            return await asyncio.wait_for(coro, timeout=remaining)
        except asyncio.TimeoutError:
            raise TimeoutError(f"still_image timeout at {name}") from None

    pw = await _step(async_playwright().start(), "playwright.start")
    browser = None
    try:
        browser = await _step(pw.chromium.launch(headless=True), "chromium.launch")
        page = await _step(
            browser.new_page(viewport={"width": style.VIDEO_W, "height": style.VIDEO_H}),
            "new_page")
        await _step(page.set_content(html, wait_until="networkidle"), "set_content")
        await _step(page.screenshot(path=str(out_png), full_page=False), "screenshot")
    finally:
        # 收尾同样有界，防 close/stop 本身挂死拖垮 job（源护栏对收尾也有超时）。
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            await asyncio.wait_for(pw.stop(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass
    return out_png


async def render(scene: dict, out_path: Path, *,
                 deadline: float | None = None) -> Path:
    """卡片场景 → 无声视频段：截图静态图 + 首尾 fade（统一输出规格）。"""
    duration = float(scene["t1"]) - float(scene["t0"])
    png = Path(out_path).with_suffix(".card.png")
    try:
        await _screenshot(_fill_template(scene), png, deadline)
        fade_out_start = max(0.0, duration - _FADE_S)
        timeout = 600.0
        if deadline is not None:
            timeout = max(60.0, min(timeout, deadline - time.monotonic()))
        await _run_ffmpeg([
            "-loop", "1", "-t", f"{duration}", "-i", str(png),
            "-vf", (f"scale={style.VIDEO_W}:{style.VIDEO_H},"
                    f"fade=t=in:st=0:d={_FADE_S},"
                    f"fade=t=out:st={fade_out_start}:d={_FADE_S},fps={style.FPS}"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", str(out_path),
        ], timeout=timeout)
    finally:
        png.unlink(missing_ok=True)            # 渲染完/失败都清理临时截图
    return Path(out_path)
