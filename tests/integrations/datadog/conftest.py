"""
Fixtures and test environment for the Datadog trace alert investigation pipeline.

Provides sample metric tag filter values, expected trace resource_name values,
and mock responses for datadog_client.list_spans() / aggregate_spans() to test
the rewrite_datadog_metric_query_into_datadog_trace_query function and the
wildcard pattern matching in _process_datadog_trace_tag_filter_value().
"""

import os
from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Environment variables required by trace_util / trace_action_util /
# datadog_trace_alert_step_runner
# ---------------------------------------------------------------------------

TEST_ENV_VARS: dict[str, str] = {
    "DD_API_KEY": "test-api-key-000000",
    "DD_APP_KEY": "test-app-key-000000",
    "DD_SITE": "datadoghq.com",
    "BACCA_SERVICE_NAME": "bacca-executor",
    "BACCA_ENV": "test",
    "DD_TRACE_QUERY_TIME_RANGE_MINUTES": "60",
    "DD_TRACE_QUERY_MAX_SPANS": "500",
}


@pytest.fixture(autouse=False)
def datadog_test_env(monkeypatch):
    """Inject all required Datadog-related env vars for the test session."""
    for key, value in TEST_ENV_VARS.items():
        monkeypatch.setenv(key, value)
    yield TEST_ENV_VARS


# ---------------------------------------------------------------------------
# Metric → Trace rewrite test cases
#
# Each case captures:
#   - metric_tag_filter_value : the value as it appears in a DD metric query
#   - expected_resource_name  : the correct trace resource_name
#   - description             : human-readable explanation of the mismatch
#
# The core bug: reserved characters (_, -, {, }) in metric tags are replaced
# with single-character wildcard `?`, but the actual trace resource_name may
# have a *different* character count (e.g. metric `_post_id` → 8 chars vs
# trace `{post_id}` → 9 chars), causing zero matching spans.
# ---------------------------------------------------------------------------

@dataclass
class MetricTraceRewriteCase:
    """A single metric-to-trace rewrite test vector."""

    metric_tag_filter_value: str
    expected_resource_name: str
    description: str = ""
    # The naive wildcard pattern that the old (buggy) code would produce.
    buggy_wildcard_pattern: str = ""


REWRITE_CASES: list[MetricTraceRewriteCase] = [
    MetricTraceRewriteCase(
        metric_tag_filter_value="GET_/posts/_post_id/guest-order",
        expected_resource_name="GET /posts/{post_id}/guest-order",
        description=(
            "Underscore-delimited path param in metric becomes "
            "curly-braced param in trace; leading _ replaced by { and "
            "trailing boundary implies }. Character count differs."
        ),
        buggy_wildcard_pattern="GET?/posts/?post_id/guest?order",
    ),
    MetricTraceRewriteCase(
        metric_tag_filter_value="POST_/users/_user_id/settings",
        expected_resource_name="POST /users/{user_id}/settings",
        description="Same underscore-to-curly-brace mismatch for user_id param.",
        buggy_wildcard_pattern="POST?/users/?user_id/settings",
    ),
    MetricTraceRewriteCase(
        metric_tag_filter_value="DELETE_/orders/_order_id",
        expected_resource_name="DELETE /orders/{order_id}",
        description="Trailing path param with no suffix after the param.",
        buggy_wildcard_pattern="DELETE?/orders/?order_id",
    ),
    MetricTraceRewriteCase(
        metric_tag_filter_value="PUT_/items/_item_id/sub-items/_sub_item_id",
        expected_resource_name="PUT /items/{item_id}/sub-items/{sub_item_id}",
        description="Multiple path params in a single route.",
        buggy_wildcard_pattern="PUT?/items/?item_id/sub?items/?sub_item_id",
    ),
    MetricTraceRewriteCase(
        metric_tag_filter_value="GET_/health",
        expected_resource_name="GET /health",
        description="Simple route with no path params — only verb separator differs.",
        buggy_wildcard_pattern="GET?/health",
    ),
    MetricTraceRewriteCase(
        metric_tag_filter_value="PATCH_/accounts/_account_id/billing-info",
        expected_resource_name="PATCH /accounts/{account_id}/billing-info",
        description="Hyphenated suffix after a path param.",
        buggy_wildcard_pattern="PATCH?/accounts/?account_id/billing?info",
    ),
]


