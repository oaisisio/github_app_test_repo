import dataclasses
import json
import re
import typing as t

from integrations.datadog.alert_util import logger_of_this_scope
from integrations.datadog.client import (
    DatadogClient,
)
from integrations.datadog.metric_monitor_parser import (
    DatadogFilterParser,
    build_datadog_metric_filter_str,
    find_and_parse_individual_query_part_dict,
)
from llm.llm_wrappers_func_call import LLMWrappersFuncCall
from protobuf.core.datadog_pb2 import DatadogMetricTagFilter
from util.logger.logger import Logger
from util.time_util import MINUTE_IN_MILLISECONDS, MONTH_IN_SECONDS

DAY_IN_MSEC = 24 * 60 * 60 * 1000

GET_SPAN_SERVICES_QUERY_BATCH_SIZE = 800

AGGREGATE_SPANS_GROUP_DEFAULT_LIMIT = 5000


@dataclasses.dataclass
class DatadogTraceSpanSpecialWord:
    word: str
    only_at_start: bool  # True = only match at the beginning


TRACE_SPAN_SAMPLE_COUNT = 100
PATTERN_FOR_METRIC_TAGS_IN_QUERY = re.compile(r"\{([^{}]+)\}")
DATADOG_TRACE_SPAN_SPECIAL_WORDS_TO_UPPER = [
    DatadogTraceSpanSpecialWord(word="get", only_at_start=True),
    DatadogTraceSpanSpecialWord(word="post", only_at_start=True),
    DatadogTraceSpanSpecialWord(word="put", only_at_start=True),
    DatadogTraceSpanSpecialWord(word="delete", only_at_start=True),
]
DATADOG_TRACE_SPAN_TAG_MAPPING = {
    "resource": "resource_hash",
    # http.status_class is a metric-level computed tag and does not exist as a
    # span attribute. http.status_code is the correct span attribute.
    "http.status_class": "http.status_code",
}
# Per-tag value rewrite rules applied after key mapping.
# Each entry maps a (post-mapping) tag name to a list of (pattern, replacement) pairs.
# Patterns are applied in order; the first match wins.
DATADOG_TRACE_SPAN_TAG_VALUE_MAPPING: t.Dict[str, t.List[t.Tuple[re.Pattern, str]]] = {
    # Metric status-class values like "5xx"/"4xx" become trace wildcards "5*"/"4*".
    "http.status_code": [(re.compile(r"^(\d)xx$", re.IGNORECASE), r"\1*")],
}
DATADOG_TRACE_SPAN_RESERVED_WORDS = [
    "env",
    "service",
    "cluster",
    "region",
    "operation_name",
    "resource_name",
    "status",
    "ingestion_reason",
    "trace_id",
]
DATADOG_TRACE_FUZZY_MATCH_TAGS = [
    "resource_name",
]
DATADOG_RESERVED_SPECIAL_CHARACTERS = [
    "-",
    "!",
    "&&",
    "||",
    ">",
    ">=",
    "<",
    "<=",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    '"',
    "?",
    ":",
    "#",
    "\\",
    "_",
]


