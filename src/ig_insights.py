"""
src/ig_insights.py — export the last N reels' insights to one markdown file for Claude.

    python -m src.ig_insights [count]        # or the 📈 Reel insights tab in the CMS

Tries the Facebook-Login token (IG_FB_TOKEN, graph.facebook.com) first, then the normal
Instagram-Login token. One metric per call (one unsupported metric would fail the whole
request). Output format is fixed — see HANDOVER-insights-export.md. No secrets in the file.
"""

import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "history" / "insights"
METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
           "ig_reels_avg_watch_time", "reels_skip_rate", "ig_reels_video_view_total_time"]
HEAD = ["posted", "reel", "hook / caption", "views", "reach", "likes", "comments", "shares", "saves",
        "interactions", "avg watch (s)", "skip rate %", "total watch (min)", "music", "link"]


def _accounts() -> list[tuple[str, str, str]]:
    """[(base_url, token, ig_user_id)] in order of preference."""
    out = []
    if os.getenv("IG_FB_TOKEN"):
        try:
            from src import ig_music
            out.append((ig_music.FB_BASE, ig_music._token(), ig_music.user_id()))
        except Exception as e:
            print(f"  [insights] Facebook-Login token unusable: {e}")
    if os.getenv("INSTAGRAM_ACCESS_TOKEN") and os.getenv("INSTAGRAM_USER_ID"):
        out.append(("https://graph.instagram.com/v25.0", os.environ["INSTAGRAM_ACCESS_TOKEN"],
                    os.environ["INSTAGRAM_USER_ID"]))
    if not out:
        raise EnvironmentError("No Instagram token configured")
    return out


def _get(base: str, token: str, path: str, **params) -> dict:
    try:
        r = requests.get(f"{base}/{path}", params=params, timeout=30,
                         headers={"Authorization": f"Bearer {token}"})
        d = r.json()
    except Exception as e:
        return {"error": str(e)}
    if "error" in d:
        return {"error": d["error"].get("message", str(d["error"]))}
    return d


def _metric(base, token, mid, name):
    d = _get(base, token, f"{mid}/insights", metric=name)
    try:
        return d["data"][0]["values"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return "—"


def _cms_index() -> dict:
    """post_id → {"reel": CMS name, "music": title} from the schedule history."""
    try:
        import src.ig_schedule as igs
        out = {}
        for i in igs.read()["items"]:
            if i.get("post_id"):
                m = i.get("music") or {}
                out[str(i["post_id"])] = {
                    "reel": i.get("inbox_id") or i.get("name", ""),
                    "music": f"{m.get('title', '')} — {m.get('artist', '')}" if m.get("title") else "—"}
        return out
    except Exception:
        return {}


def _num(x, div=1, nd=1):
    return round(x / div, nd) if isinstance(x, (int, float)) else "—"


def export(n: int = 14, progress=None) -> tuple[Path, int, str]:
    """→ (file, rows, note)."""
    errors = []
    for base, token, uid in _accounts():
        media = _get(base, token, f"{uid}/media",
                     fields="id,caption,timestamp,media_product_type,permalink", limit=50)
        if "error" in media:
            errors.append(f"{base.split('/')[2]}: {media['error']}")
            continue
        reels = [m for m in media.get("data", []) if m.get("media_product_type") == "REELS"][:n]
        jobs = [(m["id"], k) for m in reels for k in METRICS]
        with ThreadPoolExecutor(8) as ex:
            vals = list(ex.map(lambda j: _metric(base, token, *j), jobs))
        got = dict(zip(jobs, vals))
        if reels and all(v == "—" for v in vals):              # no insights permission here
            errors.append(f"{base.split('/')[2]}: no insight values (missing insights permission?)")
            continue
        cms = _cms_index()
        rows = []
        for m in reels:
            v = {k: got[(m["id"], k)] for k in METRICS}
            c = cms.get(str(m["id"]), {})
            rows.append([m["timestamp"][:16].replace("T", " "), c.get("reel", "—").replace("|", "/"),
                         (m.get("caption") or "").split("\n")[0][:60].replace("|", "/"),
                         v["views"], v["reach"], v["likes"], v["comments"], v["shares"], v["saved"],
                         v["total_interactions"], _num(v["ig_reels_avg_watch_time"], 1000),
                         v["reels_skip_rate"], _num(v["ig_reels_video_view_total_time"], 60000),
                         c.get("music", "—").replace("|", "/"), m.get("permalink", "")])
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f"insights-{dt.date.today()}.md"
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# Reel insights · exported {dt.datetime.now():%Y-%m-%d %H:%M}\n\n| "
                    + " | ".join(HEAD) + " |\n|" + "---|" * len(HEAD) + "\n")
            for r in rows:
                f.write("| " + " | ".join(str(x) for x in r) + " |\n")
        return out, len(rows), f"via {base.split('/')[2]}"
    raise RuntimeError("Instagram said: " + " · ".join(errors))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    sys.path.insert(0, str(ROOT))
    p, k, note = export(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
    print(f"Wrote {p} ({k} reels, {note})")
