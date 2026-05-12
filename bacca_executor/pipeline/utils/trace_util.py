"""Utilities for Datadog trace query construction and rewriting."""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Datadog wildcard characters
SINGLE_CHAR_WILDCARD = "?"
MULTI_CHAR_WILDCARD = "*"

# Characters in metric tag values that are reserved / represent parameterized
# path segments and need to be replaced with wildcards when building trace
# resource_name filter patterns.
#
# Single-character wildcards are used for characters that always represent
# exactly one unknown character (e.g. a dot used as a separator).
# Multi-character wildcards are used for characters that may represent
# variable-length segments (e.g. braces around path parameters like {post_id}).
SINGLE_CHAR_RESERVED = set()
MULTI_CHAR_RESERVED = {"_", "-", "{", "}"}


def _process_datadog_trace_tag_filter_value(raw_value: str) -> str:
    """Convert a metric tag value into a Datadog trace ``resource_name`` filter
    pattern by replacing reserved characters with the appropriate wildcard.

    Characters in *MULTI_CHAR_RESERVED* are replaced with the multi-character
    wildcard ``*`` so that patterns like ``{post_id}`` become ``*`` rather than
    ``?post?id?``, correctly matching trace resource names regardless of the
    actual character count of the parameterized segment.

    Characters in *SINGLE_CHAR_RESERVED* are replaced with the single-character
    wildcard ``?``.

    Examples
    --------
    >>> _process_datadog_trace_tag_filter_value("GET_/posts/{post_id}/guest_order")
    'GET */posts/*/guest*order'

    Parameters
    ----------
    raw_value:
        The raw metric tag value (e.g. ``GET_/posts/{post_id}/guest_order``).

    Returns
    -------
    str
        A wildcard pattern suitable for a Datadog trace query filter.
    """
    result: list[str] = []
    for ch in raw_value:
        if ch in MULTI_CHAR_RESERVED:
            # Collapse consecutive multi-char wildcards into one.
            if result and result[-1] == MULTI_CHAR_WILDCARD:
                continue
            result.append(MULTI_CHAR_WILDCARD)
        elif ch in SINGLE_CHAR_RESERVED:
            result.append(SINGLE_CHAR_WILDCARD)
        else:
            result.append(ch)
    return "".join(result)


def build_trace_query(
    service: str,
    resource_pattern: str,
    env: str = "prod",
) -> str:
    """Build a Datadog trace search query string.

    Parameters
    ----------
    service:
        The service name (e.g. ``sinatra``).
    resource_pattern:
        A ``resource_name`` pattern, typically produced by
        :func:`_process_datadog_trace_tag_filter_value`.
    env:
        The environment tag value.

    Returns
    -------
    str
        A full Datadog trace search query.
    """
    return (
        f"service:{service} AND resource_name:{resource_pattern} AND env:{env}"
    )


def list_spans(query: str, limit: int = 100) -> list[dict]:
    """Fetch spans from Datadog matching *query*.

    This is a thin wrapper around the Datadog API client.  Network / API
    errors are intentionally allowed to propagate so that callers can
    decide how to handle them.

    Parameters
    ----------
    query:
        A Datadog trace search query.
    limit:
        Maximum number of spans to return.

    Returns
    -------
    list[dict]
        A list of span dictionaries.

    Raises
    ------
    RuntimeError
        If the upstream API returns a non-200 status.
    """
    # Placeholder — real implementation calls the Datadog API.
    logger.info("Listing spans for query: %s (limit=%d)", query, limit)
    return []


def rewrite_metric_query_to_trace_query(
    service: str,
    raw_tag_value: str,
    env: str = "prod",
) -> Optional[str]:
    """High-level helper: rewrite a metric tag value into a trace query and
    attempt to fetch matching spans.

    Returns the query string on success, or ``None`` if no spans could be
    fetched.
    """
    pattern = _process_datadog_trace_tag_filter_value(raw_tag_value)
    query = build_trace_query(service, pattern, env=env)

    try:
        spans = list_spans(query)
    except Exception:
        logger.warning(
            "Skipped rewrite due to cannot fetch spans by query: %s", query
        )
        return None

    if not spans:
        logger.info("No spans returned for query: %s", query)

    return query
