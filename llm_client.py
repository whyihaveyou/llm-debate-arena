import json
import httpx
from typing import AsyncGenerator

THINKING_TOKEN = "\x00T\x00"  # sentinel marker for thinking chunks


class TokenUsage:
    __slots__ = ("prompt_tokens", "completion_tokens", "thinking_tokens")
    def __init__(self, prompt=0, completion=0, thinking=0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.thinking_tokens = thinking
    def total(self):
        return self.prompt_tokens + self.completion_tokens + self.thinking_tokens
    def to_dict(self):
        return {"prompt": self.prompt_tokens, "completion": self.completion_tokens, "thinking": self.thinking_tokens, "total": self.total()}


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, auth_type: str = "bearer"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.auth_type = auth_type

    def _auth_headers(self) -> dict:
        if self.auth_type == "api-key":
            return {"api-key": self.api_key, "Content-Type": "application/json"}
        if self.auth_type == "anthropic":
            return {
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        if self.auth_type != "anthropic":
            return messages
        out = []
        for m in messages:
            if m["role"] == "system":
                out.append({"role": "user", "content": f"[System]\n{m['content']}"})
            else:
                out.append(m)
        return out

    def _build_request_body(self, messages: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages, "stream": True, "temperature": 0.7}
        if self.auth_type == "anthropic":
            body["max_tokens"] = 16384
            body["thinking"] = {"type": "enabled", "budget_tokens": 10000}
        else:
            body["thinking"] = {"type": "enabled", "budget_tokens": 10000}
        return body

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        if self.auth_type == "anthropic":
            async for chunk in self._stream_anthropic(messages):
                yield chunk
        else:
            async for chunk in self._stream_openai(messages):
                yield chunk

    async def _stream_openai(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._auth_headers(),
                json=self._build_request_body(messages),
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise Exception(f"LLM API error {response.status_code}: {error_body.decode()}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        # Thinking content (MiMo uses reasoning_content)
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            yield THINKING_TOKEN + reasoning
                        # Regular content
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass

    async def _stream_anthropic(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        converted = self._convert_messages(messages)
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
                headers=self._auth_headers(),
                json=self._build_request_body(converted),
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise Exception(f"LLM API error {response.status_code}: {error_body.decode()}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    try:
                        chunk = json.loads(data)
                        if chunk.get("type") == "content_block_delta":
                            delta = chunk.get("delta", {})
                            # Thinking content (Kimi uses thinking_delta)
                            thinking = delta.get("thinking")
                            if thinking:
                                yield THINKING_TOKEN + thinking
                            # Regular text content
                            text = delta.get("text", "")
                            if text:
                                yield text
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass

    async def chat(self, messages: list[dict]) -> str:
        parts = []
        async for chunk in self.chat_stream(messages):
            if not chunk.startswith(THINKING_TOKEN):
                parts.append(chunk)
        return "".join(parts)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return len(text) // 3

    @staticmethod
    def estimate_messages_tokens(messages: list[dict]) -> int:
        total = 0
        for m in messages:
            total += 4
            total += LLMClient.estimate_tokens(m.get("content", ""))
        return total


def is_thinking_token(text: str) -> bool:
    return text.startswith(THINKING_TOKEN)


def strip_thinking_prefix(text: str) -> str:
    if text.startswith(THINKING_TOKEN):
        return text[len(THINKING_TOKEN):]
    return text
