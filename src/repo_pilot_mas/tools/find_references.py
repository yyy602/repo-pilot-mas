"""Python AST-based symbol reference discovery."""

from __future__ import annotations

import ast
from pathlib import Path

from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext, iter_files, safe_read_text


class _ReferenceVisitor(ast.NodeVisitor):
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.references: list[tuple[int, int, str]] = []

    def _add(self, node: ast.AST, kind: str) -> None:
        self.references.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), kind))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == self.symbol:
            self._add(node, "function_definition")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name == self.symbol:
            self._add(node, "async_function_definition")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == self.symbol:
            self._add(node, "class_definition")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == self.symbol:
            kind = "write" if isinstance(node.ctx, (ast.Store, ast.Del)) else "reference"
            self._add(node, kind)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.symbol:
            self._add(node, "attribute_reference")
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        visible_name = node.asname or node.name.rsplit(".", 1)[-1]
        if visible_name == self.symbol:
            self._add(node, "import")
        self.generic_visit(node)


def find_references(
    root: str | Path,
    symbol: str,
    *,
    path: str | Path = ".",
    include_definitions: bool = True,
    max_results: int = 200,
    max_file_bytes: int = 1_000_000,
) -> ToolResult:
    """Find exact Python identifier references and classify their AST context."""

    context = ToolContext.start("find_references")
    try:
        if not symbol.isidentifier():
            raise ValueError("symbol must be a valid Python identifier")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        guard = PathGuard(root)
        search_root = guard.resolve(path)
        if not search_root.is_dir():
            raise ValueError("path must point to a directory")

        results: list[dict[str, object]] = []
        parse_errors: list[dict[str, object]] = []
        scanned_files = 0
        truncated = False
        definition_kinds = {
            "function_definition",
            "async_function_definition",
            "class_definition",
        }

        for file_path in iter_files(search_root, patterns=("*.py",)):
            scanned_files += 1
            try:
                text = safe_read_text(file_path, max_file_bytes=max_file_bytes)
                tree = ast.parse(text, filename=str(file_path))
            except SyntaxError as exc:
                parse_errors.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "line": exc.lineno,
                        "message": exc.msg,
                    }
                )
                continue
            except (OSError, ValueError) as exc:
                parse_errors.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "line": None,
                        "message": str(exc),
                    }
                )
                continue

            visitor = _ReferenceVisitor(symbol)
            visitor.visit(tree)
            lines = text.splitlines()
            seen: set[tuple[int, int, str]] = set()
            for line_number, column, kind in sorted(visitor.references):
                key = (line_number, column, kind)
                if key in seen:
                    continue
                seen.add(key)
                if not include_definitions and kind in definition_kinds:
                    continue
                source_line = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
                results.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "line_number": line_number,
                        "column": column + 1,
                        "kind": kind,
                        "line": source_line[:500] + ("..." if len(source_line) > 500 else ""),
                    }
                )
                if len(results) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        return context.success(
            data={
                "symbol": symbol,
                "results": results,
                "count": len(results),
                "scanned_files": scanned_files,
                "parse_errors": parse_errors,
            },
            truncated=truncated,
        )
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="FIND_REFERENCES_ERROR", message=str(exc))
