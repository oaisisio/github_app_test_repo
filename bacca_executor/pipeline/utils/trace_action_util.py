"""Action-level utilities that orchestrate trace query rewrites."""

import logging
from typing import Optional

from bacca_executor.pipeline.utils.trace_util import (
    rewrite_metric_query_to_trace_query,
)

logger = logging.getLogger(__name__)


def attempt_trace_rewrite_for_action(
    service: str,
    raw_tag_value: str,
    env: str = "prod",
) -> Optional[str]:
    """Attempt to rewrite a metric query into a trace query for a given action.

    This function wraps :func:`rewrite_metric_query_to_trace_query` and adds
    action-level context to the log messages.

    Parameters
    ----------
    service:
        The service name.
    raw_tag_value:
        The raw metric tag value to convert.
    env:
        The target environment.

    Returns
    -------
    Optional[str]
        The rewritten trace query, or ``None`` on failure.
    """
    query = rewrite_metric_query_to_trace_query(service, raw_tag_value, env=env)

    if query is None:
        logger.warning(
            "Cannot fetch spans for service=%s tag_value=%s env=%s — "
            "propagating failure to step runner.",
            service,
            raw_tag_value,
            env,
        )

    return query
