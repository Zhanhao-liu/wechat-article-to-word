"""Article 内容模型：所有抓取/渲染都围绕这一组数据结构。

设计原则：
  - blocks[] 是唯一中间表示，渲染器只认 blocks，不再碰 HTML。
  - 样式全部结构化（color/size_pt/bold/align），不保留 CSS 字符串。
  - 任何信息缺失都要有 fallback，保证「宁可降级也不能丢内容」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

SCHEMA = "w2d.article/1"


@dataclass
class Run:
    """一段同样式的行内文字。"""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str | None = None  # 6 位 hex，不带 #
    size_pt: float | None = None
    mono: bool = False  # 等宽字体（行内代码）
    link: str | None = None

    def is_empty(self) -> bool:
        return not self.text or not self.text.strip()


@dataclass
class Block:
    """块级元素。kind 取 paragraph/heading/image/quote/list/table/card/media。"""

    kind: str
    runs: list[Run] = field(default_factory=list)
    level: int = 0
    align: str | None = None  # left/center/right/justify
    indent_cm: float | None = None
    bg: str | None = None  # 引用块底色
    asset: str | None = None  # 图片相对路径（assets/xxx.png）
    width_px: int | None = None
    caption: str | None = None
    items: list[list[Run]] = field(default_factory=list)
    ordered: bool = False
    rows: list[list[list[Run]]] = field(default_factory=list)
    header_rows: int = 0
    header_inferred: bool = False  # True = 原文没有表头样式，是我们推断的
    text_fallback: str | None = None
    link: str | None = None
    html: str | None = None  # 背景图卡片的原始 HTML 片段，供截图用；不写进 json

    def has_text(self) -> bool:
        return any(not r.is_empty() for r in self.runs)


@dataclass
class Asset:
    id: str
    src_url: str
    file: str
    status: str = "pending"  # ok/failed
    bytes: int = 0
    mime: str | None = None
    sha1: str | None = None
    reason: str | None = None
    referer_strategy: str | None = None


@dataclass
class Article:
    source_url: str
    channel: str  # http/chrome/dom/html
    fetched_at: str
    html_sha256: str
    title: str = ""
    account: str = ""
    author: str = ""
    publish_time: str = ""
    digest: str = ""
    blocks: list[Block] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema: str = SCHEMA

    def to_dict(self) -> dict:
        d = asdict(self)
        # html 片段只用于截图，不落进 article.json（体积大且无阅读价值）
        for b in d["blocks"]:
            b.pop("html", None)

        def runs_len(runs: list[Run]) -> int:
            return sum(len(r.text) for r in runs)

        # 字数要覆盖三种存放位置：普通块的 runs、列表的 items、表格的 rows，以及图注
        text_chars = 0
        for b in self.blocks:
            text_chars += runs_len(b.runs)
            text_chars += sum(runs_len(item) for item in b.items)
            text_chars += sum(runs_len(cell) for row in b.rows for cell in row)
            if b.caption:
                text_chars += len(b.caption)

        d["stats"] = {
            "blocks": len(self.blocks),
            "images": sum(1 for b in self.blocks if b.kind == "image"),
            "tables": sum(1 for b in self.blocks if b.kind == "table"),
            "chars": text_chars,
            "assets_ok": sum(1 for a in self.assets if a.status == "ok"),
            "assets_failed": sum(1 for a in self.assets if a.status == "failed"),
        }
        return d
