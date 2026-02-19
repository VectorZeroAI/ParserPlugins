#!/usr/bin/env python3
"""
PHP import resolver for ai-diagnos-lsp plugin system.

Handles:
    require 'foo.php'
    require_once __DIR__ . '/foo.php'
    include 'bar.php'
    include_once '../baz.php'

    use App\\Models\\User;        -> PSR-4 resolved via composer.json autoload
    use App\\Http\\Controllers\\{UserController, PostController};  -> grouped use

PSR-4 resolution:
    Reads composer.json from the project root (first ancestor with it),
    parses autoload.psr-4 and autoload-dev.psr-4, maps namespace prefixes
    to directories, and converts namespace paths to file paths.

__DIR__ in require/include strings is replaced with the importing file's
directory before resolution.
"""

import sys
import json
import re
from pathlib import Path

# require / require_once / include / include_once with a string literal
# Handles both single and double quotes, and __DIR__ . '/...' concatenation
REQUIRE_PATTERN = re.compile(
    r"""(?:require|include)(?:_once)?\s*\(?\s*(?:__DIR__\s*\.\s*)?['"]([^'"]+)['"]\s*\)?""",
    re.MULTILINE,
)

# Single use: use Foo\Bar\Baz;  or  use Foo\Bar\Baz as B;
USE_PATTERN = re.compile(
    r"""^use\s+([\w\\]+)(?:\s+as\s+\w+)?\s*;""",
    re.MULTILINE,
)

# Grouped use: use Foo\Bar\{Baz, Qux};
GROUPED_USE_PATTERN = re.compile(
    r"""^use\s+([\w\\]+)\\?\{([^}]+)\}\s*;""",
    re.MULTILINE,
)


def find_composer_json(start: Path) -> tuple[Path | None, dict]:
    for parent in [start.parent, *start.parents]:
        candidate = parent / "composer.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return parent, data
            except Exception:
                pass
    return None, {}


def build_psr4_map(composer_data: dict, composer_dir: Path) -> list[tuple[str, Path]]:
    """
    Returns a list of (namespace_prefix, directory) sorted by prefix length
    descending (longest match first).
    """
    mappings: list[tuple[str, Path]] = []

    for section in ("autoload", "autoload-dev"):
        psr4 = composer_data.get(section, {}).get("psr-4", {})
        for ns_prefix, directories in psr4.items():
            if isinstance(directories, str):
                directories = [directories]
            for d in directories:
                abs_dir = (composer_dir / d).resolve()
                # Normalise namespace prefix: ensure trailing backslash
                ns_norm = ns_prefix if ns_prefix.endswith("\\") else ns_prefix + "\\"
                mappings.append((ns_norm, abs_dir))

    # Longest prefix first for greedy matching
    mappings.sort(key=lambda x: len(x[0]), reverse=True)
    return mappings


def resolve_fqn(fqn: str, psr4_map: list[tuple[str, Path]]) -> Path | None:
    """Resolve a fully-qualified PHP class name to a file path."""
    # Normalise: PHP FQNs may have leading backslash
    fqn = fqn.lstrip("\\")

    for ns_prefix, base_dir in psr4_map:
        ns_prefix_norm = ns_prefix.lstrip("\\")
        if fqn.startswith(ns_prefix_norm):
            relative = fqn[len(ns_prefix_norm):]
            # Convert namespace separators to path separators
            relative_path = relative.replace("\\", "/")
            candidate = (base_dir / f"{relative_path}.php").resolve()
            if candidate.is_file():
                return candidate
    return None


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
        sys.stderr.write(f"php_parser: failed to parse stdin JSON: {e}\n")
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

    composer_dir, composer_data = find_composer_json(importing_file)
    psr4_map = build_psr4_map(composer_data, composer_dir) if composer_dir else []

    results: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path not in seen:
            if not project_scope or is_within_scope(path, scope_dirs):
                seen.add(path)
                results.append(path.as_posix())

    # --- require / include ---
    for spec in REQUIRE_PATTERN.findall(solid_file_content):
        # Handle __DIR__-relative paths (the regex already strips __DIR__ . )
        # spec may start with / (absolute) or be relative
        if spec.startswith("/"):
            candidate = Path(spec).resolve()
        else:
            candidate = (importing_file.parent / spec).resolve()

        if candidate.is_file():
            add(candidate)

    # --- use statements (PSR-4) ---
    # Single use
    for fqn in USE_PATTERN.findall(solid_file_content):
        resolved = resolve_fqn(fqn, psr4_map)
        if resolved:
            add(resolved)

    # Grouped use:  use Foo\Bar\{Baz, Qux as Q};
    for match in GROUPED_USE_PATTERN.finditer(solid_file_content):
        ns_base = match.group(1).rstrip("\\")
        members_raw = match.group(2)
        for member in members_raw.split(","):
            # Strip alias and whitespace
            member = re.sub(r"\s+as\s+\w+", "", member).strip()
            if member:
                fqn = f"{ns_base}\\{member}"
                resolved = resolve_fqn(fqn, psr4_map)
                if resolved:
                    add(resolved)

    print(json.dumps(results))


if __name__ == "__main__":
    main()

