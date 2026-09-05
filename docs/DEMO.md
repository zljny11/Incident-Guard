# Incident Guard 演示脚本

## 真实模型主线

```bash
ig lab reset
ig inject bad_deployment
source .env.deepseek
ig agent investigate --alert examples/alerts/payment-5xx.json --run-id run-agent-demo
ig agent status run-agent-demo
```

从 `pending_approvals` 复制模型生成的 `call_id`，由操作员批准：

```bash
ig agent approve run-agent-demo <call_id> --reason "rollback reviewed"
ig agent resume run-agent-demo
ig agent status run-agent-demo
```

讲解顺序：DeepSeek 收集真实容器的健康、指标、日志、部署和 Runbook；Runtime
在 rollback 前落库并退出到 `WAITING_APPROVAL`；另一个 CLI 进程写入审批，再由
新进程重放 SQLite 事件，经 MCP 执行真实回滚，最后由模型调用
`verify_recovery`。

## 无 API 成本的确定性演示

```bash
ig lab reset
ig inject bad_deployment
ig investigate --alert examples/alerts/payment-5xx.json --run-id run-demo
ig approve run-demo rollback_service-1 --reason "rollback reviewed"
ig resume run-demo
```

## Web Console

```bash
ig console --host 127.0.0.1 --port 8000
```

- `/runs`：Run 状态总览。
- `/runs/run-demo`：Durable Event、Tool、Evidence、Approval 时间线。
- `/evals`：Scripted、DeepSeek 和 LangGraph 报告。

## 安全负例

```bash
ig lab reset
ig inject dependency_outage
ig investigate --alert examples/alerts/dependency-unavailable.json --scenario dependency_outage --run-id run-dependency
ig status run-dependency
```

重点展示 dependency outage 不会触发 payment restart/rollback，而是产生安全升级建议。

## 结束清理

```bash
ig lab down
```