def find_parent_and_touched_services(
    datadog_client: DatadogClient,
    service: str,
    from_timestamp_msec: int,
    to_timestamp_msec: int,
    prev_logger: t.Optional[Logger] = None,
) -> t.Tuple[t.List[str], t.List[str]]:
    """
    Finds parent services and all services touched within the same traces by fetching data on a daily basis.

    This function iterates through each day in the specified time range, fetching up to 1000 spans
    per day. It then aggregates all spans for processing to find parent services and all services
    involved in the same traces.

    Args:
        datadog_client: The Datadog API client.
        service: The name of the service to query.
        from_timestamp_msec: The start of the time window in milliseconds.
        to_timestamp_msec: The end of the time window in milliseconds.
        prev_logger: An optional logger instance.

    Returns:
        A tuple containing two lists:
        - parent_services (t.List[str]): A list of parent service names.
        - touched_services (t.List[str]): A list of all touched service names.
    """
    logger = logger_of_this_scope(prev_logger=prev_logger)

    all_service_spans: t.List = []
    current_ts = from_timestamp_msec

    while current_ts < to_timestamp_msec:
        day_start_ts = current_ts
        day_end_ts = current_ts + DAY_IN_MSEC
        # Ensure we don't go past the overall end timestamp
        if day_end_ts > to_timestamp_msec:
            day_end_ts = to_timestamp_msec
        logger.info(f"Fetching up to 1000 spans for date: {day_start_ts}")

        try:
            daily_spans = datadog_client.list_spans(
                query=f'service:"{service}"',
                from_ts_msec=day_start_ts,
                to_ts_msec=day_end_ts,
                should_enable_retry=True,
                prev_logger=logger,
            )
            logger.info(f"Pulled {len(daily_spans)} spans for {day_start_ts}.")
            all_service_spans.extend(daily_spans)
        except Exception as e:
            logger.error(
                f"Failed to get spans for service '{service}' on {day_start_ts}: {e}"
            )

        # Move to the next day
        current_ts += DAY_IN_MSEC

    logger.info(
        f"Finished daily fetch. Total spans collected for service '{service}': {len(all_service_spans)}"
    )

    if not all_service_spans:
        return [], []

    # Extract Parent IDs and Trace IDs from All Collected Spans
    parent_ids = set()
    trace_ids = set()
    for span in all_service_spans:
        attributes = span.get("attributes", {})
        parent_id = attributes.get("parent_id")
        trace_id = attributes.get("trace_id")

        if parent_id and parent_id != "0":
            parent_ids.add(parent_id)
        if trace_id:
            trace_ids.add(trace_id)

    parent_id_list = list(parent_ids)
    trace_id_list = list(trace_ids)

    parent_services_set = set()
    touched_services_set = set()

    # Batch Query for Parent Services
    if parent_id_list:
        logger.info(
            f"Found {len(parent_id_list)} unique parent IDs. Now fetching parent services."
        )
        parent_spans = []

        for i in range(0, len(parent_id_list), GET_SPAN_SERVICES_QUERY_BATCH_SIZE):
            batch = parent_id_list[i : i + GET_SPAN_SERVICES_QUERY_BATCH_SIZE]
            id_str = " OR ".join(batch)
            filter_str = f"span_id:({id_str})"
            try:
                spans = datadog_client.list_spans(
                    query=filter_str,
                    from_ts_msec=from_timestamp_msec,
                    to_ts_msec=to_timestamp_msec,
                    should_enable_retry=True,
                    prev_logger=logger,
                )
                parent_spans.extend(spans)
            except Exception as e:
                logger.error(
                    f"Failed to get a batch of parent spans for '{service}': {e}"
                )

        for span in parent_spans:
            service_name = span.get("attributes", {}).get("service")
            if service_name:
                parent_services_set.add(service_name)

    # Batch Query for All Touched Services
    if trace_id_list:
        logger.info(
            f"Found {len(trace_id_list)} unique trace IDs. Now fetching touched services."
        )
        for i in range(0, len(trace_id_list), GET_SPAN_SERVICES_QUERY_BATCH_SIZE):
            batch = trace_id_list[i : i + GET_SPAN_SERVICES_QUERY_BATCH_SIZE]
            id_str = " OR ".join(batch)
            filter_str = f"trace_id:({id_str})"
            try:
                services_in_traces = get_datadog_trace_services(
                    datadog_client=datadog_client,
                    query=filter_str,
                    from_timestamp_msec=from_timestamp_msec,
                    to_timestamp_msec=to_timestamp_msec,
                    prev_logger=logger,
                )
                touched_services_set.update(services_in_traces)
            except Exception as e:
                logger.error(
                    f"Failed to get a batch of touched services for '{service}': {e}"
                )

    # Return the Sorted Results
    return sorted(list(parent_services_set)), sorted(list(touched_services_set))


