"""Package boundaries: `webapp` never imports `bot`, reaches `worker` only through four leaf
modules and enqueues the render and the prepare task alone; nothing imports `webapp`; `dispatch`
is a leaf; `bot` reaches `resolve`, `post`, `telegram` and `worker` through a short list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "pisniar"


def imported_modules(package_directory: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(package_directory.rglob("*.py")):
        modules: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        found[path.relative_to(SOURCE_ROOT).as_posix()] = modules
    return found


def offenders(package: str, forbidden: tuple[str, ...]) -> list[str]:
    result = []
    for module_path, modules in imported_modules(SOURCE_ROOT / package).items():
        for module in sorted(modules):
            if any(module == name or module.startswith(f"{name}.") for name in forbidden):
                result.append(f"{module_path} imports {module}")
    return result


BOT_RESTRICTED_PACKAGES = (
    "pisniar.resolve",
    "pisniar.post",
    "pisniar.telegram",
    "pisniar.worker",
)
BOT_ALLOWED_MODULES = frozenset(
    {
        "pisniar.resolve.urls",
        "pisniar.resolve.types",
        "pisniar.telegram.client",
        "pisniar.telegram.keyboards",
        "pisniar.telegram.menu",
        "pisniar.worker.celery_app",
    }
)


WEBAPP_ALLOWED_WORKER_MODULES = frozenset(
    {
        "pisniar.worker.celery_app",
        "pisniar.worker.fragment",
        "pisniar.worker.peaks_format",
        "pisniar.worker.storage",
    }
)


def unlisted_imports(
    package: str, restricted: tuple[str, ...], allowed: frozenset[str]
) -> list[str]:
    result = []
    for module_path, modules in imported_modules(SOURCE_ROOT / package).items():
        for module in sorted(modules - allowed):
            if any(module == name or module.startswith(f"{name}.") for name in restricted):
                result.append(f"{module_path} imports {module}")
    return result


def worker_modules_imported_by(module_name: str) -> set[str]:
    """`from pisniar.worker import x` counts as importing `pisniar.worker.x`."""
    path = SOURCE_ROOT.parent / (module_name.replace(".", "/") + ".py")
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "pisniar.worker":
            found.update(f"pisniar.worker.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return {name for name in found if name.startswith("pisniar.worker")}


def test_webapp_does_not_import_the_bot() -> None:
    assert offenders("webapp", ("pisniar.bot",)) == []


def test_webapp_imports_only_the_listed_worker_modules() -> None:
    assert unlisted_imports("webapp", ("pisniar.worker",), WEBAPP_ALLOWED_WORKER_MODULES) == []


def test_every_allowed_worker_module_is_still_imported_by_the_webapp() -> None:
    used = set().union(*imported_modules(SOURCE_ROOT / "webapp").values())
    assert used >= WEBAPP_ALLOWED_WORKER_MODULES


def test_the_allowed_worker_modules_import_no_other_worker_module() -> None:
    """so the webapp never loads ffmpeg, the tasks, the download or the render."""
    for module_name in sorted(WEBAPP_ALLOWED_WORKER_MODULES):
        assert worker_modules_imported_by(module_name) <= WEBAPP_ALLOWED_WORKER_MODULES, module_name


def task_name_references(package_directory: Path) -> set[str]:
    """every `*_TASK_NAME` constant the package mentions, imported or used."""
    found: set[str] = set()
    for path in package_directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name) and node.id.endswith("_TASK_NAME"):
                found.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr.endswith("_TASK_NAME"):
                found.add(node.attr)
            elif isinstance(node, ast.alias) and node.name.endswith("_TASK_NAME"):
                found.add(node.name)
    return found


QUEUE_CALL_NAMES = frozenset({"send_task", "signature", "apply_async", "delay", "s"})


def is_send_task_lookup(node: ast.Call) -> bool:
    """`getattr(client, "send_task")` reaches the queue without spelling the call."""
    return (
        ast.unparse(node.func) == "getattr"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "send_task"
    )


def queue_calls(package_directory: Path) -> list[str]:
    """each call that puts a task on the queue, with the first thing it names as the task."""
    found: list[str] = []
    for path in package_directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            callee = ast.unparse(node.func)
            if callee.rsplit(".", 1)[-1] in QUEUE_CALL_NAMES or is_send_task_lookup(node):
                found.append(f"{path.name}: {callee}")
            elif callee.endswith("dispatch_task"):
                task = ast.unparse(node.args[1]) if len(node.args) > 1 else "?"
                found.append(f"{path.name}: {callee}({task})")
    return found


def task_name_literals(package_directory: Path) -> list[str]:
    """a task name spelled as a string would slip past the constant scan."""
    return [
        node.value
        for path in package_directory.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("pisniar.worker.")
    ]


def test_the_webapp_enqueues_the_render_and_the_prepare_task_and_nothing_else() -> None:
    webapp_directory = SOURCE_ROOT / "webapp"

    references = task_name_references(webapp_directory)
    assert references == {"RENDER_POST_VIDEO_TASK_NAME", "PREPARE_POST_AUDIO_TASK_NAME"}
    assert sorted(queue_calls(webapp_directory)) == [
        "audio_preparation.py: dispatch_task(PREPARE_POST_AUDIO_TASK_NAME)",
        "fragment_choice.py: dispatch_task(RENDER_POST_VIDEO_TASK_NAME)",
    ]
    assert task_name_literals(webapp_directory) == []


def test_the_queue_scan_sees_the_bot_dispatches_and_a_spelled_out_task_name() -> None:
    bot_calls = queue_calls(SOURCE_ROOT / "bot")

    assert "handlers.py: dispatch_task(RESOLVE_POST_TASK_NAME)" in bot_calls
    assert "handlers.py: dispatch_task(PUBLISH_POST_TASK_NAME)" in bot_calls
    assert task_name_literals(SOURCE_ROOT / "worker") != []


def synthetic_package(tmp_path: Path, source: str) -> Path:
    (tmp_path / "module.py").write_text(source, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "source",
    [
        "tasks.RENDER_POST_VIDEO_TASK_NAME",
        "celery.DOWNLOAD_POST_AUDIO_TASK_NAME",
    ],
)
def test_the_reference_scan_sees_a_task_name_reached_as_an_attribute(
    tmp_path: Path, source: str
) -> None:
    assert len(task_name_references(synthetic_package(tmp_path, source))) == 1


@pytest.mark.parametrize(
    "call",
    [
        "client.send_task(name)",
        "signature(name)",
        "task.apply_async()",
        "task.delay(1)",
        "task.s(1)",
        'getattr(client, "send_task")(name)',
    ],
)
def test_the_queue_scan_sees_every_way_to_put_a_task_on_the_queue(
    tmp_path: Path, call: str
) -> None:
    assert queue_calls(synthetic_package(tmp_path, call)) != []


def test_the_queue_scan_ignores_an_ordinary_call(tmp_path: Path) -> None:
    source = 'getattr(client, "other")\nclient.close()\nvalues.sort()'

    assert queue_calls(synthetic_package(tmp_path, source)) == []


def test_dispatch_is_a_leaf_that_imports_neither_the_bot_nor_the_webapp() -> None:
    imported = imported_modules(SOURCE_ROOT)["dispatch.py"]

    assert not any(
        module.startswith(("pisniar.bot", "pisniar.webapp", "pisniar.worker"))
        for module in imported
    )


def test_the_worker_import_scan_resolves_the_from_import_form() -> None:
    assert worker_modules_imported_by("pisniar.worker.peaks") >= {
        "pisniar.worker.storage",
        "pisniar.worker.ffmpeg",
        "pisniar.worker.peaks_format",
    }


def test_the_scan_sees_the_webapp_package() -> None:
    scanned = imported_modules(SOURCE_ROOT / "webapp")
    assert "webapp/auth.py" in scanned
    assert "pisniar.config" in scanned["webapp/app.py"]


def test_bot_worker_and_api_do_not_import_webapp() -> None:
    for package in ("bot", "worker", "api"):
        assert offenders(package, ("pisniar.webapp",)) == []


def test_bot_imports_only_the_listed_modules_of_the_restricted_packages() -> None:
    assert unlisted_imports("bot", BOT_RESTRICTED_PACKAGES, BOT_ALLOWED_MODULES) == []


def test_unlisted_imports_reports_a_module_outside_the_allowed_set() -> None:
    found = unlisted_imports("bot", ("pisniar.resolve",), frozenset({"pisniar.resolve.urls"}))
    assert "bot/handlers.py imports pisniar.resolve.types" in found
    assert not any(line.endswith("pisniar.resolve.urls") for line in found)


def test_every_allowed_bot_module_is_still_imported_by_the_bot() -> None:
    used = set().union(*imported_modules(SOURCE_ROOT / "bot").values())
    assert used >= BOT_ALLOWED_MODULES


def modules_importing(package_name: str) -> list[str]:
    return [
        module_path
        for module_path, modules in imported_modules(SOURCE_ROOT).items()
        if any(name == package_name or name.startswith(f"{package_name}.") for name in modules)
    ]


def test_only_the_turntable_module_imports_pil() -> None:
    assert modules_importing("PIL") == ["worker/turntable.py"]


def test_the_pil_scan_finds_an_import_where_there_is_one() -> None:
    assert modules_importing("celery") != []
