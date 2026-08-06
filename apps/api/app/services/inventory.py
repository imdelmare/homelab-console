"""Declarative inventory loaded from the homelab config YAML.

The inventory is the only source of network targets: no tool may accept an
arbitrary URL, address or hostname from a client or a model.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.settings import get_settings

CLOUDFLARE_API_BASE_URL = "https://api.cloudflare.com/client/v4"


class HostEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    address: str = ""
    kind: str = ""
    tags: list[str] = []
    # Ports that network.host.check may probe on this host.
    check_ports: list[int] = []


class DnsResolverEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    address: str = ""
    port: int = 53


class DnsTargetEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    domain: str
    expected_addresses: list[str] = []
    internal: bool = False


class DependencyEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    label: str = ""
    # "provider" nodes map to a real provider_id; "path" nodes are synthetic
    # (e.g. a network path) used only for root-cause narration, never passed
    # to provider/tool lookups.
    kind: str = "provider"
    depends_on: list[str] = []


class TopologyNodeEntry(BaseModel):
    """Declared topology node.

    Unlike the legacy dependency graph, layout and relationship semantics are
    explicit. ``provider_id`` only links the node to health telemetry; it does
    not imply that the provider hosts the node.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    label: str = ""
    kind: str = "service"
    layer: str = "services"
    provider_id: str = ""
    observation_id: str = ""
    availability_monitor: str = ""
    inherit_provider_status: bool = True
    incident_watcher_ids: list[str] = Field(default_factory=list)
    role: str = ""
    group: str = ""
    parent_id: str = ""
    guest_vmid: int | None = None
    guest_match: list[str] = Field(default_factory=list)


class TopologyEdgeEntry(BaseModel):
    """Typed relationship between two topology nodes."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    source: str
    target: str
    kind: str = "connects"
    label: str = ""
    affects_rca: bool = True
    availability_group: str = ""


class TopologyAvailabilityGroupEntry(BaseModel):
    """Availability semantics for a set of equivalent upstream edges."""

    model_config = ConfigDict(extra="ignore")

    id: str
    label: str = ""
    mode: str = "all"


class HttpTargetEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    url: str
    expected_statuses: list[int] = []
    kind: str = ""
    owner: str = ""


class TlsTargetEntry(BaseModel):
    """A declared TLS endpoint whose certificate expiry the console observes."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    host: str
    port: int = Field(default=443, ge=1, le=65535)
    server_name: str = ""
    warning_days: int = Field(default=21, ge=1, le=365)
    critical_days: int = Field(default=7, ge=0, le=90)


class ApiProviderInstanceEntry(BaseModel):
    """A server-declared instance of one allowlisted, narrow API driver."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    name: str = ""
    driver: Literal["json_health_v1", "cloudflare_tunnel_v1", "speedtest_probe_v1"]
    base_url: str = ""
    account_id: str = ""
    tunnel_id: str = ""
    verify_tls: bool = True
    timeout_seconds: float = Field(default=5.0, ge=0.5, le=180.0)

    @model_validator(mode="after")
    def validate_driver_contract(self) -> "ApiProviderInstanceEntry":
        if self.driver == "cloudflare_tunnel_v1":
            if self.base_url and self.base_url.rstrip("/") != CLOUDFLARE_API_BASE_URL:
                raise ValueError("cloudflare_tunnel_v1 uses the fixed Cloudflare API base URL")
            if not self.verify_tls:
                raise ValueError("cloudflare_tunnel_v1 requires TLS verification")
            if not self.account_id or len(self.account_id) > 32 or not self.account_id.isalnum():
                raise ValueError(
                    "account_id must be a 1-32 character Cloudflare account identifier"
                )
            try:
                normalized_tunnel_id = str(UUID(self.tunnel_id))
            except (ValueError, AttributeError):
                raise ValueError("tunnel_id must be a UUID") from None
            self.base_url = CLOUDFLARE_API_BASE_URL
            self.tunnel_id = normalized_tunnel_id
            return self

        if self.account_id or self.tunnel_id:
            raise ValueError("account_id and tunnel_id are only valid for cloudflare_tunnel_v1")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must use HTTP or HTTPS")
        if not parsed.hostname:
            raise ValueError("base_url must include a hostname")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials")
        if self.driver == "speedtest_probe_v1" and not self.verify_tls:
            host = (parsed.hostname or "").lower()
            if not (
                host == "localhost"
                or host == "127.0.0.1"
                or host.startswith("10.")
                or host.startswith("192.168.")
                or host.endswith(".internal")
            ):
                raise ValueError(
                    "speedtest_probe_v1 may disable TLS verification only for a private endpoint"
                )
        if self.driver != "speedtest_probe_v1" and self.timeout_seconds > 30:
            raise ValueError("timeout_seconds may exceed 30 only for speedtest_probe_v1")
        self.base_url = self.base_url.rstrip("/")
        return self


def _project_path(path: str) -> Path:
    """HOMELAB_CONFIG_PATH/SECRETS_PATH are absolute in every real
    deployment (see deploy/docker-compose.yml) — resolve those directly.
    The parents[4] fallback below only serves relative dev-config paths,
    and only holds for the repo's on-disk layout (apps/api/app/services/);
    the container's flattened /app/app/services tree is too shallow for
    it, so guard against IndexError instead of crashing startup."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    parents = Path(__file__).resolve().parents
    root = parents[4] if len(parents) > 4 else parents[-1]
    return (root / candidate).resolve()


