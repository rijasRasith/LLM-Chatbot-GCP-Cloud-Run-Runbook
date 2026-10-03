import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import get_settings
from .gemini_client import GeminiService, LLMError
from .logging_config import configure_logging, log_event, request_id_var, trace_var

logger = logging.getLogger("chatbot.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.llm = GeminiService(settings)
    log_event(
        logger, logging.INFO, "startup_complete",
        model=settings.gemini_model, location=settings.google_cloud_location,
        fault_injection_enabled=settings.fault_injection_enabled,
    )
    yield


app = FastAPI(title="LLM Chatbot on Cloud Run", lifespan=lifespan)


class Turn(BaseModel):
    role: Literal["user", "model"]
    text: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    reply: str
    request_id: str
    model: str


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().chatbot_api_key
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        log_event(
            logger, logging.WARNING, "auth_failed",
            reason="missing_key" if not x_api_key else "invalid_key",
        )
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.middleware("http")
async def observe(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request_id_var.set(request_id)

    trace_header = request.headers.get("x-cloud-trace-context")
    if trace_header:
        trace_id = trace_header.split("/")[0]
        trace_var.set(f"projects/{get_settings().google_cloud_project}/traces/{trace_id}")

    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    if request.url.path != "/health":
        log_event(
            logger, logging.INFO, "request_completed",
            method=request.method, path=request.url.path,
            status=response.status_code,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    return response


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError):
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.kind, "detail": exc.detail, "request_id": request_id_var.get()},
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", exc_info=exc, extra={"fields": {"event": "unhandled_exception"}})
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": "Unexpected server error.",
                 "request_id": request_id_var.get()},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    if getattr(request.app.state, "llm", None) is None:
        raise HTTPException(status_code=503, detail="LLM client not initialised")
    return {"status": "ready", "model": get_settings().gemini_model}


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(
    body: ChatRequest,
    request: Request,
    x_debug_fault: str | None = Header(default=None),
):
    log_event(
        logger, logging.INFO, "chat_request",
        message_chars=len(body.message), history_turns=len(body.history),
    )
    reply = await request.app.state.llm.generate(body.message, body.history, fault=x_debug_fault)
    return ChatResponse(reply=reply, request_id=request_id_var.get() or "", model=get_settings().gemini_model)
