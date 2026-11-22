# 🎵 Instagram music for reels — one-time setup

Meta's Audio API only works through **Facebook Login**, so music needs a second token.
Normal posting keeps using your existing Instagram token.

## 1. Link Instagram to a Facebook Page
The Instagram website doesn't offer this. Use one of these:
- **Facebook (desktop, easiest):** switch into your Page → **Settings → Linked accounts → Instagram → Connect account**.
  If you have no Page yet: facebook.com/pages/create.
- **Instagram phone app:** Profile → **Edit profile** → under *Public business information* → **Page → Connect or create**.

The Instagram account must be Business or Creator.

## 2. Add Facebook Login to your Meta app
developers.facebook.com → your app → **Add product → Facebook Login for Business**.

## 3. Get a token
1. Open **Graph API Explorer** (developers.facebook.com/tools/explorer).
2. Choose your app, click **Get User Access Token**, and tick:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`, `business_management`.
3. Click **Generate**, then log in and allow access to your Page and Instagram account.
4. Click ⓘ → **Open in Access Token Tool** → **Extend Access Token**. This gives a token that lasts about 60 days.

## 4. Add it on the VM
In `~/cms-local/.env`:
```
IG_FB_TOKEN=<the extended token>
IG_FB_USER_ID=          # optional, found automatically
```
Then restart: `sudo systemctl restart cms`.

## Using it
Reel editor, or a reel day in the Week planner → **🎵 Add Instagram music** → search → **Use**.
The video's own sound defaults to 0, so the reel plays the music only.

## Limits
- Only tracks Meta has licensed for third-party tools. This is a smaller library than in the app.
- You can't pick where the song starts, and you can't preview the reel with the music before it goes live.
- The token expires after about 60 days. Repeat step 3 and update `.env`.
