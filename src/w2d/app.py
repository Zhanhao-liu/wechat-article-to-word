"""桌面窗口启动器：双击就打开一个程序窗口，不再跳浏览器。

启动流程：
    1. 在后台线程起本地 HTTP 服务（端口自动选，不冲突）
    2. 用系统 WebView2 开一个**原生窗口**加载界面（pywebview）
    3. 关掉窗口 → 服务自动停止、进程退出

如果 WebView2 不可用，自动退化成「Chrome/Edge 应用模式窗口」（无地址栏，也像一个独立程序）。

单独运行（排错用）：
    python -m w2d.app --check          只启动服务并自检，不开窗口
    python -m w2d.app --no-window      只启动服务，用浏览器访问
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_NAME = "公众号文章 → Word"
APP_ID = "w2d"


def _base_dir() -> Path:
    """程序数据目录：优先项目目录（本机 %LOCALAPPDATA% 有写入限制）。"""
    project = Path(__file__).resolve().parents[2]
    for cand in (project / "logs", Path(os.environ.get("TEMP", ".")) / "w2d-logs"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            return cand
        except OSError:
            continue
    return project


def _log_dir() -> Path:
    return _base_dir()


def _setup_logging() -> Path:
    log_file = _log_dir() / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    )
    return log_file


def _free_port(preferred: int = 8865) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return 0


def _wait_ready(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/settings", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def _find_browser() -> str | None:
    from . import shot

    p = shot.find_chrome()
    return str(p) if p else None


def _open_in_app_window(url: str, quit_event: threading.Event) -> None:
    """用 Chrome/Edge 的 --app 模式开一个没有地址栏的独立窗口，关掉窗口就退出程序。

    为什么不直接用 pywebview：它依赖 .NET 运行时（pythonnet），这台机器没有装；
    Chrome 应用模式同样是无地址栏的独立窗口，且有独立任务栏图标。
    """
    browser = _find_browser()
    if not browser:
        import webbrowser

        webbrowser.open(url)
        quit_event.wait()
        return

    # 每次用独立 profile：否则 Chrome 会把新进程当成「已有实例」直接交接后退出，
    # 我们就会误判成「窗口已关闭」而立刻关掉服务（实测踩过）。
    base = Path(__file__).resolve().parents[2] / "tmp" / "appwin-profiles"
    base.mkdir(parents=True, exist_ok=True)
    for old in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
        try:
            import shutil

            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    profile = base / f"p{os.getpid()}-{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(  # noqa: S603
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--window-size=1180,880",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            # 这台机器在受限环境里跑 Chrome，缺了 --no-sandbox 会以 0x80000003 直接退出
            "--no-sandbox",
            "--disable-crashpad",
            "--disable-breakpad",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logging.info("已打开应用窗口（浏览器 PID=%s）", proc.pid)
    try:
        # 窗口关闭 = Chrome 进程结束；也允许界面上的「退出」按钮提前结束
        while proc.poll() is None and not quit_event.is_set():
            time.sleep(0.4)
    finally:
        if proc.poll() is None:
            proc.terminate()
    logging.info("应用窗口已关闭")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="w2d-app", description="公众号文章 → Word 桌面程序")
    parser.add_argument("--port", type=int, default=8865, help="首选端口（被占用时自动换）")
    parser.add_argument("--out", default=None, help="输出目录（默认用上次选择的）")
    parser.add_argument("--check", action="store_true", help="只启动服务并自检，不开窗口")
    parser.add_argument("--no-window", action="store_true", help="只启动服务，用浏览器访问")
    args = parser.parse_args(argv)

    log_file = _setup_logging()
    logging.info("启动 %s，参数=%s", APP_NAME, vars(args))

    from . import web

    httpd = None
    try:
        port = _free_port(args.port)
        httpd = web.make_server(port, args.out)
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        logging.info("服务已就绪 %s，输出目录=%s", url, web.OUT_ROOT)

        if not _wait_ready(url):
            logging.error("服务自检失败：%s 无响应", url)
            return 2

        if args.check:
            with urllib.request.urlopen(url + "/api/history", timeout=5) as r:
                rows = json.loads(r.read().decode("utf-8"))
            print(f"OK {url}  history={len(rows)}  out={web.OUT_ROOT}")
            return 0

        if args.no_window:
            print(f"工作台：{url}\n输出目录：{web.OUT_ROOT}\nCtrl+C 停止")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            return 0

        # ---- 程序窗口
        quit_event = threading.Event()
        web.SHUTDOWN = quit_event
        try:
            import webview  # type: ignore

            logging.info("尝试 WebView2 原生窗口")
            webview.create_window(
                APP_NAME, url, width=1180, height=880, min_size=(900, 620), text_select=True
            )
            webview.start()
            logging.info("原生窗口已关闭")
        except Exception as exc:  # noqa: BLE001 - 没有 .NET/WebView2 就退化
            logging.info("使用浏览器应用模式窗口（原生窗口不可用：%s）", exc)
            web.SHUTDOWN = quit_event
            _open_in_app_window(url, quit_event)
        return 0
    except Exception:  # noqa: BLE001
        logging.exception("启动失败")
        return 1
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            logging.info("服务已停止")
        logging.info("日志文件：%s", log_file)


if __name__ == "__main__":
    sys.exit(main())
