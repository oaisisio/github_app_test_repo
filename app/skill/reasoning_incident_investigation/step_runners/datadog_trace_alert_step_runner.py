import logging
from typing import Any, Dict, Optional

from app.skill.reasoning_incident_investigation.util.trace_action_util import (
    build_trace_alert_context,
)

logger = logging.getLogger(__name__)


class DatadogTraceAlertStepRunner:
    """Step runner that builds trace alert context from a Datadog metric query.

    When the trace query rewrite is unavailable (returns ``None``), this runner
    logs a **warning** and continues with degraded context rather than raising
    an error.  This avoids false-positive ERROR-level alerts from the Workflow
    Error Detection monitor when trace data is sparse or unavailable.
    """

    def __init__(self, datadog_client: Any) -> None:
        self.datadog_client = datadog_client

    def run(
        self,
        metric_query_filter: str,
        start: int,
        end: int,
        additional_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute the trace alert step.

        Returns a context dict that always contains at minimum the original
        metric filter, even if trace rewrite fails.
        """
        logger.info(
            "Running DatadogTraceAlertStep for metric filter: %s",
            metric_query_filter,
        )

        context = build_trace_alert_context(
            metric_query_filter=metric_query_filter,
            datadog_client=self.datadog_client,
            start=start,
            end=end,
            additional_context=additional_context,
        )

        if not context.get("trace_analysis_available"):
            logger.warning(
                "Trace analysis unavailable for metric filter: %s. "
                "Proceeding with degraded context.",
                metric_query_filter,
            )
        else:
            logger.info(
                "Trace query rewrite succeeded: %s",
                context.get("trace_query"),
            )

        return context
