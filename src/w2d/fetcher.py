"""抓取层：把 URL 变成 HTML，并识别「不是正文页」的情况。

铁律：
  - 抓微信一律直连（清系统代理 + trust_env=False）。走境外代理更容易被风控。
  - 抓不到正文时不要抛裸异常，返回可执行的人话提示。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

ALLOWED_HOSTS = ("mp.weixin.qq.com",)
ALLOWED_PREFIX = ("/s/", "/s?", "/mp/appmsgalbum")

# 命中这些字样说明拿到的不是正文页
BLOCK_MARKERS = (
    ("环境异常", "微信判定当前环境异常，需要在微信客户端内打开该文章"),
    ("请在微信客户端打开", "该链接被限制为仅在微信客户端内打开"),
    ("该内容已被发布者删除", "文章已被作者删除"),
    ("此内容因违规无法查看", "文章因违规被屏蔽"),
    ("参数错误", "链接参数不完整（可能被截断，注意不要漏掉 signature 等参数）"),
    ("访问过于频繁", "触发访问频率限制，请稍后再试"),
)


class FetchError(RuntimeError):
    """带用户可执行提示的抓取错误。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:  # noqa: D105
        return f"{self.args[0]}" + (f"\n  建议：{self.hint}" if self.hint else "")


@dataclass
class FetchResult:
    url: str
    final_url: str
    text: str
    status: int
    sha256: str
    channel: str = "http"
    attempts: int = 1
    elapsed_ms: int = 0

    @property
    def size(self) -> int:
        return len(self.text)


def check_url(url: str) -> None:
    """URL 白名单：只处理公众号文章/合集页。"""
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchError(f"只支持 http(s) 链接：{url}")
    if p.netloc not in ALLOWED_HOSTS:
        raise FetchError(
            f"只处理公众号域名（{', '.join(ALLOWED_HOSTS)}），当前是 {p.netloc}",
            hint="本工具只做公众号文章，其他站点请用别的工具",
        )
    if not any(p.path.startswith(x) for x in ALLOWED_PREFIX):
        raise FetchError(
            f"不认识的公众号链接类型：{p.path}",
            hint="请粘贴形如 https://mp.weixin.qq.com/s/xxxxx 的文章链接",
        )


def _client(timeout: httpx.Timeout | None = None) -> httpx.Client:
    return httpx.Client(
        # 公众号页面 3.5–4.2 MB，且服务端限速时很慢：连接超时短、读取超时长
        timeout=timeout or httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=15.0),
        follow_redirects=True,
        trust_env=False,  # 忽略系统代理：抓微信必须直连
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )


def _clear_proxy_env() -> None:
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)


def fetch(url: str, *, timeout: float | None = None, retries: int = 2) -> FetchResult:
    """抓一篇文章的 HTML。网络类失败会自动重试（公众号服务端会限速）。"""
    check_url(url)
    _clear_proxy_env()

    tmo = httpx.Timeout(
        connect=15.0, read=timeout or 90.0, write=30.0, pool=15.0
    )
    last_error: Exception | None = None
    t_start = time.perf_counter()

    for attempt in range(1, retries + 2):
        try:
            with _client(tmo) as c:
                r = c.get(url)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            last_error = exc
            if attempt <= retries:
                time.sleep(1.5 * attempt)
                continue
            raise FetchError(
                f"网络失败（已重试 {retries} 次）：{type(exc).__name__}",
                hint="检查网络；或改用 --input-html 走本地 HTML 兜底",
            ) from exc

        if r.status_code == 404:
            raise FetchError("HTTP 404", hint="链接可能已失效")
        if r.status_code >= 500 and attempt <= retries:
            last_error = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(1.5 * attempt)
            continue
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}", hint="稍后重试；仍失败请用粘贴 HTML 通道")

        text = r.text
        if len(text) < 2000:
            if attempt <= retries:
                last_error = RuntimeError(f"内容过短 {len(text)} 字节")
                time.sleep(1.5 * attempt)
                continue
            raise FetchError(
                f"返回内容过短（{len(text)} 字节），可能被拦截",
                hint="在浏览器里打开该文章，用「另存为」保存网页后走 --input-html",
            )

        ensure_article_page(text)
        return FetchResult(
            url=url,
            final_url=str(r.url),
            text=text,
            status=r.status_code,
            sha256=hashlib.sha256(r.content).hexdigest(),
            attempts=attempt,
            elapsed_ms=int((time.perf_counter() - t_start) * 1000),
        )

    raise FetchError(
        f"抓取失败：{last_error}", hint="检查网络；或用 --input-html 导入本地保存的 HTML"
    )


def ensure_article_page(html: str) -> None:
    """判断拿到的是不是正文页；不是就抛带提示的错。"""
    head = html[:200_000]
    for marker, hint in BLOCK_MARKERS:
        if marker in head:
            raise FetchError(f"未获取到正文：页面提示「{marker}」", hint=hint)
    if 'id="js_content"' not in html and "id='js_content'" not in html:
        if "js_verify" in html or "verify" in head.lower() and "滑块" in head:
            raise FetchError(
                "未获取到正文：疑似验证页",
                hint="在浏览器打开该文章 → 另存为网页（.html）→ 用 --input-html 导入",
            )
        raise FetchError(
            "未获取到正文：页面里没有 #js_content 节点",
            hint="可能链接失效、页面改版，或需要走粘贴 HTML 通道",
        )
