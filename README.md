# OpenManus Backend (Render + Browserless Deploy)

**HTTP API + Server-Sent-Events wrapper around [FoundationAgents/OpenManus](https://github.com/FoundationAgents/OpenManus).**

Optimized for Render's **free tier** (512 MB RAM) with full **web-browsing capability** via a remote Chromium on [browserless.io](https://browserless.io) (also free).

## 🧠 Architecture

```
📱 PWA (Vercel)  →  🖥️ OpenManus API (Render, 512 MB)  →  🌐 Chromium (Browserless.io)
                                                        →  🔍 Web search (DuckDuckGo)
                                                        →  🤖 LLM (OpenRouter)
```

Chromium runs *outside* the Render container, connected via WebSocket/CDP. This keeps your Render service lightweight (~250 MB image) while giving the agent full browser control (navigation, clicks, scrolling, form filling, screenshots, etc.).

## 🚀 One-tap deploy from your phone

### Step 1 — Get a free Browserless token (2 min)

1. Open https://www.browserless.io in Chrome
2. **Sign Up** → free plan (no credit card required)
3. Go to **Dashboard** → your token is displayed at the top; **copy it**
4. Free tier gives you ~6 hours of browser time per month — plenty for casual use

### Step 2 — Deploy the backend

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/massziri/OpenManus-Backend)

When Render opens, it detects [`render.yaml`](./render.yaml) and asks for these secrets:

| Variable | Value |
|----------|-------|
| `OPENROUTER_API_KEY` | your OpenRouter key ([get one](https://openrouter.ai/keys)) |
| `BROWSERLESS_TOKEN` | your Browserless token (step 1) |
| `ALLOWED_ORIGINS` | `*` for now (update to your Vercel URL after PWA deploys) |

Everything else (`API_KEY`, model, browser host, timeouts) is set automatically.

### Step 3 — After the build

The build takes **~4-5 minutes** (no Chromium download → much faster than before). Once it shows **Live**:

- Copy your Render service URL (e.g. `https://openmanus-api-xxxx.onrender.com`)
- Open **Environment** tab → reveal `API_KEY` → copy that too
- Paste both into the PWA settings screen

## 📋 What was changed vs. upstream OpenManus

| File | Purpose |
|------|---------|
| [`api_server.py`](./api_server.py) | FastAPI app exposing `/api/chat` (SSE), `/api/config`, `/health` |
| [`Dockerfile.render`](./Dockerfile.render) | Lightweight image, no local Chromium |
| [`requirements.render.txt`](./requirements.render.txt) | Slim deps (removed `datasets`, `crawl4ai`, `boto3`, `gymnasium`…) |
| [`entrypoint.sh`](./entrypoint.sh) | Generates `config/config.toml` (with `wss_url`) from env vars |
| [`render.yaml`](./render.yaml) | Render Blueprint |

**Original OpenManus code untouched** — all changes are additive.

## 🆓 What "free" means here

| Service | Free tier | Enough for |
|---------|-----------|------------|
| Render web service | 750 h/mo (sleeps after 15 min idle) | Personal use, casual sessions |
| Browserless | ~6 h/mo of browser time | ~200-500 browsing requests/mo |
| OpenRouter free models | Rate-limited, ~50 req/day | Testing, light use |
| Vercel (PWA hosting) | Unlimited for hobby projects | Full-time |

**Total cost: 0 €**. For heavier use, upgrade OpenRouter (5 $ credit lasts weeks) and/or Render Starter (7 $/mo removes idle sleep and doubles RAM).

## 🔧 Local test

```bash
docker build -f Dockerfile.render -t openmanus-api .
docker run -p 8000:8000 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e BROWSERLESS_TOKEN=your-browserless-token \
  -e API_KEY=dev-key \
  -e ALLOWED_ORIGINS='*' \
  openmanus-api

curl -N http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{"prompt":"Search DuckDuckGo for latest AI news"}'
```

## 🧪 Companion PWA

The frontend that talks to this API lives at:
👉 **[massziri/OpenManus-PWA](https://github.com/massziri/OpenManus-PWA)**

## 📄 Credits

Based on the original [OpenManus project](https://github.com/FoundationAgents/OpenManus) by the MetaGPT team.
Licensed under MIT.
