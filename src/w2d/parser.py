"""解析与归一化：HTML → Article(blocks)。

这是全项目最容易因微信改版而失效的一层，所以：
  - 所有选择器集中在 SELECTORS / NOISE 常量里，改版只改这里；
  - 每个选择器后面都写了「为什么」，别凭感觉改。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from lxml import html as lxml_html

from .model import Article, Block, Run

# ---------------------------------------------------------------- 选择器
SELECTORS = {
    # 正文容器；注意它默认带 visibility:hidden，必须去掉 style 才能拿到内容
    "content": "#js_content",
    # 标题：新版页面仍保留该 id；取不到时回退 og:title / <title>
    "title": "#activity-name",
    # 公众号名
    "account": "#js_name",
    # 作者（部分号有）
    "author": "#js_author_name",
    # 发布时间（部分模板没有该节点，需回退 cgiDataNew 里的时间戳）
    "publish_time": "#publish_time",
}

# 必须删除的噪声容器：广告、底部推荐、PC 端二维码、标签预览
NOISE_SELECTORS = (
    "#js_top_ad_area",  # 顶部广告
    "#content_bottom_area",  # 文末推荐/相关阅读
    "#js_pc_qr_code",  # PC 端二维码区
    "#js_tags_preview_toast",  # 标签预览浮层
    "#js_article_comment",  # 评论区
    ".js_ad_area",
    ".rich_media_tool",  # 阅读/点赞工具条
    ".reward_area",  # 赞赏区
)

# 必须整块丢弃的标签（媒体类不在这里：它们走 MEDIA_TAGS 降级成文字）
SKIP_TAGS = {
    "script", "style", "noscript", "source", "svg", "mp-style-type",
}
CONTAINER_TAGS = {"section", "div", "article", "main", "header", "footer", "figure", "figcaption"}
WHITE_BG = {"", "transparent", "none", "rgba(0, 0, 0, 0)", "#ffffff", "#fff", "white"}

# 图注识别：alt 以「图1 / 表2 / Fig. 3 / Table 4」开头时当图注用
CAPTION_RE = re.compile(r"^\s*(图|表|照片|Figure|Fig\.?|Table)\s*[\d一二三四五六七八九十]")

# 媒体类组件：不下载媒体流，走 text_fallback 降级
MEDIA_TAGS = {
    "mpvideo": "视频",
    "mp-common-video": "视频",
    "mpvoice": "音频",
    "mp-common-voice": "音频",
    "qqmusic": "音乐",
    "mp-common-miniprogram": "小程序",
    "mp-miniprogram": "小程序",
    "iframe": "嵌入内容",
    "video": "视频",
    "audio": "音频",
}

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 样式工具
def parse_style(s: str | None) -> dict[str, str]:
    """把内联 style 拆成 dict；键统一小写。"""
    out: dict[str, str] = {}
    if not s:
        return out
    for part in s.split(";"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        # 去掉 !important
        v = re.sub(r"\s*!important\s*$", "", v, flags=re.I)
        if k and v:
            out[k] = v
    return out


def css_color_to_hex(v: str | None) -> str | None:
    """rgb(63,63,63) / #333 / #abcdef / red → 6 位 hex。"""
    if not v:
        return None
    v = v.strip().lower()
    named = {
        "black": "000000", "white": "ffffff", "red": "ff0000", "green": "008000",
        "blue": "0000ff", "gray": "808080", "grey": "808080",
    }
    if v in named:
        return named[v]
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            return "".join(c * 2 for c in h)
        if len(h) in (6, 8):
            return h[:6]
        return None
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", v)
    if m:
        r, g, b = (max(0, min(255, round(float(x)))) for x in m.groups())
        return f"{r:02x}{g:02x}{b:02x}"
    return None


def parse_px(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"(-?[\d.]+)\s*(px|pt|em|rem|%)?", v.strip().lower())
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "px"
    if unit == "pt":
        return num
    if unit in ("em", "rem"):
        return num * 16 * 0.75  # 以 16px 基准
    if unit == "%":
        return None
    return num * 0.75  # px → pt


