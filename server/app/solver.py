"""Question solver: sends a screenshot to Claude and gets back a structured answer.

The model is forced to call a single tool (`submit_answer`), so the output is always
machine-readable JSON regardless of question type.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time

log = logging.getLogger("quizbot")

# Kahoot's fixed answer tiles. Shape is the stable identifier; colour is for display.
KAHOOT_SHAPES = {
    "triangle": "red",
    "diamond": "blue",
    "circle": "yellow",
    "square": "green",
}
# Model sometimes names the tile by colour or with a word variant; map all of them to the shape.
SHAPE_ALIASES = {
    **{s: s for s in KAHOOT_SHAPES},
    **{c: s for s, c in KAHOOT_SHAPES.items()},
    "rhombus": "diamond", "red triangle": "triangle", "blue diamond": "diamond",
    "yellow circle": "circle", "green square": "square",
}

SYSTEM_PROMPT = """You are QuizBot. You receive a screenshot of a window that contains a quiz question
or a programming problem.

Your job:
1. Find the question that is currently being asked. Ignore chrome, ads, timers, scoreboards and unrelated text.
2. Classify it:
   - multiple_choice: lettered/numbered options (A/B/C/D, 1/2/3/4, radio buttons, checkboxes)
   - kahoot: coloured tiles with shapes (red triangle, blue diamond, yellow circle, green square)
   - true_false: exactly two options meaning true/false or yes/no
   - open: no options shown; a free-text answer is expected
   - coding: a programming problem (LeetCode, HackerRank, Codility, an online judge or a code editor
     with a problem statement). A problem statement alone, without an editor, is still coding.
   - other: anything else (ordering, matching, fill-in-the-blank, maths working ...)
3. Transcribe the question and every option exactly as shown.
4. Answer it. Think carefully but be fast: the user is under time pressure.
   - For choice questions, return the key(s) of the correct option(s). Use the key printed on screen
     (e.g. "B" or "2").
   - If several options are correct ("select all that apply"), return all of their keys.
   - For open questions, give a concise, correct answer the user could type directly.
   - For coding problems, see the coding rules below.
5. Give an honest confidence between 0 and 1.

Output rules (the app renders shapes and colours itself, so you only output plain text):
- Kahoot keys are exactly one lowercase word: triangle, diamond, circle or square. Use the same word
  for the option's key and its shape. Tile colour tells you the shape: red=triangle, blue=diamond,
  yellow=circle, green=square.
- Option text is the answer text only. Never put the key, shape name, colour, letter or any symbol
  or emoji into `text`, `answer` or `answer_keys` values beyond the bare key.
- Fill `reasoning` before choosing. `answer_keys` and `answer` must be exactly the option(s) your
  reasoning concludes are correct; re-check the key against the option text before submitting.
- `answer` is the text of the correct option; if several are correct, join their texts with " | "
  in on-screen order.

Coding rules (question_type = coding):
- `question`: the problem title, then a one-line summary of what to compute.
- `language`: the language selected in the editor's language picker (e.g. "Python 3", "Java 15",
  "C++20"); if none is visible, use "Python 3".
- `code`: a complete, correct, efficient solution that can be pasted into the editor as-is. Keep the
  exact function/class/method name, parameters and return type from the editor's stub. If the stub
  already contains I/O boilerplate (reading stdin, writing output, `if __name__ == '__main__'`), return
  only the function the user must complete, not the boilerplate. If the editor is empty, return a full
  program that reads input and prints output in the stated format. Respect the constraints (large
  numbers, time limits). Plain code only: no markdown fences, no explanations outside comments.
- `answer`: the approach in one sentence. `complexity`: time and space, e.g. "O(n) time, O(1) space".
- Leave `options` and `answer_keys` empty. If part of the statement is cut off, solve what is visible
  and lower your confidence.

