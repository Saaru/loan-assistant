"""HTTP API and web app for the customer assistant."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from finlib.dates import business_date, fmt

from .platform.actions import ActionError, ActionManager, AuditLog
from .platform.connectors import BackendError, Connectors, NotFound, session_cache
from .platform.orchestrator import Orchestrator
from .platform.policy import PolicyEngine
from .platform.session import Session, SessionStore
from .platform.telemetry import MetricsStore
from .platform.usecase import CONTEXT_PROVIDERS, PANEL_PROVIDERS, load_usecases, unique
from .platform.voice import SpeechUnavailable, VoiceService, is_echo, is_noise, speakable
from .settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB = Path(__file__).parent / "web"


class State:
    connectors: Connectors
    policy: PolicyEngine
    actions: ActionManager
    metrics: MetricsStore
    sessions: SessionStore
    usecases: dict
    orchestrator: Orchestrator
    voice: VoiceService
    running: dict  # session_id -> the task running that conversation's current turn


state = State()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.connectors = Connectors(settings.mock_base_url, settings.systems_config)
    state.policy = PolicyEngine(state.connectors)
    state.actions = ActionManager(AuditLog(settings.data_dir / "audit.jsonl"), settings.demo_mode)
    state.metrics = MetricsStore(settings.data_dir / "telemetry.jsonl")
    state.sessions = SessionStore(settings.session_ttl_s)
    state.usecases = load_usecases(settings.config_dir / "usecases")
    state.orchestrator = Orchestrator(settings, state.connectors, state.policy, state.actions, state.metrics,
                                      state.usecases, business_date)
    state.voice = VoiceService()
    state.running = {}
    yield
    await state.connectors.close()


app = FastAPI(title=f"{settings.brand_name} assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


def current_session(authorization: str | None = Header(None)) -> Session:
    token = (authorization or "").removeprefix("Bearer ").strip()
    session = state.sessions.get(token) if token else None
    if session is None:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    return session


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "sessions": len(state.sessions), "systems": state.connectors.health()}


@app.get("/api/config")
def config():
    llm = state.orchestrator.llm
    return {"brand": settings.brand_name, "assistant": settings.assistant_name,
            "model": f"{llm.model} via {llm.name}" if llm else None,
            "llm_configured": llm is not None, "demo_mode": settings.demo_mode,
            "voice": {"stt": state.voice.stt_available, "tts": "server" if state.voice.tts_available else "browser",
                      "tts_engine": state.voice.tts_engine, "tts_note": state.voice.tts_disabled_reason},
            "business_date": fmt(business_date()),
            "usecases": [uc.public() for uc in state.usecases.values()]}


@app.get("/api/customers")
async def demo_customers():
    """Demo sign-in list. In production the customer arrives with an app/IdP token instead."""
    try:
        return await state.connectors["crm"].get("/crm/customers", {"demo": "true"}, cache=False)
    except BackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class SessionRequest(BaseModel):
    customer_id: str
    usecase: str = "loan_servicing"


@app.post("/api/session")
async def create_session(body: SessionRequest):
    if body.usecase not in state.usecases:
        raise HTTPException(status_code=400, detail=f"Unknown workspace {body.usecase}")
    try:
        customer = await state.connectors["crm"].get(f"/crm/customers/{body.customer_id}", cache=False)
    except NotFound:
        raise HTTPException(status_code=404, detail="Customer not found")
    except BackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    session = state.sessions.create(customer["customer_id"], customer["full_name"], list(state.usecases), body.usecase)
    ctx = state.orchestrator.context(session)
    token = session_cache.set(session.cache)
    try:
        providers = unique(c for uc in session.usecases for c in state.usecases[uc].context)
        parts = await asyncio.gather(*(CONTEXT_PROVIDERS[p](ctx) for p in providers), return_exceptions=True)
        session.context_text = "\n".join(p for p in parts if isinstance(p, str))
    finally:
        session_cache.reset(token)
    return {"token": session.session_id, "customer": {"customer_id": customer["customer_id"],
                                                       "name": customer["full_name"]},
            "usecase": state.usecases[session.active].public()}


class WorkspaceRequest(BaseModel):
    usecase: str


@app.post("/api/session/workspace")
async def switch_workspace(body: WorkspaceRequest, session: Session = Depends(current_session)):
    """Change the active workspace (tab). The conversation, its history and pending actions carry over."""
    if body.usecase not in session.usecases:
        raise HTTPException(status_code=400, detail=f"Unknown workspace {body.usecase}")
    session.active = body.usecase
    return {"usecase": state.usecases[session.active].public()}


@app.get("/api/account")
async def account(session: Session = Depends(current_session)):
    panels = state.usecases[session.active].panels
    ctx = state.orchestrator.context(session)
    token = session_cache.set(session.cache)
    try:
        results = await asyncio.gather(*(PANEL_PROVIDERS[p](ctx) for p in panels), return_exceptions=True)
    finally:
        session_cache.reset(token)
    return {"as_of": fmt(business_date()), "panels": {
        name: ({"error": str(r)} if isinstance(r, Exception) else r) for name, r in zip(panels, results)}}


class ChatRequest(BaseModel):
    message: str | None = Field(None, max_length=4000)
    event: dict | None = None


def event_message(session: Session, event: dict) -> str:
    pending = session.actions.get(event.get("action_id", ""))
    if pending is None:
        raise HTTPException(status_code=400, detail="Unknown action")
    verb = {"action_completed": "confirmed", "action_cancelled": "cancelled"}.get(event.get("type"), "updated")
    return (f"[App event, not typed by the customer] The customer {verb} {pending.action_id} ({pending.title}) "
            f"in the app. Status: {pending.status}. "
            f"Result: {json.dumps(pending.result, default=str)}. Confirm in two or three sentences what happened and "
            f"what happens next, using only facts in this result. Do not calculate or state any figure that is not in it.")


def run_streaming(session: Session, work) -> StreamingResponse:
    """Run one conversation turn as a background task and stream its events as Server-Sent Events.

    The task is registered per session so a voice barge-in can cancel it. It keeps running if the browser
    disconnects, so the conversation history always stays consistent."""
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict):
        await queue.put(event)

    async def run():
        try:
            await work(emit)
        except asyncio.CancelledError:
            await queue.put({"type": "cancelled"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    state.running[session.session_id] = task
    task.add_done_callback(lambda t: state.running.get(session.session_id) is t
                           and state.running.pop(session.session_id, None))

    async def stream():
        while (event := await queue.get()) is not None:
            yield f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def cancel_running(session: Session, task: asyncio.Task | None = None) -> bool:
    task = task or state.running.get(session.session_id)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass
    return True


@app.post("/api/chat")
async def chat(body: ChatRequest, session: Session = Depends(current_session)):
    if body.event:
        content = event_message(session, body.event)
    elif body.message and body.message.strip():
        content = body.message.strip()
    else:
        raise HTTPException(status_code=400, detail="Empty message")
    await cancel_running(session)
    return run_streaming(session, lambda emit: state.orchestrator.run_turn(session, content, emit))


@app.post("/api/turn/cancel")
async def cancel_turn(session: Session = Depends(current_session)):
    return {"cancelled": await cancel_running(session)}


@app.post("/api/voice/turn")
async def voice_turn(request: Request, session: Session = Depends(current_session),
                     interrupted: bool = False, x_heard_text: str | None = Header(None)):
    """One spoken turn: audio in (WAV), the same event stream as chat out, preceded by the transcript."""
    audio = await request.body()
    if len(audio) < 2000:
        raise HTTPException(status_code=400, detail="No audio")
    if not state.voice.stt_available:
        raise HTTPException(status_code=503, detail="Speech-to-text is not configured (set GROQ_API_KEY).")
    heard = unquote(x_heard_text or "").strip()[:600]
    previous = state.running.get(session.session_id)  # the turn this utterance may be interrupting

    async def work(emit):
        await emit({"type": "stt_start"})
        started = time.perf_counter()
        try:
            transcript, no_speech, logprob = await state.voice.transcribe(audio)
        except Exception as exc:
            await emit({"type": "error", "message": f"I couldn't process the audio: {exc}"})
            return
        stt_ms = round((time.perf_counter() - started) * 1000, 1)
        # Only a real new request may interrupt: not silence, noise, or the assistant's own voice.
        if is_noise(transcript, no_speech, logprob) or (interrupted and is_echo(transcript, heard)):
            if previous is not None and not previous.done():
                state.running[session.session_id] = previous  # the original turn carries on, still cancellable
            await emit({"type": "no_speech", "transcript": transcript, "stt_ms": stt_ms})
            return
        await emit({"type": "transcript", "text": transcript, "stt_ms": stt_ms})
        await cancel_running(session, previous)  # barge-in confirmed
        content = transcript
        if interrupted:
            note = (f'The customer interrupted your previous reply after hearing: "{heard}".' if heard
                    else "The customer interrupted before your previous reply was spoken.")
            content = f"[Spoken. {note}] {transcript}"
        await state.orchestrator.run_turn(session, content, emit, channel="voice", extra={"stt_ms": stt_ms})

    return run_streaming(session, work)


class SpeakRequest(BaseModel):
    text: str = Field(..., max_length=2000)


@app.post("/api/voice/tts")
async def voice_tts(body: SpeakRequest, session: Session = Depends(current_session)):
    """Speech for one sentence. If server speech is unavailable the browser speaks the same normalised text."""
    text = speakable(body.text)
    if not text:
        return Response(status_code=204)
    try:
        audio = await state.voice.synthesize(text)
    except SpeechUnavailable as exc:
        return JSONResponse({"speak": text, "reason": str(exc)}, status_code=503)
    return Response(audio, media_type="audio/wav", headers={"X-Spoken-Text": text.encode("ascii", "ignore").decode()})


class VoiceMetrics(BaseModel):
    turn_id: str
    first_audio_ms: float | None = None
    first_answer_audio_ms: float | None = None


@app.post("/api/voice/metrics")
async def voice_metrics(body: VoiceMetrics, session: Session = Depends(current_session)):
    values = {k: v for k, v in body.model_dump().items() if k != "turn_id" and v is not None}
    return {"recorded": state.metrics.annotate(body.turn_id, values)}


class ConfirmRequest(BaseModel):
    otp: str | None = None


async def _action_call(session: Session, action_id: str, fn):
    ctx = state.orchestrator.context(session)
    token = session_cache.set(session.cache)
    try:
        await fn(ctx)
        ok, error = True, None
    except ActionError as exc:
        ok, error = False, str(exc)
    finally:
        session_cache.reset(token)
    pending = session.actions.get(action_id)
    return {"ok": ok, "error": error, "action": pending.public() if pending else None}


@app.post("/api/actions/{action_id}/confirm")
async def confirm_action(action_id: str, body: ConfirmRequest, session: Session = Depends(current_session)):
    return await _action_call(session, action_id,
                              lambda ctx: state.actions.confirm(ctx, action_id, body.otp, channel="app"))


@app.post("/api/actions/{action_id}/cancel")
async def cancel_action(action_id: str, session: Session = Depends(current_session)):
    return await _action_call(session, action_id, lambda ctx: state.actions.cancel(ctx, action_id, channel="app"))


@app.get("/api/metrics")
def metrics():
    return state.metrics.summary()


@app.get("/api/traces")
def traces(limit: int = 20):
    return state.metrics.recent(limit)


@app.get("/api/ops")
async def ops():
    try:
        response = await state.connectors.http.get(f"{settings.mock_base_url}/_admin/meta", timeout=2)
        systems = response.json()
    except Exception as exc:
        systems = {"error": f"Core systems unreachable: {exc}"}
    return {"systems": systems, "breakers": state.connectors.health(), "metrics": state.metrics.summary()}


class ChaosRequest(BaseModel):
    system: str
    mode: str


@app.post("/api/ops/chaos")
async def chaos(body: ChaosRequest):
    response = await state.connectors.http.post(f"{settings.mock_base_url}/_admin/chaos", json=body.model_dump(),
                                                timeout=2)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail"))
    return response.json()


@app.post("/api/ops/reset")
async def reset():
    response = await state.connectors.http.post(f"{settings.mock_base_url}/_admin/reseed", timeout=30)
    state.sessions.clear()
    state.policy.invalidate()
    state.metrics.turns.clear()
    return response.json()
