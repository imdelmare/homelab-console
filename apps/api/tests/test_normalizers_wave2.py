"""Wave 2 normalizers: Home Assistant, Frigate, AdGuard, Nextcloud.
Fake vendor payloads in, normalized models out; extra vendor fields dropped."""

import httpx

from app.providers.adguard import normalizers as adguard
from app.providers.frigate import normalizers as frigate
from app.providers.homeassistant import normalizers as homeassistant
from app.providers.nextcloud import normalizers as nextcloud


# --- Home Assistant ---------------------------------------------------------


def test_homeassistant_api_status_exposes_message_only():
    status = homeassistant.normalize_api_status({"message": "API running.", "extra": "internal"})
    assert status.model_dump() == {"message": "API running."}


def test_homeassistant_config_drops_sensitive_fields():
    raw = {
        "version": "2024.6.1",
        "location_name": "Home",
        "time_zone": "Europe/Rome",
        "state": "RUNNING",
        "unit_system": {"length": "km", "mass": "g", "temperature": "°C", "volume": "L"},
        "components": ["homeassistant", "frontend", "mqtt"],
        # Sensitive vendor fields that must never be exposed.
        "latitude": 45.123,
        "longitude": 9.456,
        "elevation": 120,
        "config_dir": "/config",
        "whitelist_external_dirs": ["/config/www"],
        "allowlist_external_dirs": ["/config/www"],
        "external_url": "https://ha.example.com",
        "internal_url": "http://10.0.0.5:8123",
    }
    config = homeassistant.normalize_config(raw).model_dump()
    assert config == {
        "version": "2024.6.1",
        "location_name": "Home",
        "time_zone": "Europe/Rome",
        "state": "RUNNING",
        "unit_system": {"length": "km", "mass": "g", "temperature": "°C", "volume": "L"},
        "components_count": 3,
    }
    dumped = str(config)
    assert "45.123" not in dumped
    assert "/config" not in dumped
    assert "example.com" not in dumped


def test_homeassistant_states_projected_and_summarized():
    raw = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen", "supported_features": 44, "brightness": 200},
            "last_changed": "2026-07-12T10:00:00+00:00",
            "last_updated": "2026-07-12T10:00:00+00:00",
            "context": {"id": "ctx-1", "user_id": "abc"},
        },
        {"entity_id": "sensor.temp", "state": "unavailable", "attributes": {}},
        "not-a-dict",
    ]
    states = homeassistant.normalize_states(raw)
    assert [state.entity_id for state in states] == ["light.kitchen", "sensor.temp"]
    assert states[0].model_dump() == {
        "entity_id": "light.kitchen",
        "domain": "light",
        "state": "on",
        "friendly_name": "Kitchen",
        "last_changed": "2026-07-12T10:00:00+00:00",
        "last_updated": "2026-07-12T10:00:00+00:00",
    }

    summary = homeassistant.summarize_states(states)
    assert summary.entities_total == 2
    assert summary.domains == {"light": 1, "sensor": 1}
    assert summary.problem_entities == 1


def test_homeassistant_logbook_events_projected():
    raw = [
        {
            "when": "2026-07-12T10:05:00+00:00",
            "name": "Kitchen",
            "entity_id": "light.kitchen",
            "state": "on",
            "message": "turned on",
            "context_user_id": "abc",
            "context_id": "ctx-1",
            "icon": "mdi:lightbulb",
        },
        {"when": "2026-07-12T10:06:00+00:00", "name": "Automation ran", "domain": "automation"},
    ]
    events = homeassistant.normalize_logbook_events(raw)
    assert events[0].model_dump() == {
        "when": "2026-07-12T10:05:00+00:00",
        "name": "Kitchen",
        "entity_id": "light.kitchen",
        "domain": "light",
        "state": "on",
        "message": "turned on",
    }
    assert events[1].domain == "automation"
    assert "context_user_id" not in str([event.model_dump() for event in events])


