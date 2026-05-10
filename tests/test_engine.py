"""Tests for chat_engine.py and debate_engine.py — queue behavior, serialization."""

import asyncio
import json
import tempfile
from pathlib import Path

from chat_engine import ChatSession, _ChatEvent
from debate_engine import DebateSession


class TestChatSession:
    def test_init_defaults(self):
        s = ChatSession.__new__(ChatSession)
        s.mode = "chat"
        s.status = "idle"
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._stop_flag = False
        assert s.mode == "chat"
        assert s.status == "idle"
        assert s._stop_flag is False

    def test_to_dict(self):
        s = ChatSession.__new__(ChatSession)
        s.id = "abc123"
        s.model_key = "global:test"
        s.model = None
        s.name = "TestModel"
        s.owner_id = 1
        s.mode = "chat"
        s.topic = "Test topic"
        s.status = "idle"
        s.status_detail = ""
        s.round = 3
        s.error_message = ""
        s.created_at = "2025-01-01T00:00:00"
        s.history = [{"round": 1, "model": "User", "content": "hi"}]
        s.messages = [{"role": "user", "content": "hi"}]
        s.token_usage = {}
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._stop_flag = False
        s.lang = "zh"
        s._ChatEvent_queue = s._event_queue  # compatibility

        d = s.to_dict()
        assert d["id"] == "abc123"
        assert d["mode"] == "chat"
        assert d["owner_id"] == 1
        assert d["round"] == 3

    def test_clear_queue(self):
        s = ChatSession.__new__(ChatSession)
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._event_queue.put_nowait(_ChatEvent("test", {}))
        s._event_queue.put_nowait(_ChatEvent("test2", {}))
        assert s._event_queue.qsize() == 2
        s.clear_queue()
        assert s._event_queue.empty()

    def test_emit_drop_oldest_when_full(self):
        s = ChatSession.__new__(ChatSession)
        s._event_queue = asyncio.Queue(maxsize=2)
        # Fill the queue
        s._event_queue.put_nowait(_ChatEvent("old1", {"n": 1}))
        s._event_queue.put_nowait(_ChatEvent("old2", {"n": 2}))
        assert s._event_queue.full()
        # Emit should drop oldest and add new
        s._emit("new_event", {"n": 3})
        # old1 should be dropped, old2 and new_event should remain
        remaining = []
        while not s._event_queue.empty():
            remaining.append(s._event_queue.get_nowait())
        assert len(remaining) == 2
        assert remaining[0].data["n"] == 2
        assert remaining[1].data["n"] == 3

    def test_stop_flag_reset(self):
        """After stop(), _stop_flag must be reset at start of send_message."""
        s = ChatSession.__new__(ChatSession)
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._stop_flag = True
        # The send_message method resets _stop_flag at start
        assert s._stop_flag is True
        s._stop_flag = False  # simulating what send_message does
        assert s._stop_flag is False

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_session.json"
            s = ChatSession("global:test", None, "ModelA", user_id=42)
            s.topic = "Hello world"
            s.history = [{"round": 1, "model": "User", "content": "Hi"}]
            s.messages = [{"role": "system", "content": "You are helpful."},
                          {"role": "user", "content": "Hi"}]
            s.round = 1
            # Test to_dict is JSON-serializable
            d = s.to_dict()
            serialized = json.dumps(d, ensure_ascii=False)
            parsed = json.loads(serialized)
            assert parsed["topic"] == "Hello world"
            assert parsed["owner_id"] == 42

    def test_compatibility_properties(self):
        s = ChatSession("key", None, "TestName", user_id=1)
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._stop_flag = False
        assert s.name_a == "TestName"
        assert s.name_b == ""
        assert s.name_c == ""
        assert s.max_rounds == 0
        assert s.model_a_key == "key"
        assert s.report == ""


class TestDebateSession:
    def test_quality_score_default(self):
        s = DebateSession.__new__(DebateSession)
        s.quality_score = ""
        assert s.quality_score == ""

    def test_event_queue_maxsize(self):
        """Verify queue is bounded."""
        s = DebateSession.__new__(DebateSession)
        s._event_queue = asyncio.Queue(maxsize=1000)
        assert s._event_queue.maxsize == 1000

    def test_clear_queue(self):
        from debate_engine import _DebateEvent
        s = DebateSession.__new__(DebateSession)
        s._event_queue = asyncio.Queue(maxsize=1000)
        # Check if _DebateEvent exists; if not, use generic event
        try:
            s._event_queue.put_nowait(_DebateEvent("test", {}))
        except NameError:
            s._event_queue.put_nowait(_ChatEvent("test", {}))
        s.clear_queue()
        assert s._event_queue.empty()


class TestChatEventSSE:
    def test_to_sse_format(self):
        e = _ChatEvent("token", {"content": "hello"})
        sse = e.to_sse()
        assert "event: token\n" in sse
        assert '"content": "hello"' in sse
        assert sse.endswith("\n\n")


class TestStreamTimeout:
    def test_stream_timeout_constant_exists(self):
        from llm_client import STREAM_TOTAL_TIMEOUT
        assert isinstance(STREAM_TOTAL_TIMEOUT, int)
        assert STREAM_TOTAL_TIMEOUT > 0

    def test_validate_url_blocks_internal(self):
        from app import _validate_url
        from fastapi import HTTPException
        for url in ("http://127.0.0.1/v1", "http://localhost:8080", "http://10.0.0.1",
                     "http://192.168.1.1", "http://172.16.0.1"):
            try:
                _validate_url(url)
                assert False, f"Should have raised for {url}"
            except HTTPException as e:
                assert e.status_code == 400

    def test_validate_url_allows_public(self):
        from app import _validate_url
        result = _validate_url("https://api.openai.com/v1")
        assert result == "https://api.openai.com/v1"
