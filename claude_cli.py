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
import base64
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from typing import Any, AsyncGenerator

logger = logging.getLogger("claude-code-proxy")

DEFAULT_BIN = shutil.which("claude") or "claude"
ALLOWED_DIRS = ["/tmp", os.path.expanduser("~")]

FIRST_CHUNK_TIMEOUT = float(os.getenv("CLAUDE_PROXY_FIRST_CHUNK_TIMEOUT", "180"))
IDLE_TIMEOUT = float(os.getenv("CLAUDE_PROXY_IDLE_TIMEOUT", "120"))
MAX_CONCURRENT = int(os.getenv("CLAUDE_PROXY_MAX_CONCURRENT", "2"))
STREAM_BUFFER_LIMIT = 16 * 1024 * 1024
HEARTBEAT_INTERVAL = float(os.getenv("CLAUDE_PROXY_HEARTBEAT_INTERVAL", "15"))
CB_FAILURE_THRESHOLD = int(os.getenv("CLAUDE_PROXY_CB_FAILURE_THRESHOLD", "5"))
CB_RECOVERY_TIMEOUT = float(os.getenv("CLAUDE_PROXY_CB_RECOVERY_TIMEOUT", "30"))


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = CB_FAILURE_THRESHOLD,
        recovery_timeout: float = CB_RECOVERY_TIMEOUT,
    ):
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._last_failure_time = 0.0
        self._state = "closed"
        self._total_trips = 0

    def can_execute(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                self._state = "half-open"
                logger.info("circuit breaker half-open, allowing probe request")
                return True
            return False
        return True

    def record_success(self):
        if self._state == "half-open":
            logger.info("circuit breaker closed after successful probe")
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            if self._state != "open":
                self._total_trips += 1
                logger.warning(
                    f"circuit breaker OPEN after {self._failure_count} failures "
                    f"(trip #{self._total_trips})"
                )
            self._state = "open"

    @property
    def state(self) -> str:
        if (
            self._state == "open"
            and time.monotonic() - self._last_failure_time >= self._recovery_timeout
        ):
            return "half-open"
        return self._state

    @property
    def stats(self) -> dict:
        return {
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
            "recovery_timeout_sec": self._recovery_timeout,
            "total_trips": self._total_trips,
        }


class CLIError(Exception):
    """Claude CLI returned an error."""
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class CLITimeoutError(CLIError):
    """Claude CLI timed out."""
    def __init__(self, message: str):
        super().__init__(message, status_code=504)


class CLIUnavailableError(CLIError):
    """Claude CLI binary not found."""
    def __init__(self, message: str = "Claude CLI binary not found"):
        super().__init__(message, status_code=503)


def _save_base64_image(data: str, media_type: str = "image/png") -> str | None:
    try:
        ext = media_type.split("/")[-1].replace("jpeg", "jpg")
        img_bytes = base64.b64decode(data)
        fname = f"claude_proxy_img_{uuid.uuid4().hex[:8]}.{ext}"
        fpath = os.path.join(tempfile.gettempdir(), fname)
        with open(fpath, "wb") as f:
            f.write(img_bytes)
        return fpath
    except Exception as e:
        logger.warning(f"Failed to decode image: {e}")
        return None


def _flatten_content(content: Any) -> tuple[str, list[str]]:
    """Extract text and save any inline images to temp files.
    Returns (text, list_of_temp_image_paths).
    """
    temp_files: list[str] = []

    if isinstance(content, str):
        return content, temp_files

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")

            if item_type == "text":
                text_parts.append(item.get("text", ""))

            elif item_type == "image_url":
                url_data = item.get("image_url", {})
                url = url_data.get("url", "") if isinstance(url_data, dict) else str(url_data)
                if url.startswith("data:image/"):
                    header, _, b64data = url.partition(",")
                    media_type = header.split(";")[0].replace("data:", "")
                    path = _save_base64_image(b64data, media_type)
                    if path:
                        temp_files.append(path)
                        text_parts.append(f"[Attached image saved at: {path}]")
                    else:
                        text_parts.append("[Image: decode failed]")
                elif url.startswith("http"):
                    text_parts.append(f"[Image URL: {url}]")

            elif item_type == "image":
                source = item.get("source", {})
                if source.get("type") == "base64":
                    path = _save_base64_image(
                        source.get("data", ""),
                        source.get("media_type", "image/png"),
                    )
                    if path:
                        temp_files.append(path)
                        text_parts.append(f"[Attached image saved at: {path}]")
                elif source.get("type") == "url":
                    text_parts.append(f"[Image URL: {source.get('url', '')}]")

        return " ".join(text_parts), temp_files

    return str(content or ""), temp_files


def _flatten_text(content: Any) -> str:
    text, _ = _flatten_content(content)
    return text


def _cleanup_temp_files(paths: list[str]):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _split_messages(messages: list[dict[str, Any]]) -> tuple[str, str, str, list[str]]:
    """Split messages into (system, transcript, latest_user, temp_image_paths)."""
    system_parts: list[str] = []
    transcript_lines: list[str] = []
    latest_user = ""
    all_temp_files: list[str] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text, temp_files = _flatten_content(msg.get("content"))
        all_temp_files.extend(temp_files)

        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "assistant":
            if text:
                transcript_lines.append(f"ASSISTANT: {text}")
        elif role == "user":
            if latest_user:
                transcript_lines.append(f"USER: {latest_user}")
            latest_user = text
        elif role == "tool":
            transcript_lines.append(
                f"TOOL_RESULT[{msg.get('tool_call_id', '')}]: {text}"
            )

    system = "\n\n".join(s for s in system_parts if s)
    transcript = "\n".join(transcript_lines)
    return system, transcript, latest_user, all_temp_files


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
        max_concurrent: int | None = None,
    ):
        self._bin = bin_path or os.getenv("CLAUDE_BIN") or DEFAULT_BIN
        self._default_model = default_model
        self._allowed_dirs = allowed_dirs or ALLOWED_DIRS
        self._semaphore = asyncio.Semaphore(max_concurrent or MAX_CONCURRENT)
        self._circuit_breaker = CircuitBreaker()
        self._req_counter = 0
        if not shutil.which(self._bin) and not os.path.exists(self._bin):
            logger.warning(f"claude binary not found at '{self._bin}'")

    def _next_req_id(self) -> str:
        self._req_counter += 1
        return f"req-{self._req_counter}"

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
            limit=STREAM_BUFFER_LIMIT,
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
        system, transcript, latest_user, temp_files = _split_messages(
            params.get("messages") or []
        )
        prompt = _build_prompt(transcript, latest_user)
        req_id = self._next_req_id()
        t0 = time.monotonic()

        logger.info(f"[{req_id}] complete model={model} prompt_len={len(prompt)}")

        if not self._circuit_breaker.can_execute():
            _cleanup_temp_files(temp_files)
            raise CLIUnavailableError(
                f"circuit breaker open ({self._circuit_breaker.stats['failure_count']} consecutive failures)"
            )

        try:
            try:
                result = await self._run_complete(params, model, system, prompt, req_id, t0)
                self._circuit_breaker.record_success()
                return result
            except CLITimeoutError:
                logger.warning(f"[{req_id}] timeout, retrying once")
                await asyncio.sleep(2)
                result = await self._run_complete(params, model, system, prompt, req_id, t0)
                self._circuit_breaker.record_success()
                return result
        except (CLIError, CLITimeoutError, CLIUnavailableError):
            self._circuit_breaker.record_failure()
            raise
        finally:
            _cleanup_temp_files(temp_files)

    async def _run_complete(
        self, params: dict, model: str, system: str, prompt: str,
        req_id: str, t0: float,
    ) -> dict:
        async with self._semaphore:
            proc = await self._spawn(model, system, prompt)
            text = ""
            in_tokens = 0
            out_tokens = 0
            stop_reason = "stop"
            is_error = False
            err_detail = ""

            try:
                assert proc.stdout
                saw_any = False
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=IDLE_TIMEOUT if saw_any else FIRST_CHUNK_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        kind = "idle" if saw_any else "no-output"
                        raise CLITimeoutError(f"stream {kind} timeout")
                    if not raw:
                        break
                    saw_any = True
                    evt = self._parse_event_line(raw)
                    if not evt:
                        continue
                    if evt.get("type") == "result":
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

            elapsed = time.monotonic() - t0
            logger.info(
                f"[{req_id}] done {elapsed:.1f}s in={in_tokens} out={out_tokens} error={is_error}"
            )

            if is_error:
                raise CLIError(err_detail or "unknown CLI error")

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
        system, transcript, latest_user, temp_files = _split_messages(
            params.get("messages") or []
        )
        prompt = _build_prompt(transcript, latest_user)
        req_id = self._next_req_id()
        t0 = time.monotonic()

        logger.info(f"[{req_id}] stream model={model} prompt_len={len(prompt)}")

        cid = f"chatcmpl-cli-{uuid.uuid4().hex[:16]}"
        model_label = params.get("model") or model

        if not self._circuit_breaker.can_execute():
            _cleanup_temp_files(temp_files)
            yield _sse({
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_label,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "[error] circuit breaker open"}, "finish_reason": "stop"}
                ],
            })
            yield "data: [DONE]\n\n"
            return

        yield _sse({
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_label,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
            ],
        })

        finish_reason: str | None = None
        in_tokens = 0
        out_tokens = 0
        emitted_any_text = False
        is_error = False

        await self._semaphore.acquire()
        try:
            proc = await self._spawn(model, system, prompt)
            try:
                assert proc.stdout
                saw_any = False
                while True:
                    total_wait = IDLE_TIMEOUT if saw_any else FIRST_CHUNK_TIMEOUT
                    wait_elapsed = 0.0
                    got_line = False
                    raw = b""
                    readline_task = asyncio.ensure_future(proc.stdout.readline())
                    while wait_elapsed < total_wait:
                        chunk_wait = min(HEARTBEAT_INTERVAL, total_wait - wait_elapsed)
                        try:
                            raw = await asyncio.wait_for(
                                asyncio.shield(readline_task),
                                timeout=chunk_wait,
                            )
                            got_line = True
                            break
                        except asyncio.TimeoutError:
                            wait_elapsed += chunk_wait
                            if wait_elapsed < total_wait:
                                yield ": keepalive\n\n"

                    if not got_line:
                        readline_task.cancel()
                        kind = "idle" if saw_any else "no-output"
                        is_error = True
                        yield _sse({
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_label,
                            "choices": [
                                {"index": 0, "delta": {"content": f"[error] stream {kind} timeout"}, "finish_reason": None}
                            ],
                        })
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
                                    yield _sse({
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model_label,
                                        "choices": [
                                            {"index": 0, "delta": {"content": t}, "finish_reason": None}
                                        ],
                                    })
                        elif inner_type == "message_delta":
                            usage = inner.get("usage") or {}
                            if usage.get("input_tokens") is not None:
                                in_tokens = int(usage.get("input_tokens") or in_tokens)
                            if usage.get("output_tokens") is not None:
                                out_tokens = int(usage.get("output_tokens") or out_tokens)
                            sr = (inner.get("delta") or {}).get("stop_reason")
                            if sr:
                                finish_reason = "stop" if sr == "end_turn" else sr

                    elif etype == "result":
                        if bool(evt.get("is_error")):
                            is_error = True
                            err = str(evt.get("result") or "CLI error")[:500]
                            yield _sse({
                                "id": cid,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_label,
                                "choices": [
                                    {"index": 0, "delta": {"content": f"[error] {err}"}, "finish_reason": None}
                                ],
                            })
                            finish_reason = "stop"
                        else:
                            usage = evt.get("usage") or {}
                            if usage.get("input_tokens") is not None:
                                in_tokens = int(usage.get("input_tokens") or in_tokens)
                            if usage.get("output_tokens") is not None:
                                out_tokens = int(usage.get("output_tokens") or out_tokens)
                            if not emitted_any_text:
                                full = evt.get("result") or ""
                                if full:
                                    yield _sse({
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model_label,
                                        "choices": [
                                            {"index": 0, "delta": {"content": full}, "finish_reason": None}
                                        ],
                                    })
                            sr = evt.get("stop_reason")
                            if sr and not finish_reason:
                                finish_reason = "stop" if sr == "end_turn" else sr
            finally:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
        finally:
            self._semaphore.release()
            _cleanup_temp_files(temp_files)

        if is_error:
            self._circuit_breaker.record_failure()
        else:
            self._circuit_breaker.record_success()

        elapsed = time.monotonic() - t0
        logger.info(
            f"[{req_id}] stream done {elapsed:.1f}s in={in_tokens} out={out_tokens} error={is_error}"
        )

        yield _sse({
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_label,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": finish_reason or "stop"}
            ],
            "usage": {
                "prompt_tokens": in_tokens,
                "completion_tokens": out_tokens,
                "total_tokens": in_tokens + out_tokens,
            },
        })
        yield "data: [DONE]\n\n"

    async def health_check(self) -> bool:
        return bool(
            self._bin
            and (shutil.which(self._bin) or os.path.exists(self._bin))
        )
