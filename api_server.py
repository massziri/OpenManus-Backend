"""
OpenManus HTTP API Server
=========================
FastAPI wrapper that exposes the OpenManus agent as an HTTP + Server-Sent-Events
service so the PWA frontend can talk to it.

Endpoints:
  GET  /              -> health/info
  GET  /health        -> simple healthcheck for Render
  POST /api/chat      -> submit a prompt, returns SSE stream of agent thoughts + final answer
  GET  /api/config    -> returns non-sensitive runtime info (model name, etc.)

Auth: single shared secret via `X-API-Key` header (env var API_KEY).
      If API_KEY is empty, auth is disabled (dev mode).

CORS: allowed origins are read from env var ALLOWED_ORIGINS (comma-separated).
"""

import asyncio
import json
import os
import time
import uuid
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agent.manus import Manus
from app.logger import logger

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# Auth key can be provided under either name; USER_API_KEY takes priority.
# This lets deployers dodge Render's Blueprint-managed API_KEY variable
# (which is sometimes recreated at re-sync time) by defining USER_API_KEY
# in the Environment tab and leaving API_KEY untouched.
API_KEY = (os.getenv("USER_API_KEY") or os.getenv("API_KEY") or "").strip()
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]
MAX_PROMPT_LEN = int(os.getenv("MAX_PROMPT_LEN", "8000"))
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_S", "600"))

# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="OpenManus API",
    version="1.0.0",
    description="HTTP wrapper around the OpenManus agent framework.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_LEN)
    session_id: Optional[str] = Field(default=None, description="Optional client-side session id")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _check_auth(x_api_key: Optional[str], query_key: Optional[str] = None) -> None:
    if not API_KEY:
        return  # auth disabled (dev mode: no key configured on server)
    provided = x_api_key or query_key
    if not provided or provided != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _sse(event: str, data: dict) -> bytes:
    """Format a Server-Sent-Event line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


async def _run_agent_streaming(prompt: str, session_id: str) -> AsyncGenerator[bytes, None]:
    """
    Runs the OpenManus agent for a given prompt and yields SSE-formatted chunks.

    The current OpenManus agent does not natively expose per-token streaming, so
    we stream lifecycle events (start / step / done / error) plus the final
    answer. If the agent later exposes finer callbacks, this generator can be
    extended without frontend changes.
    """
    started = time.time()
    yield _sse("start", {"session_id": session_id, "prompt": prompt})

    agent = None
    try:
        agent = await Manus.create()
        yield _sse("status", {"message": "Agent initialized. Thinking..."})

        # Run the agent. `agent.run` in OpenManus returns the final result string
        # (or None) and writes intermediate reasoning to the loguru logger.
        result = await asyncio.wait_for(
            agent.run(prompt), timeout=REQUEST_TIMEOUT_S
        )

        elapsed = round(time.time() - started, 2)
        yield _sse(
            "done",
            {
                "result": result if isinstance(result, str) else str(result or ""),
                "elapsed_s": elapsed,
            },
        )

    except asyncio.TimeoutError:
        yield _sse("error", {"message": f"Agent timed out after {REQUEST_TIMEOUT_S}s."})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent run failed")
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
    finally:
        if agent is not None:
            try:
                await agent.cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("Cleanup failed")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "OpenManus API",
            "version": "1.0.0",
            "endpoints": ["/health", "/api/chat", "/api/config"],
            "auth_required": bool(API_KEY),
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/debug/whoami")
async def debug_whoami(x_api_key: Optional[str] = Header(default=None)) -> JSONResponse:
    """
    Anonymous diagnostic endpoint. NEVER returns the actual API_KEY value;
    only fingerprints (length, first char, last char, sha256 prefix) so the
    caller can compare what they sent to what the server expects, without
    exposing either secret.
    """
    import hashlib

    def fp(s: Optional[str]) -> dict:
        if not s:
            return {"present": False, "len": 0}
        digest = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:12]
        return {
            "present": True,
            "len": len(s),
            "first": s[0],
            "last": s[-1],
            "sha256_prefix": digest,
            "has_leading_space": s != s.lstrip(),
            "has_trailing_space": s != s.rstrip(),
        }

    return JSONResponse(
        {
            "server_api_key": fp(API_KEY),
            "received_api_key": fp(x_api_key),
            "match": bool(API_KEY and x_api_key and API_KEY == x_api_key),
        }
    )


@app.get("/api/config")
async def get_config(
    x_api_key: Optional[str] = Header(default=None),
    key: Optional[str] = None,  # optional ?key=... fallback
) -> JSONResponse:
    _check_auth(x_api_key, key)
    return JSONResponse(
        {
            "model": os.getenv("OPENMANUS_MODEL", "configured-in-toml"),
            "max_prompt_length": MAX_PROMPT_LEN,
            "request_timeout_s": REQUEST_TIMEOUT_S,
        }
    )


@app.post("/api/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    key: Optional[str] = None,  # optional ?key=... fallback (bypasses CORS preflight for headers)
):
    _check_auth(x_api_key, key)

    session_id = req.session_id or str(uuid.uuid4())
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt.")

    return StreamingResponse(
        _run_agent_streaming(prompt, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering on Render
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------- #
# Local dev entrypoint
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("api_server:app", host="0.0.0.0", port=port, log_level="info")
