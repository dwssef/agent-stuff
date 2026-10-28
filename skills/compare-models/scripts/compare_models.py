#!/usr/bin/env python3
"""Compare AI models with Artificial Analysis data and emit JSON."""

import argparse
from difflib import get_close_matches
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://artificialanalysis.ai/api/v2"
DEFAULT_METRICS = (
    "input-price",
    "output-price",
    "task-cost",
    "intelligence",
    "coding",
    "agentic",
    "output-speed",
    "time-to-first-token",
    "time-to-first-answer-token",
    "end-to-end-response-time",
)


def metric(label, unit, path, direction=None, pro=False):
    return {
        "label": label,
        "unit": unit,
        "path": path,
        "direction": direction,
        "pro": pro,
    }


METRICS = {
    "input-price": metric("Input price", "USD / 1M tokens", ("pricing", "price_1m_input_tokens"), "lower"),
    "output-price": metric("Output price", "USD / 1M tokens", ("pricing", "price_1m_output_tokens"), "lower"),
    "task-cost": metric(
        "AA Intelligence Index cost per task",
        "USD / task",
        ("artificial_analysis_intelligence_index_cost", "cost_per_task", "total_cost"),
        "lower",
    ),
    "intelligence": metric(
        "Artificial Analysis Intelligence Index",
        "index points",
        ("evaluations", "artificial_analysis_intelligence_index"),
        "higher",
    ),
    "coding": metric("Coding Index", "index points", ("evaluations", "artificial_analysis_coding_index"), "higher"),
    "agentic": metric("Agentic Index", "index points", ("evaluations", "artificial_analysis_agentic_index"), "higher"),
    "cache-hit-price": metric(
        "Cache-hit price", "USD / 1M tokens", ("pricing", "price_1m_cache_hit_tokens"), "lower", True
    ),
    "cache-write-price": metric(
        "Cache-write price", "USD / 1M tokens", ("pricing", "price_1m_cache_write_tokens"), "lower", True
    ),
    "price-blended-3-to-1": metric(
        "Blended price (3:1 input/output)",
        "USD / 1M tokens",
        ("pricing", "price_1m_blended_3_to_1"),
        "lower",
        True,
    ),
    "price-blended-7-to-2-to-1": metric(
        "Blended price (7:2:1 input/cache/output)",
        "USD / 1M tokens",
        ("pricing", "price_1m_blended_7_to_2_to_1"),
        "lower",
        True,
    ),
    "finance-accounting": metric(
        "Finance & Accounting Index",
        "index points",
        ("evaluations", "artificial_analysis_finance_and_accounting_index"),
        "higher",
    ),
    "strategy-ops": metric(
        "Strategy & Operations Index",
        "index points",
        ("evaluations", "artificial_analysis_strategy_and_ops_index"),
        "higher",
    ),
    "legal": metric("Legal Index", "index points", ("evaluations", "artificial_analysis_legal_index"), "higher"),
    "healthcare-medical": metric(
        "Healthcare & Medical Index",
        "index points",
        ("evaluations", "artificial_analysis_healthcare_and_medical_index"),
        "higher",
    ),
    "engineering": metric(
        "Engineering Index",
        "index points",
        ("evaluations", "artificial_analysis_engineering_index"),
        "higher",
    ),
    "economics": metric(
        "Economics Index",
        "index points",
        ("evaluations", "artificial_analysis_economics_index"),
        "higher",
    ),
    "openness-index": metric(
        "Artificial Analysis Openness Index",
        "index points",
        ("evaluations", "artificial_analysis_openness_index"),
        "higher",
        True,
    ),
    "output-speed": metric(
        "Median output speed",
        "tokens / second",
        ("performance", "median_output_tokens_per_second"),
        "higher",
    ),
    "time-to-first-token": metric(
        "Median time to first token",
        "seconds",
        ("performance", "median_time_to_first_token_seconds"),
        "lower",
    ),
    "time-to-first-answer-token": metric(
        "Median time to first answer token",
        "seconds",
        ("performance", "median_time_to_first_answer_token_seconds"),
        "lower",
    ),
    "end-to-end-response-time": metric(
        "Median end-to-end response time",
        "seconds",
        ("performance", "median_end_to_end_response_time_seconds"),
        "lower",
    ),
    "context-window": metric("Context window", "tokens", ("context_window_tokens",), "higher", True),
    "parameters-total": metric("Total parameters", "billions", ("parameters", "total"), "descriptive", True),
    "parameters-active": metric("Active parameters", "billions", ("parameters", "active"), "descriptive", True),
    "open-weights": metric("Open weights", "boolean", ("licensing", "is_open_weights"), "descriptive", True),
}


class ApiError(RuntimeError):
    def __init__(self, message, code="api_error"):
        super().__init__(message)
        self.code = code


class ModelMatchError(RuntimeError):
    def __init__(self, query, message, candidates=None):
        super().__init__(message)
        self.query = query
        self.candidates = candidates or []


def normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def metric_key(value):
    key = value.strip().casefold().replace("_", "-")
    if key.startswith("benchmark:"):
        field = key.split(":", 1)[1].replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]+", field):
            raise ValueError(f"Invalid benchmark field: {value}")
        return f"benchmark:{field}"
    if key not in METRICS:
        choices = ", ".join(sorted(METRICS)) + ", benchmark:<API evaluation field>"
        raise ValueError(f"Unknown metric '{value}'. Available metrics: {choices}")
    return key


def metric_spec(key):
    if key.startswith("benchmark:"):
        field = key.split(":", 1)[1]
        return metric(f"Benchmark: {field.replace('_', ' ')}", "score", ("evaluations", field), None, True)
    return METRICS[key]


def select_metrics(includes=None, excludes=None):
    selected = list(DEFAULT_METRICS)
    for item in includes or []:
        key = metric_key(item)
        if key not in selected:
            selected.append(key)
    for item in excludes or []:
        key = metric_key(item)
        if key not in selected:
            raise ValueError(f"Cannot exclude metric '{item}': it is not selected")
        selected.remove(key)
    if not selected:
        raise ValueError("At least one metric must remain selected")
    return selected


def api_error_for_status(status):
    if status == 401:
        return ApiError("API key is invalid or unauthorized; check ARTIFICIAL_ANALYSIS_API_KEY", "unauthorized")
    if status == 403:
        return ApiError("The current API plan does not allow this endpoint or metric", "plan_required")
    if status == 429:
        return ApiError("Artificial Analysis API rate limit reached; try again later", "rate_limited")
    if status >= 500:
        return ApiError("Artificial Analysis API is temporarily unavailable", "service_unavailable")
    return ApiError(f"Artificial Analysis API rejected the request (HTTP {status})", "request_failed")