def get_datadog_trace_services(
    datadog_client: DatadogClient,
    query: str,
    from_timestamp_msec: int,
    to_timestamp_msec: int,
    prev_logger: t.Optional[Logger] = None,
) -> t.List[str]:
    """
    Extract all services from an aggregate response.

    Args:
        datadog_client: Datadog client
        query: query string to filter spans
        from_timestamp_msec: start timestamp in milliseconds
        to_timestamp_msec: end timestamp in milliseconds
        prev_logger: optional logger to use for logging

    Returns:
        List of service names sorted by count in descending order.
    """
    logger = logger_of_this_scope(prev_logger=prev_logger)

    response = datadog_client.get_aggregate_spans_count(
        query=query,
        from_timestamp_msec=from_timestamp_msec,
        to_timestamp_msec=to_timestamp_msec,
        group_by=[("service", AGGREGATE_SPANS_GROUP_DEFAULT_LIMIT)],
        prev_logger=logger,
    )
    if not response or "data" not in response:
        logger.warning("No valid data field in aggregate response.")
        return []

    service_counts: t.List[t.Tuple[str, int]] = []
    for item in response.get("data", []):
        try:
            service = item["attributes"]["by"]["service"]
            count = item["attributes"]["compute"]["c0"]
            service_counts.append((service, count))
        except KeyError as e:
            logger.warning(f"Missing expected field in data item: {e}")
            continue

    sorted_services = [s for s, _ in sorted(service_counts, key=lambda x: -x[1])]
    logger.info(f"Extracted {len(sorted_services)} services from response")
    return sorted_services


def extract_trace_metric_query(datadog_metric_query: str) -> t.Optional[str]:
    """
    Extract the raw query portion of a Datadog metric query that references a trace metric.

    This function parses a full Datadog metric query—potentially containing multiple
    query segments—and identifies the first segment whose metric name begins with
    ``"trace"``. Datadog trace metrics (e.g., ``trace.span.duration``,
    ``trace.flamegraph.count``) embed a query substring that specifies tag filters
    and other criteria. This helper isolates and returns that substring so it can be
    further processed or rewritten.

    The function uses
    :func:`find_and_parse_individual_query_part_dict` to decompose the metric query
    into structured components and then searches for the first trace-related entry.

    Args:
        datadog_metric_query (str):
            The full Datadog metric query string, possibly containing multiple
            individual metric query parts.

    Returns:
        Optional[str]:
            The ``query`` portion associated with the first trace metric encountered.
            Returns ``None`` if no trace metric is present.
    """
    individual_queries: t.List[t.Dict] = find_and_parse_individual_query_part_dict(
        datadog_metric_query
    )
    for individual_query in individual_queries:
        metric_name: str = individual_query.get("metric", "")
        if metric_name.startswith("trace"):
            return individual_query.get("query", "")

    return None


