"""python-docx 能力验证：能不能还原公众号文章那种「文字 + 图片 + 排版」。

不使用 Word COM，不依赖任何 Office 组件。
生成一个刻意模仿公众号排版结构的 docx，用来肉眼核对：

  标题（居中/加粗/大字号）
  作者行（灰度小字/居中）
  正文（首行缩进/1.75 行距/自定义字色/行内加粗变色）
  小标题（左侧竖色条装饰）
  居中图片 + 图注
  引用块（浅底纹 + 左边框 + 缩进）
  表格（表头深底白字 + 斑马纹）
  项目符号列表
  页脚页码

用法：.venv\\Scripts\\python.exe scripts\\demo_docx.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tmp" / "probe" / "demo_article.docx"
IMG = ROOT / "tmp" / "probe" / "demo_image.png"

CJK = "微软雅黑"
MONO = "Consolas"

# 公众号正文常见配色
C_TEXT = RGBColor(0x3F, 0x3F, 0x3F)
C_TITLE = RGBColor(0x1A, 0x1A, 0x1A)
C_MUTED = RGBColor(0x8C, 0x8C, 0x8C)
C_ACCENT = RGBColor(0x57, 0x6B, 0x95)  # 微信蓝
C_RED = RGBColor(0xE0, 0x3E, 0x2D)


# ------------------------------------------------------------------ 底层工具
def set_run_font(run, size_pt=None, color=None, bold=None, italic=None, font=CJK):
    """设置字体；中文必须写 eastAsia，否则 Word 会退回宋体。"""
    if font:
        run.font.name = font
        rpr = run._element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.insert(0, rf)
        rf.set(qn("w:ascii"), font)
        rf.set(qn("w:hAnsi"), font)
        rf.set(qn("w:eastAsia"), font)
    if size_pt is not None:
        run.font.size = Pt(size_pt)
    if color is not None:
        run.font.color.rgb = color
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic


def shade_paragraph(par, fill_hex: str):
    """段落底纹（引用块背景）。"""
    ppr = par._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    ppr.append(shd)


def paragraph_border(par, side: str = "left", color_hex: str = "D9D9D9", size: int = 18, space: int = 8):
    """段落边框（引用块左侧竖条、小标题装饰条）。size 单位 1/8 pt。"""
    ppr = par._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color_hex)
    pbdr.append(el)


def shade_cell(cell, fill_hex: str):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    tcpr.append(shd)


def add_page_number_footer(section):
    """页脚居中页码：第 X 页 / 共 Y 页。"""
    par = section.footer.paragraphs[0]
    par.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = par.add_run("第 ")
    set_run_font(run, 8, C_MUTED)
    _add_field(par, "PAGE")
    run = par.add_run(" 页 / 共 ")
    set_run_font(run, 8, C_MUTED)
    _add_field(par, "NUMPAGES")
    run = par.add_run(" 页")
    set_run_font(run, 8, C_MUTED)


def _add_field(par, code: str):
    run = par.add_run()
    set_run_font(run, 8, C_MUTED)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" {code} "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._element.append(fld_begin)
    run._element.append(instr)
    run._element.append(fld_end)


def add_hyperlink(par, url: str, text: str, size_pt: float = 10.5):
    """超链接需要手写 OOXML（python-docx 没有原生 API）。"""
    part = par.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hl = OxmlElement("w:hyperlink")
    hl.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "576B95")
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rf = OxmlElement("w:rFonts")
    rf.set(qn("w:ascii"), CJK)
    rf.set(qn("w:hAnsi"), CJK)
    rf.set(qn("w:eastAsia"), CJK)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size_pt * 2)))
    rpr.append(rf)
    rpr.append(color)
    rpr.append(u)
    rpr.append(sz)
    new_run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)
    hl.append(new_run)
    par._p.append(hl)


def body_par(doc, first_line_indent_cm: float = 0.0):
    """正文段落：1.75 行距、段后 8pt、可选首行缩进。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.75
    pf.space_before = Pt(0)
    pf.space_after = Pt(8)
    if first_line_indent_cm:
        pf.first_line_indent = Cm(first_line_indent_cm)
    return p


def make_image(path: Path):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 500), "#dfe9f5")
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 880, 480], outline="#576b95", width=4)
    d.text((60, 220), "DEMO IMAGE  900x500", fill="#1a1a1a")
    img.save(path, "PNG")


