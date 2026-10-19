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


# ---------------------------------------------------------------- settings, clearing, Kahoot output
def test_settings_drive_session_and_model(client):
    assert client.put("/api/settings", json={"session": "bio-quiz"}).status_code == 401
    r = client.put("/api/settings", json={"session": "bio-quiz", "model": "claude-haiku-5-5"}, headers=AUTH)
    assert r.json()["effective_session"] == "bio-quiz"
    body = _capture_no_session(client)
    assert body["session"] == "bio-quiz" and body["model"] == "claude-haiku-5-5"
    # A value sent by the agent still wins over the dashboard setting.
    assert _capture(client).json()["session"] == "test"
    assert client.put("/api/settings", json={"model": "gpt-x"}, headers=AUTH).status_code == 400
    r = client.put("/api/settings", json={"session": "", "model": ""}, headers=AUTH).json()
    assert r["session"] == "" and r["model"] == ""
    assert _capture_no_session(client)["session"] == r["effective_session"]  # today's date


def _capture_no_session(client):
    r = client.post("/api/capture", files={"image": ("c.png", _png(), "image/png")},
                    data={"machine": "ci"}, headers=AUTH)
    assert r.status_code == 200
    return r.json()


def test_clear_history_one_session_then_all(client):
    keep = _capture(client, session="keep").json()["id"]
    gone = _capture(client, session="drop").json()["id"]
    assert client.delete("/api/answers?session=drop").status_code == 401
    assert client.delete("/api/answers?session=drop", headers=AUTH).json()["deleted"] == 1
    assert client.get(f"/api/answers/{gone}/image").status_code == 404
    assert client.get(f"/api/answers/{keep}/image").status_code == 200
    assert client.delete("/api/answers", headers=AUTH).json()["deleted"] >= 1
    assert client.get("/api/answers").json() == []


def test_kahoot_multi_select_is_normalised():
    from app.solver import FakeSolver
    os.environ["QUIZBOT_FAKE_MODE"] = "multi"
    try:
        r = FakeSolver().solve(b"x" * 1000, "image/jpeg")
    finally:
        del os.environ["QUIZBOT_FAKE_MODE"]
    assert r["answer_keys"] == ["triangle", "square"]
    assert r["shapes"] == ["triangle", "square"]
    texts = {o["key"]: o["text"] for o in r["options"]}
    assert texts == {"triangle": "5' poly-A tails", "diamond": "Hox genes", "circle": "exons", "square": "introns"}
    assert all(o["shape"] == o["key"] for o in r["options"])


def test_colour_keys_map_to_shapes():
    from app.solver import _normalise
    r = _normalise({"question_type": "multiple_choice", "options": [{"key": "Red", "text": "A"}, {"key": "Blue", "text": "B"}],
                    "answer_keys": ["blue"], "answer": "B"})
    assert r["answer_keys"] == ["diamond"] and r["question_type"] == "kahoot" and r["shape"] == "diamond"


def test_falls_back_when_model_rejects_forced_tool(monkeypatch):
    from types import SimpleNamespace as NS
    from app.solver import ClaudeSolver

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    solver = ClaudeSolver()
    calls = []

    class Rejected(Exception):
        status_code = 400

    def create(**kw):
        calls.append(kw["tool_choice"]["type"])
        if kw["tool_choice"]["type"] == "tool":
            raise Rejected('tool_choice: type "tool" and "any" are not supported for this model.')
        answer = {"found_question": True, "question_type": "true_false", "question": "lac operon is repressible?",
                  "options": [{"key": "diamond", "text": "True"}, {"key": "triangle", "text": "False"}],
                  "answer_keys": ["triangle"], "answer": "False", "confidence": 0.9}
        return NS(content=[NS(type="thinking"), NS(type="tool_use", name="submit_answer", input=answer)],
                  stop_reason="tool_use", usage=NS(input_tokens=1200, output_tokens=300))

    monkeypatch.setattr(solver.client.messages, "create", create)
    r = solver.solve(b"img", "image/jpeg", "claude-opus-5-5")
    assert r["answer_keys"] == ["triangle"] and r["shape"] == "triangle" and r["question_type"] == "true_false"
    assert calls == ["tool", "auto"]
    solver.solve(b"img", "image/jpeg", "claude-opus-5-5")   # remembered: no second rejected call
    assert calls == ["tool", "auto", "auto"]


