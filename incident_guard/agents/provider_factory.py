from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from incident_guard.agents.openai_compatible_provider import OpenAICompatibleProvider
from incident_guard.agents.provider import FakeProvider, Provider, ProviderError


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """ProviderConfig 保存选择 provider 所需的运行配置。"""

    name: str = "fake"
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    model: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProviderConfig":
        """从环境变量读取配置；默认使用可离线运行的 fake provider。"""

        source = os.environ if environ is None else environ
        name = source.get("IG_PROVIDER", "fake").strip().lower()
        if not name:
            raise ProviderError("IG_PROVIDER cannot be empty")

        timeout_value = source.get("IG_OPENAI_TIMEOUT_SECONDS", "30")
        try:
            timeout_seconds = float(timeout_value)
        except ValueError as error:
            raise ProviderError(
                "IG_OPENAI_TIMEOUT_SECONDS must be a number"
            ) from error
        if timeout_seconds <= 0:
            raise ProviderError("IG_OPENAI_TIMEOUT_SECONDS must be greater than 0")

        return cls(
            name=name,
            api_key=source.get("IG_OPENAI_API_KEY"),
            base_url=source.get(
                "IG_OPENAI_BASE_URL",
                "https://api.openai.com/v1",
            ).rstrip("/"),
            model=source.get("IG_OPENAI_MODEL"),
            timeout_seconds=timeout_seconds,
        )


ProviderBuilder = Callable[[ProviderConfig], Provider]


def _build_fake_provider(_config: ProviderConfig) -> Provider:
    return FakeProvider()


def _build_openai_provider(config: ProviderConfig) -> Provider:
    if not config.api_key:
        raise ProviderError(
            "IG_OPENAI_API_KEY is required when IG_PROVIDER=openai"
        )
    if not config.model:
        raise ProviderError(
            "IG_OPENAI_MODEL is required when IG_PROVIDER=openai"
        )
    if not config.base_url:
        raise ProviderError(
            "IG_OPENAI_BASE_URL cannot be empty when IG_PROVIDER=openai"
        )
    return OpenAICompatibleProvider(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
    )

_PROVIDER_BUILDERS: dict[str, ProviderBuilder] = {
    "fake": _build_fake_provider,
    "openai": _build_openai_provider,
}


def create_provider(config: ProviderConfig | None = None) -> Provider:
    """根据配置创建 provider，并对不支持的名称给出清晰错误。"""

    resolved_config = config or ProviderConfig.from_env()
    builder = _PROVIDER_BUILDERS.get(resolved_config.name)
    if builder is None:
        supported = ", ".join(sorted(_PROVIDER_BUILDERS))
        raise ProviderError(
            f"Unsupported provider: {resolved_config.name}. Supported providers: {supported}"
        )
    return builder(resolved_config)
