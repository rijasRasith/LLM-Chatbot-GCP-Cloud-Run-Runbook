import asyncio
import logging
import random
import time

import httpx
from google import genai
from google.genai import errors, types

from .config import Settings
from .logging_config import log_event

logger = logging.getLogger("chatbot.llm")

SYSTEM_PROMPT = (
    "You are a concise, helpful assistant for an internal IT help desk. "
    "Answer in under 150 words. If you do not know the answer, say so."
)

RETRYABLE_CODES = {429, 500, 502, 503, 504}

KIND_TO_HTTP = {
    "rate_limited": (429, "The model service is busy. Please retry shortly."),
    "timeout": (504, "The model service took too long to respond."),
    "upstream_auth": (502, "The chatbot backend is misconfigured. Please contact support."),
    "model_not_found": (502, "The chatbot backend is misconfigured. Please contact support."),
    "upstream_unavailable": (503, "The model service is temporarily unavailable."),
    "bad_request": (400, "The request was rejected by the model service."),
    "unknown": (500, "Unexpected error while generating a reply."),
}


class LLMError(Exception):
    def __init__(self, kind: str, http_status: int, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.kind = kind
        self.http_status = http_status
        self.detail = detail
        self.retry_after = retry_after


class SimulatedUpstreamError(Exception):
    def __init__(self, code: int):
        super().__init__(f"simulated upstream error {code}")
        self.code = code


def classify(exc: Exception) -> tuple[str, int | None, bool]:
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout", None, True
    code = getattr(exc, "code", None)
    if isinstance(exc, (errors.APIError, SimulatedUpstreamError)) and isinstance(code, int):
        if code == 429:
            return "rate_limited", code, True
        if code in (401, 403):
            return "upstream_auth", code, False
        if code == 404:
            return "model_not_found", code, False
        if code in RETRYABLE_CODES:
            return "upstream_unavailable", code, True
        if 400 <= code < 500:
            return "bad_request", code, False
    return "unknown", None, False


class GeminiService:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client or genai.Client(
            vertexai=True,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            http_options=types.HttpOptions(
                api_version="v1",
                timeout=int(settings.gemini_timeout_seconds * 1000),
            ),
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_llm_calls)

    async def _invoke(self, contents, config, fault: str | None, attempt: int):
        if self.settings.fault_injection_enabled and fault:
            if fault == "429" or (fault == "429_once" and attempt == 1):
                raise SimulatedUpstreamError(429)
            if fault == "500":
                raise SimulatedUpstreamError(500)
            if fault == "timeout":
                await asyncio.sleep(self.settings.gemini_timeout_seconds + 5)
        return await self.client.aio.models.generate_content(
            model=self.settings.gemini_model, contents=contents, config=config
        )

    async def generate(self, message: str, history: list, fault: str | None = None) -> str:
        s = self.settings
        contents = [
            types.Content(role=turn.role, parts=[types.Part.from_text(text=turn.text)])
            for turn in history
        ]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=s.max_output_tokens,
            temperature=0.3,
        )

        async with self._semaphore:
            for attempt in range(1, s.max_retries + 2):
                started = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        self._invoke(contents, config, fault, attempt),
                        timeout=s.gemini_timeout_seconds + s.hard_timeout_margin_seconds,
                    )
                    usage = getattr(response, "usage_metadata", None)
                    log_event(
                        logger, logging.INFO, "llm_call_ok",
                        attempt=attempt, model=s.gemini_model,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        prompt_tokens=getattr(usage, "prompt_token_count", None),
                        output_tokens=getattr(usage, "candidates_token_count", None),
                    )
                    return response.text or ""
                except Exception as exc:
                    kind, code, retryable = classify(exc)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    if retryable and attempt <= s.max_retries:
                        delay = min(8.0, s.backoff_base_seconds * 2 ** (attempt - 1))
                        delay += random.uniform(0, s.backoff_base_seconds / 2)
                        log_event(
                            logger, logging.WARNING, "llm_retry",
                            kind=kind, upstream_code=code, attempt=attempt,
                            next_delay_s=round(delay, 2), latency_ms=latency_ms,
                        )
                        await asyncio.sleep(delay)
                        continue

                    log_event(
                        logger, logging.ERROR, "llm_call_failed",
                        kind=kind, upstream_code=code, attempt=attempt,
                        latency_ms=latency_ms, model=s.gemini_model,
                        error=str(exc)[:500],
                    )
                    http_status, public_msg = KIND_TO_HTTP[kind]
                    retry_after = 10 if kind == "rate_limited" else None
                    raise LLMError(kind, http_status, public_msg, retry_after) from exc
        raise LLMError("unknown", 500, KIND_TO_HTTP["unknown"][1])
