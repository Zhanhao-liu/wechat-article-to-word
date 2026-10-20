"""把 HTML 片段渲染成 PNG（背景图卡片专用）。

为什么需要：公众号的「标题条 / 引用卡片」靠 CSS `background-image` 铺底、文字浮在上面，
Word 没有这个概念。最接近原样的做法是把这一小块**按原样截图**再当图片插进去。

实现：复用本机已有的 Chrome/Edge 无头模式，不引入 Playwright/Puppeteer。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

CARD_WIDTH = 375  # 公众号移动端正文宽度
MAX_HEIGHT = 2400

_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_chrome() -> Path | None:
    env = os.environ.get("W2D_CHROME")
    if env and Path(env).exists():
        return Path(env)
    for c in _CANDIDATES:
        if Path(c).exists():
            return Path(c)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for rel in (
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
        ):
            p = Path(local) / rel
            if p.exists():
                return p
    return None


def _page(fragment_html: str, width: int) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;background:#fff;}}
  body{{width:{width}px;font-family:-apple-system,"Microsoft YaHei",sans-serif;
        font-size:16px;line-height:1.6;color:#333;word-wrap:break-word;}}
  p{{margin:0;}}
  img{{max-width:{width}px;height:auto;}}
  table{{border-collapse:collapse;}}
  td,th{{border:1px solid #ccc;padding:2px 6px;}}
</style></head><body>{fragment_html}</body></html>"""


def _trim_whitespace(png: Path, pad: int = 2) -> None:
    """截出来上下有空白，按非白像素裁掉。"""
    try:
        from PIL import Image, ImageChops

        with Image.open(png) as im:
            rgb = im.convert("RGB")
            bg = Image.new("RGB", rgb.size, (255, 255, 255))
            bbox = ImageChops.difference(rgb, bg).getbbox()
            if not bbox:
                return
            left, top, right, bottom = bbox
            left = max(0, left - pad)
            top = max(0, top - pad)
            right = min(rgb.width, right + pad)
            bottom = min(rgb.height, bottom + pad)
            if (right - left) < 4 or (bottom - top) < 4:
                return
            rgb.crop((left, top, right, bottom)).save(png)
    except Exception:  # noqa: BLE001 - 裁剪失败就保留原图
        return


def render_fragment(
    chrome: Path,
    fragment_html: str,
    out_png: Path,
    *,
    width: int = CARD_WIDTH,
    scale: float = 2.0,
    timeout: float = 45.0,
) -> bool:
    """把一段 HTML 渲染成 PNG。成功返回 True。"""
    if not fragment_html.strip():
        return False
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if out_png.exists():
        out_png.unlink()

    page = _page(fragment_html, width)
    tmpdir = out_png.parent / "_shot_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(page.encode("utf-8")).hexdigest()[:10]
    html_path = tmpdir / f"{tag}.html"
    html_path.write_text(page, encoding="utf-8")

    # 新版 headless 必须显式指定 user-data-dir（默认 profile 目录不可写会直接退出）。
    # 注意：Chrome 会把相对路径当成自己的安装目录，这里必须用绝对路径。
    profile = (tmpdir / "profile").resolve()
    profile.mkdir(parents=True, exist_ok=True)
    out_abs = out_png.resolve()

    cmd = [
        str(chrome),
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-crashpad",
        "--disable-breakpad",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        "--default-background-color=ffffffff",
        f"--force-device-scale-factor={scale}",
        f"--window-size={width},{MAX_HEIGHT}",
        "--virtual-time-budget=3000",
        f"--screenshot={out_abs}",
        html_path.resolve().as_uri(),
    ]
    try:
        subprocess.run(
            cmd,
            check=False,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        html_path.unlink(missing_ok=True)

    if not out_png.exists() or out_png.stat().st_size < 800:
        return False
    _trim_whitespace(out_png)
    return out_png.exists() and out_png.stat().st_size > 800


def cleanup_tmp(dir_: Path) -> None:
    """清掉截图用的临时 html 与 profile 目录。"""
    import shutil

    try:
        shutil.rmtree(dir_, ignore_errors=True)
    except OSError:
        pass
