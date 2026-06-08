#!/usr/bin/env bash
# Test environment setup for local development
# Usage: source test_env_setup.sh

export TEST_DB_URL="postgresql://localhost:5432/test_db"
export LOG_LEVEL="debug"
export APP_ENV="test"
