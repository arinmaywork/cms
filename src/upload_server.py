"""
src/upload_server.py — resumable, chunked upload service (stdlib only).

Why: Streamlit's st.file_uploader sends each file as ONE request, buffers it
in server RAM, and loses everything when the websocket hiccups or the page
reruns. On a 1 GB VM over a phone/Wi-Fi link that fails constantly.

This service (port CMS_UPLOAD_PORT, default 8502):
  • writes 8 MB chunks straight to disk as <file>.part (no RAM buffering)
  • the browser asks the server how many bytes it already has and resumes
    from there — network drops retry with backoff, a closed tab resumes
    when the same files are re-selected, finished files are skipped
  • renames .part → final only when complete, so the CMS never sees a
    half-written file
  • protected by APP_PASSWORD (X-CMS-Key header) when set
"""

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.projects import INPUT_DIRS, PLATFORM_EXTS, safe_name

MAX_CHUNK = 64 * 1024 * 1024
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(p: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(p), threading.Lock())


def _allowed(platform: str) -> set[str]:
    exts = set(PLATFORM_EXTS[platform])
    if platform == "youtube":
        exts |= {".jpg", ".jpeg", ".png"}      # thumbnails
    return exts


class _Handler(BaseHTTPRequestHandler):
    server_version = "CMSUpload/1"

    def log_message(self, *a):   # keep journald quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _auth(self) -> bool:
        pw = os.getenv("APP_PASSWORD", "")
        return not pw or hmac.compare_digest(self.headers.get("X-CMS-Key", ""), pw)

    def _target(self, q: dict) -> Path:
        platform = q.get("platform", [""])[0]
        if platform not in INPUT_DIRS:
            raise ValueError("bad platform")
        project = safe_name(q.get("project", [""])[0], "")
        name = safe_name(q.get("name", [""])[0], "")
        if not project or not name:
            raise ValueError("bad project or file name")
        if Path(name).suffix.lower() not in _allowed(platform):
            raise ValueError(f"file type not allowed: {name}")
        return INPUT_DIRS[platform] / project / name

    def _drain(self, n: int) -> None:
        while n > 0:
            got = self.rfile.read(min(1_048_576, n))
            if not got:
                break
            n -= len(got)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
        if u.path != "/api/status":
            return self._json(404, {"error": "not found"})
        if not self._auth():
            return self._json(401, {"error": "Wrong password"})
        try:
            t = self._target(parse_qs(u.query))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        part = t.with_name(t.name + ".part")
        if t.exists():
            return self._json(200, {"size": t.stat().st_size, "complete": True})
        return self._json(200, {"size": part.stat().st_size if part.exists() else 0,
                                "complete": False})

    def do_PUT(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        if u.path != "/api/chunk":
            self._drain(length)
            return self._json(404, {"error": "not found"})
        if not self._auth():
            self._drain(length)
            return self._json(401, {"error": "Wrong password"})
        q = parse_qs(u.query)
        try:
            t = self._target(q)
            offset, total = int(q["offset"][0]), int(q["total"][0])
        except (ValueError, KeyError) as e:
            self._drain(length)
            return self._json(400, {"error": f"bad request: {e}"})
        if length <= 0 or length > MAX_CHUNK or offset + length > total:
            self._drain(length)
            return self._json(400, {"error": "bad chunk size"})

        with _lock_for(t):
            t.parent.mkdir(parents=True, exist_ok=True)
            part = t.with_name(t.name + ".part")
            if t.exists() and t.stat().st_size == total:
                self._drain(length)
                return self._json(200, {"size": total, "complete": True})
            cur = part.stat().st_size if part.exists() else 0
            if offset != cur:                       # client out of sync → tell it where we are
                self._drain(length)
                return self._json(409, {"size": cur})
            remaining = length
            with open(part, "ab") as f:
                while remaining > 0:
                    got = self.rfile.read(min(1_048_576, remaining))
                    if not got:
                        break
                    f.write(got)
                    remaining -= len(got)
                f.flush()
                os.fsync(f.fileno())
            if remaining:                           # connection died mid-chunk → roll back
                os.truncate(part, cur)
                return self._json(400, {"error": "incomplete chunk", "size": cur})
            new = cur + length
            if new == total:
                os.replace(part, t)
            return self._json(200, {"size": new, "complete": new == total})


def start(host: str = "", port: int = 8502) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host or "0.0.0.0", port), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="upload-server").start()
    print(f"  [upload] resumable upload service on {host or '0.0.0.0'}:{port}")
    return srv


_PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>CMS Upload</title>
<style>body{font-family:system-ui,sans-serif;background:#0f0f1a;color:#e2e8f0;max-width:760px;margin:auto;padding:16px}
label{display:block;margin-top:8px;font-size:14px;color:#aaa}
input,select,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #333;background:#1a1a2e;color:#e2e8f0;width:100%;box-sizing:border-box;margin:4px 0}
button{background:#8b5cf6;border:0;cursor:pointer;margin-top:12px}button:disabled{opacity:.5}
.f{background:#1a1a2e;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:14px;word-break:break-all}
.bar{height:6px;background:#333;border-radius:3px;overflow:hidden;margin:6px 0 2px}.bar>i{display:block;height:100%;background:#8b5cf6;width:0}
.ok .bar>i{background:#2ecc71}.err{color:#e74c3c}small{color:#999}#sum{margin:12px 0;font-weight:600}</style></head><body>
<h2>⬆️ Upload to CMS</h2>
<small>Files stream to the server in 8&nbsp;MB chunks. Network drops are retried automatically. If this page closes, open it again and select the same files — whatever already arrived is skipped.</small>
<label>Platform<select id=pf><option value=youtube>YouTube</option><option value=instagram>Instagram</option><option value=behance>Behance</option></select></label>
<label>Project folder name<input id=pj placeholder="e.g. integrated-photonics"></label>
<label>Password<input id=key type=password placeholder="APP_PASSWORD (leave empty if not set)"></label>
<label>Files<input id=files type=file multiple></label>
<button id=go>Upload</button><div id=sum></div><div id=list></div>
<script>
const $=i=>document.getElementById(i),CH=8*1048576,q=new URLSearchParams(location.search),sleep=ms=>new Promise(r=>setTimeout(r,ms));
if(q.get('platform'))$('pf').value=q.get('platform');if(q.get('project'))$('pj').value=q.get('project');
$('key').value=localStorage.getItem('cmsk')||'';
const u=(p,f,x)=>`/api/${p}?platform=${encodeURIComponent($('pf').value)}&project=${encodeURIComponent($('pj').value.trim())}&name=${encodeURIComponent(f.name)}`+(x||'');
async function call(m,url,body){const r=await fetch(url,{method:m,body,headers:{'X-CMS-Key':$('key').value}});return{s:r.status,j:await r.json().catch(()=>({}))}}
const mb=n=>(n/1048576).toFixed(1);
async function up(f,row){const bar=row.querySelector('i'),st=row.querySelector('small');let tries=0;
 for(;;){try{
  let r=await call('GET',u('status',f));if(r.s==401||r.s==400)throw{fatal:r.j.error};if(r.s!=200)throw new Error('HTTP '+r.s);
  let off=r.j.size;if(r.j.complete){bar.style.width='100%';st.textContent='already on server ✓';row.className='f ok';return true}
  while(off<f.size){const c=await call('PUT',u('chunk',f,`&offset=${off}&total=${f.size}`),f.slice(off,Math.min(off+CH,f.size)));
   if(c.s==409){off=c.j.size;continue}if(c.s==401||(c.s==400&&!('size' in c.j)))throw{fatal:c.j.error};if(c.s!=200)throw new Error('HTTP '+c.s);
   off=c.j.size;tries=0;bar.style.width=(100*off/f.size).toFixed(1)+'%';st.textContent=`${mb(off)} / ${mb(f.size)} MB`}
  row.className='f ok';st.textContent='done ✓';return true
 }catch(e){if(e.fatal){st.innerHTML='<span class=err>'+e.fatal+'</span>';return false}
  tries++;const w=Math.min(30,2**tries);st.textContent=`connection issue — retrying in ${w}s (attempt ${tries})`;await sleep(w*1000)}}}
let wl=null;
$('go').onclick=async()=>{const fs=[...$('files').files];if(!fs.length||!$('pj').value.trim())return alert('Enter a project name and pick files');
 localStorage.setItem('cmsk',$('key').value);$('go').disabled=true;$('list').innerHTML='';
 try{wl=await navigator.wakeLock.request('screen')}catch(e){}
 fs.sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true}));
 const rows=fs.map(f=>{const d=document.createElement('div');d.className='f';d.innerHTML='<div></div><div class=bar><i></i></div><small>waiting</small>';d.firstChild.textContent=f.name;$('list').append(d);return d});
 let ok=0;for(let i=0;i<fs.length;i++){$('sum').textContent=`Uploading file ${i+1} of ${fs.length}…`;if(await up(fs[i],rows[i]))ok++}
 $('sum').textContent=`✅ ${ok}/${fs.length} files on the server. Open the CMS → pick project “${$('pj').value.trim()}”.`;
 $('go').disabled=false;if(wl)wl.release()};
window.onbeforeunload=()=>$('go').disabled?'Upload in progress':undefined;
</script></body></html>"""
