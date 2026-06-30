from __future__ import annotations

import json
import urllib.error
import urllib.request

from jeb.health import JebHealthState, start_health_server


def read_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_jeb_health_endpoints_report_live_and_ready() -> None:
    server = start_health_server("127.0.0.1", 0, JebHealthState(source_count=3))
    try:
        host, port = server.server_address[:2]

        assert read_json(f"http://{host}:{port}/health/live") == {
            "service": "jeb",
            "status": "ok",
        }
        assert read_json(f"http://{host}:{port}/health/ready") == {
            "service": "jeb",
            "source_count": 3,
            "status": "ok",
        }
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_health_unknown_paths_are_not_healthy() -> None:
    server = start_health_server("127.0.0.1", 0, JebHealthState(source_count=0))
    try:
        host, port = server.server_address[:2]

        try:
            read_json(f"http://{host}:{port}/not-health")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert json.loads(exc.read().decode("utf-8")) == {
                "service": "jeb",
                "status": "not_found",
            }
        else:
            raise AssertionError("unknown Jeb health paths must return 404")
    finally:
        server.shutdown()
        server.server_close()
