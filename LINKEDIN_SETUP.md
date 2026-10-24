# 💼 LinkedIn setup (one time, ~5 min)

1. Go to **linkedin.com/developers → Create app**. LinkedIn requires a Company Page to attach the app to — any page you admin works (create a simple one if needed). Fill name + logo → Create.
2. **Products** tab → request **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect** (both are instant, self-serve).
3. **Auth** tab → *Authorized redirect URLs* → add exactly:
   `http://localhost:8765/linkedin-callback`
4. Copy **Client ID** and **Client Secret** → CMS **⚙️ Settings → 💼 LinkedIn → App credentials → Save**.
5. **Connect / reconnect → Generate login link → Allow**. The browser lands on a page that can't load — copy its full address, paste it in Settings → **Finish connecting**.

Notes
- Posts go to your **personal profile**. Company-page posting needs LinkedIn's Community Management API approval.
- Standard apps get **60-day** logins with no refresh; the status strip warns when < 10 days remain — repeat step 5.
- Supported: text, 1 image, or 2–20 images. Max 3,000 characters. `#tags` become real hashtags.
