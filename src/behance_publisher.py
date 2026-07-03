"""
src/behance_publisher.py — Behance headless publisher (Playwright Chromium).

Flow:
  1. Generate metadata (title, description, tags, cover) via Gemini Vision
  2. Open editor, add header text
  3. For each image: upload → padding → text (interleaved, correct order)
  4. Add footer text
  5. Click Continue → fill metadata page → click Publish
"""

import asyncio
import time
from pathlib import Path
from typing import Any

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, TimeoutError as PWTimeout,
)
from bs4 import BeautifulSoup
import src.progress as progress
from src.ai_generator import generate_behance_metadata

NS         = "behance"   # progress namespace
BASE_URL   = "https://www.behance.net"
STATE_DIR  = Path(__file__).resolve().parent.parent / ".browser_state"
STATE_FILE = STATE_DIR / "behance_state.json"
EDITOR_URL = f"{BASE_URL}/portfolio/editor"


async def _launch(pw, storage_state):
    browser = await pw.chromium.launch(headless=True, slow_mo=50)
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        storage_state=storage_state,
    )
    return browser, ctx, await ctx.new_page()


def _step_labels(n: int) -> list[str]:
    s = ["Generate metadata", "Open editor", "Add header text"]
    for i in range(n):
        s += [f"Upload image {i+1}", f"Add padding {i+1}", f"Add image {i+1} text"]
    return s + ["Add footer text", "Fill metadata & Publish"]


async def _dismiss(page: Page) -> None:
    for sel in ['button:has-text("Cancel")', 'button:has-text("Close")',
                '[aria-label="Close"]']:
        btn = await page.query_selector(sel)
        if btn: await btn.click(); await page.wait_for_timeout(500); return
    await page.keyboard.press("Escape"); await page.wait_for_timeout(400)


async def _deselect(page: Page) -> None:
    await page.mouse.click(560, 170)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)


async def _click_add_content(page: Page, label: str) -> None:
    await _deselect(page)
    for sel in [f'[aria-label="{label}"]', f'aside button:has-text("{label}")',
                f'button:has-text("{label}"):not(:has-text("Photo"))']:
        try:
            btn = await page.wait_for_selector(sel, timeout=3_000)
            if btn: await btn.click(); return
        except PWTimeout: continue
    await page.evaluate(f"""
        () => {{
            const all = [...document.querySelectorAll('button,[role="button"]')];
            const btn = all.find(b => b.textContent.trim() === '{label}');
            if (btn) btn.click();
        }}
    """)


async def _upload_image(page: Page, img_path: Path, step: int) -> None:
    progress.update(NS, step, "active", f"Uploading {img_path.name}…")
    await _deselect(page)
    full_path = str(img_path.resolve())

    try:
        async with page.expect_file_chooser(timeout=4_000) as fc_info:
            await _click_add_content(page, "Image")
        fc = await fc_info.value
        await fc.set_files(full_path)
        await page.wait_for_timeout(5000)
        progress.update(NS, step, "done", f"{img_path.name} ✓")
        return
    except Exception: pass

    await _click_add_content(page, "Image")
    await page.wait_for_timeout(800)
    for _ in range(6):
        inputs = await page.query_selector_all('input[type="file"]')
        if inputs:
            await inputs[-1].set_input_files(full_path)
            await page.wait_for_timeout(5000)
            progress.update(NS, step, "done", f"{img_path.name} ✓")
            return
        await page.wait_for_timeout(500)
    progress.update(NS, step, "error", f"Upload failed: {img_path.name}")


