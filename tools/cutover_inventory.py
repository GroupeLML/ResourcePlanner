from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable


CANONICAL_LAYERS: dict[str, tuple[str, ...]] = {
    "app/domain": ("app.domain",),
    "app/application": ("app.application", "app.domain"),
    "app/infrastructure/sql": (
        "app.infrastructure.sql",
        "app.application",
        "app.domain",
    ),
    "app/infrastructure/acumatica": (
        "app.infrastructure.acumatica",
        "app.application",
        "app.domain",
    ),
    "app/infrastructure/m365": (
        "app.infrastructure.m365",
        "app.application",
        "app.domain",
    ),
    "app/infrastructure/smtp": (
        "app.infrastructure.smtp",
        "app.application",
        "app.domain",
    ),
    "app/server": (
        "app.server",
        "app.application",
        "app.domain",
        "app.infrastructure.sql",
        "app.infrastructure.acumatica",
        "app.infrastructure.m365",
        "app.infrastructure.smtp",
    ),
}

FORBIDDEN_EXTERNAL_IMPORTS = {"nicegui", "xlwings", "openpyxl"}
LEGACY_REQUIREMENTS = {"nicegui", "xlwings", "openpyxl"}
LEGACY_ENTRYPOINTS = ("main.py", "Lancer_Application.bat", "Installer.bat")
MIGRATION_TOOLS = ("tools/cutover_excel_to_sql.py",)
VERSIONED_MODULE = re.compile(r"^app/v\d.*\.py$", re.IGNORECASE)

# 336B: no historical V1 bridge or canonical boundary-debt exemption remains.
# New regressions must fail the existing architecture guard.
KNOWN_BOUNDARY_DEBT: dict[str, tuple[str, ...]] = {}


@dataclass(frozen=True, slots=True)
class BoundaryViolation:
    path: str
    line: int
    imported_module: str
    reason: str


@dataclass(frozen=True, slots=True)
class Inventory:
    legacy_entrypoints: tuple[str, ...]
    migration_tools: tuple[str, ...]
    legacy_requirements: tuple[str, ...]
    versioned_modules: tuple[str, ...]
    compatibility_modules: tuple[str, ...]
    ui_modules: tuple[str, ...]
    excel_modules: tuple[str, ...]
    runtime_steps: tuple[tuple[str, str], ...]
    boundary_violations: tuple[BoundaryViolation, ...]

    @property
    def unexpected_boundary_violations(self) -> tuple[BoundaryViolation, ...]:
        return tuple(item for item in self.boundary_violations if not _is_known_debt(item))

    @property
    def known_boundary_debt(self) -> tuple[BoundaryViolation, ...]:
        return tuple(item for item in self.boundary_violations if _is_known_debt(item))

    def as_dict(self) -> dict[str, object]:
        def serialize(items: tuple[BoundaryViolation, ...]) -> list[dict[str, object]]:
            return [
                {
                    "path": item.path,
                    "line": item.line,
                    "imported_module": item.imported_module,
                    "reason": item.reason,
                }
                for item in items
            ]

        return {
            "legacy_entrypoints": list(self.legacy_entrypoints),
            "migration_tools": list(self.migration_tools),
            "legacy_requirements": list(self.legacy_requirements),
            "versioned_modules": list(self.versioned_modules),
            "compatibility_modules": list(self.compatibility_modules),
            "ui_modules": list(self.ui_modules),
            "excel_modules": list(self.excel_modules),
            "runtime_steps": [
                {"name": name, "category": category}
                for name, category in self.runtime_steps
            ],
            "known_boundary_debt": serialize(self.known_boundary_debt),
            "unexpected_boundary_violations": serialize(self.unexpected_boundary_violations),
        }


def _module_and_package(root: Path, path: Path) -> tuple[str, str]:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts.pop()
    module = ".".join(parts)
    package = module if is_package else module.rsplit(".", 1)[0]
    return module, package


