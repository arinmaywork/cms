"""
src/behance_login.py — Behance (Adobe ID) login driven from the web UI.

A headless Chromium on the server walks the Adobe sign-in pages step by step
(email → password → optional verification code → optional prompts) and
saves the Playwright session to .browser_state/behance_state.json.
The UI watches progress through a status file + live screenshot, and
supplies a verification code through a hand-off file when Adobe asks.

If Adobe shows a human check (CAPTCHA) it stops and says so — use
"Upload session file" instead (we never try to bypass those).

Also: test_session() — checks a saved session still opens the editor.
"""

import asyncio
import threading
import time
from pathlib import Path

from src.fsutil import atomic_write_json, load_json

ROOT       = Path(__file__).resolve().parent.parent
STATE_DIR  = ROOT / ".browser_state"
STATE_FILE = STATE_DIR / "behance_state.json"
STATUS     = ROOT / ".queue" / "bh_login.json"
CODE_FILE  = ROOT / ".queue" / "bh_login_code.txt"
SHOT       = STATE_DIR / "login_live.png"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
_lock = threading.Lock()


def _set(status: str, message: str = "", **extra) -> None:
    atomic_write_json(STATUS, {"status": status, "message": message,
                               "ts": time.time(), **extra})


def read_status() -> dict:
    s = load_json(STATUS, {})
    return s if isinstance(s, dict) else {}


def busy() -> bool:
    s = read_status()
    return s.get("status") in ("running", "need_code", "testing") \
        and time.time() - s.get("ts", 0) < 900


def submit_code(code: str) -> None:
    CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CODE_FILE.write_text(code.strip(), encoding="utf-8")


def save_uploaded_state(raw: bytes) -> tuple[bool, str]:
    """Validate + store a behance_state.json produced on another computer."""
    import json
    try:
        data = json.loads(raw.decode("utf-8"))
        domains = {c.get("domain", "") for c in data.get("cookies", [])}
    except Exception as e:
        return False, f"Not a valid Playwright storage-state JSON: {e}"
    if not any("behance" in d or "adobe" in d for d in domains):
        return False, "File has no Behance/Adobe cookies — export it after logging in."
    atomic_write_json(STATE_FILE, data, mode=0o600)
    return True, f"Session saved ({len(data.get('cookies', []))} cookies)."


# ── Browser helpers ───────────────────────────────────────────────────────────

async def _visible(page, selector: str):
    for el in await page.query_selector_all(selector):
        try:
            if await el.is_visible():
                return el
        except Exception:
            pass
    return None


async def _click_text(page, *labels: str) -> bool:
    for label in labels:
        for sel in (f'button:has-text("{label}")', f'[role="button"]:has-text("{label}")',
                    f'a:has-text("{label}")'):
            el = await _visible(page, sel)
            if el:
                await el.click()
                return True
    return False


