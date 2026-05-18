"""Golden-set loader: schema validation + happy path.

The loader is the only thing that runs at golden-set authorship time,
so its error messages need to be good. These tests pin every guard
against future drift."""

from __future__ import annotations

from pathlib import Path

import pytest

from pronaos.eval.data import EvalCase, load_golden_set

# Path to the bundled example golden set.
BASIC = Path(__file__).resolve().parents[2] / "eval" / "data" / "basic.yaml"


# --------------------------------------------------------------------------- #
# Happy path: the bundled set loads                                           #
# --------------------------------------------------------------------------- #


def test_basic_golden_set_loads() -> None:
    """The example set under tests/eval/data/ must parse cleanly. If
    it doesn't, downstream demos and CI guides both break."""
    gs = load_golden_set(BASIC)
    assert gs.name == "basic"
    assert len(gs) >= 5, f"basic set unexpectedly small: {len(gs)}"
    # Every case has the required fields.
    for case in gs.cases:
        assert isinstance(case, EvalCase)
        assert case.id
        assert case.prompt.strip()
        assert case.expected.strip()


def test_categories_cover_intended_axes() -> None:
    """Sanity guard so future edits to basic.yaml don't drop coverage
    on the axes the eval is designed to test."""
    gs = load_golden_set(BASIC)
    cats = {c.category for c in gs.cases}
    expected_axes = {"factual", "cs_factual", "reasoning", "safety"}
    missing = expected_axes - cats
    assert not missing, f"basic set missing categories: {missing}"


# --------------------------------------------------------------------------- #
# Schema validation                                                            #
# --------------------------------------------------------------------------- #


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "set.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_name_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "cases:\n  - id: a\n    prompt: hi\n    expected: hi\n")
    with pytest.raises(ValueError, match="name"):
        load_golden_set(p)


def test_empty_cases_rejected(tmp_path: Path) -> None:
    """A golden set with zero cases would silently produce a 0/0 score
    that looks like a clean run. Catch it at load time."""
    p = _write_yaml(tmp_path, "name: empty\ncases: []\n")
    with pytest.raises(ValueError, match="cases"):
        load_golden_set(p)


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    """Duplicate ids would silently collapse rows in run output.
    Reject at load time."""
    body = """
name: dup
cases:
  - id: x
    prompt: a
    expected: a
  - id: x
    prompt: b
    expected: b
"""
    p = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="duplicate"):
        load_golden_set(p)


def test_missing_expected_rejected(tmp_path: Path) -> None:
    """A case without a rubric is meaningless to score against."""
    body = """
name: missing
cases:
  - id: x
    prompt: a
"""
    p = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="expected"):
        load_golden_set(p)


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """Specifically FileNotFoundError, not generic ValueError — lets
    the CLI distinguish 'path typo' from 'malformed file'."""
    with pytest.raises(FileNotFoundError):
        load_golden_set(tmp_path / "doesnotexist.yaml")
