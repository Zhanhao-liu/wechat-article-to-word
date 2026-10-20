"""渲染层：Article(blocks) → docx / html。

Word 只用 python-docx（不依赖 Office）。原则：
  - 图片必须内嵌进 docx，绝不外链；
  - 中文字体三个 rFonts 属性齐写，否则 Word 会跳宋体；
  - 复杂块（背景图卡片）降级为文字，绝不丢内容。
"""

from __future__ import annotations

import base64
import html as html_escape
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .model import Article, Block, Run

CJK = "微软雅黑"
MONO = "Consolas"

C_TITLE = RGBColor(0x1A, 0x1A, 0x1A)
C_MUTED = RGBColor(0x8C, 0x8C, 0x8C)
C_DEFAULT = RGBColor(0x3F, 0x3F, 0x3F)

ALIGN_MAP = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}
BODY_SIZE = 10.5
LINE_SPACING = 1.6


# ---------------------------------------------------------------- OOXML 小工具
def _set_font(run, size_pt=None, color: str | None = None, bold=None, italic=None,
              underline=None, font: str = CJK):
    run.font.name = font
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rf.set(qn(attr), font)
    if size_pt:
        run.font.size = Pt(size_pt)
    if color:
        run.font.color.rgb = color if isinstance(color, RGBColor) else RGBColor.from_string(color.upper())
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if underline is not None:
        run.underline = underline


def _shade_paragraph(par, fill: str):
    ppr = par._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill.lstrip("#"))
    ppr.append(shd)


def _paragraph_border(par, side="left", color="D9D9D9", size=18, space=8):
    ppr = par._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color.lstrip("#"))
    pbdr.append(el)


def _shade_cell(cell, fill: str):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill.lstrip("#"))
    tcpr.append(shd)


def _add_field(par, code: str, size=8.5, color=C_MUTED):
    run = par.add_run()
    _set_font(run, size, color=color)
    b = OxmlElement("w:fldChar")
    b.set(qn("w:fldCharType"), "begin")
    i = OxmlElement("w:instrText")
    i.set(qn("xml:space"), "preserve")
    i.text = f" {code} "
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "end")
    for x in (b, i, e):
        run._element.append(x)


def _add_hyperlink(par, url: str, text: str, size_pt: float = BODY_SIZE):
    part = par.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hl = OxmlElement("w:hyperlink")
    hl.set(qn("r:id"), r_id)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rf = OxmlElement("w:rFonts")
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rf.set(qn(attr), CJK)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "576B95")
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size_pt * 2)))
    rpr.append(rf)
    rpr.append(color)
    rpr.append(u)
    rpr.append(sz)
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    hl.append(r)
    par._p.append(hl)


def _write_runs(par, runs: list[Run], default_size: float = BODY_SIZE, default_color=C_DEFAULT):
    for run in runs:
        if run.text == "\n":
            par.add_run().add_break()
            continue
        if not run.text:
            continue
        mono = MONO if run.mono else CJK
        if run.link:
            _add_hyperlink(par, run.link, run.text, run.size_pt or default_size)
            continue
        r = par.add_run(run.text)
        _set_font(
            r,
            size_pt=run.size_pt or default_size,
            color=run.color,
            bold=run.bold or None,
            italic=run.italic or None,
            underline=run.underline or None,
            font=mono,
        )
        if not run.color:
            r.font.color.rgb = default_color


def _image_width_cm(path: Path, width_px: int | None, content_cm: float) -> float:
    """图片宽度：优先用页面里的像素宽，读不到就按原图尺寸，统一限在版心内。"""
    px = width_px
    if not px:
        try:
            from PIL import Image

            with Image.open(path) as im:
                px = im.width
        except Exception:  # noqa: BLE001
            px = None
    if not px:
        return min(content_cm, 12.0)
    cm = px * 0.75 / 72 * 2.54
    return max(1.5, min(content_cm, cm))


