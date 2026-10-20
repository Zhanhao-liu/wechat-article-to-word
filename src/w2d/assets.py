"""图片与卡片资源层：下载、截图、压缩、命名，并记录到 Article.assets。

铁律：docx 里只允许嵌入本地文件，绝不允许外链图片。
      （导出后「图片全红叉」的第一大原因就是 docx 里存了外链）
"""

from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from . import shot
from .model import Article, Asset

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# 图片域名白名单：只下载微信/腾讯的图，避免被当成通用下载器滥用
ALLOWED_IMG_HOSTS = ("mmbiz.qpic.cn", "mmbiz.qlogo.cn", "res.wx.qq.com", "wx.qlogo.cn")

# mmbiz 的防盗链策略随时段/域名波动，两种都试，先空 Referer
REFERER_STRATEGIES = (
    ("none", {}),
    ("wechat", {"Referer": "https://mp.weixin.qq.com/"}),
)

MAX_BYTES = 20 * 1024 * 1024
MIN_BYTES = 512

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def _ext_from_content_type(ct: str) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
    }.get(ct, ".png")


def _is_allowed(url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_IMG_HOSTS)


# ---------------------------------------------------------------- 卡片转图
def materialize_cards(
    article: Article,
    folder: Path,
    *,
    enable: bool = True,
    chrome: Path | None = None,
) -> None:
    """把背景图卡片整块截图成 PNG，成功后改成 image block。

    截图失败不报错：保留 card block，渲染时退化成「背景图 + 文字」。
    """
    cards = [b for b in article.blocks if b.kind == "card"]
    if not cards:
        return
    assets_dir = folder / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    if not enable:
        article.warnings.append(f"{len(cards)} 个背景图卡片未截图（--no-screenshot），降级为「背景图 + 文字」")
        return
    chrome = chrome or shot.find_chrome()
    if chrome is None:
        article.warnings.append(f"{len(cards)} 个背景图卡片未截图（未找到 Chrome/Edge），降级为「背景图 + 文字」")
        return

    ok = fail = 0
    for n, b in enumerate(cards, start=1):
        out = assets_dir / f"card_{n:03d}.png"
        if b.html and shot.render_fragment(chrome, b.html, out):
            b.kind = "image"
            b.asset = f"assets/{out.name}"
            b.html = None
            b.width_px = None
            ok += 1
        else:
            fail += 1
    shot.cleanup_tmp(assets_dir / "_shot_tmp")
    if ok:
        article.warnings.append(f"{ok} 个背景图卡片已整块截图，作为图片插入")
    if fail:
        article.warnings.append(f"{fail} 个背景图卡片截图失败，降级为「背景图 + 文字」")


# ---------------------------------------------------------------- 下载
def _download_one(url: str) -> tuple[bytes, str, str] | None:
    """按 Referer 策略逐个尝试；返回 (bytes, ext, strategy) 或 None。"""
    for name, headers in REFERER_STRATEGIES:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=True, trust_env=False
            ) as c:
                r = c.get(url, headers={"User-Agent": UA, **headers})
            if r.status_code != 200:
                continue
            data = r.content
            ct = r.headers.get("content-type", "")
            if not ct.startswith("image/"):
                continue
            if not (MIN_BYTES <= len(data) <= MAX_BYTES):
                continue
            return data, _ext_from_content_type(ct), name
        except httpx.HTTPError:
            continue
    return None


def _to_insertable(data: bytes, ext: str) -> tuple[bytes, str]:
    """Word 对 webp 支持很差，统一转 png；其余原样保留。"""
    if ext != ".webp":
        return data, ext
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
            return buf.getvalue(), ".png"
    except Exception:  # noqa: BLE001
        return data, ext