async def _add_padding(page: Page, step: int) -> None:
    progress.update(NS, step, "active", "Adding padding…")
    await page.wait_for_timeout(800)
    for sel in ["figure img", ".image-block img", "img"]:
        imgs = await page.query_selector_all(sel)
        if imgs: await imgs[-1].hover(); await page.wait_for_timeout(500); break
    found = await page.evaluate("""
        () => {
            const btns = [...document.querySelectorAll("button")];
            const b = btns.find(b => {
                const lbl = (b.getAttribute("aria-label") || "").toLowerCase();
                const txt = b.textContent;
                return lbl.includes("padding") || lbl.includes("breathing")
                    || (txt.includes("→") && txt.includes("←"));
            });
            if (b) { b.click(); return true; } return false;
        }
    """)
    progress.update(NS, step, "done", "Padding ✓" if found else "Padding not found")


async def _count_editors(page: Page) -> int:
    return len(await page.query_selector_all('[contenteditable="true"]'))


async def _add_text_block(page: Page, text: str, step: int, label: str) -> None:
    if not text.strip():
        progress.update(NS, step, "done", f"{label} — no text, skipped")
        return

    progress.update(NS, step, "active", f"Adding {label}…")
    before = await _count_editors(page)

    await _click_add_content(page, "Text")

    # Wait for a NEW editor to appear
    for _ in range(12):
        if await _count_editors(page) > before: break
        await page.wait_for_timeout(500)

    all_eds = await page.query_selector_all('[contenteditable="true"]')
    if not all_eds:
        progress.update(NS, step, "error", f"{label} — editor not found")
        return

    editor = all_eds[-1]  # LAST = newest
    await editor.scroll_into_view_if_needed()
    await editor.click()
    await page.wait_for_timeout(300)
    for chunk in [text[i:i+300] for i in range(0, len(text), 300)]:
        await page.keyboard.type(chunk, delay=6)
    progress.update(NS, step, "done", f"{label} ✓ ({len(text)} chars)")


def _parse_content(html: str, image_paths: list[Path]) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    # Per-image text: each wrapped in <div class="img-section">
    sections = soup.find_all("div", class_="img-section")
    image_texts = [BeautifulSoup(str(s), "html.parser").get_text("\n").strip()
                   for s in sections]

    # Header = everything before first img-section
    header_parts, past = [], False
    for el in soup.children:
        from bs4 import Tag
        if isinstance(el, Tag):
            if el.get("class") and "img-section" in el.get("class", []): past = True; continue
            if not past: header_parts.append(el.get_text("\n").strip())
    header = "\n".join(p for p in header_parts if p)

    # Footer = everything after last img-section
    footer_parts, found_last = [], False
    for el in reversed(list(soup.children)):
        from bs4 import Tag
        if isinstance(el, Tag):
            if el.get("class") and "img-section" in el.get("class", []): found_last=True; break
            footer_parts.insert(0, el.get_text("\n").strip())
    footer = "\n".join(p for p in footer_parts if p) if found_last else ""

    while len(image_texts) < len(image_paths): image_texts.append("")
    return {"header": header, "image_texts": image_texts[:len(image_paths)], "footer": footer}