def _style_to_run_kwargs(style: dict[str, str]) -> dict:
    kw: dict = {}
    color = css_color_to_hex(style.get("color"))
    if color:
        kw["color"] = color
    size = parse_px(style.get("font-size"))
    if size and 5 <= size <= 48:
        kw["size_pt"] = round(size * 2) / 2
    weight = style.get("font-weight", "")
    if weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600):
        kw["bold"] = True
    deco = style.get("text-decoration", "")
    if "underline" in deco:
        kw["underline"] = True
    if "italic" in style.get("font-style", ""):
        kw["italic"] = True
    return kw


def _align_of(style: dict[str, str]) -> str | None:
    a = style.get("text-align", "").lower()
    if a in ("left", "center", "right", "justify"):
        return a
    return None


def _norm_text(t: str | None) -> str:
    if not t:
        return ""
    t = t.replace("\u200b", "").replace("\xa0", " ")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return t


def localname(el) -> str:
    tag = getattr(el, "tag", "")
    if not isinstance(tag, str):
        return ""
    return tag.split("}")[-1].lower()


# ---------------------------------------------------------------- 元数据
def _text_of(doc, selector: str) -> str:
    els = doc.cssselect(selector)
    for el in els:
        t = _norm_text(" ".join(el.itertext())).strip()
        if t:
            return t
    return ""


def _var_value(raw: str, *names: str) -> str | None:
    """从内联脚本里挖 var 值：var ct = "1791389162" / "ct": "..."。

    注意：f-string 里的正则量词必须写成 {{1,200}}，否则会被当成格式字段。
    """
    for name in names:
        for pat in (
            rf'var\s+{name}\s*=\s*"([^"]{{1,200}})"',
            rf"var\s+{name}\s*=\s*'([^']{{1,200}})'",
            rf'"{name}"\s*:\s*"([^"]{{1,200}})"',
        ):
            m = re.search(pat, raw)
            if m:
                return m.group(1)
    return None


def _fmt_epoch(ts: str) -> str:
    try:
        sec = int(str(ts).strip()[:10])
    except (TypeError, ValueError):
        return ""
    if sec < 946_684_800:  # 2000-01-01 之前，明显不是发布时间
        return ""
    return datetime.fromtimestamp(sec, CST).strftime("%Y-%m-%d %H:%M")


def extract_meta(doc, raw: str) -> dict:
    title = _text_of(doc, SELECTORS["title"])
    if not title:
        og = doc.cssselect('meta[property="og:title"]')
        title = _norm_text(og[0].get("content")).strip() if og else ""
    if not title:
        # 最后兜底：<title> 里通常是「文章标题」或「标题 公众号名」
        t = doc.cssselect("title")
        if t:
            title = _norm_text(t[0].text_content()).strip()
            title = re.sub(r"\s*[-—|]\s*[^-—|]{1,30}$", "", title).strip()

    account = _text_of(doc, SELECTORS["account"]) or (_var_value(raw, "nickname") or "")
    author = _text_of(doc, SELECTORS["author"])
    ptime = _text_of(doc, SELECTORS["publish_time"])
    if not ptime:
        # 新版模板没有 #publish_time，发布时间在 cgiDataNew 的时间戳里
        for key in ("ct", "create_time", "publish_time"):
            v = _var_value(raw, key)
            if v:
                ptime = _fmt_epoch(v)
                if ptime:
                    break

    digest = ""
    for sel in ('meta[name="description"]', 'meta[property="og:description"]'):
        els = doc.cssselect(sel)
        if els and els[0].get("content"):
            digest = _norm_text(els[0].get("content")).strip()[:200]
            break

    return {"title": title, "account": account, "author": author, "publish_time": ptime, "digest": digest}


