from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest


LAB_DIR = Path(__file__).parents[1] / "lab"


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker CLI is required for the real Incident Lab smoke test",
)


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=LAB_DIR,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def wait_for_health(url: str) -> dict:
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read())
            if payload.get("status") == "healthy":
                return payload
        except Exception as error:  # service startup is eventually consistent
            last_error = error
        time.sleep(0.5)
    raise AssertionError(f"service did not become healthy: {last_error}")


def test_compose_reset_recreates_same_healthy_initial_state() -> None:
    observed = []
    try:
        for _ in range(2):
            compose("down", "--volumes", "--remove-orphans")
            compose("up", "--build", "--detach", "--wait")
            observed.append(
                tuple(
                    (payload["service"], payload["version"], payload["status"])
                    for payload in (
                        wait_for_health("http://127.0.0.1:18080/health"),
                        wait_for_health("http://127.0.0.1:18081/health"),
                        wait_for_health("http://127.0.0.1:18082/health"),
                    )
                )
            )
        assert observed[0] == observed[1]
    finally:
        compose("down", "--volumes", "--remove-orphans")