def rewrite_datadog_metric_query_into_datadog_trace_query(
    datadog_client: DatadogClient,
    datadog_metric_query: str,
    query_ts_msec: int,
    llm_wrapper: t.Optional[LLMWrappersFuncCall] = None,
    prev_logger: t.Optional[Logger] = None,
) -> t.Optional[str]:
    """
    Rewrite a Datadog metric query into an equivalent Datadog trace query.

    This function inspects a Datadog metric query, identifies whether it
    references a trace metric (i.e., any metric whose name begins with
    ``"trace"``), and if so, extracts and rewrites its tag filters into the
    syntax and semantics expected by Datadog trace search.

    The transformation includes:

    * Parsing the metric query to locate its metric name and tag filters.
    * Normalizing tag filter values using
      :func:`process_datadog_trace_tag_filter_value`, ensuring uppercase
      special words and replaced reserved characters.
    * Separating tag filters into:
        - **Reserved-word tag filters** (tags whose names are reserved span
          attributes such as ``resource_name``).
        - **Non-reserved tag filters**, which may need a ``"@"`` prefix if they
          refer to Datadog "custom attributes".
    * Sampling a small set of spans using the Datadog trace API to determine
      whether non-reserved tags appear in ``attributes.custom`` or top-level
      ``attributes``. Tags discovered under ``attributes.custom`` are rewritten
      as ``@tag``.
    * When span sampling returns no results and ``llm_wrapper`` is provided,
      a fuzzy-match fallback is attempted: actual tag values are queried via
      ``aggregate_spans``, and the LLM selects the best-matching candidates
      (accounting for wildcards, special-character replacements, and casing
      differences). The span query is then retried with corrected tag values.
    * Finally assembling all rewritten tag filters into a valid Datadog trace
      query string.

    Args:
        datadog_client (DatadogClient):
            A client capable of querying Datadog spans using ``list_spans``.
        datadog_metric_query (str):
            The raw Datadog metric query string to analyze and convert.
        query_ts_msec (int):
            A millisecond timestamp used to define the 30-minute window for
            sampling spans.
        llm_wrapper (Optional[LLMWrappersFuncCall]):
            Optional LLM wrapper used for fuzzy-matching tag values when
            span sampling returns no results. When provided, a fallback
            path queries actual tag values via ``aggregate_spans`` and uses
            the LLM to pick the closest match before retrying.
        prev_logger (Optional[Logger]):
            Optional logger instance to reuse; if omitted, a scoped logger is
            created for this function.

    Returns:
        Optional[str]:
            A rewritten Datadog trace query composed of transformed tag filters.
            Returns ``None`` if:
            * the metric query does not reference a trace metric,
            * tag filters cannot be extracted,
            * span sampling fails.
    """
    logger = logger_of_this_scope(prev_logger=prev_logger)

    # Check if it is a trace metric
    metric_query_to_rewrite: t.Optional[str] = extract_trace_metric_query(
        datadog_metric_query=datadog_metric_query
    )
    if not metric_query_to_rewrite:
        logger.info("Skipped rewrite due to not a trace metric query")
        return None

    # Extract existing tag filters from datadog_metric_query
    match = PATTERN_FOR_METRIC_TAGS_IN_QUERY.search(datadog_metric_query)
    tag_filters_str: str = ""
    if match:
        tag_filters_str = match.group(1)
    else:
        logger.info(
            "Skipped rewrite due to cannot extract tag filters from metric query"
        )
        return None

    # Split the tag filters string into individual tags filters
    tag_filters: t.List[DatadogMetricTagFilter] = DatadogFilterParser().parse_filter(
        tag_filters_str
    )

    # Process each tag filter and separate them into reserved_word_tag_filters and remaining_tag_filters
    reserved_word_tag_filters: t.List[DatadogMetricTagFilter] = []
    remaining_tag_filters: t.List[DatadogMetricTagFilter] = []
    for tag_filter in tag_filters:
        if tag_filter.tag in DATADOG_TRACE_SPAN_TAG_MAPPING.keys():
            tag_filter.tag = DATADOG_TRACE_SPAN_TAG_MAPPING[tag_filter.tag]
        tag_filter.equal_values[:] = [
            _remap_tag_value(tag_filter.tag, _process_datadog_trace_tag_filter_value(v))
            for v in tag_filter.equal_values
        ]
        tag_filter.non_equal_values[:] = [
            _remap_tag_value(tag_filter.tag, _process_datadog_trace_tag_filter_value(v))
            for v in tag_filter.non_equal_values
        ]
        if tag_filter.tag in DATADOG_TRACE_SPAN_RESERVED_WORDS:
            reserved_word_tag_filters.append(tag_filter)
        else:
            remaining_tag_filters.append(tag_filter)

    # If not non-reserved tag filter, early return
    datadog_trace_filter_str = build_datadog_metric_filter_str(
        reserved_word_tag_filters
    )

    # Samples a small set of spans to decide whether a non-reserved tag requires `@`
    spans = datadog_client.list_spans(
        query=datadog_trace_filter_str,
        from_ts_msec=query_ts_msec - MONTH_IN_SECONDS * 1000,
        to_ts_msec=query_ts_msec,
        limit=TRACE_SPAN_SAMPLE_COUNT,
        total_limit=TRACE_SPAN_SAMPLE_COUNT,
        prev_logger=logger,
    )
    logger.info(
        f"Initial sampling with query {datadog_trace_filter_str} -> {len(spans)} spans"
    )
    if not spans and llm_wrapper:
        fuzzy_result = _try_fuzzy_match_reserved_tags(
            datadog_client=datadog_client,
            llm_wrapper=llm_wrapper,
            reserved_word_tag_filters=reserved_word_tag_filters,
            from_ts_msec=query_ts_msec - 30 * MINUTE_IN_MILLISECONDS,
            to_ts_msec=query_ts_msec,
            prev_logger=logger,
        )
        if fuzzy_result is not None:
            reserved_word_tag_filters = fuzzy_result
            datadog_trace_filter_str = build_datadog_metric_filter_str(
                reserved_word_tag_filters
            )
            spans = datadog_client.list_spans(
                query=datadog_trace_filter_str,
                from_ts_msec=query_ts_msec - MONTH_IN_SECONDS * 1000,
                to_ts_msec=query_ts_msec,
                limit=TRACE_SPAN_SAMPLE_COUNT,
                total_limit=TRACE_SPAN_SAMPLE_COUNT,
                prev_logger=logger,
            )
            logger.info(
                f"Second sampling with fuzzy query {datadog_trace_filter_str} -> {len(spans)} spans"
            )
    if not spans:
        logger.warning(
            f"Skipped rewrite due to cannot fetch spans by query {datadog_trace_filter_str}"
        )
        return None

    # Construct the trace query
    trace_query_tag_filters = reserved_word_tag_filters
    for tag_filter in remaining_tag_filters:
        tag_parts = tag_filter.tag.split(".")
        for span in spans:
            span_tags = span.get("attributes", {}).get("tags", [])
            if tag_parts[0] in span.get("attributes", {}).get("custom", {}):
                tag_filter.tag = f"@{tag_filter.tag}"
                break
            if tag_parts[0] in span.get("attributes", {}):
                break
            if tag_filter.tag in [tag.split(":", 1)[0] for tag in span_tags]:
                break
        trace_query_tag_filters.append(tag_filter)

    return build_datadog_metric_filter_str(trace_query_tag_filters)


