"""
Shared test fixtures and configuration for the Datadog trace investigation
pipeline test suite.

This module automatically loads environment variables from .env.test at the
repository root before any tests run, ensuring that all required configuration
values (API keys, service names, thresholds, etc.) are available as stubs.
"""

import os
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Locate the repo root relative to this file (tests/conftest.py -> ..)
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_TEST_FILE = REPO_ROOT / ".env.test"


def _load_env_file(filepath: pathlib.Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file, skipping comments and blank lines."""
    env_vars: dict[str, str] = {}
    if not filepath.exists():
        return env_vars
    with open(filepath, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env_vars[key.strip()] = value.strip()
    return env_vars


@pytest.fixture(autouse=True, scope="session")
def load_test_env():
    """Load .env.test variables into ``os.environ`` for the test session.

    Existing environment variables are NOT overwritten so that CI or local
    overrides still take precedence.
    """
    env_vars = _load_env_file(ENV_TEST_FILE)
    for key, value in env_vars.items():
        os.environ.setdefault(key, value)
    yield
    # No teardown needed — test process exits after the session.


@pytest.fixture()
def dd_api_key():
    """Convenience fixture returning the Datadog API key."""
    return os.environ.get("DD_API_KEY", "")


@pytest.fixture()
def dd_app_key():
    """Convenience fixture returning the Datadog App key."""
    return os.environ.get("DD_APP_KEY", "")


@pytest.fixture()
def dd_service_name():
    """Convenience fixture returning the configured service name."""
    return os.environ.get("DD_SERVICE_NAME", "bacca-executor")


@pytest.fixture()
def trace_query_pattern():
    """Convenience fixture returning the trace query pattern."""
    return os.environ.get("TRACE_QUERY_PATTERN", "")