# ---------------------------------------------------------------- docx
def render_docx(article: Article, out_path: Path, *, title_override: str | None = None) -> Path:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.4)
    sec.top_margin = sec.bottom_margin = Cm(2.2)
    content_cm = 21.0 - 2.4 * 2

    normal = doc.styles["Normal"]
    normal.font.name = CJK
    normal.font.size = Pt(BODY_SIZE)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK)

    # 页脚页码
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run("第 ")
    _set_font(r, 8.5)
    r.font.color.rgb = C_MUTED
    _add_field(fp, "PAGE")
    r = fp.add_run(" 页 / 共 ")
    _set_font(r, 8.5)
    r.font.color.rgb = C_MUTED
    _add_field(fp, "NUMPAGES")
    r = fp.add_run(" 页")
    _set_font(r, 8.5)
    r.font.color.rgb = C_MUTED

    # 标题
    title = title_override or article.title or "未命名文章"
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.3
    _set_font(p.add_run(title), 16, bold=True, color=C_TITLE)

    # 元信息行
    meta_bits = [x for x in (article.account, article.author, article.publish_time) if x]
    if meta_bits:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(14)
        r = p.add_run("　·　".join(meta_bits))
        _set_font(r, 8.5, color=C_MUTED)

    asset_root = out_path.parent

    for b in article.blocks:
        if b.kind == "heading":
            level = min(max(b.level or 2, 1), 3)
            p = doc.add_paragraph()
            # 用真正的 Heading 样式：这样 Word 导航窗格与自动目录才认得出层级
            try:
                p.style = doc.styles[f"Heading {level}"]
            except KeyError:
                pass
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.space_after = Pt(6)
            p.paragraph_format.line_spacing = 1.35
            p.paragraph_format.keep_with_next = True
            if b.align:
                p.alignment = ALIGN_MAP[b.align]
            size = {1: 15, 2: 13.5, 3: 12.5}.get(level, 12)
            _write_runs(p, b.runs, default_size=size, default_color=C_TITLE)
            for r in p.runs:
                if not r.bold:
                    r.bold = True

        elif b.kind == "quote":
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.left_indent = Cm(0.4)
            pf.right_indent = Cm(0.4)
            pf.space_before = Pt(6)
            pf.space_after = Pt(6)
            pf.line_spacing = 1.5
            if b.align:
                p.alignment = ALIGN_MAP[b.align]
            _shade_paragraph(p, b.bg or "F7F7F7")
            _paragraph_border(p, "left", "D0D0D0", 18, 10)
            _write_runs(p, b.runs, default_size=BODY_SIZE)

        elif b.kind == "card":
            # 截图成功的话 asset 已经是本地 png；否则用背景图兜底 + 文字
            path = asset_root / b.asset if b.asset and b.asset.startswith("assets/") else None
            if path is not None and path.exists():
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(6)
                p.paragraph_format.space_after = Pt(4)
                try:
                    p.add_run().add_picture(
                        str(path), width=Cm(_image_width_cm(path, None, content_cm))
                    )
                except Exception:  # noqa: BLE001
                    pass
            if b.text_fallback:
                par = doc.add_paragraph()
                par.paragraph_format.left_indent = Cm(0.3)
                par.paragraph_format.space_after = Pt(6)
                _set_font(par.add_run(b.text_fallback), 9, color=C_MUTED)

        elif b.kind == "media":
            par = doc.add_paragraph()
            par.paragraph_format.space_before = Pt(4)
            par.paragraph_format.space_after = Pt(4)
            par.paragraph_format.left_indent = Cm(0.3)
            _shade_paragraph(par, "F5F5F5")
            _set_font(
                par.add_run(b.text_fallback or "【媒体内容未下载】"),
                9,
                color=RGBColor(0x66, 0x66, 0x66),
            )
            if b.link:
                _set_font(par.add_run("　"), 9)
                _add_hyperlink(par, b.link, b.link[:60], 8.5)

        elif b.kind == "image":
            if not b.asset:
                continue
            path = asset_root / b.asset
            if not path.exists():
                p = doc.add_paragraph()
                _set_font(p.add_run(f"【图片缺失：{b.asset}】"), 9, color=C_MUTED)
                continue
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(6)
            width = _image_width_cm(path, b.width_px, content_cm)
            try:
                p.add_run().add_picture(str(path), width=Cm(width))
            except Exception as exc:  # noqa: BLE001
                _set_font(p.add_run(f"【图片无法插入：{exc}】"), 9, color=C_MUTED)
            if b.caption:
                cp = doc.add_paragraph()
                cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                cp.paragraph_format.space_after = Pt(10)
                _set_font(cp.add_run(b.caption), 8.5, color=C_MUTED)

        elif b.kind == "list":
            style = "List Number" if b.ordered else "List Bullet"
            for item in b.items:
                try:
                    p = doc.add_paragraph(style=style)
                except KeyError:
                    p = doc.add_paragraph()
                    _set_font(p.add_run("· "), BODY_SIZE)
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.line_spacing = LINE_SPACING
                _write_runs(p, item)

        elif b.kind == "table":
            if not b.rows:
                continue
            ncols = max(len(r) for r in b.rows)
            table = doc.add_table(rows=len(b.rows), cols=ncols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for i, row in enumerate(b.rows):
                for j in range(ncols):
                    cell = table.cell(i, j)
                    cell.text = ""
                    is_header = i < b.header_rows
                    if is_header:
                        # 明确表头用原文风格的深底白字；推断出来的表头只加淡底，不伪造配色
                        _shade_cell(cell, "F2F2F2" if b.header_inferred else "576B95")
                    elif i % 2 == 0:
                        _shade_cell(cell, "F7F9FC")
                    runs = row[j] if j < len(row) else [Run(text="")]
                    par = cell.paragraphs[0]
                    par.paragraph_format.space_after = Pt(2)
                    if is_header:
                        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    white = is_header and not b.header_inferred
                    _write_runs(
                        par,
                        runs,
                        default_size=9.5,
                        default_color=RGBColor(0xFF, 0xFF, 0xFF) if white else C_DEFAULT,
                    )
                    if is_header:
                        for r in par.runs:
                            r.bold = True
            doc.add_paragraph().paragraph_format.space_after = Pt(2)

        else:  # paragraph
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
            pf.line_spacing = LINE_SPACING
            pf.space_before = Pt(0)
            pf.space_after = Pt(6)
            if b.align:
                p.alignment = ALIGN_MAP[b.align]
            if b.indent_cm:
                pf.left_indent = Cm(b.indent_cm)
            if b.bg:
                _shade_paragraph(p, b.bg)
            _write_runs(p, b.runs)

    # 结尾来源块
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    _paragraph_border(p, "top", "E0E0E0", 6, 6)
    _set_font(p.add_run("原文："), 8.5, color=C_MUTED)
    _add_hyperlink(p, article.source_url, article.source_url, 8.5)
    p2 = doc.add_paragraph()
    _set_font(p2.add_run("本文仅供个人存档与研究使用，版权归原作者所有。"), 8, color=C_MUTED)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


# ---------------------------------------------------------------- html
def render_html(article: Article, out_path: Path) -> Path:
    """离线单文件 HTML：图片转 base64 内联，作为「保真参照物」。"""
    root = out_path.parent

    def img_tag(rel: str | None) -> str:
        if not rel:
            return '<p style="color:#999">【图片缺失】</p>'
        p = root / rel
        if not p.exists():
            return f'<p style="color:#999">【图片缺失：{html_escape.escape(rel)}】</p>'
        mime = "image/png"
        if p.suffix.lower() in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif p.suffix.lower() == ".gif":
            mime = "image/gif"
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f'<img src="data:{mime};base64,{b64}" style="max-width:100%">'

    def runs_html(runs: list[Run]) -> str:
        out = []
        for r in runs:
            if r.text == "\n":
                out.append("<br>")
                continue
            css = []
            if r.color:
                css.append(f"color:#{r.color}")
            if r.size_pt:
                css.append(f"font-size:{r.size_pt * 4 / 3:.1f}px")
            if r.bold:
                css.append("font-weight:700")
            if r.italic:
                css.append("font-style:italic")
            if r.underline:
                css.append("text-decoration:underline")
            if r.mono:
                css.append("font-family:Consolas,monospace")
            text = html_escape.escape(r.text)
            style = f' style="{";".join(css)}"' if css else ""
            if r.link:
                out.append(
                    f'<a href="{html_escape.escape(r.link)}"{style}>{text}</a>'
                )
            else:
                out.append(f"<span{style}>{text}</span>")
        return "".join(out)

    body: list[str] = []
    for b in article.blocks:
        align = f"text-align:{b.align};" if b.align else ""
        if b.kind == "heading":
            body.append(f"<h{b.level} style=\"{align}\">{runs_html(b.runs)}</h{b.level}>")
        elif b.kind == "quote":
            body.append(
                f'<div style="background:#{b.bg or "F7F7F7"};border-left:3px solid #D0D0D0;'
                f'padding:8px 12px;margin:10px 0;">{runs_html(b.runs)}</div>'
            )
        elif b.kind == "image":
            body.append(f'<p style="text-align:center">{img_tag(b.asset)}</p>')
        elif b.kind == "card":
            if b.asset and b.asset.startswith("assets/"):
                body.append(f'<p style="text-align:center">{img_tag(b.asset)}</p>')
            if b.text_fallback:
                body.append(
                    f'<p style="color:#8c8c8c;font-size:13px;margin-left:6px">'
                    f"{html_escape.escape(b.text_fallback)}</p>"
                )
        elif b.kind == "media":
            link = (
                f' <a href="{html_escape.escape(b.link)}">{html_escape.escape(b.link[:50])}</a>'
                if b.link
                else ""
            )
            body.append(
                '<div style="background:#f5f5f5;padding:8px 12px;margin:8px 0;'
                'color:#666;font-size:13px">'
                f"{html_escape.escape(b.text_fallback or '【媒体内容未下载】')}{link}</div>"
            )
        elif b.kind == "list":
            tag = "ol" if b.ordered else "ul"
            items = "".join(f"<li>{runs_html(i)}</li>" for i in b.items)
            body.append(f"<{tag}>{items}</{tag}>")
        elif b.kind == "table":
            rows = []
            for i, row in enumerate(b.rows):
                tag = "th" if i < b.header_rows else "td"
                tds = "".join(f"<{tag}>{runs_html(c)}</{tag}>" for c in row)
                rows.append(f"<tr>{tds}</tr>")
            body.append(
                '<table border="1" cellspacing="0" style="border-collapse:collapse;width:100%;'
                'font-size:14px">'
                + "".join(rows)
                + "</table>"
            )
        else:
            body.append(f'<p style="{align}">{runs_html(b.runs)}</p>')

    meta_bits = " · ".join(x for x in (article.account, article.publish_time) if x)
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html_escape.escape(article.title)}</title>
<style>
body{{max-width:720px;margin:0 auto;padding:24px 18px 60px;background:#fff;
  font-family:-apple-system,"Microsoft YaHei",sans-serif;color:#3f3f3f;
  font-size:15px;line-height:1.75;}}
h1{{font-size:22px;text-align:center;color:#1a1a1a;}}
p{{margin:0 0 12px;}}
img{{max-width:100%;height:auto;}}
.meta{{color:#8c8c8c;font-size:13px;text-align:center;margin-bottom:24px;}}
.src{{margin-top:36px;padding-top:12px;border-top:1px solid #e0e0e0;color:#8c8c8c;font-size:12px;}}
</style></head><body>
<h1>{html_escape.escape(article.title)}</h1>
<div class="meta">{html_escape.escape(meta_bits)}</div>
{"".join(body)}
<div class="src">原文：<a href="{html_escape.escape(article.source_url)}">{html_escape.escape(article.source_url)}</a><br>
本文仅供个人存档与研究使用，版权归原作者所有。</div>
</body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path
