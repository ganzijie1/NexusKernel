# NexusKernel Starter architecture

Dependency direction is fixed: `app -> nexuskernel`. The framework never
imports an application module statically; module class paths are loaded from
strict YAML configuration.

The Kernel validates a stable dependency topology, exposes only Protocol-listed
module methods, starts Providers before Modules, rolls back required failures,
and stops resources in reverse order. Optional failures remain visible as
`degraded` health without preventing liveness.

The Gateway owns transport concerns: request and trace IDs, body limits,
timeouts, envelopes, exception mapping, low-cardinality metrics, health, and
fixed V1 routes. `app` owns authentication policy, provider selection, and the
example business module.

AI V1 supports non-streaming text, server-registered tools, and normalized token
usage. `openai_compatible` and `litellm` implement `ModelClient`; `google_adk`
implements the separate `AgentRuntime` contract with explicit user/session IDs
and TTL sessions.

Mongo, Redis Stream, Qdrant, and OpenTelemetry are optional adapters. Their
contracts are importable without installing their SDK extras and they are not
enabled by default.
