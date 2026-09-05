from __future__ import annotations

from importlib.metadata import version

import pytest

from incident_guard.cli import build_parser, main


def test_cli_help_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "usage: ig" in help_text
    assert "Incident Guard" in help_text


def test_cli_version_reports_installed_distribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["version"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == (
        f"Incident Guard {version('incident-guard')}"
    )


def test_cli_demo_gateway_runs_existing_offline_flow(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(["demo", "gateway"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Incident Guard Gateway Demo" in output
    assert "agent: incident-agent" in output
    assert "reason: selected single incident-agent profile" in output
    assert "[fake-agent-response] I received: 检查 Gateway 基线" in output
    assert len(list((tmp_path / "data" / "sessions").glob("*.jsonl"))) == 1
    assert len(list((tmp_path / "data" / "traces").glob("*.jsonl"))) == 1


def test_cli_rejects_unknown_command() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["unknown"])

    assert exit_info.value.code == 2
