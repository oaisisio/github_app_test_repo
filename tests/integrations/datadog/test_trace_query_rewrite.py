"""
Tests for the Datadog metric-to-trace query rewrite pipeline.

These tests validate that:
1. Metric tag filter values (Sinatra-style underscored routes) are correctly
   translated into trace resource_name patterns with curly-brace params.
2. The old single-character wildcard approach is demonstrated to fail when
   the character count differs between metric and trace representations.
3. The LLM fuzzy-match fallback path is exercised when exact wildcard
   matching yields zero spans.
"""

from __future__ import annotations

import re

from .conftest import (
    REWRITE_CASES,
    SAMPLE_SPANS,
    MetricTraceRewriteCase,
    MockDatadogAggregateResponse,
    MockDatadogSpanResponse,
)


# ---------------------------------------------------------------------------
# Helpers that mirror the production logic under test
# ---------------------------------------------------------------------------

def buggy_wildcard_pattern(metric_value: str) -> str:
    """Reproduce the old (buggy) behaviour: replace reserved chars with '?'."""
    return re.sub(r"[_\-{}]", "?", metric_value)


def improved_rewrite(metric_value: str) -> str:
    """
    Improved rewrite that correctly handles the underscore-to-curly-brace
    path-parameter expansion.

    Strategy:
      1. Replace the *first* underscore (verb separator) with a space.
      2. Detect path-parameter segments that start with `_` after a `/`
         and wrap them in `{}`, stripping the leading underscore.
      3. Preserve literal hyphens as-is (they are not wildcards).
    """
    # Step 1 — verb separator (e.g. "GET_/..." → "GET /...")
    result = metric_value.replace("_", " ", 1) if metric_value and "_" in metric_value else metric_value

    # Step 2 — path params: /_param_name → /{param_name}
    result = re.sub(r"/\s(\w+)", _replace_path_param, result)
    # Fallback for remaining underscore-prefixed segments not caught above
    result = re.sub(r"/ (\w+)", lambda m: "/{" + m.group(1) + "}", result)

    return result


def _replace_path_param(match: re.Match) -> str:
    """Convert a matched path-parameter segment into {param} form."""
    param = match.group(1)
    return "/{" + param + "}"


def wildcard_matches(pattern: str, text: str) -> bool:
    """Simple wildcard matcher where '?' matches exactly one character."""
    if len(pattern) != len(text):
        return False
    return all(p == "?" or p == t for p, t in zip(pattern, text))


def fuzzy_match_resource(metric_value: str, available_resources: list[str]) -> str | None:
    """
    LLM-style fuzzy fallback: pick the resource whose normalised form is
    closest to the normalised metric value.  (Simplified Levenshtein stand-in.)
    """
    def _normalise(s: str) -> str:
        return re.sub(r"[^a-zA-Z0-9/]", "", s).lower()

    norm_metric = _normalise(metric_value)
    best, best_score = None, 0
    for res in available_resources:
        norm_res = _normalise(res)
        # simple common-substring ratio
        common = sum(a == b for a, b in zip(norm_metric, norm_res))
        score = common / max(len(norm_metric), len(norm_res), 1)
        if score > best_score:
            best, best_score = res, score
    return best if best_score > 0.6 else None


# ---------------------------------------------------------------------------
# Tests — wildcard mismatch demonstration
# ---------------------------------------------------------------------------

class TestBuggyWildcardMismatch:
    """Demonstrate that the old single-char wildcard approach fails."""

    def test_wildcard_length_mismatch(self, rewrite_case: MetricTraceRewriteCase):
        """The buggy pattern has a different length than the real resource_name
        whenever underscores map to curly-braced params."""
        pattern = buggy_wildcard_pattern(rewrite_case.metric_tag_filter_value)
        resource = rewrite_case.expected_resource_name

        if rewrite_case.buggy_wildcard_pattern:
            assert pattern == rewrite_case.buggy_wildcard_pattern

        # For routes with path params the lengths diverge → zero matches
        has_path_params = "{" in resource
        if has_path_params:
            assert len(pattern) != len(resource), (
                f"Expected length mismatch for {resource!r} but got equal lengths"
            )
            assert not wildcard_matches(pattern, resource)

    def test_buggy_wildcard_returns_no_spans(
        self,
        rewrite_case: MetricTraceRewriteCase,
        mock_list_spans_response: MockDatadogSpanResponse,
    ):
        """When using the buggy pattern, no span in the response matches."""
        pattern = buggy_wildcard_pattern(rewrite_case.metric_tag_filter_value)
        matched = [
            s for s in mock_list_spans_response.data
            if wildcard_matches(pattern, s["resource_name"])
        ]
        has_path_params = "{" in rewrite_case.expected_resource_name
        if has_path_params:
            assert matched == [], "Buggy wildcard should not match any span"


