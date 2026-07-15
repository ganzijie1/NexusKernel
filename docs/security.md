# Security contract

- Repository environment examples contain empty credential values only.
- Real `.env` files, virtual environments, build output, logs, and caches are
  excluded from Git and Docker contexts.
- Chat authentication is fail-closed: an unconfigured service token returns
  `503 auth_not_configured`; a missing or wrong token returns `401 unauthorized`.
- AI SDK credentials are passed explicitly. Runtime code does not mutate global
  environment variables and does not patch LiteLLM private APIs.
- Logs, health data, and HTTP errors expose stable codes rather than SDK
  exceptions, Prompts, raw model responses, Authorization values, or credential
  URLs.
- `scripts/security_scan.py` checks secret-shaped keys, credential URLs, private
  addresses, real environment files, and Docker context exclusions.

Credential rotation for any source project is operational work outside this
template and should be handled independently.
