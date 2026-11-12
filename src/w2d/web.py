"""本地 Web 工作台：打开 http://127.0.0.1:8865 就能粘贴链接导出。

刻意不引 FastAPI/uvicorn（本机没有构建链，也不想为一个小工作台多装依赖）：
用标准库 ThreadingHTTPServer + 一个后台线程串行跑导出，前端轮询进度。

安全：只监听 127.0.0.1；下载/打开目录的路径必须落在输出目录内。
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import fetcher
from .pipeline import export

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
OUT_ROOT = Path("output")
SETTINGS = Path(".w2d-config.json")
SHUTDOWN: threading.Event | None = None  # 桌面模式下由 app.py 注入，用于「退出程序」


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(**kw) -> None:
    data = load_settings()
    data.update({k: str(v) for k, v in kw.items() if v is not None})
    try:
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def pick_folder(initial: str = "") -> str | None:
    """弹 Windows 原生「选择文件夹」对话框（需要 STA 线程，所以起一个 PowerShell）。"""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = '选择 Word 保存目录'; "
        f"$d.SelectedPath = '{initial}'; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($d.SelectedPath) }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", script],
            capture_output=True, timeout=180, text=True, encoding="utf-8", errors="replace",
        )
        path = (r.stdout or "").strip()
        return path or None
    except (OSError, subprocess.SubprocessError):
        return None


def set_out(value: str) -> tuple[bool, str]:
    """切换输出目录：校验能否创建，成功后记住并返回新路径。"""
    global OUT_ROOT
    p = Path(value).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".w2d-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return False, f"这个目录无法写入：{exc}"
    OUT_ROOT = p.resolve()
    save_settings(out=str(OUT_ROOT))
    return True, str(OUT_ROOT)


# ---------------------------------------------------------------- 后台任务
def _run_job(job_id: str, urls: list[str], opts: dict) -> None:
    job = JOBS[job_id]
    for i, url in enumerate(urls):
        item = job["items"][i]
        item["status"] = "running"
        try:
            r = export(
                url,
                out_root=OUT_ROOT,
                formats=tuple(opts.get("formats") or ("docx", "html", "json")),
                force=bool(opts.get("force")),
                compress=bool(opts.get("compress")),
                screenshot=not bool(opts.get("no_screenshot")),
                verbose=False,
            )
            item.update(
                status="skipped" if r.get("skipped") else "done",
                title=r.get("title") or "",
                dir=r.get("dir") or "",
                stats=r.get("stats") or {},
                warnings=r.get("warnings") or [],
                docx=_docx_of(r.get("dir")),
            )
        except fetcher.FetchError as exc:
            item.update(status="failed", error=str(exc), hint=exc.hint)
        except Exception as exc:  # noqa: BLE001
            item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    job["state"] = "finished"
    job["finished_at"] = time.time()


def _docx_of(folder: str | None) -> str:
    if not folder:
        return ""
    p = Path(folder) / "article.docx"
    return str(p) if p.exists() else ""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "w2d"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _safe_path(self, rel: str) -> Path | None:
        """只允许输出目录内的路径。"""
        try:
            p = Path(rel).resolve()
            root = OUT_ROOT.resolve()
            if p == root or root in p.parents:
                return p
        except (OSError, RuntimeError):
            return None
        return None

    # ---- GET
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            html = PAGE.encode("utf-8")
            self._send(200, html, "text/html; charset=utf-8")
            return

        if u.path == "/api/job":
            job = JOBS.get((q.get("id") or [""])[0])
            self._json(job or {"error": "job not found"}, 200 if job else 404)
            return

        if u.path == "/api/settings":
            self._json({"out": str(OUT_ROOT)})
            return

        if u.path == "/api/pick-folder":
            chosen = pick_folder(str(OUT_ROOT))
            if not chosen:
                self._json({"cancelled": True, "out": str(OUT_ROOT)})
                return
            ok, msg = set_out(chosen)
            self._json({"out": msg} if ok else {"error": msg}, 200 if ok else 400)
            return

        if u.path == "/api/history":
            self._json(self._history())
            return

        if u.path == "/api/file":
            p = self._safe_path((q.get("path") or [""])[0])
            if p is None or not p.is_file():
                self._json({"error": "文件不存在或越界"}, 404)
                return
            data = p.read_bytes()
            name = quote(p.name)
            self._send(
                200, data,
                mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
            )
            return

        if u.path == "/api/open":
            p = self._safe_path((q.get("path") or [""])[0])
            if p is None or not p.exists():
                self._json({"error": "目录不存在或越界"}, 404)
                return
            target = p if p.is_dir() else p.parent
            try:
                subprocess.Popen(["explorer", str(target)])  # noqa: S603,S607
                self._json({"ok": True})
            except OSError as exc:
                self._json({"error": str(exc)}, 500)
            return

        self._json({"error": "not found"}, 404)

    def _history(self) -> list[dict]:
        rows: list[dict] = []
        if not OUT_ROOT.exists():
            return rows
        for mf in OUT_ROOT.glob("*/manifest.json"):
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            docx = mf.parent / "article.docx"
            rows.append(
                {
                    "title": m.get("title") or mf.parent.name,
                    "account": m.get("account") or "",
                    "publish_time": m.get("publish_time") or "",
                    "dir": str(mf.parent),
                    "docx": str(docx) if docx.exists() else "",
                    "mb": round(docx.stat().st_size / 1048576, 2) if docx.exists() else 0,
                    "mtime": mf.stat().st_mtime,
                    "images": (m.get("stats") or {}).get("images", 0),
                }
            )
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return rows[:100]

    # ---- POST
    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route == "/api/quit":
            if SHUTDOWN is not None:
                SHUTDOWN.set()
                self._json({"ok": True, "message": "程序正在退出"})
            else:
                self._json({"error": "命令行模式下请按 Ctrl+C 停止"}, 400)
            return
        if route == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8-sig") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._json({"error": "请求体不是合法 JSON"}, 400)
                return
            ok, msg = set_out(str(payload.get("out") or ""))
            self._json({"out": msg} if ok else {"error": msg}, 200 if ok else 400)
            return
        if route != "/api/export":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            # utf-8-sig：兼容带 BOM 的请求体（PowerShell / 某些客户端会加）
            payload = json.loads(self.rfile.read(length).decode("utf-8-sig") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        urls = [u.strip() for u in str(payload.get("urls", "")).splitlines() if u.strip()]
        urls = [u for u in urls if not u.startswith("#")]
        if not urls:
            self._json({"error": "请至少填写一个公众号文章链接"}, 400)
            return
        if len(urls) > 50:
            self._json({"error": "一次最多 50 篇"}, 400)
            return

        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "state": "running",
            "started_at": time.time(),
            "items": [
                {"url": u, "status": "pending", "title": "", "dir": "", "error": "", "docx": ""}
                for u in urls
            ],
        }
        with LOCK:
            JOBS[job_id] = job
        threading.Thread(target=_run_job, args=(job_id, urls, payload), daemon=True).start()
        self._json({"job": job_id, "count": len(urls)})


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>公众号文章 → Word 工作台</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:28px 18px 60px;background:#f5f6f8;color:#222;
 font-family:-apple-system,"Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#888;font-size:13px;margin-bottom:20px}
.card{background:#fff;border-radius:10px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px}
textarea{width:100%;min-height:110px;padding:10px;border:1px solid #dcdfe6;border-radius:8px;
 font-family:Consolas,monospace;font-size:13px;resize:vertical;outline:none}
textarea:focus{border-color:#576b95}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:12px}
label{display:flex;gap:6px;align-items:center;color:#555;cursor:pointer}
button{padding:9px 20px;border:0;border-radius:8px;background:#576b95;color:#fff;
 font-size:14px;cursor:pointer}
button:disabled{background:#aab;cursor:not-allowed}
button.ghost{background:#eef1f6;color:#576b95}
.item{border:1px solid #eee;border-radius:8px;padding:12px;margin-bottom:10px;background:#fff}
.item .u{font-size:12px;color:#999;word-break:break-all}
.item .t{font-weight:600;margin:2px 0 4px}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;margin-right:8px}
.pending{background:#f0f0f0;color:#777}
.running{background:#e8f0fe;color:#1a56db}
.done{background:#e7f7ee;color:#0f7b47}
.skipped{background:#fff4e5;color:#a45f00}
.failed{background:#fdecec;color:#c0392b}
.mini{font-size:12px;color:#888}
.warn{font-size:12px;color:#a45f00;margin-top:6px}
a{color:#576b95}
.hist{max-height:320px;overflow:auto}
.hrow{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid #f0f0f0}
.hrow .h{font-size:13px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body><div class="wrap">
<h1>公众号文章 → Word</h1>
<div class="sub">粘贴文章链接（每行一个）→ 导出 docx，图片全部内嵌、断网可看。全程本机运行。</div>

<div class="card">
  <textarea id="urls" placeholder="https://mp.weixin.qq.com/s/xxxxxxxx&#10;https://mp.weixin.qq.com/s/yyyyyyyy"></textarea>
  <div class="row">
    <button id="go">开始导出</button>
    <button id="clear" class="ghost">清空</button>
    <label><input type="checkbox" id="compress"> 压缩图片（GIF 转静态、体积小很多）</label>
    <label><input type="checkbox" id="force"> 强制重跑</label>
  </div>
  <div class="row" style="margin-top:10px">
    <span style="color:#555">保存到</span>
    <input id="outdir" readonly style="flex:1;min-width:220px;padding:7px 10px;border:1px solid #dcdfe6;border-radius:8px;font-size:12px;color:#555;background:#fafbfc">
    <button class="ghost" id="pick">选择文件夹…</button>
  </div>
</div>

<div id="result"></div>

<div class="card">
  <div class="row" style="margin:0 0 8px"><b>已导出</b><span class="mini" id="hcount"></span>
    <button class="ghost" id="refresh" style="margin-left:auto;padding:5px 12px">刷新</button></div>
  <div class="hist" id="hist"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let timer = null;

function el(tag, cls, text){ const d=document.createElement(tag); if(cls)d.className=cls; if(text!=null)d.textContent=text; return d; }

function render(job){
  const box = $('result');
  box.innerHTML = '';
  const card = el('div','card');
  const done = job.items.filter(i=>['done','skipped','failed'].includes(i.status)).length;
  card.appendChild(el('div','sub', `任务 ${job.id} · ${job.state==='finished'?'已完成':'进行中'} ${done}/${job.items.length}`));
  job.items.forEach(it=>{
    const d = el('div','item');
    const tag = el('span','tag '+it.status, {pending:'排队',running:'导出中',done:'完成',skipped:'已存在',failed:'失败'}[it.status]||it.status);
    d.appendChild(tag);
    if(it.title) d.appendChild(el('span','t',it.title));
    if(it.error) d.appendChild(el('div','warn','✗ '+it.error + (it.hint? '　建议：'+it.hint : '')));
    d.appendChild(el('div','u',it.url));
    if(it.docx){
      const row = el('div','row');
      const a = el('a',null,'下载 Word'); a.href = '/api/file?path='+encodeURIComponent(it.docx); row.appendChild(a);
      const b = el('button','ghost','打开所在文件夹'); b.style.padding='5px 12px';
      b.onclick = ()=> fetch('/api/open?path='+encodeURIComponent(it.dir));
      row.appendChild(b);
      if(it.stats && it.stats.images!=null) row.appendChild(el('span','mini', `图 ${it.stats.assets_ok}/${it.stats.images} · 表 ${it.stats.tables} · ${it.stats.chars} 字`));
      d.appendChild(row);
    }
    (it.warnings||[]).forEach(w=> d.appendChild(el('div','warn','⚠ '+w)));
    card.appendChild(d);
  });
  box.appendChild(card);
}

async function poll(jobId){
  const r = await fetch('/api/job?id='+jobId).then(r=>r.json());
  if(r.error){ clearInterval(timer); return; }
  render(r);
  if(r.state==='finished'){ clearInterval(timer); timer=null; $('go').disabled=false; loadHistory(); }
}

$('go').onclick = async ()=>{
  const urls = $('urls').value.trim();
  if(!urls){ alert('请粘贴至少一个公众号文章链接'); return; }
  $('go').disabled = true;
  const res = await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({urls, compress:$('compress').checked, force:$('force').checked})}).then(r=>r.json());
  if(res.error){ alert(res.error); $('go').disabled=false; return; }
  if(timer) clearInterval(timer);
  timer = setInterval(()=>poll(res.job), 900);
  poll(res.job);
};
$('clear').onclick = ()=>{ $('urls').value=''; $('result').innerHTML=''; };
$('refresh').onclick = loadHistory;

async function loadSettings(){
  try{ const s = await fetch('/api/settings').then(r=>r.json()); $('outdir').value = s.out || ''; }catch(e){}
}
$('pick').onclick = async ()=>{
  const b = $('pick'); const old = b.textContent;
  b.disabled = true; b.textContent = '等待选择…';
  try{
    const s = await fetch('/api/pick-folder').then(r=>r.json());
    if(s.error){ alert(s.error); }
    else if(s.out){ $('outdir').value = s.out; loadHistory(); }
  }catch(e){ alert('选择目录失败：'+e); }
  b.disabled = false; b.textContent = old;
};

async function loadHistory(){
  const rows = await fetch('/api/history').then(r=>r.json());
  $('hcount').textContent = `（${rows.length} 篇）`;
  const box = $('hist'); box.innerHTML='';
  if(!rows.length){ box.appendChild(el('div','mini','还没有导出记录')); return; }
  rows.forEach(r=>{
    const d = el('div','hrow');
    const left = el('div'); left.style.flex='1'; left.style.minWidth='0';
    left.appendChild(el('div','h', r.title));
    left.appendChild(el('div','mini', `${r.account||'—'} · ${r.publish_time||'—'} · 图 ${r.images} · ${r.mb} MB`));
    d.appendChild(left);
    if(r.docx){ const a=el('a',null,'下载'); a.href='/api/file?path='+encodeURIComponent(r.docx); d.appendChild(a); }
    const b=el('a',null,'文件夹'); b.href='javascript:void 0'; b.onclick=()=>fetch('/api/open?path='+encodeURIComponent(r.dir)); d.appendChild(b);
    box.appendChild(d);
  });
}
loadHistory();
loadSettings();
</script></div></body></html>
"""


def init_out(out: Path | str | None = None) -> Path:
    """确定并创建输出目录（命令行指定 > 上次选择 > 默认 output）。"""
    global OUT_ROOT
    if out is not None:
        OUT_ROOT = Path(out).resolve()
    else:
        OUT_ROOT = Path(load_settings().get("out") or "output").resolve()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    return OUT_ROOT


def make_server(port: int = 8865, out: Path | str | None = None) -> ThreadingHTTPServer:
    """只建服务、不阻塞，供桌面窗口与自检复用。"""
    init_out(out)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int = 8865, out: Path | None = None) -> None:
    srv = make_server(port, out)
    print(f"工作台已启动：http://127.0.0.1:{srv.server_address[1]}")
    print(f"输出目录：{OUT_ROOT}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
