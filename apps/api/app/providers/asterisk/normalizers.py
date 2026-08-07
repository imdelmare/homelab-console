"""Convert raw Asterisk AMI blocks into normalized internal models."""

from typing import Any

from app.providers.asterisk.models import ChannelInfo, CoreInfo, PjsipEndpoint, SipPeer


def normalize_core(status: dict[str, Any], settings: dict[str, Any]) -> CoreInfo:
    return CoreInfo(
        version=settings.get("AsteriskVersion", ""),
        ami_version=settings.get("AMIversion", ""),
        startup_time=f"{status.get('CoreStartupDate', '')} {status.get('CoreStartupTime', '')}".strip(),
        reload_time=f"{status.get('CoreReloadDate', '')} {status.get('CoreReloadTime', '')}".strip(),
        current_calls=status.get("CoreCurrentCalls"),
        max_calls=settings.get("CoreMaxCalls"),
    )


def normalize_channel(block: dict[str, Any]) -> ChannelInfo:
    return ChannelInfo(
        channel=block.get("Channel", ""),
        state=block.get("ChannelStateDesc", ""),
        caller_number=block.get("CallerIDNum", ""),
        connected_number=block.get("ConnectedLineNum", ""),
        application=block.get("Application", ""),
        duration=block.get("Duration", ""),
        bridge_id=block.get("BridgeId", ""),
    )


def normalize_channels(events: list[dict[str, Any]]) -> list[ChannelInfo]:
    return [
        normalize_channel(block)
        for block in events
        if block.get("Event") == "CoreShowChannel"
    ]


def normalize_pjsip_endpoint(block: dict[str, Any]) -> PjsipEndpoint:
    return PjsipEndpoint(
        endpoint=block.get("ObjectName", ""),
        state=block.get("DeviceState", ""),
        contacts=block.get("Contacts", ""),
        transport=block.get("Transport", ""),
    )


def normalize_pjsip_endpoints(events: list[dict[str, Any]]) -> list[PjsipEndpoint]:
    return [
        normalize_pjsip_endpoint(block)
        for block in events
        if block.get("Event") == "EndpointList"
    ]


def normalize_sip_peer(block: dict[str, Any]) -> SipPeer:
    return SipPeer(
        endpoint=block.get("ObjectName", ""),
        state=block.get("Status", ""),
        address=block.get("IPaddress", ""),
        dynamic=block.get("Dynamic", ""),
    )


def normalize_sip_peers(events: list[dict[str, Any]]) -> list[SipPeer]:
    return [
        normalize_sip_peer(block)
        for block in events
        if block.get("Event") == "PeerEntry"
    ]
