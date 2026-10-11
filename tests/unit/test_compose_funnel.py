"""`compose.yaml` and the Funnel config, read as data: the public entry is the only one,
its secret reaches no other service, and its state cannot be deleted by `down -v`.
The compose files are not in the images, so these tests skip inside a container.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
SERVE_FILE = REPOSITORY_ROOT / "deploy" / "tailscale" / "serve.json"

pytestmark = pytest.mark.skipif(not COMPOSE_FILE.is_file(), reason="compose.yaml is not available in this container")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def environment_files(service: dict[str, Any]) -> list[str]:
    return [str(entry) for entry in service.get("env_file", [])]


def test_tailscale_runs_only_under_the_funnel_profile(compose: dict[str, Any]) -> None:
    assert compose["services"]["tailscale"]["profiles"] == ["funnel"]
    others = {name: service for name, service in compose["services"].items() if name not in {"tailscale", "bench"}}
    assert all("profiles" not in service for service in others.values())


def test_bench_is_a_profile_service_without_env_volumes_or_network(
    compose: dict[str, Any],
) -> None:
    """`make bench-render` runs synthetic renders: nothing of the app is handed to it."""
    bench = compose["services"]["bench"]

    assert bench["profiles"] == ["bench"]
    assert bench["network_mode"] == "none"
    assert bench["command"] == ["python", "-m", "pisniar.worker.bench"]
    for forbidden in ("env_file", "environment", "volumes", "ports", "depends_on"):
        assert forbidden not in bench


def test_only_tailscale_reads_the_tailscale_env_file(compose: dict[str, Any]) -> None:
    readers = [
        name
        for name, service in compose["services"].items()
        if any(".env.tailscale" in entry for entry in environment_files(service))
    ]
    assert readers == ["tailscale"]


def test_the_auth_key_is_not_declared_for_any_python_service(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        if name == "tailscale":
            continue
        assert "TS_AUTHKEY" not in json.dumps(service), name


def test_the_node_state_volume_is_external_and_named(compose: dict[str, Any]) -> None:
    volume = compose["volumes"]["tailscale-state"]
    assert volume == {"external": True, "name": "pisniar-tailscale-state"}
    assert "tailscale-state:/var/lib/tailscale" in compose["services"]["tailscale"]["volumes"]


def test_tailscale_runs_in_userspace_with_the_state_on_the_volume(compose: dict[str, Any]) -> None:
    service = compose["services"]["tailscale"]

    assert service["hostname"] == "pisniar"
    assert service["environment"]["TS_USERSPACE"] == "true"
    assert service["environment"]["TS_STATE_DIR"] == "/var/lib/tailscale"
    assert service["environment"]["TS_SERVE_CONFIG"] == "/config/serve.json"
    assert "./deploy/tailscale:/config:ro" in service["volumes"]
    assert service["depends_on"] == {"webapp": {"condition": "service_healthy"}}


def test_the_tailscale_image_is_pinned_by_tag_and_digest(compose: dict[str, Any]) -> None:
    image = compose["services"]["tailscale"]["image"]
    assert "@sha256:" in image
    assert ":v" in image.split("@")[0]


def test_webapp_publishes_no_port_in_the_base_file(compose: dict[str, Any]) -> None:
    assert "ports" not in compose["services"]["webapp"]


def test_webapp_gets_the_authenticated_broker_and_the_media_volume_read_only(
    compose: dict[str, Any],
) -> None:
    webapp = compose["services"]["webapp"]
    bot = compose["services"]["bot"]
    assert webapp["volumes"] == ["media-data:/data:ro"]
    assert "redis" not in webapp["depends_on"]
    assert webapp["environment"]["REDIS_URL"] == bot["environment"]["REDIS_URL"]
    assert "${REDIS_PASSWORD:?" in webapp["environment"]["REDIS_URL"]
    assert webapp["build"]["target"] == "webapp"


def test_the_serve_config_proxies_only_to_the_webapp() -> None:
    serve = json.loads(SERVE_FILE.read_text(encoding="utf-8"))

    assert serve["TCP"] == {"443": {"HTTPS": True}}
    assert serve["AllowFunnel"] == {"${TS_CERT_DOMAIN}:443": True}
    handlers = serve["Web"]["${TS_CERT_DOMAIN}:443"]["Handlers"]
    assert handlers == {"/": {"Proxy": "http://webapp:8081"}}


def test_no_secret_is_committed_in_the_serve_config() -> None:
    assert "tskey" not in SERVE_FILE.read_text(encoding="utf-8")