async def _fill_metadata_page(page: Page, metadata: dict, project_name: str,
                               image_paths: list[Path], step: int) -> None:
    """
    Fill the metadata modal. CRITICAL RULES:
    1. Wait for the modal Title input to be VISIBLE before doing anything
    2. Never press Escape at top level — it closes the whole modal
    3. After tags, click inside the modal (not outside) to close dropdown
    """
    import os as _os

    progress.update(NS, step, "active", "Waiting for metadata modal…")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Wait until the Title input is visible — confirms modal is open
    try:
        await page.wait_for_selector(
            'input[placeholder*="Give your project" i]', timeout=10_000)
    except PWTimeout:
        await page.screenshot(path=str(STATE_DIR / "modal_wait_failed.png"))
        progress.update(NS, step, "error", "Modal did not open — check modal_wait_failed.png")
        return

    await page.wait_for_timeout(1000)
    await page.screenshot(path=str(STATE_DIR / "metadata_page.png"), full_page=False)

    ai_title = metadata.get("title", project_name)
    ai_desc  = metadata.get("description", "")
    ai_tags  = metadata.get("tags", [])

    # ── Title ──────────────────────────────────────────────────────────────────
    try:
        title_inp = await page.wait_for_selector(
            'input[placeholder*="Give your project" i]', timeout=5_000)
        await title_inp.click()
        await page.wait_for_timeout(200)
        await title_inp.fill(ai_title)
        await page.wait_for_timeout(500)
        progress.update(NS, step, "active", f"Title: {ai_title[:50]}")
    except Exception as e:
        progress.update(NS, step, "active", f"Title failed: {e}")

    # ── Tags (before category so dropdown is closed before we need category) ──
    tags_to_add = (ai_tags[:10] if ai_tags else
        ["photography", "art", "design"] +
        [w.lower() for w in project_name.replace("-","_").replace(" ","_").split("_")
         if len(w) > 2][:5])
    tags_filled = 0
    try:
        tag_inp = await page.wait_for_selector(
            'input[placeholder*="keywords" i]', timeout=5_000)
        for tag in tags_to_add[:8]:
            await tag_inp.click()
            await tag_inp.fill(tag)
            await page.wait_for_timeout(300)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(400)
            tags_filled += 1
        progress.update(NS, step, "active", f"Tags: {tags_filled}")
    except Exception as e:
        progress.update(NS, step, "active", f"Tags failed: {e}")

    # Close tags dropdown: Tab out of tags field to move focus forward
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(500)

    # ── Category (REQUIRED) ────────────────────────────────────────────────────
    # The Category row has a "View all" link (top-right) that opens the checkbox modal.
    # Flow: click "View all" → modal opens → search → check category → click Done
    category = _os.getenv("BEHANCE_CATEGORY", "Photography")
    cat_filled = False
    try:
        cat_opened = False

        # ── DEBUG: dump the HTML around the category section ─────────────────
        # This saves category_dom.txt so we know the real element structure.
        try:
            cat_html = await page.evaluate("""
                () => {
                    const all = [...document.querySelectorAll('*')];
                    const el = all.find(e =>
                        e.textContent.includes('How Would You Categorize') ||
                        e.textContent.includes('View all')
                    );
                    return el ? el.closest('section,div,form')?.innerHTML?.slice(0, 4000) || el.outerHTML : 'NOT FOUND';
                }
            """)
            (STATE_DIR / "category_dom.txt").write_text(cat_html or "empty")
        except Exception:
            pass

        # ── STEP 1: scroll the category row into view ─────────────────────────
        await page.evaluate("""
            () => {
                const all = [...document.querySelectorAll('*')];
                const el = all.find(e =>
                    e.children.length === 0 &&
                    e.textContent.trim() === 'View all'
                );
                if (el) el.scrollIntoView({block: 'center'});
            }
        """)
        await page.wait_for_timeout(400)

        # ── STEP 2: JS dispatchEvent click on "View all" (bypasses React issues) ──
        # React needs bubbling synthetic-like events; dispatchEvent with bubbles works.
        cat_opened = await page.evaluate("""
            () => {
                const all = [...document.querySelectorAll('*')];
                // Find the leaf node with exact text "View all"
                const el = all.find(e =>
                    e.textContent.trim() === 'View all' && e.children.length === 0
                );
                if (!el) return false;
                const target = el.closest('a,button,[role="button"]') || el;
                // Fire full mouse event sequence
                ['mousedown','mouseup','click'].forEach(type => {
                    target.dispatchEvent(new MouseEvent(type, {
                        bubbles: true, cancelable: true, view: window
                    }));
                });
                return true;
            }
        """)
        await page.wait_for_timeout(2000)
        search_check = await page.query_selector('input[placeholder*="Search Creative" i]')
        if search_check:
            cat_opened = True

        # ── STEP 3: if still not open, try Playwright click with force=True ───
        if not cat_opened:
            for sel in ['a:has-text("View all")', 'button:has-text("View all")',
                        ':text-is("View all")']:
                try:
                    el = await page.wait_for_selector(sel, timeout=2_000)
                    if el:
                        await el.scroll_into_view_if_needed()
                        await el.click(force=True)
                        await page.wait_for_timeout(2000)
                        search_check = await page.query_selector(
                            'input[placeholder*="Search Creative" i]')
                        if search_check:
                            cat_opened = True
                            break
                except Exception:
                    continue

        # ── STEP 4: click the placeholder field itself (whole row may be clickable) ──
        if not cat_opened:
            cat_opened = await page.evaluate("""
                () => {
                    const all = [...document.querySelectorAll('*')];
                    const ph = all.find(e =>
                        e.children.length === 0 &&
                        e.textContent.trim() === 'How Would You Categorize This Project?'
                    );
                    if (!ph) return false;
                    // Walk up to find the clickable container (could be 1-3 levels up)
                    let target = ph;
                    for (let i = 0; i < 4; i++) {
                        const p = target.parentElement;
                        if (!p) break;
                        const role = p.getAttribute('role');
                        if (role === 'button' || role === 'combobox' || role === 'listbox') {
                            target = p; break;
                        }
                        // Also stop at a div with an onClick-style handler
                        if (p.onclick || p.getAttribute('tabindex') === '0') {
                            target = p; break;
                        }
                        target = p;
                    }
                    ['mousedown','mouseup','click'].forEach(type =>
                        target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}))
                    );
                    return true;
                }
            """)
            await page.wait_for_timeout(2000)
            search_check = await page.query_selector('input[placeholder*="Search Creative" i]')
            if search_check:
                cat_opened = True

        # ── STEP 5: coordinate fallback — "View all" is at ~x=1075 in the modal ──
        if not cat_opened:
            # From screenshot: "View all" link is top-right of Category section
            # Modal right panel starts ~x=600; "View all" is near x=1075, Category row y varies
            # Try both the position in the first screenshot and second screenshot
            for x, y in [(1075, 120), (1075, 392), (1195, 392), (1195, 120)]:
                await page.mouse.click(x, y)
                await page.wait_for_timeout(1500)
                search_check = await page.query_selector(
                    'input[placeholder*="Search Creative" i]')
                if search_check:
                    cat_opened = True
                    break

        await page.screenshot(path=str(STATE_DIR / "category_modal.png"), full_page=False)
        progress.update(NS, step, "active", f"Category modal opened: {cat_opened}")

        # ── Type in the Search input to filter the list ───────────────────────
        search_inp = await page.wait_for_selector(
            'input[placeholder*="Search Creative" i]', timeout=5_000)
        await search_inp.click()
        await search_inp.fill(category)
        await page.wait_for_timeout(800)

        # ── Click the matching checkbox ───────────────────────────────────────
        # After filtering, click the label/row for our category.
        # Try Playwright's text locator first (most reliable), then JS fallback.
        checked = False
        try:
            # get_by_label hits <label> elements — works when checkbox has a label
            await page.get_by_label(category, exact=True).first.check()
            checked = True
        except Exception:
            pass

        if not checked:
            checked = await page.evaluate(f"""
                () => {{
                    const cat = '{category}';
                    // Prefer clicking a <label> that wraps or is paired with a checkbox
                    const labels = [...document.querySelectorAll('label')];
                    const lbl = labels.find(l => l.textContent.trim() === cat);
                    if (lbl) {{ lbl.click(); return true; }}

                    // Fallback: any list item / role=option / role=checkbox
                    const items = [...document.querySelectorAll(
                        'li, [role="checkbox"], [role="option"], [role="menuitem"]')];
                    const item = items.find(i => i.textContent.trim() === cat);
                    if (item) {{
                        const cb = item.querySelector('input[type="checkbox"]');
                        (cb || item).click();
                        return true;
                    }}
                    return false;
                }}
            """)

        if not checked:
            try:
                await page.get_by_text(category, exact=True).first.click()
                checked = True
            except Exception:
                pass

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(STATE_DIR / "category_checked.png"), full_page=False)

        # ── Click Done to confirm ─────────────────────────────────────────────
        for done_sel in [
            'button:has-text("Done")',
            'button:has-text("Apply")',
        ]:
            done_btn = await page.query_selector(done_sel)
            if done_btn and await done_btn.is_visible():
                await done_btn.click()
                cat_filled = True
                await page.wait_for_timeout(800)
                break

        if not cat_filled:
            try:
                await page.get_by_role("button", name="Done").click()
                cat_filled = True
                await page.wait_for_timeout(800)
            except Exception:
                pass

        progress.update(NS, step, "active",
            f"Category: {'✓' if cat_filled else '✗'} checked={checked} opened={cat_opened}")
    except Exception as e:
        progress.update(NS, step, "active", f"Category failed: {e}")

    # ── Tools Used ────────────────────────────────────────────────────────────
    # Chip/tag input — same pattern as Tags. Each tool entered + Enter key.
    # Configured via BEHANCE_TOOLS env var as comma-separated list.
    # Default matches the standard setup: "CAPTURE ONE,FUJIFILM XT30"
    tools_raw = _os.getenv("BEHANCE_TOOLS", "CAPTURE ONE,FUJIFILM XT30")
    tools_list = [t.strip() for t in tools_raw.split(",") if t.strip()]
    if tools_list:
        try:
            tools_inp = await page.wait_for_selector(
                'input[placeholder*="software" i], input[placeholder*="hardware" i], '
                'input[placeholder*="materials" i]',
                timeout=4_000,
            )
            for tool in tools_list:
                await tools_inp.click()
                await tools_inp.fill(tool)
                await page.wait_for_timeout(300)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(400)
            progress.update(NS, step, "active", f"Tools: {', '.join(tools_list)}")
        except Exception as e:
            progress.update(NS, step, "active", f"Tools failed: {e}")

    # ── Description ──────────────────────────────────────────────────────────
    if ai_desc:
        try:
            desc_inp = await page.wait_for_selector(
                'textarea[placeholder*="short description" i]', timeout=4_000)
            await desc_inp.click()
            await desc_inp.fill(ai_desc)
            await page.wait_for_timeout(400)
        except Exception: pass

    await page.screenshot(path=str(STATE_DIR / "metadata_filled.png"), full_page=False)
    progress.update(NS, step, "active", f"All fields filled — clicking Publish…")

    await page.wait_for_timeout(500)
    progress.update(NS, step, "active", f"Filled: '{ai_title}' — clicking Publish…")
    await page.wait_for_timeout(500)

    # ── Find the exact Publish button by iterating all buttons ────────────────
    # The modal footer has: Cancel | Save as Draft | Publish (green, exact text "Publish")
    # We use exact text match to avoid hitting "Save as Draft" or other buttons
    clicked = False
    all_btns = await page.query_selector_all("button")
    publish_btn = None
    for b in all_btns:
        txt = (await b.text_content() or "").strip()
        if txt == "Publish":
            publish_btn = b

    if publish_btn:
        await publish_btn.scroll_into_view_if_needed()
        await publish_btn.click()
        clicked = True
        progress.update(NS, step, "active", "Publish button clicked ✓")
    else:
        # Coordinate fallback: green Publish button at bottom-right of modal
        await page.mouse.click(1113, 741)
        clicked = True
        progress.update(NS, step, "active", "Publish clicked via coordinates")

    await page.wait_for_timeout(4000)
    await page.screenshot(path=str(STATE_DIR / "after_publish.png"))

    # If PRO upsell appeared, dismiss it then click Publish again
    pro_text = await page.evaluate(
        "() => document.body.innerText.includes('Upgrade your experience')")
    if pro_text:
        progress.update(NS, step, "active", "PRO modal appeared — dismissing…")
        await _dismiss_pro_modal(page)
        await page.wait_for_timeout(1000)
        # Re-click Publish
        all_btns2 = await page.query_selector_all("button")
        for b in all_btns2:
            txt = (await b.text_content() or "").strip()
            if txt == "Publish":
                await b.click()
                await page.wait_for_timeout(5000)
                break

    await page.screenshot(path=str(STATE_DIR / "after_publish.png"))
    progress.update(NS, step, "done", f"Done — {page.url}")


