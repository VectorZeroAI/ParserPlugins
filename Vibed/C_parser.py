#!/usr/bin/env python3
"""
C / C++ #include resolver for ai-diagnos-lsp plugin system.

Handles:
    #include "relative/path.h"   -> resolved relative to the including file
    #include <name.h>            -> searched in scope dirs (project-local headers only;
                                    system headers from /usr/include etc. are ignored
                                    unless they happen to live inside the scope)

Common patterns resolved:
    #include "foo.h"
    #include "subdir/foo.hpp"
    #include <project/config.h>

Extension probing: exact match only — C/C++ headers must be named explicitly.
"""

import sys
import json
import re
from pathlib import Path

# #include "..." or #include <...>
INCLUDE_PATTERN = re.compile(
    r"""^\s*#\s*include\s*(?:"([^"]+)"|<([^>]+)>)""",
    re.MULTILINE,
)


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for s in scope_dirs:
        try:
            path.relative_to(s)
            return True
        except ValueError:
            pass
    return False


def resolve_include(specifier: str, is_angle: bool, importing_file: Path, scope_dirs: list[Path]) -> Path | None:
    if not is_angle:
        # Quoted include — relative to the including file first
        candidate = (importing_file.parent / specifier).resolve()
        if candidate.is_file():
            return candidate
        # Then fall through to scope search

    # Search scope dirs (handles both <...> and quoted that didn't resolve above)
    for scope_dir in scope_dirs:
        candidate = (scope_dir / specifier).resolve()
        if candidate.is_file():
            return candidate

    return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"c_parser: failed to parse stdin JSON: {e}\n")
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

    results: list[str] = []
    seen: set[Path] = set()

    for match in INCLUDE_PATTERN.finditer(solid_file_content):
        quoted, angle = match.group(1), match.group(2)
        specifier = quoted or angle
        is_angle = angle is not None

        resolved = resolve_include(specifier, is_angle, importing_file, scope_dirs)
        if resolved is None or resolved in seen:
            continue
        if project_scope and not is_within_scope(resolved, scope_dirs):
            continue
        seen.add(resolved)
        results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