def _remap_tag_value(mapped_tag: str, value: str) -> str:
    """Apply value remapping rules defined in DATADOG_TRACE_SPAN_TAG_VALUE_MAPPING.

    For each rule associated with ``mapped_tag``, the first matching pattern is
    used to rewrite the value. If no rule matches, the value is returned as-is.
    """
    for pattern, replacement in DATADOG_TRACE_SPAN_TAG_VALUE_MAPPING.get(
        mapped_tag, []
    ):
        new_value = pattern.sub(replacement, value)
        if new_value != value:
            return new_value
    return value


def _process_datadog_trace_tag_filter_value(value: str) -> str:
    """
    Process a Datadog trace tag filter value.

    This helper function performs two transformations on a tag filter value
    before it is used in a Datadog trace query:

    1. **Uppercase special whole-words** defined in
       `DATADOG_TRACE_SPAN_SPECIAL_WORDS_TO_UPPER`.
       These words are only uppercased when matched as whole tokens
       (i.e., not embedded inside a longer alphanumeric string).
       A word may be restricted to matching only at the beginning of the value.

    2. **Replace reserved special characters with `"?"`**, according to
       `DATADOG_RESERVED_SPECIAL_CHARACTERS`.

    Args:
        value (str):
            The raw tag filter value extracted from a metric query.

    Returns:
        str:
            A normalized value safe for insertion into a Datadog trace query.
    """
    # Step 1: uppercase special words
    for special_word in DATADOG_TRACE_SPAN_SPECIAL_WORDS_TO_UPPER:
        if special_word.only_at_start:
            pattern = rf"(?i)^(?<![A-Za-z0-9]){special_word.word}(?![A-Za-z0-9])"
        else:
            pattern = rf"(?i)(?<![A-Za-z0-9]){special_word.word}(?![A-Za-z0-9])"

        value = re.sub(pattern, special_word.word.upper(), value)

    # Step 2: replace reserved special characters
    for reserved_char in DATADOG_RESERVED_SPECIAL_CHARACTERS:
        value = value.replace(reserved_char, "?")

    return value


