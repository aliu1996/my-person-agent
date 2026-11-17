"""Run moderation cases through chat_stream.moderation (DFA, regex, optional model)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
from chat_stream.moderation import Blocked, TermMatcher, check
from pa_shared.model_gateway import GatewayError

DEFAULT_CASES = Path(__file__).resolve().parent / "fixtures" / "cases.jsonl"
DEFAULT_TERMS = Path(__file__).resolve().parent / "fixtures" / "terms.json"
TEMPLATE = Path(__file__).resolve().parent / "report-template.md"
VALID_DIRECTIONS = {"input", "output"}
VALID_EXPECTED = {"allow", "block"}
VALID_LAYERS = {"dfa", "regex", "model"}

# Input always reaches the model after local layers; output may too.
MODEL_NOT_CONFIGURED = "Moderation model is not configured"


def load_jsonl(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON array")
        return data
    rows = []
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{index} is not valid JSON") from exc
    return rows


def parse_case(raw: dict, source: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: case must be an object")
    case_id = raw.get("id")
    text = raw.get("text")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"{source}: id is required")
    if not isinstance(text, str) or not text:
        raise ValueError(f"{source}: text is required")
    direction = raw.get("direction", "input")
    expected = raw.get("expected")
    layer = raw.get("expectedLayer")
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"{source}: invalid direction")
    if expected not in VALID_EXPECTED:
        raise ValueError(f"{source}: expected must be allow or block")
    if layer is not None and layer not in VALID_LAYERS:
        raise ValueError(f"{source}: invalid expectedLayer")
    extra = set(raw) - {"id", "text", "direction", "expected", "expectedLayer", "note"}
    if extra:
        raise ValueError(f"{source}: unexpected fields {sorted(extra)}")
    return {
        "id": case_id.strip(),
        "text": text,
        "direction": direction,
        "expected": expected,
        "expectedLayer": layer,
        "note": raw.get("note") or "",
    }


def load_terms(path: Path) -> list[str]:
    terms = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(terms, list) or any(not isinstance(item, str) or not item.strip() for item in terms):
        raise ValueError(f"{path} must be a JSON array of terms")
    return [item.strip() for item in terms]


def load_cases(path: Path) -> list[dict]:
    cases = [parse_case(raw, f"{path}:{index}") for index, raw in enumerate(load_jsonl(path), 1)]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} contains duplicate case ids")
    return cases


async def run_case(case: dict, matcher: TermMatcher, http, model: str | None) -> dict:
    try:
        await check(http, matcher, model, case["direction"], case["text"])
        return {"id": case["id"], "outcome": "allow", "layer": None, "error": None}
    except Blocked as exc:
        return {"id": case["id"], "outcome": "block", "layer": exc.hit.layer, "error": None}
    except GatewayError as exc:
        pending = MODEL_NOT_CONFIGURED in str(exc)
        return {
            "id": case["id"],
            "outcome": "model_pending" if pending else "error",
            "layer": "model" if pending else None,
            "error": None if pending else str(exc),
        }
    except Exception as exc:
        return {"id": case["id"], "outcome": "error", "layer": None, "error": str(exc)}


def judged(case: dict, result: dict, *, live: bool) -> dict:
    expected = case["expected"]
    layer = case["expectedLayer"]
    outcome = result["outcome"]
    caught = False
    false_positive = False
    layer_ok = None
    if expected == "block":
        if layer == "model":
            caught = (live and outcome == "block" and result["layer"] == "model") or (
                not live and outcome == "model_pending"
            )
        else:
            caught = outcome == "block"
        if layer:
            layer_ok = caught and (result["layer"] == layer or (not live and layer == "model"))
    elif expected == "allow":
        false_positive = outcome == "block"
        caught = False
    return {
        **result,
        "expected": expected,
        "expectedLayer": layer,
        "caught": caught,
        "falsePositive": false_positive,
        "layerMatch": layer_ok,
        "note": case["note"],
    }


def rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a"
    return f"{numerator / denominator:.4f}"


def summarize(rows: list[dict], *, live: bool) -> dict:
    counts = Counter(row["outcome"] for row in rows)
    layers = Counter(row["layer"] for row in rows if row["layer"])
    should_block = [row for row in rows if row["expected"] == "block"]
    should_allow = [row for row in rows if row["expected"] == "allow"]
    labeled_layer = [row for row in should_block if row["expectedLayer"]]
    return {
        "mode": "live" if live else "offline",
        "total": len(rows),
        "allowed": counts["allow"],
        "blocked": counts["block"],
        "model_pending": counts["model_pending"],
        "errors": counts["error"],
        "blocked_or_pending": counts["block"] + counts["model_pending"],
        "catch_rate": rate(sum(row["caught"] for row in should_block), len(should_block)),
        "false_positive_rate": rate(sum(row["falsePositive"] for row in should_allow), len(should_allow)),
        "layer_matches": sum(row["layerMatch"] is True for row in labeled_layer),
        "labeled_layer": len(labeled_layer),
        "layers": dict(layers),
        "should_block": len(should_block),
        "should_allow": len(should_allow),
    }


def render_report(template: str, context: dict) -> str:
    table = "无"
    if context.get("layers"):
        lines = ["| 层 | 数量 |", "| --- | --- |"]
        lines.extend(f"| {name} | {count} |" for name, count in context["layers"].items())
        table = "\n".join(lines)
    rendered = template
    for key, value in {**context, "layer_table": table}.items():
        rendered = rendered.replace("{{ " + key + " }}", str(value))
    return rendered


async def evaluate(cases: list[dict], terms: list[str], *, model: str | None) -> list[dict]:
    matcher = TermMatcher(terms)
    async with httpx.AsyncClient(trust_env=False) as http:
        results = []
        for case in cases:
            raw = await run_case(case, matcher, http, model)
            results.append(judged(case, raw, live=bool(model)))
        return results


def write_outputs(out_dir: Path, summary: dict, rows: list[dict], context: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps({"summary": summary, "results": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    template = TEMPLATE.read_text(encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(template, context), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="JSONL or JSON array of eval cases")
    parser.add_argument("--terms", type=Path, default=DEFAULT_TERMS, help="JSON array of DFA terms")
    parser.add_argument("--out", type=Path, default=Path("deploy/eval/data/last-run"))
    parser.add_argument(
        "--model",
        default="",
        help="Moderation model code for a live run via the existing gateway. Leave empty for offline.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = load_cases(args.cases)
    terms = load_terms(args.terms)
    model = args.model.strip() or None
    rows = asyncio.run(evaluate(cases, terms, model=model))
    summary = summarize(rows, live=bool(model))
    context = {
        **summary,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "cases_path": str(args.cases),
        "terms_path": str(args.terms),
        "model": model or "（离线，未调用审核模型）",
    }
    write_outputs(args.out, summary, rows, context)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {args.out / 'summary.json'} and {args.out / 'report.md'}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
