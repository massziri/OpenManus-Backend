# OpenManus Backend (Render Deploy)

**HTTP API + Server-Sent-Events wrapper around [FoundationAgents/OpenManus](https://github.com/FoundationAgents/OpenManus).**

This fork adds everything needed to run OpenManus as a hosted API that a mobile PWA can talk to.

## 🚀 Deploy to Render in one click

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/massziri/OpenManus-Backend)

When Render opens, it will detect [`render.yaml`](./render.yaml) automatically and prompt you for the secrets you need to provide:

- **`OPENROUTER_API_KEY`** → your OpenRouter API key (get one free at https://openrouter.ai/keys)
- **`ALLOWED_ORIGINS`** → put `*` for now; update it later with your Vercel PWA URL

Everything else (`API_KEY`, model, timeouts) is set automatically.

## What's inside

| File | Purpose |
|------|---------|
| [`api_server.py`](./api_server.py) | FastAPI app exposing `/api/chat` (SSE), `/api/config`, `/health` |
| [`Dockerfile.render`](./Dockerfile.render) | Optimized image with Playwright + Chromium |
| [`entrypoint.sh`](./entrypoint.sh) | Generates `config/config.toml` from env vars at boot |
| [`render.yaml`](./render.yaml) | Render Blueprint (auto-provisioning) |

Nothing in the original OpenManus code was modified — only additions.

## Free-tier caveats

- Service sleeps after **15 min** of inactivity (cold start ~30-60s next request)
- **512 MB RAM** limit — heavy browser automation may OOM. Upgrade to Starter ($7/mo) if needed
- OpenRouter free models have **rate limits** (~50 req/day). Add a few $ of credit for daily use

## Companion PWA

The frontend that talks to this API lives at:
👉 **[massziri/OpenManus-PWA](https://github.com/massziri/OpenManus-PWA)**

## Local test

```bash
docker build -f Dockerfile.render -t openmanus-api .
docker run -p 8000:8000 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e API_KEY=dev-key \
  -e ALLOWED_ORIGINS='*' \
  openmanus-api

curl -N http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{"prompt":"What is 2+2?"}'
```

## Credits

Based on the original [OpenManus project](https://github.com/FoundationAgents/OpenManus) by MetaGPT team.
Licensed under MIT.
