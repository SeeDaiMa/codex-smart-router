from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMART_ROUTER_", env_file=".env", extra="ignore")

    api_key: SecretStr | None = None
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"
    chatgpt_base_url: str = "https://chatgpt.com/backend-api/codex"
    upstream_base_url: str = "http://127.0.0.1:4001/v1"
    upstream_api_key: SecretStr | None = None
    upstream_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    classifier_timeout_seconds: float = Field(default=8, gt=0, le=60)
    classifier_enabled: bool = False
    classifier_model: str = "gpt-6-luna"
    project_roots: list[Path] = Field(default_factory=list)
    rules_file: Path = PROJECT_DIR / "router" / "rules.yaml"
    models_file: Path = PROJECT_DIR / "providers" / "models.yaml"
    catalog_file: Path = PROJECT_DIR / "config" / "codex-models.json"
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    max_scan_files: int = Field(default=5000, ge=1, le=100000)
    route_ttl_seconds: int = Field(default=7200, ge=1)
    route_capacity: int = Field(default=4096, ge=1)

    @field_validator("upstream_base_url", "chatgpt_base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("upstream_base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not put credentials, query strings or fragments in the URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Non-loopback upstreams require HTTPS")
        return value.rstrip("/")

    @field_validator("chatgpt_base_url")
    @classmethod
    def validate_chatgpt_destination(cls, value: str) -> str:
        if urlparse(value).hostname not in {"chatgpt.com", "127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "ChatGPT credentials may only go to the official Codex backend or loopback"
            )
        return value

    @property
    def inference_base_url(self) -> str:
        return self.chatgpt_base_url if self.auth_mode == "chatgpt" else self.upstream_base_url
