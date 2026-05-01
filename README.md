# QQ Bot Linux Deployment

This is a lightweight Linux deployment copy of the current QQ bot project.

It keeps the runtime bot code and startup entrypoint, while excluding Windows installers, local virtual environments, logs, cache files, test artifacts, real `.env` secrets, private `config.yaml`, and runtime data JSON files.

## Included

- `run_bot.py`: startup entrypoint
- `src/bot_app`: runtime bot code
- `config.example.yaml`: non-private configuration template
- `data/avatar_pool`: optional local avatar image pool
- `scripts/start.sh`: Linux install-and-start helper
- `scripts/deploy_to_runtime.sh`: deploy a GitHub checkout to a runtime directory while preserving private local files
- `deploy/systemd/qqbot.service.example`: systemd service template

## Deploy

```bash
git clone <your-repo-url> Linux_bot
cd Linux_bot
cp .env.example .env
cp config.example.yaml config.yaml
nano .env
nano config.yaml
chmod +x scripts/*.sh
./scripts/start.sh
```

Required environment values:

- `NAPCAT_WS_URL`
- `NAPCAT_ACCESS_TOKEN`
- `MINIMAX_API_KEY`

The real `.env` is intentionally ignored by Git.

The real `config.yaml` and runtime `data/*.json` files are also intentionally ignored by Git because they contain QQ IDs, group IDs, learned rules, and live state.

## Sync flow

Use this order for production updates:

```bash
# local machine
git push origin main

# server
cd /opt/Linux_bot_repo
git pull --ff-only origin main
./scripts/deploy_to_runtime.sh /opt/Linux_bot
sudo systemctl restart qqbot
```

`deploy_to_runtime.sh` copies code from the GitHub checkout into `/opt/Linux_bot` but preserves `/opt/Linux_bot/.env`, `/opt/Linux_bot/config.yaml`, `/opt/Linux_bot/.venv`, `/opt/Linux_bot/data`, and logs.

## systemd

```bash
sudo mkdir -p /opt
sudo cp -r Linux_bot /opt/Linux_bot
cd /opt/Linux_bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
cp config.example.yaml config.yaml
nano .env
nano config.yaml
sudo cp deploy/systemd/qqbot.service.example /etc/systemd/system/qqbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
```

Check logs:

```bash
journalctl -u qqbot -f
```

## Avatar rotation

The bot can change its QQ avatar automatically every 24 hours. Put `.jpg`, `.jpeg`, `.png`, or `.webp` images under `data/avatar_pool`, or edit `avatar_rotation.image_urls` in `config.yaml`.

## Push to GitHub

```bash
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```