# ------------------------------------------------------------------ 正文
def build() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    make_image(IMG)

    doc = Document()

    # 页面：A4 + 中文文档常用页边距
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.6)
    sec.top_margin = Cm(2.2)
    sec.bottom_margin = Cm(2.2)
    add_page_number_footer(sec)

    # 默认样式兜底（防止未显式设置的段落跳宋体）
    normal = doc.styles["Normal"]
    normal.font.name = CJK
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK)

    # 1) 标题
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(6)
    set_run_font(p.add_run("如何用 python-docx 还原公众号排版"), 18, C_TITLE, bold=True)

    # 2) 作者行
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(18)
    set_run_font(p.add_run("原创 张三　2026-10-20"), 9, C_MUTED)

    # 3) 正文
    p = body_par(doc, first_line_indent_cm=0)
    set_run_font(p.add_run("公众号文章的排版特点是：全内联样式、大量 "), 10.5, C_TEXT)
    set_run_font(p.add_run("嵌套 section"), 10.5, C_ACCENT, bold=True)
    set_run_font(p.add_run("、图片懒加载、字号偏小且行距很大。下面逐项验证 python-docx 能否还原。"), 10.5, C_TEXT)

    # 4) 小标题（左侧竖条）
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(8)
    p.paragraph_format.left_indent = Cm(0.3)
    paragraph_border(p, "left", "576B95", 24, 8)
    set_run_font(p.add_run("一、文字能还原到什么程度"), 13, C_TITLE, bold=True)

    p = body_par(doc)
    set_run_font(p.add_run("颜色、加粗、斜体、字号、对齐、首行缩进、行距、" ), 10.5, C_TEXT)
    set_run_font(p.add_run("下划线"), 10.5, C_RED, bold=True)
    set_run_font(p.add_run("、行内代码 "), 10.5, C_TEXT)
    set_run_font(p.add_run("w:shd"), 10, C_RED, font=MONO)
    set_run_font(p.add_run(" 都能精确控制。链接："), 10.5, C_TEXT)
    add_hyperlink(p, "https://mp.weixin.qq.com/", "原文链接")
    set_run_font(p.add_run(" 也可以写成可点击的超链接。"), 10.5, C_TEXT)

    # 5) 居中图片 + 图注
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(4)
    p.add_run().add_picture(str(IMG), width=Cm(14))

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(14)
    set_run_font(p.add_run("图 1：图片按版心宽度等比缩放并居中"), 8.5, C_MUTED)

    # 6) 引用块：浅底纹 + 左边框 + 缩进
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.4)
    p.paragraph_format.right_indent = Cm(0.4)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.5
    shade_paragraph(p, "F7F7F7")
    paragraph_border(p, "left", "D9D9D9", 18, 10)
    set_run_font(p.add_run("引用块：公众号里常用「浅灰底 + 左侧竖条」承载金句或提示，"
                           "这两项都能通过段落底纹与段落边框实现。"), 10, RGBColor(0x5A, 0x5A, 0x5A))

    # 7) 表格
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    set_run_font(p.add_run("二、表格"), 13, C_TITLE, bold=True)

    table = doc.add_table(rows=4, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ["能力项", "python-docx", "说明"]
    rows = [
        ["文字与颜色", "完全支持", "run 级 rgb/bold/size"],
        ["图片内嵌", "完全支持", "add_picture，图片写进 docx"],
        ["表格", "支持", "样式 + 单元格底纹"],
    ]
    for j, h in enumerate(headers):
        cell = table.cell(0, j)
        cell.text = ""
        shade_cell(cell, "576B95")
        run = cell.paragraphs[0].add_run(h)
        set_run_font(run, 10, RGBColor(0xFF, 0xFF, 0xFF), bold=True)
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for i, row in enumerate(rows, start=1):
        for j, val in enumerate(row):
            cell = table.cell(i, j)
            cell.text = ""
            if i % 2 == 0:
                shade_cell(cell, "F2F5FA")  # 斑马纹
            run = cell.paragraphs[0].add_run(val)
            set_run_font(run, 9.5, C_TEXT)

    # 8) 列表
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    set_run_font(p.add_run("三、列表"), 13, C_TITLE, bold=True)
    for item in (
        "无序列表用项目符号样式，缩进可调",
        "有序列表用 List Number 样式，Word 会自动编号",
    ):
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(4)
        set_run_font(p.add_run(item), 10.5, C_TEXT)

    # 9) 结尾
    p = body_par(doc)
    p.paragraph_format.space_before = Pt(12)
    set_run_font(p.add_run("结论：公众号文章里 "), 10.5, C_TEXT)
    set_run_font(p.add_run("95% 的内容"), 10.5, C_ACCENT, bold=True)
    set_run_font(p.add_run(" 都能用 python-docx 精确落进 Word；"
                           "剩下的「背景图卡片、圆角、多列布局」转成图片插入即可。"), 10.5, C_TEXT)

    doc.save(str(OUT))
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"已生成：{path}")
    print(f"大小：{path.stat().st_size} B")
