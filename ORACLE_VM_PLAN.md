# 🗺️ Oracle VM Implementation Plan — with token preservation

Goal: move the CMS from your Mac to an always-on Oracle Cloud VM **without re-authenticating anything**. All three credentials are just files — save them once, copy them once, and the app keeps them fresh forever after.

## Where every token lives

| Credential | File(s) | Renewal |
|---|---|---|
| YouTube OAuth | `.secrets/youtube_client_secret.json` + `.secrets/youtube_token.json` | Auto — refresh token renews itself on every API call |
| Instagram token | inside `.env` (`INSTAGRAM_ACCESS_TOKEN`) + `.queue/ig_token_meta.json` | Auto — app refreshes weekly, writes back to `.env` |
| Behance session | `.browser_state/behance_state.json` | Manual re-login only when Behance expires it (~months) |
| Gemini / Cloudinary keys | `.env` | Never expire |
| App password | `.env` (`APP_PASSWORD`) | You choose it |

**None of these are in git** (`.gitignore` blocks them). Code travels via GitHub; tokens travel via `scp`. That separation is deliberate — a leaked repo must never leak your accounts.

---

## Phase 0 — On your Mac (10 min): capture all tokens locally first

1. `python launch.py` and confirm each connection works **on your Mac** first:
   - **YouTube tab → Connect YouTube account** → approve code → sidebar shows "Connected". This writes `.secrets/youtube_token.json`. *(Needs the one-time Google Cloud setup from `YOUTUBE_SETUP.md`, and the consent screen set to "In production" so the token never expires.)*
   - **Sidebar → Refresh Behance Login** → log in → writes `.browser_state/behance_state.json`.
   - Instagram: token already in `.env`; after 24h the app's weekly auto-refresh takes over.
2. Set a password in `.env`: `APP_PASSWORD=<long-random-string>`
3. Push code to GitHub (see Phase 1). Secrets stay behind.

## Phase 1 — Push code to GitHub (2 min)

```bash
cd ~/cms-local
git add -A
git commit -m "Add YouTube publishing, VM support, token auto-refresh"
git push origin main
```

Verify on github.com that **no** `.env`, `.secrets/`, `.browser_state/` appear in the repo.

## Phase 2 — Provision the Oracle VM (15 min)

1. OCI Console → Compute → Create Instance
   - Image: **Ubuntu 24.04**, Shape: **VM.Standard.A1.Flex — 2 OCPU / 12 GB** (Always Free)
   - Add your SSH public key; note the public IP
2. SSH in and install the stack:
   ```bash
   ssh ubuntu@<VM_IP>
   sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg git
   git clone https://github.com/arinmaywork/cms.git cms-local
   cd cms-local
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium && playwright install-deps chromium
   ```

## Phase 3 — Transfer the tokens (the critical 2 minutes)

From **your Mac** (not the VM):

```bash
cd ~/cms-local
scp .env ubuntu@<VM_IP>:~/cms-local/.env
scp -r .secrets ubuntu@<VM_IP>:~/cms-local/
scp -r .browser_state ubuntu@<VM_IP>:~/cms-local/
scp -r history ubuntu@<VM_IP>:~/cms-local/        # optional: keeps AI brand voice
```

Then lock the permissions on the VM:
```bash
ssh ubuntu@<VM_IP> "chmod 600 ~/cms-local/.env ~/cms-local/.secrets/* ~/cms-local/.browser_state/*.json"
```

That's it — the VM now has every credential and will keep them fresh itself:
- YouTube: refresh token renews access tokens automatically
- Instagram: weekly refresh rewrites `.env` **on the VM** (so the VM's copy becomes the source of truth from now on — don't overwrite it with an old Mac copy later)
- Behance: session file reused until Behance expires it

## Phase 4 — Run as a service (5 min)

```bash
sudo tee /etc/systemd/system/cms.service > /dev/null <<'UNIT'
[Unit]
Description=Local Social Media CMS
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/cms-local
ExecStart=/home/ubuntu/cms-local/.venv/bin/python launch.py --headless --port 8501
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now cms
systemctl status cms          # should say "active (running)"
```

## Phase 5 — Access from any device (5 min)

**Recommended: Tailscale** (encrypted, nothing exposed publicly, free):
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up      # open the printed link once to authorise
tailscale ip -4        # note the 100.x.x.x address
```
Install Tailscale on your phone/laptop → open `http://100.x.x.x:8501` → enter `APP_PASSWORD`. To let someone else in: share the VM node from the Tailscale admin console and give them the app password.

*(Public HTTPS alternative with Caddy + domain: see `DEPLOYMENT.md` §4B.)*

## Phase 6 — Verify tokens survived (2 min)

Open the UI from your phone:
1. Sidebar → **▶️ YouTube Account** → shows "Connected: <your channel>" ✅
2. Sidebar → **🔗 Connections** → Instagram token shows days left ✅
3. Sidebar → Behance → "Session saved" ✅
4. Drop a test folder via SFTP into `~/cms-local/input_youtube/` → publish something **unlisted** end-to-end.

## Ongoing operations

| Task | How |
|---|---|
| Get content to the VM | SFTP (Cyberduck/Files app) into `~/cms-local/input_*/`, or paste a VM path in the UI |
| Update code | `cd ~/cms-local && git pull && sudo systemctl restart cms` (tokens untouched — they're not in git) |
| Logs | `journalctl -u cms -f` |
| Back up tokens | `scp -r ubuntu@<VM_IP>:~/cms-local/{.env,.secrets,.browser_state} ~/cms-backup/` (run monthly) |
| Behance session expired | Re-login once on your Mac, `scp .browser_state` to the VM again |

## Failure playbook

- **YouTube shows "Connect account"** → refresh token revoked (rare; happens if consent screen left in "Testing" mode). Fix mode in Cloud Console → click Connect in the UI → approve code from your phone. VM never needs a GUI.
- **Instagram code 190** → token was invalidated (password change etc.). Generate one token in Meta portal, paste into VM's `.env`, restart. Auto-refresh resumes.
- **Two machines fighting over the IG token**: run the app in only ONE place. The refresh invalidates older tokens, so a Mac copy left running can kill the VM's token.
