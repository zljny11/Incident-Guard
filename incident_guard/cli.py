from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from importlib.metadata import version

from incident_guard.channels.cli_adapter import CliChannelAdapter
from incident_guard.gateway.runtime import GatewayRuntime


DISTRIBUTION_NAME = "incident-guard"


def build_parser() -> argparse.ArgumentParser:
    """Build the minimal project CLI parser.

    Cycle 0 exposes only version information and the existing Gateway demo.
    Incident investigation commands are intentionally deferred to later cycles.
    """

    # 创建命令行解析器，并将程序名称固定为 `ig`。
    parser = argparse.ArgumentParser(
        prog="ig",
        description=(
            "Incident Guard: an auditable runtime for incident "
            "investigation and recovery."
        ),
    )
    # 注册一级子命令，例如 `ig version` 和 `ig demo`。
    commands = parser.add_subparsers(dest="command")
    parser.add_argument("--data-dir", default="data", help=argparse.SUPPRESS)
    parser.add_argument("--lab-dir", default="lab", help=argparse.SUPPRESS)

    # `version` 命令用于显示当前安装的项目版本。
    version_parser = commands.add_parser(
        "version",
        help="Show the installed Incident Guard version.",
    )
    # 解析到该命令后，main() 会调用 _show_version()。
    version_parser.set_defaults(handler=_show_version)

    # `demo` 命令用于运行本地的确定性演示。
    demo_parser = commands.add_parser(
        "demo",
        help="Run a deterministic local demo.",
    )
    # demo 下面还需要一个具体的演示名称；required=True 表示不能省略。
    demos = demo_parser.add_subparsers(dest="demo_name", required=True)
    # 当前只提供 Gateway 演示，完整命令为 `ig demo gateway`。
    gateway_parser = demos.add_parser(
        "gateway",
        help="Run the existing Fake Provider Gateway flow.",
    )
    # 选择 Gateway 演示后，main() 会调用 _run_gateway_demo()。
    gateway_parser.set_defaults(handler=_run_gateway_demo)

    lab_parser = commands.add_parser("lab", help="Control the Docker Incident Lab.")
    lab_commands = lab_parser.add_subparsers(dest="lab_command", required=True)
    for action in ("up", "down", "reset"):
        action_parser = lab_commands.add_parser(action)
        action_parser.set_defaults(handler=_run_lab_action, lab_action=action)
    lab_inject = lab_commands.add_parser("inject")
    _add_scenario_argument(lab_inject)
    lab_inject.set_defaults(handler=_inject_incident)

    inject_parser = commands.add_parser("inject", help="Inject a lab fault.")
    _add_scenario_argument(inject_parser)
    inject_parser.set_defaults(handler=_inject_incident)

    investigate = commands.add_parser("investigate", help="Start a durable incident run.")
    investigate.add_argument("--alert", required=True)
    investigate.add_argument("--scenario", choices=_scenario_choices())
    investigate.add_argument("--run-id")
    investigate.set_defaults(handler=_investigate)

    agent = commands.add_parser(
        "agent",
        help="Run the durable model -> approval -> MCP -> Docker workflow.",
    )
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_investigate = agent_commands.add_parser("investigate")
    agent_investigate.add_argument("--alert", required=True)
    agent_investigate.add_argument("--scenario", choices=_scenario_choices())
    agent_investigate.add_argument("--run-id")
    agent_investigate.set_defaults(handler=_agent_investigate)
    agent_status = agent_commands.add_parser("status")
    agent_status.add_argument("run_id")
    agent_status.set_defaults(handler=_agent_status)
    for command, approved in (("approve", True), ("reject", False)):
        decision = agent_commands.add_parser(command)
        decision.add_argument("run_id")
        decision.add_argument("call_id")
        decision.add_argument("--reason", default=f"operator {command}d")
        decision.set_defaults(handler=_agent_decide, approved=approved)
    agent_resume = agent_commands.add_parser("resume")
    agent_resume.add_argument("run_id")
    agent_resume.set_defaults(handler=_agent_resume)

    status = commands.add_parser("status", help="Show a durable run status.")
    status.add_argument("run_id")
    status.set_defaults(handler=_run_status)

    for command, approved in (("approve", True), ("reject", False)):
        decision = commands.add_parser(command, help=f"{command.title()} a pending tool call.")
        decision.add_argument("run_id")
        decision.add_argument("call_id")
        decision.add_argument("--reason", default=f"operator {command}d")
        decision.set_defaults(handler=_decide, approved=approved)

    cancel = commands.add_parser("cancel", help="Cancel an active run.")
    cancel.add_argument("run_id")
    cancel.set_defaults(handler=_cancel)

    resume = commands.add_parser("resume", help="Resume an approved run.")
    resume.add_argument("run_id")
    resume.set_defaults(handler=_resume)

    eval_parser = commands.add_parser("eval", help="Run reproducible evaluations.")
    eval_commands = eval_parser.add_subparsers(dest="eval_command", required=True)
    scripted = eval_commands.add_parser("scripted")
    scripted.add_argument("--scenario-dir", default="evals/scenarios")
    scripted.add_argument("--output-dir", default="evals/reports")
    scripted.set_defaults(handler=_run_scripted_eval)

    real = eval_commands.add_parser("real")
    real.add_argument("--scenario-dir", default="evals/scenarios")
    real.add_argument("--output-dir", default="evals/reports")
    real.add_argument("--runs-per-scenario", type=int, default=5)
    real.add_argument("--max-steps", type=int, default=10)
    real.set_defaults(handler=_run_real_eval)

    langgraph = eval_commands.add_parser("langgraph-baseline")
    langgraph.add_argument("--output-dir", default="evals/reports")
    langgraph.set_defaults(handler=_run_langgraph_baseline)

    console = commands.add_parser("console", help="Run the local Web Console.")
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=8000)
    console.add_argument("--eval-dir", default="evals/reports")
    console.set_defaults(handler=_run_console)

    return parser


