# OpenManus — Render Deployment Guide

This fork adds an HTTP + Server-Sent-Events layer on top of OpenManus so you can
call the agent from a web/PWA frontend and deploy it on **Render** in one click.

## What was added

| File | Purpose |
|------|---------|
| `api_server.py` | FastAPI app exposing `/api/chat` (SSE), `/api/config`, `/health` |
| `Dockerfile.render` | Optimized image with Playwright + Chromium |
| `entrypoint.sh` | Generates `config/config.toml` from env vars at boot |
| `render.yaml` | Render Blueprint (auto-provisioning) |

Nothing in the original OpenManus code was modified.

## 1-minute deploy

1. **Fork this repo** on your GitHub account (already done if you're reading this).
2. Go to https://dashboard.render.com → **New +** → **Blueprint**.
3. Connect your GitHub and select this repo. Render detects `render.yaml`.
4. Fill in the two secrets it asks for:
   - `OPENROUTER_API_KEY` → your OpenRouter key (get one free at https://openrouter.ai/keys)
   - `ALLOWED_ORIGINS` → the URL of your Vercel PWA, e.g. `https://openmanus-pwa.vercel.app`
5. Click **Apply**. First build takes ~8-10 min (Playwright chromium download).
6. When status becomes **Live**, copy your service URL, e.g.
   `https://openmanus-api-xxxx.onrender.com`
7. In the Render dashboard, open the service → **Environment** tab → copy the
   auto-generated `API_KEY`. You'll paste both URL and key into the PWA.

## Free-tier caveats

- Service **sleeps after 15 min** of inactivity. First request after sleep takes
  30-60 s (cold start).
- **512 MB RAM** limit. Heavy browser automation may OOM. For serious use,
  upgrade to Starter ($7/mo).
- **No persistent disk** on free tier. Files written by the agent to
  `/workspace` are lost on restart. Mount a Render Disk if needed.

## Environment variables

| Var | Required | Default | Description |
|-----|----------|---------|-------------|
| `OPENROUTER_API_KEY` | ✅ | — | Your OpenRouter API key |
| `OPENMANUS_MODEL` | | `deepseek/deepseek-chat-v3.1:free` | Any OpenRouter model slug |
| `OPENMANUS_BASE_URL` | | `https://openrouter.ai/api/v1` | LLM API base URL |
| `API_KEY` | | (auto-generated) | Shared secret between backend and PWA |
| `ALLOWED_ORIGINS` | ✅ | `*` | Comma-separated list of allowed frontend URLs |
| `REQUEST_TIMEOUT_S` | | `600` | Max seconds per agent run |
| `MAX_PROMPT_LEN` | | `8000` | Max characters accepted per prompt |

## Recommended free OpenRouter models

- `deepseek/deepseek-chat-v3.1:free` — good general reasoning
- `google/gemini-2.0-flash-exp:free` — fast, multimodal
- `meta-llama/llama-3.3-70b-instruct:free` — strong open model
- `qwen/qwen3-coder:free` — best for code tasks

## Local test

```bash
docker build -f Dockerfile.render -t openmanus-api .
docker run -p 8000:8000 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e API_KEY=dev-key \
  -e ALLOWED_ORIGINS='*' \
  openmanus-api

# in another terminal:
curl -N http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{"prompt":"What is 2+2?"}'
```

You should see a stream of SSE events: `start`, `status`, `done`.
