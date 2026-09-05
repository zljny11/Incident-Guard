# ADR 0001: Keep the native event-driven Runtime and use LangGraph as a baseline

- Status: Accepted
- Scope: `bad_deployment` comparison in C6-T5

## Context

Incident Guard needs durable replay, operator approval, fail-closed mutation recovery,
tool policy, live events, and evaluator-visible invariants. LangGraph provides graph
state, checkpoint persistence, conditional routing, and resumable interrupts. Its
documented interrupt model restarts the interrupted node on resume, so side effects
before an interrupt must be idempotent.

The native Runtime instead stores domain events (`tool.requested`,
`approval.decided`, `tool.started`, `tool.completed`) in an append-only SQLite log.
It distinguishes retryable reads from an uncertain mutation that started without a
durable completion event.

## Decision

Keep the native Runtime as the product implementation. Maintain a minimal LangGraph
baseline to verify that the same bad-deployment flow can be represented with six
nodes, explicit state, a conditional approval route, and checkpointed resume.

The baseline still sends every tool through Incident Guard's existing Tool Registry,
Schema, Policy, approval, and named-lane boundary. LangGraph coordinates the graph;
it does not replace the safety boundary.

## Comparison

| Concern | Native Runtime | Minimal LangGraph baseline |
| --- | --- | --- |
| State | Derived from append-only domain events | Mutable typed graph state plus checkpoints |
| Recovery | Replays events; completed effects are not repeated | Resumes from a checkpoint/super-step |
| Approval | Durable request/decision events and fail-closed execution | `interrupt()` plus `Command(resume=...)` |
| Uncertain mutation | Explicit `FAILED_UNCERTAIN` terminal state | Requires application-specific idempotency/ledger logic |
| Policy | Central Tool Pipeline, independent of orchestration | Reuses the same Tool Pipeline |
| Observability | Domain event timeline is the source of truth | Checkpoint history is graph-centric |
| Complexity | More domain code, exact incident semantics | Less routing code, framework dependency and graph state mapping |

## Evidence

`ig eval langgraph-baseline` runs approved and rejected branches. Both must interrupt
once, execute zero mutations before approval, execute exactly one rollback only in
the approved branch, and verify recovery only after that rollback.

LangGraph documents checkpoint persistence and super-step recovery in its
[persistence guide](https://docs.langchain.com/oss/python/langgraph/persistence), and
documents node restart/idempotency requirements in its
[interrupt guide](https://docs.langchain.com/oss/python/langgraph/interrupts).

## Migration conditions

Reconsider adopting LangGraph as the primary coordinator if the project needs broad
dynamic graph composition, framework-native distributed execution, or integration
with a managed LangGraph deployment. A migration must preserve the append-only audit
log, explicit uncertain-mutation state, Tool Pipeline safety checks, and current
replay invariants.
