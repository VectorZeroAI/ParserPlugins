#!/usr/bin/env python3
"""
C# import resolver for ai-diagnos-lsp plugin system.

C# `using` directives refer to namespaces, not files. To resolve them to
actual source files we:

    1. Walk all .cs files inside the project scope.
    2. For each file, extract its namespace declaration.
    3. Build a namespace -> [file, ...] index.
    4. For each `using X.Y.Z;` in the analysed file, look up X.Y.Z (and all
       its sub-namespaces) in the index and return matching files.

Additionally, `using static X.Y.Z.ClassName;` is resolved the same way.
`global using` (C# 10+) directives are also captured.

This approach requires scanning source files once — for a large project this
can be slow on the very first run, but is accurate because it doesn't rely
on any external tools.

File-scoped namespaces (C# 10+):  namespace Foo.Bar;
Traditional block namespaces:     namespace Foo.Bar { ... }
"""

import sys
import json
import re
from pathlib import Path
from typing import DefaultDict
from collections import defaultdict

# using Foo.Bar.Baz;  /  using static Foo.Bar;  /  global using Foo.Bar;
USING_PATTERN = re.compile(
    r"""^(?:global\s+)?using\s+(?:static\s+)?([\w.]+)\s*;""",
    re.MULTILINE,
)

# namespace Foo.Bar.Baz;       (file-scoped, C# 10+)
# namespace Foo.Bar.Baz { ...  (block, any version)
NAMESPACE_PATTERN = re.compile(
    r"""^namespace\s+([\w.]+)\s*[{;]""",
    re.MULTILINE,
)

# Common .NET system namespaces to skip
SYSTEM_PREFIXES = (
    "System", "Microsoft", "Windows", "Xamarin",
    "NUnit", "Xunit", "Moq",
)


def is_system_ns(ns: str) -> bool:
    return any(ns == p or ns.startswith(p + ".") for p in SYSTEM_PREFIXES)


def build_namespace_index(scope_dirs: list[Path]) -> DefaultDict[str, list[Path]]:
    """Scan all .cs files in scope and map namespace -> list of files."""
    index: DefaultDict[str, list[Path]] = defaultdict(list)
    for scope_dir in scope_dirs:
        for cs_file in scope_dir.rglob("*.cs"):
            try:
                text = cs_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for ns_match in NAMESPACE_PATTERN.finditer(text):
                ns = ns_match.group(1)
                index[ns].append(cs_file.resolve())
    return index


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
        sys.stderr.write(f"csharp_parser: failed to parse stdin JSON: {e}\n")
        print("[]")
        return

    solid_file_content: str = payload.get("solid_file_content", "")
    project_scope: list[str] = payload.get("project_scope", [])
    file_path_str: str = payload.get("file_path", "")

    if not file_path_str:
        print("[]")
        return

    scope_dirs = [Path(s).resolve() for s in project_scope if Path(s).is_dir()]

    if not scope_dirs:
        print("[]")
        return

    # Build namespace index from the project
    ns_index = build_namespace_index(scope_dirs)

    used_namespaces = USING_PATTERN.findall(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for ns in used_namespaces:
        if is_system_ns(ns):
            continue

        # Direct namespace match
        for file_path in ns_index.get(ns, []):
            if file_path not in seen:
                seen.add(file_path)
                results.append(file_path.as_posix())

        # Also include files whose namespace starts with this prefix
        # (e.g. `using App.Services` pulls in App.Services.UserService)
        prefix = ns + "."
        for indexed_ns, files in ns_index.items():
            if indexed_ns.startswith(prefix):
                for file_path in files:
                    if file_path not in seen:
                        seen.add(file_path)
                        results.append(file_path.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

