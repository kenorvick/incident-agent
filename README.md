# incident-agent

An agent that investigates microservice incidents by querying telemetry through a task-shaped MCP server, scored by a scenario-based eval suite.

```
OTel Demo App (microservices, flagd fault flags, load generator)
        │  OTLP
        ▼
OTel Collector ──► file exporter (JSONL)  ──► DuckDB (local, default backend)
        │
        └────────► Honeycomb (optional live backend)

                         ┌──────────────────────────┐
                         │  MCP server (this repo)  │
                         │  tools over TelemetryBackend │
                         └─────────────▲────────────┘
                                       │ MCP
                         ┌─────────────┴────────────┐
                         │  Agent harness           │
                         │  loop · context · memory │
                         └─────────────▲────────────┘
                                       │
                CLI  ◄─────────────────┴──────────► Eval runner
```

Status: in progress.
