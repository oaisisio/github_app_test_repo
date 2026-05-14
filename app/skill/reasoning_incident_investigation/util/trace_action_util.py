import logging
from typing import Any, Dict, Optional

from app.integrations.datadog.trace_util import (
    rewrite_datadog_metric_query_into_datadog_trace_query,
)

logger = logging.getLogger(__name__)


def build_trace_query_from_metric_query(
    metric_query_filter: str,
    datadog_client: Any,
    start: int,
    end: int,
) -> Optional[str]:
    """Attempt to rewrite a Datadog metric query filter into a trace query.

    Wraps :func:`rewrite_datadog_metric_query_into_datadog_trace_query` and
    handles the ``None`` return gracefully by logging a warning (not an error)
    and returning ``None`` so the caller can decide how to proceed.
    """
    trace_query = rewrite_datadog_metric_query_into_datadog_trace_query(
        query_filter_str=metric_query_filter,
        datadog_client=datadog_client,
        start=start,
        end=end,
    )

    if trace_query is None:
        logger.warning(
            "Trace query rewrite returned None for metric filter: %s. "
            "Downstream trace analysis will be skipped.",
            metric_query_filter,
        )

    return trace_query


def build_trace_alert_context(
    metric_query_filter: str,
    datadog_client: Any,
    start: int,
    end: int,
    additional_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a trace alert context dict, including the rewritten query if available."""
    context: Dict[str, Any] = {
        "original_metric_filter": metric_query_filter,
        "trace_query": None,
        "trace_analysis_available": False,
    }

    if additional_context:
        context.update(additional_context)

    trace_query = build_trace_query_from_metric_query(
        metric_query_filter=metric_query_filter,
        datadog_client=datadog_client,
        start=start,
        end=end,
    )

    if trace_query is not None:
        context["trace_query"] = trace_query
        context["trace_analysis_available"] = True

    return context
