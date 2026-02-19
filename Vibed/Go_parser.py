#!/usr/bin/env python3
"""
Go import resolver for ai-diagnos-lsp plugin system.

Go imports are module-path based. Given a go.mod file we know the module
root (e.g. "github.com/foo/myapp") and can map any import that starts with
that prefix to a local directory.

Handles:
    import "github.com/foo/myapp/internal/util"
    import (
        "fmt"
        "github.com/foo/myapp/pkg/config"
        mypkg "github.com/foo/myapp/models"
    )

Strategy:
    1. Find go.mod by walking up from file_path.
    2. Parse the module name from go.mod.
    3. For each import string that starts with the module name, strip the
       module prefix and resolve the remainder as a directory under the
       module root, returning all .go files in that directory (excluding
       _test.go files, but including them if you want — configurable below).
    4. Imports that don't start with the module name are stdlib/third-party
       and are ignored (they won't be in scope).
    5. Scope filtering is applied as usual.
"""

import sys
import json
import re
from pathlib import Path

INCLUDE_TEST_FILES = False  # set True to also return _test.go files

# Single import: import "path"  or  import alias "path"
SINGLE_IMPORT_PATTERN = re.compile(
    r"""^import\s+(?:\w+\s+)?["']([^"']+)["']""",
    re.MULTILINE,
)

# Block import: import ( ... )
BLOCK_IMPORT_PATTERN = re.compile(
    r"""import\s*\(([^)]+)\)""",
    re.MULTILINE | re.DOTALL,
)

# Individual path inside a block
BLOCK_PATH_PATTERN = re.compile(
    r"""(?:\w+\s+)?["']([^"']+)["']"""
)


def find_go_mod(start: Path) -> tuple[Path, str] | tuple[None, None]:
    """Walk up to find go.mod; return (module_root_dir, module_name)."""
    for parent in [start, *start.parents]:
        go_mod = parent / "go.mod"
        if go_mod.is_file():
            text = go_mod.read_text(encoding="utf-8")
            match = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
            if match:
                return parent, match.group(1)
    return None, None


def extract_imports(source: str) -> list[str]:
    imports: list[str] = []

    # Block imports first
    for block_match in BLOCK_IMPORT_PATTERN.finditer(source):
        block_body = block_match.group(1)
        for path_match in BLOCK_PATH_PATTERN.finditer(block_body):
            imports.append(path_match.group(1))

    # Single-line imports (avoid double-counting those inside blocks by
    # checking they're not inside a captured block range)
    block_spans = [(m.start(), m.end()) for m in BLOCK_IMPORT_PATTERN.finditer(source)]

    for m in SINGLE_IMPORT_PATTERN.finditer(source):
        inside_block = any(start <= m.start() <= end for start, end in block_spans)
        if not inside_block:
            imports.append(m.group(1))

    return imports


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for s in scope_dirs:
        try:
            path.relative_to(s)
            return True
        except ValueError:
            pass
    return False


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"go_parser: failed to parse stdin JSON: {e}\n")
        print("[]")
        return

    solid_file_content: str = payload.get("solid_file_content", "")
    project_scope: list[str] = payload.get("project_scope", [])
    file_path_str: str = payload.get("file_path", "")

    if not file_path_str:
        print("[]")
        return

    importing_file = Path(file_path_str).resolve()
    scope_dirs = [Path(s).resolve() for s in project_scope if Path(s).is_dir()]

    module_root, module_name = find_go_mod(importing_file)
    if module_root is None:
        # No go.mod found — can't resolve anything
        print("[]")
        return

    import_paths = extract_imports(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for import_path in import_paths:
        if not import_path.startswith(module_name):
            continue  # stdlib or third-party

        # Strip module prefix to get relative directory
        relative = import_path[len(module_name):].lstrip("/")
        pkg_dir = (module_root / relative).resolve()

        if not pkg_dir.is_dir():
            continue

        # Return all .go files in that package directory (non-recursive;
        # Go packages are single-directory)
        for go_file in pkg_dir.glob("*.go"):
            if not INCLUDE_TEST_FILES and go_file.name.endswith("_test.go"):
                continue
            resolved = go_file.resolve()
            if resolved in seen:
                continue
            if project_scope and not is_within_scope(resolved, scope_dirs):
                continue
            seen.add(resolved)
            results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