def _try_fuzzy_match_reserved_tags(
    datadog_client: DatadogClient,
    llm_wrapper: LLMWrappersFuncCall,
    reserved_word_tag_filters: t.List[DatadogMetricTagFilter],
    from_ts_msec: int,
    to_ts_msec: int,
    prev_logger: t.Optional[Logger] = None,
) -> t.Optional[t.List[DatadogMetricTagFilter]]:
    """Try fuzzy matching reserved tag values against actual trace span values.

    When the initial span query returns no results, this function queries Datadog
    for actual tag values via ``get_aggregate_spans_count``, then uses an LLM to
    pick the closest match for each target value.

    Returns an updated copy of *reserved_word_tag_filters* with corrected values,
    or ``None`` if fuzzy matching is not applicable or fails.
    """
    logger = logger_of_this_scope(prev_logger=prev_logger)

    # Identify filters eligible for fuzzy matching
    fuzzy_filters: t.List[DatadogMetricTagFilter] = []
    base_filters: t.List[DatadogMetricTagFilter] = []
    for tag_filter in reserved_word_tag_filters:
        if tag_filter.tag in DATADOG_TRACE_FUZZY_MATCH_TAGS:
            fuzzy_filters.append(tag_filter)
        else:
            base_filters.append(tag_filter)

    if not fuzzy_filters:
        return None

    base_query = build_datadog_metric_filter_str(base_filters)

    updated_filters = list(base_filters)
    for tag_filter in fuzzy_filters:
        if not tag_filter.equal_values:
            updated_filters.append(tag_filter)
            continue

        # Query actual values for this tag
        response = datadog_client.get_aggregate_spans_count(
            query=base_query,
            from_timestamp_msec=from_ts_msec,
            to_timestamp_msec=to_ts_msec,
            group_by=[(tag_filter.tag, AGGREGATE_SPANS_GROUP_DEFAULT_LIMIT)],
            prev_logger=logger,
        )
        if not response or "data" not in response:
            logger.warning(f"Fuzzy match: no aggregate data for tag {tag_filter.tag}")
            return None

        candidate_values: t.List[str] = []
        for item in response.get("data", []):
            try:
                val = item["attributes"]["by"][tag_filter.tag]
                candidate_values.append(val)
            except KeyError:
                continue

        if not candidate_values:
            logger.warning(f"Fuzzy match: no candidate values for tag {tag_filter.tag}")
            return None

        # Build a single LLM request for this tag
        request = {
            "tag": tag_filter.tag,
            "target_values": ", ".join(tag_filter.equal_values),
            "candidates": candidate_values,
        }

        llm_response = llm_wrapper.call_func(
            "FuzzyMatchDatadogTraceTagValues",
            {"fuzzy_match_requests": json.dumps(request, indent=2)},
            prev_logger=prev_logger,
        )
        if not llm_response:
            logger.warning(
                f"Fuzzy match: LLM returned no response for tag {tag_filter.tag}"
            )
            return None

        try:
            parsed = json.loads(llm_response)
            matched_values = [
                v
                for v in parsed.get("matched_values", [])
                if isinstance(v, str) and v.strip()
            ]
        except (json.JSONDecodeError, AttributeError):
            logger.warning(
                f"Fuzzy match: failed to parse LLM response for tag {tag_filter.tag}"
            )
            return None

        if not matched_values:
            logger.warning(
                f"Fuzzy match: no candidate matched any target value for "
                f"tag {tag_filter.tag}"
            )
            return None

        # Create updated filter with matched values
        new_filter = DatadogMetricTagFilter()
        new_filter.CopyFrom(tag_filter)

        def _replace_special_char(value) -> str:
            for reserved_char in DATADOG_RESERVED_SPECIAL_CHARACTERS + [" "]:
                value = value.replace(reserved_char, "?")
            return value

        new_filter.equal_values[:] = [
            _replace_special_char(value) for value in matched_values
        ]
        logger.info(
            f"Fuzzy match: rewrote {tag_filter.tag} values "
            f"{list(tag_filter.equal_values)} -> {list(new_filter.equal_values)}"
        )
        updated_filters.append(new_filter)

    return updated_filters
