"""M0 探针：验证三件生死事。

1. 出网：能否直连 mp.weixin.qq.com（不走境外代理）
2. 抓取：能否拿到含 #js_content 的文章 HTML（需要传 URL）
3. Word COM：本地 HTML → Word 打开 → SaveAs2(.docx)，且图片被内嵌、不弹窗

用法：
    .venv\\Scripts\\python.exe scripts\\probe.py
    .venv\\Scripts\\python.exe scripts\\probe.py "https://mp.weixin.qq.com/s/xxxx"
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tmp" / "probe"
TMP.mkdir(parents=True, exist_ok=True)

# pywin32 的 gencache 会把早期绑定缓存写到 %TEMP%\gen_py；本机该目录被安全策略拒写。
# 在 import win32com 之前把 TEMP 指到工作区内，代价可控（仅影响本进程）。
_PY_TEMP = ROOT / "tmp" / "temp"
_PY_TEMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = str(_PY_TEMP)
os.environ["TMP"] = str(_PY_TEMP)
import tempfile  # noqa: E402

tempfile.tempdir = str(_PY_TEMP)

RESULT: dict = {"ok": False, "checks": {}}


def check(name: str):
    """把一次检查包成惰性调用：只有真正调用时才执行，失败不中断后续检查。"""

    def deco(fn):
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                info = fn(*args, **kwargs) or {}
                info["ok"] = True
            except Exception as exc:  # noqa: BLE001 - 探针要吞掉所有异常
                info = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc(limit=4),
                }
            info["ms"] = round((time.perf_counter() - t0) * 1000)
            RESULT["checks"][name] = info
            flag = "PASS" if info["ok"] else "FAIL"
            print(f"[{flag}] {name}  ({info['ms']}ms)")
            if not info["ok"]:
                print("       " + str(info.get("error"))[:300])
            return info

        return wrapper

    return deco


# ---------------------------------------------------------------- 1. 环境
@check("env")
def probe_env():
    import docx
    import httpx
    import PIL
    import win32com  # noqa: F401

    return {
        "python": sys.version.split()[0],
        "httpx": httpx.__version__,
        "python_docx": getattr(docx, "__version__", "?"),
        "pillow": PIL.__version__,
        "root": str(ROOT),
        "root_has_nonascii": any(ord(c) > 127 for c in str(ROOT)),
    }


# ---------------------------------------------------------------- 2. 出网
@check("egress_direct")
def probe_egress():
    import httpx

    # 关键：抓微信必须直连。国内站点走境外代理会被风控，也可能连不上。
    prev = {k: os.environ.pop(k, None) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    for k in ("http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, trust_env=False) as client:
            r = client.get(
                "https://mp.weixin.qq.com/",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
                    ),
                },
            )
        return {"status": r.status_code, "bytes": len(r.content), "server": r.headers.get("server")}
    finally:
        for k, v in prev.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------- 3. 抓文章
def probe_article(url: str) -> dict:
    import httpx
    from lxml import html as lxml_html

    prev = {k: os.environ.pop(k, None) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True, trust_env=False) as client:
            r = client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
                    ),
                },
            )
    finally:
        for k, v in prev.items():
            if v is not None:
                os.environ[k] = v

    raw = TMP / "raw_article.html"
    raw.write_text(r.text, encoding="utf-8", errors="replace")

    doc = lxml_html.fromstring(r.text)
    content = doc.cssselect("#js_content")
    style_attr = content[0].get("style") if content else None
    warn = doc.cssselect("#js_verify, .weui-msg__title, #js_error_msg")
    imgs = content[0].cssselect("img") if content else []
    data_src = sum(1 for i in imgs if i.get("data-src"))
    return {
        "status": r.status_code,
        "bytes": len(r.content),
        "html_saved": str(raw.relative_to(ROOT)),
        "has_js_content": bool(content),
        "js_content_style": style_attr,
        "img_total": len(imgs),
        "img_with_data_src": data_src,
        "text_len": len(content[0].text_content().strip()) if content else 0,
        "warn_nodes": [w.get("id") or w.get("class") for w in warn][:3],
        "title_dom": bool(doc.cssselect("#activity-name")),
        "title_in_cginew": "cgiDataNew" in r.text[:400000],
    }


# ---------------------------------------------------------------- 4. Word COM
def _minimal_html(path: Path, png: Path) -> None:
    path.write_text(
        f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>W2D COM Probe</title>
<style>
 body {{ font-family: "Microsoft YaHei", sans-serif; font-size: 11pt; }}
 h1 {{ font-size: 18pt; color: #111111; text-align: center; }}
 .q {{ color: #576b95; font-weight: bold; }}
 .quote {{ background: #f7f7f7; border-left: 3px solid #ddd; padding: 8pt; }}
 table {{ border-collapse: collapse; }}
 td {{ border: 1px solid #cccccc; padding: 4pt 8pt; }}
</style></head>
<body>
<h1>中文标题测试：公众号排版探针</h1>
<p>这是一段正文，包含<span class="q">蓝色加粗强调</span>、普通文字与 English mix 123。</p>
<p style="text-align:center;"><img src="{png.as_uri()}" width="180"></p>
<div class="quote"><p>引用块：背景色 + 左边框能否保留？</p></div>
<table><tr><td>表头 A</td><td>表头 B</td></tr><tr><td>1</td><td>2</td></tr></table>
<p style="color:#e03e2d;">红色文字</p>
</body></html>""",
        encoding="utf-8",
    )


def _make_png(path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (360, 200), "#4a90d9")
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 350, 190], outline="#ffffff", width=3)
    d.text((30, 90), "W2D IMAGE PROBE", fill="#ffffff")
    img.save(path, "PNG")


