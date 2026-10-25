"""
cleanup_r2.py — find (and optionally delete) things in the R2 bucket that nothing uses.

    python cleanup_r2.py                  # report only, deletes nothing
    python cleanup_r2.py --ask            # report, then ask you to type DELETE to apply
    python cleanup_r2.py --delete         # report and delete without asking
    python cleanup_r2.py --stories        # also clear dead story entries (see below)
    python cleanup_r2.py --min-age-hours 0

What counts as unused:
  • images   — objects under img/ that no stories/<slug>.json refers to (replaced images,
               unpublished stories, files from old key naming). Anything newer than
               --min-age-hours (default 24) is kept, so an image uploaded by a publish that
               is still in progress is never deleted.
  • stories  — (only with --stories)
               - stories/<slug>.json files whose slug is missing from stories/index.json
                 (they are invisible on the site anyway); their images then count as unused;
               - entries in stories/index.json whose story file no longer exists (these would
                 show broken cards on the home page); the entry is dropped from the index.

Safety: every story file must be read and parsed before anything is deleted (any failure
aborts), and nothing is deleted if no stories are found unless --force is given.
Only img/ and stories/ are ever touched; other prefixes in the bucket are left alone.

Note: deleting a file doesn't remove copies already cached by Cloudflare (images are cached
for up to a year). If one must disappear at once, purge its URL in the Cloudflare dashboard.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import src.r2 as r2  # noqa: E402

INDEX = "stories/index.json"


def list_objects(prefix: str):
    paginator = r2._s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=os.environ["R2_BUCKET"], Prefix=prefix):
        yield from page.get("Contents", [])


def mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def image_keys(story: dict) -> set[str]:
    urls = [im["url"] for im in story.get("images", [])] + [story.get("cover", {}).get("url", "")]
    return {urlparse(u).path.lstrip("/") for u in urls if u}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete", action="store_true", help="delete without asking")
    ap.add_argument("--ask", action="store_true", help="show the report, then ask before deleting")
    ap.add_argument("--stories", action="store_true", help="also clear unlisted story files and dead index entries")
    ap.add_argument("--min-age-hours", type=float, default=24, help="keep images newer than this (default 24)")
    ap.add_argument("--force", action="store_true", help="allow deleting when no stories were found")
    args = ap.parse_args()

    if not r2.configured():
        sys.exit("R2 isn't configured — fill in the R2_* values in .env first.")
    bucket = os.environ["R2_BUCKET"]
    s3 = r2._s3()

    story_objs = {o["Key"]: o for o in list_objects("stories/") if o["Key"].endswith(".json") and o["Key"] != INDEX}
    images = {o["Key"]: o for o in list_objects("img/")}
    index = r2._get_json(INDEX, {"schema": r2.SCHEMA, "stories": []})
    listed = {s["slug"] for s in index["stories"]}

    # Read every story file first; a failure here must stop everything (never guess what's referenced).
    stories = {}
    for key in story_objs:
        try:
            stories[key] = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception as e:
            sys.exit(f"Aborting: couldn't read {key} ({e}). Nothing was deleted.")

    unlisted = [k for k, s in stories.items() if s.get("slug") not in listed] if args.stories else []
    dead_entries = sorted(listed - {s.get("slug") for s in stories.values()}) if args.stories else []
    live = {k: s for k, s in stories.items() if k not in unlisted}
    referenced = set().union(*(image_keys(s) for s in live.values())) if live else set()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.min_age_hours)
    unused = sorted(k for k in images if k not in referenced)
    old_enough = [k for k in unused if images[k]["LastModified"] <= cutoff]
    too_new = [k for k in unused if k not in old_enough]

    print(f"Bucket {bucket}: {len(stories)} story file(s), {len(listed)} in the index, "
          f"{len(images)} image(s) ({mb(sum(o['Size'] for o in images.values()))}), "
          f"{len(referenced)} referenced\n")
    if old_enough:
        print(f"Unused images ({len(old_enough)}, {mb(sum(images[k]['Size'] for k in old_enough))}):")
        for k in old_enough:
            print(f"  {k}  {mb(images[k]['Size'])}  {images[k]['LastModified']:%Y-%m-%d}")
    else:
        print("Unused images: none")
    if too_new:
        print(f"Kept (unused but newer than {args.min_age_hours:g} h): {len(too_new)}")
    if args.stories:
        print(f"Unlisted story files: {len(unlisted)}" + "".join(f"\n  {k}" for k in unlisted))
        print(f"Dead index entries: {len(dead_entries)}" + "".join(f"\n  {s}" for s in dead_entries))

    todo = len(old_enough) + len(unlisted) + len(dead_entries)
    if todo == 0:
        print("\nNothing to clean up.")
        return
    if not stories and not args.force:
        sys.exit("\nNo stories found in the bucket — refusing to delete anything (use --force to override).")
    if not (args.delete or args.ask):
        print("\nDry run — nothing deleted. Re-run with --ask (or --delete) to apply.")
        return
    if args.ask and input(f"\nType DELETE to remove {todo} item(s): ").strip() != "DELETE":
        print("Cancelled — nothing deleted.")
        return

    victims = old_enough + unlisted
    for i in range(0, len(victims), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in victims[i:i + 1000]]})
    if dead_entries:
        index["stories"] = [s for s in index["stories"] if s["slug"] not in dead_entries]
        index["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        r2.put_json(INDEX, index)
    print(f"Deleted {len(victims)} object(s)" + (f", dropped {len(dead_entries)} index entr{'y' if len(dead_entries) == 1 else 'ies'}" if dead_entries else "") + ".")


if __name__ == "__main__":
    main()
