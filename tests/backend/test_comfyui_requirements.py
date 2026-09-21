"""``requirements.txt`` is what a ComfyUI install actually reads.

ComfyUI and ComfyUI-Manager install a node pack by running
``pip install -r requirements.txt``; neither of them reads
``pyproject.toml``.  A dependency that exists only in ``pyproject.toml``
is therefore absent from ComfyUI's Python environment, and the route
registration that needs it fails at startup while the sidebar tab keeps
rendering - the extension looks installed but has no HTTP API.

These tests pin the file's existence and keep it in step with
``[project].dependencies``.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REQUIREMENTS = _REPO_ROOT / "requirements.txt"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _requirement_lines() -> list[str]:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _project_dependencies() -> list[str]:
    """Read ``[project].dependencies`` without ``tomllib``.

    ``tomllib`` is 3.11+, and this package supports 3.10; parsing the one
    array we care about keeps the test runnable on every supported
    interpreter.
    """

    text = _PYPROJECT.read_text(encoding="utf-8")
    project_section = re.search(
        r"^\[project\]\s*$(.*?)^\[", text, re.M | re.S
    )
    assert project_section, "[project] section not found in pyproject.toml"
    array = re.search(
        r"^dependencies\s*=\s*\[(.*?)\]", project_section.group(1), re.M | re.S
    )
    assert array, "[project].dependencies not found in pyproject.toml"
    return re.findall(r'"([^"]+)"', array.group(1))


def test_requirements_file_exists() -> None:
    assert _REQUIREMENTS.is_file(), (
        "requirements.txt is missing; a ComfyUI install would not pick up "
        "the runtime dependencies"
    )


def test_requirements_match_pyproject_dependencies() -> None:
    assert _requirement_lines() == _project_dependencies(), (
        "requirements.txt and [project].dependencies have drifted; the "
        "ComfyUI install would get a different dependency set than a pip "
        "install of this package"
    )


def test_requirements_pin_every_module_scope_third_party_import() -> None:
    """Every third-party module imported at ``src/`` module scope is declared.

    ``jsonschema`` was imported at module scope by ``llm_provider`` but was
    never installed into ComfyUI's environment, so route registration died
    at startup.  Module scope is the part that matters: those imports run
    the moment ComfyUI loads the package.  Imports inside functions (the
    optional ``modal`` / ``boto3`` backends) are deliberately not covered.
    """

    declared = {
        re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower()
        for line in _requirement_lines()
    }
    # Import name -> distribution name, where the two differ.
    distribution_of = {"pil": "pillow", "yaml": "pyyaml"}
    exempt = set(sys.stdlib_module_names) | {"manga_autopilot", "__future__"}

    missing: dict[str, Path] = {}
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _module_scope_import_roots(tree):
            if name in exempt:
                continue
            distribution = distribution_of.get(name.lower(), name.lower())
            if distribution not in declared:
                missing.setdefault(f"{name} -> {distribution}", path)

    assert not missing, (
        "module-scope third-party imports with no entry in requirements.txt: "
        + ", ".join(
            f"{key} ({value.relative_to(_REPO_ROOT)})" for key, value in missing.items()
        )
    )


def _module_scope_import_roots(tree: ast.Module) -> set[str]:
    """Root module names imported when the file itself is imported.

    Covers the module body and the bodies of top-level ``if`` statements
    (``if TYPE_CHECKING:`` and friends), which also execute on import.
    """

    roots: set[str] = set()
    statements: list[ast.stmt] = []
    for node in tree.body:
        statements.append(node)
        if isinstance(node, ast.If):
            statements.extend(node.body)
            statements.extend(node.orelse)

    for node in statements:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots
