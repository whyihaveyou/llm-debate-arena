# AI Debate Arena

Multi-model debate and chat platform for mathematics, physics, and computer science.

## Features

- **Single Chat** — Chat with one AI model, multi-turn with streaming
- **Sequential Debate** — Model A answers, Model B reviews, alternating turns
- **Blind Debate** — Both models answer independently, then exchange perspectives
- **3-Way Chain Debate** — A → B → C → A chain rotation
- **BYOK** — Bring Your Own API Key for any OpenAI-compatible model
- **Post-debate tools** — Compile derivation, analyze persuasion, score quality
- **File upload** — Attach PDF/code/markdown as reference material
- **Export** — Markdown and PDF export
- **i18n** — Chinese/English UI and system prompts

## Quick Start

```bash
# 1. Clone
git clone https://github.com/whyihaveyou/llm-debate-arena.git
cd llm-debate-arena

# 2. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp config.example.json config.json
# Edit config.json: add API keys for built-in models

# 4. Run
python3 app.py
# Open http://localhost:8765
```

## Configuration

### config.json

```json
{
  "models": {
    "model-id": {
      "name": "Display Name",
      "base_url": "https://api.example.com/v1",
      "api_key": "your-key-here",
      "model": "model-id",
      "auth_type": "bearer"
    }
  },
  "server": { "host": "0.0.0.0", "port": 8765 },
  "password": "CHANGE_ME",
  "max_rounds": 20,
  "disagreement_threshold": 5,
  "quota": { "monthly_limit": 50, "topic_max_length": 2000 },
  "rate_limit": { "max_debates_per_hour": 10, "max_concurrent_debates": 3 }
}
```

### Auth Types

| auth_type | Header format |
|-----------|---------------|
| `bearer` | `Authorization: Bearer <key>` |
| `api-key` | `api-key: <key>` |
| `anthropic` | `x-api-key: <key>` + `anthropic-version: 2023-06-01` |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DEBATE_ENCRYPTION_KEY` | Fernet key for encrypting user API keys (generate with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |

## Deployment

For public access, use a reverse proxy or tunnel:

```bash
# Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8765

# Or serve directly with uvicorn
uvicorn app:app --host 0.0.0.0 --port 8765
```

PDF export requires Chrome/Chromium installed on the server.

## Tech Stack

- **Backend**: FastAPI + SQLite + SSE/WebSocket
- **Frontend**: Vanilla HTML/CSS/JS + Tailwind CDN + KaTeX + highlight.js
- **Auth**: bcrypt password hashing, Fernet key encryption