async def _dismiss_pro_modal(page: Page) -> bool:
    """
    Dismiss the Behance PRO upsell modal if it appears.
    The modal has an × at top-right of the inner white card (~1149, 163 in 1440px viewport).
    Returns True if a modal was found and dismissed.
    """
    # Check if a PRO/upsell modal is visible
    modal_visible = await page.evaluate("""
        () => {
            const texts = ['Upgrade your experience', 'Behance Pro', 'Start your 7 day'];
            return texts.some(t => document.body.innerText.includes(t));
        }
    """)

    if not modal_visible:
        return False

    # Try clicking the × close button (top-right of inner modal card)
    closed = await page.evaluate("""
        () => {
            // Find all buttons that look like close buttons (×, ✕, X, close)
            const btns = [...document.querySelectorAll('button')];
            const closeBtn = btns.find(b => {
                const txt = b.textContent.trim();
                const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
                return txt === '×' || txt === '✕' || txt === 'X' || txt === '×'
                    || lbl.includes('close') || lbl.includes('dismiss');
            });
            if (closeBtn) { closeBtn.click(); return true; }
            return false;
        }
    """)

    if not closed:
        # Coordinate click on the × position (top-right of inner modal)
        # Modal inner card ×  is at roughly x=1149, y=163 in 1440px viewport
        await page.mouse.click(1149, 163)
        await page.wait_for_timeout(500)

    await page.wait_for_timeout(1000)

    # Verify modal is gone
    still_visible = await page.evaluate("""
        () => document.body.innerText.includes('Upgrade your experience')
    """)
    return not still_visible


