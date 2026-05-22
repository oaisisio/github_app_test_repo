import logging

logger = logging.getLogger(__name__)


def _process_datadog_trace_tag_filter_value(value):
    """
    Process a tag filter value, replacing path parameter placeholders
    (e.g. {post_id}, :param, <param>) with the `?` wildcard character
    for use in Datadog trace queries.
    """
    import re
    # Replace {param}, :param, and <param> style path parameters with ?
    value = re.sub(r'\{[^}]+\}', '?', value)
    value = re.sub(r':[a-zA-Z_][a-zA-Z0-9_]*', '?', value)
    value = re.sub(r'<[^>]+>', '?', value)
    return value


def rewrite_datadog_metric_query_into_datadog_trace_query(metric_query, fetch_spans_fn):
    """
    Rewrite a Datadog metric query into a Datadog trace query.

    When the resource_name contains path parameters (e.g. GET /posts/{post_id}/guest_order),
    the ?-substitution performed by _process_datadog_trace_tag_filter_value() may produce
    patterns that don't match actual span resource names (which use :param or <param> notation).
    In that case, fetch_spans_fn returns 0 spans, which is a known limitation handled by a
    fuzzy match fallback added elsewhere. This condition is not an actionable failure, so it
    is logged at WARNING level rather than ERROR.

    Returns the rewritten trace query string, or None if spans cannot be fetched.
    """
    # Build the trace query from the metric query
    trace_query = _build_trace_query(metric_query)

    spans = fetch_spans_fn(trace_query)

    if not spans:
        logger.warning(
            "Skipped rewrite due to cannot fetch spans by query %s",
            trace_query,
        )
        return None

    return trace_query


def _build_trace_query(metric_query):
    """
    Build a Datadog trace query string from a metric query, applying
    wildcard substitution for path parameters in resource names.
    """
    # Extract resource_name filter value if present and apply wildcard substitution
    import re
    def replace_resource(match):
        raw_value = match.group(1)
        processed = _process_datadog_trace_tag_filter_value(raw_value)
        return 'resource_name:"{}"'.format(processed)

    trace_query = re.sub(
        r'resource_name:"([^"]*)"',
        replace_resource,
        metric_query,
    )
    return trace_query