def config_path() -> Path:
    """Resolved path of HOMELAB_CONFIG_PATH, absolute or relative to the
    repo root. Exposed so startup guards can fail fast on a missing file."""
    return _project_path(get_settings().homelab_config_path)


_cached_path = ""
_cached_signature: tuple[int, int] | None = None
_cached_raw: dict[str, Any] = {}
_cached_error = ""
_cached_loaded_at: datetime | None = None


def _validate_raw(raw: dict[str, Any]) -> None:
    """Validate every typed inventory section before swapping the live copy."""
    for item in raw.get("hosts", []) or []:
        HostEntry(**item)
    dns = raw.get("dns", {}) or {}
    for item in dns.get("resolvers", []) or []:
        DnsResolverEntry(**item)
    for item in dns.get("targets", []) or []:
        DnsTargetEntry(**item)
    http = raw.get("http", {}) or {}
    for item in http.get("targets", []) or []:
        HttpTargetEntry(**item)
    tls = raw.get("tls", {}) or {}
    for item in tls.get("certificate_targets", []) or []:
        TlsTargetEntry(**item)
    instances = [ApiProviderInstanceEntry(**item) for item in raw.get("api_provider_instances", []) or []]
    instance_ids = [instance.id for instance in instances]
    if len(instance_ids) != len(set(instance_ids)):
        raise ValueError("api_provider_instances contains duplicate ids")
    dependencies = raw.get("dependencies", {}) or {}
    for item in dependencies.get("nodes", []) or []:
        DependencyEntry(**item)
    topology = raw.get("topology", {}) or {}
    for item in topology.get("availability_groups", []) or []:
        TopologyAvailabilityGroupEntry(**item)
    for item in topology.get("nodes", []) or []:
        TopologyNodeEntry(**item)
    for item in topology.get("edges", []) or []:
        TopologyEdgeEntry(**item)


def _load_raw() -> dict[str, Any]:
    global _cached_path, _cached_signature, _cached_raw, _cached_error, _cached_loaded_at
    path = config_path()
    if not path.exists():
        _cached_error = "inventory file is missing"
        return {}
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    if str(path) == _cached_path and signature == _cached_signature:
        return _cached_raw
    try:
        with path.open("r", encoding="utf-8") as handle:
            candidate = yaml.safe_load(handle) or {}
        if not isinstance(candidate, dict):
            raise ValueError("inventory root must be a mapping")
        _validate_raw(candidate)
    except Exception as exc:
        _cached_path = str(path)
        _cached_signature = signature
        _cached_error = f"{exc.__class__.__name__}: {exc}"
        if _cached_raw:
            return _cached_raw
        raise
    _cached_path = str(path)
    _cached_signature = signature
    _cached_raw = candidate
    _cached_error = ""
    _cached_loaded_at = datetime.now(UTC)
    return _cached_raw


def clear_cache() -> None:
    global _cached_path, _cached_signature, _cached_raw, _cached_error, _cached_loaded_at
    _cached_path = ""
    _cached_signature = None
    _cached_raw = {}
    _cached_error = ""
    _cached_loaded_at = None


