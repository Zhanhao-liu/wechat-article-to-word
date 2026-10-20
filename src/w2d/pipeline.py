"""编排层：一次导出的完整流程 + 产物清单（manifest）。

流程：抓取 → 解析归一化 → 图片本地化 → 渲染 → 写 manifest
设计：幂等（同 URL 同内容直接跳过）、断点（中间产物落盘）、可审计（哈希+告警）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import assets as assets_mod
from . import fetcher, parser, render
from .model import Article

TOOL_VERSION = "0.1.0"
CST = None  # 由 fetcher 层负责时区，这里只记录字符串

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _publish_date(publish_time: str) -> str:
    """用文章发布日期做目录名的一部分（不是导出日期，否则跨天重跑会生成两份）。"""
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", publish_time or "")
    if m:
        y, mo, d = m.groups()
        return f"{y}{int(mo):02d}{int(d):02d}"
    return datetime.now().strftime("%Y%m%d")


def slugify(title: str, url: str, publish_time: str = "", max_len: int = 60) -> str:
    """生成安全目录名：去非法字符、压缩空白、限长、加发布日期与短哈希。"""
    base = INVALID_CHARS.sub(" ", title or "untitled")
    base = re.sub(r"\s+", " ", base).strip()
    base = base.replace("　", " ")
    if len(base) > max_len:
        base = base[:max_len].rstrip()
    short = hashlib.sha256(url.encode()).hexdigest()[:6]
    return f"{base or 'untitled'}_{_publish_date(publish_time)}_{short}"


def find_existing(out_root: Path, identity: str) -> tuple[Path, dict] | None:
    """按文章身份在输出目录里找已有产物。

    幂等必须靠「文章身份」，不能靠目录名 —— 目录名里带日期/标题，
    跨天或标题微调都会变，导致同一篇重复导出。
    """
    if not out_root.exists():
        return None
    for mf in out_root.glob("*/manifest.json"):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("identity") == identity:
            return mf.parent, data
    return None

FORMAT_FILE = {"docx": "article.docx", "html": "article.html", "json": "article.json"}


def article_identity(url: str) -> str:
    """文章身份：用 URL 的 host+path，不用整页 HTML 哈希。

    注意：公众号页面每次请求都带随机 nonce/时间戳，整页 HTML 的 sha256 必然不同，
    拿它当幂等键会导致「每次都重跑」。URL 路径（/s/xxxx）才是稳定身份。
    """
    from urllib.parse import urlparse

    p = urlparse(url)
    return f"{p.netloc}{p.path}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(
    url: str | None,
    *,
    out_root: Path,
    formats: tuple[str, ...] = ("docx", "html", "json"),
    force: bool = False,
    input_html: Path | None = None,
    source_url: str | None = None,
    verbose: bool = True,
    compress: bool = False,
    screenshot: bool = True,
    max_edge: int = 1600,
) -> dict:
    """导出一篇文章；返回结果字典（含产物路径与告警）。"""
    t_start = time.perf_counter()
    timings: dict[str, int] = {}
    url = url or source_url
    if not url and not input_html:
        raise fetcher.FetchError("必须提供文章 URL，或用 --input-html 指定本地 HTML")

    # ---------------- 抓取
    t0 = time.perf_counter()
    if input_html:
        raw = input_html.read_text(encoding="utf-8", errors="replace")
        channel = "html"
        sha = hashlib.sha256(raw.encode()).hexdigest()
        final_url = source_url or ""
    else:
        res = fetcher.fetch(url)
        raw, channel, sha, final_url = res.text, res.channel, res.sha256, res.final_url
    timings["fetch_ms"] = int((time.perf_counter() - t0) * 1000)

    # ---------------- 解析
    t0 = time.perf_counter()
    article = parser.parse(
        raw,
        url=final_url or url,
        channel=channel,
        sha256=sha,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )
    timings["parse_ms"] = int((time.perf_counter() - t0) * 1000)

    # ---------------- 产物目录
    identity = article_identity(final_url or url)
    existing = find_existing(out_root, identity)
    folder = (
        existing[0]
        if existing
        else out_root / slugify(article.title, final_url or url, article.publish_time)
    )
    manifest_path = folder / "manifest.json"

    if existing and not force:
        old = existing[1]
        have = {o.get("file") for o in old.get("outputs", [])}
        need = {FORMAT_FILE[f] for f in formats if f in FORMAT_FILE}
        if need <= have and all((folder / f).exists() for f in need):
            return {
                "ok": True,
                "status": "existing",
                "skipped": True,
                "dir": str(folder),
                "title": article.title,
                "message": "该文章已有完整产物，已跳过（--force 可强制重跑）",
            }

    folder.mkdir(parents=True, exist_ok=True)

    # ---------------- 卡片截图（必须在下图之前，成功的话会变成 image block）
    t0 = time.perf_counter()
    assets_mod.materialize_cards(article, folder, enable=screenshot)
    timings["cards_ms"] = int((time.perf_counter() - t0) * 1000)

    # ---------------- 图片
    t0 = time.perf_counter()
    assets_mod.download_all(
        article, folder / "assets", compress=compress, max_edge=max_edge
    )
    timings["assets_ms"] = int((time.perf_counter() - t0) * 1000)

    # ---------------- 渲染
    t0 = time.perf_counter()
    outputs: list[dict] = []
    if "docx" in formats:
        p = render.render_docx(article, folder / "article.docx")
        outputs.append(
            {"file": p.name, "format": "docx", "sha256": _sha256_file(p),
             "bytes": p.stat().st_size, "renderer": "python-docx"}
        )
    if "html" in formats:
        p = render.render_html(article, folder / "article.html")
        outputs.append(
            {"file": p.name, "format": "html", "sha256": _sha256_file(p),
             "bytes": p.stat().st_size, "renderer": "inline-base64"}
        )
    if "json" in formats:
        p = folder / "article.json"
        p.write_text(
            json.dumps(article.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        outputs.append(
            {"file": p.name, "format": "json", "sha256": _sha256_file(p),
             "bytes": p.stat().st_size, "renderer": "model-dump"}
        )
    timings["render_ms"] = int((time.perf_counter() - t0) * 1000)

    # 体积提示：GIF / 大图会让 docx 膨胀，提醒用 --compress
    docx_out = next((o for o in outputs if o["format"] == "docx"), None)
    if docx_out and docx_out["bytes"] > 8 * 1024 * 1024:
        article.warnings.append(
            f"docx 体积 {docx_out['bytes'] / 1048576:.1f} MB 偏大，可加 --compress 压缩图片"
        )

    # ---------------- manifest
    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    stats = article.to_dict()["stats"]
    manifest = {
        "schema": "w2d.manifest/1",
        "tool_version": TOOL_VERSION,
        "identity": identity,
        "source_url": final_url or url,
        "channel": channel,
        "html_sha256": sha,
        "title": article.title,
        "account": article.account,
        "publish_time": article.publish_time,
        "stats": stats,
        "outputs": outputs,
        "assets": [
            {"file": a.file, "bytes": a.bytes, "status": a.status,
             "referer": a.referer_strategy, "reason": a.reason}
            for a in article.assets
        ],
        "warnings": article.warnings,
        "timings": timings,
        "notice": "本文内容版权归原作者所有，本文件仅供个人存档与研究使用。",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "status": "done",
        "dir": str(folder),
        "title": article.title,
        "account": article.account,
        "publish_time": article.publish_time,
        "stats": stats,
        "outputs": [o["file"] for o in outputs],
        "warnings": article.warnings,
        "timings": timings,
    }


def export_many(urls: list[str], *, out_root: Path, **kw) -> list[dict]:
    """批量导出：逐篇串行（渲染是 CPU 密集；也便于看清是哪篇出错）。"""
    results = []
    for i, u in enumerate(urls, 1):
        try:
            r = export(u, out_root=out_root, **kw)
            r["url"] = u
            r["index"] = i
        except fetcher.FetchError as exc:
            r = {"ok": False, "url": u, "index": i, "status": "fetch_error",
                 "error": str(exc), "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001 - 批量里单篇失败不能拖垮整体
            r = {"ok": False, "url": u, "index": i, "status": "error",
                 "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)
    return results
