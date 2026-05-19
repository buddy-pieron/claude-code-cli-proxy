"""
Claude CLI Provider — wraps the local `claude` binary.

Spawns `claude --print --output-format stream-json` and translates its
event stream into OpenAI-compatible responses (both streaming SSE and
non-streaming JSON).

Auth uses whatever OAuth credentials Claude CLI has in
~/.claude/.credentials.json — so every request bills against your
Claude Code subscription, not per-token API spend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from typing import Any, AsyncGenerator

logger = logging.getLogger("claude-code-proxy")

DEFAULT_BIN = shutil.which("claude") or "claude"

ALLOWED_DIRS = ["/tmp", os.path.expanduser("~")]


def _flatten_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                out.append(item.get("text", ""))
        return "".join(out)
    return str(content or "")


def _split_messages(messages: list[dict[str, Any]]) -> tuple[str, str, str]:
    system_parts: list[str] = []
    transcript_lines: list[str] = []
    latest_user = ""

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = _flatten_text(msg.get("content"))

        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role == "assistant":
            if content:
                transcript_lines.append(f"ASSISTANT: {content}")
            continue
        if role == "user":
            if latest_user:
                transcript_lines.append(f"USER: {latest_user}")
            latest_user = content
            continue
        if role == "tool":
            transcript_lines.append(
                f"TOOL_RESULT[{msg.get('tool_call_id', '')}]: {content}"
            )
            continue

    system = "\n\n".join(s for s in system_parts if s)
    transcript = "\n".join(transcript_lines)
    return system, transcript, latest_user


def _build_prompt(transcript: str, latest_user: str) -> str:
    if not transcript:
        return latest_user
    return (
        "Previous conversation transcript:\n"
        f"{transcript}\n\n"
        "Current user message:\n"
        f"{latest_user}"
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


class ClaudeCLIProvider:
    def __init__(
        self,
        bin_path: str | None = None,
        default_model: str = "claude-sonnet-4-6",
        allowed_dirs: list[str] | None = None,
    ):
        self._bin = bin_path or os.getenv("CLAUDE_BIN") or DEFAULT_BIN
        self._default_model = default_model
        self._allowed_dirs = allowed_dirs or ALLOWED_DIRS
        if not shutil.which(self._bin) and not os.path.exists(self._bin):
            logger.warning(f"claude binary not found at '{self._bin}'")

    def _resolve_model(self, params: dict) -> str:
        m = (params.get("model") or "").strip()
        if "/" in m:
            _, m = m.split("/", 1)
        return m or self._default_model

    def _build_argv(self, model: str, system: str) -> list[str]:
        argv = [
            self._bin,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
            "--dangerously-skip-permissions",
        ]
        for d in self._allowed_dirs:
            argv.extend(["--add-dir", d])
        if system:
            argv.extend(["--append-system-prompt", system])
        return argv

    async def _spawn(self, model: str, system: str, prompt: str):
        argv = self._build_argv(model, system)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
        )
        if proc.stdin:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        return proc

    @staticmethod
    def _parse_event_line(line: bytes) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception:
            return None

    async def complete(self, params: dict) -> dict:
        model = self._resolve_model(params)
        system, transcript, latest_user = _split_messages(
            params.get("messages") or []
        )
        prompt = _build_prompt(transcript, latest_user)

        proc = await self._spawn(model, system, prompt)
        text = ""
        in_tokens = 0
        out_tokens = 0
        stop_reason = "stop"
        is_error = False
        err_detail = ""
        FIRST_CHUNK_TIMEOUT = 90.0
        IDLE_TIMEOUT = 60.0

        try:
            assert proc.stdout
            saw_any = False
            while True:
                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=(
                            IDLE_TIMEOUT if saw_any else FIRST_CHUNK_TIMEOUT
                        ),
                    )
                except asyncio.TimeoutError:
                    is_error = True
                    err_detail = (
                        "stream idle timeout"
                        if saw_any
                        else "no-output timeout"
                    )
                    break
                if not raw:
                    break
                saw_any = True
                evt = self._parse_event_line(raw)
                if not evt:
                    continue
                etype = evt.get("type")
                if etype == "result":
                    is_error = bool(evt.get("is_error"))
                    if is_error:
                        err_detail = str(evt.get("result") or "")[:500]
                    text = evt.get("result") or text
                    usage = evt.get("usage") or {}
                    in_tokens = int(usage.get("input_tokens") or 0)
                    out_tokens = int(usage.get("output_tokens") or 0)
                    sr = evt.get("stop_reason")
                    if sr:
                        stop_reason = "stop" if sr == "end_turn" else sr
        finally:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

        if is_error:
            raise RuntimeError(f"claude-cli error: {err_detail or 'unknown'}")

        return {
            "id": f"chatcmpl-cli-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": params.get("model") or model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": stop_reason,
                }
            ],
            "usage": {
                "prompt_tokens": in_tokens,
                "completion_tokens": out_tokens,
                "total_tokens": in_tokens + out_tokens,
            },
        }

    async def stream_completion(
        self, params: dict
    ) -> AsyncGenerator[str, None]:
        model = self._resolve_model(params)
        system, transcript, latest_user = _split_messages(
            params.get("messages") or []
        )
        prompt = _build_prompt(transcript, latest_user)

        cid = f"chatcmpl-cli-{uuid.uuid4().hex[:16]}"
        model_label = params.get("model") or model

        yield _sse(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_label,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        )

        proc = await self._spawn(model, system, prompt)
        finish_reason: str | None = None
        in_tokens = 0
        out_tokens = 0
        emitted_any_text = False
        FIRST_CHUNK_TIMEOUT = 90.0
        IDLE_TIMEOUT = 60.0

        try:
            assert proc.stdout
            saw_any = False
            while True:
                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=(
                            IDLE_TIMEOUT if saw_any else FIRST_CHUNK_TIMEOUT
                        ),
                    )
                except asyncio.TimeoutError:
                    timeout_kind = "idle" if saw_any else "no-output"
                    yield _sse(
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_label,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "content": f"[claude-cli error] stream {timeout_kind} timeout"
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    finish_reason = "stop"
                    break
                if not raw:
                    break
                saw_any = True
                evt = self._parse_event_line(raw)
                if not evt:
                    continue
                etype = evt.get("type")
                if etype == "stream_event":
                    inner = evt.get("event") or {}
                    inner_type = inner.get("type")
                    if inner_type == "content_block_delta":
                        delta = inner.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            t = delta.get("text") or ""
                            if t:
                                emitted_any_text = True
                                yield _sse(
                                    {
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model_label,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": t},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                )
                    elif inner_type == "message_delta":
                        usage = inner.get("usage") or {}
                        if usage.get("input_tokens") is not None:
                            in_tokens = int(
                                usage.get("input_tokens") or in_tokens
                            )
                        if usage.get("output_tokens") is not None:
                            out_tokens = int(
                                usage.get("output_tokens") or out_tokens
                            )
                        sr = (inner.get("delta") or {}).get("stop_reason")
                        if sr:
                            finish_reason = (
                                "stop" if sr == "end_turn" else sr
                            )
                elif etype == "result":
                    if bool(evt.get("is_error")):
                        err = str(
                            evt.get("result") or "claude-cli error"
                        )[:500]
                        yield _sse(
                            {
                                "id": cid,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_label,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "content": f"[claude-cli error] {err}"
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        )
                        finish_reason = "stop"
                    else:
                        usage = evt.get("usage") or {}
                        if usage.get("input_tokens") is not None:
                            in_tokens = int(
                                usage.get("input_tokens") or in_tokens
                            )
                        if usage.get("output_tokens") is not None:
                            out_tokens = int(
                                usage.get("output_tokens") or out_tokens
                            )
                        if not emitted_any_text:
                            full = evt.get("result") or ""
                            if full:
                                yield _sse(
                                    {
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model_label,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": full},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                )
                        sr = evt.get("stop_reason")
                        if sr and not finish_reason:
                            finish_reason = (
                                "stop" if sr == "end_turn" else sr
                            )
        finally:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

        yield _sse(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_label,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": finish_reason or "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": in_tokens,
                    "completion_tokens": out_tokens,
                    "total_tokens": in_tokens + out_tokens,
                },
            }
        )
        yield "data: [DONE]\n\n"

    async def health_check(self) -> bool:
        return bool(
            self._bin
            and (shutil.which(self._bin) or os.path.exists(self._bin))
        )
