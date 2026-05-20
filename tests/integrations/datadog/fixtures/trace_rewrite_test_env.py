"""Test environment fixtures for trace query rewrite logic.

These fixtures reproduce the scenario from incident c6d429775f2e4822
where wildcard pattern generation produced non-matching patterns for
Sinatra service resource names.
"""

# Sample metric tag values as they appear in Datadog metric monitors
METRIC_TAG_VALUES = [
    "GET_/posts/_post_id/guest-order",
    "POST_/users/_user_id/settings",
    "DELETE_/items/_item_id",
]

# Corresponding actual trace resource_name values in Datadog APM
EXPECTED_TRACE_RESOURCE_NAMES = [
    "GET /posts/{post_id}/guest-order",
    "POST /users/{user_id}/settings",
    "DELETE /items/{item_id}",
]

# Current (buggy) wildcard patterns produced by _process_datadog_trace_tag_filter_value
CURRENT_WILDCARD_PATTERNS = [
    "GET?/posts/?post?id/guest?order",
    "POST?/users/?user?id/settings",
    "DELETE?/items/?item?id",
]

# Expected (fixed) wildcard patterns that should use * for multi-char matches
FIXED_WILDCARD_PATTERNS = [
    "GET*/posts/*post*id*/guest-order",
    "POST*/users/*user*id*/settings",
    "DELETE*/items/*item*id*",
]

# Test environment configuration
TEST_ENV_CONFIG = {
    "service": "sinatra",
    "env": "prod",
    "customer_id": "92646e48-3ca7-4442-9ca4-61af3354d3fb",
}

# Mock span data for list_spans responses
MOCK_SPANS = [
    {
        "service": "sinatra",
        "resource_name": "GET /posts/{post_id}/guest-order",
        "env": "prod",
        "duration": 150000000,
        "span_id": "test-span-001",
    },
]
