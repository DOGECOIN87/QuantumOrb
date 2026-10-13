import json

import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, Settings, load_settings
from scripts.check_config import main as check_config_main


def isolated_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, _env_prefix="T002_TEST_", **overrides)  # type: ignore[arg-type]


def test_missing_key_is_allowed_in_demo_mode() -> None:
    settings = isolated_settings(ingest_mode="demo", ingest_transport="fixture")

    assert settings.helius_api_key is None
    assert settings.safe_diagnostics()["helius_api_key"] == "[missing]"


def test_missing_key_is_rejected_in_live_mode() -> None:
    with pytest.raises(ValidationError, match="HELIUS_API_KEY is required"):
        isolated_settings(ingest_mode="live", ingest_transport="parsed_streams")


def test_live_mode_accepts_key_and_provider_transport() -> None:
    settings = isolated_settings(
        ingest_mode="live",
        ingest_transport="parsed_streams",
        helius_api_key="server-side-test-key",
    )

    assert settings.safe_diagnostics()["helius_api_key"] == "[set]"


def test_live_fixture_transport_is_rejected() -> None:
    with pytest.raises(ValidationError, match="live mode requires a provider transport"):
        isolated_settings(
            ingest_mode="live",
            ingest_transport="fixture",
            helius_api_key="server-side-test-key",
        )


def test_invalid_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        isolated_settings(app_port=65536)


def test_non_loopback_host_is_rejected() -> None:
    with pytest.raises(ValidationError):
        isolated_settings(app_host="0.0.0.0")


@pytest.mark.parametrize("field", ["pump_program_id", "pump_amm_program_id"])
def test_invalid_program_id_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        isolated_settings(**{field: "not-a-solana-address"})


def test_provider_urls_reject_credentials_and_query_parameters() -> None:
    with pytest.raises(ValidationError):
        isolated_settings(helius_rpc_url="https://example.invalid/?api-key=not-safe")
    with pytest.raises(ValidationError):
        isolated_settings(helius_parsed_streams_url="wss://user:pass@example.invalid/")


def test_api_key_is_rejected_when_embedded_in_url_even_in_demo_mode() -> None:
    secret = "unique-url-key-do-not-print"
    with pytest.raises(ValidationError, match="must not be embedded"):
        isolated_settings(
            helius_api_key=secret,
            helius_rpc_url=f"https://rpc.example.invalid/{secret}",
        )


def test_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        isolated_settings(max_concurrent_enrichments=4)
    with pytest.raises(ValidationError):
        isolated_settings(cartographer_max_wallets_per_token=201)


def test_production_environment_alias_is_normalized() -> None:
    assert isolated_settings(app_env="PROD").app_env == "production"


def test_pumpfun_program_id_environment_name_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_program_id = "11111111111111111111111111111111"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("INGEST_MODE", "demo")
    monkeypatch.setenv("INGEST_TRANSPORT", "fixture")
    monkeypatch.setenv("PUMPFUN_PROGRAM_ID", expected_program_id)

    settings = load_settings()

    assert settings.pump_program_id == expected_program_id


def test_safe_diagnostics_never_return_the_key() -> None:
    secret = "unique-test-secret-do-not-print"
    settings = isolated_settings(helius_api_key=secret)

    diagnostic_json = json.dumps(settings.safe_diagnostics())

    assert secret not in diagnostic_json
    assert '"helius_api_key": "[set]"' in diagnostic_json


def test_loader_replaces_validation_errors_with_redacted_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unique-environment-secret-do-not-print"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("HELIUS_API_KEY", secret)
    monkeypatch.setenv("HELIUS_RPC_URL", "https://rpc.example.invalid/?api-key=" + secret)

    with pytest.raises(ConfigurationError) as raised:
        load_settings()

    assert secret not in str(raised.value)
    assert "helius_rpc_url" in str(raised.value)


def test_demo_config_command_does_not_print_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "unique-cli-secret-do-not-print"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("HELIUS_API_KEY", secret)

    assert check_config_main(["--demo"]) == 0
    output = capsys.readouterr().out

    assert secret not in output
    assert '"helius_api_key": "[set]"' in output


def test_live_config_command_without_key_fails_safely(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setenv("INGEST_TRANSPORT", "parsed_streams")

    assert check_config_main(["--live"]) == 2
    assert "Configuration invalid:" in capsys.readouterr().err
