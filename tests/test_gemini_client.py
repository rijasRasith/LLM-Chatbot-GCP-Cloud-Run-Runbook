import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.gemini_client import GeminiService, LLMError, SimulatedUpstreamError


def make_service(fake_generate, **overrides):
    settings = Settings(
        google_cloud_project="p", chatbot_api_key="k",
        max_retries=2, backoff_base_seconds=0.0,
        gemini_timeout_seconds=0.1, hard_timeout_margin_seconds=0.0,
        **overrides,
    )
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)))
    return GeminiService(settings, client=client)


def ok_response():
    return SimpleNamespace(text="hello", usage_metadata=None)


def test_retries_429_then_succeeds():
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise SimulatedUpstreamError(429)
        return ok_response()

    svc = make_service(fake)
    assert asyncio.run(svc.generate("hi", [])) == "hello"
    assert calls["n"] == 3


def test_429_exhausted_raises_rate_limited():
    async def fake(**kwargs):
        raise SimulatedUpstreamError(429)

    with pytest.raises(LLMError) as err:
        asyncio.run(make_service(fake).generate("hi", []))
    assert err.value.kind == "rate_limited" and err.value.http_status == 429


def test_403_is_not_retried_and_hidden_from_client():
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        raise SimulatedUpstreamError(403)

    with pytest.raises(LLMError) as err:
        asyncio.run(make_service(fake).generate("hi", []))
    assert calls["n"] == 1
    assert err.value.kind == "upstream_auth" and err.value.http_status == 502


def test_hang_becomes_504_timeout():
    async def fake(**kwargs):
        await asyncio.Event().wait()

    with pytest.raises(LLMError) as err:
        asyncio.run(make_service(fake).generate("hi", []))
    assert err.value.kind == "timeout" and err.value.http_status == 504