def test_text_json_answer_is_accepted():
    from app.solver import _answer_from_text
    assert _answer_from_text('Sure: {"answer": "False", "answer_keys": ["triangle"]} done')["answer"] == "False"
    assert _answer_from_text("no json here") is None


# ---------------------------------------------------------------- escalation (second opinion)
class _StubSolver:
    """Haiku answers 'triangle' with the given confidence; Sonnet answers per `sonnet_keys`."""

    def __init__(self, haiku_conf, sonnet_keys):
        self.haiku_conf, self.sonnet_keys, self.calls = haiku_conf, sonnet_keys, []
        self.model = "claude-haiku-5-5"

    def solve(self, image, media_type, model=None):
        model = model or self.model
        self.calls.append(model)
        keys = ["triangle"] if model == "claude-haiku-5-5" else self.sonnet_keys
        opts = [{"key": "triangle", "text": "False", "shape": "triangle"},
                {"key": "diamond", "text": "True", "shape": "diamond"}]
        return {"found_question": True, "question_type": "true_false", "question": "Q?", "options": opts,
                "answer_keys": keys, "answer": "False" if keys == ["triangle"] else "True",
                "shape": keys[0], "color": None, "confidence": self.haiku_conf if model == "claude-haiku-5-5" else 0.95,
                "reasoning": "", "model": model, "model_ms": 10, "input_tokens": 1, "output_tokens": 1}


def _with_stub(monkeypatch, client, haiku_conf, sonnet_keys):
    import app.main as m
    stub = _StubSolver(haiku_conf, sonnet_keys)
    monkeypatch.setattr(m, "solver", stub)
    client.put("/api/settings", json={"model": "", "session": "",
                                      "escalate_model": "claude-sonnet-5-5", "escalate_below": "0.8"}, headers=AUTH)
    return stub


def test_escalation_changes_answer_when_models_disagree(monkeypatch, client):
    stub = _with_stub(monkeypatch, client, haiku_conf=0.6, sonnet_keys=["diamond"])
    first = _capture(client, model="claude-haiku-5-5").json()
    assert first["answer_keys"] == ["triangle"]            # agent gets the fast answer
    assert first["escalation"]["status"] == "pending"
    rec = next(r for r in client.get("/api/answers").json() if r["id"] == first["id"])
    assert stub.calls == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert rec["answer_keys"] == ["diamond"] and rec["answer"] == "True"
    assert rec["model"] == "claude-haiku-5-5"   # stats/timings stay with the first call
    assert rec["escalation"]["status"] == "changed"
    assert rec["escalation"]["first"]["answer_keys"] == ["triangle"]
    assert rec["timings"]["escalate_ms"] == 10


def test_escalation_agrees_keeps_first_answer(monkeypatch, client):
    _with_stub(monkeypatch, client, haiku_conf=0.6, sonnet_keys=["triangle"])
    first = _capture(client, model="claude-haiku-5-5").json()
    rec = next(r for r in client.get("/api/answers").json() if r["id"] == first["id"])
    assert rec["escalation"]["status"] == "agreed" and rec["model"] == "claude-haiku-5-5"


def test_no_escalation_when_confident_or_off(monkeypatch, client):
    stub = _with_stub(monkeypatch, client, haiku_conf=0.9, sonnet_keys=["diamond"])
    assert _capture(client, model="claude-haiku-5-5").json()["escalation"] is None
    client.put("/api/settings", json={"escalate_model": ""}, headers=AUTH)
    stub.haiku_conf = 0.1
    assert _capture(client, model="claude-haiku-5-5").json()["escalation"] is None
    assert stub.calls == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert client.get("/api/settings").json()["escalate_model"] == ""
    assert client.put("/api/settings", json={"escalate_below": "2"}, headers=AUTH).status_code == 400


def test_schema_asks_for_reasoning_before_answer():
    from app.solver import ANSWER_TOOL
    order = list(ANSWER_TOOL["input_schema"]["properties"])
    assert order.index("reasoning") < order.index("answer_keys") < order.index("answer")
    assert "reasoning" in ANSWER_TOOL["input_schema"]["required"]


