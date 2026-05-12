# Welcome to the Repository
This is an initial commit by the GitHub App.

## Local Test Setup

To run the Datadog trace utility tests locally:

1. Copy the example environment file:
   ```bash
   cp .env.test.example .env.test
   ```
2. Edit `.env.test` and replace placeholder values with real credentials where needed.
3. Run the tests:
   ```bash
   pytest tests/ -k "trace"
   ```

> **Note:** `.env.test` is git-ignored — never commit files containing real secrets.