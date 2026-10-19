"""Replay saved captures against several models / image sizes and compare speed.

Run inside the server container (uses the same API key and stored captures):
    docker compose exec quizbot python -m app.bench --models claude-haiku-5-5,claude-sonnet-5-5 --n 10
    docker compose exec quizbot python -m app.bench --models claude-haiku-5-5 --edges 1568,1024,768
    python -m app.bench --images ./my_screens/ --models claude-haiku-5-5,claude-sonnet-5-5

"agree" = share of questions where the variant picked the same option(s) as the reference
(the first model at the largest edge). It is a consistency check, not ground truth.
Stored captures are already downscaled to QUIZBOT_MAX_EDGE, so only smaller edges are meaningful.
"""
from __future__ import annotations

import argparse
import os
import statistics
import time
from pathlib import Path

from .main import DATA_DIR, prepare_image, store
from .solver import make_solver


def _p(values: list[int], q: float) -> int:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=os.getenv("QUIZBOT_MODEL", "claude-sonnet-5-5"),
                    help="comma-separated model ids")
    ap.add_argument("--edges", default="1568", help="comma-separated max image edges, e.g. 1568,1024,768")
    ap.add_argument("--n", type=int, default=10, help="number of most recent captures to replay")
    ap.add_argument("--images", type=Path, help="folder of .png/.jpg screenshots instead of stored captures")
    ap.add_argument("--repeat", type=int, default=1, help="calls per image per variant (reduces noise)")
    ap.add_argument("--diff", action="store_true",
                    help="print each question where the variants disagree, with every variant's pick")
    args = ap.parse_args()

    if args.images:
        paths = sorted(p for p in args.images.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"})[: args.n]
        labels = [p.name for p in paths]
    else:
        caps = store.captures(args.n)
        paths = [Path(p) for _, p in caps]
        labels = [f"capture #{i}  (image: /api/answers/{i}/image)" for i, _ in caps]
    if not paths:
        raise SystemExit(f"no captures found (looked in {args.images or DATA_DIR / 'captures'})")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    edges = sorted({int(e) for e in args.edges.split(",")}, reverse=True)
    solver = make_solver()
    raw_images = [p.read_bytes() for p in paths]
    print(f"{len(paths)} images x {len(models)} models x {len(edges)} sizes x {args.repeat} repeats\n")

    results: dict[tuple[str, int], list[dict]] = {}
    for model in models:
        for edge in edges:
            runs = []
            for i, raw in enumerate(raw_images):
                jpeg, px = prepare_image(raw, edge)
                for _ in range(args.repeat):
                    t = time.perf_counter()
                    try:
                        r = solver.solve(jpeg, "image/jpeg", model)
                        r["wall_ms"] = int((time.perf_counter() - t) * 1000)
                        r["img"] = i
                        runs.append(r)
                    except Exception as exc:
                        print(f"  {model} @{edge}: image {i} failed: {exc}")
                print(f"  {model} @{edge}px  image {i + 1}/{len(raw_images)}", end="\r")
            results[(model, edge)] = runs
    print(" " * 60)

    ref_key = (models[0], edges[0])
    ref = {r["img"]: sorted(r.get("answer_keys") or [r.get("answer", "")]) for r in results.get(ref_key, [])}

    header = f"{'model':<22}{'edge':>6}{'n':>4}{'p50 ms':>9}{'p90 ms':>9}{'mean':>8}{'in tok':>8}{'agree':>7}"
    print(header)
    print("-" * len(header))
    for (model, edge), runs in results.items():
        if not runs:
            print(f"{model:<22}{edge:>6}   all calls failed")
            continue
        ms = [r["wall_ms"] for r in runs]
        toks = [r.get("input_tokens") or 0 for r in runs]
        same = [sorted(r.get("answer_keys") or [r.get("answer", "")]) == ref.get(r["img"]) for r in runs if r["img"] in ref]
        agree = f"{100 * sum(same) / len(same):.0f}%" if same else "-"
        print(f"{model:<22}{edge:>6}{len(runs):>4}{_p(ms, .5):>9}{_p(ms, .9):>9}"
              f"{statistics.mean(ms):>8.0f}{int(statistics.median(toks)):>8}{agree:>7}")
    print(f"\nreference for 'agree': {ref_key[0]} @{ref_key[1]}px")
    if args.diff:
        print_diff(results, labels)


def _pick(r: dict) -> tuple[list[str], str]:
    keys = sorted(r.get("answer_keys") or [])
    return keys, (", ".join(keys) if keys else str(r.get("answer", "")).strip())


def print_diff(results: dict[tuple[str, int], list[dict]], labels: list[str]) -> None:
    """One block per image where variants picked different answers (first run of each variant)."""
    variants = list(results)
    shown = 0
    for i, label in enumerate(labels):
        runs = {v: next((r for r in results[v] if r["img"] == i), None) for v in variants}
        picks = {v: _pick(r)[0] or [_pick(r)[1].lower()] for v, r in runs.items() if r}
        if len({tuple(p) for p in picks.values()}) <= 1:
            continue
        shown += 1
        base = next(r for r in runs.values() if r)
        print(f"\n=== {label}  [{base.get('question_type')}]")
        print(f"Q: {base.get('question', '').strip()}")
        for o in base.get("options") or []:
            print(f"   {o.get('key', ''):>9}  {o.get('text', '')}")
        for (model, edge), r in runs.items():
            if not r:
                print(f"   {model} @{edge}: failed")
                continue
            keys, text = _pick(r)
            texts = {o.get("key"): o.get("text") for o in r.get("options") or []}
            shown_text = " | ".join(texts.get(k, k) for k in keys) if keys else text
            print(f"-> {model:<20} @{edge:<5} {', '.join(keys) or '-':<20} {shown_text}  "
                  f"({round(100 * (r.get('confidence') or 0))}%)")
            if r.get("reasoning"):
                print(f"   {'':<27} why: {r['reasoning'][:160]}")
    print(f"\n{shown} question(s) with disagreement" if shown else "\nNo disagreements.")


if __name__ == "__main__":
    main()
