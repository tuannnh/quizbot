"""API tests using the offline fake solver (no API key, no network)."""
import io
import os
import tempfile

os.environ["QUIZBOT_SOLVER"] = "fake"
os.environ["QUIZBOT_TOKEN"] = "test-token"
os.environ["QUIZBOT_DATA_DIR"] = tempfile.mkdtemp(prefix="quizbot-test-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402

AUTH = {"X-QuizBot-Token": "test-token"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _png(w=2400, h=1600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def _capture(client, **data):
    return client.post("/api/capture", files={"image": ("c.png", _png(), "image/png")},
                       data={"machine": "ci", "session": "test", **data}, headers=AUTH)


def test_health(client):
    assert client.get("/healthz").json()["ok"] is True


def test_rejects_missing_token(client):
    r = client.post("/api/capture", files={"image": ("c.png", _png(), "image/png")})
    assert r.status_code == 401


def test_token_as_form_field(client):
    r = client.post("/api/capture", files={"image": ("c.png", _png(), "image/png")},
                    data={"token": "test-token", "source": "iphone"})
    assert r.status_code == 200


def test_capture_returns_structured_answer(client):
    r = _capture(client, capture_ms="90")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["question_type"] == "kahoot"
    assert body["shape"] in {"triangle", "diamond", "circle", "square"}
    assert body["display"]
    assert {"capture_ms", "prepare_ms", "model_ms", "server_ms"} <= body["timings"].keys()


def test_image_is_downscaled(client):
    row_id = _capture(client).json()["id"]
    img = Image.open(io.BytesIO(client.get(f"/api/answers/{row_id}/image").content))
    assert max(img.size) <= 1568


def test_model_allowlist(client):
    assert _capture(client, model="not-a-model").status_code == 400
    assert _capture(client, model="claude-haiku-5-5").json()["model"] == "claude-haiku-5-5"


def test_client_timing_and_stats(client):
    row_id = _capture(client, capture_ms="100", model="claude-sonnet-5-5").json()["id"]
    rec = client.post(f"/api/answers/{row_id}/client-timing", json={"roundtrip_ms": 2000},
                      headers=AUTH).json()
    assert rec["timings"]["end_to_end_ms"] == 2100
    assert rec["timings"]["network_ms"] >= 0
    models = {s["model"] for s in client.get("/api/stats?session=test").json()}
    assert "claude-sonnet-5-5" in models


def test_workspace_header(monkeypatch):
    from app.solver import ClaudeSolver

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    assert ClaudeSolver().client.default_headers["anthropic-workspace-id"] == "wrkspc_123"
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    assert "anthropic-workspace-id" not in ClaudeSolver().client.default_headers


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "QuizBot" in r.text