def _show_version(_args: argparse.Namespace) -> int:
    # 从已安装的 Python 包元数据中读取版本号并输出。
    print(f"Incident Guard {version(DISTRIBUTION_NAME)}")
    return 0


def _run_gateway_demo(_args: argparse.Namespace) -> int:
    # 构造一条模拟命令行消息，并转换成系统内部统一的消息格式。
    message = CliChannelAdapter().to_inbound_message(
        {
            "account_id": "demo-account",
            "peer_id": "demo-operator",
            "text": "检查 Gateway 基线",
        }
    )
    # 将消息交给 Gateway 运行时处理，得到本次演示的执行结果。
    result = GatewayRuntime().handle_message(message)

    # 输出演示结果，便于用户查看处理原因、会话和追踪文件位置。
    print("Incident Guard Gateway Demo")
    print(f"agent: {result.agent_id}")
    print(f"reason: {result.reason}")
    print(f"session_id: {result.session_id}")
    print(f"response: {result.response_text}")
    print(f"trace_file: {result.trace_path}")
    return 0


def _scenario_choices() -> list[str]:
    from incident_guard.tools import IncidentScenario

    return [scenario.value for scenario in IncidentScenario]


def _add_scenario_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("scenario", choices=_scenario_choices())


def _with_service(args: argparse.Namespace, operation) -> int:
    from incident_guard.incident_cli import IncidentCLIError, IncidentCLIService

    service = IncidentCLIService(args.data_dir, args.lab_dir)
    try:
        result = operation(service)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (IncidentCLIError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}")
        return 2
    finally:
        service.close()


def _run_lab_action(args: argparse.Namespace) -> int:
    return _with_service(args, lambda service: service.lab(args.lab_action))


def _inject_incident(args: argparse.Namespace) -> int:
    return _with_service(args, lambda service: service.inject(args.scenario))


def _investigate(args: argparse.Namespace) -> int:
    return _with_service(
        args,
        lambda service: service.investigate(
            args.alert, scenario=args.scenario, run_id=args.run_id
        ),
    )


def _run_status(args: argparse.Namespace) -> int:
    return _with_service(args, lambda service: service.status(args.run_id))


def _decide(args: argparse.Namespace) -> int:
    return _with_service(
        args,
        lambda service: service.decide(
            args.run_id,
            args.call_id,
            approved=args.approved,
            reason=args.reason,
        ),
    )


def _cancel(args: argparse.Namespace) -> int:
    return _with_service(args, lambda service: service.cancel(args.run_id))


def _resume(args: argparse.Namespace) -> int:
    return _with_service(args, lambda service: service.resume(args.run_id))


def _with_agent_service(args: argparse.Namespace, operation) -> int:
    from incident_guard.durable_incident_agent import (
        DurableIncidentAgentError,
        DurableIncidentAgentService,
    )

    service = DurableIncidentAgentService(args.data_dir, args.lab_dir)
    try:
        result = operation(service)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        DurableIncidentAgentError,
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}")
        return 2
    finally:
        service.close()


def _agent_investigate(args: argparse.Namespace) -> int:
    return _with_agent_service(
        args,
        lambda service: service.investigate(
            args.alert, scenario=args.scenario, run_id=args.run_id
        ),
    )


def _agent_status(args: argparse.Namespace) -> int:
    return _with_agent_service(args, lambda service: service.status(args.run_id))


def _agent_decide(args: argparse.Namespace) -> int:
    return _with_agent_service(
        args,
        lambda service: service.decide(
            args.run_id,
            args.call_id,
            approved=args.approved,
            reason=args.reason,
        ),
    )


def _agent_resume(args: argparse.Namespace) -> int:
    return _with_agent_service(args, lambda service: service.resume(args.run_id))


def _run_scripted_eval(args: argparse.Namespace) -> int:
    from incident_guard.evals import run_scripted_matrix

    report = run_scripted_matrix(args.scenario_dir, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _run_real_eval(args: argparse.Namespace) -> int:
    from incident_guard.evals import run_real_matrix

    try:
        report = run_real_matrix(
            args.scenario_dir,
            args.output_dir,
            runs_per_scenario=args.runs_per_scenario,
            max_steps=args.max_steps,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}")
        return 2
    print(json.dumps(report["aggregate"], ensure_ascii=False, sort_keys=True))
    return 0


def _run_langgraph_baseline(args: argparse.Namespace) -> int:
    from incident_guard.baselines import write_langgraph_baseline_report

    report = write_langgraph_baseline_report(args.output_dir)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _run_console(args: argparse.Namespace) -> int:
    from incident_guard.web import serve_console

    serve_console(
        data_dir=args.data_dir,
        lab_dir=args.lab_dir,
        eval_dir=args.eval_dir,
        host=args.host,
        port=args.port,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Incident Guard command-line entry point."""

    # 创建解析器并解析命令行参数；传入 argv 主要方便测试时指定参数。
    parser = build_parser()
    args = parser.parse_args(argv)
    # 每个命令都会在解析结果中绑定一个 handler 处理函数。
    handler = getattr(args, "handler", None)
    if handler is None:
        # 用户未输入具体命令时，显示使用帮助而不是报错。
        parser.print_help()
        return 0
    # 执行当前命令对应的处理函数，并返回其退出码。
    return handler(args)


if __name__ == "__main__":
    # 直接运行本文件时启动 CLI；返回值会成为进程退出状态码。
    raise SystemExit(main())
