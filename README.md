# 📱 Local Social Media CMS

A local-first content management system that automates publishing photography projects to **Instagram** and **Behance**. Drop a project folder into a watched directory — the app uploads your images, generates AI copy with Google Gemini, and publishes with one click.

---

## Features

- **Folder watcher** — drop a project folder into `input_instagram/` or `input_behance/` and it appears in the UI automatically
- **AI copy generation** — Google Gemini analyses your images and writes captions, hooks, hashtags, alt text, and Behance descriptions
- **Instagram publishing** — uploads images to Cloudinary, then publishes via the Instagram Graph API (single image or carousel up to 10 slides)
- **Behance publishing** — headless Playwright automation opens the Behance editor, uploads images, adds text blocks, fills metadata, and publishes
- **Live progress UI** — step-by-step progress widget shows exactly what's happening during automation and publishing
- **History sidebar** — review your last N posts per platform
- **No cloud dependency** — runs entirely on your machine; only outbound calls go to the APIs

---

## Architecture

```
launch.py               ← single entry point (run this)
  ├── src/watcher.py    ← watchdog observer on input_instagram/ and input_behance/
  │     └── src/file_queue.py  ← JSON-file IPC queue between watcher and Streamlit
  └── app.py (subprocess)      ← Streamlit UI
        ├── ui/instagram_ui.py
        │     ├── src/uploader.py         ← Cloudinary upload
        │     ├── src/ai_generator.py     ← Gemini Vision + text generation
        │     └── src/instagram_publisher.py  ← Instagram Graph API v25.0
        ├── ui/behance_ui.py
        │     └── src/behance_publisher.py    ← Playwright headless automation
        ├── ui/progress_widget.py   ← background thread + live progress rendering
        └── src/history_manager.py  ← JSON history per platform
```

### IPC design

`launch.py` (the watcher process) and `app.py` (the Streamlit subprocess) are separate OS processes. They communicate through JSON files in `.queue/`:

| File | Purpose |
|---|---|
| `.queue/instagram.json` | Queue of pending Instagram project paths |
| `.queue/behance.json` | Queue of pending Behance project paths |
| `.queue/publish_progress.json` | Live step-by-step progress state |

Both sides use a companion `.lock` file for atomic cross-process access.

### Background thread pattern

All blocking work (Cloudinary upload, Gemini API calls, Playwright automation) runs in daemon threads spawned by `start_publish_thread()`. The Streamlit script runner returns in milliseconds on every autorefresh tick, reading only the progress JSON file to render the UI. This eliminates the Python 3.13 `RuntimeError: reentrant call inside <_io.BufferedWriter name='<stdout>'>` crash that occurs when blocking work is done in the script runner thread.

---

## Setup

### Prerequisites

- Python 3.11 or 3.12 (recommended; 3.13 supported)
- A Google account (for Gemini API key — free tier)
- A Meta developer account (for Instagram Graph API)
- A Cloudinary account (free tier)
- A Behance account

### 1. Clone and install

```bash
git clone <repo-url>
cd cms-local

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Open `.env` and set:

| Variable | Description | Where to get it |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free |
| `INSTAGRAM_ACCESS_TOKEN` | Instagram Login API token (`IGAA…`) | Meta developer portal — see below |
| `INSTAGRAM_USER_ID` | Your Instagram scoped user ID | Returned by `/me` on `graph.instagram.com` |
| `CLOUDINARY_URL` | Full Cloudinary URL | Cloudinary dashboard → API Keys |
| `BEHANCE_EMAIL` | Your Behance login email | — |
| `BEHANCE_PASSWORD` | Your Behance password | — |
| `BEHANCE_CATEGORY` | Default project category | e.g. `Photography` |
| `BEHANCE_TOOLS` | Comma-separated tools list | e.g. `CAPTURE ONE,FUJIFILM XT30` |

### 3. Getting an Instagram Access Token

The app uses the **Instagram Login API** (not Facebook Login). You need a token that starts with `IGAA`.

1. Go to [developers.facebook.com](https://developers.facebook.com) and open your app
2. Add the **Instagram** product to your app
3. Under **Instagram → API Setup**, click **Generate Token** for your Instagram account
4. Copy the token (starts with `IGAA`) into `.env` as `INSTAGRAM_ACCESS_TOKEN`
5. The token is valid for 60 days; regenerate it before it expires

**Required permissions:** `instagram_basic`, `instagram_content_publish`

**Finding your User ID:** After setting the token, you can find your scoped user ID from `graph.instagram.com/v25.0/me` — the app uses this automatically.

### 4. Behance login (first run)

The Behance publisher uses Playwright to automate the browser. On first use:

1. Click **Behance Login** in the Streamlit sidebar
2. A browser window opens — log in manually
3. The session is saved to `.browser_state/behance_state.json`
4. Future runs reuse the saved session (no login needed until it expires)

---

## Usage

### Start the CMS

```bash
python launch.py
```

This starts the file watcher and opens the Streamlit UI at `http://localhost:8501`.

Optional flags:
```bash
python launch.py --port 8888      # use a different port
python launch.py --no-browser     # don't auto-open the browser
```

### Publishing to Instagram