# ---------------------------------------------------------------------------
# Tests — improved rewrite
# ---------------------------------------------------------------------------

class TestImprovedRewrite:
    """Validate the corrected rewrite logic."""

    def test_rewrite_produces_correct_resource_name(
        self, rewrite_case: MetricTraceRewriteCase
    ):
        rewritten = improved_rewrite(rewrite_case.metric_tag_filter_value)
        assert rewritten == rewrite_case.expected_resource_name

    def test_rewritten_value_matches_sample_span(
        self,
        rewrite_case: MetricTraceRewriteCase,
        sample_spans: list[dict],
    ):
        rewritten = improved_rewrite(rewrite_case.metric_tag_filter_value)
        matching = [s for s in sample_spans if s["resource_name"] == rewritten]
        assert len(matching) == 1, (
            f"Expected exactly one span for {rewritten!r}, got {len(matching)}"
        )


# ---------------------------------------------------------------------------
# Tests — fuzzy match fallback
# ---------------------------------------------------------------------------

class TestFuzzyMatchFallback:
    """Validate the LLM-style fuzzy fallback when wildcard fails."""

    def test_fuzzy_match_finds_correct_resource(
        self, rewrite_case: MetricTraceRewriteCase
    ):
        available = [s["resource_name"] for s in SAMPLE_SPANS]
        result = fuzzy_match_resource(rewrite_case.metric_tag_filter_value, available)
        assert result == rewrite_case.expected_resource_name

    def test_fuzzy_match_returns_none_for_unknown_route(self):
        available = [s["resource_name"] for s in SAMPLE_SPANS]
        result = fuzzy_match_resource("OPTIONS_/completely/unknown/route", available)
        assert result is None


# ---------------------------------------------------------------------------
# Tests — mock response shapes
# ---------------------------------------------------------------------------

class TestMockResponses:
    """Ensure mock response objects serialise correctly."""

    def test_list_spans_response_shape(
        self, mock_list_spans_response: MockDatadogSpanResponse
    ):
        data = mock_list_spans_response.to_dict()
        assert "data" in data
        assert isinstance(data["data"], list)
        assert len(data["data"]) == len(SAMPLE_SPANS)

    def test_aggregate_spans_response_shape(
        self, mock_aggregate_spans_response: MockDatadogAggregateResponse
    ):
        data = mock_aggregate_spans_response.to_dict()
        assert "data" in data
        assert "buckets" in data["data"]
        assert len(data["data"]["buckets"]) == len(SAMPLE_SPANS)

    def test_empty_responses(
        self,
        mock_empty_list_spans_response: MockDatadogSpanResponse,
        mock_empty_aggregate_spans_response: MockDatadogAggregateResponse,
    ):
        assert mock_empty_list_spans_response.to_dict() == {"data": []}
        assert mock_empty_aggregate_spans_response.to_dict() == {
            "data": {"buckets": []}
        }

    def test_span_meta_contains_required_keys(self, sample_spans: list[dict]):
        required_keys = {"service", "resource_name", "env"}
        for span in sample_spans:
            assert required_keys.issubset(span["meta"].keys()), (
                f"Span {span['span_id']} missing required meta keys"
            )


# ---------------------------------------------------------------------------
# Tests — environment variables
# ---------------------------------------------------------------------------

class TestEnvironmentSetup:
    """Verify the datadog_test_env fixture injects expected vars."""

    def test_env_vars_are_set(self, datadog_test_env: dict[str, str]):
        import os

        for key, value in datadog_test_env.items():
            assert os.environ[key] == value

    def test_required_keys_present(self, datadog_test_env: dict[str, str]):
        required = {"DD_API_KEY", "DD_APP_KEY", "DD_SITE"}
        assert required.issubset(datadog_test_env.keys())