def _resolve_import_from(current_package: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = current_package.split(".") if current_package else []
    keep = max(0, len(package_parts) - (node.level - 1))
    prefix = package_parts[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _app_import_allowed(target: str, allowed_prefixes: tuple[str, ...]) -> bool:
    return any(target == prefix or target.startswith(prefix + ".") for prefix in allowed_prefixes)


def _is_known_debt(item: BoundaryViolation) -> bool:
    prefixes = KNOWN_BOUNDARY_DEBT.get(item.path, ())
    return any(
        item.imported_module == prefix or item.imported_module.startswith(prefix + ".")
        for prefix in prefixes
    )


def _scan_python_boundary(
    root: Path,
    path: Path,
    *,
    allowed_prefixes: tuple[str, ...],
) -> list[BoundaryViolation]:
    relative = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError) as exc:
        return [
            BoundaryViolation(
                relative,
                getattr(exc, "lineno", 0) or 0,
                "<parse-error>",
                "canonical source cannot be parsed",
            )
        ]

    _current_module, current_package = _module_and_package(root, path)
    violations: list[BoundaryViolation] = []
    for node in ast.walk(tree):
        targets: list[tuple[str, int]] = []
        if isinstance(node, ast.Import):
            targets.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            targets.append((_resolve_import_from(current_package, node), node.lineno))

        for target, line in targets:
            if not target:
                continue
            external_root = target.split(".", 1)[0]
            if external_root in FORBIDDEN_EXTERNAL_IMPORTS:
                violations.append(
                    BoundaryViolation(
                        relative,
                        line,
                        target,
                        "canonical Web/SQL code must not import V1 UI/Excel dependencies",
                    )
                )
                continue
            if target == "app" or target.startswith("app."):
                if not _app_import_allowed(target, allowed_prefixes):
                    violations.append(
                        BoundaryViolation(
                            relative,
                            line,
                            target,
                            "canonical layer imports a legacy or disallowed app module",
                        )
                    )
    return violations


def boundary_violations(root: Path) -> tuple[BoundaryViolation, ...]:
    violations: list[BoundaryViolation] = []
    for directory, allowed in CANONICAL_LAYERS.items():
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            violations.extend(_scan_python_boundary(root, path, allowed_prefixes=allowed))
    return tuple(sorted(violations, key=lambda item: (item.path, item.line, item.imported_module)))


def _requirements(root: Path) -> tuple[str, ...]:
    path = root / "requirements.txt"
    if not path.exists():
        return ()
    found: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().casefold()
        if name in LEGACY_REQUIREMENTS:
            found.add(name)
    return tuple(sorted(found))


def _runtime_steps(root: Path) -> tuple[tuple[str, str], ...]:
    path = root / "app/runtime_composition.py"
    if not path.exists():
        return ()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    result: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "CompositionStep":
            continue
        if len(node.args) < 2:
            continue
        name, category = node.args[:2]
        if (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and isinstance(category, ast.Constant)
            and isinstance(category.value, str)
        ):
            result.append((name.value, category.value))
    return tuple(result)


def _app_python_files(root: Path) -> Iterable[Path]:
    app = root / "app"
    if not app.exists():
        return ()
    return sorted(app.rglob("*.py"))


def build_inventory(root: Path) -> Inventory:
    files = tuple(_app_python_files(root))
    relative = {path: path.relative_to(root).as_posix() for path in files}

    versioned = sorted(rel for rel in relative.values() if VERSIONED_MODULE.match(rel))
    compatibility = sorted(
        rel for path, rel in relative.items() if path.name.endswith("_compat.py")
    )
    ui = sorted(
        rel
        for path, rel in relative.items()
        if path.name == "ui.py" or path.name.endswith("_ui.py")
    )
    excel = sorted(
        rel
        for path, rel in relative.items()
        if (
            ("excel" in path.name.casefold() or "/infrastructure/excel/" in f"/{rel.casefold()}/")
            and not rel.startswith("app/infrastructure/erp_export/")
        )
    )

    return Inventory(
        legacy_entrypoints=tuple(path for path in LEGACY_ENTRYPOINTS if (root / path).exists()),
        migration_tools=tuple(path for path in MIGRATION_TOOLS if (root / path).exists()),
        legacy_requirements=_requirements(root),
        versioned_modules=tuple(versioned),
        compatibility_modules=tuple(compatibility),
        ui_modules=tuple(ui),
        excel_modules=tuple(excel),
        runtime_steps=_runtime_steps(root),
        boundary_violations=boundary_violations(root),
    )


def render_markdown(inventory: Inventory) -> str:
    categories = Counter(category for _name, category in inventory.runtime_steps)
    lines = [
        "# V1 → SQL runtime cutover inventory",
        "",
        "## Summary",
        f"- Legacy entrypoints: **{len(inventory.legacy_entrypoints)}**",
        f"- Legacy runtime dependencies: **{len(inventory.legacy_requirements)}**",
        f"- Versioned V1 modules: **{len(inventory.versioned_modules)}**",
        f"- Compatibility modules: **{len(inventory.compatibility_modules)}**",
        f"- NiceGUI/UI modules: **{len(inventory.ui_modules)}**",
        f"- Excel-named/adaptor modules: **{len(inventory.excel_modules)}**",
        f"- Transitional runtime composition steps: **{len(inventory.runtime_steps)}**",
        f"- Known canonical boundary debt: **{len(inventory.known_boundary_debt)}**",
        f"- Unexpected canonical boundary violations: **{len(inventory.unexpected_boundary_violations)}**",
        "",
        "## Runtime composition categories",
    ]
    if categories:
        lines.extend(f"- `{category}`: {count}" for category, count in sorted(categories.items()))
    else:
        lines.append("- none")

    sections = (
        ("Legacy entrypoints", inventory.legacy_entrypoints),
        ("Legacy dependencies in requirements.txt", inventory.legacy_requirements),
        ("One-shot migration tools", inventory.migration_tools),
        ("Versioned V1 modules", inventory.versioned_modules),
        ("Compatibility modules", inventory.compatibility_modules),
        ("UI modules", inventory.ui_modules),
        ("Excel modules/adapters", inventory.excel_modules),
    )
    for title, values in sections:
        lines.extend(("", f"## {title}"))
        if values:
            lines.extend(f"- `{value}`" for value in values)
        else:
            lines.append("- none")

    lines.extend(("", "## Known canonical boundary debt"))
    if inventory.known_boundary_debt:
        for item in inventory.known_boundary_debt:
            lines.append(
                f"- `{item.path}:{item.line}` → `{item.imported_module}` — baseline to remove"
            )
    else:
        lines.append("- none")

    lines.extend(("", "## Unexpected canonical boundary violations"))
    if inventory.unexpected_boundary_violations:
        for item in inventory.unexpected_boundary_violations:
            lines.append(
                f"- `{item.path}:{item.line}` → `{item.imported_module}` — {item.reason}"
            )
    else:
        lines.append("- none — no regression beyond the explicit cutover baseline")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory transitional V1 dependencies and guard canonical Web/SQL imports."
    )
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--check-boundaries",
        action="store_true",
        help="Return exit code 1 for boundary regressions beyond the explicit known-debt baseline.",
    )
    args = parser.parse_args()

    inventory = build_inventory(Path(args.root).resolve())
    if args.format == "json":
        print(json.dumps(inventory.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_markdown(inventory), end="")

    if args.check_boundaries and inventory.unexpected_boundary_violations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
