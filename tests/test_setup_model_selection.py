"""Tests for _setup_provider_model_selection and the zai/kimi/minimax branch.

Regression test for the is_coding_plan NameError that crashed setup when
selecting zai, kimi-coding, minimax, or minimax-cn providers.
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def mock_provider_registry():
    """Minimal PROVIDER_REGISTRY entries for tested providers."""
    class FakePConfig:
        def __init__(self, name, env_vars, base_url_env, inference_url):
            self.name = name
            self.api_key_env_vars = env_vars
            self.base_url_env_var = base_url_env
            self.inference_base_url = inference_url

    return {
        "zai": FakePConfig("ZAI", ["ZAI_API_KEY"], "ZAI_BASE_URL", "https://api.zai.example"),
        "kimi-coding": FakePConfig("Kimi Coding", ["KIMI_API_KEY"], "KIMI_BASE_URL", "https://api.kimi.example"),
        "minimax": FakePConfig("MiniMax", ["MINIMAX_API_KEY"], "MINIMAX_BASE_URL", "https://api.minimax.example"),
        "minimax-cn": FakePConfig("MiniMax CN", ["MINIMAX_API_KEY"], "MINIMAX_CN_BASE_URL", "https://api.minimax-cn.example"),
        "upstream-zen": FakePConfig("upstream Zen", ["UPSTREAM_ZEN_API_KEY"], "UPSTREAM_ZEN_BASE_URL", "https://upstream.ai/zen/v1"),
        "upstream-go": FakePConfig("upstream Go", ["UPSTREAM_GO_API_KEY"], "UPSTREAM_GO_BASE_URL", "https://upstream.ai/zen/go/v1"),
    }


class TestSetupProviderModelSelection:
    """Verify _setup_provider_model_selection works for all providers
    that previously hit the is_coding_plan NameError."""

    @pytest.mark.parametrize("provider_id,expected_defaults", [
        ("zai", ["glm-5", "glm-4.7", "glm-4.5", "glm-4.5-flash"]),
        ("kimi-coding", ["kimi-k2.5", "kimi-k2-thinking", "kimi-k2-turbo-preview"]),
        ("minimax", ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M2.1"]),
        ("minimax-cn", ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M2.1"]),
        ("upstream-zen", ["gpt-5.4", "gpt-5.3-codex", "claude-sonnet-4-6", "gemini-3-flash"]),
        ("upstream-go", ["glm-5", "kimi-k2.5", "minimax-m2.5", "minimax-m2.7"]),
    ])
    @patch("rhemify_cli.models.fetch_api_models", return_value=[])
    @patch("rhemify_cli.config.get_env_value", return_value="fake-key")
    def test_falls_back_to_default_models_without_crashing(
        self, mock_env, mock_fetch, provider_id, expected_defaults, mock_provider_registry
    ):
        """Previously this code path raised NameError: 'is_coding_plan'.
        Now it delegates to _setup_provider_model_selection which uses
        _DEFAULT_PROVIDER_MODELS -- no crash, correct model list."""
        from rhemify_cli.setup import _setup_provider_model_selection

        captured_choices = {}

        def fake_prompt_choice(label, choices, default):
            captured_choices["choices"] = choices
            # Select "Keep current" (last item)
            return len(choices) - 1

        with patch("rhemify_cli.auth.PROVIDER_REGISTRY", mock_provider_registry):
            _setup_provider_model_selection(
                config={"model": {}},
                provider_id=provider_id,
                current_model="some-model",
                prompt_choice=fake_prompt_choice,
                prompt_fn=lambda _: None,
            )

        # The offered model list should start with the default models
        offered = captured_choices["choices"]
        for model in expected_defaults:
            assert model in offered, f"{model} not in choices for {provider_id}"

    @patch("rhemify_cli.models.fetch_api_models")
    @patch("rhemify_cli.config.get_env_value", return_value="fake-key")
    def test_live_models_used_when_available(
        self, mock_env, mock_fetch, mock_provider_registry
    ):
        """When fetch_api_models returns results, those are used instead of defaults."""
        from rhemify_cli.setup import _setup_provider_model_selection

        live = ["live-model-1", "live-model-2"]
        mock_fetch.return_value = live

        captured_choices = {}

        def fake_prompt_choice(label, choices, default):
            captured_choices["choices"] = choices
            return len(choices) - 1

        with patch("rhemify_cli.auth.PROVIDER_REGISTRY", mock_provider_registry):
            _setup_provider_model_selection(
                config={"model": {}},
                provider_id="zai",
                current_model="some-model",
                prompt_choice=fake_prompt_choice,
                prompt_fn=lambda _: None,
            )

        offered = captured_choices["choices"]
        assert "live-model-1" in offered
        assert "live-model-2" in offered

    @patch("rhemify_cli.models.fetch_api_models", return_value=[])
    @patch("rhemify_cli.config.get_env_value", return_value="fake-key")
    def test_custom_model_selection(
        self, mock_env, mock_fetch, mock_provider_registry
    ):
        """Selecting 'Custom model' lets user type a model name."""
        from rhemify_cli.setup import _setup_provider_model_selection, _DEFAULT_PROVIDER_MODELS

        defaults = _DEFAULT_PROVIDER_MODELS["zai"]
        custom_model_idx = len(defaults)  # "Custom model" is right after defaults

        config = {"model": {}}

        def fake_prompt_choice(label, choices, default):
            return custom_model_idx

        with patch("rhemify_cli.auth.PROVIDER_REGISTRY", mock_provider_registry):
            _setup_provider_model_selection(
                config=config,
                provider_id="zai",
                current_model="some-model",
                prompt_choice=fake_prompt_choice,
                prompt_fn=lambda _: "my-custom-model",
            )

        assert config["model"]["default"] == "my-custom-model"

    @patch("rhemify_cli.models.fetch_api_models", return_value=["upstream-go/kimi-k2.5", "upstream-go/minimax-m2.7"])
    @patch("rhemify_cli.config.get_env_value", return_value="fake-key")
    def test_upstream_live_models_are_normalized_for_selection(
        self, mock_env, mock_fetch, mock_provider_registry
    ):
        from rhemify_cli.setup import _setup_provider_model_selection

        captured_choices = {}

        def fake_prompt_choice(label, choices, default):
            captured_choices["choices"] = choices
            return len(choices) - 1

        with patch("rhemify_cli.auth.PROVIDER_REGISTRY", mock_provider_registry):
            _setup_provider_model_selection(
                config={"model": {}},
                provider_id="upstream-go",
                current_model="upstream-go/kimi-k2.5",
                prompt_choice=fake_prompt_choice,
                prompt_fn=lambda _: None,
            )

        offered = captured_choices["choices"]
        assert "kimi-k2.5" in offered
        assert "minimax-m2.7" in offered
        assert all("upstream-go/" not in choice for choice in offered)
