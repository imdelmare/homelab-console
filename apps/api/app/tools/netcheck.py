"""Bounded TCP reachability check against inventory hosts only.

This replaces the old arbitrary host.ping / http.health tools. Targets are
resolved from the inventory by ID; ports and timeouts are strictly bounded.
"""

import asyncio
import ssl
from datetime import UTC, datetime

from cryptography import x509

from app.services.inventory import TlsTargetEntry, get_host, list_hosts, list_tls_targets

DEFAULT_PORTS = [22, 80, 443]
MAX_PORTS = 10
TLS_PROBE_TIMEOUT = 10.0


async def _probe(address: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def host_check(host_id: str, ports: list[int] | None, timeout: float) -> dict:
    host = get_host(host_id)
    if host is None:
        # Unknown inventory ID is an input error surfaced as a normal result:
        # the ID space is public to authenticated operators.
        return {"ok": False, "error": "unknown_host_id", "host_id": host_id}

    selected = ports or host.check_ports or DEFAULT_PORTS
    selected = selected[:MAX_PORTS]

    results = []
    for port in selected:
        reachable = await _probe(host.address, port, timeout)
        results.append({"port": port, "open": reachable})

    return {
        "ok": any(item["open"] for item in results),
        "host_id": host.id,
        "host_name": host.name,
        "checks": results,
    }


def _tls_observation_context() -> ssl.SSLContext:
    # Observation-only probe: the goal is reading the certificate's validity
    # window even when it is already expired or self-signed, which a verifying
    # handshake would abort before the certificate is available. Nothing is
    # trusted or transmitted over this connection.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _probe_certificate(target: TlsTargetEntry) -> dict:
    result: dict = {
        "id": target.id,
        "name": target.name or target.id,
        "port": target.port,
        "ok": False,
        "not_before": None,
        "not_after": None,
        "days_until_expiry": None,
        "warning_days": target.warning_days,
        "critical_days": target.critical_days,
        "error": None,
    }
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                target.host,
                target.port,
                ssl=_tls_observation_context(),
                server_hostname=target.server_name or target.host,
            ),
            TLS_PROBE_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
        result["error"] = "timeout" if isinstance(exc, asyncio.TimeoutError) else "unreachable"
        return result
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if not der:
            result["error"] = "no_certificate"
            return result
        certificate = x509.load_der_x509_certificate(der)
        not_before = certificate.not_valid_before_utc
        not_after = certificate.not_valid_after_utc
        result["ok"] = True
        result["not_before"] = not_before.isoformat()
        result["not_after"] = not_after.isoformat()
        result["days_until_expiry"] = round(
            (not_after - datetime.now(UTC)).total_seconds() / 86400, 1
        )
    except (ValueError, ssl.SSLError):
        result["error"] = "invalid_certificate"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return result


async def tls_certificates() -> dict:
    """Read certificate validity for declared TLS targets only.

    Targets come exclusively from the server-side inventory; REST/MCP callers
    cannot supply a host or port.
    """
    targets = list_tls_targets()
    certificates = list(await asyncio.gather(*(_probe_certificate(target) for target in targets)))
    expiring = [
        item["id"]
        for item in certificates
        if item["days_until_expiry"] is not None
        and item["days_until_expiry"] <= item["warning_days"]
    ]
    return {
        "certificates": certificates,
        "total": len(certificates),
        "unreachable": [item["id"] for item in certificates if not item["ok"]],
        "expiring": expiring,
        "source": "inventory",
    }


async def clients_list(kind: str | None = None, tag: str | None = None, limit: int = 100) -> dict:
    """List configured network clients from inventory only.

    This is deliberately not a LAN scan or ARP lookup: the inventory remains
    the boundary for network targets exposed to agents.
    """
    hosts = list_hosts()
    if kind:
        hosts = [host for host in hosts if host.kind == kind]
    if tag:
        hosts = [host for host in hosts if tag in host.tags]
    hosts = hosts[:limit]

    by_kind: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    clients = []
    for host in hosts:
        by_kind[host.kind or "unknown"] = by_kind.get(host.kind or "unknown", 0) + 1
        for host_tag in host.tags:
            by_tag[host_tag] = by_tag.get(host_tag, 0) + 1
        clients.append(
            {
                "id": host.id,
                "name": host.name,
                "address": host.address,
                "kind": host.kind,
                "tags": host.tags,
                "check_ports": host.check_ports,
            }
        )

    return {
        "clients": clients,
        "total": len(clients),
        "by_kind": by_kind,
        "by_tag": by_tag,
        "source": "inventory",
    }
