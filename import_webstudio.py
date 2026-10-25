"""
import_webstudio.py — turn the story pages of a Webstudio project into CMS website projects.

Reads a synced Webstudio bundle (.webstudio/data.json + public/assets), follows the
Home page's card list (so order and category tags match the home gallery), and for
every card writes  input_website/<slug>/  containing the image and a pre-filled
.cms.json draft (title, description, story text, tag, alt text, specs line).

    python import_webstudio.py                  # prepare folders only, review in the CMS
    python import_webstudio.py --publish        # prepare AND publish all to R2, then move them to published/
    python import_webstudio.py --alt-ai         # write alt text with Gemini (else: the title)
    python import_webstudio.py --data PATH --assets PATH

The bundle must be the ORIGINAL static gallery (Capture_Local), synced with:
  cd <webstudio repo>/deploy-builds/import-capture-local && webstudio sync --buildId <draft build id>
  -> .webstudio/data.json + .webstudio/assets
"""

import argparse
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.projects import INPUT_DIRS, archive_project, load_draft, save_draft  # noqa: E402

DEFAULT_BUNDLE = Path(
    r"C:\Users\Datta\VSCode\webstudio_self_host\webstudio\deploy-builds"
    r"\import-capture-local")
DESC_MAX = 160


def unquote(expr: str) -> str:
    """Page titles/descriptions are stored as JS string expressions: "\"Beauty\"" -> Beauty."""
    expr = (expr or "").strip()
    if expr.startswith('"') and expr.endswith('"'):
        try:
            return json.loads(expr)
        except ValueError:
            pass
    return expr


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "story"


def short(text: str, limit: int = DESC_MAX) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def load_bundle(data_path: Path):
    d = json.loads(data_path.read_text(encoding="utf-8"))
    b = d["build"]
    items = lambda key: [x[1] if isinstance(x, list) else x for x in b[key]]
    ins = {i["id"]: i for i in items("instances")}
    props: dict[str, list] = {}
    for p in items("props"):
        props.setdefault(p["instanceId"], []).append(p)
    pages = {p["id"]: p for p in b["pages"]["pages"]}
    home = b["pages"]["homePageId"]
    assets = {a["id"]: a for a in d["assets"]}
    return ins, props, pages, home, assets


def prop(props, iid, name):
    return next((p for p in props.get(iid, []) if p["name"] == name), None)


def walk(ins, iid):
    """Instances under iid in document order (skips Slots/embeds: shared chrome, not content)."""
    i = ins.get(iid)
    if not i or i["component"] in ("Slot", "HtmlEmbed"):
        return
    yield i
    for c in i.get("children", []):
        if c["type"] == "id":
            yield from walk(ins, c["value"])


def text_of(ins, iid) -> str:
    i = ins[iid]
    return "".join(c["value"] for c in i.get("children", []) if c["type"] == "text").strip()


def read_story(ins, props, assets, page) -> dict:
    nodes = list(walk(ins, page["rootInstanceId"]))
    label = lambda n: n.get("label", "")
    images = [n for n in nodes if n["component"] == "Image"]
    box = next((n for n in nodes if label(n) == "Image Specs"), None)
    specs = next((text_of(ins, c["value"]) for c in (box or {}).get("children", [])
                  if c["type"] == "id" and ins[c["value"]]["component"] == "Text"), "")
    heading = next((text_of(ins, n["id"]) for n in nodes if n["component"] == "Heading"), "")
    paragraphs = [text_of(ins, n["id"]) for n in nodes if n["component"] == "Paragraph"]
    files = []
    for n in images:
        src = prop(props, n["id"], "src")
        if src and src["type"] == "asset" and src["value"] in assets:
            alt = prop(props, n["id"], "alt")
            files.append({"asset": assets[src["value"]], "alt": alt["value"] if alt and alt["type"] == "string" else ""})
    return {"heading": heading, "paragraphs": [p for p in paragraphs if p], "specs": specs, "files": files}


