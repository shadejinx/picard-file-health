"""Tests for docs/intent/file-health-scoring/file-health-scoring-specs.md."""
from __future__ import annotations

from pathlib import Path

import analysis


# --- Tier Assignment ---

def test_undecodable_file_is_unplayable(undecodable_file):
    """@spec FH-TIER-001"""
    result = analysis.analyze_file_health(undecodable_file)
    assert result.file_tier == analysis.FILE_TIER_BROKEN


def test_structural_issue_forces_bad(corrupt_mp3_bit_reservoir):
    """@spec FH-TIER-002"""
    result = analysis.analyze_file_health(corrupt_mp3_bit_reservoir)
    assert result.file_tier == analysis.FILE_TIER_BAD


def test_structural_issue_forces_bad_even_with_a_tag_issue_present(corrupt_mp3_bit_reservoir):
    """@spec FH-TIER-002

    A file with both a structural and a tag/artwork issue must resolve
    to Bad — the structural check wins regardless of what else is wrong.
    """
    data = bytearray(Path(corrupt_mp3_bit_reservoir).read_bytes())
    assert data[:3] == b"ID3"
    data[3] = 0xFF  # also malform the tag version, on top of the existing corruption
    combined = Path(corrupt_mp3_bit_reservoir).with_name("combined.mp3")
    combined.write_bytes(bytes(data))
    result = analysis.analyze_file_health(str(combined))
    assert result.file_tier == analysis.FILE_TIER_BAD


def test_tag_issue_alone_caps_at_ok_not_bad(malformed_tag_mp3):
    """@spec FH-TIER-003"""
    result = analysis.analyze_file_health(malformed_tag_mp3)
    assert result.file_tier == analysis.FILE_TIER_GOOD  # FILE_TIER_GOOD's value is "OK"


def test_artwork_issue_alone_caps_at_ok_not_bad(corrupt_artwork_mp3):
    """@spec FH-TIER-003"""
    result = analysis.analyze_file_health(corrupt_artwork_mp3)
    assert result.file_tier == analysis.FILE_TIER_GOOD


def test_clean_low_bitrate_lossy_file_is_ok_not_excellent(low_bitrate_mp3):
    """@spec FH-TIER-004, FH-TIER-007"""
    result = analysis.analyze_file_health(low_bitrate_mp3)
    assert result.file_issues == []
    assert result.file_tier == analysis.FILE_TIER_GOOD


def test_clean_lossless_file_is_excellent(flac_clean):
    """@spec FH-TIER-005"""
    result = analysis.analyze_file_health(flac_clean)
    assert result.file_issues == []
    assert result.file_tier == analysis.FILE_TIER_EXCELLENT


def test_clean_high_bitrate_lossy_file_is_good(high_bitrate_mp3):
    """@spec FH-TIER-006"""
    result = analysis.analyze_file_health(high_bitrate_mp3)
    assert result.file_issues == []
    assert result.file_tier == analysis.FILE_TIER_GREAT  # FILE_TIER_GREAT's value is "Good"


# --- Issue Categorization ---

def test_both_issue_types_appear_in_the_flat_issue_list(corrupt_mp3_bit_reservoir):
    """@spec FH-ISSUE-001, FH-ISSUE-002"""
    data = bytearray(Path(corrupt_mp3_bit_reservoir).read_bytes())
    data[3] = 0xFF
    combined = Path(corrupt_mp3_bit_reservoir).with_name("combined2.mp3")
    combined.write_bytes(bytes(data))
    result = analysis.analyze_file_health(str(combined))
    assert any("corruption" in issue.lower() for issue in result.file_issues)
    assert any("tag" in issue.lower() for issue in result.file_issues)


# --- Unplayable Terminal State ---

def test_unplayable_reports_a_specific_reason(undecodable_file):
    """@spec FH-BROKEN-001"""
    result = analysis.analyze_file_health(undecodable_file)
    assert len(result.file_issues) == 1
    assert "decode" in result.file_issues[0].lower()


def test_unplayable_still_computes_content_hash(undecodable_file):
    """@spec FH-BROKEN-002"""
    result = analysis.analyze_file_health(undecodable_file)
    assert result.content_hash
    assert result.content_hash == analysis.content_hash(undecodable_file)
