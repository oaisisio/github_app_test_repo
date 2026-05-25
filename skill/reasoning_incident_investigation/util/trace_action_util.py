import json
import typing as t

from assistant.toolbox.datadog_trace_query_toolkit import DatadogTraceQueryToolkit
from assistant.toolbox.trace_query_toolkit import (
    SHOULD_QUERY_ERROR_NORMAL_SPANS_SEPARATELY,
)
from integrations.datadog.trace_util import (
    rewrite_datadog_metric_query_into_datadog_trace_query,
)
from protobuf.storage.investigation_action_pb2 import (
    CondenseInvestigationAction,
    InvestigationTimeRange,
    TraceSpansInvestigationAction,
)
from protobuf.storage.investigation_action_visualization_pb2 import (
    InvestigationActionVisualization,
)
from protobuf.storage.knowledge_graph_pb2 import KnowledgePlatformType
from skill.reasoning_incident_investigation.common import (
    ActionSummary,
    InvestigationContext,
)
from skill.reasoning_incident_investigation.util.action_render_util import (
    render_action_header,
    render_resource_entry,
)
from skill.reasoning_incident_investigation.util.action_util import (
    render_generic_action_result,
)
from util.investigation_action_util import (
    ActionReqRsp,
    get_current_trace_spans_range_reqrsp,
)
from util.time_util import DAY_IN_SECONDS, HOUR_IN_SECONDS
from util.string_util import prefixed_hash_id

DEFAULT_MAX_NUM_CURRENT_TRACE_SPANS_FOR_DETAILED_TRACE_PROCESS = 20
DEFAULT_MAX_NUM_CURRENT_TRACE_SPANS_TO_KEEP = 10


def construct_trace_alert_investigation_action(
    investigation_context: InvestigationContext,
    datadog_metric_query: str,
) -> t.Optional[CondenseInvestigationAction]:
    """
    Build a Datadog trace spans investigation action from a trace metric alert query.

    Args:
        investigation_context: Investigation context providing services, config, and timestamps.
        datadog_metric_query: The original Datadog metric query from the alert to rewrite into a trace query.

    Returns:
        A populated `CondenseInvestigationAction` when the trace query can be constructed, otherwise None.
    """
    ctx = investigation_context.ctx
    logger = ctx.get_logger().this_scope()

    datadog_client = ctx.get_datadog_client()
    if not datadog_client:
        logger.error("Missing datadog client")
        return None

    # Rewrite the metric query to trace query
    # Using the query_metric_with_groups instead of query_raw since it includes group tag filters (if any)
    logger.info(f"Begin rewriting datadog metric query into trace query")
    llm_wrappers_func_call = ctx.get_llm_wrappers_func_call()
    datadog_trace_query: t.Optional[str] = (
        rewrite_datadog_metric_query_into_datadog_trace_query(
            datadog_client=datadog_client,
            datadog_metric_query=datadog_metric_query,
            query_ts_msec=investigation_context.investigation_session.query_timestamp_sec
            * 1000,
            llm_wrapper=llm_wrappers_func_call,
            prev_logger=logger,
        )
    )
    if not datadog_trace_query:
        logger.warning(
            f"Failed to rewrite datadog metric query into trace query: {datadog_metric_query}"
        )
        return None
    logger.info(
        f"Finished rewriting datadog metric query into trace query: {datadog_metric_query}->{datadog_trace_query}"
    )

    # Construct the trace spans action
    logger.info(f"Begin constructing the trace spans action")
    trace_spans_action = TraceSpansInvestigationAction(
        name=f"trace_spans_query",
        description=f"trace spans query for metric query: {datadog_metric_query}",
        query_string=datadog_trace_query,
        platform=KnowledgePlatformType.Enum.DATADOG,
        time_ranges=[
            InvestigationTimeRange(start_offset_sec=HOUR_IN_SECONDS, end_offset_sec=0),
            InvestigationTimeRange(
                start_offset_sec=DAY_IN_SECONDS + HOUR_IN_SECONDS,
                end_offset_sec=DAY_IN_SECONDS,
            ),
        ],
        additional_query_params={SHOULD_QUERY_ERROR_NORMAL_SPANS_SEPARATELY: "True"},
    )
    visualization = InvestigationActionVisualization()
    visualization.parameter.datadog.host = (
        datadog_client.get_datadog_visualization_host()
    )
    condense_action = CondenseInvestigationAction(
        id=prefixed_hash_id(
            prefix="MANUAL_datadog_trace_spans_from_metric",
            hash_suffix_parts=[
                datadog_trace_query,
                datadog_metric_query,
            ],
        ),
        trace_spans_investigation_action=trace_spans_action,
        visualizations=[visualization],
    )
    logger.info(f"Finished constructing the trace spans action: {condense_action}")

    return condense_action


