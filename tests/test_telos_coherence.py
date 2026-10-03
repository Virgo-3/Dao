"""Coherence is a scoped descriptive statistic, never an action objective."""

import pytest

from dao.relational import coherence_diagnostic


def relation(identifier, belief, basis="observation"):
    return {
        "id": identifier,
        "subject": "user",
        "object": identifier,
        "dimension": "experience",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "evidence_ids": [],
        "basis": basis,
        "belief": belief,
    }


def test_coherence_reports_scope_and_excludes_unassessed():
    positive = relation("positive", {"enhancing": 1, "neutral": 0, "degrading": 0})
    negative = relation("negative", {"enhancing": 0, "neutral": 0, "degrading": 1})
    unknown = relation(
        "unknown", {"enhancing": 1 / 3, "neutral": 1 / 3, "degrading": 1 / 3},
        "unassessed",
    )
    report = coherence_diagnostic([positive, negative, unknown])
    assert report["expected_count_ratio"] == 0.5
    assert report["expected_active_count"] == 2
    assert report["unassessed_ids"] == ["unknown"]
    assert report["scope_ids"] == ["negative", "positive", "unknown"]
    assert coherence_diagnostic([positive])["expected_count_ratio"] == 1


def test_all_neutral_coherence_is_unavailable():
    report = coherence_diagnostic([
        relation("neutral", {"enhancing": 0, "neutral": 1, "degrading": 0})
    ])
    assert report["expected_count_ratio"] is None
    assert report["expected_active_count"] == 0


def test_coherence_rejects_invalid_or_duplicate_beliefs():
    item = relation("one", {"enhancing": 1, "neutral": 0, "degrading": 0})
    with pytest.raises(ValueError):
        coherence_diagnostic([item, item])
    with pytest.raises(ValueError):
        coherence_diagnostic([
            relation("bad", {"enhancing": 0.8, "neutral": 0.8, "degrading": 0})
        ])
