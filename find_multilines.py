#!/usr/bin/env python3
"""Find Python code that builds multiline strings via literal concatenation.

Detects patterns that are candidates for ``textwrap.dedent()`` / d-string
conversion (human review required).

**Concatenation** (only reports when **every** operand is a string literal, the
**first** literal ends with ``\\n``, and at least one literal contains an
embedded newline):

1. **explicit** — ``"a\\n" + "b"`` (only literal operands).

2. **implicit** — adjacent string literals ``"a\\n" "b"`` spanning multiple
   source lines (tokenize-based; AST merges these into one constant and cannot
   see the parts).  Same-line implicit concatenation is ignored.

**Literal** — triple-quoted string literals (including ``f``/``t``/``b``/``r`` prefixes
and combinations) spanning at least three source lines that could be prefixed with
``d`` (not already a d-string; docstrings excluded; closing quotes must be indented).

Usage::

    ./python.exe Tools/scripts/find_multilines.py Lib/
    ./python.exe Tools/scripts/find_multilines.py Lib/test
    ./python.exe Tools/scripts/find_multilines.py Lib/ --kind explicit
    ./python.exe Tools/scripts/find_multilines.py Lib/ --kind literal
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


PREFIX_RE = re.compile(
    r'^([fFrRbBdDtT]*)([\'"]{3})',
)


@dataclass(frozen=True)
class Hit:
    path: Path
    lineno: int
    col_offset: int
    kind: str  # 'explicit' | 'implicit' | 'literal'
    parts: int
    has_multiline: bool
    snippet: str
    lines: int = 0
    score: int = 0

    def format(self, root: Path) -> str:
        try:
            display = self.path.relative_to(root)
        except ValueError:
            display = self.path
        flags = []
        if self.kind == "literal":
            flags.append(f"{self.lines} lines")
        else:
            if self.has_multiline:
                flags.append("multiline")
            flags.append(f"{self.parts} parts")
        flag_s = ", ".join(flags)
        return (f"{display}:{self.lineno}:{self.col_offset + 1}: [{self.kind}; {flag_s}]\n" +
                f"{' '*self.col_offset}{self.snippet}")


def _string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _unwrap_string_expr(node: ast.AST) -> ast.AST:
    """Peel ``"..."``.strip()`` / ``.rstrip()`` etc. for analysis."""
    while isinstance(node, ast.Call) and not node.args and not node.keywords:
        if isinstance(node.func, ast.Attribute):
            node = node.func.value
            continue
        break
    return node


def _expr_has_string_literal(
    node: ast.AST, *, multiline: bool = False
) -> bool:
    node = _unwrap_string_expr(node)
    value = _string_value(node)
    if value is not None:
        return (not multiline) or ("\n" in value)
    if isinstance(node, ast.JoinedStr):
        return not multiline
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return any(
            _expr_has_string_literal(op, multiline=multiline)
            for op in _flatten_add(node)
        )
    return False


def _operands_multiline(operands: list[ast.AST]) -> bool:
    return any(_expr_has_string_literal(op, multiline=True) for op in operands)


def _literal_operand_values(operands: list[ast.AST]) -> list[str] | None:
    """Return decoded string values when every operand is a string literal."""
    values: list[str] = []
    for op in operands:
        op = _unwrap_string_expr(op)
        value = _string_value(op)
        if value is None:
            return None
        values.append(value)
    return values


def _starts_with_newline_continuation(values: list[str]) -> bool:
    """True when the first literal ends with a newline (dedent candidate)."""
    return bool(values) and values[0].endswith("\n")


def _flatten_add(node: ast.AST) -> list[ast.AST]:
    """Flatten ``a + b + c`` into operand list."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_add(node.left) + _flatten_add(node.right)
    return [node]


def _snippet(source: str, node: ast.AST, *, max_lines: int = 4) -> str:
    segment = ast.get_source_segment(source, node)
    if not segment:
        return "<source unavailable>"
    lines = segment.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["..."]
    return "\n  ".join(line.rstrip() for line in lines)


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


class _AddFinder(ast.NodeVisitor):
    def __init__(
        self, path: Path, source: str, parents: dict[int, ast.AST]
    ) -> None:
        self.path = path
        self.source = source
        self.parents = parents
        self.hits: list[Hit] = []

    def visit_BinOp(self, node: ast.BinOp) -> None:
        parent = self.parents.get(id(node))
        if isinstance(node.op, ast.Add) and not (
            isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add)
        ):
            operands = _flatten_add(node)
            values = _literal_operand_values(operands)
            if (
                values is not None
                and len(values) >= 2
                and _starts_with_newline_continuation(values)
            ):
                multiline = _operands_multiline(operands)
                if multiline:
                    self.hits.append(
                        Hit(
                            path=self.path,
                            lineno=node.lineno,
                            col_offset=node.col_offset,
                            kind="explicit",
                            parts=len(operands),
                            has_multiline=multiline,
                            snippet=_snippet(self.source, node),
                            score=len(operands) * 30 + (50 if multiline else 0),
                        )
                    )
        self.generic_visit(node)