async def _logged_in(page) -> bool:
    from src.behance_publisher import EDITOR_URL, _is_logged_in
    await page.goto(EDITOR_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    return "behance.net" in page.url and "adobe" not in page.url and await _is_logged_in(page)


# ── Login flow ────────────────────────────────────────────────────────────────

async def _login(email: str, password: str) -> None:
    from playwright.async_api import async_playwright
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CODE_FILE.unlink(missing_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900},
                                        user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        try:
            _set("running", "Opening Behance…")
            await page.goto("https://www.behance.net/", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await _click_text(page, "Log In", "Sign In")
            done_email = done_pw = False
            unknown, deadline = 0, time.time() + 600
            while time.time() < deadline:
                await page.wait_for_timeout(2500)
                await page.screenshot(path=str(SHOT))
                url = page.url
                text = (await page.inner_text("body"))[:4000].lower()

                if "behance.net" in url and "adobe" not in url and (done_pw or done_email):
                    _set("running", "Back on Behance — verifying session…")
                    if await _logged_in(page):
                        await ctx.storage_state(path=str(STATE_FILE))
                        await page.screenshot(path=str(SHOT))
                        _set("ok", "Logged in — session saved ✓")
                        return
                if await _visible(page, 'iframe[src*="captcha"], iframe[src*="arkose"], '
                                        'iframe[title*="challenge" i], #px-captcha'):
                    _set("blocked", "Adobe is showing a human check (CAPTCHA). Use "
                                    "'Upload session file' instead.")
                    return

                code_box = await _visible(page, 'input[autocomplete="one-time-code"], '
                                                'input[data-id^="CodeInput"], '
                                                'input[inputmode="numeric"]')
                pw_box = await _visible(page, 'input[type="password"]')
                email_box = await _visible(page, 'input[type="email"], input[name="username"], '
                                                 'input[name="email"], #EmailPage-EmailField')
                if code_box and "code" in text:
                    _set("need_code", "Adobe sent a verification code (check your email "
                                      "or phone) — enter it below.")
                    t0 = time.time()
                    while not CODE_FILE.exists() and time.time() - t0 < 600:
                        await asyncio.sleep(2)
                    if not CODE_FILE.exists():
                        _set("failed", "No verification code entered within 10 minutes.")
                        return
                    code = CODE_FILE.read_text(encoding="utf-8").strip()
                    CODE_FILE.unlink(missing_ok=True)
                    await code_box.click()
                    await page.keyboard.type(code, delay=80)   # works for split digit boxes
                    await page.wait_for_timeout(800)
                    await _click_text(page, "Verify", "Continue", "Submit")
                    _set("running", "Code submitted…")
                    unknown = 0
                elif pw_box and not done_pw:
                    _set("running", "Entering password…")
                    await pw_box.fill(password)
                    await _click_text(page, "Continue", "Sign in", "Sign In") \
                        or await page.keyboard.press("Enter")
                    done_pw, unknown = True, 0
                elif email_box and not done_email:
                    _set("running", "Entering email…")
                    await email_box.fill(email)
                    await _click_text(page, "Continue", "Next") or await page.keyboard.press("Enter")
                    done_email, unknown = True, 0
                elif await _click_text(page, "Not now", "Skip", "Maybe later", "Continue",
                                       "Send code", "Send"):
                    _set("running", "Handling an extra Adobe prompt…")
                    unknown = 0
                else:
                    unknown += 1
                    if "incorrect" in text or "wrong password" in text:
                        _set("failed", "Adobe says the email or password is wrong.")
                        return
                    if unknown >= 6:
                        _set("failed", "Stuck on an unexpected page — see the screenshot. "
                                       "Try again or use 'Upload session file'.")
                        return
            _set("failed", "Timed out after 10 minutes.")
        except Exception as e:
            try:
                await page.screenshot(path=str(SHOT))
            except Exception:
                pass
            _set("failed", f"Login error: {str(e)[:200]}")
        finally:
            await browser.close()


async def _test() -> None:
    from playwright.async_api import async_playwright
    if not STATE_FILE.exists():
        _set("expired", "No saved session.")
        return
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=str(STATE_FILE), user_agent=UA,
                                        viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        try:
            ok = await _logged_in(page)
            await page.screenshot(path=str(SHOT))
            if ok:
                await ctx.storage_state(path=str(STATE_FILE))   # refresh cookies
            _set("ok" if ok else "expired",
                 "Session works ✓" if ok else "Session expired — log in again.")
        except Exception as e:
            _set("failed", f"Test error: {str(e)[:200]}")
        finally:
            await browser.close()


def _run(coro_fn, *args) -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        asyncio.run(coro_fn(*args))
    finally:
        _lock.release()


def start_login(email: str, password: str) -> None:
    _set("running", "Starting browser…")
    threading.Thread(target=_run, args=(_login, email, password), daemon=True,
                     name="behance-login").start()


def start_test() -> None:
    _set("testing", "Opening the Behance editor with the saved session…")
    threading.Thread(target=_run, args=(_test,), daemon=True, name="behance-test").start()
