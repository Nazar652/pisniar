"""Package boundaries: `webapp` never imports `bot` or `worker`, and neither imports it."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "pisniar"


def imported_modules(package_dir: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
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


def test_webapp_does_not_import_bot_or_worker() -> None:
    assert offenders("webapp", ("pisniar.bot", "pisniar.worker")) == []


def test_the_scan_sees_the_webapp_package() -> None:
    scanned = imported_modules(SOURCE_ROOT / "webapp")
    assert "webapp/auth.py" in scanned
    assert "pisniar.config" in scanned["webapp/app.py"]


def test_bot_worker_and_api_do_not_import_webapp() -> None:
    for package in ("bot", "worker", "api"):
        assert offenders(package, ("pisniar.webapp",)) == []
