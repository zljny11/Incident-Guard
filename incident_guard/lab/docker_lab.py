from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


class LabCommandError(RuntimeError):
    """A restricted Docker Lab operation failed."""


CommandRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]


def _default_runner(
    command: Sequence[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class DockerLabController:
    """Allowlisted Docker Compose operations for the local lab only."""

    SERVICE_URLS = {
        "shop-api": "http://127.0.0.1:18080",
        "payment-service": "http://127.0.0.1:18081",
        "dependency-service": "http://127.0.0.1:18082",
    }
    RESTARTABLE_SERVICES = frozenset({"payment-service"})
    ROLLBACK_TARGETS = {"payment-service": frozenset({"v1"})}

    def __init__(
        self,
        lab_dir: str | Path,
        *,
        admin_token: str = "incident-guard-local",
        runner: CommandRunner = _default_runner,
    ) -> None:
        self.lab_dir = Path(lab_dir).resolve()
        if not (self.lab_dir / "docker-compose.yml").is_file():
            raise ValueError("lab_dir must contain docker-compose.yml")
        if not isinstance(admin_token, str) or not admin_token:
            raise ValueError("admin_token must be non-empty")
        self.admin_token = admin_token
        self.runner = runner

    def up(self, *, build: bool = True) -> None:
        arguments = ["up", "--detach", "--wait"]
        if build:
            arguments.insert(1, "--build")
        self._compose_with_payment_version("v1", *arguments, timeout=180)

    def down(self) -> None:
        self._compose("down", "--volumes", "--remove-orphans", timeout=60)

    def reset(self) -> None:
        self.down()
        self.up()

    def inject_transient_hang(self, service_id: str = "payment-service") -> dict:
        if service_id != "payment-service":
            raise ValueError("transient_hang is only supported for payment-service")
        return self._post_json(
            f"{self.SERVICE_URLS[service_id]}/admin/inject",
            {"fault": "transient_hang"},
        )

    def restart_service(self, service_id: str) -> None:
        if service_id not in self.RESTARTABLE_SERVICES:
            raise ValueError(f"service is not restartable: {service_id}")
        self._compose("restart", service_id, timeout=60)

    def deploy_bad_deployment(self, service_id: str = "payment-service") -> None:
        if service_id != "payment-service":
            raise ValueError("bad_deployment is only supported for payment-service")
        self._compose_with_payment_version(
            "v2",
            "up",
            "--build",
            "--detach",
            "--force-recreate",
            service_id,
            timeout=180,
        )

    def rollback_service(self, service_id: str, target_version: str) -> None:
        allowed_targets = self.ROLLBACK_TARGETS.get(service_id, frozenset())
        if target_version not in allowed_targets:
            raise ValueError(
                f"rollback target is not allowed: {service_id}:{target_version}"
            )
        self._compose_with_payment_version(
            target_version,
            "up",
            "--build",
            "--detach",
            "--force-recreate",
            service_id,
            timeout=180,
        )

    def inject_dependency_outage(self) -> None:
        # Fault injection is fixed to the downstream dependency; callers cannot
        # use this method as a general-purpose container stop primitive.
        self._compose("stop", "dependency-service", timeout=60)

    def query_health(self, service_id: str, *, timeout: float = 5.0) -> dict:
        try:
            url = f"{self.SERVICE_URLS[service_id]}/health"
        except KeyError as error:
            raise ValueError(f"unknown lab service: {service_id}") from error
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return json.loads(error.read().decode("utf-8"))

    def query_metrics(self, service_id: str, *, timeout: float = 5.0) -> dict:
        if service_id not in self.SERVICE_URLS:
            raise ValueError(f"unknown lab service: {service_id}")
        with urllib.request.urlopen(
            f"{self.SERVICE_URLS[service_id]}/metrics", timeout=timeout
        ) as response:
            body = response.read().decode("utf-8")
        values = {}
        for line in body.splitlines():
            if not line or line.startswith("#"):
                continue
            name, value = line.rsplit(" ", 1)
            values[name.split("{", 1)[0]] = float(value)
        requests = values.get("incident_guard_requests_total", 0.0)
        errors = values.get("incident_guard_errors_total", 0.0)
        return {
            "service_id": service_id,
            "requests": int(requests),
            "errors": int(errors),
            "error_rate": errors / requests if requests else 0.0,
        }

    def query_logs(self, service_id: str, *, limit: int = 20) -> dict:
        if service_id not in self.SERVICE_URLS:
            raise ValueError(f"unknown lab service: {service_id}")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        result = self._compose(
            "logs", "--no-color", "--tail", str(limit), service_id, timeout=15
        )
        return {"service_id": service_id, "records": result.stdout.splitlines()}

    def wait_healthy(self, service_id: str, *, timeout: float = 45.0) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                health = self.query_health(service_id)
                if health.get("status") == "healthy":
                    return health
            except (OSError, ValueError) as error:
                last_error = error
            time.sleep(0.25)
        raise LabCommandError(
            f"service did not become healthy: {service_id}: {last_error}"
        )

    def container_health(self, service_id: str) -> str:
        if service_id not in self.SERVICE_URLS:
            raise ValueError(f"unknown lab service: {service_id}")
        result = self._compose(
            "ps", "--format", "json", service_id, timeout=15
        )
        records = [
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        ]
        if len(records) != 1:
            raise LabCommandError(f"expected one container for {service_id}")
        return str(records[0].get("Health", "")).lower()

    def image_exists(self, image: str) -> bool:
        if image not in {
            "incident-guard/payment-service:v1",
            "incident-guard/payment-service:v2",
        }:
            raise ValueError(f"image is not inspectable: {image}")
        try:
            self.runner(
                ("docker", "image", "inspect", image), self.lab_dir, 15
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return True

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.admin_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def _compose(
        self, *arguments: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                ("docker", "compose", *arguments), self.lab_dir, timeout
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise LabCommandError(
                f"docker compose command failed: {' '.join(arguments)}"
            ) from error

    def _compose_with_payment_version(
        self, version: str, *arguments: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if version not in {"v1", "v2"}:
            raise ValueError(f"unsupported payment version: {version}")
        env_file = self.lab_dir / "env" / f"payment-{version}.env"
        try:
            return self.runner(
                (
                    "docker",
                    "compose",
                    "--env-file",
                    str(env_file),
                    *arguments,
                ),
                self.lab_dir,
                timeout,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise LabCommandError(
                f"docker compose payment {version} command failed: "
                f"{' '.join(arguments)}"
            ) from error