If no question is visible, set found_question to false and leave the rest minimal."""

ANSWER_TOOL = {
    "name": "submit_answer",
    "description": "Submit the transcribed question and its answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found_question": {"type": "boolean"},
            "same_problem": {
                "type": "boolean",
                "description": "Only when earlier screenshots are provided: true if the newest screenshot "
                               "continues the same problem, false if it is a different question.",
            },
            "question_type": {
                "type": "string",
                "enum": ["multiple_choice", "kahoot", "true_false", "open", "coding", "other"],
            },
            "question": {"type": "string", "description": "Question text as shown."},
            "options": {
                "type": "array",
                "description": "All options shown, in on-screen order. Empty for open questions.",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "A/B/C/D, 1/2/3/4, or Kahoot shape name."},
                        "text": {"type": "string"},
                        "shape": {
                            "type": "string",
                            "enum": ["triangle", "diamond", "circle", "square", "none"],
                        },
                    },
                    "required": ["key", "text"],
                },
            },
            # Reasoning comes BEFORE the answer fields on purpose: the model fills tool inputs in
            # this order, so it works the question out first and then picks the key(s) it argued for.
            "reasoning": {
                "type": "string",
                "description": "Work it out first, in one to three short sentences: what the question asks, "
                               "which option(s) are correct and why. The answer fields below must match this.",
            },
            "answer_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key(s) of the correct option(s). Empty for open questions.",
            },
            "answer": {
                "type": "string",
                "description": "The answer as text (option text for choice questions, full answer for open ones).",
            },
            "language": {"type": "string", "description": "Coding only: language of `code`."},
            "code": {"type": "string", "description": "Coding only: the complete solution, plain code."},
            "complexity": {"type": "string", "description": "Coding only: time and space complexity."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["found_question", "question_type", "question", "options", "reasoning",
                     "answer_keys", "answer", "confidence"],
    },
}


def _shape_of(value: str | None) -> str | None:
    return SHAPE_ALIASES.get((value or "").strip().lower())


def _strip_fences(code: str) -> str:
    """Remove ``` fences a model may wrap code in, keeping the code itself untouched."""
    text = str(code).strip("\n")
    m = re.match(r"^\s*```[\w+#.-]*\s*\n(.*?)\n?\s*```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


def _normalise(result: dict) -> dict:
    """Fill defaults and make Kahoot answers consistent: every tile's key is its shape name.

    The dashboard draws tiles from `shape`, so whatever words the model used
    ("Triangle", "red", "rhombus") end up as one of the four canonical shapes.
    """
    result.setdefault("options", [])
    result.setdefault("answer_keys", [])
    result.setdefault("reasoning", "")
    if result.get("code"):
        result["code"] = _strip_fences(result["code"])
        result["question_type"] = "coding"
    if result.get("question_type") != "coding":
        for key in ("language", "code", "complexity"):
            result.pop(key, None)

    renamed: dict[str, str] = {}
    for opt in result["options"]:
        shape = _shape_of(opt.get("shape")) or _shape_of(opt.get("key"))
        opt["shape"] = shape
        if shape:
            renamed[str(opt.get("key"))] = shape
            opt["key"] = shape
        # Strip a leading shape/colour word the model may have glued onto the text
        # ("triangle5' poly-A tails", "square introns"), but not real words ("circles of Willis").
        text = str(opt.get("text", "")).strip()
        if shape:
            for word in (shape, KAHOOT_SHAPES[shape]):
                m = re.match(rf"{word}(?![a-z])[\s:.\-–]*", text, re.IGNORECASE)
                if m and text[m.end():].strip():
                    text = text[m.end():].strip()
                    break
        opt["text"] = text

    keys = []
    for key in result["answer_keys"]:
        key = renamed.get(str(key), _shape_of(key) or str(key))
        if key not in keys:
            keys.append(key)
    result["answer_keys"] = keys

    shapes = [k for k in keys if k in KAHOOT_SHAPES]
    if shapes and result.get("question_type") not in ("kahoot", "true_false"):
        result["question_type"] = "kahoot"
    result["shapes"] = shapes                  # every correct tile, in order
    result["shape"] = shapes[0] if shapes else None
    result["color"] = KAHOOT_SHAPES.get(result["shape"]) if result["shape"] else None
    return result


AUTO_TOOL_NOTE = (
    "\n\nAlways reply by calling the submit_answer tool exactly once. Do not answer in plain text."
)


def _is_tool_choice_rejection(exc: Exception) -> bool:
    """400 from models that don't accept a forced tool_choice (e.g. ones that always think first)."""
    return getattr(exc, "status_code", None) == 400 and "tool_choice" in str(exc)


def _answer_from_text(text: str) -> dict | None:
    """Last resort when a model answers in text: take the outermost JSON object, if any."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and "answer" in data else None


def _image_block(data: bytes, media_type: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.standard_b64encode(data).decode()}}


def build_content(image: bytes, media_type: str, context: list[bytes] | None = None) -> list[dict]:
    """User message: earlier pages of an open coding problem (if any), then the newest screenshot."""
    if not context:
        return [_image_block(image, media_type),
                {"type": "text", "text": "Read and answer the question in this screenshot."}]
    content: list[dict] = [{"type": "text", "text": (
        f"The user is working on a programming problem and captured it in parts. Here are the "
        f"{len(context)} earlier screenshot(s), oldest first:")}]
    for i, page in enumerate(context, 1):
        content += [{"type": "text", "text": f"Earlier screenshot {i}:"}, _image_block(page, "image/jpeg")]
    content += [
        {"type": "text", "text": "Newest screenshot:"},
        _image_block(image, media_type),
        {"type": "text", "text": (
            "Decide whether the newest screenshot shows the SAME problem as the earlier ones (for example "
            "the statement scrolled further, examples, constraints, or the code editor and its stub).\n"
            "- Same problem: set same_problem=true and answer using ALL screenshots together: the full "
            "statement, every constraint and the editor's language and stub.\n"
            "- Different question: set same_problem=false and answer only from the newest screenshot, "
            "ignoring the earlier ones.")},
    ]
    return content


class ClaudeSolver:
    def __init__(self) -> None:
        import anthropic  # imported lazily so the fake solver works without it

        # Keys that aren't scoped to a workspace must name one on every request.
        workspace = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
        self.client = anthropic.Anthropic(  # reads ANTHROPIC_API_KEY
            default_headers={"anthropic-workspace-id": workspace} if workspace else None,
        )
        self.model = os.getenv("QUIZBOT_MODEL", "claude-sonnet-5-5")
        # Only a ceiling: models that think before answering need room for it.
        self.max_tokens = int(os.getenv("QUIZBOT_MAX_TOKENS", "4096"))
        # Models found to reject a forced tool call; they get tool_choice "auto" from then on.
        self._no_forced_tool: set[str] = set()

    def _create(self, model: str, image: bytes, media_type: str, forced: bool,
                context: list[bytes] | None = None):
        return self.client.messages.create(
            model=model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT if forced else SYSTEM_PROMPT + AUTO_TOOL_NOTE,
            tools=[ANSWER_TOOL],
            tool_choice={"type": "tool", "name": "submit_answer"} if forced else {"type": "auto"},
            messages=[{"role": "user", "content": build_content(image, media_type, context)}],
        )

    def solve(self, image: bytes, media_type: str, model: str | None = None,
              context: list[bytes] | None = None) -> dict:
        model = model or self.model
        started = time.perf_counter()
        forced = model not in self._no_forced_tool
        try:
            resp = self._create(model, image, media_type, forced, context)
        except Exception as exc:
            if not (forced and _is_tool_choice_rejection(exc)):
                raise
            self._no_forced_tool.add(model)
            log.info("%s rejects forced tool_choice; using auto from now on", model)
            started = time.perf_counter()  # time the call that actually answered
            resp = self._create(model, image, media_type, forced=False, context=context)
        model_ms = int((time.perf_counter() - started) * 1000)

        result = None
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_answer":
                result = dict(block.input)
                break
        if result is None:
            text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
            result = _answer_from_text(text)
        if result is None:
            raise RuntimeError(f"{model} did not return an answer (stop_reason={resp.stop_reason})")
        result.setdefault("found_question", True)
        result.setdefault("question_type", "other")
        result.setdefault("question", "")
        result.setdefault("confidence", 0.5)
        if not context:
            result["same_problem"] = False
        result["model"] = model
        result["model_ms"] = model_ms
        result["input_tokens"] = resp.usage.input_tokens
        result["output_tokens"] = resp.usage.output_tokens
        return _normalise(result)


class FakeSolver:
    """Offline solver for testing the pipeline without an API key."""

    def solve(self, image: bytes, media_type: str, model: str | None = None,
              context: list[bytes] | None = None) -> dict:
        model = model or "fake"
        delay = 0.15 if "haiku" in model else 0.4  # lets the dashboard show a visible difference
        time.sleep(delay + random.random() * 0.1)
        if os.getenv("QUIZBOT_FAKE_MODE") == "coding":
            pages = len(context or []) + 1
            return _normalise({
                "same_problem": bool(context),
                "found_question": True, "question_type": "coding",
                "question": "A Very Big Sum — return the sum of an array of large integers."
                            + (f" (from {pages} screenshots)" if pages > 1 else ""),
                "reasoning": "Python ints are arbitrary precision, so a plain sum cannot overflow.",
                "language": "Python 3",
                "code": "```python\ndef aVeryBigSum(ar):\n    return sum(ar)\n```",
                "answer": "Sum the array; Python integers don't overflow.", "complexity": "O(n) time, O(1) space",
                "confidence": 0.97, "model": model, "model_ms": int(delay * 1000),
                "input_tokens": len(image) // 750, "output_tokens": 90,
            })
        if os.getenv("QUIZBOT_FAKE_MODE") == "multi" or random.random() < 0.3:
            # Multi-select with messy model output: capitalised keys and shape words in the text.
            return _normalise({
                "found_question": True, "question_type": "kahoot",
                "question": "Which of these are removed or added during mRNA processing?",
                "options": [
                    {"key": "Triangle", "text": "triangle 5' poly-A tails", "shape": "none"},
                    {"key": "Diamond", "text": "Hox genes"},
                    {"key": "Circle", "text": "exons"},
                    {"key": "Square", "text": "square introns"},
                ],
                "answer_keys": ["Triangle", "Square"],
                "answer": "5' poly-A tails | introns", "confidence": 0.82,
                "reasoning": "Fake multi-select answer.", "model": model, "model_ms": int(delay * 1000),
                "input_tokens": len(image) // 750, "output_tokens": 140,
            })
        shape = random.choice(list(KAHOOT_SHAPES))
        return _normalise(
            {
                "found_question": True,
                "question_type": "kahoot",
                "question": "Which planet is known as the Red Planet?",
                "options": [
                    {"key": "triangle", "text": "Venus", "shape": "triangle"},
                    {"key": "diamond", "text": "Mars", "shape": "diamond"},
                    {"key": "circle", "text": "Jupiter", "shape": "circle"},
                    {"key": "square", "text": "Mercury", "shape": "square"},
                ],
                "answer_keys": [shape],
                "answer": "Mars" if shape == "diamond" else "(fake answer)",
                "confidence": 0.9,
                "reasoning": f"Fake solver picked {shape} at random ({len(image)} bytes received).",
                "model": model,
                "model_ms": int((delay) * 1000),
                "input_tokens": len(image) // 750,
                "output_tokens": 120,
            }
        )


def make_solver():
    if os.getenv("QUIZBOT_SOLVER", "claude").lower() == "fake":
        return FakeSolver()
    return ClaudeSolver()
