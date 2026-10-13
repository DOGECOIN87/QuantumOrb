"""Validated, secret-safe application configuration for demo and live modes."""

from __future__ import annotations

import ipaddress
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IngestMode = Literal["demo", "live"]
IngestTransport = Literal[
    "parsed_streams",
    "laserstream",
    "logs_subscribe",
    "parsed_events",
    "fixture",
]
SolanaNetwork = Literal["mainnet-beta", "devnet", "testnet"]

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ConfigurationError(ValueError):
    """Safe configuration diagnostic that never includes supplied values."""


def _validate_base58_address(value: str) -> str:
    if not 32 <= len(value) <= 44 or any(character not in BASE58_ALPHABET for character in value):
        raise ValueError("must be a valid Base58 Solana address")

    decoded_number = 0
    for character in value:
        decoded_number = decoded_number * 58 + BASE58_ALPHABET.index(character)

    decoded_bytes = (decoded_number.bit_length() + 7) // 8
    leading_zero_bytes = len(value) - len(value.lstrip("1"))
    if decoded_bytes + leading_zero_bytes != 32:
        raise ValueError("must decode to a 32-byte Solana address")
    return value


def _validate_provider_url(value: str, *, required_scheme: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("must be a well-formed provider URL") from None
    if (
        parsed.scheme.lower() != required_scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(
            f"must be a valid {required_scheme.upper()} URL without credentials, query, or fragment"
        )
    return value


class Settings(BaseSettings):
    """Typed settings loaded from environment variables and an optional local `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
        validate_default=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)

    solana_network: SolanaNetwork = "mainnet-beta"
    ingest_mode: IngestMode = "demo"
    ingest_transport: IngestTransport = "fixture"

    helius_api_key: SecretStr | None = None
    helius_rpc_url: str = "https://mainnet.helius-rpc.com/"
    helius_parsed_streams_url: str = "wss://fs-beta.helius-rpc.com/"

    pump_program_id: str = Field(
        default="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        validation_alias="PUMPFUN_PROGRAM_ID",
    )
    pump_amm_program_id: str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

    poll_interval_seconds: int = Field(default=15, ge=1, le=3600)
    backfill_page_size: int = Field(default=100, ge=1, le=1000)
    max_concurrent_enrichments: int = Field(default=3, ge=1, le=3)
    cartographer_max_history_tx: int = Field(default=1000, ge=1, le=1000)
    cartographer_max_wallets_per_token: int = Field(default=200, ge=1, le=200)
    cartographer_max_counterparties_per_wallet: int = Field(default=100, ge=1, le=100)
    cartographer_lookback_days: int = Field(default=30, ge=1, le=30)

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_app_env_aliases(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        aliases = {"dev": "development", "testing": "test", "prod": "production"}
        return aliases.get(normalized, normalized)

    @field_validator("app_host")
    @classmethod
    def require_local_host(cls, value: str) -> str:
        if value == "localhost":
            return value
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError("must be localhost or a loopback IP address") from None
        if not address.is_loopback:
            raise ValueError("must be localhost or a loopback IP address")
        return value

    @field_validator("helius_rpc_url")
    @classmethod
    def require_rpc_url_without_secrets(cls, value: str) -> str:
        return _validate_provider_url(value, required_scheme="https")

    @field_validator("helius_parsed_streams_url")
    @classmethod
    def require_stream_url_without_secrets(cls, value: str) -> str:
        return _validate_provider_url(value, required_scheme="wss")

    @field_validator("pump_program_id", "pump_amm_program_id")
    @classmethod
    def require_valid_program_id(cls, value: str) -> str:
        return _validate_base58_address(value)

    @model_validator(mode="after")
    def validate_mode_and_transport(self) -> Self:
        key_value = (
            self.helius_api_key.get_secret_value().strip()
            if self.helius_api_key is not None
            else ""
        )
        if key_value and (
            key_value in self.helius_rpc_url or key_value in self.helius_parsed_streams_url
        ):
            raise ValueError("HELIUS_API_KEY must not be embedded in provider URLs")
        if self.ingest_mode == "live":
            if not key_value:
                raise ValueError("HELIUS_API_KEY is required in live mode")
            if self.ingest_transport == "fixture":
                raise ValueError("live mode requires a provider transport")
        elif self.ingest_transport != "fixture":
            raise ValueError("demo mode requires INGEST_TRANSPORT=fixture")
        return self

    def safe_diagnostics(self) -> dict[str, str | int]:
        """Return useful configuration details while redacting the provider key."""
        key_is_set = bool(
            self.helius_api_key is not None and self.helius_api_key.get_secret_value().strip()
        )
        return {
            "app_env": self.app_env,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "solana_network": self.solana_network,
            "ingest_mode": self.ingest_mode,
            "ingest_transport": self.ingest_transport,
            "helius_api_key": "[set]" if key_is_set else "[missing]",
            "helius_rpc_url": self.helius_rpc_url,
            "helius_parsed_streams_url": self.helius_parsed_streams_url,
            "pump_program_id": self.pump_program_id,
            "pump_amm_program_id": self.pump_amm_program_id,
            "poll_interval_seconds": self.poll_interval_seconds,
            "backfill_page_size": self.backfill_page_size,
            "max_concurrent_enrichments": self.max_concurrent_enrichments,
            "cartographer_max_history_tx": self.cartographer_max_history_tx,
            "cartographer_max_wallets_per_token": self.cartographer_max_wallets_per_token,
            "cartographer_max_counterparties_per_wallet": (
                self.cartographer_max_counterparties_per_wallet
            ),
            "cartographer_lookback_days": self.cartographer_lookback_days,
        }


def load_settings(*, mode_override: IngestMode | None = None) -> Settings:
    """Load settings and replace Pydantic errors with value-free diagnostics."""
    try:
        if mode_override is None:
            return Settings()
        return Settings(ingest_mode=mode_override)
    except ValidationError as error:
        problems: list[str] = []
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            field = location or "cross-field settings"
            error_type = str(item.get("type", "invalid"))
            problems.append(f"{field} ({error_type})")
        summary = "; ".join(problems) or "unknown validation error"
        raise ConfigurationError(f"Invalid configuration: {summary}") from None
