import json
import subprocess
import unittest
from unittest.mock import patch

from server import normalize_speedtest
from server import run_speedtest


class NormalizeSpeedtestTest(unittest.TestCase):
    def test_normalizes_allowlisted_fields_and_converts_bandwidth(self):
        result = normalize_speedtest(
            {
                "timestamp": "2026-07-24T10:00:00Z",
                "ping": {"jitter": 1.25, "latency": 12.5, "low": 8, "high": 20},
                "download": {"bandwidth": 12_500_000, "bytes": 99},
                "upload": {"bandwidth": 2_500_000, "bytes": 88},
                "packetLoss": 0.2,
                "isp": "Example ISP",
                "interface": {"name": "eth0", "internalIp": "discarded"},
                "server": {
                    "id": 123,
                    "name": "Example",
                    "location": "Rome",
                    "country": "Italy",
                    "ip": "discarded",
                },
                "result": {"url": "https://www.speedtest.net/result/123"},
            }
        )

        self.assertEqual(result["download_mbps"], 100.0)
        self.assertEqual(result["upload_mbps"], 20.0)
        self.assertEqual(result["server"]["id"], 123)
        self.assertNotIn("internalIp", result)
        self.assertNotIn("ip", result["server"])

    @patch("server.subprocess.run")
    def test_runner_uses_fixed_command_and_normalizes_json(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "timestamp": "2026-07-24T10:00:00Z",
                    "ping": {"jitter": 1, "latency": 10},
                    "download": {"bandwidth": 1_000_000},
                    "upload": {"bandwidth": 500_000},
                    "packetLoss": 0,
                    "isp": "ISP",
                    "interface": {"name": "eth0"},
                    "server": {"id": 1, "name": "S", "location": "L", "country": "C"},
                }
            ),
            stderr="",
        )

        result = run_speedtest()

        run.assert_called_once_with(
            [
                "/opt/speedtest-cli/speedtest",
                "--accept-license",
                "--accept-gdpr",
                "--format=json",
                "--progress=no",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result["download_mbps"], 8.0)


if __name__ == "__main__":
    unittest.main()
