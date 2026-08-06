"""Bounded DNS diagnostics.

DNS tools only query configured targets/resolvers from homelab.yml. They never
accept arbitrary hostnames or resolver addresses from callers.
"""

from __future__ import annotations

import asyncio
import random
import socket
import struct
from typing import Any, cast

from app.providers.adguard import tools as adguard_tools
from app.providers.errors import ProviderError
from app.services.inventory import DnsResolverEntry, DnsTargetEntry, get_dns_resolver, get_dns_target, list_dns_resolvers, list_dns_targets

MAX_DNS_TARGETS = 12
MAX_DNS_RESOLVERS = 6
DNS_TIMEOUT_SECONDS = 2.0


def _encode_name(domain: str) -> bytes:
    labels = domain.rstrip(".").split(".")
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"


def _build_query(domain: str, query_id: int) -> bytes:
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    question = _encode_name(domain) + struct.pack("!HH", 1, 1)
    return header + question


def _skip_name(packet: bytes, offset: int) -> int:
    while True:
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += length + 1


def _parse_a_records(packet: bytes, expected_id: int) -> tuple[list[str], int]:
    if len(packet) < 12:
        return [], 0
    query_id, _flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", packet[:12])
    if query_id != expected_id:
        return [], 0
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(packet, offset) + 4
    records = []
    for _ in range(ancount):
        offset = _skip_name(packet, offset)
        if offset + 10 > len(packet):
            break
        rtype, rclass, _ttl, rdlength = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        data = packet[offset:offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and rdlength == 4:
            records.append(socket.inet_ntoa(data))
    return sorted(set(records)), ancount


async def _udp_query(domain: str, resolver: DnsResolverEntry, timeout: float = DNS_TIMEOUT_SECONDS) -> dict[str, Any]:
    query_id = random.randint(1, 65535)
    packet = _build_query(domain, query_id)
    loop = asyncio.get_running_loop()

    def _blocking_query() -> tuple[list[str], int]:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (resolver.address, resolver.port))
            response, _ = sock.recvfrom(4096)
            return _parse_a_records(response, query_id)

    try:
        addresses, answer_count = await loop.run_in_executor(None, _blocking_query)
        return {
            "resolver_id": resolver.id,
            "resolver": resolver.address,
            "ok": bool(addresses),
            "error": "" if addresses else "no_a_records",
            "addresses": addresses,
            "answer_count": answer_count,
        }
    except socket.timeout:
        return {"resolver_id": resolver.id, "resolver": resolver.address, "ok": False, "error": "timeout", "addresses": []}
    except OSError as exc:
        return {"resolver_id": resolver.id, "resolver": resolver.address, "ok": False, "error": exc.__class__.__name__, "addresses": []}


async def _system_resolve(domain: str, timeout: float = DNS_TIMEOUT_SECONDS) -> dict[str, Any]:
    async def _resolve() -> list[str]:
        infos = await asyncio.to_thread(socket.getaddrinfo, domain, None, socket.AF_INET, socket.SOCK_STREAM)
        return sorted({cast(str, info[4][0]) for info in infos})

    try:
        addresses = await asyncio.wait_for(_resolve(), timeout)
        return {
            "resolver_id": "system",
            "resolver": "system",
            "ok": bool(addresses),
            "error": "" if addresses else "no_a_records",
            "addresses": addresses,
            "answer_count": len(addresses),
        }
    except asyncio.TimeoutError:
        return {"resolver_id": "system", "resolver": "system", "ok": False, "error": "timeout", "addresses": []}
    except socket.gaierror as exc:
        return {"resolver_id": "system", "resolver": "system", "ok": False, "error": exc.__class__.__name__, "addresses": []}


def _expected_match(target: DnsTargetEntry, addresses: list[str]) -> bool | None:
    if not target.expected_addresses:
        return None
    return sorted(target.expected_addresses) == sorted(addresses)


def _public_target(target: DnsTargetEntry) -> dict[str, Any]:
    return {
        "target_id": target.id,
        "name": target.name,
        "domain": target.domain,
        "internal": target.internal,
        "expected_addresses": target.expected_addresses,
    }


