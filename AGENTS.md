# AGENTS.md

Project memory for `E:\QQbot\Linux_bot`.

## What This Repo Is

Lightweight Linux deployment package for the QQ court-claim assistant.

It runs a NoneBot2 + OneBot V11 bot against NapCat, watches configured QQ groups, detects badminton court offer/swap messages, and notifies or replies according to the configured mode.

## Core Boundaries

- Keep this repo deploy-focused. Do not add Windows-only tooling, local caches, logs, test artifacts, virtualenvs, or real secrets.
- Treat `.env`, `config.yaml`, and runtime `data/*.json` as private. Only `.env.example`, `config.example.yaml`, and non-sensitive placeholders belong in git.
- Preserve the OneBot/NapCat entrypoint: `run_bot.py` -> `bot_app.main.bootstrap()`.
- Keep runtime state JSON-based unless explicitly asked to change storage.
- Do not broaden automation silently. Sending `1`, deleting messages, restarting the bot, and applying learned rules are live side effects.

## Bot Behavior

- Target group filtering is mandatory. Ignore non-target groups unless the user explicitly changes config behavior.
- Manual mode creates owner confirmation tasks. Auto mode may send `1`, but must respect cooldown and recall/correction logic.
- If someone else already sent `1` after the source message, cancel instead of sending another `1`.
- Swap logic is separate from offer logic. Exchange messages should create swap-match notices, not auto-claim as offers.
- Self-learning should stay conservative: high-confidence exact rules may be saved, but review/report commands must remain auditable.

## Change Style

- Make surgical changes only. Every changed line should map to the requested behavior.
- Prefer existing services and models over new abstractions.
- Match current Python style: small service classes, Pydantic models, async bot calls, JSON state files.
- Add tests or focused checks when changing parsing, workflow decisions, self-learning, cooldown, confirmation, or swap matching.

## Useful Entry Points

- Startup: `run_bot.py`
- Config: `config.yaml`, `.env.example`
- Runtime assembly: `src/bot_app/runtime.py`
- Group message flow: `src/bot_app/plugins/message_ingest.py`
- Owner commands: `src/bot_app/plugins/private_commands.py`
- Claim/swap workflow: `src/bot_app/services/workflow.py`
- Offer parsing: `src/bot_app/services/semantic_parse.py`
- Swap parsing: `src/bot_app/services/exchange_parse.py`
- Self-learning: `src/bot_app/services/self_learning.py`
- State: `data/state.json`
