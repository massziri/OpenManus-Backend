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
from typing import AsyncGenerator, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Header, Request
from openai import AsyncOpenAI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agent.manus import Manus
from app.llm import LLM
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
    version="1.1.0",
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
class Attachment(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    mime_type: str = Field(..., min_length=1, max_length=120)
    kind: Literal["image", "text"]
    data_url: Optional[str] = None
    text_content: Optional[str] = None
    size: Optional[int] = None


class ChatRequest(BaseModel):
    prompt: str = Field(default="", max_length=MAX_PROMPT_LEN)
    session_id: Optional[str] = Field(default=None, description="Optional client-side session id")
    attachments: List[Attachment] = Field(default_factory=list)


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


def _first_nonempty(*values: Optional[str]) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _resolve_vision_config() -> dict[str, str]:
    """
    Resolve vision settings with safe fallbacks.

    Priority order:
      1) Explicit VISION_* environment variables
      2) Main OpenManus LLM environment variables
      3) OpenRouter/OpenAI legacy fallbacks for api_key only
    """
    api_key = _first_nonempty(
        os.getenv("VISION_API_KEY"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("OPENROUTER_API_KEY"),
    )
    base_url = _first_nonempty(
        os.getenv("VISION_BASE_URL"),
        os.getenv("OPENMANUS_BASE_URL"),
        os.getenv("BASE_URL"),
    )
    model = _first_nonempty(
        os.getenv("VISION_MODEL"),
        os.getenv("OPENMANUS_MODEL"),
        os.getenv("MODEL"),
    )
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def _has_vision_config() -> bool:
    vision = _resolve_vision_config()
    return bool(vision["api_key"] and vision["base_url"] and vision["model"])


def _prepare_attachment_context(attachments: List[Attachment]) -> tuple[str, List[str]]:
    text_blocks: list[str] = []
    image_urls: list[str] = []

    for attachment in attachments:
        if attachment.kind == "text" and attachment.text_content:
            body = attachment.text_content.strip()
            if not body:
                continue
            truncated = body[:20000]
            suffix = "\n[truncated]" if len(body) > len(truncated) else ""
            text_blocks.append(
                f"Attached file: {attachment.name} ({attachment.mime_type})\n```\n{truncated}{suffix}\n```"
            )
        elif attachment.kind == "image" and attachment.data_url:
            image_urls.append(attachment.data_url)

    merged_text = "\n\n".join(text_blocks)
    return merged_text, image_urls


async def _vision_describe(prompt: str, image_urls: List[str]) -> str:
    vision = _resolve_vision_config()
    if not (vision["api_key"] and vision["base_url"] and vision["model"]):
        raise ValueError(
            "Image analysis is not configured. Set VISION_* variables or rely on OPENAI_API_KEY + OPENMANUS_BASE_URL + OPENMANUS_MODEL fallbacks."
        )

    client = AsyncOpenAI(
        api_key=vision["api_key"],
        base_url=vision["base_url"],
    )
    system_text = os.getenv(
        "VISION_SYSTEM_PROMPT",
        "Analyze the attached image carefully and answer in the user's language. Be factual and concise.",
    )
    content = [{"type": "text", "text": prompt}]
    for image_url in image_urls[:3]:
        content.append({"type": "image_url", "image_url": {"url": image_url}})

    response = await client.chat.completions.create(
        model=vision["model"],
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": content},
        ],
        max_tokens=1200,
    )
    if not response.choices or not response.choices[0].message.content:
        raise ValueError("Empty response from vision model")
    return response.choices[0].message.content


def _needs_full_agent(prompt: str) -> bool:
    """Use the full tool-enabled agent only when the request appears to need tools."""
    lowered = prompt.lower()
    markers = (
        "http://",
        "https://",
        "www.",
        "browser",
        "navig",
        "ouvre",
        "open ",
        "site",
        "website",
        "url",
        "click",
        "search",
        "recherche",
        "web",
        "scrape",
        "screenshot",
        "capture",
        "fichier",
        "file",
        "python",
        "code",
        "terminal",
        "bash",
        "csv",
        "excel",
        "pdf",
        "image jointe",
        "pièce jointe",
    )
    return any(marker in lowered for marker in markers)


async def _run_agent_streaming(prompt: str, session_id: str, attachments: List[Attachment]) -> AsyncGenerator[bytes, None]:
    """
    Runs the OpenManus agent for a given prompt and yields SSE-formatted chunks.

    The current OpenManus agent does not natively expose per-token streaming, so
    we stream lifecycle events (start / step / done / error) plus the final
    answer. If the agent later exposes finer callbacks, this generator can be
    extended without frontend changes.
    """
    started = time.time()
    text_context, image_urls = _prepare_attachment_context(attachments)
    yield _sse(
        "start",
        {
            "session_id": session_id,
            "prompt": prompt,
            "attachments": [
                {"name": a.name, "kind": a.kind, "mime_type": a.mime_type}
                for a in attachments
            ],
        },
    )

    effective_prompt = prompt.strip() or "Analyze the attached content and answer the user clearly."
    if text_context:
        effective_prompt = f"{effective_prompt}\n\n{text_context}"

    agent = None
    try:
        if image_urls and not _has_vision_config():
            raise ValueError(
                "Image analysis is not enabled on this backend yet. Configure VISION_API_KEY / VISION_BASE_URL / VISION_MODEL, or set OPENAI_API_KEY + OPENMANUS_BASE_URL + OPENMANUS_MODEL so the automatic vision fallback can activate."
            )

        if image_urls and not _needs_full_agent(prompt):
            yield _sse("status", {"message": "Analyzing attached image..."})
            result = await asyncio.wait_for(
                _vision_describe(effective_prompt, image_urls),
                timeout=min(90, REQUEST_TIMEOUT_S),
            )
        elif attachments and not image_urls:
            yield _sse("status", {"message": "Analyzing attached text file..."})
            llm = LLM()
            result = await asyncio.wait_for(
                llm.ask(
                    messages=[{"role": "user", "content": effective_prompt}],
                    system_msgs=[
                        {
                            "role": "system",
                            "content": "You are OpenManus. Analyze the attached text content directly and answer in the user's language. Do not ask for a path unless the user explicitly requests filesystem actions.",
                        }
                    ],
                    stream=False,
                ),
                timeout=min(90, REQUEST_TIMEOUT_S),
            )
        elif not _needs_full_agent(prompt):
            yield _sse("status", {"message": "Direct answer mode..."})
            llm = LLM()
            result = await asyncio.wait_for(
                llm.ask(
                    messages=[{"role": "user", "content": effective_prompt}],
                    system_msgs=[
                        {
                            "role": "system",
                            "content": "You are OpenManus. Answer directly, briefly, and in the user's language. Use tools only when explicitly necessary.",
                        }
                    ],
                    stream=False,
                ),
                timeout=min(90, REQUEST_TIMEOUT_S),
            )
        else:
            agent_prompt = effective_prompt
            if image_urls:
                yield _sse("status", {"message": "Analyzing attached image before tool run..."})
                image_summary = await asyncio.wait_for(
                    _vision_describe(
                        "Summarize the attached image so another text-only agent can use it. Focus on the user's goal if stated: " + (prompt or "analyze the image"),
                        image_urls,
                    ),
                    timeout=min(90, REQUEST_TIMEOUT_S),
                )
                agent_prompt += f"\n\nAttached image analysis:\n{image_summary}"

            agent = await Manus.create()
            yield _sse("status", {"message": "Agent initialized. Thinking..."})

            # Run the agent. `agent.run` in OpenManus returns the final result string
            # (or None) and writes intermediate reasoning to the loguru logger.
            result = await asyncio.wait_for(
                agent.run(agent_prompt), timeout=REQUEST_TIMEOUT_S
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
            "version": "1.1.0",
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
    vision = _resolve_vision_config()
    return JSONResponse(
        {
            "model": os.getenv("OPENMANUS_MODEL", vision["model"] or "configured-in-toml"),
            "max_prompt_length": MAX_PROMPT_LEN,
            "request_timeout_s": REQUEST_TIMEOUT_S,
            "attachments": {
                "text": True,
                "image": _has_vision_config(),
            },
            "vision": {
                "enabled": _has_vision_config(),
                "provider": vision["base_url"] or None,
                "model": vision["model"] or None,
            },
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
    if not prompt and not req.attachments:
        raise HTTPException(status_code=400, detail="Empty prompt.")

    return StreamingResponse(
        _run_agent_streaming(prompt, session_id, req.attachments),
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
