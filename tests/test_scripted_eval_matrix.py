from pathlib import Path

from incident_guard.evals import run_scripted_matrix


SCENARIOS = Path(__file__).parents[1] / "evals" / "scenarios"


def test_scripted_matrix_is_100_percent_and_writes_stable_reports(tmp_path):
    first = run_scripted_matrix(SCENARIOS, tmp_path)
    json_first = (tmp_path / "scripted-eval.json").read_text()
    markdown_first = (tmp_path / "scripted-eval.md").read_text()
    second = run_scripted_matrix(SCENARIOS, tmp_path)

    assert first == second
    assert first["deterministic_scenario_pass_rate"] == 1.0
    assert all(row["passed"] for row in first["scenarios"])
    assert all(row["detected"] for row in first["fault_injection_matrix"])
    assert not any(first["runtime_invariants"].values())
    assert (tmp_path / "scripted-eval.json").read_text() == json_first
    assert (tmp_path / "scripted-eval.md").read_text() == markdown_first
    assert "Deterministic scenario pass rate: 100%" in markdown_first
