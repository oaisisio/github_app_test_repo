"""Step runner for Datadog trace-based alert investigation."""

import logging
from typing import Any, Optional

from bacca_executor.pipeline.utils.trace_action_util import (
    attempt_trace_rewrite_for_action,
)

logger = logging.getLogger(__name__)


class DatadogTraceAlertStepRunner:
    """Outermost pipeline layer that drives trace query rewrites for alert
    investigation steps.

    This is the **only** layer that should log at ERROR level when a rewrite
    fails, so that a single root-cause failure produces exactly one ERROR log
    entry (and therefore one alert).
    """

    def __init__(self, service: str, env: str = "prod") -> None:
        self.service = service
        self.env = env

    def run(self, raw_tag_value: str) -> Optional[dict[str, Any]]:
        """Execute the trace alert investigation step.

        Parameters
        ----------
        raw_tag_value:
            The raw metric tag value extracted from the alert payload.

        Returns
        -------
        Optional[dict]
            A result dictionary on success, or ``None`` if the rewrite failed.
        """
        query = attempt_trace_rewrite_for_action(
            self.service,
            raw_tag_value,
            env=self.env,
        )

        if query is None:
            logger.error(
                "Trace query rewrite failed for service=%s tag_value=%s env=%s. "
                "Investigation step cannot proceed.",
                self.service,
                raw_tag_value,
                self.env,
            )
            return None

        return {
            "status": "success",
            "query": query,
        }
