import json
from dataclasses import replace
from pathlib import Path

from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.tools import (
    apply_patch,
    collect_diff,
    find_references,
    inspect_code,
    list_files,
    rollback_workspace,
    run_tests,
    search_code,
    static_check,
)


def test_list_files_is_deterministic_and_hides_git(sample_repo: Path) -> None:
    result = list_files(sample_repo, max_depth=3)

    paths = [entry["path"] for entry in result.data["entries"]]
    assert result.ok
    assert "src/math_utils.py" in paths
    assert all(not path.startswith(".git") for path in paths)


def test_list_files_rejects_traversal(sample_repo: Path) -> None:
    result = list_files(sample_repo, path="../")

    assert not result.ok
    assert result.error is not None


def test_search_code_finds_text_and_skips_binary(sample_repo: Path) -> None:
    result = search_code(sample_repo, "return add", patterns=("*.py",))

    assert result.ok
    assert result.data["count"] == 1
    assert result.data["results"][0]["path"] == "src/math_utils.py"


def test_search_code_reports_invalid_regex(sample_repo: Path) -> None:
    result = search_code(sample_repo, "[", regex=True)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "INVALID_REGEX"


def test_inspect_code_by_symbol(sample_repo: Path) -> None:
    result = inspect_code(sample_repo, "src/math_utils.py", symbol="Calculator", context_lines=0)

    assert result.ok
    assert result.data["symbol"]["kind"] == "ClassDef"
    assert "class Calculator" in result.data["text"]


def test_inspect_code_limits_line_count(sample_repo: Path) -> None:
    result = inspect_code(
        sample_repo,
        "src/math_utils.py",
        start_line=1,
        end_line=50,
        max_lines=2,
    )

    assert result.ok
    assert result.truncated
    assert len(result.data["lines"]) == 2


def test_find_references_classifies_definition_and_calls(sample_repo: Path) -> None:
    result = find_references(sample_repo, "add")

    assert result.ok
    kinds = {item["kind"] for item in result.data["results"]}
    paths = {item["path"] for item in result.data["results"]}
    assert "function_definition" in kinds
    assert "reference" in kinds
    assert "tests/test_math.py" in paths


def test_all_nine_tools_return_traceable_structured_errors(
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(sample_repo, tmp_path / "workspaces").create("trace-errors")
    invalid_workspace = replace(workspace, root=tmp_path / "outside")
    results = [
        list_files(sample_repo, path="../"),
        search_code(sample_repo, "[", regex=True),
        inspect_code(sample_repo, "/etc/passwd"),
        find_references(sample_repo, "not a symbol"),
        run_tests(sample_repo, command=("git", "status")),
        apply_patch(workspace.root, ""),
        collect_diff(workspace, max_diff_chars=1),
        rollback_workspace(invalid_workspace),
        static_check(sample_repo, path="../", run_ruff=False),
    ]

    assert {result.tool for result in results} == {
        "list_files",
        "search_code",
        "inspect_code",
        "find_references",
        "run_tests",
        "apply_patch",
        "collect_diff",
        "rollback_workspace",
        "static_check",
    }
    for result in results:
        assert not result.ok
        assert result.trace_id
        assert result.error is not None
        assert result.error.code
        json.dumps(result.to_dict())