# ---------------------------------------------------------------- 行内转换
def inline_runs(el, style: dict[str, str], flags: dict | None = None) -> list[Run]:
    """把一个元素的「行内内容」展开成 runs（递归处理 strong/span/a/br）。"""
    flags = dict(flags or {})
    base_kwargs = _style_to_run_kwargs(style)
    out: list[Run] = []

    def add(text: str | None):
        t = _norm_text(text)
        if not t.strip():
            return
        kw = dict(base_kwargs)
        kw.update({k: v for k, v in flags.items() if v})
        out.append(Run(text=t, **kw))

    add(el.text)
    for child in el:
        tag = localname(child)
        if tag in SKIP_TAGS:
            add(child.tail)
            continue
        if tag == "br":
            out.append(Run(text="\n"))
            add(child.tail)
            continue
        if tag == "img":
            # 行内图片（表情、装饰图标）不在这里处理，交给块级转换；只保留其后的文字
            add(child.tail)
            continue

        st = {**style, **parse_style(child.get("style"))}
        fl = dict(flags)
        if tag in ("strong", "b"):
            fl["bold"] = True
        if tag in ("em", "i"):
            fl["italic"] = True
        if tag == "u":
            fl["underline"] = True
        if tag in ("code", "kbd", "samp"):
            fl["mono"] = True
        if tag == "a":
            href = child.get("href")
            if href and href.startswith(("http://", "https://")):
                fl["link"] = href
        out.extend(inline_runs(child, st, fl))
        add(child.tail)
    return out


# ---------------------------------------------------------------- 块级转换
def _is_emoji_img(el) -> bool:
    cls = (el.get("class") or "").lower()
    src = (el.get("src") or "") + (el.get("data-src") or "")
    return "emoji" in cls or src.startswith("data:")


def _bg_image_url(style: dict[str, str]) -> str | None:
    """只有真正带 url(...) 的背景图才算卡片。

    坑：公众号满页面都是 `background-image: none`，一旦用
    `"background-image" in style` 判断，会把几十个普通节点误判成卡片（实测 70 个）。
    """
    val = (style.get("background-image") or "").strip()
    if not val or val.lower() in ("none", "initial", "inherit"):
        return None
    m = re.search(r"url\(\s*['\"]?([^'\")]+)", val)
    if not m:
        return None  # linear-gradient 等，不当卡片
    url = m.group(1).strip()
    return url if url.startswith("http") else None


def _caption_from_alt(el) -> str | None:
    alt = _norm_text(el.get("alt")).strip()
    return alt if alt and CAPTION_RE.match(alt) else None


def _attr_text(el, *names: str) -> str | None:
    """按顺序取第一个非空属性值。"""
    for n in names:
        v = el.get(n)
        if v and v.strip():
            return _norm_text(v).strip()
    return None


def _caption_core(s: str) -> str:
    """去掉「图1 / Fig. 2 / 表3」前缀与标点，只留标题本体，用于判重。"""
    return re.sub(
        r"^\s*(图|表|照片|Figure|Fig\.?|Table)\s*[\d一二三四五六七八九十]+\s*[:：.、]?\s*", "", s
    ).strip()


def _dedupe_captions(blocks: list[Block]) -> None:
    """图片的 alt 图注若与后文自带的「图1 xxx」重复，就去掉 alt 那条，避免出现两行图注。"""
    def text_of(b: Block) -> str:
        return "".join(r.text for r in b.runs)

    for i, b in enumerate(blocks):
        if b.kind != "image" or not b.caption:
            continue
        core = _caption_core(b.caption)
        if len(core) < 4:
            continue
        # 图注行通常很短；长段落里出现「如图4所示」这种引用不算重复
        limit = max(40, len(core) * 3)
        for j in range(i + 1, min(i + 3, len(blocks))):
            nb = blocks[j]
            if nb.kind == "image":
                break
            text = text_of(nb).strip()
            if not text or len(text) > limit:
                continue
            if core[:6] in text:
                b.caption = None
                break


def _img_src(el) -> str | None:
    """图片真地址在 data-src；src 往往只是占位 gif。"""
    for attr in ("data-src", "data-original", "src"):
        v = el.get(attr)
        if v and not v.startswith("data:"):
            return v.strip()
    return None


def _img_width_px(el) -> int | None:
    st = parse_style(el.get("style"))
    for key in ("width", "max-width"):
        px = parse_px(st.get(key))
        if px:
            return int(px / 0.75)  # 还原成 px
    for attr in ("width", "data-w"):
        v = (el.get(attr) or "").strip()
        if v.isdigit():
            return int(v)
    return None


BLOCKISH_TAGS = {
    "img", "table", "ul", "ol", "blockquote", "section", "div",
    "figure", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
}


