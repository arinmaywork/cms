"""
seo_backfill.py — fix search-weak text on stories that are already published (titles-only, alt = title,
cut-off descriptions, no location/date). Two steps, with YOU in between:

  python seo_backfill.py draft [--no-ai] [--location "Pune, India"] [--captured 2023] [--only slug ...]
      Reads the live stories (public URL, no R2 keys needed), asks Gemini for a descriptive alt text, a
      "what is in the photo" summary and a full-sentence description, and writes seo_review.csv.
      Location/captured are NEVER guessed: they come from --location/--captured or stay blank for you to fill.

  (open seo_review.csv in a spreadsheet, correct the text, fill location + captured per story, save as CSV)

  python seo_backfill.py rename-tag life Anima [--dry-run]     # rename a category on every story that has it

  python seo_backfill.py apply [--dry-run] [--csv seo_review.csv]
      Validates every row with the same rules as the CMS editor (src/seo.py) and updates the stories in R2
      (text only, images untouched). Rows that fail validation are skipped and listed. Backs up each
      story JSON to seo_backup/ first.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src import seo  # noqa: E402

COLS = ["slug", "img", "title", "location", "captured", "summary", "description", "alt"]


def _base() -> str:
    return (os.getenv("R2_PUBLIC_BASE") or "https://media.adatta.in").rstrip("/")


def _fetch(url: str, binary: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": "cms-seo-backfill"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if binary else json.loads(data)


def cmd_draft(a):
    if not a.no_ai:
        from src.ai_generator import generate_story_draft, generate_website_alt
    slugs = [s["slug"] for s in _fetch(f"{_base()}/stories/index.json")["stories"]]
    if a.only:
        slugs = [s for s in slugs if s in a.only]
    rows = []
    for slug in slugs:
        s = _fetch(f"{_base()}/stories/{slug}.json")
        loc, cap = s.get("location") or a.location, s.get("captured") or a.captured
        summary, desc = s.get("summary", ""), s["description"]
        alts = [im.get("alt", "") for im in s["images"]]
        if not a.no_ai:
            try:
                with tempfile.TemporaryDirectory() as td:
                    paths = []
                    for n, im in enumerate(s["images"], 1):
                        pth = Path(td) / f"{n}.jpg"
                        pth.write_bytes(_fetch(im["url"], binary=True))
                        paths.append(pth)
                    ctx = " ".join(x for x in (loc, cap) if x)
                    for n, pth in enumerate(paths):
                        if not alts[n].strip() or alts[n].strip().lower() == s["title"].strip().lower():
                            alts[n] = generate_website_alt(pth, s["title"], ctx)
                    draft = generate_story_draft(
                        slug, paths[0], "Rewrite only summary and description for this EXISTING story; "
                        f"title is '{s['title']}'. Location: {loc or 'unknown (do not mention one)'}. "
                        f"Year: {cap or 'unknown (do not mention one)'}. Story text: {s.get('body_md', '')[:1500]}")
                summary = summary or draft["summary"]
                if seo.cut_off(desc):     # only replace descriptions that fail the rules
                    desc = draft["description"] or desc
                time.sleep(1)                              # be gentle with the free Gemini quota
            except Exception as e:
                print(f"  ! {slug}: AI failed ({str(e)[:120]}) — row left for manual editing")
        for n, alt in enumerate(alts, 1):
            first = n == 1
            rows.append({"slug": slug, "img": n, "title": s["title"] if first else "",
                         "location": loc if first else "", "captured": cap if first else "",
                         "summary": summary if first else "", "description": desc if first else "", "alt": alt})
        print(f"  drafted {slug}")
    out = Path(a.csv)
    with out.open("w", newline="", encoding="utf-8-sig") as f:      # utf-8-sig so Excel/Numbers read it right
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {out} ({len(rows)} rows). Edit it (esp. location + captured), then: python seo_backfill.py apply")


def cmd_apply(a):
    import src.r2 as r2
    if not a.dry_run and not r2.configured():
        sys.exit("R2 isn't configured — fill in the R2_* values in .env first.")
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    by_slug: dict[str, list[dict]] = {}
    for r in rows:
        by_slug.setdefault(r["slug"].strip(), []).append(r)
    backup = ROOT / "seo_backup"
    ok = bad = 0
    for slug, rs in by_slug.items():
        head = next((r for r in rs if str(r["img"]).strip() in ("1", "")), rs[0])
        title, loc, cap = head["title"].strip(), head["location"].strip(), head["captured"].strip()
        problems, warn = seo.check(title, head["description"], [(f"{slug}#{r['img']}", r["alt"]) for r in rs],
                                   loc, head["summary"], cap)
        if problems:
            bad += 1
            print(f"  SKIP {slug}: " + "; ".join(problems))
            continue
        if a.dry_run:
            ok += 1
            print(f"  ok   {slug}: {seo.build_subtitle(head['summary'], loc, cap)}"
                  + (f"   (⚠ {'; '.join(warn)})" if warn else ""))
            continue
        story = r2.get_story(slug)
        if story is None:
            print(f"  SKIP {slug}: not published")
            bad += 1
            continue
        backup.mkdir(exist_ok=True)
        (backup / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
            json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")
        alts = {i: r["alt"].strip() for i, r in ((int(str(r["img"]) or 1), r) for r in rs)}
        images = [{"url": im["url"], "alt": alts.get(n, im.get("alt", ""))}
                  for n, im in enumerate(story["images"], 1)]
        r2.update_story(slug, {"title": title, "description": head["description"].strip(),
                               "location": loc, "captured": cap, "summary": head["summary"].strip()}, images)
        ok += 1
        print(f"  updated {slug}")
    print(f"\n{ok} {'valid' if a.dry_run else 'updated'}, {bad} skipped.")


def cmd_rename_tag(a):
    """Rename a tag/category on every story that has it (the first tag is the home-page filter)."""
    import src.r2 as r2
    if not r2.configured():
        sys.exit("R2 isn't configured — fill in the R2_* values in .env first.")
    old, new = a.old.strip().lower(), a.new.strip()
    hit = 0
    for s in r2.list_stories():
        tags = list(s.get("tags", []))
        if old not in [t.lower() for t in tags]:
            continue
        tags = [new if t.lower() == old else t for t in tags]
        hit += 1
        print(f"  {'would set' if a.dry_run else 'set'} {s['slug']}: {s.get('tags')} → {tags}")
        if not a.dry_run:
            r2.update_story(s["slug"], {"tags": tags})
    print(f"\n{hit} stor{'y' if hit == 1 else 'ies'} {'matched' if a.dry_run else 'updated'}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--csv", default="seo_review.csv")
    d.add_argument("--no-ai", action="store_true", help="just export the current values")
    d.add_argument("--location", default="", help="default location for stories that have none (only if true for all)")
    d.add_argument("--captured", default="", help="default year for stories that have none")
    d.add_argument("--only", nargs="*", help="limit to these slugs")
    d.set_defaults(fn=cmd_draft)
    p = sub.add_parser("apply")
    p.add_argument("--csv", default="seo_review.csv")
    p.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    p.set_defaults(fn=cmd_apply)
    r = sub.add_parser("rename-tag", help="rename a category/tag on all stories")
    r.add_argument("old")
    r.add_argument("new")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_rename_tag)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