def test_homeassistant_service_domains_normalized():
    raw = [{"domain": "light", "services": {"turn_on": {}, "turn_off": {}}, "internal": True}]
    domains = homeassistant.normalize_service_domains(raw)
    assert domains[0].model_dump() == {
        "domain": "light",
        "services": ["turn_off", "turn_on"],
        "count": 2,
    }


# --- Frigate ----------------------------------------------------------------


def test_frigate_camera_and_detector_stats_projected():
    raw = {
        "cameras": {
            "front": {
                "camera_fps": 5.1,
                "detection_fps": 0.2,
                "process_fps": 5.0,
                "skipped_fps": 0.0,
                "ffmpeg_pid": 123,
                "capture_pid": 456,
                "audio_dBFS": -30,
            }
        },
        "detectors": {
            "coral": {
                "inference_speed": 8.7,
                "detection_start": 0.0,
                "pid": 789,
                "model": {"path": "/config/model.tflite"},
            }
        },
    }
    cameras = frigate.normalize_camera_stats(raw)
    assert [camera.model_dump() for camera in cameras] == [
        {
            "name": "front",
            "camera_fps": 5.1,
            "detection_fps": 0.2,
            "process_fps": 5.0,
            "skipped_fps": 0.0,
        }
    ]
    detectors = frigate.normalize_detector_stats(raw)
    assert detectors[0].model_dump() == {
        "name": "coral",
        "inference_speed": 8.7,
        "detection_start": 0.0,
        "pid": 789,
    }
    dumped = str([item.model_dump() for item in cameras + detectors])
    assert "ffmpeg_pid" not in dumped
    assert "model.tflite" not in dumped


def test_frigate_service_stats_drop_raw_service_payload():
    raw = {
        "detection_fps": 0.5,
        "process_uptime": 3600,
        "service": {
            "uptime": 7200,
            "version": "0.14.1",
            "latest_version": "0.15.0",
            "storage": {"/media/frigate/recordings": {"used": 100, "mount_type": "ext4"}},
            "temperatures": {"apex_0": 52.1},
        },
    }
    service = frigate.normalize_service_stats(raw)
    assert service.model_dump() == {
        "detection_fps": 0.5,
        "process_uptime": 3600,
        "uptime_seconds": 7200,
        "version": "0.14.1",
        "latest_version": "0.15.0",
        "temperatures": {"apex_0": 52.1},
    }
    assert "/media/frigate" not in str(service.model_dump())


def test_frigate_camera_config_projected():
    config_payload = {
        "cameras": {
            "front": {
                "enabled": True,
                "detect": {"enabled": True, "fps": 5},
                "record": {"enabled": True},
                "snapshots": {"enabled": False},
                "zones": {"driveway": {"coordinates": "0,0,1,1"}},
                "ffmpeg": {"inputs": [{"path": "rtsp://example:example@10.0.0.9/stream"}]},
            }
        }
    }
    stats_payload = {"cameras": {"front": {"camera_fps": 5.0, "skipped_fps": 0.1}}}
    cameras = frigate.normalize_camera_configs(config_payload, stats_payload)
    assert cameras[0].model_dump() == {
        "name": "front",
        "enabled": True,
        "detect_enabled": True,
        "detect_fps": 5.0,
        "record_enabled": True,
        "snapshots_enabled": False,
        "zones": ["driveway"],
        "camera_fps": 5.0,
        "detection_fps": None,
        "process_fps": None,
        "skipped_fps": 0.1,
    }
    # Stream URLs (with credentials) must never leak.
    assert "rtsp://" not in str(cameras[0].model_dump())


