import asyncio, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
from playwright.async_api import async_playwright

STATE_DIR  = ROOT / ".browser_state"
STATE_FILE = STATE_DIR / "behance_state.json"

def _find_brave():
    import shutil, os
    for p in ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
              "/usr/bin/brave-browser", "/usr/bin/brave"]:
        if os.path.exists(p): return p
    return shutil.which("brave") or shutil.which("brave-browser")

async def main():
    print("\nBrave will open — log in to Behance manually, then return here.\n")
    brave = _find_brave()
    kw = {"headless": False, "slow_mo": 50}
    if brave: kw["executable_path"] = brave
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**kw)
        ctx     = await browser.new_context(viewport={"width":1280,"height":900})
        page    = await ctx.new_page()
        await page.goto("https://www.behance.net/login")
        print("Waiting up to 3 minutes for login...")
        await page.wait_for_function(
            "() => location.href.includes('behance.net') && "
            "!location.href.includes('login') && "
            "!location.href.includes('adobe.com')",
            timeout=180_000)
        await page.wait_for_timeout(2000)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(STATE_FILE))
        print(f"\n✅ Session saved to {STATE_FILE}")
        print("You can now use Approve & Publish without logging in.\n")
        await browser.close()

asyncio.run(main())
