# Incident Guard 简历与面试材料

## 简历项目名称

**Incident Guard｜可审计的故障响应 Agent Runtime｜Python / AsyncIO / SQLite / MCP / Docker / DeepSeek API**

## 推荐项目描述

- 实现异步结构化 Agent 执行循环，提供统一流式事件接口、Tool Result 回灌及
  Step、Token、Tool Call、超时预算；设计可插拔的确定性 Goal Gate，校验证据、
  审批及恢复结果，阻止模型仅凭文本声明任务完成。
- 设计统一 Tool Registry 与安全执行管线，依次完成 JSON Schema 校验、策略判定、
  人工审批与 Handler 执行，隔离模型意图和真实副作用；只读工具支持有界并发，
  restart/rollback 通过进程内 Named Lane 按服务串行执行。
- 基于 SQLite append-only Event Store 持久化模型消息、工具调用、审批及状态转换，
  通过数据库 Trigger 禁止事件修改和删除；支持状态重放、cancel/resume，恢复时
  跳过已完成调用，并将结果未知的修改操作标记为 `FAILED_UNCERTAIN`。
- 搭建 Docker 多服务故障实验环境，通过 MCP stdio 暴露 8 个受限工具，打通
  DeepSeek、持久化 Runtime、人工审批与真实容器处置链路；验证审批前零副作用、
  跨进程恢复、受控回滚及健康检查，206 个自动化测试全部通过。

> 口径边界：重复 15 次的模型回归仍使用确定性内存工具；另有 1 次经过人工审批的
> DeepSeek + MCP + Docker 真实部署回归验收。不能将单次受控验收外推为生产可靠性。

## Recommended English Version

**Incident Guard (Auditable Incident-Response Agent Runtime)** | *Python, AsyncIO, SQLite, MCP, Docker, DeepSeek API*

- Implemented an asynchronous structured agent loop with a unified streaming-event
  interface, Tool Result feedback, and step, token, tool-call, and timeout budgets;
  designed a pluggable deterministic Goal Gate that validates evidence, approvals,
  and recovery outcomes instead of trusting text-only completion claims.
- Designed a centralized Tool Registry and safety execution pipeline covering JSON Schema
  validation, policy evaluation, human approval, and Handler invocation, separating model
  intent from real side effects; bounded read concurrency and process-local Named Lanes
  serialize restart/rollback operations per service.
- Persisted model messages, tool calls, approvals, and state transitions in a SQLite
  append-only Event Store, with database triggers rejecting event updates and deletions;
  deterministic replay supports cancel/resume, skips completed calls, and marks mutations
  with unknown outcomes as `FAILED_UNCERTAIN`.
- Built a multi-service Docker fault lab exposing eight restricted tools over MCP stdio,
  connecting DeepSeek, the durable Runtime, human approval, and real container remediation;
  validated zero pre-approval side effects, cross-process recovery, controlled rollback,
  and health checks, with all 206 automated tests passing.

## 一句话介绍

这是一个面向容器化服务故障处理的可审计 Agent Harness：模型负责判断，
Runtime 负责状态、预算和恢复，Policy/Approval 决定工具是否真的能执行。

## 数据证据索引

| 简历数据 | 可核验证据 |
| --- | --- |
| 15 次真实模型运行，15/15 passed（确定性内存工具后端） | `evals/reports/real-model-eval.json`、`incident_guard/evals/real_matrix.py` |
| DeepSeek + 持久化 Runtime + 人工审批 + MCP/Docker 端到端验收 | `evals/reports/durable-deepseek-docker.md` |
| 100% root cause / evidence / resolution / verification | 同一报告的 `aggregate` 与逐 Run `metrics` |
| 129,878 tokens、166 tool calls、$0.06592 保守成本 | 同一报告的 token/tool/cost 字段 |
| 5/5 故障注入被识别，Runtime invariant 全为 0 | `evals/reports/scripted-eval.json` |
| LangGraph 批准前 0 mutation，拒绝分支 0 mutation | `evals/reports/langgraph-baseline.json` |
| 206 个自动化测试通过 | 全量 `pytest` 结果；测试范围见 `tests/` |
| Docker rollback/restart/dependency 与 MCP 调用链 | `tests/test_lab_*.py`、`tests/test_incident_cli_docker.py`、`tests/test_mcp_tools.py` |

## 面试重点学习顺序

1. `react_runtime.py`：为什么 Loop 消费结构化 Tool Call，而不是解析文本 Action。
2. `tool_pipeline.py`：Schema、Policy、Approval、并发和 Named Lane 的顺序。
3. `event_store.py`、`projection.py`、`event_runtime.py`：事件源、恢复边界和
   `FAILED_UNCERTAIN`。
4. `context_engine.py`：pinning、Tool Call/Result 配对和 Token Budget。
5. `real_matrix.py`：Oracle 隔离、真实轨迹评分和成本统计。
6. ADR 0001：当前 Runtime 与 LangGraph checkpoint/interrupt 的取舍。

## 高频追问回答方向

### 为什么不直接使用 LangGraph？

项目最关键的是审计事件和 mutation 不确定态，而不是动态 Graph 编排。
LangGraph baseline 能简化路由和 checkpoint，但仍需要额外的副作用 ledger、
Policy 和领域事件。本项目保留原生 Runtime，并把 LangGraph 作为可复现对照。

### 如何避免 Agent 乱操作？

模型只产生 Tool Call 意图；所有调用必须经过 Tool Registry、JSON Schema、
Policy 和 Approval。MUTATE 工具强制串行且按 service 使用 Named Lane，未批准
不会进入 handler，执行后必须调用 `verify_recovery`。

### 进程在修改操作中崩溃怎么办？

如果 `tool.started` 已持久化但没有 `tool.completed`，Runtime 不猜测是否成功，
而是进入 `FAILED_UNCERTAIN`，禁止自动重试。只读工具则可以在恢复后安全重试。

### 100% 是否代表模型在生产中可靠？

不是。重复评测数据只代表三个受控内存仿真场景、每场景五次的当前基线。
评测 Oracle 不进入模型消息，但完成门会用它判断必需证据是否已经收集齐全；
另有一次真实 DeepSeek + MCP + Docker 验收，仍然只是本地受控实验，不能外推为
生产 SLA。
