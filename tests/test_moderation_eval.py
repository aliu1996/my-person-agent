import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "eval" / "run_moderation_eval.py"
FIXTURES = ROOT / "deploy" / "eval" / "fixtures"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_moderation_eval", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


eval_mod = load_runner()


def test_fixture_cases_match_the_schema():
    cases = eval_mod.load_cases(FIXTURES / "cases.jsonl")
    terms = eval_mod.load_terms(FIXTURES / "terms.json")
    assert terms == ["EVAL_PLACEHOLDER_TERM"]
    assert [case["id"] for case in cases] == [
        "allow-weather",
        "allow-thanks-output",
        "dfa-placeholder",
        "regex-private-key",
        "model-output-review",
        "allow-input-needs-model",
    ]


def test_parse_case_rejects_unknown_fields_and_duplicate_ids(tmp_path):
    with pytest.raises(ValueError, match="unexpected fields"):
        eval_mod.parse_case({"id": "x", "text": "hi", "expected": "allow", "extra": 1}, "s")
    with pytest.raises(ValueError, match="expected"):
        eval_mod.parse_case({"id": "x", "text": "hi", "expected": "maybe"}, "s")
    target = tmp_path / "dup-cases.json"
    target.write_text(
        '[{"id":"a","text":"t","expected":"allow"},{"id":"a","text":"u","expected":"allow"}]',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        eval_mod.load_cases(target)


@pytest.mark.asyncio
async def test_offline_runner_uses_chat_stream_moderation_on_fixtures(tmp_path):
    cases = eval_mod.load_cases(FIXTURES / "cases.jsonl")
    terms = eval_mod.load_terms(FIXTURES / "terms.json")
    rows = await eval_mod.evaluate(cases, terms, model=None)
    by_id = {row["id"]: row for row in rows}
    assert by_id["dfa-placeholder"]["outcome"] == "block"
    assert by_id["dfa-placeholder"]["layer"] == "dfa"
    assert by_id["dfa-placeholder"]["caught"] is True
    assert by_id["regex-private-key"]["outcome"] == "block"
    assert by_id["regex-private-key"]["layer"] == "regex"
    assert by_id["allow-thanks-output"]["outcome"] == "model_pending"
    assert by_id["allow-thanks-output"]["falsePositive"] is False
    assert by_id["allow-weather"]["outcome"] == "model_pending"
    assert by_id["allow-weather"]["falsePositive"] is False
    assert by_id["allow-input-needs-model"]["outcome"] == "model_pending"
    assert by_id["model-output-review"]["outcome"] == "model_pending"
    assert by_id["model-output-review"]["caught"] is True
    summary = eval_mod.summarize(rows, live=False)
    assert summary["total"] == 6
    assert summary["errors"] == 0
    assert summary["catch_rate"] == "1.0000"
    assert summary["false_positive_rate"] == "0.0000"
    eval_mod.write_outputs(
        tmp_path,
        summary,
        rows,
        {
            **summary,
            "generated_at": "now",
            "cases_path": "fixtures",
            "terms_path": "terms",
            "model": "offline",
        },
    )
    assert (tmp_path / "summary.json").exists()
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "2,000" in report
    assert "算法备案" in report
