"""
launch.py — single entry point for the CMS.

    python launch.py                 # local, opens browser, live dashboard
    python launch.py --headless      # server/VM (auto-detected under systemd)

This process (always running) owns the background services:
  • resumable upload service   (src/upload_server.py, port CMS_UPLOAD_PORT=8502)
  • YouTube queue supervisor    (restarts the batch worker after reboots/crashes;
                                 a cross-process flock guarantees one worker)
  • Instagram token refresher   (weekly; 6 h backoff after failures)
and runs Streamlit (app.py) as a managed subprocess in its own session.

Why its own session: on Python 3.13, Streamlit sharing our process group got
SIGINT (terminal) + SIGTERM (us) ms apart → re-entrant stdout flush crash.
start_new_session=True ⇒ exactly one signal.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "streamlit.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
REQUIRED_DIRS = ["input_instagram", "input_behance", "input_youtube", "archive",
                 "history", ".queue", ".browser_state", ".secrets", "logs"]


def _preflight() -> list[str]:
    import shutil
    w = []
    if not (ROOT / ".env").exists():
        w.append(".env not found — copy .env.example and fill it in")
    for key, why in [("GEMINI_API_KEY", "AI copy disabled"),
                     ("INSTAGRAM_ACCESS_TOKEN", "Instagram publishing disabled"),
                     ("BEHANCE_EMAIL", "Behance publishing disabled")]:
        if not os.getenv(key):
            w.append(f"{key} not set — {why}")
    if not os.getenv("APP_PASSWORD"):
        w.append("APP_PASSWORD not set — anyone who can reach the ports can use the app")
    if not ((ROOT / ".secrets/youtube_client_secret.json").exists()
            or os.getenv("YOUTUBE_CLIENT_SECRET_FILE")):
        w.append("YouTube client secret missing — see YOUTUBE_SETUP.md")
    if not shutil.which("ffmpeg"):
        w.append("ffmpeg not found — YouTube AI falls back to filenames")
    return w


def _rotate_log() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    except OSError:
        pass


def _launch_streamlit(port: int, bind: str):
    _rotate_log()
    exe = ROOT / ".venv" / ("Scripts/streamlit.exe" if os.name == "nt" else "bin/streamlit")
    cmd = [str(exe) if exe.exists() else "streamlit", "run", "app.py",
           "--server.port", str(port), "--server.headless", "true",
           "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false"]
    if bind:
        cmd += ["--server.address", bind]
    fh = open(LOG_FILE, "a")  # noqa: SIM115
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    kw = dict(cwd=str(ROOT), stdout=fh, stderr=fh, env=env)
    if os.name != "nt":
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw), fh


def _services_loop(stop: threading.Event) -> None:
    """YouTube queue supervisor (30 s) + Instagram token refresh (30 min tick;
    the module itself decides whether a refresh is actually due)."""
    from src.youtube_batch import ensure_worker
    from src.instagram_token import refresh_if_due
    last_ig = 0.0
    while not stop.is_set():
        try:
            ensure_worker()
        except Exception as e:
            print(f"  [supervisor] queue check failed: {e}")
        if time.time() - last_ig > 1800:
            last_ig = time.time()
            try:
                refresh_if_due()
            except Exception as e:
                print(f"  [supervisor] IG token check failed: {e}")
        stop.wait(30)


def _dashboard(url: str, upload_url: str) -> None:
    from src.projects import list_projects
    from src.youtube_batch import counts
    os.system("cls" if os.name == "nt" else "clear")
    print("\n  📱  LOCAL SOCIAL MEDIA CMS — RUNNING\n")
    print(f"  UI        {url}\n  Uploads   {upload_url}\n")
    for p in ("instagram", "behance", "youtube"):
        print(f"  {p:<10} {len(list_projects(p))} project(s) waiting")
    c = counts()
    if c["total"]:
        print(f"\n  YouTube queue: {c['done']}/{c['total']} done · {c['pending']} pending · "
              f"{c['deferred']} waiting reset · {c['failed']} failed")
    print("\n  Ctrl+C to stop.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Local Social Media CMS")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--headless", action="store_true",
                    help="server mode: no browser, no screen-clearing dashboard")
    args = ap.parse_args()
    headless = args.headless or not sys.stdout.isatty()

    for d in REQUIRED_DIRS:
        (ROOT / d).mkdir(parents=True, exist_ok=True)

    print("\n  📱  LOCAL SOCIAL MEDIA CMS\n")
    for w in _preflight():
        print(f"    ⚠  {w}")

    bind = os.getenv("CMS_BIND_ADDRESS", "")
    up_port = int(os.getenv("CMS_UPLOAD_PORT", "8502"))
    host = bind if bind and bind != "0.0.0.0" else "localhost"
    url, upload_url = f"http://{host}:{args.port}", f"http://{host}:{up_port}"

    from src.upload_server import start as start_upload_server
    try:
        start_upload_server(bind, up_port)
    except OSError as e:
        print(f"    ⚠  upload service could not start on port {up_port}: {e}")

    stop = threading.Event()
    threading.Thread(target=_services_loop, args=(stop,), daemon=True,
                     name="services").start()

    proc, fh = _launch_streamlit(args.port, bind)
    time.sleep(2.5)
    if proc.poll() is not None:
        print(f"  ❌ Streamlit failed to start — see {LOG_FILE}")
        sys.exit(1)
    print(f"  ✅ UI {url}   ✅ uploads {upload_url}")
    if not (args.no_browser or headless):
        webbrowser.open(url)

    def _shutdown(*_):
        print("\n  Shutting down…")
        stop.set()
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        fh.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        if proc.poll() is not None:
            print("  ⚠  Streamlit exited — restarting…")
            fh.close()
            proc, fh = _launch_streamlit(args.port, bind)
        if not headless:
            _dashboard(url, upload_url)
        time.sleep(10 if headless else 5)


if __name__ == "__main__":
    main()