async def dns_resolve(target_id: str, resolver_id: str | None = None) -> dict:
    target = get_dns_target(target_id)
    if target is None:
        return {"ok": False, "error": "unknown_dns_target", "target_id": target_id}
    resolvers = list_dns_resolvers()[:MAX_DNS_RESOLVERS]
    if resolver_id:
        resolver = get_dns_resolver(resolver_id)
        if resolver is None:
            return {"ok": False, "error": "unknown_dns_resolver", "resolver_id": resolver_id}
        resolvers = [resolver]
    checks = await asyncio.gather(*(_udp_query(target.domain, resolver) for resolver in resolvers))
    system = await _system_resolve(target.domain)
    rows = [system, *checks]
    for row in rows:
        row["matches_expected"] = _expected_match(target, row.get("addresses", []))
    return {
        "ok": any(row.get("ok") for row in rows),
        "target": _public_target(target),
        "results": rows,
    }


async def dns_path_check(target_id: str) -> dict:
    result = await dns_resolve(target_id)
    if not result.get("ok"):
        return result
    rows = result["results"]
    successful = [row for row in rows if row.get("ok")]
    address_sets = {tuple(row.get("addresses", [])) for row in successful}
    expected_mismatches = [
        row["resolver_id"] for row in successful if row.get("matches_expected") is False
    ]
    return {
        **result,
        "diagnosis": {
            "successful_resolvers": len(successful),
            "failed_resolvers": len(rows) - len(successful),
            "split_horizon": len(address_sets) > 1,
            "expected_mismatches": expected_mismatches,
        },
    }


async def adguard_dns_health() -> dict:
    try:
        status, stats = await asyncio.gather(adguard_tools.status(), adguard_tools.stats())
    except ProviderError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.message}
    state = status.get("status", {})
    dns_stats = stats.get("stats", {})
    return {
        "ok": bool(state.get("running")) and bool(state.get("protection_enabled")),
        "running": state.get("running"),
        "protection_enabled": state.get("protection_enabled"),
        "dns_addresses": state.get("dns_addresses", []),
        "dns_port": state.get("dns_port"),
        "dns_queries": dns_stats.get("dns_queries"),
        "blocked_filtering": dns_stats.get("blocked_filtering"),
        "avg_processing_time_ms": dns_stats.get("avg_processing_time_ms"),
    }


async def dns_summary() -> dict:
    targets = list_dns_targets()[:MAX_DNS_TARGETS]
    checks = await asyncio.gather(*(dns_path_check(target.id) for target in targets))
    adguard = await adguard_dns_health()
    findings = []
    for check in checks:
        target = check.get("target", {})
        diagnosis = check.get("diagnosis", {})
        if not check.get("ok"):
            findings.append({"severity": "critical", "message": f"{target.get('domain', check.get('target_id'))} did not resolve"})
            continue
        if diagnosis.get("failed_resolvers"):
            findings.append({"severity": "warning", "message": f"{target.get('domain')} failed on {diagnosis.get('failed_resolvers')} resolver(s)"})
        if diagnosis.get("expected_mismatches"):
            findings.append({"severity": "critical", "message": f"{target.get('domain')} differs from expected address(es)"})
    if not adguard.get("ok"):
        findings.append({"severity": "critical", "message": "AdGuard DNS protection is not healthy"})
    severity = "critical" if any(item["severity"] == "critical" for item in findings) else "warning" if findings else "ok"
    return {
        "summary": {
            "provider_id": "lab.dns",
            "status": "healthy" if severity == "ok" else "degraded",
            "severity": severity,
            "metrics": {
                "targets_total": len(targets),
                "resolvers_total": len(list_dns_resolvers()),
                "targets_ok": len([item for item in checks if item.get("ok")]),
                "adguard_ok": adguard.get("ok"),
                "findings_total": len(findings),
            },
            "findings": findings[:20],
            "next_actions": [f"Check: {item['message']}" for item in findings[:5]],
        },
        "adguard": adguard,
        "checks": checks,
    }
