import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Datadog reserved tag keys that are natively supported in trace queries
# and do not require the `@` prefix for custom tag filtering.
DATADOG_TRACE_RESERVED_WORDS = frozenset(
    {
        "service",
        "resource_name",
        "env",
        "operation_name",
        "status",
    }
)


def _parse_tag_filters(query_filter_str: str) -> List[Tuple[str, str]]:
    """Parse a Datadog metric filter string into a list of (key, value) pairs.

    Example input: ``"service:sinatra AND resource_name:GET/posts AND env:prod"``
    Returns: ``[("service", "sinatra"), ("resource_name", "GET/posts"), ("env", "prod")]``
    """
    tag_filters: List[Tuple[str, str]] = []
    if not query_filter_str:
        return tag_filters

    parts = [p.strip() for p in query_filter_str.split("AND")]
    for part in parts:
        if ":" in part:
            key, _, value = part.partition(":")
            tag_filters.append((key.strip(), value.strip()))
    return tag_filters


def _rewrite_reserved_tag(key: str, value: str) -> str:
    """Rewrite a reserved tag filter into its trace-query equivalent."""
    return f"{key}:{value}"


def _rewrite_custom_tag_with_span_info(
    key: str,
    value: str,
    span_tags: Dict[str, Any],
) -> Optional[str]:
    """Rewrite a custom (non-reserved) tag filter using span tag metadata.

    Uses fuzzy matching against sampled span tags to determine whether the
    custom tag requires the ``@`` prefix in a trace analytics query.

    Returns the rewritten filter string, or ``None`` if the tag cannot be
    resolved.
    """
    # Check if the tag exists with @ prefix in the span
    at_prefixed_key = f"@{key}"
    if at_prefixed_key in span_tags or key in span_tags:
        # Prefer the @-prefixed form if present in spans
        resolved_key = at_prefixed_key if at_prefixed_key in span_tags else key
        return f"{resolved_key}:{value}"

    # Fuzzy match: try case-insensitive lookup
    for span_key in span_tags:
        if span_key.lower() == key.lower() or span_key.lower() == at_prefixed_key.lower():
            return f"{span_key}:{value}"

    return None


def _fetch_sample_spans(
    datadog_client: Any,
    service_name: Optional[str],
    env: Optional[str],
    start: int,
    end: int,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch a sample of spans from Datadog for tag introspection.

    Returns a list of span dicts, or ``None`` if the fetch fails or returns
    no results.
    """
    try:
        spans = datadog_client.search_spans(
            service=service_name,
            env=env,
            start=start,
            end=end,
            limit=10,
        )
        return spans if spans else None
    except Exception:
        logger.debug(
            "Failed to fetch sample spans for service=%s env=%s",
            service_name,
            env,
            exc_info=True,
        )
        return None


def rewrite_datadog_metric_query_into_datadog_trace_query(
    query_filter_str: str,
    datadog_client: Any,
    start: int,
    end: int,
) -> Optional[str]:
    """Rewrite a Datadog metric query filter string into a trace analytics query.

    For reserved-word-only tag filters (``service``, ``resource_name``, ``env``,
    ``operation_name``, ``status``), the rewrite is performed directly without
    span sampling since these tags do not require ``@``-prefix disambiguation.

    For queries that include non-reserved custom tags, span sampling is used to
    determine whether the ``@`` prefix is needed.

    Parameters
    ----------
    query_filter_str:
        The metric query filter string, e.g.
        ``"service:web AND resource_name:GET/api AND env:prod"``.
    datadog_client:
        A Datadog API client instance with a ``search_spans`` method.
    start:
        Query window start (epoch seconds).
    end:
        Query window end (epoch seconds).

    Returns
    -------
    Optional[str]
        The rewritten trace query filter string, or ``None`` if the rewrite
        cannot be completed (e.g. span sampling fails for custom tags).
    """
    tag_filters = _parse_tag_filters(query_filter_str)
    if not tag_filters:
        logger.debug("No tag filters found in query: %s", query_filter_str)
        return None

    # Separate reserved-word tags from custom tags
    reserved_parts: List[str] = []
    remaining_tag_filters: List[Tuple[str, str]] = []
    service_name: Optional[str] = None
    env_value: Optional[str] = None

    for key, value in tag_filters:
        if key in DATADOG_TRACE_RESERVED_WORDS:
            reserved_parts.append(_rewrite_reserved_tag(key, value))
            if key == "service":
                service_name = value
            elif key == "env":
                env_value = value
        else:
            remaining_tag_filters.append((key, value))

    # Build the trace filter string from reserved-word tags
    datadog_trace_filter_str = " AND ".join(reserved_parts)

    # Early return: when all tag filters are reserved words, span sampling is
    # unnecessary — reserved tags don't need @-prefix disambiguation.
    if not remaining_tag_filters:
        return datadog_trace_filter_str

    # For non-reserved custom tags, we need span sampling to determine the
    # correct prefix. Fetch sample spans.
    spans = _fetch_sample_spans(
        datadog_client,
        service_name=service_name,
        env=env_value,
        start=start,
        end=end,
    )

    if not spans:
        logger.warning(
            "No spans found for service=%s env=%s in [%d, %d]; "
            "skipping trace query rewrite for query with custom tags: %s",
            service_name,
            env_value,
            start,
            end,
            query_filter_str,
        )
        return None

    # Use the first span's tags for prefix resolution
    sample_span_tags = spans[0].get("tags", {})

    custom_parts: List[str] = []
    for key, value in remaining_tag_filters:
        rewritten = _rewrite_custom_tag_with_span_info(key, value, sample_span_tags)
        if rewritten is None:
            logger.warning(
                "Could not resolve custom tag '%s' from span data; "
                "skipping trace query rewrite for: %s",
                key,
                query_filter_str,
            )
            return None
        custom_parts.append(rewritten)

    all_parts = reserved_parts + custom_parts
    return " AND ".join(all_parts)