def test_frigate_events_and_reviews_projected():
    events = frigate.normalize_events(
        [
            {
                "id": "evt-1",
                "camera": "front",
                "label": "person",
                "sub_label": None,
                "start_time": 1000.0,
                "end_time": 1010.0,
                "score": 0.8,
                "top_score": 0.9,
                "has_clip": True,
                "has_snapshot": True,
                "false_positive": False,
                "zones": ["driveway"],
                "thumbnail": "base64-blob",
                "box": [1, 2, 3, 4],
            }
        ]
    )
    assert events[0].id == "evt-1"
    assert events[0].zones == ["driveway"]
    assert "base64-blob" not in str(events[0].model_dump())

    reviews = frigate.normalize_reviews(
        [
            {
                "id": "rev-1",
                "camera": "front",
                "severity": "alert",
                "start_time": 1000.0,
                "end_time": 1020.0,
                "thumb_path": "/clips/review/thumb.webp",
                "data": {
                    "detections": [{"label": "person"}, {"label": "car"}, {"label": "person"}],
                    "zones": ["driveway"],
                    "audio": [],
                },
            }
        ]
    )
    assert reviews[0].detections == 3
    assert reviews[0].objects == ["car", "person"]
    assert reviews[0].zones == ["driveway"]


# --- AdGuard Home -----------------------------------------------------------


def test_adguard_status_drops_extra_vendor_fields():
    status = adguard.normalize_status(
        {
            "version": "v0.107.50",
            "running": True,
            "protection_enabled": True,
            "dns_addresses": ["10.0.0.3"],
            "dns_port": 53,
            "http_port": 80,
            "language": "en",
        }
    )
    assert status.model_dump() == {
        "version": "v0.107.50",
        "running": True,
        "protection_enabled": True,
        "dns_addresses": ["10.0.0.3"],
        "dns_port": 53,
    }


def test_adguard_stats_top_lists_flattened_and_trimmed():
    stats = adguard.normalize_stats(
        {
            "time_units": "days",
            "num_dns_queries": 1000,
            "num_blocked_filtering": 100,
            "avg_processing_time": 0.002,
            "top_queried_domains": [{f"host{index}.lab": index} for index in range(15)],
            "top_blocked_domains": [{"ads.example": 33}],
            "top_clients": [{"10.0.0.42": 500}],  # extra, dropped
        }
    )
    dumped = stats.model_dump()
    assert dumped["dns_queries"] == 1000
    assert len(dumped["top_queried_domains"]) == 10
    assert dumped["top_blocked_domains"] == [{"name": "ads.example", "count": 33}]
    assert "10.0.0.42" not in str(dumped)


def test_adguard_filtering_status_normalized():
    filtering = adguard.normalize_filtering_status(
        {
            "enabled": True,
            "interval": 24,
            "filters": [
                {"name": "AdGuard DNS filter", "enabled": True, "rules_count": 50000,
                 "last_updated": "2026-07-10T00:00:00Z", "url": "https://filters.example/1.txt"},
            ],
            "user_rules": ["||ads.example^", "@@allow.example"],
        }
    )
    dumped = filtering.model_dump()
    assert dumped["filters_total"] == 1
    assert dumped["filters"][0] == {
        "name": "AdGuard DNS filter",
        "enabled": True,
        "rules_count": 50000,
        "last_updated": "2026-07-10T00:00:00Z",
    }
    assert dumped["user_rules_count"] == 2
    # Rule contents and filter URLs are not exposed.
    assert "ads.example" not in str(dumped)


# --- Nextcloud --------------------------------------------------------------


def test_nextcloud_status_normalized():
    status = nextcloud.normalize_status(
        {
            "installed": True,
            "maintenance": False,
            "needsDbUpgrade": False,
            "versionstring": "29.0.1",
            "edition": "",
            "productname": "Nextcloud",  # extra, dropped
        }
    )
    assert status.model_dump() == {
        "installed": True,
        "maintenance": False,
        "needs_db_upgrade": False,
        "version": "29.0.1",
        "edition": "",
    }