def parse_trace_action_result(
    action_reqrsp: ActionReqRsp,
) -> ActionSummary:
    """
    Convert a trace action result into a structured `ActionSummary`.

    Args:
        action_reqrsp: Action wrapper containing execution metadata and span samples.

    Returns:
        `ActionSummary` with header/query/span-statistics for humans plus the
        richer `llm_input` payload for LLM calls.
    """
    trace_spans_action_reqrsp = action_reqrsp.trace_spans_action_reqrsp
    current_trace_spans_range_reqrsp = get_current_trace_spans_range_reqrsp(
        action_reqrsp
    )
    recent_trace_spans = current_trace_spans_range_reqrsp.spans

    action_header = render_action_header(action_reqrsp=action_reqrsp)
    query_string = trace_spans_action_reqrsp.query_string
    action_block = f"{action_header}\nQuery String: {query_string}"

    span_stats_section: str = (
        f"[SPAN STATISTICS]\n{render_generic_action_result(action_reqrsp, include_header=False)}"
    )
    raw_spans_section: str = ""
    schema_section: str = ""
    resources: t.List[str] = []

    if (
        len(recent_trace_spans)
        < DEFAULT_MAX_NUM_CURRENT_TRACE_SPANS_FOR_DETAILED_TRACE_PROCESS
    ):
        # Case 1: if there are small number of trace spans, we provide span statistics and detailed trace spans to llm for analysis
        raw_spans = "\n---\n".join(
            [
                DatadogTraceQueryToolkit.render_span(trace_span)
                for trace_span in sorted(
                    recent_trace_spans,
                    key=lambda x: x.end_timestamp_msec,
                    reverse=True,
                )[:DEFAULT_MAX_NUM_CURRENT_TRACE_SPANS_TO_KEEP]
            ]
        )
        raw_spans_section = f"[RAW SPANS]\n{raw_spans}"
    else:
        # Case 2: we use span statistics and schema if there are large number of trace spans
        normal_spans, error_spans = DatadogTraceQueryToolkit.partition_spans_by_status(
            recent_trace_spans
        )
        normal_span_schema = DatadogTraceQueryToolkit.generate_span_field_schema(
            normal_spans
        )
        error_span_schema = DatadogTraceQueryToolkit.generate_span_field_schema(
            error_spans
        )
        schema_section = (
            f"[SPAN SCHEMA]\n"
            f"**Normal Span Schema:**\n```\n{json.dumps(normal_span_schema, indent=2)}\n```\n"
            f"**Error Span Schema:**\n```\n{json.dumps(error_span_schema, indent=2)}\n```\n"
        )
        resources = [render_resource_entry(action_reqrsp, resource_type="TRACE_SPANS")]

    # Construct output summary sections.
    output_summary_sections: t.List[str] = [
        action_block,
        span_stats_section,
    ]

    # Construct LLM input sections.
    llm_input_sections: t.List[str] = [action_block, span_stats_section]
    if raw_spans_section:
        llm_input_sections.append(raw_spans_section)
    elif schema_section:
        llm_input_sections.append(schema_section)

    # Construct detailed result sections.
    detailed_result_sections: t.List[str] = [action_block, span_stats_section]
    if raw_spans_section:
        detailed_result_sections.append(raw_spans_section)
    elif schema_section:
        detailed_result_sections.append(schema_section)

    return ActionSummary(
        output_summary=output_summary_sections,
        llm_input=llm_input_sections,
        detailed_results=detailed_result_sections,
        resources=resources,
    )
