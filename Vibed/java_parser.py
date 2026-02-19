#!/usr/bin/env python3
"""
Java import resolver for ai-diagnos-lsp plugin system.

Java imports are fully-qualified class names that map directly to file paths.

    import com.example.myapp.utils.StringHelper;
    -> com/example/myapp/utils/StringHelper.java

    import com.example.myapp.models.*;   (wildcard — resolves entire package dir)
    -> com/example/myapp/models/*.java

Strategy:
    1. Parse all `import` statements (ignoring `import static` for simplicity,
       though we handle those too by stripping the `static` keyword).
    2. Convert dotted name to path.
    3. Search each scope dir for that path.
    4. Wildcard imports expand to all .java files in the resolved directory.
    5. java.* / javax.* / sun.* stdlib imports are skipped.
"""

import sys
import json
import re
from pathlib import Path

IMPORT_PATTERN = re.compile(
    r"""^import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;""",
    re.MULTILINE,
)

STDLIB_PREFIXES = (
    "java.", "javax.", "sun.", "com.sun.", "jdk.",
    "org.w3c.", "org.xml.", "org.ietf.", "org.omg.",
)


def is_stdlib(fqn: str) -> bool:
    return any(fqn.startswith(p) for p in STDLIB_PREFIXES)


def fqn_to_path(fqn: str) -> tuple[str, bool]:
    """Convert a fully-qualified name to a relative file path. Returns (path, is_wildcard)."""
    is_wildcard = fqn.endswith(".*")
    if is_wildcard:
        fqn = fqn[:-2]  # strip .*
    return fqn.replace(".", "/"), is_wildcard


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
        sys.stderr.write(f"java_parser: failed to parse stdin JSON: {e}\n")
        print("[]")
        return

    solid_file_content: str = payload.get("solid_file_content", "")
    project_scope: list[str] = payload.get("project_scope", [])
    file_path_str: str = payload.get("file_path", "")

    if not file_path_str:
        print("[]")
        return

    scope_dirs = [Path(s).resolve() for s in project_scope if Path(s).is_dir()]

    # Also add common Maven/Gradle source roots automatically
    extra_roots: list[Path] = []
    for scope_dir in scope_dirs:
        for src_root in ["src/main/java", "src/test/java", "src"]:
            candidate = scope_dir / src_root
            if candidate.is_dir():
                extra_roots.append(candidate.resolve())
    all_roots = list(dict.fromkeys(scope_dirs + extra_roots))  # deduplicate, preserve order

    fqns = IMPORT_PATTERN.findall(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for fqn in fqns:
        if is_stdlib(fqn):
            continue

        rel_path, is_wildcard = fqn_to_path(fqn)

        for root in all_roots:
            if is_wildcard:
                pkg_dir = root / rel_path
                if pkg_dir.is_dir():
                    for java_file in pkg_dir.glob("*.java"):
                        resolved = java_file.resolve()
                        if resolved in seen:
                            continue
                        if project_scope and not is_within_scope(resolved, scope_dirs):
                            continue
                        seen.add(resolved)
                        results.append(resolved.as_posix())
            else:
                candidate = (root / f"{rel_path}.java").resolve()
                if candidate.is_file() and candidate not in seen:
                    if project_scope and not is_within_scope(candidate, scope_dirs):
                        continue
                    seen.add(candidate)
                    results.append(candidate.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

