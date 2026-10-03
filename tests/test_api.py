from fastapi.testclient import TestClient

from app.gemini_client import LLMError
from app.main import app


class FakeLLM:
    async def generate(self, message, history, fault=None):
        if fault == "429":
            raise LLMError("rate_limited", 429, "busy", retry_after=10)
        return f"echo: {message}"


app.state.llm = FakeLLM()
client = TestClient(app)
HEADERS = {"X-API-Key": "test-key"}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_chat_requires_key():
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_chat_rejects_wrong_key():
    r = client.post("/chat", json={"message": "hi"}, headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_chat_ok():
    r = client.post("/chat", json={"message": "hi"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["reply"] == "echo: hi"
    assert "x-request-id" in r.headers


def test_empty_message_rejected():
    assert client.post("/chat", json={"message": ""}, headers=HEADERS).status_code == 422


def test_rate_limit_maps_to_429_with_retry_after():
    r = client.post("/chat", json={"message": "hi"}, headers={**HEADERS, "X-Debug-Fault": "429"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "10"
    assert r.json()["error"] == "rate_limited"