@pytest.fixture(params=REWRITE_CASES, ids=[c.metric_tag_filter_value for c in REWRITE_CASES])
def rewrite_case(request) -> MetricTraceRewriteCase:
    """Parametrized fixture that yields each rewrite test case in turn."""
    return request.param


# ---------------------------------------------------------------------------
# Mock Datadog client responses
# ---------------------------------------------------------------------------

def _make_span(
    trace_id: str,
    span_id: str,
    resource_name: str,
    service: str = "sinatra-app",
    duration_ns: int = 15_000_000,
    status: str = "ok",
    extra_tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a single span dict matching the shape returned by datadog_client.list_spans()."""
    tags = {
        "service": service,
        "resource_name": resource_name,
        "env": "production",
        "http.method": resource_name.split(" ")[0] if " " in resource_name else "GET",
        "http.url": "/" + resource_name.split(" ", 1)[-1].lstrip("/") if " " in resource_name else "/",
    }
    if extra_tags:
        tags.update(extra_tags)

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "resource_name": resource_name,
        "service": service,
        "duration": duration_ns,
        "status": status,
        "meta": tags,
    }


SAMPLE_SPANS: list[dict[str, Any]] = [
    _make_span(
        trace_id="abc123",
        span_id="span-001",
        resource_name="GET /posts/{post_id}/guest-order",
        duration_ns=23_400_000,
    ),
    _make_span(
        trace_id="abc124",
        span_id="span-002",
        resource_name="POST /users/{user_id}/settings",
        duration_ns=8_100_000,
    ),
    _make_span(
        trace_id="abc125",
        span_id="span-003",
        resource_name="DELETE /orders/{order_id}",
        duration_ns=5_300_000,
    ),
    _make_span(
        trace_id="abc126",
        span_id="span-004",
        resource_name="PUT /items/{item_id}/sub-items/{sub_item_id}",
        duration_ns=31_000_000,
    ),
    _make_span(
        trace_id="abc127",
        span_id="span-005",
        resource_name="GET /health",
        duration_ns=1_200_000,
    ),
    _make_span(
        trace_id="abc128",
        span_id="span-006",
        resource_name="PATCH /accounts/{account_id}/billing-info",
        duration_ns=12_700_000,
    ),
]


@pytest.fixture()
def sample_spans() -> list[dict[str, Any]]:
    """Return the full set of sample spans for list_spans mock responses."""
    return list(SAMPLE_SPANS)


# ---------------------------------------------------------------------------
# Mock list_spans / aggregate_spans responses
# ---------------------------------------------------------------------------

@dataclass
class MockDatadogSpanResponse:
    """Mimics the shape returned by datadog_client.list_spans()."""

    data: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"data": self.data}


@dataclass
class MockDatadogAggregateResponse:
    """Mimics the shape returned by datadog_client.aggregate_spans()."""

    buckets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"data": {"buckets": self.buckets}}


@pytest.fixture()
def mock_list_spans_response() -> MockDatadogSpanResponse:
    """A pre-populated list_spans response containing all sample spans."""
    return MockDatadogSpanResponse(data=SAMPLE_SPANS)


@pytest.fixture()
def mock_aggregate_spans_response() -> MockDatadogAggregateResponse:
    """A pre-populated aggregate_spans response with one bucket per unique resource."""
    buckets = []
    for span in SAMPLE_SPANS:
        buckets.append(
            {
                "by": {"resource_name": span["resource_name"]},
                "computes": {
                    "c0": span["duration"],  # e.g. sum of duration
                    "c1": 1,                 # count
                },
            }
        )
    return MockDatadogAggregateResponse(buckets=buckets)


@pytest.fixture()
def mock_empty_list_spans_response() -> MockDatadogSpanResponse:
    """An empty list_spans response — simulates the zero-match bug."""
    return MockDatadogSpanResponse(data=[])


@pytest.fixture()
def mock_empty_aggregate_spans_response() -> MockDatadogAggregateResponse:
    """An empty aggregate_spans response — simulates the zero-match bug."""
    return MockDatadogAggregateResponse(buckets=[])
