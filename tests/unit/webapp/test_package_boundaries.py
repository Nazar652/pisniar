"""Package boundaries: `webapp` never imports `bot` and reaches `worker` only through three leaf
modules that load neither ffmpeg nor the tasks; nothing imports `webapp`; `bot` reaches
`resolve`, `post`, `telegram` and `worker` only through a short list of modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

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
