"""Smoke test for conftest.py's fixture generation — not a spec test,
just confirms the fixtures themselves produce what they claim to before
the real spec-tracing test files rely on them.
"""
from __future__ import annotations

import analysis


def test_clean_wav_has_no_issues(clean_wav):
    result = analysis.analyze_file_health(clean_wav)
    assert result.file_issues == []


def test_corrupt_mp3_triggers_corruption_signature(corrupt_mp3_bit_reservoir):
    result = analysis.analyze_file_health(corrupt_mp3_bit_reservoir)
    assert any("corruption" in issue.lower() for issue in result.file_issues)


def test_truncated_mp3_triggers_size_mismatch(truncated_mp3):
    result = analysis.analyze_file_health(truncated_mp3)
    assert result.file_tier == analysis.FILE_TIER_BAD


def test_undecodable_file_is_unplayable(undecodable_file):
    result = analysis.analyze_file_health(undecodable_file)
    assert result.file_tier == analysis.FILE_TIER_BROKEN


def test_phase_inverted_wav_is_detected(phase_inverted_wav):
    result = analysis.analyze_track_health(phase_inverted_wav)
    assert result.is_out_of_phase is True


def test_clipped_wav_is_detected(clipped_wav):
    result = analysis.analyze_track_health(clipped_wav)
    assert result.clipping_flat_factor > analysis.MIN_FLAT_FACTOR_FOR_CLIPPING


def test_malformed_tag_mp3_triggers_tag_error(malformed_tag_mp3):
    result = analysis.analyze_file_health(malformed_tag_mp3)
    assert result.file_tier == analysis.FILE_TIER_GOOD
    assert any("tag" in issue.lower() for issue in result.file_issues)


def test_corrupt_artwork_mp3_triggers_artwork_error(corrupt_artwork_mp3):
    result = analysis.analyze_file_health(corrupt_artwork_mp3)
    assert result.file_tier == analysis.FILE_TIER_GOOD
    assert any("artwork" in issue.lower() for issue in result.file_issues)
