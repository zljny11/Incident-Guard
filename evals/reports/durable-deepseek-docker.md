# Durable DeepSeek + MCP + Docker End-to-End Validation

Date: 2026-09-04  
Model: `deepseek-v4-flash`  
Scenario: `bad_deployment`  
Run: `agent-deepseek-docker-001`

## Result

| Check | Result |
| --- | --- |
| Final run status | `COMPLETED` |
| State before approval | payment-service `v2`; rollback `REQUESTED` |
| Mutation before approval | 0 |
| Human approval | persisted at event sequence 27 |
| State after cross-process resume | payment-service and shop-api `v1` / `healthy` |
| Executed recovery | one `rollback_service`, followed by `verify_recovery` |
| Durable events | 40 |
| Runtime invariant findings | 0 |
| Model tool calls | 7 |
| Token usage | 9,799 input + 499 output = 10,298 total |

## Executed Path

```text
DeepSeek
-> EventDrivenAgentRuntime
-> five read-only investigation tools
-> rollback_service proposed
-> approval.requested persisted; process exits in WAITING_APPROVAL
-> operator approval persisted by a separate CLI process
-> a new CLI process replays SQLite events
-> Tool Pipeline validates policy and durable approval
-> MCP stdio calls DockerIncidentToolProvider
-> payment-service v2 is recreated as v1
-> DeepSeek requests verify_recovery
-> payment-service and shop-api are healthy
-> run.completed
```

The five investigation calls were `query_service_health`, `query_metrics`,
`query_logs`, `get_recent_deployments`, and `read_runbook`. The approval was made
by the operator, not auto-approved by the evaluation harness.

## Recovery Semantics

This run demonstrates process-independent pause/resume at a safe approval boundary.
Automated crash-injection tests additionally verify that completed calls are not
repeated after restart, started read calls are safely retried, and a crash after a
mutation's durable `tool.started` marker becomes `FAILED_UNCERTAIN` instead of an
automatic retry.

This is one controlled local Docker validation, not a production reliability claim.