def test_nextcloud_capabilities_normalized():
    caps = nextcloud.normalize_capabilities(
        {
            "ocs": {
                "meta": {"status": "ok"},
                "data": {
                    "version": {"string": "29.0.1", "edition": "", "major": 29},
                    "capabilities": {"files": {"bigfilechunking": True}, "theming": {}},
                },
            }
        }
    )
    assert caps.model_dump() == {"version": "29.0.1", "edition": "", "apps": ["files", "theming"]}


def test_nextcloud_serverinfo_normalized_and_tolerant():
    info = nextcloud.normalize_serverinfo(
        {
            "ocs": {
                "data": {
                    "nextcloud": {
                        "system": {
                            "version": "29.0.1.1",
                            "freespace": 5000000,
                            "mem_total": "8000000",
                            "mem_free": "N/A",
                            "cpuload": ["0.5", 0.4, "bad"],
                            "apps": {"app_updates": {"mail": "3.7.0"}},  # extra, dropped
                        },
                        "storage": {"num_users": 3, "num_files": 1200},
                    },
                    "activeUsers": {"last24hours": 2, "last5minutes": 0},
                }
            }
        }
    )
    assert info.model_dump() == {
        "version": "29.0.1.1",
        "freespace_bytes": 5000000,
        "memory_total_kb": 8000000,
        "memory_free_kb": None,
        "cpu_load": [0.5, 0.4],
        "users_total": 3,
        "files_total": 1200,
        "active_users_last_day": 2,
    }


# --- Leak fixes end-to-end through the tools --------------------------------


def _transport(monkeypatch, module: str, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", client_factory)


async def test_homeassistant_config_tool_does_not_leak(monkeypatch):
    monkeypatch.setattr(
        "app.providers.homeassistant.client.get_provider_secrets",
        lambda _pid: {"base_url": "https://ha.test", "token": "ha-token"},
        raising=True,
    )

    def handler(request):
        assert request.headers["authorization"] == "Bearer ha-token"
        return httpx.Response(200, json={
            "version": "2024.6.1", "location_name": "Home", "time_zone": "Europe/Rome",
            "state": "RUNNING", "unit_system": {"temperature": "°C"},
            "components": ["mqtt"], "latitude": 45.1, "longitude": 9.2,
            "config_dir": "/config", "internal_url": "http://10.0.0.5:8123",
        })

    _transport(monkeypatch, "app.providers.httpclient", handler)
    from app.providers.homeassistant.tools import config

    result = await config()
    assert result["config"]["version"] == "2024.6.1"
    assert result["config"]["components_count"] == 1
    dumped = str(result)
    assert "latitude" not in dumped
    assert "45.1" not in dumped
    assert "/config" not in dumped
    assert "10.0.0.5" not in dumped


async def test_frigate_stats_tool_does_not_leak(monkeypatch):
    monkeypatch.setattr(
        "app.providers.frigate.client.get_provider_secrets",
        lambda _pid: {"base_url": "https://frg.test"},
        raising=True,
    )

    def handler(request):
        return httpx.Response(200, json={
            "detection_fps": 0.5,
            "process_uptime": 3600,
            "cameras": {"front": {"camera_fps": 5.0, "detection_fps": 0.1,
                                  "process_fps": 5.0, "skipped_fps": 0.0, "ffmpeg_pid": 123}},
            "detectors": {"coral": {"inference_speed": 8.7, "detection_start": 0.0, "pid": 789}},
            "service": {"uptime": 7200, "version": "0.14.1", "latest_version": "0.15.0",
                        "storage": {"/media/frigate/recordings": {"used": 100}}},
        })

    _transport(monkeypatch, "app.providers.httpclient", handler)
    from app.providers.frigate.tools import stats

    result = await stats()
    assert result["service"]["version"] == "0.14.1"
    assert result["service"]["uptime_seconds"] == 7200
    assert result["cameras"][0]["name"] == "front"
    assert result["detectors"][0]["inference_speed"] == 8.7
    dumped = str(result)
    assert "ffmpeg_pid" not in dumped
    assert "/media/frigate" not in dumped
