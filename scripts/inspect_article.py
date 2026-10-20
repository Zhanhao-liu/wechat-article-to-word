"""检查公众号文章页面结构：为 M1 的选择器提供事实依据。

用法：.venv\\Scripts\\python.exe scripts\\inspect_article.py URL [URL...]

对每个 URL 输出：
  - HTTP 状态、HTML 大小、是否命中风控/验证页
  - #js_content 是否存在、是否带隐藏 style、文本长度
  - 标题/号名/时间的来源（DOM id 还是 window.cgiDataNew）
  - 图片统计：img 总数、data-src 占比、真实域名分布
  - 噪声节点是否存在
并把原始 HTML 存到 tmp/inspect/ 便于后续调试。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
from lxml import html as lxml_html

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tmp" / "inspect"
OUT.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

NOISE_IDS = [
    "js_top_ad_area",
    "content_bottom_area",
    "js_pc_qr_code",
    "js_tags_preview_toast",
    "js_article",
    "page-content",
]


def fetch(url: str) -> httpx.Response:
    # 抓微信一律直连：清掉系统代理，且 trust_env=False
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    with httpx.Client(timeout=25.0, follow_redirects=True, trust_env=False) as c:
        return c.get(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})


def first_text(doc, *selectors: str) -> tuple[str, str] | tuple[None, None]:
    for sel in selectors:
        try:
            els = doc.cssselect(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in els:
            t = " ".join(el.text_content().split())
            if t:
                return t, sel
    return None, None


def cgi_value(raw: str, key: str) -> str | None:
    """从 window.cgiDataNew / 内联脚本里挖字段（新版页面元数据的真正来源）。"""
    import re

    for pat in (
        rf'var\s+{key}\s*=\s*"([^"]*)"',
        rf'{key}\s*:\s*"([^"]*)"',
        rf'"{key}"\s*:\s*"([^"]*)"',
        rf"{key}\s*:\s*'([^']*)'",
    ):
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return None


def inspect(url: str) -> dict:
    info: dict = {"url": url}
    r = fetch(url)
    raw = r.text
    info["status"] = r.status_code
    info["bytes"] = len(r.content)
    info["final_url"] = str(r.url)
    info["sha256"] = hashlib.sha256(r.content).hexdigest()[:16]

    slug = hashlib.sha256(url.encode()).hexdigest()[:8]
    (OUT / f"{slug}.html").write_text(raw, encoding="utf-8", errors="replace")
    info["saved"] = f"tmp/inspect/{slug}.html"

    doc = lxml_html.fromstring(raw)

    # 风控 / 异常页
    warn = []
    for sel in ("#js_verify", ".weui-msg__title", "#js_error_msg", ".weui-msg"):
        try:
            for el in doc.cssselect(sel):
                t = " ".join(el.text_content().split())[:60]
                if t:
                    warn.append(t)
        except Exception:  # noqa: BLE001
            pass
    info["warn_nodes"] = warn[:3]

    # 正文节点
    content = doc.cssselect("#js_content")
    info["has_js_content"] = bool(content)
    if content:
        el = content[0]
        info["js_content_style"] = el.get("style")
        info["text_len"] = len(" ".join(el.text_content().split()))
        info["child_tags"] = dict(Counter(c.tag for c in el.iter() if c is not el).most_common(12))

        imgs = el.cssselect("img")
        info["img_total"] = len(imgs)
        info["img_with_data_src"] = sum(1 for i in imgs if i.get("data-src"))
        info["img_src_prefix"] = dict(
            Counter((i.get("data-src") or i.get("src") or "")[:32] for i in imgs).most_common(5)
        )
        info["img_sample"] = [
            {
                "src": (i.get("src") or "")[:60],
                "data-src": (i.get("data-src") or "")[:80],
                "style": (i.get("style") or "")[:40],
                "w": i.get("width"),
                "h": i.get("height"),
            }
            for i in imgs[:3]
        ]
        info["section_depth"] = max(
            (len(list(anc.iterancestors())) for anc in el.iter() if anc.tag == "section"), default=0
        )
        info["sections"] = len(el.cssselect("section"))
        info["tables"] = len(el.cssselect("table"))
        info["svg_imgs"] = sum(1 for i in imgs if (i.get("src") or "").startswith("data:image/svg"))

    # 元数据
    title, tsel = first_text(doc, "#activity-name", ".rich_media_title", "h1")
    account, asel = first_text(doc, "#js_name", ".rich_media_meta_nickname", "#js_wx_follow_nickname")
    ptime, psel = first_text(doc, "#publish_time", "#meta_content .rich_media_meta_text")
    info["meta_dom"] = {"title": title, "title_sel": tsel, "account": account, "account_sel": asel, "ptime": ptime}
    info["cgiDataNew_present"] = "cgiDataNew" in raw
    info["meta_cgi"] = {
        k: cgi_value(raw, k)
        for k in ("nickname", "title", "ct", "create_time", "user_name", "msg_title")
    }

    # 噪声节点存在性
    info["noise_present"] = {n: bool(doc.cssselect(f"#{n}")) for n in NOISE_IDS}
    info["has_meta_og_title"] = bool(doc.cssselect('meta[property="og:title"]'))
    info["og_title"] = (
        doc.cssselect('meta[property="og:title"]')[0].get("content")
        if doc.cssselect('meta[property="og:title"]')
        else None
    )
    info["host"] = urlparse(info["final_url"]).netloc
    return info


def main() -> int:
    urls = sys.argv[1:]
    if not urls:
        print(__doc__)
        return 2
    results = [inspect(u) for u in urls]
    for info in results:
        print("\n" + "=" * 78)
        print(f"{info['url']}")
        print("=" * 78)
        print(f"status={info['status']}  bytes={info['bytes']}  host={info['host']}  saved={info['saved']}")
        print(f"warn_nodes={info['warn_nodes']}")
        print(f"#js_content={info['has_js_content']}  text_len={info.get('text_len')}  "
              f"style={info.get('js_content_style')!r}")
        print(f"sections={info.get('sections')}  max_depth={info.get('section_depth')}  "
              f"tables={info.get('tables')}  svg_imgs={info.get('svg_imgs')}")
        print(f"img_total={info.get('img_total')}  with_data_src={info.get('img_with_data_src')}")
        if info.get("img_sample"):
            for s in info["img_sample"]:
                print(f"   img src={s['src']!r} data-src={s['data-src']!r} w={s['w']} h={s['h']}")
        print(f"DOM  meta: {info['meta_dom']}")
        print(f"cgiDataNew present={info['cgiDataNew_present']}  values={info['meta_cgi']}")
        print(f"og_title={info['og_title']!r}")
        print(f"noise present: {info['noise_present']}")

    (OUT / "inspect-result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n明细已写入 tmp/inspect/inspect-result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
