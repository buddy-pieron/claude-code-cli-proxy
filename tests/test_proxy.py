import base64
import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from claude_cli import (
    CircuitBreaker,
    _build_prompt,
    _flatten_content,
    _flatten_text,
    _split_messages,
)
from server import app


class TestFlattenContent:
    def test_string(self):
        text, files = _flatten_content("hello")
        assert text == "hello"
        assert files == []

    def test_none(self):
        text, files = _flatten_content(None)
        assert text == ""
        assert files == []

    def test_text_blocks(self):
        content = [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
        text, files = _flatten_content(content)
        assert "hello" in text
        assert "world" in text
        assert files == []

    def test_image_url_base64(self):
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        b64 = base64.b64encode(png_header).decode()
        content = [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        text, files = _flatten_content(content)
        assert "describe this" in text
        assert len(files) == 1
        assert files[0].endswith(".png")
        assert os.path.exists(files[0])
        os.unlink(files[0])

    def test_image_url_http(self):
        content = [
            {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg"}},
        ]
        text, files = _flatten_content(content)
        assert "https://example.com/img.jpg" in text
        assert files == []

    def test_anthropic_image_base64(self):
        b64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 50).decode()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        ]
        text, files = _flatten_content(content)
        assert len(files) == 1
        assert files[0].endswith(".jpg")
        os.unlink(files[0])

    def test_empty_list(self):
        text, files = _flatten_content([])
        assert text == ""
        assert files == []


class TestFlattenText:
    def test_delegates_to_flatten_content(self):
        assert _flatten_text("hello") == "hello"
        assert _flatten_text(None) == ""
        assert "world" in _flatten_text([{"type": "text", "text": "world"}])


class TestSplitMessages:
    def test_basic(self):
        msgs = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ]
        system, transcript, latest, files = _split_messages(msgs)
        assert system == "Be helpful."
        assert "Hi" in transcript
        assert "Hello!" in transcript
        assert latest == "How are you?"
        assert files == []

    def test_user_only(self):
        system, transcript, latest, files = _split_messages(
            [{"role": "user", "content": "just me"}]
        )
        assert system == ""
        assert transcript == ""
        assert latest == "just me"

    def test_empty(self):
        system, transcript, latest, files = _split_messages([])
        assert system == ""
        assert latest == ""

    def test_tool_messages(self):
        msgs = [
            {"role": "user", "content": "call the tool"},
            {"role": "tool", "content": "result data", "tool_call_id": "tc_1"},
            {"role": "user", "content": "now what?"},
        ]
        system, transcript, latest, files = _split_messages(msgs)
        assert "TOOL_RESULT[tc_1]: result data" in transcript
        assert latest == "now what?"


class TestBuildPrompt:
    def test_no_transcript(self):
        assert _build_prompt("", "hello") == "hello"

    def test_with_transcript(self):
        result = _build_prompt("USER: hi\nASSISTANT: hey", "bye")
        assert "Previous conversation transcript:" in result
        assert "Current user message:" in result
        assert "bye" in result


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1)
        assert cb.state == "closed"
        assert cb.can_execute()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.can_execute()
        cb.record_failure()
        assert cb.state == "open"
        assert not cb.can_execute()

    def test_success_resets(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        assert cb.stats["failure_count"] == 0

    def test_half_open_after_recovery(self):
        import time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.state == "half-open"
        assert cb.can_execute()

    def test_stats(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30)
        cb.record_failure()
        cb.record_failure()
        stats = cb.stats
        assert stats["failure_count"] == 2
        assert stats["failure_threshold"] == 5
        assert stats["state"] == "closed"
        assert stats["total_trips"] == 0

    def test_trips_counter(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb.stats["total_trips"] == 1
        import time
        time.sleep(0.02)
        cb.can_execute()
        cb.record_success()
        cb.record_failure()
        assert cb.stats["total_trips"] == 2


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_returns_status(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data
            assert data["provider"] == "claude-cli"


@pytest.mark.asyncio
class TestModelsEndpoint:
    async def test_lists_models(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/models")
            assert resp.status_code == 200
            data = resp.json()
            assert data["object"] == "list"
            model_ids = [m["id"] for m in data["data"]]
            assert "claude-sonnet-4-6" in model_ids
            assert "claude-opus-4-7" in model_ids