async def _click_publish_btn(page: Page) -> bool:
    """Find and click the Publish button. Returns True if clicked."""
    for sel in ['button:has-text("Publish")', 'button:has-text("Publish Now")',
                'button:has-text("Publish Project")', '[data-testid*="publish"]']:
        btns = await page.query_selector_all(sel)
        for btn in reversed(btns):
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    return True
            except Exception:
                continue
    return False


async def _publish(page: Page, step: int) -> str:
    progress.update(NS, step, "active", "Clicking Publish…")

    # First attempt: click Publish
    clicked = await _click_publish_btn(page)
    if not clicked:
        progress.update(NS, step, "error", "Publish button not found on first attempt")
        await page.screenshot(path=str(STATE_DIR / "publish_failed.png"))
        return page.url

    await page.wait_for_timeout(2000)

    # Check if PRO upsell modal appeared (common after clicking Publish)
    dismissed = await _dismiss_pro_modal(page)
    if dismissed:
        progress.update(NS, step, "active", "PRO modal dismissed — clicking Publish again…")
        await page.wait_for_timeout(500)
        # Click Publish again now that modal is gone
        await _click_publish_btn(page)
        await page.wait_for_timeout(2000)

    # Handle any final confirmation dialog
    for c_sel in ['button:has-text("Publish Now")', 'button:has-text("Confirm")',
                  'button:has-text("Done")']:
        c = await page.query_selector(c_sel)
        if c and await c.is_visible():
            await c.click()
            await page.wait_for_timeout(3000)
            break

    await page.wait_for_load_state("networkidle", timeout=20_000)
    await page.screenshot(path=str(STATE_DIR / "after_publish.png"))
    progress.update(NS, step, "done", f"Published → {page.url}")
    return page.url


