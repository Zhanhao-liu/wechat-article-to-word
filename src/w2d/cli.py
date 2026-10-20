"""命令行入口：w2d export <url>... [--out DIR] [--formats docx,html,json]"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import fetcher
from .pipeline import export, export_many

app = typer.Typer(
    add_completion=False,
    help="把公众号文章导出为保留文字与图片排版的 Word。",
)
console = Console()


def _parse_formats(s: str) -> tuple[str, ...]:
    items = tuple(x.strip().lower() for x in s.split(",") if x.strip())
    allowed = {"docx", "html", "json"}
    bad = [x for x in items if x not in allowed]
    if bad:
        raise typer.BadParameter(f"不支持的格式：{', '.join(bad)}（可选 {', '.join(sorted(allowed))}）")
    return tuple(dict.fromkeys(items))


@app.command("export")
def cmd_export(
    urls: list[str] = typer.Argument(None, help="一个或多个公众号文章 URL"),
    out: Path = typer.Option(Path("output"), "--out", "-o", help="产物根目录"),
    formats: str = typer.Option("docx,html,json", "--formats", "-f", help="输出格式，逗号分隔"),
    force: bool = typer.Option(False, "--force", help="忽略已有产物，强制重跑"),
    input_html: Path = typer.Option(None, "--input-html", help="改用本地保存的 HTML（兜底通道）"),
    source_url: str = typer.Option(None, "--source-url", help="配合 --input-html 说明原始 URL"),
    url_file: Path = typer.Option(None, "--url-file", help="每行一个 URL 的文件"),
    compress: bool = typer.Option(
        False, "--compress", help="压缩图片（长边超限则缩放，GIF/webp 转静态图）"
    ),
    max_edge: int = typer.Option(1600, "--max-edge", help="配合 --compress：长边上限像素"),
    screenshot: bool = typer.Option(
        True, "--screenshot/--no-screenshot", help="是否把背景图卡片整块截图（默认开）"
    ),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出结果（便于脚本调用）"),
) -> None:
    """导出公众号文章为 docx。"""
    targets = list(urls or [])
    if url_file:
        if not url_file.exists():
            raise typer.BadParameter(f"URL 文件不存在：{url_file}")
        # utf-8-sig：PowerShell 的 Set-Content -Encoding UTF8 会写 BOM，
        # 不剥掉的话首行 URL 会变成 "\ufeffhttps://..."，urlparse 认不出 scheme
        for line in url_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                targets.append(line)
    if not targets and not input_html:
        console.print("[red]请提供至少一个公众号文章 URL，或用 --input-html 指定本地 HTML[/red]")
        raise typer.Exit(2)
    if not targets and input_html:
        targets = [""]

    fmt = _parse_formats(formats)
    out_root = out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    results = []
    for i, u in enumerate(targets, 1):
        label = u or str(input_html)
        if not json_out:
            console.rule(f"[{i}/{len(targets)}] {label[:80]}")
        try:
            r = export(
                u or None,
                out_root=out_root,
                formats=fmt,
                force=force,
                input_html=input_html,
                source_url=source_url,
                compress=compress,
                screenshot=screenshot,
                max_edge=max_edge,
            )
            r["url"] = label
            r["index"] = i
        except fetcher.FetchError as exc:
            r = {"ok": False, "url": label, "index": i, "status": "fetch_error", "error": str(exc), "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "url": label, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)

        if not json_out:
            _print_one(r)

    if json_out:
        console.print_json(json.dumps(results, ensure_ascii=False))
    else:
        _print_summary(results)
    if not all(r.get("ok") for r in results):
        raise typer.Exit(1)


def _print_one(r: dict) -> None:
    if not r.get("ok"):
        console.print(f"[red]✗ {r.get('status')}[/red]  {r.get('error')}")
        if r.get("hint"):
            console.print(f"  [yellow]建议：{r['hint']}[/yellow]")
        return
    if r.get("skipped"):
        console.print(f"[yellow]跳过[/yellow] {r.get('message')}")
        console.print(f"  已有产物：{r.get('dir')}")
        return
    st = r.get("stats") or {}
    console.print(f"[green]✓[/green] {r.get('title')}")
    console.print(f"  公众号：{r.get('account') or '—'}    发布：{r.get('publish_time') or '—'}")
    console.print(
        f"  块 {st.get('blocks')} ｜ 图 {st.get('images')}（成功 {st.get('assets_ok')} / 失败 {st.get('assets_failed')}）"
        f" ｜ 表 {st.get('tables')} ｜ 字数 {st.get('chars')}"
    )
    console.print(f"  产物：{r.get('dir')}")
    for f in r.get("outputs") or []:
        console.print(f"    - {f}")
    t = r.get("timings") or {}
    if t:
        console.print(
            f"  [dim]耗时：抓取 {t.get('fetch_ms')}ms / 解析 {t.get('parse_ms')}ms / "
            f"下图 {t.get('assets_ms')}ms / 渲染 {t.get('render_ms')}ms[/dim]"
        )
    for w in r.get("warnings") or []:
        console.print(f"  [yellow]⚠ {w}[/yellow]")


def _print_summary(results: list[dict]) -> None:
    if len(results) <= 1:
        return
    table = Table(title="批量结果", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("状态")
    table.add_column("标题 / 错误")
    table.add_column("产物目录")
    for r in results:
        status = "✓" if r.get("ok") else "✗"
        name = r.get("title") or r.get("error") or ""
        table.add_row(str(r.get("index", "")), status, str(name)[:50], str(r.get("dir", ""))[:60])
    console.print(table)


@app.command("serve")
def cmd_serve(
    port: int = typer.Option(8865, "--port", help="本地工作台端口"),
    out: Path = typer.Option(None, "--out", help="产物根目录（默认用上次选择的目录）"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
) -> None:
    """启动本地 Web 工作台：打开网页粘贴链接即可导出。"""
    import subprocess
    import threading

    from . import shot
    from .web import serve

    url = f"http://127.0.0.1:{port}"
    if not no_browser:
        def _open() -> None:
            # 本机没有默认浏览器关联时 webbrowser 会弹「选择浏览器」框，
            # 所以优先直接用已探测到的 Chrome/Edge 打开。
            browser = shot.find_chrome()
            try:
                if browser is not None:
                    subprocess.Popen([str(browser), url])  # noqa: S603
                else:
                    import webbrowser

                    webbrowser.open(url)
            except OSError:
                console.print(f"[yellow]请手动在浏览器打开：[/yellow] {url}")

        threading.Timer(1.2, _open).start()
    serve(port=port, out=out)


@app.command("links")
def cmd_links(
    page_url: str = typer.Argument(..., help="合集/专辑页或任意含文章链接的页面"),
    out: Path = typer.Option(None, "--out", "-o", help="把链接写入文件（每行一个）"),
) -> None:
    """从一个页面里提取公众号文章链接（合集页批量导出的入口）。

    说明：公众号合集页（appmsgalbum）的列表常常靠 JS 渲染，HTML 里可能没有链接；
    这时本命令会返回空，需要改用逐篇 URL。属于 best-effort。
    """
    import re

    from . import fetcher as f

    try:
        res = f.fetch(page_url)
    except fetcher.FetchError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc

    pattern = re.compile(r"https?://mp\.weixin\.qq\.com/s\?[^\s\"'<>\\]+")
    found: list[str] = []
    for m in pattern.finditer(res.text):
        url = m.group(0).replace("&amp;", "&")
        if url not in found:
            found.append(url)

    if not found:
        console.print("[yellow]页面 HTML 里没有直接出现文章链接。[/yellow]")
        console.print("  合集页通常靠 JS 渲染，请改用逐篇 URL，或浏览器另存后走 --input-html。")
        raise typer.Exit(1)

    for u in found:
        console.print(u)
    console.print(f"\n共 {len(found)} 条")
    if out:
        out.write_text("\n".join(found) + "\n", encoding="utf-8")
        console.print(f"已写入 {out}")


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()
