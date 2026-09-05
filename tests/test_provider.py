from __future__ import annotations

import pytest

from incident_guard.agents.agent_runtime import AgentRuntime
from incident_guard.agents.openai_compatible_provider import OpenAICompatibleProvider
from incident_guard.agents.provider import FakeProvider, ProviderError, ProviderResponse
from incident_guard.agents.provider_factory import ProviderConfig, create_provider


class StubProvider:
    """不继承任何基类，用来验证 Provider 的结构化契约。"""

    def generate(self, messages: list[dict]) -> ProviderResponse:
        return ProviderResponse(
            text=f"stub saw {len(messages)} messages",
            stop_reason="end_turn",
        )


def test_provider_config_defaults_to_fake() -> None:
    assert ProviderConfig.from_env({}) == ProviderConfig(name="fake")


def test_provider_config_normalizes_environment_value() -> None:
    config = ProviderConfig.from_env({"IG_PROVIDER": "  FAKE  "})

    assert config.name == "fake"


def test_provider_config_rejects_empty_environment_value() -> None:
    with pytest.raises(ProviderError, match="IG_PROVIDER cannot be empty"):
        ProviderConfig.from_env({"IG_PROVIDER": "   "})


def test_provider_factory_creates_fake_provider() -> None:
    provider = create_provider(ProviderConfig(name="fake"))

    assert isinstance(provider, FakeProvider)


def test_provider_factory_rejects_unknown_provider() -> None:
    with pytest.raises(
        ProviderError,
        match="Unsupported provider: unknown. Supported providers: fake, openai",
    ):
        create_provider(ProviderConfig(name="unknown"))


def test_openai_provider_requires_api_key() -> None:
    config = ProviderConfig(name="openai", model="test-model")

    with pytest.raises(ProviderError, match="IG_OPENAI_API_KEY is required"):
        create_provider(config)


def test_openai_provider_requires_model() -> None:
    config = ProviderConfig(name="openai", api_key="test-key")

    with pytest.raises(ProviderError, match="IG_OPENAI_MODEL is required"):
        create_provider(config)


def test_openai_provider_is_created_from_environment_config() -> None:
    config = ProviderConfig.from_env(
        {
            "IG_PROVIDER": "openai",
            "IG_OPENAI_API_KEY": "test-key",
            "IG_OPENAI_MODEL": "test-model",
            "IG_OPENAI_BASE_URL": "https://llm.example/v1/",
            "IG_OPENAI_TIMEOUT_SECONDS": "12.5",
        }
    )

    provider = create_provider(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "test-model"
    assert provider.base_url == "https://llm.example/v1"
    assert provider.timeout_seconds == 12.5


def test_agent_runtime_accepts_provider_protocol_implementation() -> None:
    runtime = AgentRuntime(provider=StubProvider())

    response = runtime.run([{"role": "user", "content": "hello"}])

    assert response == "stub saw 1 messages"