def request_page(api_key, endpoint, page):
    query = urlencode({"page": page})
    request = Request(
        f"{API_BASE}/{endpoint}?{query}",
        headers={"x-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError("Artificial Analysis API returned invalid JSON", "invalid_response") from exc
    except HTTPError as exc:
        raise api_error_for_status(exc.code) from None
    except URLError as exc:
        raise ApiError("Could not reach Artificial Analysis API; check network connectivity", "network_error") from exc
    except TimeoutError as exc:
        raise ApiError("Artificial Analysis API request timed out", "timeout") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ApiError("Artificial Analysis API response did not contain a model list", "invalid_response")
    return payload


def fetch_endpoint_pages(api_key, endpoint):
    first = request_page(api_key, endpoint, 1)
    pagination = first.get("pagination") or {}
    if not isinstance(pagination, dict):
        raise ApiError("Artificial Analysis API returned invalid pagination metadata", "invalid_response")
    try:
        total_pages = int(pagination.get("total_pages", 1))
    except (TypeError, ValueError) as exc:
        raise ApiError("Artificial Analysis API returned invalid pagination metadata", "invalid_response") from exc
    if total_pages < 1:
        raise ApiError("Artificial Analysis API returned invalid pagination metadata", "invalid_response")

    rows = list(first["data"])
    for page in range(2, total_pages + 1):
        response = request_page(api_key, endpoint, page)
        rows.extend(response["data"])
    if any(not isinstance(row, dict) for row in rows):
        raise ApiError("Artificial Analysis API returned an invalid model entry", "invalid_response")
    return rows, first.get("tier"), first.get("intelligence_index_version")


def fetch_models(api_key, selected_metrics):
    pro_metrics = [key for key in selected_metrics if metric_spec(key)["pro"]]
    endpoint = "language/models" if pro_metrics else "language/models/free"
    try:
        rows, tier, index_version = fetch_endpoint_pages(api_key, endpoint)
        return rows, tier, index_version, []
    except ApiError as exc:
        if not pro_metrics or exc.code != "plan_required":
            raise

    # Preserve the core comparison when a free-tier key cannot access requested Pro fields.
    rows, tier, index_version = fetch_endpoint_pages(api_key, "language/models/free")
    unavailable = [
        key for key in pro_metrics
        if any(read_path(row, metric_spec(key)["path"]) is None for row in rows)
    ]
    warnings = []
    if unavailable:
        warnings.append({
            "code": "plan_required",
            "message": "Some requested metrics are not available on the current API plan.",
            "metrics": unavailable,
        })
    return rows, tier, index_version, warnings


def resolve_model(query, rows):
    query_normalized = normalize(query)
    if not query_normalized:
        raise ModelMatchError(query, "Model name is empty")

    exact = [
        row for row in rows
        if query_normalized in (normalize(row.get("name")), normalize(row.get("slug")))
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        choices = [row for row in exact if row.get("name")]
        raise ModelMatchError(query, "Model name matches multiple entries", choices)

    partial = [
        row for row in rows
        if query_normalized in normalize(row.get("name")) or query_normalized in normalize(row.get("slug"))
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        choices = [row for row in partial if row.get("name")]
        raise ModelMatchError(query, "Model name is ambiguous", choices[:10])

    available = [row for row in rows if row.get("name")]
    suggestions = get_close_matches(
        str(query).casefold(),
        [str(row["name"]).casefold() for row in available],
        n=5,
        cutoff=0.35,
    )
    suggestion_set = {item.casefold() for item in suggestions}
    candidates = [row for row in available if str(row["name"]).casefold() in suggestion_set]
    raise ModelMatchError(query, "No matching model was found", candidates)


def read_path(data, path):
    value = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def build_comparisons(models, selected_metrics):
    comparisons = []
    if not models:
        return comparisons
    baseline = models[0]
    for key in selected_metrics:
        spec = metric_spec(key)
        baseline_value = baseline["metrics"].get(key)
        for candidate in models[1:]:
            candidate_value = candidate["metrics"].get(key)
            difference = None
            percent_change = None
            winner = None
            if (
                isinstance(baseline_value, (int, float))
                and not isinstance(baseline_value, bool)
                and isinstance(candidate_value, (int, float))
                and not isinstance(candidate_value, bool)
            ):
                difference = round(candidate_value - baseline_value, 6)
                if baseline_value > 0:
                    percent_change = round((candidate_value - baseline_value) / baseline_value * 100, 2)
                if spec["direction"] == "lower":
                    winner = candidate["name"] if candidate_value < baseline_value else (
                        baseline["name"] if candidate_value > baseline_value else "tie"
                    )
                elif spec["direction"] == "higher":
                    winner = candidate["name"] if candidate_value > baseline_value else (
                        baseline["name"] if candidate_value < baseline_value else "tie"
                    )
            comparisons.append({
                "metric": key,
                "baseline_model": baseline["name"],
                "candidate_model": candidate["name"],
                "baseline_value": baseline_value,
                "candidate_value": candidate_value,
                "difference": difference,
                "percent_change_vs_baseline": percent_change,
                "metric_winner": winner,
            })
    return comparisons


def compare(model_queries, selected_metrics, api_key):
    rows, tier, index_version, warnings = fetch_models(api_key, selected_metrics)
    resolved = []
    match_errors = []
    for query in model_queries:
        try:
            row = resolve_model(query, rows)
            values = {key: read_path(row, metric_spec(key)["path"]) for key in selected_metrics}
            creator_data = row.get("model_creator")
            resolved.append({
                "query": query,
                "name": row.get("name"),
                "slug": row.get("slug"),
                "creator": creator_data.get("name") if isinstance(creator_data, dict) else None,
                "metrics": values,
            })
        except ModelMatchError as exc:
            match_errors.append({
                "query": exc.query,
                "message": str(exc),
                "candidates": [
                    {"name": item.get("name"), "slug": item.get("slug")}
                    for item in exc.candidates
                ],
            })
    if match_errors:
        return {
            "source": "Artificial Analysis",
            "status": "needs_clarification",
            "tier": tier,
            "intelligence_index_version": index_version,
            "metrics": selected_metrics,
            "models": resolved,
            "comparisons": [],
            "model_match_errors": match_errors,
            "warnings": warnings,
        }
    return {
        "source": "Artificial Analysis",
        "status": "ok",
        "tier": tier,
        "intelligence_index_version": index_version,
        "metrics": [
            {"key": key, "label": metric_spec(key)["label"], "unit": metric_spec(key)["unit"]}
            for key in selected_metrics
        ],
        "models": resolved,
        "comparisons": build_comparisons(resolved, selected_metrics),
        "warnings": warnings,
    }


def dump_error(code, message):
    json.dump(
        {"source": "Artificial Analysis", "status": "error", "errors": [{"code": code, "message": message}]},
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+", help="At least two model names or slugs to compare")
    parser.add_argument(
        "--include",
        action="append",
        nargs="+",
        default=[],
        metavar="METRIC",
        help="Add one or more metrics to the defaults; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        nargs="+",
        default=[],
        metavar="METRIC",
        help="Hide one or more selected metrics; repeatable",
    )
    args = parser.parse_args(argv)
    if len(args.models) < 2:
        parser.error("at least two models are required")

    try:
        selected_metrics = select_metrics(
            [item for group in args.include for item in group],
            [item for group in args.exclude for item in group],
        )
    except ValueError as exc:
        dump_error("invalid_metric", str(exc))
        return 2

    api_key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
    if not api_key:
        dump_error(
            "missing_api_key",
            "Set ARTIFICIAL_ANALYSIS_API_KEY with an Artificial Analysis API key, then retry.",
        )
        return 2

    try:
        result = compare(args.models, selected_metrics, api_key)
    except ApiError as exc:
        dump_error(exc.code, str(exc))
        return 1
    except (KeyError, TypeError, ValueError) as exc:
        dump_error("invalid_response", f"Could not interpret API response ({type(exc).__name__})")
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
