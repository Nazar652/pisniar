"""Package boundaries: `webapp` never imports `bot` or `worker`, and neither imports it;
`bot` reaches `resolve`, `post`, `telegram` and `worker` only through a short list of modules.
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


def unlisted_imports(
    package: str, restricted: tuple[str, ...], allowed: frozenset[str]
) -> list[str]:
    result = []
    for module_path, modules in imported_modules(SOURCE_ROOT / package).items():
        for module in sorted(modules - allowed):
            if any(module == name or module.startswith(f"{name}.") for name in restricted):
                result.append(f"{module_path} imports {module}")
    return result


def test_webapp_does_not_import_bot_or_worker() -> None:
    assert offenders("webapp", ("pisniar.bot", "pisniar.worker")) == []


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
