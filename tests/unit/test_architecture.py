"""The dependency arrow points one way: archicad -> ingest -> core.

Enforced by walking the AST rather than by a linter plugin, so it needs no
dependency and fails with a message naming the offending file and line.

This is not style policing. ``core`` is the layer whose correctness can be
established by tests that run without Archicad; the moment a compliance rule or
a geometry operation drifts into the adapter, or an Archicad import drifts into
``core``, that property is gone and nobody notices until the numbers are wrong.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "sun_study"

# Each layer may import from itself and from anything below it. Anything not
# listed is forbidden.
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "core": set(),
    "rules": {"core"},
    "ingest": {"core"},
    "report": {"core", "rules"},
    "archicad": {"core", "rules", "ingest", "report"},
}

LAYERS = sorted(ALLOWED_IMPORTS)


def _layer_modules(layer: str) -> list[Path]:
    directory = PACKAGE_ROOT / layer
    if not directory.is_dir():
        return []
    return sorted(directory.rglob("*.py"))


def _imported_sun_study_modules(source: str, filename: str) -> list[tuple[str, int]]:
    """Every ``sun_study.<something>`` name imported, with its line number."""
    tree = ast.parse(source, filename=filename)
    found: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sun_study."):
                    found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import. Flagged rather than resolved: the layering
                # should be readable at the import site.
                found.append((f"RELATIVE:{node.level}:{node.module or ''}", node.lineno))
            elif node.module and node.module.startswith("sun_study."):
                found.append((node.module, node.lineno))
    return found


@pytest.mark.parametrize("layer", LAYERS)
def test_layer_only_imports_from_permitted_layers(layer: str) -> None:
    permitted = ALLOWED_IMPORTS[layer] | {layer}
    violations: list[str] = []

    for path in _layer_modules(layer):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        for imported, line in _imported_sun_study_modules(
            path.read_text(encoding="utf-8"), relative
        ):
            if imported.startswith("RELATIVE:"):
                violations.append(
                    f"{relative}:{line} uses a relative import; write the full "
                    f"path so the layering is visible at the import site"
                )
                continue
            parts = imported.split(".")
            if len(parts) < 2:
                continue
            target_layer = parts[1]
            if target_layer in LAYERS and target_layer not in permitted:
                violations.append(
                    f"{relative}:{line} imports {imported} -- "
                    f"'{layer}' may not depend on '{target_layer}'"
                )

    assert not violations, "Layering violations:\n  " + "\n  ".join(violations)


def test_core_imports_no_third_party_beyond_numpy() -> None:
    """``core`` stays pure: standard library plus numpy, nothing else.

    trimesh and its embree backend arrive in ``core.occlusion`` at milestone
    M1 behind an optional import with a numpy fallback, and this allowlist is
    the place that decision gets recorded when it does.
    """
    allowed_third_party = {"numpy"}
    stdlib_ok = set(getattr(__import__("sys"), "stdlib_module_names", set()))
    violations: list[str] = []

    for path in _layer_modules("core"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root in {"sun_study", *allowed_third_party} or root in stdlib_ok:
                    continue
                violations.append(f"{relative}:{node.lineno} imports {name}")

    assert not violations, (
        "core must not depend on anything but the standard library and numpy:\n  "
        + "\n  ".join(violations)
    )


def test_core_does_not_perform_io() -> None:
    """No file or network access in ``core``.

    ``core`` takes geometry and numbers in and returns numbers out. If it grows
    a file read, some caller has started passing it a path instead of data.
    """
    forbidden = {"open", "input"}
    violations: list[str] = []

    for path in _layer_modules("core"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden
            ):
                violations.append(f"{relative}:{node.lineno} calls {node.func.id}()")

    assert not violations, "core must not perform I/O:\n  " + "\n  ".join(violations)


def test_every_declared_layer_directory_exists() -> None:
    """Keeps this test honest: a renamed package must update the allowlist."""
    missing = [layer for layer in LAYERS if not (PACKAGE_ROOT / layer).is_dir()]
    assert not missing, f"declared layers with no directory: {missing}"
