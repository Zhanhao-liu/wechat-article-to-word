"""视觉验收：docx → PDF → PNG，用来肉眼看版面有没有问题。

PDF 转换优先用 Word COM（只开 docx，不碰 HTML）；失败则尝试 WPS COM。
栅格化用 PyMuPDF。

用法：.venv\\Scripts\\python.exe scripts\\qa_render.py output/xxx/article.docx [页数]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "tmp" / "qa"
QA.mkdir(parents=True, exist_ok=True)

_py_temp = ROOT / "tmp" / "temp"
_py_temp.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = str(_py_temp)
os.environ["TMP"] = str(_py_temp)
import tempfile  # noqa: E402

tempfile.tempdir = str(_py_temp)


def docx_to_pdf_com(docx: Path, pdf: Path) -> tuple[bool, str]:
    """用 Word/WPS COM 把 docx 另存为 PDF。返回 (成功, 说明)。"""
    try:
        import win32com.client as win32
    except ImportError:
        return False, "pywin32 未安装"

    for progid, label in (("Word.Application", "Word"), ("KWPS.Application", "WPS")):
        app = None
        try:
            app = win32.DispatchEx(progid)
            app.Visible = False
            try:
                app.DisplayAlerts = 0
            except Exception:  # noqa: BLE001
                pass
            doc = app.Documents.Open(
                str(docx), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False
            )
            doc.SaveAs2(str(pdf), FileFormat=17)  # 17 = wdFormatPDF
            doc.Close(0)
            return True, f"{label} 导出成功"
        except Exception as exc:  # noqa: BLE001
            info = f"{label} 失败：{type(exc).__name__}: {' '.join(str(exc).split())[:120]}"
        finally:
            if app is not None:
                try:
                    app.Quit(0)
                except Exception:  # noqa: BLE001
                    pass
        print("   " + info)
    return False, "Word / WPS 均失败"


def rasterize(pdf: Path, stem: str, pages: int = 3, dpi: int = 110) -> list[Path]:
    import fitz  # PyMuPDF

    out: list[Path] = []
    with fitz.open(pdf) as doc:
        for i in range(min(pages, doc.page_count)):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=dpi)
            p = QA / f"{stem}_p{i + 1}.png"
            pix.save(str(p))
            out.append(p)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    docx = Path(sys.argv[1]).resolve()
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if not docx.exists():
        print(f"文件不存在：{docx}")
        return 3

    stem = docx.parent.name[:24].replace(" ", "_").replace("：", "_").replace("|", "_")
    pdf = QA / f"{stem}.pdf"
    t0 = time.perf_counter()
    ok, msg = docx_to_pdf_com(docx, pdf)
    print(f"PDF: {msg}  ({round((time.perf_counter()-t0)*1000)}ms)")
    if not ok or not pdf.exists():
        return 1
    print(f"PDF 大小：{pdf.stat().st_size} B")
    shots = rasterize(pdf, stem, pages)
    for s in shots:
        print(f"PNG: {s}  ({s.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
