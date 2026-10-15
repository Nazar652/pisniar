"""How the frontend is wired into the repository: one pinned node, a strict webapp healthcheck,
node only in the build stage, generated files marked, a dev server bound to loopback.
The sources are not in the images, so these tests skip inside a container.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from pisniar.webapp.app import (
    CONTENT_SECURITY_POLICY,
    WAVESURFER_STYLE_HASH,
    WAVESURFER_VERSION,
)
from pisniar.webapp.export_openapi import GENERATED_NOTICE, build_openapi_schema
from pisniar.worker.ass import LAST_LINE_MAX_MS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPOSITORY_ROOT / "frontend"
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
NODE_VERSION = "24.16.0"

pytestmark = pytest.mark.skipif(
    not (FRONTEND / "package.json").is_file(),
    reason="no frontend sources in this container",
)


def stage_of(dockerfile: str, name: str) -> str:
    """the text of one Dockerfile stage, from its `FROM ... AS name` to the next `FROM`."""
    pattern = rf"^FROM [^\n]* AS {re.escape(name)}$(.*?)(?=^FROM |\Z)"
    match = re.search(pattern, dockerfile, re.M | re.S)
    assert match is not None, name
    return match.group(0)


@contextmanager
def serve_status(status: int) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *arguments: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def healthcheck_code() -> str:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    command = compose["services"]["webapp"]["healthcheck"]["test"]
    assert command[:3] == ["CMD", "python", "-c"]
    return command[3]


@pytest.mark.parametrize(("status", "healthy"), [(200, True), (503, False), (404, False)])
def test_the_webapp_healthcheck_passes_only_on_a_200_from_the_index(status: int, healthy: bool) -> None:
    with serve_status(status) as port:
        code = healthcheck_code().replace("8081", str(port)).replace("localhost", "127.0.0.1")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)

    assert (result.returncode == 0) is healthy


def test_the_webapp_healthcheck_fails_when_nothing_listens() -> None:
    code = healthcheck_code().replace("8081", "9").replace("localhost", "127.0.0.1")

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)

    assert result.returncode != 0


def test_one_node_version_on_the_host_in_the_package_and_in_the_image() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))

    assert (FRONTEND / ".node-version").read_text(encoding="utf-8").strip() == NODE_VERSION
    assert package["engines"]["node"] == f">={NODE_VERSION} <25"
    assert re.search(rf"^ARG NODE_IMAGE=node:{NODE_VERSION}-slim@sha256:[0-9a-f]{{64}}$", dockerfile, re.M)
    assert dockerfile.count("node:24.") == 1


def test_the_npm_configuration_blocks_install_scripts_and_floating_versions() -> None:
    npmrc = (FRONTEND / ".npmrc").read_text(encoding="utf-8").split()

    assert {"ignore-scripts=true", "save-exact=true", "engine-strict=true"} <= set(npmrc)


def test_dependencies_are_pinned_exactly() -> None:
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    versions = {**package["dependencies"], **package["devDependencies"]}

    assert versions
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", version) for version in versions.values())
    assert set(package["dependencies"]) == {"vue", "openapi-fetch", "wavesurfer.js"}


def test_the_csp_style_hash_names_the_wavesurfer_version_in_package_json() -> None:
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))

    assert package["dependencies"]["wavesurfer.js"] == WAVESURFER_VERSION
    assert f"'{WAVESURFER_STYLE_HASH}'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY


def test_the_editor_preview_uses_the_render_ceiling_of_the_last_line() -> None:
    source = (FRONTEND / "src" / "lyrics" / "timing.ts").read_text(encoding="utf-8")
    match = re.search(r"^export const LAST_LINE_MAX_MS = ([\d_]+)$", source, re.M)

    assert match is not None
    assert int(match.group(1).replace("_", "")) == LAST_LINE_MAX_MS


def test_node_exists_only_in_the_frontend_build_stage() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    build = stage_of(dockerfile, "frontend-build")

    assert "FROM ${NODE_IMAGE} AS frontend-build" in build
    assert "npm ci --ignore-scripts" in build
    for name in ("bot", "worker", "bot-dev", "worker-dev", "webapp-dev", "base", "source-prod"):
        assert "frontend-build" not in stage_of(dockerfile, name), name
        assert "node" not in stage_of(dockerfile, name).lower().replace("nodejs", ""), name


def test_the_webapp_stage_takes_only_the_built_dist() -> None:
    webapp = stage_of(DOCKERFILE.read_text(encoding="utf-8"), "webapp")

    copies = re.findall(r"^COPY --from=frontend-build .*$", webapp, re.M)
    assert len(copies) == 1
    assert "/frontend/dist " in copies[0]
    assert copies[0].rstrip().endswith("/app/webapp-static")


def test_the_dev_server_listens_on_loopback_only() -> None:
    config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")

    assert "host: '127.0.0.1'" in config
    assert "0.0.0.0" not in config


def test_the_production_build_has_no_inlined_assets_no_sourcemap_and_a_fixed_target() -> None:
    config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")

    assert "assetsInlineLimit: 0" in config
    assert "sourcemap: false" in config
    assert "target: 'es2020'" in config


def test_build_outputs_and_dependencies_stay_out_of_git_and_out_of_the_images() -> None:
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert {"frontend/node_modules/", "frontend/dist/"} <= set(gitignore)
    assert {"frontend/node_modules", "frontend/dist"} <= set(dockerignore)


def test_the_generated_files_say_so_and_use_unix_line_endings() -> None:
    schema_types = (FRONTEND / "src" / "api" / "schema.ts").read_bytes()
    schema_json = (FRONTEND / "openapi.json").read_bytes()

    assert schema_types.startswith(b"// generated")
    assert b"\r" not in schema_types
    assert b"\r" not in schema_json
    assert json.loads(schema_json)["info"]["x-generated"] == GENERATED_NOTICE


def test_the_committed_schema_is_what_the_application_exports() -> None:
    committed = json.loads((FRONTEND / "openapi.json").read_text(encoding="utf-8"))

    assert committed == build_openapi_schema()
