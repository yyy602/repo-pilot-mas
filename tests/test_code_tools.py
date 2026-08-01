from pathlib import Path

from repo_pilot_mas.tools import find_references, inspect_code, list_files, search_code


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