# ---------------------------------------------------------------- coding problems
def test_coding_answer_is_stored_and_fences_stripped(monkeypatch, client):
    import app.main as m
    from app.solver import FakeSolver
    monkeypatch.setattr(m, "solver", FakeSolver())
    monkeypatch.setenv("QUIZBOT_FAKE_MODE", "coding")
    body = _capture(client).json()
    assert body["question_type"] == "coding" and body["language"] == "Python 3"
    assert body["code"] == "def aVeryBigSum(ar):\n    return sum(ar)"
    assert body["complexity"].startswith("O(n)")
    assert body["display"].startswith("Python 3 solution ready")
    assert body["escalation"] is None


def test_fence_variants():
    from app.solver import _strip_fences
    assert _strip_fences("```cpp\nint f(){}\n```") == "int f(){}"
    assert _strip_fences("```\nx = 1\n```\n") == "x = 1"
    assert _strip_fences("def f():\n    return 1") == "def f():\n    return 1"


def test_code_fields_dropped_for_non_coding():
    from app.solver import _normalise
    r = _normalise({"question_type": "open", "answer": "42", "language": "Python 3", "complexity": "O(1)"})
    assert "language" not in r and "complexity" not in r


# ---------------------------------------------------------------- multi-screenshot coding problems
class _CodingStub:
    def __init__(self, same=True, qtype="coding"):
        self.same, self.qtype, self.contexts = same, qtype, []
        self.model = "claude-sonnet-5-5"

    def solve(self, image, media_type, model=None, context=None):
        self.contexts.append(len(context or []))
        r = {"found_question": True, "question_type": self.qtype, "question": "Two Sum", "reasoning": "",
             "answer": "hash map", "confidence": 0.9, "model": model or self.model, "model_ms": 5,
             "same_problem": bool(context) and self.same, "options": [], "answer_keys": []}
        if self.qtype == "coding":
            r.update(language="Python 3", code=f"def twoSum(): pass  # pages={len(context or []) + 1}",
                     complexity="O(n)")
        return r


def _coding_client(monkeypatch, client, **kw):
    import app.main as m
    stub = _CodingStub(**kw)
    monkeypatch.setattr(m, "solver", stub)
    client.put("/api/settings", json={"escalate_model": ""}, headers=AUTH)
    client.delete("/api/answers", headers=AUTH)
    return stub


def _cap(client, machine="ci"):
    return client.post("/api/capture", files={"image": ("c.png", _png(400, 300), "image/png")},
                       data={"machine": machine, "session": "code"}, headers=AUTH).json()


def test_coding_pages_are_combined(monkeypatch, client):
    stub = _coding_client(monkeypatch, client)
    p1, p2 = _cap(client), _cap(client)
    assert stub.contexts == [0, 1]
    assert p2["pages"] == [p1["id"], p2["id"]] and "pages=2" in p2["code"]
    assert "[from 2 screenshots]" in p2["display"]
    ids = [r["id"] for r in client.get("/api/answers?session=code").json()]
    assert ids == [p2["id"]]                     # the partial first answer is hidden
    assert client.get(f"/api/answers/{p1['id']}/image").status_code == 200  # its screenshot is kept
    p3 = _cap(client)
    assert stub.contexts == [0, 1, 2] and p3["pages"] == [p1["id"], p2["id"], p3["id"]]


def test_other_machine_or_new_problem_not_combined(monkeypatch, client):
    stub = _coding_client(monkeypatch, client, same=False)
    p1 = _cap(client)
    other = _cap(client, machine="other")
    assert other["pages"] == [other["id"]]
    p3 = _cap(client)                            # same machine, model says different question
    assert stub.contexts == [0, 0, 1] and p3["pages"] == [p3["id"]]
    assert len(client.get("/api/answers?session=code").json()) == 3


def test_page_limit_and_clear(monkeypatch, client):
    import app.main as m
    stub = _coding_client(monkeypatch, client)
    monkeypatch.setattr(m, "CODING_MAX_PAGES", 3)
    caps = [_cap(client) for _ in range(4)]
    assert stub.contexts == [0, 1, 2, 2]         # never more than max-1 earlier pages
    assert caps[-1]["pages"] == [caps[1]["id"], caps[2]["id"], caps[3]["id"]]
    assert client.delete("/api/answers?session=code", headers=AUTH).json()["deleted"] == 4


def test_non_coding_answer_does_not_open_a_problem(monkeypatch, client):
    stub = _coding_client(monkeypatch, client, qtype="open")
    _cap(client), _cap(client)
    assert stub.contexts == [0, 0]