def inventory_status() -> dict[str, Any]:
    _load_raw()
    return {
        "status": "stale" if _cached_error and _cached_raw else "error" if _cached_error else "fresh",
        "loaded_at": _cached_loaded_at.isoformat() if _cached_loaded_at else None,
        "version": f"{_cached_signature[0]}:{_cached_signature[1]}" if _cached_signature else "",
        "warning": _cached_error,
    }


def list_hosts() -> list[HostEntry]:
    return [HostEntry(**item) for item in _load_raw().get("hosts", [])]


def get_host(host_id: str) -> HostEntry | None:
    return next((host for host in list_hosts() if host.id == host_id), None)


def list_dns_resolvers() -> list[DnsResolverEntry]:
    dns = _load_raw().get("dns", {}) or {}
    return [DnsResolverEntry(**item) for item in dns.get("resolvers", [])]


def get_dns_resolver(resolver_id: str) -> DnsResolverEntry | None:
    return next((resolver for resolver in list_dns_resolvers() if resolver.id == resolver_id), None)


def list_dns_targets() -> list[DnsTargetEntry]:
    dns = _load_raw().get("dns", {}) or {}
    return [DnsTargetEntry(**item) for item in dns.get("targets", [])]


def get_dns_target(target_id: str) -> DnsTargetEntry | None:
    return next((target for target in list_dns_targets() if target.id == target_id), None)


def list_http_targets() -> list[HttpTargetEntry]:
    http = _load_raw().get("http", {}) or {}
    return [HttpTargetEntry(**item) for item in http.get("targets", [])]


def list_tls_targets() -> list[TlsTargetEntry]:
    tls = _load_raw().get("tls", {}) or {}
    return [TlsTargetEntry(**item) for item in tls.get("certificate_targets", [])]


def list_api_provider_instances() -> list[ApiProviderInstanceEntry]:
    instances = [
        ApiProviderInstanceEntry(**item)
        for item in _load_raw().get("api_provider_instances", [])
    ]
    ids = [instance.id for instance in instances]
    if len(ids) != len(set(ids)):
        raise ValueError("api_provider_instances contains duplicate ids")
    return instances


def get_api_provider_instance(instance_id: str) -> ApiProviderInstanceEntry | None:
    return next(
        (instance for instance in list_api_provider_instances() if instance.id == instance_id),
        None,
    )


def get_http_target(target_id: str) -> HttpTargetEntry | None:
    return next((target for target in list_http_targets() if target.id == target_id), None)


def list_dependencies() -> list[DependencyEntry]:
    dependencies = _load_raw().get("dependencies", {}) or {}
    return [DependencyEntry(**item) for item in dependencies.get("nodes", [])]


def get_dependency(node_id: str) -> DependencyEntry | None:
    return next((node for node in list_dependencies() if node.id == node_id), None)


def list_topology_nodes() -> list[TopologyNodeEntry]:
    topology = _load_raw().get("topology", {}) or {}
    return [TopologyNodeEntry(**item) for item in topology.get("nodes", [])]


def list_topology_edges() -> list[TopologyEdgeEntry]:
    topology = _load_raw().get("topology", {}) or {}
    return [TopologyEdgeEntry(**item) for item in topology.get("edges", [])]


def list_topology_availability_groups() -> list[TopologyAvailabilityGroupEntry]:
    topology = _load_raw().get("topology", {}) or {}
    return [
        TopologyAvailabilityGroupEntry(**item)
        for item in topology.get("availability_groups", [])
    ]


def provider_config(provider_id: str) -> dict[str, Any]:
    """Non-secret provider connection settings from the homelab config."""
    providers = _load_raw().get("providers", {}) or {}
    config = providers.get(provider_id, {}) or {}
    return config if isinstance(config, dict) else {}


def tool_overrides() -> dict[str, dict[str, Any]]:
    """Optional per-tool overrides from YAML, e.g. {tool_id: {enabled: false}}."""
    return _load_raw().get("tool_overrides", {}) or {}


def medium_risk_allowlist() -> list[str]:
    """Tools of medium risk must be explicitly allowed here to run."""
    return _load_raw().get("policy", {}).get("allow_medium_risk", []) or []
