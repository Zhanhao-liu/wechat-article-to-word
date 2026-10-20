"""验收：把 docx 反解成 HTML（图片 base64 内联），再交给 Chrome 截图。

目的：验证「真正的交付物 docx 里到底装了什么」——文字顺序、图片是否内嵌、
表格/标题是否成形。mammoth 只做结构反解，不保证样式一致，所以这里看的是
「内容对不对」，不是「版式像不像」。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import mammoth

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "tmp" / "qa"
QA.mkdir(parents=True, exist_ok=True)


def convert(docx: Path, out_html: Path) -> dict:
    counter = {"n": 0}

    def img_handler(image):
        counter["n"] += 1
        with image.open() as f:
            data = f.read()
        b64 = base64.b64encode(data).decode()
        return {"src": f"data:{image.content_type};base64,{b64}"}

    with docx.open("rb") as f:
        result = mammoth.convert_to_html(f, convert_image=mammoth.images.img_element(img_handler))
    html = result.value
    msgs = [str(m) for m in result.messages]

    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{docx.parent.name}</title>
<style>
body{{max-width:820px;margin:0 auto;padding:28px 20px 60px;background:#fff;
 font-family:"Microsoft YaHei",sans-serif;color:#333;font-size:14px;line-height:1.7;}}
h1,h2,h3,h4,h5{{color:#1a1a1a;margin:18px 0 8px;}}
img{{max-width:100%;height:auto;display:block;margin:8px auto;}}
table{{border-collapse:collapse;margin:10px 0;}}
td,th{{border:1px solid #ccc;padding:4px 8px;}}
</style></head><body>
<div style="color:#999;font-size:12px;margin-bottom:16px">
docx 反解自：{docx.name}（{docx.stat().st_size} B）｜图片 {counter['n']} 张已内嵌
</div>
{html}
</body></html>"""
    out_html.write_text(doc, encoding="utf-8")
    return {"images": counter["n"], "html_bytes": len(doc), "messages": msgs[:5]}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for i, arg in enumerate(sys.argv[1:], start=1):
        docx = Path(arg).resolve()
        if not docx.exists():
            print(f"跳过（不存在）：{docx}")
            continue
        out = QA / f"docx_check_{i}.html"
        info = convert(docx, out)
        print(f"[{i}] {docx.parent.name[:36]}")
        print(f"    内嵌图片 {info['images']} 张，HTML {info['html_bytes']} 字节 -> {out.name}")
        for m in info["messages"]:
            print(f"    mammoth: {m[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
