# ▶️ YouTube Setup — one-time, ~5 minutes

The YouTube publisher uses the official **YouTube Data API v3** with OAuth. You need your own (free) Google Cloud project and one OAuth client. After this setup you sign in once in the app and never touch it again — the token refreshes itself.

## 1. Create the Google Cloud project & enable the API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) (any Google account)
2. Top bar → project dropdown → **New Project** → name it e.g. `my-cms` → Create
3. Menu → **APIs & Services → Library** → search **"YouTube Data API v3"** → **Enable**

## 2. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen** (Google Auth Platform)
2. Audience: **External** → Create
3. App name: `Local CMS`, add your email in both email fields → Save through the steps
4. Under **Audience → Test users**, add your own Google/YouTube account email
5. **Important:** after testing that login works, set **Publishing status → In production**. Apps left in "Testing" mode get refresh tokens that expire every 7 days, which would force weekly re-login. "In production" (even unverified) keeps the token alive indefinitely — you'll just see an "unverified app" warning once during sign-in, which is fine for personal use.

## 3. Create the OAuth client (type matters!)

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **TVs and Limited Input devices**  ← must be this type; it's what lets you sign in from any device (including when the CMS runs on a VM)
3. Name it anything → Create → **Download JSON**
4. Save the file as:
   ```
   cms-local/.secrets/youtube_client_secret.json
   ```
   (or set `YOUTUBE_CLIENT_SECRET_FILE=/path/to/file.json` in `.env`)

## 4. Sign in from the app

1. Start the CMS → **▶️ YouTube** tab → sidebar → **🔐 Connect YouTube account**
2. The sidebar shows a URL (`google.com/device`) and a short code
3. Open the URL on **any** device (your phone works), enter the code, pick your YouTube account, approve
4. The sidebar flips to "Connected" within a few seconds. Done — the refresh token is stored in `.secrets/youtube_token.json` and renews itself.

## Limits the app manages for you

| Limit | Value | What the app does |
|---|---|---|
| API quota | 10,000 units/day (upload = 100, thumbnail/playlist = 50) | Tracks locally, shows remaining in sidebar, blocks a batch that wouldn't fit. Resets midnight Pacific. |
| Channel upload cap | ~10–15/day for most channels (YouTube-enforced, varies) | Local cap `YOUTUBE_MAX_UPLOADS_PER_DAY` (default 10) stops the queue cleanly before YouTube rejects; also handles `uploadLimitExceeded` gracefully. |
| Title | 100 chars, no `<` `>` | Auto-sanitised |
| Description | 5,000 bytes | Auto-truncated |
| Tags | 500 chars total | Auto-trimmed |
| Made-for-kids | Declaration required (COPPA) | Checkbox in UI, sent as `selfDeclaredMadeForKids` |
| Scheduled publish | Requires private + `publishAt` | Handled automatically when you pick a schedule |

## ⚠️ Public videos & the API audit

Google policy: videos uploaded **as public** through an API project that hasn't completed YouTube's *API compliance audit* are automatically **locked to private**. Two ways to deal with it:

- **Easy (recommended):** upload as **unlisted** (the app's default), then flip to public in YouTube Studio — takes seconds, and scheduled/unlisted workflows are unaffected.
- **Proper:** fill in the [API audit form](https://support.google.com/youtube/contact/yt_api_form) once; personal-use projects are routinely approved, after which public API uploads work directly.

## Custom thumbnails

`thumbnails.set` requires your channel to be **phone-verified** ([youtube.com/verify](https://www.youtube.com/verify)). If it isn't, the app uploads the video fine and just skips the thumbnail with a note.

## ffmpeg (recommended)

Frame extraction for AI metadata + thumbnail grabs needs ffmpeg:
- macOS: `brew install ffmpeg`
- Ubuntu/Oracle VM: `sudo apt install -y ffmpeg`

Without it, AI still writes metadata from the filename and your notes.
