"""Question solver: sends a screenshot to Claude and gets back a structured answer.

The model is forced to call a single tool (`submit_answer`), so the output is always
machine-readable JSON regardless of question type.
"""
from __future__ import annotations

import base64
import os
import random
import time

# Kahoot's fixed answer tiles. Shape is the stable identifier; colour is for display.
KAHOOT_SHAPES = {
    "triangle": "red",
    "diamond": "blue",
    "circle": "yellow",
    "square": "green",
}

SYSTEM_PROMPT = """You are QuizBot. You receive a screenshot of a window that contains a quiz question.

Your job:
1. Find the question that is currently being asked. Ignore chrome, ads, timers, scoreboards and unrelated text.
2. Classify it:
   - multiple_choice: lettered/numbered options (A/B/C/D, 1/2/3/4, radio buttons, checkboxes)
   - kahoot: coloured tiles with shapes (red triangle, blue diamond, yellow circle, green square)
   - true_false: exactly two options meaning true/false or yes/no
   - open: no options shown; a free-text answer is expected
   - other: anything else (ordering, matching, fill-in-the-blank, maths working ...)
3. Transcribe the question and every option exactly as shown.
4. Answer it. Think carefully but be fast: the user is under time pressure.
   - For choice questions, return the key(s) of the correct option(s). Use the key printed on screen
     (e.g. "B" or "2"); for Kahoot use the shape name as the key.
   - If several options are correct ("select all that apply"), return all of their keys.
   - For open questions, give a concise, correct answer the user could type directly.
5. Give an honest confidence between 0 and 1.

If no question is visible, set found_question to false and leave the rest minimal."""

ANSWER_TOOL = {
    "name": "submit_answer",
    "description": "Submit the transcribed question and its answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found_question": {"type": "boolean"},
            "question_type": {
                "type": "string",
                "enum": ["multiple_choice", "kahoot", "true_false", "open", "other"],
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
            "answer_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key(s) of the correct option(s). Empty for open questions.",
            },
            "answer": {
                "type": "string",
                "description": "The answer as text (option text for choice questions, full answer for open ones).",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string", "description": "One or two sentences on why."},
        },
        "required": ["found_question", "question_type", "question", "answer", "confidence"],
    },
}


def _normalise(result: dict) -> dict:
    """Fill defaults and derive the Kahoot shape/colour of the answer."""
    result.setdefault("options", [])
    result.setdefault("answer_keys", [])
    result.setdefault("reasoning", "")
    shape = None
    for opt in result["options"]:
        if opt.get("key") in result["answer_keys"] and opt.get("shape") in KAHOOT_SHAPES:
            shape = opt["shape"]
            break
    if shape is None:
        for key in result["answer_keys"]:
            if key.lower() in KAHOOT_SHAPES:
                shape = key.lower()
                break
    result["shape"] = shape
    result["color"] = KAHOOT_SHAPES.get(shape) if shape else None
    return result


class ClaudeSolver:
    def __init__(self) -> None:
        import anthropic  # imported lazily so the fake solver works without it

        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = os.getenv("QUIZBOT_MODEL", "claude-sonnet-5-5")
        self.max_tokens = int(os.getenv("QUIZBOT_MAX_TOKENS", "1024"))

    def solve(self, image: bytes, media_type: str, model: str | None = None) -> dict:
        model = model or self.model
        started = time.perf_counter()
        resp = self.client.messages.create(
            model=model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            tools=[ANSWER_TOOL],
            tool_choice={"type": "tool", "name": "submit_answer"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image).decode(),
                            },
                        },
                        {"type": "text", "text": "Read and answer the question in this screenshot."},
                    ],
                }
            ],
        )
        model_ms = int((time.perf_counter() - started) * 1000)
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_answer":
                result = dict(block.input)
                result["model"] = model
                result["model_ms"] = model_ms
                result["input_tokens"] = resp.usage.input_tokens
                result["output_tokens"] = resp.usage.output_tokens
                return _normalise(result)
        raise RuntimeError(f"Model did not return an answer (stop_reason={resp.stop_reason})")


class FakeSolver:
    """Offline solver for testing the pipeline without an API key."""

    def solve(self, image: bytes, media_type: str, model: str | None = None) -> dict:
        model = model or "fake"
        delay = 0.15 if "haiku" in model else 0.4  # lets the dashboard show a visible difference
        time.sleep(delay + random.random() * 0.1)
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