async def _is_logged_in(page: Page) -> bool:
    """Check if we are actually logged in by looking for a profile avatar or absence of login buttons."""
    try:
        # If we see a "Log In" button or a login form, we aren't logged in.
        login_btn = await page.query_selector('a:has-text("Log In"), button:has-text("Log In")')
        if login_btn and await login_btn.is_visible():
            return False
        # If the URL is redirected to a login or landing page
        if "behance.net/login" in page.url or page.url == "https://www.behance.net/":
            # Wait a bit to see if it was a slow redirect
            await page.wait_for_timeout(2000)
            if "behance.net/login" in page.url or page.url == "https://www.behance.net/":
                return False
        return True
    except Exception:
        return False


async def _run(project_name: str, html_content: str,
               image_paths: list[Path]) -> dict[str, Any]:
    n      = len(image_paths)
    labels = _step_labels(n)
    progress.start(NS, labels)
    content  = _parse_content(html_content, image_paths)
    storage  = str(STATE_FILE) if STATE_FILE.exists() else None

    si = [0]
    def cur(): return si[0]
    def nxt(): si[0] += 1

    async with async_playwright() as pw:
        browser, ctx, page = await _launch(pw, storage)
        try:
            # ── 0: Generate metadata ─────────────────────────────────────────
            progress.update(NS, cur(), "active", "Analysing images with Gemini Vision…")
            try:
                metadata = generate_behance_metadata(project_name, image_paths)
                progress.update(NS, cur(), "done",
                    f"'{metadata['title']}' | cover: img {metadata['cover_index']+1}")
            except Exception as e:
                metadata = {"title": project_name, "description": "", "tags": [], "cover_index": 0}
                progress.update(NS, cur(), "done", f"Metadata fallback (error: {e})")
            nxt()

            # ── 1: Open editor ───────────────────────────────────────────────
            progress.update(NS, cur(), "active", "Opening editor…")
            await page.goto(EDITOR_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)

            if not await _is_logged_in(page):
                progress.update(NS, cur(), "error", "Login expired!")
                raise RuntimeError(
                    "Behance session expired or missing. Please click 'Behance Login' "
                    "in the sidebar to refresh your credentials."
                )

            await _dismiss(page)
            progress.update(NS, cur(), "done", "Editor ready ✓"); nxt()

            # ── 2: Header ────────────────────────────────────────────────────
            await _add_text_block(page, content["header"], cur(), "Header"); nxt()

            # ── 3…: Per-image (upload → padding → text in strict order) ──────
            for i, img_path in enumerate(image_paths):
                await _upload_image(page, img_path, cur()); nxt()
                await _add_padding(page, cur()); nxt()
                await _add_text_block(
                    page, content["image_texts"][i], cur(), f"Image {i+1} text"); nxt()

            # ── N+3: Footer ──────────────────────────────────────────────────
            await _add_text_block(page, content["footer"], cur(), "Footer"); nxt()

            # ── N+4: Continue → fill metadata ────────────────────────────────
            progress.update(NS, cur(), "active", "Clicking Continue…")
            try:
                btn = await page.wait_for_selector('button:has-text("Continue")', timeout=8_000)
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(3000)
            except PWTimeout:
                progress.update(NS, cur(), "active", "Continue not found — trying Publish directly")

            await _fill_metadata_page(page, metadata, project_name, image_paths, cur()); nxt()

            result = {"success": True, "url": page.url}
            progress.finish(NS, result)
            return result

        except Exception as exc:
            shot = STATE_DIR / f"error_{int(time.time())}.png"
            try: await page.screenshot(path=str(shot), full_page=True)
            except Exception: pass
            err = str(exc)
            for idx, s in enumerate(progress.read(NS).get("steps", [])):
                if s["status"] in ("active", "pending"):
                    progress.update(NS, idx, "error", err[:80])
            progress.fail(NS, err)
            return {"success": False, "error": err, "screenshot": str(shot)}
        finally:
            await browser.close()


def publish_to_behance(project_name: str, html_content: str,
                       image_paths: list[Path]) -> dict[str, Any]:
    return asyncio.run(_run(project_name, html_content, image_paths))
