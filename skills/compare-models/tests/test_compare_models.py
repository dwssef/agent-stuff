import io
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import compare_models


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def model(name, slug, *, input_price=1.0, intelligence=10, task_cost=0.1):
    return {
        "name": name,
        "slug": slug,
        "model_creator": {"name": "Test Provider"},
        "evaluations": {
            "artificial_analysis_intelligence_index": intelligence,
            "artificial_analysis_coding_index": None,
            "artificial_analysis_agentic_index": 5,
        },
        "artificial_analysis_intelligence_index_cost": {
            "cost_per_task": {"total_cost": task_cost},
        },
        "pricing": {"price_1m_input_tokens": input_price, "price_1m_output_tokens": 2.0},
    }


class CompareModelsTests(unittest.TestCase):
    def test_default_metrics_and_per_call_changes(self):
        self.assertEqual(
            compare_models.select_metrics(),
            ["input-price", "output-price", "task-cost", "intelligence", "coding", "agentic",
             "output-speed", "time-to-first-token", "time-to-first-answer-token", "end-to-end-response-time"],
        )
        self.assertEqual(
            compare_models.select_metrics(["output-speed", "context_window"], ["agentic"]),
            ["input-price", "output-price", "task-cost", "intelligence", "coding", "output-speed",
             "time-to-first-token", "time-to-first-answer-token", "end-to-end-response-time", "context-window"],
        )

    def test_unknown_or_all_excluded_metrics_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown metric"):
            compare_models.select_metrics(["not-a-metric"])
        with self.assertRaisesRegex(ValueError, "At least one metric"):
            compare_models.select_metrics(excludes=list(compare_models.DEFAULT_METRICS))

    def test_fetch_models_reads_all_pages(self):
        pages = [
            {"tier": "free", "intelligence_index_version": 4.2,
             "pagination": {"total_pages": 2}, "data": [model("A", "a")]},
            {"tier": "free", "pagination": {"total_pages": 2}, "data": [model("B", "b")]},
        ]
        with patch.object(compare_models, "urlopen", side_effect=[FakeResponse(pages[0]), FakeResponse(pages[1])]) as mocked:
            rows, tier, version, warnings = compare_models.fetch_models("test-key", list(compare_models.DEFAULT_METRICS))
        self.assertEqual([row["slug"] for row in rows], ["a", "b"])
        self.assertEqual((tier, version), ("free", 4.2))
        self.assertEqual(warnings, [])
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("page=1", mocked.call_args_list[0].args[0].full_url)
        self.assertIn("page=2", mocked.call_args_list[1].args[0].full_url)

    def test_pro_metric_uses_full_endpoint(self):
        page = {"tier": "pro", "pagination": {"total_pages": 1}, "data": []}
        with patch.object(compare_models, "urlopen", return_value=FakeResponse(page)) as mocked:
            _, _, _, warnings = compare_models.fetch_models("test-key", ["context-window"])
        self.assertIn("/language/models?", mocked.call_args.args[0].full_url)
        self.assertEqual(warnings, [])

    def test_pro_plan_denial_falls_back_and_marks_missing_metrics(self):
        free_page = {
            "tier": "free",
            "intelligence_index_version": 4.2,
            "pagination": {"total_pages": 1},
            "data": [model("Model A", "a"), model("Model B", "b")],
        }
        denied = HTTPError("https://example.invalid", 403, "forbidden", {}, io.BytesIO())
        with patch.object(compare_models, "urlopen", side_effect=[denied, FakeResponse(free_page)]) as mocked:
            rows, tier, version, warnings = compare_models.fetch_models("test-key", ["task-cost", "context-window"])
        self.assertEqual(len(rows), 2)
        self.assertEqual((tier, version), ("free", 4.2))
        self.assertEqual(warnings[0]["metrics"], ["context-window"])
        self.assertIn("/language/models/free?", mocked.call_args.args[0].full_url)

    def test_exact_partial_ambiguous_and_missing_model_matching(self):
        rows = [model("Claude Sonnet 4", "claude-sonnet-4"), model("Claude Sonnet 4 Thinking", "claude-sonnet-4-thinking")]
        self.assertEqual(compare_models.resolve_model("claude-sonnet-4", rows)["name"], "Claude Sonnet 4")
        duplicates = [model("Claude Sonnet 4", "claude-sonnet-4"), model("Claude Sonnet 4", "claude-sonnet-4-v2")]
        with self.assertRaises(compare_models.ModelMatchError) as ambiguous:
            compare_models.resolve_model("Claude Sonnet 4", duplicates)
        self.assertEqual(len(ambiguous.exception.candidates), 2)
        with self.assertRaises(compare_models.ModelMatchError):
            compare_models.resolve_model("nonexistent model", rows)

    def test_comparison_reports_metric_deltas_without_overall_score(self):
        rows = [model("Model A", "a", input_price=2, intelligence=10, task_cost=0.5),
                model("Model B", "b", input_price=1, intelligence=12, task_cost=0.25)]
        with patch.object(compare_models, "fetch_models", return_value=(rows, "free", 4.2, [])):
            result = compare_models.compare(["Model A", "Model B"], list(compare_models.DEFAULT_METRICS), "key")
        self.assertEqual(result["status"], "ok")
        comparisons = {item["metric"]: item for item in result["comparisons"]}
        self.assertEqual(comparisons["input-price"]["percent_change_vs_baseline"], -50.0)
        self.assertEqual(comparisons["input-price"]["metric_winner"], "Model B")
        self.assertEqual(comparisons["intelligence"]["metric_winner"], "Model B")
        self.assertIsNone(result.get("overall_score"))
        self.assertIsNone(result["models"][0]["metrics"]["coding"])

    def test_ambiguous_models_return_candidates_for_clarification(self):
        rows = [model("Claude Sonnet 4", "claude-sonnet-4"),
                model("Claude Sonnet 4 Thinking", "claude-sonnet-4-thinking")]
        with patch.object(compare_models, "fetch_models", return_value=(rows, "free", 4.2, [])):
            result = compare_models.compare(["Claude Sonnet", "Model B"], list(compare_models.DEFAULT_METRICS), "key")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["model_match_errors"][0]["query"], "Claude Sonnet")
        self.assertTrue(result["model_match_errors"][0]["candidates"])

    def test_api_status_errors_are_sanitized_and_actionable(self):
        for status, code, expected in (
            (401, "unauthorized", "ARTIFICIAL_ANALYSIS_API_KEY"),
            (403, "plan_required", "plan"),
            (429, "rate_limited", "rate limit"),
        ):
            with self.subTest(status=status):
                error = HTTPError("https://example.invalid", status, "secret response", {}, io.BytesIO(b"secret"))
                with patch.object(compare_models, "urlopen", side_effect=error):
                    with self.assertRaises(compare_models.ApiError) as caught:
                        compare_models.fetch_models("never-print-this-key", list(compare_models.DEFAULT_METRICS))
                self.assertEqual(caught.exception.code, code)
                self.assertIn(expected.casefold(), str(caught.exception).casefold())
                self.assertNotIn("never-print-this-key", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_missing_key_is_reported_without_network_request(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", output), patch.object(compare_models, "urlopen") as mocked:
            status = compare_models.main(["Model A", "Model B"])
        self.assertEqual(status, 2)
        mocked.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["errors"][0]["code"], "missing_api_key")


if __name__ == "__main__":
    unittest.main()
