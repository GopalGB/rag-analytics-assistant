"""Pre-generate answers to the demo's suggested questions for the hosted public demo.

The public demo is stateless and its documents never change between deploys, so an answer to a
suggested question is the same every time it is asked. This script asks each suggested question once
through the real pipeline (same guardrails, routing, tools and model as the live app) and writes the
answers with a fingerprint of the documents. The demo serves them instantly (labelled "cached answer")
and only while the documents still match the fingerprint; anything else goes to the model as usual.

Run it with the same model configuration as the deployment, e.g.:

    GROQ_API_KEY=... ALLOW_CLOUD_AI=true PUBLIC_DEMO=true DB_PATH=:memory: EMBEDDING_PROVIDER=local \\
    INVOICE_AI_ASSIST=false CLOUD_ALLOWED_DATA=documents,invoices,accounting,bank REPORT_AS_OF=2026-07-15 \\
    LLM_MODELS_STRONG=groq:openai/gpt-oss-120b LLM_MODELS_FAST=groq:openai/gpt-oss-120b \\
    python scripts/build_answer_cache.py --pace-seconds 20

Then set ANSWER_CACHE_SEED=data/demo_answer_cache.json on the deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.answer_cache import UTC, cacheable, normalize, seed_fingerprint  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import usage  # noqa: E402
from app.main import examples  # noqa: E402
from app.workspace import Workspace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", default=str(ROOT / "data" / "demo_answer_cache.json"))
    parser.add_argument(
        "--pace-seconds", type=float, default=20.0, help="pause between questions (free tiers)"
    )
    parser.add_argument("--question", action="append", help="extra question to include (repeatable)")
    parser.add_argument(
        "--only-missing", action="store_true",
        help="keep the answers already in --output (same documents) and ask only the others",
    )
    args = parser.parse_args()

    settings = Settings(answer_cache_seed=None)
    if not settings.public_demo:
        print("Set PUBLIC_DEMO=true: the seed must come from the same stateless pipeline the demo runs.")
        return 2
    ws = Workspace(settings)
    ws.startup()
    ws.engine.answer_cache = None  # always ask the model here
    if not ws.router.available:
        print(f"No AI model is available: {ws.llm_note}")
        return 2

    questions = list(dict.fromkeys(examples()["examples"] + (args.question or [])))
    answers: dict[str, dict] = {}
    output = Path(args.output)
    if args.only_missing and output.is_file():
        previous = json.loads(output.read_text(encoding="utf-8"))
        if previous.get("fingerprint") == seed_fingerprint(settings):
            answers = previous.get("answers") or {}
    failed: list[str] = []
    asked = 0
    for question in questions:
        if normalize(question) in answers:
            print(f"kept    {question}")
            continue
        if asked:
            time.sleep(args.pace_seconds)
        asked += 1
        n = asked
        with usage.capture() as used:
            payload = ws.engine.answer(f"seed-{n}", question, actor="seed-builder")
        tokens = f"{used.input_tokens} in / {used.output_tokens} out"
        if cacheable(payload):
            payload["cached_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            answers[normalize(question)] = payload
            print(f"ok      {question}  ({payload['routing']['model']}, {tokens})")
        else:
            failed.append(question)
            print(f"skipped {question}  ({tokens}; route={payload.get('route')}: {payload.get('text', '')[:120]!r})")

    out = {
        "fingerprint": seed_fingerprint(settings),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": sorted({p["routing"]["model"] for p in answers.values()}),
        "answers": answers,
    }
    output.write_text(json.dumps(out, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"\nWrote {len(answers)} answer(s) to {args.output}; {len(failed)} skipped.")
    return 0 if answers and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
