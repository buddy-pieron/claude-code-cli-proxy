"""
Claude Code Proxy — OpenAI-compatible API server powered by Claude Code CLI.

Turn your Claude Code subscription into a local API. Every request goes through
the `claude` CLI binary, so you pay $0 per token on top of your existing
subscription.

Endpoints:
  POST /v1/chat/completions   OpenAI chat format (streaming + non-streaming)
  POST /v1/messages           Anthropic messages format
  GET  /v1/models             List available models
  GET  /health                Health check
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from claude_cli import ClaudeCLIProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("claude-code-proxy")

HOST = os.getenv("CLAUDE_PROXY_HOST", "127.0.0.1")
PORT = int(os.getenv("CLAUDE_PROXY_PORT", "8070"))
API_KEY = os.getenv("CLAUDE_PROXY_API_KEY", "")

MODELS = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

provider = ClaudeCLIProvider()

app = FastAPI(
    title="Claude Code Proxy",
    version="1.0.0",
    description="OpenAI-compatible API powered by Claude Code CLI",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_auth(request: Request):
    if not API_KEY:
        return
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
async def health():
    ok = await provider.health_check()
    return {"status": "ok" if ok else "degraded", "provider": "claude-cli"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "anthropic"}
            for m in MODELS
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_auth(request)
    body = await request.json()
    stream = body.get("stream", False)

    if stream:
        return StreamingResponse(
            provider.stream_completion(body),
            media_type="text/event-stream",
        )
    else:
        result = await provider.complete(body)
        return JSONResponse(result)


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    _check_auth(request)
    body = await request.json()
    openai_body = _anthropic_to_openai(body)
    stream = body.get("stream", False)

    if stream:
        async def translate_stream():
            async for chunk in provider.stream_completion(openai_body):
                yield chunk
        return StreamingResponse(
            translate_stream(),
            media_type="text/event-stream",
        )
    else:
        result = await provider.complete(openai_body)
        anthropic_resp = _openai_to_anthropic(result, body.get("model", ""))
        return JSONResponse(anthropic_resp)


def _anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            sys_text = "\n\n".join(
                blk.get("text", "") for blk in system
                if isinstance(blk, dict) and blk.get("type") == "text"
            )
        else:
            sys_text = str(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            text = " ".join(
                blk.get("text", "") for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            )
            messages.append({"role": role, "content": text})

    return {
        "model": body.get("model", ""),
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
        "temperature": body.get("temperature"),
        "stream": body.get("stream", False),
    }


_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    None: "end_turn",
}


def _openai_to_anthropic(result: dict, model: str) -> dict:
    choice = (result.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    usage = result.get("usage", {})
    return {
        "id": result.get("id", f"msg_{uuid.uuid4().hex[:16]}"),
        "type": "message",
        "role": "assistant",
        "model": model or result.get("model", ""),
        "content": [{"type": "text", "text": msg.get("content", "")}],
        "stop_reason": _STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def main():
    logger.info(f"Starting Claude Code Proxy on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
