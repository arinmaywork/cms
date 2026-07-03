# ☁️ Running the CMS on an Oracle Cloud VM (access from any device)

Goal: the CMS runs 24/7 on a free Oracle VM; you (and anyone you share the password with) open it in a browser from any device, drop files in via the UI's manual path loader or SFTP, and publish.

## 0. VM shape

Oracle's Always-Free tier works well: **VM.Standard.A1.Flex (Ampere ARM), 2 OCPU / 12 GB RAM, Ubuntu 22.04/24.04**. Playwright Chromium and video uploads are comfortable at that size.

## 1. Install

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg git
git clone <your-repo-or-scp-the-folder> cms-local && cd cms-local

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium        # system libs for headless Chromium

cp .env <fill in credentials>           # or scp your working .env from your Mac
```

Also copy from your Mac (so you don't re-authenticate):
- `.secrets/` (YouTube client secret + token)
- `.browser_state/behance_state.json` (Behance session)

```bash
scp -r .secrets .browser_state ubuntu@VM_IP:~/cms-local/
```

## 2. Set a password

In `.env` on the VM:
```env
APP_PASSWORD=something-long-and-random
```
The web UI now shows a login screen. Share this password with anyone you want to give access.

## 3. Run as a service (survives reboots)

`sudo nano /etc/systemd/system/cms.service`:

```ini
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
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cms
sudo systemctl status cms           # check it's running
journalctl -u cms -f                # live logs
```

`--headless` disables the browser auto-open and the screen-clearing terminal dashboard (it's auto-detected under systemd too).

## 4. Reach it from your devices — pick ONE

### Option A (recommended): Tailscale — zero config, encrypted, nothing exposed to the internet

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
Install Tailscale on your phone/laptop (free for personal use), then open
`http://<vm-tailscale-ip>:8501` from anywhere. No firewall rules, no TLS certs, not reachable by strangers. To share with one other person, use Tailscale's node-sharing.

### Option B: Public HTTPS with Caddy (needs a domain name)

```bash
sudo apt install -y caddy
```
`/etc/caddy/Caddyfile`:
```
cms.yourdomain.com {
    reverse_proxy localhost:8501
}
```
```bash
sudo systemctl restart caddy
```
Caddy gets a Let's Encrypt certificate automatically. Then open the Oracle-side firewall:
- OCI Console → your VM's **VCN → Security List** → add ingress rules for TCP **80** and **443** from `0.0.0.0/0`
- On the VM: `sudo ufw allow 80,443/tcp` (if ufw is active); Oracle Ubuntu images also use iptables:
  ```bash
  sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
  sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
  sudo netfilter-persistent save
  ```

**Never** expose port 8501 directly over plain HTTP — the password would travel unencrypted.

## 5. Getting content onto the VM

- **SFTP/Finder**: connect to the VM (e.g. with Cyberduck / `sftp ubuntu@vm`) and drop project folders straight into `~/cms-local/input_youtube/` etc. The watcher picks them up like on your Mac. Video files finish queueing only after the copy completes (the watcher waits for file sizes to stabilise).
- **Manual path**: any folder already on the VM can be loaded via the "paste folder path" box in each tab.

## 6. Notes

- Gemini, Cloudinary, Instagram Graph API and YouTube API are all outbound HTTPS — no inbound ports needed for publishing.
- The Instagram token auto-refreshes weekly and is written back to `.env` — no more 60-day expiry surprises.
- The YouTube device-code login works from the VM exactly like locally: the sidebar shows a code, you approve it on your phone.
- Timezone: quota resets are midnight **Pacific**; the sidebar shows the countdown so you don't have to think about it.
