# 📱 Local Social Media CMS

A local-first content management system that automates publishing to **Instagram**, **Behance**, and **YouTube**. Drop a project folder into a watched directory — the app generates AI copy with Google Gemini, and publishes with one click. Runs on your machine or on a cloud VM accessed from any browser.

---

## Features

- **Folder watcher** — drop a folder into `input_instagram/`, `input_behance/` or `input_youtube/` and it appears in the UI automatically (video files wait until the copy finishes before queueing)
- **AI copy generation** — Gemini analyses your images/video frames and writes captions, hooks, hashtags, alt text, Behance descriptions, and YouTube titles/descriptions/tags
- **Instagram** — Cloudinary upload → Instagram Graph API (single image or carousel up to 10 slides); the 60-day access token now **auto-refreshes weekly**
- **Behance** — headless Playwright automation: uploads images, adds text blocks, fills metadata, publishes
- **YouTube** — official Data API v3: resumable uploads with retry, public/unlisted/private, scheduled publishing, playlists (existing or new), custom thumbnails, made-for-kids flag, and **built-in quota + daily-upload-limit tracking** so batches stop cleanly instead of failing mid-way
- **Live progress UI** — per-platform progress (simultaneous publishes don't collide)
- **History sidebar** — last N posts per platform; feeds the AI your brand voice
- **Remote-ready** — optional password login (`APP_PASSWORD`), headless mode, systemd deployment; see `DEPLOYMENT.md`

---

## Architecture

```
launch.py               ← single entry point (run this; --headless for VMs)
  ├── src/watcher.py    ← watchdog observer on input_instagram|behance|youtube/
  │     └── src/file_queue.py  ← JSON-file IPC queue between watcher and Streamlit
  └── app.py (subprocess)      ← Streamlit UI (+ optional password gate)
        ├── ui/instagram_ui.py
        │     ├── src/uploader.py             ← Cloudinary upload
        │     ├── src/ai_generator.py         ← Gemini vision + text
        │     ├── src/instagram_publisher.py  ← Instagram Graph API v25.0
        │     └── src/instagram_token.py      ← weekly token auto-refresh → .env
        ├── ui/behance_ui.py
        │     └── src/behance_publisher.py    ← Playwright headless automation
        ├── ui/youtube_ui.py
        │     ├── src/youtube_auth.py         ← OAuth device flow (works on VMs)
        │     ├── src/youtube_publisher.py    ← resumable uploads, playlists, thumbnails
        │     └── src/youtube_quota.py        ← quota + upload-cap tracking (PT reset)
        ├── ui/progress_widget.py   ← background thread + live progress rendering
        └── src/history_manager.py  ← JSON history per platform
```

### IPC design

`launch.py` (watcher process) and `app.py` (Streamlit subprocess) communicate through JSON files in `.queue/` with companion `.lock` files for atomic cross-process access. Progress state is **namespaced per pipeline** (`progress_ig.json`, `progress_behance.json`, `progress_yt.json`, …) so long YouTube uploads and Instagram posts can run concurrently.

### Background thread pattern

All blocking work (uploads, Gemini calls, Playwright) runs in daemon threads spawned by `start_publish_thread()`. The Streamlit script runner only reads progress JSON on each autorefresh tick — this avoids the Python 3.13 re-entrant stdout crash.

---

## Setup

### Prerequisites

- Python 3.11–3.13
- `ffmpeg` (for YouTube AI frame analysis + thumbnail extraction): `brew install ffmpeg` / `apt install ffmpeg`
- Accounts: Google (Gemini + YouTube), Meta developer (Instagram), Cloudinary, Behance

### 1. Install

```bash
git clone <repo-url> && cd cms-local
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env             # then fill it in
```

| Variable | Description | Where to get it |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free |
| `INSTAGRAM_ACCESS_TOKEN` | Instagram Login API token (`IGAA…`) | Meta developer portal (see below) — auto-refreshed weekly after first setup |
| `INSTAGRAM_USER_ID` | Instagram scoped user ID | `/me` on `graph.instagram.com` |
| `CLOUDINARY_URL` | Full Cloudinary URL | Cloudinary dashboard → API Keys |
| `BEHANCE_EMAIL` / `BEHANCE_PASSWORD` | Behance login | — |
| `BEHANCE_CATEGORY` / `BEHANCE_TOOLS` | Project defaults | e.g. `Photography` / `CAPTURE ONE,FUJIFILM XT30` |
| `YOUTUBE_MAX_UPLOADS_PER_DAY` | Local safety cap (default 10) | see `YOUTUBE_SETUP.md` |
| `APP_PASSWORD` | Web UI login (set on VMs) | you |

### 3. Instagram token (first time only)

1. [developers.facebook.com](https://developers.facebook.com) → your app → add the **Instagram** product
2. **Instagram → API Setup → Generate Token** (starts with `IGAA`) → paste into `.env`
3. Required permissions: `instagram_basic`, `instagram_content_publish`

From then on the app refreshes the token every week automatically and writes it back to `.env` — no more 60-day expiry.

### 4. Behance login (first run)

Click **Refresh Behance Login** in the sidebar, log in in the opened browser; the session is saved to `.browser_state/behance_state.json`.

### 5. YouTube (first time only)

Follow **`YOUTUBE_SETUP.md`** (~5 min): create a Google Cloud project, enable YouTube Data API v3, create a *"TVs and Limited Input devices"* OAuth client, drop the JSON into `.secrets/`, then click **Connect YouTube account** in the app and approve the shown code from any device.

---

## Usage

```bash
python launch.py                  # starts watcher + UI at http://localhost:8501
python launch.py --port 8888      # custom port
python launch.py --headless       # server/VM mode (also auto-detected under systemd)
```

### Instagram
Drop an image folder into `input_instagram/` → **🤖 Automate Everything** → review hook/captions/hashtags/alt-text → **🚀 APPROVE & PUBLISH LIVE**.

### Behance
Drop an image folder into `input_behance/` → AI-generate or write the sections → **🚀 Approve & Publish**.

### YouTube
Drop a folder with video file(s) (optionally + a `.jpg`/`.png` thumbnail) into `input_youtube/` →
**🤖 Automate Everything** (Gemini watches sampled frames, writes title/description/tags) →
choose **visibility** (unlisted default / public / private), **schedule** (optional), **playlist** (pick existing or type a new name), **category**, **made-for-kids** →
**🚀 APPROVE & UPLOAD**. The sidebar shows remaining API quota and today's upload count; batches that would exceed either limit are blocked up-front with the reset countdown (midnight Pacific).

> **Public uploads:** until your API project passes YouTube's one-time audit, publicly-set API uploads get locked private by YouTube. Default is therefore *unlisted* — flip to public in YouTube Studio in seconds, or request the audit once (see `YOUTUBE_SETUP.md`).

---

## Project folder structure

```
input_instagram/my-project/      01.jpg 02.jpg 03.jpg
input_behance/architecture/      hero.jpg detail-01.jpg
input_youtube/travel-film/       final-cut.mp4 thumbnail.jpg
```

Files are sorted alphabetically — prefix with numbers to control order.

---

## Deployment on a VM (Oracle Cloud etc.)

See **`DEPLOYMENT.md`** for the full guide: systemd service, password login, and two access options (Tailscale — recommended, or Caddy HTTPS with a domain). Everything (including YouTube sign-in) works identically on a headless VM.

---

## AI model cascade

Requests fall through on quota/rate errors:
`gemini-2.0-flash` → `gemini-1.5-flash` → `gemini-2.5-flash-lite` → `gemini-2.5-flash`

---

## Troubleshooting

**"INSTAGRAM_ACCESS_TOKEN … must be set"** → fill `.env`.
**"Invalid OAuth access token" (code 190)** → token was revoked; generate a fresh one once, auto-refresh takes over again.
**"Behance session expired"** → sidebar → Refresh Behance Login.
**YouTube "uploadLimitExceeded"** → your channel hit YouTube's own 24h cap; queue resumes after midnight PT. Consider lowering `YOUTUBE_MAX_UPLOADS_PER_DAY` so the app stops earlier.
**YouTube "quotaExceeded"** → the Cloud project's 10,000 daily units are spent; resets midnight PT (sidebar shows countdown).
**Public YouTube video became private** → API project not audited yet; see `YOUTUBE_SETUP.md`.
**Thumbnail skipped** → verify your phone at youtube.com/verify (YouTube requirement for custom thumbnails).
**Gemini quota errors** → cascade handles it; if all models are exhausted, wait a day or add billing.
**Folder not detected** → type `s` + Enter in the terminal dashboard, or use **Scan Now** in the UI.

---

## License

MIT