def _contains_block(el) -> bool:
    """子树里是否有块级内容。

    必须看整棵子树，不能只看直接子节点：公众号经常把图片写成
    <section><figure><span leaf><img></span></figure></section>，
    中间夹的 span 会把「直接子节点」判断骗过去，导致图片被当行内内容丢弃。
    """
    for d in el.iterdescendants():
        tag = localname(d)
        if tag in BLOCKISH_TAGS or tag in MEDIA_TAGS or tag.startswith("mp-"):
            return True
    return False


def convert(el, ctx: dict, blocks: list[Block]) -> None:
    """把 el 的内容追加到 blocks。ctx 提供 warnings 等上下文。"""
    tag = localname(el)
    if tag in SKIP_TAGS or tag in ("hr",):
        return

    # 图片
    if tag == "img":
        if _is_emoji_img(el):
            return
        src = _img_src(el)
        if not src:
            return
        blocks.append(
            Block(
                kind="image",
                asset=src,
                width_px=_img_width_px(el),
                caption=_caption_from_alt(el),
            )
        )
        return

    # 公众号名片组件：可见文字为空，信息全在 data-* 属性里，不处理就整块丢失
    if tag == "mp-common-profile":
        nick = _norm_text(el.get("data-nickname")).strip()
        alias = _norm_text(el.get("data-alias")).strip()
        sig = _norm_text(el.get("data-signature")).strip()
        bits = [nick or "未知公众号"]
        if alias:
            bits.append(f"（{alias}）")
        if sig:
            bits.append(f"　{sig}")
        text = "【公众号名片】" + "".join(bits)
        blocks.append(Block(kind="quote", runs=[Run(text=text, size_pt=9)], bg="F7F7F7"))
        return

    # 视频 / 音频 / 音乐 / 小程序 / 嵌入内容：媒体流不下载，但要留下可读的替代信息
    if tag in MEDIA_TAGS:
        label = MEDIA_TAGS[tag]
        title = _attr_text(el, "data-title", "data-name", "title", "data-nickname", "alt")
        link = _attr_text(el, "data-url", "data-src", "src")
        text = f"【{label}】{title}" if title else f"【{label}】"
        text += "（媒体内容未下载，请在原文查看）"
        blocks.append(Block(kind="media", text_fallback=text, link=link))
        ctx["media"] = ctx.get("media", 0) + 1
        return

    # 表格
    if tag == "table":
        blocks.append(_table_block(el))
        return

    # 列表
    if tag in ("ul", "ol"):
        ordered = tag == "ol"
        pending: list[list[Run]] = []

        def flush_items() -> None:
            nonlocal pending
            if pending:
                blocks.append(Block(kind="list", items=list(pending), ordered=ordered))
                pending = []

        for li in el:
            if localname(li) != "li":
                continue
            st_li = parse_style(li.get("style"))
            imgs = [
                d for d in li.iterdescendants()
                if localname(d) == "img" and not _is_emoji_img(d)
            ]
            if not imgs:
                runs = inline_runs(li, st_li)
                if runs:
                    pending.append(runs)
                continue
            # 含图片的列表项：文字仍作列表项，图片按原顺序紧跟其后（不能丢）
            flush_items()
            runs = inline_runs(li, st_li)
            if runs:
                blocks.append(Block(kind="list", items=[runs], ordered=ordered))
            for im in imgs:
                src = _img_src(im)
                if src:
                    blocks.append(
                        Block(
                            kind="image",
                            asset=src,
                            width_px=_img_width_px(im),
                            caption=_caption_from_alt(im),
                        )
                    )
        flush_items()
        return

    # 引用
    if tag == "blockquote":
        runs = inline_runs(el, parse_style(el.get("style")))
        if runs:
            blocks.append(Block(kind="quote", runs=runs, bg="F7F7F7"))
        return

    # 标题
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        st = parse_style(el.get("style"))
        runs = inline_runs(el, st)
        if runs:
            blocks.append(Block(kind="heading", runs=runs, level=int(tag[1]), align=_align_of(st)))
        return

    # 预格式（代码块）
    if tag == "pre":
        txt = el.text_content()
        if txt and txt.strip():
            blocks.append(Block(kind="paragraph", runs=[Run(text=txt.rstrip(), mono=True)]))
        return

    st = parse_style(el.get("style"))
    bg = css_color_to_hex(st.get("background-color") or st.get("background"))

    # 背景图卡片：Word 里放不下 CSS 背景。优先把整块截图成图片（M2 由 assets 层完成），
    # 截不了图就退化成「背景图 + 文字」或纯文字，绝不丢内容。
    bg_url = _bg_image_url(st)
    if bg_url:
        text = _norm_text(" ".join(el.itertext())).strip()
        ctx["cards"] = ctx.get("cards", 0) + 1
        try:
            from lxml import html as _h

            frag = _h.tostring(el, encoding="unicode")
        except Exception:  # noqa: BLE001
            frag = None
        if text or frag:
            blocks.append(
                Block(
                    kind="card",
                    asset=bg_url,  # 截图失败时的兜底：直接用背景图
                    html=frag,
                    text_fallback=text or None,
                    bg=bg or None,
                )
            )
        return

    # figure：图片 + 图注，合并成一个 image block（图注交给渲染器排版）
    if tag == "figure":
        img = None
        caption = ""
        for d in el.iter():
            lt = localname(d)
            if lt == "img" and img is None and not _is_emoji_img(d):
                img = d
            elif lt == "figcaption":
                caption = _norm_text(" ".join(d.itertext())).strip()
        if img is not None:
            src = _img_src(img)
            if src:
                blocks.append(
                    Block(
                        kind="image",
                        asset=src,
                        width_px=_img_width_px(img),
                        caption=caption or _caption_from_alt(img),
                    )
                )
                return
        # 没有图就退化成普通容器
        for child in el:
            convert(child, ctx, blocks)
        return

    # 容器：只要子树里有块级内容就往下拆。
    # 注意不要只判断 `tag in CONTAINER_TAGS`：公众号会把图片写成
    # <p>文字<span leaf><img></span>图注</p> 或 <section><span><img></span></section>，
    # 中间那层 <span> 不在容器名单里，只按标签判断会让图片被当行内内容丢掉。
    if _contains_block(el):
        # 容器自身的直接文字先成段，再处理子节点
        lead = inline_runs(_text_only_shim(el), st)
        if lead:
            blocks.append(Block(kind="paragraph", runs=lead, align=_align_of(st)))
        for child in el:
            convert(child, ctx, blocks)
            # 子节点的 tail 文字（挂在块级子节点后面的行内文字）不能丢
            tail = _norm_text(child.tail)
            if tail.strip():
                blocks.append(Block(kind="paragraph", runs=[Run(text=tail.strip())]))
        return

    runs = inline_runs(el, st)
    if not runs:
        return
    align = _align_of(st)
    if bg and bg not in {x.lstrip("#") for x in WHITE_BG}:
        blocks.append(Block(kind="quote", runs=runs, bg=bg, align=align))
    else:
        blocks.append(Block(kind="paragraph", runs=runs, align=align))


