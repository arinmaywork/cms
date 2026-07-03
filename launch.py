"""
launch.py
─────────────────────────────────────────────────────────────────────────────
The single entry point for the entire CMS.

Run it:
    python launch.py                  # default port 8501
    python launch.py --port 8888      # custom port
    python launch.py --no-browser     # skip auto-open

What it does
────────────
1. Validates that .env exists and GEMINI_API_KEY is set.
2. Creates all required directories (.queue/, input_*, history/, .browser_state/).
3. Starts the watchdog observer (in THIS process, on a background thread).
4. Launches `streamlit run app.py` as a managed subprocess.
5. Opens the browser automatically (unless --no-browser).
6. Runs a live terminal dashboard showing:
   - Watcher status (which folders are being monitored)
   - A log of every detected project (platform, name, image count, time)
   - Keyboard shortcuts
7. On Ctrl+C  → stops Streamlit subprocess, stops watcher, clears queue files,
                 prints a clean exit message.

IPC architecture
────────────────
  launch.py (this process)
    └── watcher thread   →  writes to  .queue/instagram.json
                                       .queue/behance.json
  streamlit subprocess
    └── app.py           →  reads from .queue/instagram.json  (via file_queue.pop_all)
                                       .queue/behance.json
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# ── Ensure project root is importable ─────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.watcher import start_watcher, scan_once
from src.file_queue import clear_all as fq_clear_all, peek as fq_peek


# ── ANSI colour helpers (Windows-safe via colorama if available) ───────────────
try:
    from colorama import init as _cinit, Fore, Style
    _cinit(autoreset=True)
    def _c(text: str, code: str) -> str:
        return code + text + Style.RESET_ALL
except ImportError:
    class Fore:  # type: ignore[no-redef]
        GREEN = CYAN = YELLOW = RED = MAGENTA = WHITE = ""
    class Style:  # type: ignore[no-redef]
        RESET_ALL = BRIGHT = ""
    def _c(text: str, code: str) -> str:
        return text

def green(t: str)   -> str: return _c(t, Fore.GREEN)
def cyan(t: str)    -> str: return _c(t, Fore.CYAN)
def yellow(t: str)  -> str: return _c(t, Fore.YELLOW)
def red(t: str)     -> str: return _c(t, Fore.RED)
def bold(t: str)    -> str: return _c(t, Style.BRIGHT)
def magenta(t: str) -> str: return _c(t, Fore.MAGENTA)


# ── Required directories ──────────────────────────────────────────────────────
REQUIRED_DIRS = [
    ROOT / "input_instagram",
    ROOT / "input_behance",
    ROOT / "input_youtube",
    ROOT / "history",
    ROOT / ".queue",
    ROOT / ".browser_state",
    ROOT / ".secrets",
]


# ── Pre-flight checks ─────────────────────────────────────────────────────────
def _preflight() -> list[str]:
    """Return a list of warning strings (empty = all good)."""
    warnings: list[str] = []

    env_file = ROOT / ".env"
    if not env_file.exists():
        warnings.append(".env file not found — copy .env.example to .env and fill in credentials.")
    else:
        if not os.getenv("GEMINI_API_KEY"):
            warnings.append("GEMINI_API_KEY is not set in .env  →  get a free key at https://aistudio.google.com/apikey")
        if not os.getenv("INSTAGRAM_ACCESS_TOKEN"):
            warnings.append("INSTAGRAM_ACCESS_TOKEN is not set (Instagram publishing will fail)")
        if not os.getenv("BEHANCE_EMAIL"):
            warnings.append("BEHANCE_EMAIL is not set (Behance publishing will fail)")
        if not os.getenv("APP_PASSWORD"):
            warnings.append("APP_PASSWORD is not set — anyone who can reach the port can use the app "
                            "(fine locally; set it before exposing on a VM)")

    yt_secret = ROOT / ".secrets" / "youtube_client_secret.json"
    if not (yt_secret.exists() or os.getenv("YOUTUBE_CLIENT_SECRET_FILE")):
        warnings.append("YouTube client secret not found — see YOUTUBE_SETUP.md "
                        "(YouTube publishing disabled until then)")

    import shutil as _sh
    if not _sh.which("ffmpeg"):
        warnings.append("ffmpeg not found — YouTube AI will work from filename only "
                        "and frame/thumbnail extraction is disabled "
                        "(install: brew install ffmpeg / apt install ffmpeg)")

    return warnings


# ── Event log ─────────────────────────────────────────────────────────────────
_event_log: list[tuple[str, str, str, int]] = []   # (time, platform, name, count)
_MAX_LOG = 30


def _on_detected(platform: str, folder: Path, image_count: int) -> None:
    """Callback from watcher thread — appends to the event log."""
    ts = datetime.now().strftime("%H:%M:%S")
    _event_log.append((ts, platform, folder.name, image_count))
    if len(_event_log) > _MAX_LOG:
        _event_log.pop(0)
    # Print inline (will be visible between dashboard redraws)
    icon = {"instagram": "📸", "behance": "🎨", "youtube": "▶️"}.get(platform, "📁")
    print(
        f"\n  {icon}  {cyan(ts)}  "
        f"{bold(folder.name)}  [{platform}]  "
        f"{yellow(str(image_count))} image(s)  → queued for UI"
    )


# ── Dashboard renderer ────────────────────────────────────────────────────────
def _banner() -> str:
    lines = [
        "",
        bold("╔══════════════════════════════════════════════════════╗"),
        bold("║") + magenta("        📱  LOCAL SOCIAL MEDIA CMS  — RUNNING        ") + bold("║"),
        bold("╚══════════════════════════════════════════════════════╝"),
    ]
    return "\n".join(lines)


def _render_dashboard(url: str, observer_alive: bool) -> None:
    os.system("cls" if os.name == "nt" else "clear")
    print(_banner())
    print()

    # Status row
    w_status = green("● ACTIVE") if observer_alive else red("○ STOPPED")
    print(f"  Watcher   {w_status}")
    print(f"  UI        {green('● ' + url)}")
    print()

    # Watched folders
    print(f"  {bold('Watching:')}")
    print(f"    📸  {cyan(str(ROOT / 'input_instagram'))}")
    print(f"    🎨  {cyan(str(ROOT / 'input_behance'))}")
    print(f"    ▶️  {cyan(str(ROOT / 'input_youtube'))}")
    print()

    # Pending queue lengths
    ig_q = fq_peek("instagram")
    bh_q = fq_peek("behance")
    yt_q = fq_peek("youtube")
    if ig_q or bh_q or yt_q:
        print(f"  {bold('Pending (not yet opened in UI):')}")
        if ig_q:
            print(f"    📸  Instagram: {yellow(str(len(ig_q)))} project(s)")
        if bh_q:
            print(f"    🎨  Behance:   {yellow(str(len(bh_q)))} project(s)")
        if yt_q:
            print(f"    ▶️  YouTube:   {yellow(str(len(yt_q)))} project(s)")
        print()

    # Recent events
    if _event_log:
        print(f"  {bold('Recent detections:')}")
        for ts, platform, name, count in reversed(_event_log[-8:]):
            icon = {"instagram": "📸", "behance": "🎨", "youtube": "▶️"}.get(platform, "📁")
            print(f"    {icon}  {cyan(ts)}  {bold(name)}  "
                  f"[{platform}]  {yellow(str(count))} image(s)")
        print()
    else:
        print(f"  {bold('Recent detections:')}  {yellow('none yet')}")
        print(f"  {yellow('→  Drop a project folder into one of the watched directories above.')}")
        print()

    # Manual scan hint
    print(f"  {bold('Tip:')} If a folder isn't detected, type {cyan('s')} + Enter to scan manually.")
    print()
    # Controls
    print(f"  {bold('Controls:')}")
    print(f"    Ctrl+C  — stop watcher and Streamlit")
    print()


# ── Subprocess management ─────────────────────────────────────────────────────
def _find_streamlit() -> str:
    """Return the streamlit executable path inside the active venv."""
    venv_scripts = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    candidate    = venv_scripts / ("streamlit.exe" if os.name == "nt" else "streamlit")
    if candidate.exists():
        return str(candidate)
    # Fallback: hope it's on PATH
    return "streamlit"


def _launch_streamlit(port: int) -> tuple[subprocess.Popen, "IO[str]"]:
    streamlit_exe = _find_streamlit()
    cmd = [
        streamlit_exe, "run", "app.py",
        "--server.port", str(port),
        "--server.headless", "true",
        "--server.fileWatcherType", "none",
        "--browser.gatherUsageStats", "false",
    ]

    # Create logs directory if it doesn't exist
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_fh = open(log_dir / "streamlit.log", "a")  # noqa: SIM115

    # ── Environment: keep PYTHONUNBUFFERED as a secondary defence ────────────
    # Even though it doesn't fully solve the re-entrancy issue (Streamlit
    # reinitialises sys.stdout after startup), it removes one layer of
    # buffering and speeds up I/O to the log file.
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # ── Process isolation: the real fix for the double-signal crash ───────────
    #
    # ROOT CAUSE (Python 3.13 + macOS):
    #   Without start_new_session, Streamlit inherits launch.py's process group.
    #   When SIGINT reaches the terminal (Ctrl+C, tab close, st_autorefresh
    #   self-interrupt, etc.) the OS fans it out to EVERY process in the group,
    #   so Streamlit gets SIGINT directly.  At the same moment, _shutdown() also
    #   sends SIGTERM to Streamlit.  Streamlit now has TWO signals queued in
    #   milliseconds.  Both fire bootstrap.py:signal_handler() → server.stop()
    #   → click.secho("Stopping…") → sys.stdout.flush().  Signal #1 starts
    #   flushing; signal #2 arrives mid-flush on the SAME thread and re-enters
    #   the same BufferedWriter.  Python 3.13 raises:
    #     RuntimeError: reentrant call inside <_io.BufferedWriter name='<stdout>'>
    #   (Python ≤3.12 silently corrupted the write instead of crashing.)
    #
    # FIX: start_new_session=True puts Streamlit in its own OS session/group.
    #   Terminal signals never reach it.  _shutdown() sends exactly ONE SIGTERM.
    #   One signal → one signal_handler call → no re-entrancy.
    #
    # Windows note: start_new_session is not supported on Windows; the existing
    #   proc.terminate() path in _shutdown() is correct there.
    popen_kwargs: dict = dict(
        cwd=str(ROOT),
        stdout=log_fh,
        stderr=log_fh,
        env=env,
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    return proc, log_fh


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Local Social Media CMS launcher")
    parser.add_argument("--port",       type=int, default=8501, help="Streamlit port (default: 8501)")
    parser.add_argument("--no-browser", action="store_true",    help="Don't open browser automatically")
    parser.add_argument("--headless",   action="store_true",
                        help="Server mode (VM/systemd): no browser, no live terminal dashboard")
    args = parser.parse_args()

    # Auto-detect non-interactive environments (systemd, nohup, docker)
    headless = args.headless or not sys.stdout.isatty()
    if headless:
        args.no_browser = True

    url = f"http://localhost:{args.port}"

    # ── Create directories ────────────────────────────────────────────────────
    for d in REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    # ── Pre-flight ────────────────────────────────────────────────────────────
    warnings = _preflight()

    print(_banner())
    print()

    if warnings:
        print(f"  {bold('⚠  Warnings:')} (app will still launch — fix before publishing)")
        for w in warnings:
            print(f"    {yellow('→')} {w}")
        print()

    # ── Clear stale queue files from a previous run ───────────────────────────
    fq_clear_all()

    # ── Start watcher ─────────────────────────────────────────────────────────
    print(f"  {bold('Starting file watcher…')}", end="", flush=True)
    observer = start_watcher(on_detected=_on_detected)
    print(f"  {green('done')}")

    # ── Start Streamlit ───────────────────────────────────────────────────────
    print(f"  {bold('Starting Streamlit on')} {cyan(url)} …", end="", flush=True)
    streamlit_proc, streamlit_log_fh = _launch_streamlit(args.port)
    time.sleep(2.5)   # give Streamlit time to bind the port

    if streamlit_proc.poll() is not None:
        print(f"\n  {red('Streamlit failed to start.')} Check that all requirements are installed.")
        observer.stop()
        sys.exit(1)

    print(f"  {green('done')}")

    # ── Open browser ──────────────────────────────────────────────────────────
    if not args.no_browser:
        print(f"  {bold('Opening browser…')}", end="", flush=True)
        time.sleep(0.5)
        webbrowser.open(url)
        print(f"  {green('done')}")

    print()
    print(f"  {green('Everything is running.')}  Press {bold('Ctrl+C')} to stop.")
    time.sleep(1.5)

    # ── Live dashboard loop ───────────────────────────────────────────────────
    def _shutdown(sig=None, frame=None):
        print(f"\n\n  {yellow('Shutting down…')}")
        try:
            observer.stop()
            observer.join(timeout=3)
        except Exception:
            pass
        try:
            if os.name == "nt":
                # Windows: terminate the process directly
                streamlit_proc.terminate()
            else:
                # Unix: Streamlit runs in its own process group (start_new_session).
                # Kill the whole group so Playwright subprocesses also get cleaned up.
                try:
                    os.killpg(os.getpgid(streamlit_proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass  # process already gone
            streamlit_proc.wait(timeout=5)
        except Exception:
            try:
                streamlit_proc.kill()
            except Exception:
                pass
        try:
            streamlit_log_fh.close()
        except Exception:
            pass
        fq_clear_all()
        print(f"  {green('Stopped cleanly.')}  Goodbye 👋\n")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            # Restart Streamlit if it crashes unexpectedly
            if streamlit_proc.poll() is not None:
                print(f"\n  {yellow('Streamlit exited unexpectedly — restarting…')}")
                # Close the old log file handle before opening a new one
                try:
                    streamlit_log_fh.close()
                except Exception:
                    pass
                streamlit_proc, streamlit_log_fh = _launch_streamlit(args.port)
                time.sleep(2)

            if headless:
                time.sleep(10)   # no screen-clearing dashboard in server mode
            else:
                _render_dashboard(url, observer.is_alive())
                time.sleep(3)

    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