def _compress(path: Path, max_edge: int, quality: int) -> Path:
    """压缩档：长边超限就缩放；GIF/webp 转静态图（Word 里动不了，又占体积）。

    失败时一定返回原路径，且不留半截文件 —— 宁可不压缩，也不能把图搞坏。
    """
    target: Path | None = None
    try:
        from PIL import Image

        # Pillow 10+ 把重采样常量搬到了 Image.Resampling；13 起 Image.LANCZOS 已移除
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
            dynamic = fmt in ("GIF", "WEBP") or path.suffix.lower() in (".gif", ".webp")
            too_big = max(w, h) > max_edge
            # 大 PNG 如果没透明通道，转 JPEG 通常能省一半以上；有 alpha 就保留 PNG
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            heavy_png = (
                fmt == "PNG" and not has_alpha and path.stat().st_size > 400 * 1024
            )
            if not dynamic and not too_big and not heavy_png:
                return path
            if too_big and resample is not None:
                ratio = max_edge / max(w, h)
                im = im.resize(
                    (max(1, int(w * ratio)), max(1, int(h * ratio))), resample
                )
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            target = path.with_suffix(".png" if has_alpha else ".jpg")
            if has_alpha:
                im.convert("RGBA").save(target, "PNG", optimize=True)
            else:
                im.convert("RGB").save(target, "JPEG", quality=quality, optimize=True)
        if target != path:
            path.unlink(missing_ok=True)
        return target
    except Exception:  # noqa: BLE001 - 压缩失败就用原图，并清掉半截目标文件
        if target is not None and target != path:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        return path


def download_all(
    article: Article,
    assets_dir: Path,
    *,
    workers: int = 4,
    compress: bool = False,
    max_edge: int = 1600,
    quality: int = 85,
) -> None:
    """把 blocks 里的图片 URL 换成本地文件名，并填充 article.assets。"""
    assets_dir.mkdir(parents=True, exist_ok=True)

    targets = ("image", "card")
    urls: list[str] = []
    for b in article.blocks:
        if b.kind in targets and b.asset and b.asset.startswith("http"):
            if b.asset not in urls:
                urls.append(b.asset)
    if not urls:
        return

    plan = [(u, assets_dir / f"img_{i:03d}") for i, u in enumerate(urls, start=1)]

    def work(item):
        url, stem = item
        if not _is_allowed(url):
            return url, None, stem, "域名不在白名单（只下载微信/腾讯图床）"
        got = _download_one(url)
        if not got:
            return url, None, stem, "下载失败：两种 Referer 策略均未拿到有效图片"
        return url, got, stem, None

    results: dict[str, tuple] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, got, stem, reason in pool.map(work, plan):
            results[url] = (got, stem, reason)

    url_to_file: dict[str, str] = {}
    compressed_count = 0
    compressed_before = 0
    compressed_after = 0
    for i, (url, (got, stem, reason)) in enumerate(results.items(), start=1):
        asset_id = f"img_{i:03d}"
        if got is None:
            article.assets.append(
                Asset(id=asset_id, src_url=url, file="", status="failed", reason=reason)
            )
            continue
        data, ext, strategy = got
        data, ext = _to_insertable(data, ext)
        path = stem.with_suffix(ext)
        path.write_bytes(data)
        if compress:
            before_bytes = len(data)
            path = _compress(path, max_edge, quality)
            data = path.read_bytes()
            if len(data) < before_bytes:
                compressed_count += 1
                compressed_before += before_bytes
                compressed_after += len(data)
        rel = f"assets/{path.name}"
        article.assets.append(
            Asset(
                id=asset_id,
                src_url=url,
                file=rel,
                status="ok",
                bytes=len(data),
                mime=MIME_BY_EXT.get(path.suffix.lower(), "image/png"),
                sha1=hashlib.sha1(data).hexdigest(),
                referer_strategy=strategy,
            )
        )
        url_to_file[url] = rel

    failed = [a for a in article.assets if a.status == "failed"]
    if failed:
        article.warnings.append(f"{len(failed)}/{len(article.assets)} 张图片下载失败，已用文字占位")
    if compressed_count:
        article.warnings.append(
            f"已压缩 {compressed_count} 张图片："
            f"{compressed_before / 1048576:.2f} MB → {compressed_after / 1048576:.2f} MB"
        )
    gifs = [a for a in article.assets if (a.file or "").lower().endswith(".gif")]
    if gifs:
        article.warnings.append(
            f"{len(gifs)} 张 GIF 动图在 Word 里只会显示首帧（文件仍是原 GIF，加 --compress 可转静态图）"
        )

    for b in article.blocks:
        if b.kind in targets and b.asset in url_to_file:
            b.asset = url_to_file[b.asset]

    _remove_orphans(assets_dir, article)


def _remove_orphans(assets_dir: Path, article: Article) -> None:
    """清掉没人引用的残留图片（重跑 --force 换格式后会留下孤儿文件）。"""
    keep = {Path(a.file).name for a in article.assets if a.file}
    keep |= {Path(b.asset).name for b in article.blocks if b.asset and b.asset.startswith("assets/")}
    for p in assets_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() in MIME_BY_EXT and p.name not in keep:
            p.unlink(missing_ok=True)