def home_cards(ins, props, pages, home_id):
    """(page, category) in home gallery order, from the ListItem cards that link to story pages."""
    cards = []
    for n in walk(ins, pages[home_id]["rootInstanceId"]):
        cls = prop(props, n["id"], "className") or prop(props, n["id"], "class")
        if n["component"] != "ListItem" or not cls or "card" not in str(cls["value"]).split():
            continue
        link = next((ins[c["value"]] for c in n.get("children", []) if c["type"] == "id"
                     and ins.get(c["value"], {}).get("component") == "Link"), None)
        href = prop(props, link["id"], "href") if link else None
        if href and href["type"] == "page" and isinstance(href["value"], str) and href["value"] in pages:
            cat = prop(props, n["id"], "data-category")
            cards.append((pages[href["value"]], (cat["value"] if cat else "").strip().lower()))
    return cards


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DEFAULT_BUNDLE / ".webstudio" / "data.json")
    ap.add_argument("--assets", type=Path, default=DEFAULT_BUNDLE / ".webstudio" / "assets")
    ap.add_argument("--publish", action="store_true", help="also publish every story to R2 and move the folders to published/")
    ap.add_argument("--alt-ai", action="store_true", help="write alt text with Gemini (needs GEMINI_API_KEY)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N stories (for a trial run)")
    args = ap.parse_args()

    ins, props, pages, home_id, assets = load_bundle(args.data)
    cards = home_cards(ins, props, pages, home_id)
    if args.limit:
        cards = cards[:args.limit]
    print(f"{len(cards)} story cards on the home page")

    if args.publish:
        import src.r2 as r2
        if not r2.configured():
            sys.exit("R2 isn't configured — fill in the R2_* values in .env first.")
        done = {s["slug"] for s in r2.list_stories()}
    else:
        done = set()
    if args.alt_ai:
        from src.ai_generator import generate_alt_text

    root = INPUT_DIRS["website"]
    used: set[str] = set()
    made: list[tuple[Path, dict]] = []
    for page, category in cards:
        st = read_story(ins, props, assets, page)
        title = unquote(page["title"]) or st["heading"].title()
        slug = slugify(title)
        if slug in used:
            slug = f"{slug}-{len(used)}"
        used.add(slug)
        specs_text = re.sub(r"\s*[   ]+\s*", " · ", st["specs"]).strip(" ·")
        if slug in done:
            print(f"  skip   {slug} (already published)")
            continue
        if not st["files"]:
            print(f"  SKIP   {slug}: no image found on page {page['path']}")
            continue
        folder = root / slug
        if folder.exists():
            draft = load_draft(folder).get("website")
            if args.publish and draft:  # prepared earlier (and maybe edited in the CMS): publish that
                if "specs" not in draft and draft.get("names"):  # saved by a CMS build without the specs field
                    draft["specs"] = {draft["names"][0]: specs_text}
                made.append((folder, draft))
                print(f"  reuse  {slug} (prepared folder)")
            else:
                print(f"  skip   {slug} (folder already exists)")
            continue
        folder.mkdir(parents=True)
        names, alts, specs = [], {}, {}
        for n, f in enumerate(st["files"], 1):
            a = f["asset"]
            src_file = args.assets / a["name"]
            if not src_file.exists():
                print(f"  WARN   {slug}: missing asset file {a['name']}")
                continue
            name = f"{slug}{'' if len(st['files']) == 1 else f'-{n}'}.{a['format'].replace('jpeg', 'jpg')}"
            shutil.copy2(src_file, folder / name)
            names.append(name)
            alts[name] = f["alt"] or (generate_alt_text(folder / name) if args.alt_ai else title)
            specs[name] = re.sub(r"\s*[\u2003\u2002\u2009]+\s*", " · ", st["specs"]).strip(" ·") if n == 1 else ""
        if not names:
            folder.rmdir()
            continue
        body = "\n\n".join(st["paragraphs"])
        desc = unquote(page["meta"].get("description", "")) or short(body)
        taken = (assets[page["meta"]["socialImageAssetId"]] if page["meta"].get("socialImageAssetId") in assets else
                 st["files"][0]["asset"])["createdAt"][:10]
        draft = {"names": names, "title": title, "slug": slug, "description": short(desc),
                 "location": "", "tags": category, "published_at": taken or datetime.date.today().isoformat(),
                 "body_md": body, "alts": alts, "captions": {n: "" for n in names}, "specs": specs}
        save_draft(folder, {"website": draft})
        made.append((folder, draft))
        print(f"  ready  {slug:<28} tag={category or '-':<9} {len(names)} image(s)")

    if not args.publish:
        print(f"\nPrepared {len(made)} folder(s) in {root}.\n"
              "Open the CMS → 🌐 Website to review/edit and publish each one, or re-run with --publish.")
        return
    import src.r2 as r2
    for folder, d in made:  # order = home gallery order (ties keep publish order)
        r2.publish_story(
            d["slug"], d["title"], d["description"], d["body_md"],
            [{"path": folder / n, "alt": d["alts"].get(n) or d["title"], "caption": d.get("captions", {}).get(n, ""),
              "specs": d.get("specs", {}).get(n, "")} for n in d["names"]],
            tags=[t for t in d["tags"].split(",") if t], published_at=d["published_at"])
        archive_project("website", folder)
        print(f"  published {d['slug']}")


if __name__ == "__main__":
    main()
