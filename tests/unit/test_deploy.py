"""The production overlay, the deploy scripts, the workflows and the pre-push hook, read as data
and run in a scratch directory. None of it is in the images, so these tests skip inside a container.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from pisniar.config import Settings
from pisniar.worker.celery_app import TASK_TIME_LIMIT_SECONDS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
PROD_FILE = REPOSITORY_ROOT / "compose.prod.yml"
DEPLOY_SCRIPT = REPOSITORY_ROOT / "deploy" / "deploy.sh"
WRITE_ENV_SCRIPT = REPOSITORY_ROOT / "deploy" / "write-env.sh"
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
PRE_PUSH_HOOK = REPOSITORY_ROOT / ".githooks" / "pre-push"
MAKEFILE = REPOSITORY_ROOT / "Makefile"

pytestmark = pytest.mark.skipif(
    not COMPOSE_FILE.is_file(), reason="the repository files are not available in this container"
)

PROD_RESTARTED_SERVICES = ["postgres", "redis", "bot", "worker", "worker-render", "webapp"]
DATABASE_SERVICES = ["migrate", "bot", "worker", "worker-render", "webapp"]
SECRET_KEYS_BEYOND_REQUIRED = {"TS_AUTHKEY", "WEBAPP_PUBLIC_URL", "MUSICBRAINZ_CONTACT", "YTDLP_PROXY"}
FAKE_VALUES = {
    "BOT_TOKEN": "111111111:fake-bot-token-value",
    "ADMIN_IDS": "111111111,222222222",
    "ALERT_CHAT_ID": "333333333",
    "CHANNEL_ID": "-1004444444444",
    "SPOTIFY_CLIENT_ID": "fake-spotify-id",
    "SPOTIFY_CLIENT_SECRET": "fake-spotify-secret",
    "REDIS_PASSWORD": "fake-redis-password",
    "POSTGRES_PASSWORD": "fake-postgres-password",
}


def read_yaml(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def quoted_list(script_text: str, name: str) -> list[str]:
    match = re.search(rf'^{name}="([^"]*)"$', script_text, re.MULTILINE)
    assert match, name
    return match.group(1).split()


def ssh_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    steps = workflow["jobs"]["deploy"]["steps"]
    return next(step for step in steps if step.get("uses", "").startswith("appleboy/ssh-action@"))


def grace_seconds(text: str) -> int:
    match = re.fullmatch(r"(\d+)([sm])", text)
    assert match, text
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def run_write_env(directory: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(WRITE_ENV_SCRIPT)],
        cwd=directory,
        env={"PATH": "/usr/bin:/bin", **environment},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return read_yaml(COMPOSE_FILE)


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return read_yaml(PROD_FILE)


@pytest.fixture(scope="module")
def deploy_workflow() -> dict[Any, Any]:
    return read_yaml(DEPLOY_WORKFLOW)


def test_each_service_runs_the_image_of_its_build_target(
    compose: dict[str, Any], prod: dict[str, Any], deploy_workflow: dict[Any, Any]
) -> None:
    prefix = deploy_workflow["env"]["IMAGE_PREFIX"]
    assert {name for name, service in prod["services"].items() if "image" in service} == {
        *DATABASE_SERVICES,
        "bench",
    }

    for name, service in prod["services"].items():
        if "image" not in service:
            continue
        target = compose["services"][name]["build"]["target"]
        assert (
            service["image"]
            == f"{prefix}/pisniar-{target}:${{PISNIAR_IMAGE_TAG:?set PISNIAR_IMAGE_TAG to the image sha}}"
        )


def test_the_overlay_publishes_no_port(prod: dict[str, Any]) -> None:
    assert all("ports" not in service for service in prod["services"].values())


def test_long_running_services_restart_and_the_migration_does_not(
    compose: dict[str, Any], prod: dict[str, Any]
) -> None:
    restarted = [name for name, service in prod["services"].items() if service.get("restart") == "unless-stopped"]

    assert sorted(restarted) == sorted(PROD_RESTARTED_SERVICES)
    assert "restart" not in prod["services"]["migrate"]
    assert compose["services"]["migrate"]["restart"] == "no"


def test_the_database_url_carries_the_generated_password_in_every_service(
    compose: dict[str, Any], prod: dict[str, Any]
) -> None:
    urls = {name: prod["services"][name]["environment"]["DATABASE_URL"] for name in DATABASE_SERVICES}

    assert len(set(urls.values())) == 1
    url = urls["bot"]
    assert "${POSTGRES_PASSWORD:?" in url
    assert "pisniar:pisniar@" not in url
    assert "pisniar:pisniar@" not in PROD_FILE.read_text(encoding="utf-8")
    assert prod["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert "DATABASE_URL" in compose["services"]["bot"]["environment"]


def test_the_grace_period_outlasts_the_task_hard_limit(prod: dict[str, Any]) -> None:
    render_limit = Settings.model_fields["render_task_time_limit_seconds"].default

    assert grace_seconds(prod["services"]["worker"]["stop_grace_period"]) >= TASK_TIME_LIMIT_SECONDS
    assert grace_seconds(prod["services"]["worker-render"]["stop_grace_period"]) >= render_limit


def test_deploy_script_pulls_stops_removes_migrates_then_starts() -> None:
    lines = [line.strip() for line in DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines()]
    ordered = [
        "docker compose pull --quiet",
        "docker compose stop bot webapp",
        "docker compose stop worker worker-render",
        "docker compose rm -f bot webapp worker worker-render",
        "docker compose run --rm migrate",
        "docker compose run --rm migrate alembic check",
        "docker compose up -d --no-build --wait --wait-timeout 600 --remove-orphans",
    ]

    positions = [lines.index(command) for command in ordered]

    assert positions == sorted(positions)


def test_deploy_script_prints_no_configuration_and_destroys_nothing() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    for forbidden in ("set -x", "compose config", "compose down", "prune", "docker login", "down -v", "printenv"):
        assert forbidden not in text


def test_write_env_writes_quoted_lines_with_mode_600_and_prints_no_value(tmp_path: Path) -> None:
    environment = {**FAKE_VALUES, "LOG_LEVEL": "DEBUG", "ITUNES_COUNTRY": "", "TS_AUTHKEY": "tskey-fake-auth-key"}

    result = run_write_env(tmp_path, environment)

    assert result.returncode == 0
    env_lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    assert env_lines[:2] == ["ENVIRONMENT=docker", "LOG_FORMAT=json"]
    for key, value in FAKE_VALUES.items():
        assert f"{key}='{value}'" in env_lines
    assert "LOG_LEVEL='DEBUG'" in env_lines
    assert not any(line.startswith("ITUNES_COUNTRY") for line in env_lines)
    assert (tmp_path / ".env.tailscale").read_text(encoding="utf-8") == "TS_AUTHKEY='tskey-fake-auth-key'\n"
    for name in (".env", ".env.tailscale"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.new"))
    output = result.stdout + result.stderr
    assert not any(value in output for value in [*FAKE_VALUES.values(), "tskey-fake-auth-key"])


def test_write_env_leaves_the_tailscale_file_empty_without_a_key(tmp_path: Path) -> None:
    result = run_write_env(tmp_path, FAKE_VALUES)

    assert result.returncode == 0
    assert (tmp_path / ".env.tailscale").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("missing", sorted(FAKE_VALUES))
def test_write_env_refuses_a_missing_required_key_by_name(tmp_path: Path, missing: str) -> None:
    environment = {key: value for key, value in FAKE_VALUES.items() if key != missing}

    result = run_write_env(tmp_path, environment)

    assert result.returncode != 0
    assert missing in result.stderr
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("unsafe", ["has'quote", "has\nnewline", "has\rreturn"])
def test_write_env_refuses_a_value_that_could_end_the_quote(tmp_path: Path, unsafe: str) -> None:
    result = run_write_env(tmp_path, {**FAKE_VALUES, "BOT_TOKEN": unsafe})

    assert result.returncode != 0
    assert "BOT_TOKEN" in result.stderr
    assert "has" not in result.stdout + result.stderr
    assert not (tmp_path / ".env").exists()


def test_write_env_requires_what_settings_requires_and_the_workflow_passes_every_key() -> None:
    script = WRITE_ENV_SCRIPT.read_text(encoding="utf-8")
    required = quoted_list(script, "required_keys")
    optional = quoted_list(script, "optional_keys")
    settings_required = {
        name.upper() for name, field in Settings.model_fields.items() if field.is_required() and name != "database_url"
    }
    step = ssh_step(read_yaml(DEPLOY_WORKFLOW))
    passed = set(step["with"]["envs"].split(","))
    all_keys = {*required, *optional, "TS_AUTHKEY"}

    assert settings_required <= set(required)
    assert {"REDIS_PASSWORD", "POSTGRES_PASSWORD"} <= set(required)
    assert passed == all_keys | {"IMAGE_SHA"}
    assert set(step["env"]) == passed
    for key in all_keys:
        source = "secrets" if key in {*required, *SECRET_KEYS_BEYOND_REQUIRED} else "vars"
        assert step["env"][key] == f"${{{{ {source}.{key} }}}}"


def test_deploy_guard_admits_only_a_green_push_to_master_of_this_repository(deploy_workflow: dict[Any, Any]) -> None:
    guard = " ".join(deploy_workflow["jobs"]["target"]["if"].split())

    for clause in (
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/master'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'master'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert clause in guard
    assert deploy_workflow["permissions"] == {}
    assert deploy_workflow["concurrency"] == {"group": "deploy-prod", "cancel-in-progress": False}
    assert deploy_workflow["jobs"]["deploy"]["permissions"] == {}
    assert deploy_workflow["jobs"]["build-push"]["permissions"] == {"contents": "read", "packages": "write"}


def test_untrusted_workflow_values_reach_scripts_through_the_environment(deploy_workflow: dict[Any, Any]) -> None:
    for job in deploy_workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")
    assert "${{" not in ssh_step(deploy_workflow)["with"]["script"]


def test_every_action_is_pinned_to_a_commit_and_the_ssh_host_is_verified(deploy_workflow: dict[Any, Any]) -> None:
    for path in (DEPLOY_WORKFLOW, CI_WORKFLOW):
        for reference in re.findall(r"^\s*-?\s*uses:\s*(\S+)", path.read_text(encoding="utf-8"), re.MULTILINE):
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", reference), reference
    with_block = ssh_step(deploy_workflow)["with"]
    assert with_block["fingerprint"] == "${{ secrets.SSH_FINGERPRINT }}"
    assert with_block["command_timeout"]
    assert "docker login" not in with_block["script"]


def test_ci_runs_on_pull_requests_without_secrets_and_runs_the_light_checks() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in text
    assert "pull_request_target" not in text
    assert "secrets." not in text
    assert "run: make -k check" in text
    assert "docker" not in text


def test_the_hook_gates_pushes_to_master_with_the_docker_suites_and_starts_nothing() -> None:
    text = PRE_PUSH_HOOK.read_text(encoding="utf-8")

    for required in (
        "refs/heads/master",
        "git status --porcelain",
        "make -k test-int test-fonts test-media",
        "--no-verify",
    ):
        assert required in text
    assert "--no-verify" in text.splitlines()[2]
    for forbidden in ("compose up", "compose down", "compose build", "down -v", "make up", "make build"):
        assert forbidden not in text.replace("run make up", "")


def test_make_hooks_points_git_at_the_committed_hooks() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")

    assert re.search(
        r"^hooks:.*\n\tgit config core\.hooksPath \.githooks\n\tchmod \+x \.githooks/pre-push$", text, re.MULTILINE
    )


def test_the_ssh_host_fingerprint_is_checked_before_the_ssh_step(deploy_workflow: dict[Any, Any]) -> None:
    steps = deploy_workflow["jobs"]["deploy"]["steps"]
    check = next(step for step in steps if step.get("name") == "Require the SSH host fingerprint")

    assert steps.index(check) < steps.index(ssh_step(deploy_workflow))
    assert check["env"] == {"SSH_FINGERPRINT": "${{ secrets.SSH_FINGERPRINT }}"}
    assert "^SHA256:[A-Za-z0-9+/]{43}$" in check["run"]
    assert "exit 1" in check["run"]
    assert "${{" not in check["run"]


def test_the_hook_uses_only_containers_started_from_this_checkout() -> None:
    text = PRE_PUSH_HOOK.read_text(encoding="utf-8")

    assert "git rev-parse --show-toplevel" in text
    assert text.count("com.docker.compose.project.working_dir=$checkout_root") == 1
    assert "docker compose" not in text
    assert text.index("com.docker.compose.project.working_dir") < text.index("make -k test-int")


def run_hook(directory: Path, push_line: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(PRE_PUSH_HOOK), "origin", "https://example.invalid/repo.git"],
        cwd=directory,
        input=push_line,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(directory)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repository_with_a_commit(tmp_path: Path) -> tuple[Path, str]:
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    for command in (["init", "-q"], [*identity, "commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", *command], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return tmp_path, head.stdout.strip()


def test_the_hook_ignores_other_branches_and_deletions(repository_with_a_commit: tuple[Path, str]) -> None:
    directory, head = repository_with_a_commit
    zero = "0" * 40

    assert run_hook(directory, f"refs/heads/feature {head} refs/heads/feature {zero}\n").returncode == 0
    assert run_hook(directory, f"(delete) {zero} refs/heads/master {head}\n").returncode == 0


def test_the_hook_refuses_master_from_a_dirty_tree_or_another_commit(
    repository_with_a_commit: tuple[Path, str],
) -> None:
    directory, head = repository_with_a_commit
    zero = "0" * 40

    other = run_hook(directory, f"refs/heads/master {'1' * 40} refs/heads/master {zero}\n")
    (directory / "untracked.txt").write_text("x", encoding="utf-8")
    dirty = run_hook(directory, f"refs/heads/master {head} refs/heads/master {zero}\n")

    assert "HEAD is not the pushed commit" in other.stderr
    assert "uncommitted changes" in dirty.stderr
    for refusal in (other, dirty):
        assert refusal.returncode == 1


def test_the_hook_refuses_without_an_env_file_in_one_line(repository_with_a_commit: tuple[Path, str]) -> None:
    directory, head = repository_with_a_commit

    refusal = run_hook(directory, f"refs/heads/master {head} refs/heads/master {'0' * 40}\n")

    assert refusal.returncode == 1
    assert refusal.stderr.count("\n") == 1
    assert "no .env" in refusal.stderr
