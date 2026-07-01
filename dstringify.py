#!/usr/bin/env python3
"""Convert textwrap.dedent(string-literal) calls to d-strings in Lib/."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXCLUDES = {
    "Lib/test/test_textwrap.py",
    "Lib/test/test_dstring.py",
    # some tests check line numbers, so skip
    "Lib/test/test_ast.py",
    "Lib/test/test_bdb.py",
    "Lib/test/test_capi/test_misc.pu",
    "Lib/test/test_compile.py",
    "Lib/test/test_tokenize.py",
}

PREFIX_RE = re.compile(
    r'^([fFrRbBdD]*)([\'"]{3})',
)


@dataclass
class Replacement:
    start: int
    end: int
    new_text: str
    lineno: int
    reason: str


def normalize_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def is_excluded(path: Path, root: Path, excludes: set[str]) -> bool:
    rel = normalize_path(path, root)
    return rel in excludes or rel.replace("\\", "/") in excludes


def is_textwrap_dedent(node: ast.Call) -> bool:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "dedent"
        and isinstance(func.value, ast.Name)
        and func.value.id == "textwrap"
    ):
        return True
    return isinstance(func, ast.Name) and func.id == "dedent"


def literal_expr(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.Constant, ast.JoinedStr)):
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            return None
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format" and not node.keywords:
            inner = literal_expr(node.func.value)
            if inner is not None:
                return node
    return None


def mod_format_literal(
    node: ast.AST,
) -> tuple[ast.AST, ast.BinOp | None] | None:
    """Return ``(literal, binop)`` for a literal or ``literal % rhs`` dedent argument."""
    if literal_expr(node) is not None:
        return node, None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if literal_expr(node.left) is not None:
            return node.left, node
    return None


def expr_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            names.add(n.id)
    return names


def verification_names(inner: ast.AST) -> set[str]:
    if isinstance(inner, ast.JoinedStr):
        return fstring_names(inner)
    names = expr_names(inner)
    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
        if inner.func.attr == "format":
            names |= fstring_names(inner)
    return names


def fstring_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.FormattedValue):
            for sub in ast.walk(n.value):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def make_verify_globals() -> dict:
    class _Self:
        delimiters = ("=", ":")
        strict = True

    class _PdbServer:
        @staticmethod
        def protocol_version():
            return 1

    class _Colorize:
        @staticmethod
        def can_colorize():
            return True

    g: dict = {
        "__builtins__": __builtins__,
        "self": _Self(),
        "sys": __import__("sys"),
        "os": __import__("os"),
        "json": __import__("json"),
        "textwrap": textwrap,
        "dedent": textwrap.dedent,
        "ver": "3.16",
        "pyrepl_keys": "",
        "port": 12345,
        "commands": ["c"],
        "use_signal_thread": False,
        "colorize": True,
        "client_address": ("127.0.0.1", 1234),
        "request": None,
        "threading": __import__("threading"),
        "_PdbServer": _PdbServer,
        "_colorize": _Colorize,
    }
    return g


def values_equivalent(old: object, new: object) -> bool:
    if old == new:
        return True
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    if old.lstrip("\n") == new.lstrip("\n"):
        return True
    if old.rstrip() == new.rstrip():
        return True
    return False


def values_diff_reason(old: object, new: object) -> str:
    if not isinstance(old, str) or not isinstance(new, str):
        return (
            f"value types differ: {type(old).__name__} vs {type(new).__name__}"
        )
    if len(old) != len(new):
        return f"string lengths differ: {len(old)} vs {len(new)}"
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            start = max(0, i - 20)
            end = i + 20
            return (
                f"strings differ at index {i}: "
                f"{old[start:end]!r} vs {new[start:end]!r}"
            )
    return "strings differ"


def add_d_prefix(literal_src: str) -> str | None:
    m = PREFIX_RE.match(literal_src)
    if not m:
        return None
    prefix, quote = m.groups()
    rest = literal_src[m.end() :]
    if rest.startswith("\\"):
        rest = rest[1:]
        if not rest.startswith("\n"):
            return None
    elif not rest.startswith("\n"):
        return None
    letters = prefix.lower()
    if "u" in letters:
        return None
    if "d" in letters:
        return None
    new_prefix = prefix + "d" if prefix else "d"
    return f"{new_prefix}{quote}{rest}"


def fix_closing_indent(literal_src: str) -> str:
    """Align closing triple-quote indent with content common indent for d-string."""
    m = PREFIX_RE.match(literal_src)
    if not m:
        return literal_src
    quote = m.group(2)
    body = literal_src[m.end() :]
    close_idx = body.rfind(quote)
    if close_idx < 0:
        return literal_src
    content = body[:close_idx]
    close_line_start = body.rfind("\n", 0, close_idx) + 1
    lines = content.splitlines()
    common = common_leading_whitespace(lines)
    if common is None:
        return literal_src
    close_stripped = body[close_line_start : close_idx + len(quote)].lstrip(
        " \t"
    )
    if not close_stripped.startswith(quote):
        return literal_src
    new_close = common + close_stripped
    new_body = (
        body[:close_line_start] + new_close + body[close_idx + len(quote) :]
    )
    return literal_src[: m.end()] + new_body


def convert_literal(literal_src: str) -> str | None:
    converted = add_d_prefix(literal_src)
    if converted is None:
        return None
    return fix_closing_indent(converted)


def get_source(source: str, node: ast.AST) -> str | None:
    segment = ast.get_source_segment(source, node)
    return segment


def find_call_extent(source: str, call: ast.Call) -> tuple[int, int]:
    start = call.col_offset
    if hasattr(call, "end_col_offset") and call.end_col_offset is not None:
        end = call.end_lineno, call.end_col_offset
    else:
        end = None
    # Use lineno-based slice: find from line start
    lines = source.splitlines(keepends=True)
    line_start = sum(len(lines[i]) for i in range(call.lineno - 1))
    start = line_start + call.col_offset
    if call.end_lineno is not None and call.end_col_offset is not None:
        end_pos = (
            sum(len(lines[i]) for i in range(call.end_lineno - 1))
            + call.end_col_offset
        )
    else:
        end_pos = start + len(get_source(source, call) or "")
    return start, end_pos


def find_replacements(
    source: str,
    tree: ast.AST,
    *,
    file_label: str = "",
) -> list[Replacement]:
    replacements: list[Replacement] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not is_textwrap_dedent(node):
            continue
        if len(node.args) != 1 or node.keywords:
            continue
        arg = node.args[0]
        parsed = mod_format_literal(arg)
        if parsed is None:
            continue
        literal_node, binop_node = parsed
        literal_src = get_source(source, literal_node)
        if literal_src is None:
            continue
        new_literal = convert_literal(literal_src)
        if new_literal is None:
            continue
        if binop_node is not None:
            binop_src = get_source(source, binop_node)
            if binop_src is None:
                continue
            new_expr = binop_src.replace(literal_src, new_literal, 1)
        else:
            new_expr = new_literal
        old_call_src = get_source(source, node)
        if old_call_src is None:
            continue
        ctx = (
            f"{file_label}:{node.lineno}"
            if file_label
            else f"line {node.lineno}"
        )
        start, end = find_call_extent(source, node)
        replacements.append(
            Replacement(start, end, new_expr, node.lineno, "dedent")
        )
    return replacements


def common_leading_whitespace(lines: list[str]) -> str | None:
    indents = []
    for line in lines:
        if not line.strip():
            continue
        m = re.match(r"[ \t]+", line)
        if not m:
            return ""
        indents.append(m.group(0))
    if not indents:
        return ""
    prefix = indents[0]
    for ind in indents[1:]:
        while prefix and not ind.startswith(prefix):
            prefix = prefix[:-1]
    if any(
        not line.startswith(prefix) and line.strip()
        for line in lines
        if line.strip()
    ):
        # check prefix is valid for all
        for line in lines:
            if not line.strip():
                continue
            if not line.startswith(prefix):
                return None
    return prefix


def apply_replacements(source: str, replacements: list[Replacement]) -> str:
    for rep in sorted(replacements, key=lambda r: r.start, reverse=True):
        source = source[: rep.start] + rep.new_text + source[rep.end :]
    return source


def remove_unused_textwrap_import(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    uses_textwrap = False
    uses_dedent = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "textwrap":
            uses_textwrap = True
        if isinstance(node, ast.Name) and node.id == "dedent":
            uses_dedent = True
        if isinstance(node, ast.Attribute) and isinstance(
            node.value, ast.Name
        ):
            if node.value.id == "textwrap":
                uses_textwrap = True
    if uses_textwrap or uses_dedent:
        return source
    lines = source.splitlines(keepends=True)
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if (
            stripped == "import textwrap"
            or stripped == "from textwrap import dedent"
        ):
            continue
        if stripped.startswith("import ") and "textwrap" in stripped:
            mods = [m.strip() for m in stripped[len("import ") :].split(",")]
            mods = [m for m in mods if m != "textwrap"]
            if mods:
                new_lines.append("import " + ", ".join(mods) + "\n")
            continue
        new_lines.append(line)
    return "".join(new_lines)


def read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"SKIP non-utf8 {path}", file=sys.stderr)
        return None


def process_file(
    path: Path, root: Path, python: str, apply: bool, excludes: set[str]
) -> list[Replacement]:
    if is_excluded(path, root, excludes):
        return []
    source = read_source(path)
    if source is None:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        print(f"SKIP syntax error {path}: {e}", file=sys.stderr)
        return []
    rel = normalize_path(path, root)
    replacements = find_replacements(source, tree, file_label=rel)
    if not replacements:
        return []
    new_source = apply_replacements(source, replacements)
    if apply:
        new_source = remove_unused_textwrap_import(new_source)
        path.write_text(new_source, encoding="utf-8")
    for r in sorted(replacements, key=lambda x: x.lineno):
        action = "APPLY" if apply else "DRY-RUN"
        print(f"{action} {rel}:{r.lineno} [{r.reason}]")
    return replacements


def iter_py_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.py"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target", nargs="?", default="Lib", help="File or directory"
    )
    parser.add_argument(
        "--python", default="./python.exe", help="Python executable"
    )
    parser.add_argument("--apply", action="store_true", help="Write changes")
    parser.add_argument(
        "--exclude", action="append", default=[], help="Relative path exclude"
    )
    args = parser.parse_args()

    root = Path(".").resolve()
    target = Path(args.target)
    excludes = set(DEFAULT_EXCLUDES) | set(args.exclude)
    global sys
    sys.executable = str(Path(args.python).resolve())

    total = 0
    for path in iter_py_files(target):
        reps = process_file(path, root, args.python, args.apply, excludes)
        total += len(reps)
    print(f"\nTotal replacements: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
