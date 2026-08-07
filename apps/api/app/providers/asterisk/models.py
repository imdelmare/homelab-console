"""Normalized Asterisk models. These are the only shapes exposed to the
frontend and to model providers — never the raw AMI response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CoreInfo(_Model):
    version: str = ""
    ami_version: str = ""
    startup_time: str = ""
    reload_time: str = ""
    current_calls: str | None = None
    max_calls: str | None = None


class ChannelInfo(_Model):
    channel: str = ""
    state: str = ""
    caller_number: str = ""
    connected_number: str = ""
    application: str = ""
    duration: str = ""
    bridge_id: str = ""


class PjsipEndpoint(_Model):
    endpoint: str = ""
    state: str = ""
    contacts: str = ""
    transport: str = ""


class SipPeer(_Model):
    endpoint: str = ""
    state: str = ""
    address: str = ""
    dynamic: str = ""
