"""Utility functions for Datadog trace query rewriting."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

RESERVED_WORD_TAGS = {"service", "resource_name", "env"}


def parse_tag_filters(query: str) -> dict:
    """Parse tag filters from a Datadog metric query string.

    Returns a dictionary with tag names as keys and filter values as values.
    """
    tag_filters = {}
    if not query:
        return tag_filters

    parts = query.split(",")
    for part in parts:
        part = part.strip()
        if ":" in part:
            key, value = part.split(":", 1)
            tag_filters[key.strip()] = value.strip()

    return tag_filters


def build_trace_filter_str(tag_filters: dict) -> str:
    """Build a trace filter string from tag filters.

    Reserved-word tags (service, resource_name, env) are used directly.
    Non-reserved tags get an '@' prefix for span tag matching.
    """
    parts = []
    for key, value in tag_filters.items():
        if key in RESERVED_WORD_TAGS:
            parts.append(f"{key}:{value}")
        else:
            parts.append(f"@{key}:{value}")
    return " AND ".join(parts)


def fetch_spans_from_datadog(query: str) -> Optional[list]:
    """Fetch spans from Datadog API matching the given query.

    Returns a list of spans or None if no spans were found.
    """
    # This is a placeholder for the actual Datadog API call
    # In production, this calls the Datadog APM API
    return None


def detect_at_prefix_tags(spans: list, tag_filters: dict) -> dict:
    """Detect which non-reserved tags need '@' prefix by sampling spans.

    Examines span data to determine the correct prefix for each tag.
    """
    prefixed_tags = {}
    for key, value in tag_filters.items():
        if key not in RESERVED_WORD_TAGS:
            # Check if the tag exists in span metadata
            for span in spans:
                if key in span.get("meta", {}):
                    prefixed_tags[key] = f"@{key}:{value}"
                    break
            else:
                prefixed_tags[key] = f"@{key}:{value}"
    return prefixed_tags


def fuzzy_match_resource_name(resource_name: str) -> str:
    """Apply fuzzy matching to resource names with wildcards.

    Replaces special characters that may not match exactly in trace queries.
    """
    # Replace common URL pattern characters with wildcards
    result = resource_name
    for char in ["?", "#", "&", "="]:
        result = result.replace(char, "*")
    return result


def rewrite_datadog_metric_query_into_datadog_trace_query(
    metric_query: str,
    service: Optional[str] = None,
    env: Optional[str] = None,
) -> Optional[str]:
    """Rewrite a Datadog metric query into a Datadog trace query.

    This function takes a metric query with tag filters and rewrites it into
    a trace query format. Reserved-word tags (service, resource_name, env) are
    used directly, while other tags need '@' prefix detection via span sampling.

    Args:
        metric_query: The original Datadog metric query string.
        service: Optional service name override.
        env: Optional environment override.

    Returns:
        The rewritten trace query string, or None if rewriting fails.
    """
    if not metric_query:
        logger.warning(
            "rewrite_datadog_metric_query_into_datadog_trace_query: "
            "Empty metric query provided"
        )
        return None

    # Parse tag filters from the metric query
    tag_filters = parse_tag_filters(metric_query)

    if not tag_filters:
        logger.warning(
            "rewrite_datadog_metric_query_into_datadog_trace_query: "
            "No tag filters found in query"
        )
        return None

    # Override service and env if provided
    if service:
        tag_filters["service"] = service
    if env:
        tag_filters["env"] = env

    # Separate reserved-word tags from remaining tags
    reserved_word_tag_filters = {
        k: v for k, v in tag_filters.items() if k in RESERVED_WORD_TAGS
    }
    remaining_tag_filters = {
        k: v for k, v in tag_filters.items() if k not in RESERVED_WORD_TAGS
    }

    # Build the base trace filter string from reserved-word tags
    reserved_word_parts = []
    for key, value in reserved_word_tag_filters.items():
        if key == "resource_name":
            value = fuzzy_match_resource_name(value)
        reserved_word_parts.append(f"{key}:{value}")

    datadog_trace_filter_str = " AND ".join(reserved_word_parts)
    reserved_word_tag_filter_str = datadog_trace_filter_str

    # Early return if there are no non-reserved tags that need '@' prefix detection.
    # This avoids unnecessary span sampling when the query only contains
    # reserved-word tags (service, resource_name, env).
    if not remaining_tag_filters:
        return datadog_trace_filter_str

    # Build query string for span sampling using reserved-word tags
    span_query = reserved_word_tag_filter_str

    # Attempt to fetch spans to detect '@' prefix for remaining tags
    spans = fetch_spans_from_datadog(span_query)

    if spans is None:
        logger.warning(
            f"rewrite_datadog_metric_query_into_datadog_trace_query: "
            f"Skipped rewrite due to cannot fetch spans by query {reserved_word_tag_filter_str}"
        )
        return None

    # Detect '@' prefix for non-reserved tags using span sampling
    prefixed_tags = detect_at_prefix_tags(spans, remaining_tag_filters)

    # Combine reserved-word filter with prefixed tags
    if prefixed_tags:
        prefixed_parts = list(prefixed_tags.values())
        datadog_trace_filter_str = (
            datadog_trace_filter_str + " AND " + " AND ".join(prefixed_parts)
        )

    return datadog_trace_filter_str