1. Drop a project folder containing `.jpg`, `.png`, or `.webp` images into `input_instagram/`
2. The folder appears automatically in the **📸 Instagram** tab
3. Optionally add AI context notes (e.g. "minimal architecture, golden hour")
4. Click **🤖 Automate Everything** — the app uploads to Cloudinary and generates all copy
5. Review and edit the hook, per-slide captions, hashtags, and alt texts
6. Click **🚀 APPROVE & PUBLISH LIVE**

The progress widget shows live step-by-step status. Single images and carousels (up to 10 slides) are supported.

### Publishing to Behance

1. Drop a project folder into `input_behance/`
2. Switch to the **🎨 Behance** tab
3. Optionally enter a text script or notes in the content editor
4. Click **🚀 Publish to Behance**

The Playwright automation: generates AI metadata, opens the editor, uploads images with padding and per-image text blocks, fills the metadata page (title, tags, category, tools, description), and publishes.

---

## Project folder structure

Each project folder should contain only image files:

```
input_instagram/
  my-project/
    01.jpg
    02.jpg
    03.jpg

input_behance/
  architecture-series/
    hero.jpg
    detail-01.jpg
    detail-02.jpg
```

Images are sorted alphabetically, so prefix filenames with numbers to control order.

---

## Directory layout

```
cms-local/
├── app.py                    # Streamlit app (launched by launch.py)
├── launch.py                 # Entry point — watcher + Streamlit launcher
├── requirements.txt
├── .env                      # Credentials (not committed)
├── .env.example              # Credentials template
│
├── src/
│   ├── ai_generator.py       # Gemini Vision + text generation
│   ├── behance_publisher.py  # Playwright headless Behance automation
│   ├── file_queue.py         # Cross-process JSON queue
│   ├── history_manager.py    # JSON history per platform
│   ├── instagram_publisher.py # Instagram Graph API v25.0
│   ├── progress.py           # Thread-safe progress state (JSON file)
│   ├── uploader.py           # Cloudinary image uploader
│   └── watcher.py            # Watchdog observer
│
├── ui/
│   ├── behance_ui.py         # Behance Streamlit UI
│   ├── folder_picker.py      # Manual folder picker widget
│   ├── instagram_ui.py       # Instagram Streamlit UI
│   └── progress_widget.py    # Live progress panel + background thread manager
│
├── input_instagram/          # Drop Instagram project folders here
├── input_behance/            # Drop Behance project folders here
├── history/                  # Auto-generated publish history (JSON)
├── .queue/                   # Auto-generated IPC files
├── .browser_state/           # Auto-generated Behance session state
└── logs/                     # Streamlit log file
```

---

## AI model cascade

The app tries models in this order, automatically falling back on quota/rate errors:

| Priority | Model | Notes |
|---|---|---|
| 1 | `gemini-2.0-flash` | Best free quota, full vision support |
| 2 | `gemini-1.5-flash` | High daily limit, reliable |
| 3 | `gemini-2.5-flash-lite` | 10 RPM free |
| 4 | `gemini-2.5-flash` | ~20 requests/day on free tier |

---

## Instagram API details

The app uses the **Instagram Graph API v25.0** at `https://graph.instagram.com/v25.0`.

| Flow | Endpoints |
|---|---|
| Single image | `POST /{user_id}/media` → poll status → `POST /{user_id}/media_publish` |
| Carousel | `POST /{user_id}/media` × N (carousel items) → `POST /{user_id}/media` (CAROUSEL) → `POST /{user_id}/media_publish` |

Images must be publicly accessible URLs — Cloudinary provides these. The token is sent as `Authorization: Bearer <token>` (not as a URL parameter).

**Caption limit:** 2,200 characters. The UI shows a live character count and blocks publishing if exceeded.

---

## Troubleshooting

**"INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID must be set in .env"**
→ Copy `.env.example` to `.env` and fill in your credentials.

**"Invalid OAuth access token" (code 190)**
→ Your token has expired (60-day limit). Regenerate it in the Meta developer portal and update `.env`.

**"Behance session expired or missing"**
→ Click **Behance Login** in the sidebar to re-authenticate.

**Playwright timeout errors on Behance**
→ Behance's editor can be slow. The publisher has built-in waits and fallback selectors. If publishing consistently fails, check the screenshots saved to `.browser_state/` for visual debugging.

**Images not detected in the watcher**
→ Type `s` + Enter in the terminal dashboard to trigger a manual scan.

**Gemini quota errors**
→ The free tier has daily limits. The app cascades through models automatically. If all models are exhausted, wait until the next day or add billing at [aistudio.google.com](https://aistudio.google.com).

---

## Environment variables reference

```env
# Google Gemini
GEMINI_API_KEY=your_key_here

# Instagram Graph API (Instagram Login token — starts with IGAA)
INSTAGRAM_ACCESS_TOKEN=IGAAUr6...
INSTAGRAM_USER_ID=26361529496800682

# Behance credentials (used by Playwright automation)
BEHANCE_EMAIL=you@example.com
BEHANCE_PASSWORD=your_password
BEHANCE_CATEGORY=Photography
BEHANCE_TOOLS=CAPTURE ONE,FUJIFILM XT30

# Cloudinary (full URL format)
CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name
```

---

## License

MIT