@check("word_com_html_to_docx")
def probe_word_com():
    import win32com.client as win32

    # pywin32 默认会往 %TEMP%\gen_py 写早期绑定缓存；沙箱/受限环境下会被拒。
    # 置为只读后走进程内动态绑定，不落盘。
    try:
        win32.gencache.is_readonly = True
    except Exception:  # noqa: BLE001
        pass

    png = TMP / "probe_image.png"
    html_path = TMP / "probe.html"
    docx_path = TMP / "probe.docx"
    pdf_path = TMP / "probe.pdf"
    for p in (docx_path, pdf_path):
        p.unlink(missing_ok=True)
    if not png.exists():
        _make_png(png)
    _minimal_html(html_path, png)

    app = None
    info: dict = {}
    try:
        # DispatchEx 会用 CLSCTX_LOCAL_SERVER 新起一个独立 Word 进程，
        # 不会连到用户正在编辑的 Word 实例（那会误关用户文档）。
        app = win32.DispatchEx("Word.Application")
        info["bind_mode"] = "DispatchEx(gencache)"
        app.Visible = False
        app.DisplayAlerts = 0
        try:
            app.AutomationSecurity = 3  # 强制禁用宏
            app.Options.UpdateLinksAtOpen = False
        except Exception:  # noqa: BLE001
            pass
        info["word_version"] = str(app.Version)

        t0 = time.perf_counter()
        doc = app.Documents.Open(
            str(html_path),
            ConfirmConversions=False,
            ReadOnly=False,
            AddToRecentFiles=False,
            Visible=False,
        )
        info["open_ms"] = round((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        doc.SaveAs2(str(docx_path), FileFormat=16)  # 16 = wdFormatXMLDocument (.docx)
        info["save_ms"] = round((time.perf_counter() - t0) * 1000)

        # 顺便验证"HTML 直接导 PDF"这条路（exact 档的备选，不依赖 Chrome）
        try:
            doc.SaveAs2(str(pdf_path), FileFormat=17)  # 17 = wdFormatPDF
            info["pdf_via_word"] = pdf_path.exists() and pdf_path.stat().st_size > 1000
        except Exception as exc:  # noqa: BLE001
            info["pdf_via_word_error"] = f"{type(exc).__name__}: {exc}"

        info["paragraphs"] = doc.Paragraphs.Count
        info["inline_shapes"] = doc.InlineShapes.Count
        info["tables"] = doc.Tables.Count
        doc.Close(SaveChanges=0)
    finally:
        if app is not None:
            try:
                app.Quit(SaveChanges=0)
            except Exception:  # noqa: BLE001
                pass

    if not docx_path.exists():
        raise RuntimeError("docx 未生成")

    import zipfile

    with zipfile.ZipFile(docx_path) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/")]
        info["docx_bytes"] = docx_path.stat().st_size
        info["embedded_media"] = media

    # 用 python-docx 回读，确认文件可被程序消费
    import docx as pydocx

    d = pydocx.Document(str(docx_path))
    info["readback_paragraphs"] = len(d.paragraphs)
    info["readback_images"] = len(d.inline_shapes)
    info["readback_first_text"] = d.paragraphs[0].text[:40] if d.paragraphs else ""
    info["docx_path"] = str(docx_path.relative_to(ROOT))
    return info


# ---------------------------------------------------------------- 5. python-docx 降级档
@check("docx_clean_python_docx")
def probe_clean():
    import docx
    from docx.shared import Pt

    png = TMP / "probe_image.png"
    if not png.exists():
        _make_png(png)
    out = TMP / "probe_clean.docx"
    out.unlink(missing_ok=True)
    d = docx.Document()
    p = d.add_heading("中文标题测试", level=1)
    for run in p.runs:  # 中文字体必须走 eastAsia，否则会跳宋体
        run.font.name = "微软雅黑"
        rpr = run._element.get_or_add_rPr()
        rf = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
        if rf is None:
            from docx.oxml.ns import qn

            rf = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rf)
        from docx.oxml.ns import qn

        rf.set(qn("w:eastAsia"), "微软雅黑")
    body = d.add_paragraph("这是一段正文。")
    body.add_run("加粗").bold = True
    body.runs[0].font.size = Pt(10.5)
    d.add_picture(str(png))
    t = d.add_table(rows=2, cols=2)
    t.style = "Table Grid"
    t.cell(0, 0).text = "A"
    t.cell(0, 1).text = "B"
    d.save(str(out))
    return {"docx_bytes": out.stat().st_size, "path": str(out.relative_to(ROOT))}


# ---------------------------------------------------------------- main
def main() -> int:
    probe_env()
    probe_egress()
    if len(sys.argv) > 1:
        url = sys.argv[1]
        try:
            RESULT["checks"]["article"] = {"ok": True, **probe_article(url), "ms": 0}
            print(f"[PASS] article  status={RESULT['checks']['article']['status']}")
        except Exception as exc:  # noqa: BLE001
            RESULT["checks"]["article"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"[FAIL] article  {exc}")
    probe_word_com()
    probe_clean()

    checks = RESULT["checks"]
    critical = ["env", "egress_direct", "word_com_html_to_docx", "docx_clean_python_docx"]
    RESULT["ok"] = all(checks.get(k, {}).get("ok") for k in critical)
    RESULT["verdict"] = "M0 通过，可以进入 M1" if RESULT["ok"] else "M0 未通过，见失败项"

    out = ROOT / "docs" / "probe-result.json"
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(RESULT["verdict"])
    print(f"明细已写入 {out.relative_to(ROOT)}")
    return 0 if RESULT["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
