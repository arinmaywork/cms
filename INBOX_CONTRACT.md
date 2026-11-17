# CMS Inbox Contract v1 — scheduled Instagram posts & Reels

For tools on the same VM (e.g. the Telegram content bot) that hand finished content to the CMS for scheduled publishing. The CMS checks the inbox every ~30 s.

## Where

`/home/ubuntu/cms-local/inbox/instagram/`

## How to submit (atomic)

1. Create `<id>.tmp/` and write the media files + `post.json` into it.
2. Rename `<id>.tmp/` → `<id>/`. The CMS ignores `*.tmp` folders, so it never reads a half-written submission.

`<id>`: unique, filesystem-safe (`[A-Za-z0-9_-]`), e.g. `tg-20261120-001`. Re-using an id that's already scheduled is rejected, so a double "confirm" can't post twice.

## post.json

```jsonc
{
  "schema": 1,
  "id": "tg-20261120-001",
  "type": "image" | "carousel" | "reel",
  "caption": "Final caption incl. hashtags",   // ≤ 2200 chars, ≤ 30 hashtags
  "append_defaults": false,                     // true → CMS appends its signature + default hashtags
  "publish_at": "2026-11-21T19:00:00+05:30",    // ISO-8601 with offset; null → next free daily slot;
                                                // a time in the past → published within ~1 min
  "source": "telegram-bot",

  // image | carousel
  "slides": [ { "file": "1.jpg", "alt_text": "≤ 1000 chars" } ],   // 1–10, in order; JPEG/PNG, any ratio
  "frame": null,               // null → CMS no-crop framing defaults; or
                               // {"on": true, "ratio": "4:5 portrait", "color": "#000000", "margin": 0, "grid_safe": true}

  // reel
  "video": { "file": "reel.mp4" },   // MP4/MOV, H.264/HEVC + AAC, 3 s – 15 min, 9:16 recommended,
                                     // ≤ 100 MB on Cloudinary's free plan (Instagram max 300 MB)
  "cover": "cover.jpg",              // optional cover image, or:
  "cover_second": 2.5,               // optional: use the frame at 2.5 s as cover
  "share_to_feed": true              // also show on the profile grid / feed
}
```

Alt text applies to images only (Instagram doesn't accept it for Reels). Music can't be added through the API — bake it into the video.

## Status — written by the CMS: `<id>/status.json`

```json
{ "state": "received|scheduled|publishing|published|failed|rejected|cancelled",
  "publish_at": "2026-11-21T13:30:00Z", "schedule_id": "ab12cd34",
  "post_id": "...", "permalink": "https://www.instagram.com/p/...",
  "error": "", "updated": "2026-11-20T10:00:05Z" }
```

| state | meaning |
|---|---|
| `received` | validated, uploading media |
| `scheduled` | in the schedule (`publish_at` is UTC). An `error` starting with "retrying:" = a failed attempt; up to 3 attempts, 10 min apart |
| `publishing` | posting now (Reels: Instagram processes the video for 1–5 min) |
| `published` | live — `permalink` set |
| `failed` | gave up — `error` says why. If it says "Interrupted", check Instagram first (it may already be live) |
| `rejected` | invalid submission — `error` says why; fix it and resubmit with a **new** id |
| `cancelled` | cancelled before publishing |

Poll every ~60 s and message the user on each `state` change.

## Cancel

Create an empty file `<id>/cancel`. Works while the state is `received`, `scheduled` or `failed`; ignored once `publishing` or `published`.

## Changes

After submission, edits happen in the CMS UI (Instagram → 📅 schedule panel: move, post now, remove), or cancel and resubmit with a new id.