class _text_only_shim:
    """包一层只暴露 el.text 的假元素，供 inline_runs 处理容器直接文字。"""

    def __init__(self, el):
        self._el = el
        self.text = el.text
        self.tail = None
        self.tag = "span"

    def __iter__(self):
        return iter(())

    def get(self, key, default=None):
        return self._el.get(key, default)


def _cell_text(runs: list[Run]) -> str:
    return "".join(r.text for r in runs).strip()


def _is_numeric_cell(text: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d[\d.,%()\s~\-]*", text or ""))


def _looks_like_header(rows: list[list[list[Run]]]) -> bool:
    """原文没有 <th>/thead 时，判断首行是不是事实上的表头。

    依据：首行没有纯数字单元格，而下面某行有 —— 这是论文表格最常见的形态。
    """
    if len(rows) < 2:
        return False
    first = [_cell_text(c) for c in rows[0]]
    if not first or any(_is_numeric_cell(t) for t in first):
        return False
    return any(any(_is_numeric_cell(_cell_text(c)) for c in row) for row in rows[1:])


def _table_block(el) -> Block:
    rows: list[list[list[Run]]] = []
    header_rows = 0
    for tr in el.iter():
        if localname(tr) != "tr":
            continue
        cells: list[list[Run]] = []
        for cell in tr:
            if localname(cell) not in ("td", "th"):
                continue
            cells.append(inline_runs(cell, parse_style(cell.get("style"))) or [Run(text="")])
        if cells:
            rows.append(cells)

    inferred = False
    # 有 thead 或首行全 th 视为明确表头
    if rows:
        first_tr = next((tr for tr in el.iter() if localname(tr) == "tr"), None)
        if first_tr is not None and all(
            localname(c) == "th" for c in first_tr if localname(c) in ("td", "th")
        ):
            header_rows = 1
        elif el.cssselect("thead"):
            header_rows = 1
        elif _looks_like_header(rows):
            # 原文没写表头样式，但首行确实是表头：只加粗 + 淡底纹，不伪造原文的配色
            header_rows = 1
            inferred = True
    return Block(kind="table", rows=rows, header_rows=header_rows, header_inferred=inferred)


# ---------------------------------------------------------------- 尾部清理
def _cleanup(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        if b.kind in ("paragraph", "heading", "quote") and not b.has_text():
            continue
        if b.kind == "image" and not b.asset:
            continue
        if b.kind == "table" and not b.rows:
            continue
        # 合并紧邻的同类空段
        if out and b.kind == "paragraph" and out[-1].kind == "paragraph":
            if not b.has_text() and not out[-1].has_text():
                continue
        out.append(b)
    # 去掉首尾多余的空段
    while out and out[0].kind == "paragraph" and not out[0].has_text():
        out.pop(0)
    while out and out[-1].kind == "paragraph" and not out[-1].has_text():
        out.pop()
    return out


# ---------------------------------------------------------------- 入口
def parse(
    raw: str,
    *,
    url: str,
    channel: str,
    sha256: str,
    fetched_at: str,
) -> Article:
    """HTML → Article。"""
    doc = lxml_html.fromstring(raw)

    meta = extract_meta(doc, raw)

    content = doc.cssselect(SELECTORS["content"])
    if not content:
        raise ValueError("页面里找不到正文节点 #js_content")
    root = content[0]

    # 关键：正文默认 visibility:hidden，不移除 style 会导致「抓到空内容」
    if root.get("style"):
        root.attrib.pop("style", None)

    # 摘掉噪声容器
    for sel in NOISE_SELECTORS:
        for el in root.cssselect(sel):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    ctx: dict = {"warnings": []}
    blocks: list[Block] = []
    for child in root:
        convert(child, ctx, blocks)
    # 容器自身直接文字（一般没有，防御性处理）
    lead = inline_runs(_text_only_shim(root), parse_style(root.get("style")))
    if lead:
        blocks.insert(0, Block(kind="paragraph", runs=lead))

    blocks = _cleanup(blocks)
    _dedupe_captions(blocks)

    article = Article(
        source_url=url,
        channel=channel,
        fetched_at=fetched_at,
        html_sha256=sha256,
        title=meta["title"],
        account=meta["account"],
        author=meta["author"],
        publish_time=meta["publish_time"],
        digest=meta["digest"],
        blocks=blocks,
    )

    if ctx.get("cards"):
        article.warnings.append(
            f"{ctx['cards']} 个背景图卡片：优先整块截图插入 Word，失败则降级为「背景图 + 文字」"
        )
    if ctx.get("media"):
        article.warnings.append(
            f"{ctx['media']} 个视频/音频/小程序等媒体组件未下载，已降级为文字说明"
        )
    svg_count = len(root.cssselect("svg"))
    if svg_count:
        article.warnings.append(f"{svg_count} 个内联 SVG 图标已删除（Word 不支持内联矢量图标）")
    icon_imgs = sum(
        1 for i in root.cssselect("img") if (i.get("src") or "").startswith("data:image/svg")
    )
    if icon_imgs:
        article.warnings.append(f"{icon_imgs} 个 data:svg 装饰图标已删除")
    if not article.title:
        article.warnings.append("未能提取到文章标题")
    if not article.blocks:
        article.warnings.append("正文解析结果为空，请检查页面结构是否改版")
    return article
