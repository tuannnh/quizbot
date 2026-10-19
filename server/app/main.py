"""QuizBot server: receives screenshots, solves them with Claude, serves a live dashboard."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

from .db import Store
from .solver import KAHOOT_SHAPES, make_solver

log = logging.getLogger("quizbot")
logging.basicConfig(level=os.getenv("QUIZBOT_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = Path(os.getenv("QUIZBOT_DATA_DIR", "data"))
CAPTURE_DIR = DATA_DIR / "captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
TOKEN = os.getenv("QUIZBOT_TOKEN", "")
MAX_EDGE = int(os.getenv("QUIZBOT_MAX_EDGE", "1568"))  # Claude's sweet spot for vision input
# Models an agent may request per capture (for side-by-side speed comparison).
ALLOWED_MODELS = {m.strip() for m in os.getenv(
    "QUIZBOT_ALLOWED_MODELS", "claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5").split(",") if m.strip()}
STATIC = Path(__file__).parent / "static"
try:
    LOCAL_TZ = ZoneInfo(os.getenv("TZ") or "Asia/Ho_Chi_Minh")
except Exception:  # unknown TZ string or no tzdata in the image
    LOCAL_TZ = None

app = FastAPI(title="QuizBot")
store = Store(DATA_DIR / "quizbot.db")
solver = make_solver()


# ---------------------------------------------------------------- live updates (SSE)
class Hub:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def publish(self, record: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(record)


hub = Hub()


# ---------------------------------------------------------------- helpers
def _check_token(token: str | None) -> None:
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing token")


def prepare_image(raw: bytes, max_edge: int = MAX_EDGE) -> tuple[bytes, str]:
    """Downscale to max_edge and re-encode as JPEG: smaller upload to Claude, faster answers."""
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), f"{img.width}x{img.height}"


def _today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _resolve(session: str | None, model: str | None) -> tuple[str, str | None]:
    """Precedence: value sent by the agent > dashboard setting > default (today's date / server model)."""
    settings = store.get_settings()
    session = (session or "").strip() or settings.get("session") or _today()
    model = (model or "").strip() or settings.get("model") or None
    return session, model


def _ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def _display_line(r: dict) -> str:
    """One-line answer for agents and the iPhone Shortcut notification."""
    if r["status"] == "no_question":
        return "No question found on screen"
    if r["status"] == "error":
        return f"Error: {r.get('error')}"
    keys_list = r.get("answer_keys") or []
    keys = ", ".join(keys_list)
    conf = f"{round((r.get('confidence') or 0) * 100)}%"
    shapes = [k for k in keys_list if k in KAHOOT_SHAPES]
    if shapes:
        tiles = " + ".join(f"{KAHOOT_SHAPES[s].upper()} {s}" for s in shapes)
        return f"{tiles} — {r['answer']} ({conf})"
    if keys:
        return f"{keys} — {r['answer']} ({conf})"
    return f"{r['answer']} ({conf})"


# ---------------------------------------------------------------- API
@app.post("/api/capture")
async def capture(
    image: UploadFile = File(...),
    machine: str = Form("unknown"),
    session: str | None = Form(default=None),  # empty = the session set on the dashboard
    source: str = Form("desktop"),
    x_quizbot_token: str | None = Header(default=None),
    token: str | None = Form(default=None),  # iPhone Shortcuts find form fields easier than headers
    model: str | None = Form(default=None),    # empty = the model set on the dashboard
    capture_ms: int | None = Form(default=None),  # measured by the agent before upload
):
    started = time.perf_counter()
    _check_token(x_quizbot_token or token)
    session, model = _resolve(session, model)
    if model and model not in ALLOWED_MODELS:
        raise HTTPException(400, f"model not allowed; choose one of {sorted(ALLOWED_MODELS)}")
    raw = await image.read()
    if not raw:
        raise HTTPException(400, "empty image")
    timings: dict = {"receive_ms": _ms(started)}  # multipart parse + read of the upload
    if capture_ms is not None:
        timings["capture_ms"] = capture_ms

    row_id = store.create_pending(session=session, machine=machine.strip() or "unknown", source=source)
    hub.publish(store.get(row_id))

    try:
        t = time.perf_counter()
        jpeg, px = await run_in_threadpool(prepare_image, raw)
        timings["prepare_ms"] = _ms(t)
        path = CAPTURE_DIR / f"{row_id}.jpg"
        path.write_bytes(jpeg)
        store.update(row_id, image_path=str(path), image_px=px)

        t = time.perf_counter()
        result = await run_in_threadpool(solver.solve, jpeg, "image/jpeg", model)
        timings["model_ms"] = result.get("model_ms") or _ms(t)
        timings["server_ms"] = _ms(started)
        latency = timings["server_ms"]
        store.update(
            row_id,
            status="done" if result.get("found_question") else "no_question",
            question_type=result.get("question_type"),
            question=result.get("question"),
            options=result.get("options", []),
            answer_keys=result.get("answer_keys", []),
            answer=result.get("answer"),
            shape=result.get("shape"),
            color=result.get("color"),
            confidence=result.get("confidence"),
            reasoning=result.get("reasoning"),
            model=result.get("model"),
            latency_ms=latency,
            timings=timings,
            input_tokens=result.get("input_tokens"),
            output_tokens=result.get("output_tokens"),
        )
        log.info("answered #%s in %sms (model %s: %sms) %s/%s",
                 row_id, latency, result.get("model"), timings["model_ms"], session, machine)
    except Exception as exc:  # keep the record so the dashboard shows what failed
        log.exception("solve failed for #%s", row_id)
        timings["server_ms"] = _ms(started)
        store.update(row_id, status="error", error=str(exc)[:500], latency_ms=timings["server_ms"], timings=timings)

    record = store.get(row_id)
    hub.publish(record)
    return {**record, "display": _display_line(record)}


@app.get("/api/answers")
def list_answers(session: str | None = None, machine: str | None = None, limit: int = 100):
    return store.list(session=session, machine=machine, limit=min(limit, 500))


@app.get("/api/answers/{row_id}/image")
def answer_image(row_id: int):
    path = store.image_path(row_id)
    if not path or not Path(path).exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/answers/{row_id}/client-timing")
async def client_timing(row_id: int, request: Request, x_quizbot_token: str | None = Header(default=None)):
    """Agent reports its full round trip after receiving the answer; network = roundtrip - server."""
    _check_token(x_quizbot_token)
    body = await request.json()
    roundtrip = int(body.get("roundtrip_ms", 0))
    current = store.get(row_id)
    if not current:
        raise HTTPException(404)
    timings = current.get("timings") or {}
    extra = {"roundtrip_ms": roundtrip, "network_ms": max(0, roundtrip - timings.get("server_ms", 0))}
    if "capture_ms" in body:
        extra["capture_ms"] = int(body["capture_ms"])
    extra["end_to_end_ms"] = extra.get("capture_ms", timings.get("capture_ms", 0)) + roundtrip
    record = store.merge_timings(row_id, extra)
    hub.publish(record)
    return record


@app.get("/api/stats")
def stats(session: str | None = None):
    """p50/p90 per model for each timing stage, to compare models (and later Jev)."""
    groups: dict[str, list[dict]] = {}
    for r in store.timing_rows(session):
        groups.setdefault(r["model"] or "unknown", []).append(r)
    out = []
    for model, rows in groups.items():
        entry = {"model": model, "n": len(rows)}
        for stage in ("capture_ms", "network_ms", "prepare_ms", "model_ms", "server_ms", "end_to_end_ms"):
            vals = [r["timings"][stage] for r in rows if r["timings"].get(stage) is not None]
            entry[stage] = {"p50": _pct(vals, 0.5), "p90": _pct(vals, 0.9), "n": len(vals)}
        toks = [r["input_tokens"] for r in rows if r["input_tokens"]]
        entry["input_tokens_p50"] = _pct(toks, 0.5)
        out.append(entry)
    return sorted(out, key=lambda e: e["model_ms"]["p50"] or 0)


@app.get("/api/models")
def models():
    return {"default": getattr(solver, "model", "fake"), "allowed": sorted(ALLOWED_MODELS)}


# ---------------------------------------------------------------- settings + history
SETTING_KEYS = ("session", "model")


def _settings_view() -> dict:
    s = store.get_settings()
    return {
        "session": s.get("session", ""),
        "model": s.get("model", ""),
        "effective_session": s.get("session") or _today(),
        "effective_model": s.get("model") or getattr(solver, "model", "fake"),
        "allowed_models": sorted(ALLOWED_MODELS),
        "auth_required": bool(TOKEN),
    }


@app.get("/api/settings")
def get_settings():
    return _settings_view()


@app.put("/api/settings")
async def put_settings(request: Request, x_quizbot_token: str | None = Header(default=None)):
    """Dashboard-controlled defaults for agents that don't set QUIZBOT_SESSION / QUIZBOT_MODEL."""
    _check_token(x_quizbot_token)
    body = await request.json()
    values = {k: (str(body[k]).strip() if body[k] is not None else "") for k in SETTING_KEYS if k in body}
    if values.get("model") and values["model"] not in ALLOWED_MODELS:
        raise HTTPException(400, f"model not allowed; choose one of {sorted(ALLOWED_MODELS)}")
    if len(values.get("session", "")) > 80:
        raise HTTPException(400, "session name too long (max 80)")
    store.put_settings(values)
    view = _settings_view()
    hub.publish({"type": "settings", **view})
    return view


@app.delete("/api/answers")
def clear_answers(session: str | None = None, x_quizbot_token: str | None = Header(default=None)):
    """Delete history and stored captures: one session, or everything when no session is given."""
    _check_token(x_quizbot_token)
    paths = store.delete_answers(session or None)
    removed = 0
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
            removed += 1
        except OSError:
            log.warning("could not delete %s", p)
    log.info("cleared %d answers (%s), %d images removed", len(paths), session or "all sessions", removed)
    hub.publish({"type": "cleared", "session": session or None})
    return {"deleted": len(paths), "session": session or None}


@app.get("/api/facets")
def facets():
    return store.facets()


@app.get("/api/events")
async def events(request: Request):
    queue: asyncio.Queue = asyncio.Queue()
    hub.subscribers.add(queue)

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(record, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "solver": type(solver).__name__}


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")
