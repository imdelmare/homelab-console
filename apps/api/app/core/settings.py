from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Homelab Console"
    app_env: Literal["live", "test"] = "live"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "postgresql+psycopg://example:example@127.0.0.1:5432/console"
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_timeout_seconds: float = 30.0
    database_pool_recycle_seconds: int = 1800

    session_secret: str = "change-me-with-a-long-random-value"
    session_ttl_minutes: int = 720
    cookie_secure: str = "auto"  # auto | true | false; auto = true in live
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    trusted_proxy_enabled: bool = False
    allow_insecure_local_tls: bool = False

    auth_notification_adapter: str = "telegram"  # test | telegram
    auth_login_mode: Literal["password", "telegram_only"] = "password"
    auth_recovery_enabled: bool = True
    approval_ttl_seconds: int = 600
    login_challenge_ttl_seconds: int = 300
    login_challenge_max_attempts: int = 5
    otp_length: int = 8

    # Test-only bootstrap: creates the first user when the user table is empty.
    # It is rejected by the live runtime.
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_user_id: str = ""
    telegram_allowed_chat_id: str = ""
    telegram_webhook_secret: str = ""
    telegram_notifications_enabled: bool = False
    notification_outbox_enabled: bool = True
    notification_worker_interval_seconds: float = 2.0
    notification_warning_debounce_seconds: int = 900
    notification_cooldown_seconds: int = 7200
    notification_critical_debounce_seconds: int = 120
    notification_critical_cooldown_seconds: int = 1800
    notification_aggregation_window_seconds: int = 120
    notification_max_attempts: int = 5
    operational_retention_enabled: bool = True
    retention_interval_seconds: int = 3600
    audit_retention_days: int = 180
    tool_invocation_retention_days: int = 90
    watcher_run_retention_days: int = 90
    notification_outbox_retention_days: int = 90
    retention_batch_size: int = 1000

    homelab_config_path: str = "config/homelab.example.yml"
    secrets_path: str = "config/secrets.local.yml"
    runbooks_config_path: str = "config/runbooks.example.yml"

    audit_jsonl_enabled: bool = False
    audit_jsonl_path: str = "data/audit.jsonl"

    mcp_agent_id: str = "codex"
    mcp_client_token_path: str = ""
    mcp_client_label: str = ""
    mcp_pairing_timeout_seconds: int = 300
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8765
    mcp_http_path: str = "/mcp/"
    mcp_http_allowed_hosts: str = "127.0.0.1:8765,localhost:8765"
    mcp_http_allowed_origins: str = ""

    # Narrow control-plane dispatch to the dedicated OpenCode-powered Fixer.
    fixer_dispatch_enabled: bool = False
    fixer_dispatch_url: str = "http://127.0.0.1:8767/fixer"
    fixer_dispatch_secret: str = ""
    fixer_dispatch_timeout_seconds: float = 5.0

    openai_api_key: str = ""
    conversation_provider: Literal["openai", "ollama", "ai_manager", "opencode_go"] = "ollama"
    conversation_model: str = "gpt-5.6-luna"
    opencode_go_api_key: str = ""
    opencode_go_chat_model: Literal["grok-4.5", "deepseek-v4-flash"] = "deepseek-v4-flash"
    opencode_go_router_model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    opencode_go_max_attempts: int = 3
    conversation_reasoning_effort: str = "low"
    conversation_max_turns: int = 4
    conversation_max_tool_calls: int = 3
    conversation_max_output_tokens: int = 600
    conversation_timeout_seconds: float = 60.0
    conversation_input_cost_per_million: float = 0.0
    conversation_output_cost_per_million: float = 0.0
    ai_manager_host_id: str = "ai-host"
    ai_manager_port: int = 8080
    ai_manager_model: str = "Qwen3.5-4B-Q8_0"
    ai_manager_connect_timeout_seconds: float = 2.0
    ai_manager_timeout_seconds: float = 60.0
    ai_manager_failure_cooldown_seconds: float = 90.0
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4-e4b-agentic:latest"
    ollama_connect_timeout_seconds: float = 2.0
    ollama_timeout_seconds: float = 90.0
    ollama_failure_cooldown_seconds: float = 90.0
    ollama_keep_alive: str = "10m"
    telegram_media_enabled: bool = True
    telegram_media_max_image_bytes: int = 8_000_000
    telegram_media_max_audio_bytes: int = 10_000_000
    telegram_media_max_audio_seconds: int = 120

    task_router_enabled: bool = False
    task_router_provider: Literal["", "openai", "ai_manager", "opencode_go"] = ""
    task_router_model: str = ""
    task_router_reasoning_effort: str = "low"
    task_router_max_output_tokens: int = 1000
    task_router_timeout_seconds: float = 30.0
    task_router_worker_interval_seconds: float = 1.0
    task_router_job_lease_seconds: int = 300
    task_router_max_attempts: int = 3
    incident_matcher_enabled: bool = True
    incident_matcher_max_candidates: int = 5
    incident_matcher_max_calls_per_hour: int = 10
    incident_matcher_auto_handle_confidence: float = 0.9

    watchers_enabled: bool = True
    watchers_interval_seconds: int = 300
    watchers_min_severity: str = "warning"  # warning | critical
    watchers_ignore_patterns: str = ""
    watchers_resolve_after_missing_runs: int = 3

    model_config = SettingsConfigDict(
        env_file="../../.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_live(self) -> bool:
        return self.app_env == "live"

    @property
    def cookie_secure_flag(self) -> bool:
        if self.cookie_secure == "auto":
            return self.is_live
        return self.cookie_secure.lower() == "true"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def watchers_ignore_pattern_list(self) -> list[str]:
        return [item.strip().lower() for item in self.watchers_ignore_patterns.split(",") if item.strip()]

    @property
    def mcp_http_allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.mcp_http_allowed_hosts.split(",") if host.strip()]

    @property
    def mcp_http_allowed_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.mcp_http_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
