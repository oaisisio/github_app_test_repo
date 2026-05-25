import typing as t

from core.context import Context
from integrations.datadog.client import DatadogClient
from integrations.datadog.trace_util import (
    extract_trace_metric_query,
)
from protobuf.core.alert_pb2 import DatadogMonitorAlertMetadata
from protobuf.storage.alert_knowledge_pb2 import AlertMetadata
from skill.reasoning_incident_investigation.common import (
    InitialInvestigationStepRunner,
    InvestigationContext,
    InvestigationStepReqRsp,
    generate_current_alert_status_str,
)
from skill.reasoning_incident_investigation.util.datadog_alert_util import (
    execute_datadog_trace_alert_investigation_action,
)
from skill.reasoning_incident_investigation.util.trace_action_util import (
    construct_trace_alert_investigation_action,
)
from util.logger.logger import Logger, logger_of_this_scope
from util.proto_util import is_proto_message_empty
from util.string_util import random_str6
from util.time_util import get_current_timestamp_sec

SUPPORTED_METADATA_TYPES = ["query_alert_metadata"]


class DatadogTraceAlertStepRunner(InitialInvestigationStepRunner):
    def should_rerun_step_for_current_status(self) -> bool:
        return True

    def get_name(self) -> str:
        return "DatadogTraceAlertInvestigation"

    def is_schedulable(
        self, investigation_ctx: InvestigationContext
    ) -> t.Tuple[bool, str]:
        if investigation_ctx.ctx.get_datadog_client() is None:
            return False, "missing datadog_client"
        if investigation_ctx.alert_accessor is None:
            return False, "missing alert_accessor"
        alert_metadata = investigation_ctx.alert_accessor.get_alert_metadata()
        if not alert_metadata or is_proto_message_empty(
            alert_metadata.datadog_metadata
        ):
            return False, "alert is not datadog-based"
        return True, ""

    def execute(
        self, context: InvestigationContext
    ) -> t.Optional[InvestigationStepReqRsp]:
        ctx: Context = context.ctx
        logger = logger_of_this_scope(prev_logger=context.logger)
        step_start_timestamp_sec: int = get_current_timestamp_sec()

        datadog_client: t.Optional[DatadogClient] = ctx.get_datadog_client()
        if not datadog_client:
            logger.error("Missing datadog client")
            return None

        alert_metadata: t.Optional[AlertMetadata] = (
            context.alert_accessor.get_alert_metadata()
        )
        if not alert_metadata:
            logger.error("Missing alert_metadata in context")
            return None
        datadog_metadata = (
            DatadogTraceAlertStepRunner.get_supported_datadog_alert_metadata(
                datadog_metadata=alert_metadata.datadog_metadata,
                prev_logger=logger,
            )
        )
        if not datadog_metadata:
            # Helper already logged why we’re skipping.
            return None

        # Consturct the trace spans action
        condense_action = construct_trace_alert_investigation_action(
            investigation_context=context,
            datadog_metric_query=datadog_metadata.query_alert_metadata.query_metric_with_groups,
        )
        if not condense_action:
            logger.warning(f"Failed to construct the trace spans action")
            return None

        # Execute the trace spans action
        step_reqrsp = execute_datadog_trace_alert_investigation_action(
            investigation_context=context,
            condense_action=condense_action,
        )
        if step_reqrsp is None:
            logger.info(
                "Skipped due to no trace spans result (no data or execution skipped)"
            )
            return None

        step_reqrsp.id = random_str6()
        step_reqrsp.name = self.get_name()
        step_reqrsp.timestamp_sec = step_start_timestamp_sec
        step_reqrsp.motivation_str = "As alert under investigation is trace based alert, carry out the step to query the alert trace spans and summarize key findings"
        step_reqrsp.is_step_success = True
        step_reqrsp.finish_timestamp_sec = get_current_timestamp_sec()

        # Add current alert status
        current_alert_status = generate_current_alert_status_str(context)
        step_reqrsp.output_summary = (
            f"{current_alert_status}\n\n{step_reqrsp.output_summary}"
        )

        return step_reqrsp

    @staticmethod
    def get_supported_datadog_alert_metadata(
        datadog_metadata: DatadogMonitorAlertMetadata,
        prev_logger: t.Optional[Logger] = None,
    ) -> t.Optional[DatadogMonitorAlertMetadata]:
        logger = logger_of_this_scope(prev_logger=prev_logger)

        if not datadog_metadata:
            logger.info("Skipped due to alert_metadata is not datadog-based")
            return None

        metadata_type = datadog_metadata.WhichOneof("metadata")
        if metadata_type not in SUPPORTED_METADATA_TYPES:
            logger.info(
                f"Skipped due to alert_metadata is not supported: {metadata_type}"
            )
            return None

        if not extract_trace_metric_query(
            datadog_metric_query=datadog_metadata.query_alert_metadata.query_metric_with_groups
        ):
            logger.info(
                f"Skipped due to not a trace metric query: {datadog_metadata.query_alert_metadata.query_metric_with_groups}"
            )
            return None

        return datadog_metadata