def _find_explicit(path: Path, source: str, tree: ast.AST) -> Iterator[Hit]:
    parents = _parent_map(tree)
    finder = _AddFinder(path, source, parents)
    finder.visit(tree)
    yield from finder.hits


def _dedupe_explicit(hits: list[Hit]) -> list[Hit]:
    """Keep only the largest Add expression per starting line."""
    by_line: dict[tuple[Path, int], Hit] = {}
    for hit in hits:
        key = (hit.path, hit.lineno)
        prev = by_line.get(key)
        if prev is None or hit.parts > prev.parts:
            by_line[key] = hit
    return sorted(
        by_line.values(), key=lambda h: (str(h.path), h.lineno, h.col_offset)
    )


def _literal_source_lines(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None)
    if end is None:
        return 1
    return end - node.lineno + 1


def _is_docstring_node(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    if not isinstance(parent, ast.Expr):
        return False
    grandparent = parents.get(id(parent))
    if not isinstance(
        grandparent,
        (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
    ):
        return False
    body = grandparent.body
    return bool(body) and body[0] is parent


def _is_inner_literal_part(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    return isinstance(parent, (ast.JoinedStr, ast.TemplateStr, ast.Interpolation))


def _is_d_string_source(literal_src: str) -> bool:
    m = PREFIX_RE.match(literal_src)
    return m is not None and "d" in m.group(1).lower()


def _common_leading_whitespace(lines: list[str]) -> str:
    indents: list[str] = []
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
    return prefix


def _literal_indent_score(literal_src: str) -> int:
    """Estimate dedent benefit from uniform leading whitespace."""
    m = PREFIX_RE.match(literal_src)
    if not m:
        return 0
    quote = m.group(2)
    body = literal_src[m.end() :]
    close_idx = body.rfind(quote)
    if close_idx < 0:
        return 0
    content_lines = body[:close_idx].splitlines()
    common = _common_leading_whitespace(content_lines)
    indent = len(common)
    content_count = sum(1 for line in content_lines if line.strip())
    return indent * content_count


def _score_literal_node(literal_src: str, lines: int) -> int:
    indent = _literal_indent_score(literal_src)
    return lines * 20 + indent * 3


def score_hit(hit: Hit, *, literal_src: str | None = None) -> int:
    """Higher scores suggest larger d-string conversion benefit."""
    if hit.kind == "literal":
        if literal_src is not None:
            return _score_literal_node(literal_src, hit.lines)
        return hit.lines * 20
    if hit.kind in {"explicit", "implicit"}:
        return hit.parts * 30 + (50 if hit.has_multiline else 0)
    return hit.lines


def _is_dstring_literal_candidate(literal_src: str) -> bool:
    """True when *literal_src* could take a ``d`` prefix (see ``dstringify``)."""
    m = PREFIX_RE.match(literal_src)
    if not m:
        return False
    prefix, _quote = m.groups()
    letters = prefix.lower()
    if "u" in letters or "d" in letters:
        return False
    rest = literal_src[m.end() :]
    if rest.startswith("\\"):
        rest = rest[1:]
        return rest.startswith("\n")
    return rest.startswith("\n")


def _closing_quote_has_indent(literal_src: str) -> bool:
    """True when the closing triple quote is indented or shares a line with content."""
    m = PREFIX_RE.match(literal_src)
    if not m:
        return False
    quote = m.group(2)
    body = literal_src[m.end() :]
    close_idx = body.rfind(quote)
    if close_idx < 0:
        return False
    line_start = body.rfind("\n", 0, close_idx) + 1
    before_close = body[line_start:close_idx]
    if before_close.strip():
        return True
    return bool(before_close) and before_close.isspace()


class _LiteralFinder(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        source: str,
        parents: dict[int, ast.AST],
        *,
        min_lines: int,
    ) -> None:
        self.path = path
        self.source = source
        self.parents = parents
        self.min_lines = min_lines
        self.hits: list[Hit] = []

    def _maybe_record(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (str, bytes)
        ):
            return
        if _is_docstring_node(node, self.parents):
            return
        literal_src = ast.get_source_segment(self.source, node)
        if literal_src is None:
            return
        if _is_d_string_source(literal_src):
            return
        if not _is_dstring_literal_candidate(literal_src):
            return
        if not _closing_quote_has_indent(literal_src):
            return
        lines = _literal_source_lines(node)
        if lines < self.min_lines:
            return
        self.hits.append(
            Hit(
                path=self.path,
                lineno=node.lineno,
                col_offset=node.col_offset,
                kind="literal",
                parts=1,
                has_multiline=True,
                snippet=_snippet(self.source, node),
                lines=lines,
                score=_score_literal_node(literal_src, lines),
            )
        )

    def visit_Constant(self, node: ast.Constant) -> None:
        if _is_inner_literal_part(node, self.parents):
            return
        self._maybe_record(node)
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._maybe_record(node)
        self.generic_visit(node)

    def visit_TemplateStr(self, node: ast.TemplateStr) -> None:
        self._maybe_record(node)
        self.generic_visit(node)


def _find_literals(
    path: Path,
    source: str,
    tree: ast.AST,
    *,
    min_lines: int,
) -> Iterator[Hit]:
    parents = _parent_map(tree)
    finder = _LiteralFinder(path, source, parents, min_lines=min_lines)
    finder.visit(tree)
    yield from finder.hits


def _implicit_group_spans_lines(group: list[tokenize.TokenInfo]) -> bool:
    """True when string literals in *group* start on more than one source line."""
    first, last = group[0], group[-1]
    return first.start[0] != last.start[0]


def _find_implicit(path: Path, source: str) -> Iterator[Hit]:
    lines = source.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type != tokenize.STRING:
            i += 1
            continue
        group = [tok]
        j = i + 1
        while j < len(tokens):
            mid = tokens[j]
            if mid.type == tokenize.STRING:
                group.append(mid)
                j += 1
                continue
            if mid.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT):
                j += 1
                continue
            break
        if len(group) >= 2 and _implicit_group_spans_lines(group):
            values: list[str] = []
            for t in group:
                try:
                    v = ast.literal_eval(t.string)
                except (SyntaxError, ValueError):
                    v = None
                if isinstance(v, bytes):
                    values.append(v.decode("latin1"))
                elif isinstance(v, str):
                    values.append(v)
            if len(values) >= 2 and _starts_with_newline_continuation(values):
                combined = "".join(values)
                has_multiline = "\n" in combined or any(
                    "\n" in v for v in values
                )
                if not has_multiline:
                    i = j if j > i + 1 else i + 1
                    continue
                first, last = group[0], group[-1]
                snippet_lines = lines[first.start[0] - 1 : last.end[0]]
                snippet = "".join(snippet_lines).strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
                yield Hit(
                    path=path,
                    lineno=first.start[0],
                    col_offset=first.start[1],
                    kind="implicit",
                    parts=len(group),
                    has_multiline=has_multiline,
                    snippet=snippet.replace("\n", "\n  "),
                    score=len(group) * 30 + (50 if has_multiline else 0),
                )
        i = j if j > i + 1 else i + 1


def scan_file(path: Path, *, kinds: set[str], min_lines: int) -> list[Hit]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    hits: list[Hit] = []
    tree: ast.AST | None = None
    if kinds & {"explicit", "literal"}:
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return hits
    if "explicit" in kinds and tree is not None:
        hits.extend(_find_explicit(path, source, tree))
        hits = _dedupe_explicit(hits)
    if "implicit" in kinds:
        hits.extend(_find_implicit(path, source))
    if "literal" in kinds and tree is not None:
        hits.extend(_find_literals(path, source, tree, min_lines=min_lines))
    return hits


def iter_py_files(paths: list[Path]) -> Iterator[Path]:
    for root in paths:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="+", type=Path, help="files or directories"
    )
    parser.add_argument(
        "--kind",
        choices=("explicit", "implicit", "literal", "both", "all"),
        default="both",
        help="which kinds to report: both=concatenation only, all=include literal (default: both)",
    )
    parser.add_argument(
        "--min-parts",
        type=int,
        default=3,
        help="minimum number of concatenated parts (default: 2)",
    )
    parser.add_argument(
        "--min-lines",
        type=int,
        default=3,
        help="minimum source lines for literal kind (default: 3)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="report only the top N hits by benefit score (0 = all)",
    )
    parser.add_argument(
        "--exclude-test",
        action="store_true",
        help="skip Lib/test/ when scanning directories",
    )
    args = parser.parse_args(argv)

    if args.kind == "both":
        kinds = {"explicit", "implicit"}
    elif args.kind == "all":
        kinds = {"explicit", "implicit", "literal"}
    else:
        kinds = {args.kind}
    root = args.paths[0].resolve() if len(args.paths) == 1 else Path.cwd()

    all_hits: list[Hit] = []
    for path in iter_py_files(args.paths):
        if args.exclude_test and "Lib/test" in str(path):
            continue
        for hit in scan_file(path, kinds=kinds, min_lines=args.min_lines):
            if (
                hit.kind in {"explicit", "implicit"}
                and hit.parts < args.min_parts
            ):
                continue
            all_hits.append(hit)

    all_hits.sort(key=lambda h: (-h.score, str(h.path), h.lineno, h.col_offset))
    if args.top:
        all_hits = all_hits[: args.top]

    for hit in all_hits:
        print(hit.format(root))
        print()

    total = len(all_hits)

    if total == 0:
        print("No matches.", file=sys.stderr)
    else:
        print(f"{total} hit(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
