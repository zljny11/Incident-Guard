"""Small comparison implementations used by evaluation ADRs."""

from incident_guard.baselines.langgraph_baseline import (
    run_bad_deployment_baseline,
    write_langgraph_baseline_report,
)

__all__ = ["run_bad_deployment_baseline", "write_langgraph_baseline_report"]
