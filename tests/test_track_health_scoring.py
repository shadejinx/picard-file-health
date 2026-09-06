"""Tests for docs/intent/track-health-scoring/track-health-scoring-specs.md."""
from __future__ import annotations

import pytest

import analysis
from analysis import Thresholds, TrackHealthInputs, compute_track_score


# --- Weighted Composite Scoring ---

def test_score_is_a_weighted_average_not_a_single_check_veto():
    """@spec TH-SCORE-001

    A single severely-defective check (out-of-phase) combined with one
    clean check must land strictly between 0 and 1 — proving the result
    is an average, not one check forcing the whole score to its own
    extreme the way the old OR-gate tier architecture did.
    """
    inputs = TrackHealthInputs(
        flat_factor=None, true_peak_dbtp=None, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=True,
        dr14=None, has_mains_hum=False, noise_floor_db=None,
    )
    score = compute_track_score(inputs, Thresholds())
    assert score is not None
    assert 0.0 < score < 1.0


def test_unmeasurable_check_is_excluded_not_treated_as_clean():
    """@spec TH-SCORE-002

    Omitting a check (None) must change the composite from what it
    would be if that same check were present and measured clean (0.0) —
    otherwise "not measured" and "measured, clean" are indistinguishable,
    silently pulling the score toward "better than it is".
    """
    thresholds = Thresholds()
    with_hum_measured_clean = TrackHealthInputs(
        flat_factor=None, true_peak_dbtp=None, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=True,
        dr14=None, has_mains_hum=False, noise_floor_db=None,
    )
    hum_not_measured = TrackHealthInputs(
        flat_factor=None, true_peak_dbtp=None, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=True,
        dr14=None, has_mains_hum=None, noise_floor_db=None,
    )
    score_measured_clean = compute_track_score(with_hum_measured_clean, thresholds)
    score_not_measured = compute_track_score(hum_not_measured, thresholds)
    assert score_measured_clean != score_not_measured


def test_full_weight_checks_are_weighted_at_the_maximum():
    """@spec TH-SCORE-003"""
    full_weight_checks = ('clipping', 'spectral_cutoff', 'out_of_phase', 'dr14')
    max_weight = max(analysis.TRACK_HEALTH_WEIGHTS.values())
    for check in full_weight_checks:
        assert analysis.TRACK_HEALTH_WEIGHTS[check] == max_weight


def test_uncertain_checks_are_weighted_below_full():
    """@spec TH-SCORE-004"""
    max_weight = max(analysis.TRACK_HEALTH_WEIGHTS.values())
    for check in ('true_peak', 'fake_hires', 'mains_hum', 'noise_floor'):
        assert analysis.TRACK_HEALTH_WEIGHTS[check] < max_weight


def test_weight_difference_is_observable_in_the_composite_score():
    """@spec TH-SCORE-003, TH-SCORE-004

    Behavioral proof, not just a constants check: the same binary defect
    firing on a full-weight check must pull the composite further than
    it would on a reduced-weight check, when paired against the same
    clean counterpart check on the other side.
    """
    thresholds = Thresholds()
    full_weight_defect = TrackHealthInputs(
        flat_factor=None, true_peak_dbtp=None, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=True,
        dr14=None, has_mains_hum=False, noise_floor_db=None,
    )
    reduced_weight_defect = TrackHealthInputs(
        flat_factor=None, true_peak_dbtp=None, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=False,
        dr14=None, has_mains_hum=True, noise_floor_db=None,
    )
    score_full = compute_track_score(full_weight_defect, thresholds)
    score_reduced = compute_track_score(reduced_weight_defect, thresholds)
    assert score_full > score_reduced


def test_clipping_and_true_peak_both_score_even_when_only_one_is_reported(clipped_wav):
    """@spec TH-SCORE-005

    A file that both clips and has true-peak overs must have BOTH
    checks' weighted contributions in the composite score, even though
    analyze_track_health's itemized text only names clipping (true-peak
    text is suppressed as redundant once clipping already fired).
    """
    thresholds = Thresholds()
    both_defective = TrackHealthInputs(
        flat_factor=30.0, true_peak_dbtp=2.5, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=None,
        dr14=None, has_mains_hum=None, noise_floor_db=None,
    )
    clip_only = TrackHealthInputs(
        flat_factor=30.0, true_peak_dbtp=None, has_signal=False,
        spectral_energy_above_cutoff_db=None, is_hires=False,
        spectral_energy_above_hires_cutoff_db=None, is_out_of_phase=None,
        dr14=None, has_mains_hum=None, noise_floor_db=None,
    )
    assert compute_track_score(both_defective, thresholds) != compute_track_score(clip_only, thresholds)

    result = analysis.analyze_track_health(clipped_wav)
    assert any("clip" in issue.lower() for issue in result.track_issues)
    assert not any("peak" in issue.lower() for issue in result.track_issues)


# --- Issue Reporting ---

def test_score_is_reported_even_when_there_are_no_issues(clean_music_wav):
    """@spec TH-ISSUE-001"""
    result = analysis.analyze_track_health(clean_music_wav)
    assert result.track_issues == []
    assert result.track_score is not None

def test_fired_checks_are_reported_as_itemized_issues_alongside_the_score(phase_inverted_wav):
    """@spec TH-ISSUE-001"""
    result = analysis.analyze_track_health(phase_inverted_wav)
    assert any("cancel" in issue.lower() for issue in result.track_issues)
    assert result.track_score is not None


# --- Tier Mapping ---

def test_tier_boundaries_are_fixed_and_percentile_calibrated():
    """@spec TH-TIER-001"""
    assert analysis.track_tier_from_score(0.35) == "Bad"
    assert analysis.track_tier_from_score(0.349) == "OK"
    assert analysis.track_tier_from_score(0.25) == "OK"
    assert analysis.track_tier_from_score(0.249) == "Good"
    assert analysis.track_tier_from_score(0.15) == "Good"
    assert analysis.track_tier_from_score(0.149) == "Excellent"


def test_decode_failure_assigns_the_unplayable_tier(undecodable_file):
    """@spec TH-TIER-002"""
    result = analysis.analyze_track_health(undecodable_file)
    assert result.track_tier == analysis.FILE_TIER_BROKEN


# --- Sensitivity Thresholds ---

def test_analysis_module_owns_the_slider_hint_text_and_position_lookup():
    """@spec TH-SLIDER-001"""
    for check in analysis.SENSITIVITY_CHECKS:
        default_position = analysis.sensitivity_default_position(check)
        value, hint = analysis.sensitivity_step(check, default_position)
        assert isinstance(value, float)
        assert isinstance(hint, str) and hint
        # Position 1 (most lenient) and 10 (most strict) both resolve to
        # a real, distinct calibrated value — not a placeholder range.
        lenient_value, _ = analysis.sensitivity_step(check, 1)
        strict_value, _ = analysis.sensitivity_step(check, 10)
        assert lenient_value != strict_value


def test_sensitivity_step_rejects_an_out_of_range_position():
    """@spec TH-SLIDER-001"""
    with pytest.raises(ValueError):
        analysis.sensitivity_step('clip', 0)
    with pytest.raises(ValueError):
        analysis.sensitivity_step('clip', 11)


def test_dr14_shift_slider_has_ten_positions_with_a_new_lenient_step():
    """@spec TH-SLIDER-001"""
    assert len(analysis.DR14_SHIFT_STEPS) == 10
    most_lenient, _ = analysis.sensitivity_step('dr14_shift', 1)
    assert most_lenient == 5.0
    default_value, _ = analysis.sensitivity_step('dr14_shift', analysis.sensitivity_default_position('dr14_shift'))
    assert default_value == 0.0


def test_thresholds_are_applied_independently_per_call_not_via_shared_state(clipped_wav):
    """@spec TH-SLIDER-002

    A strict Thresholds instance passed to one call must not leak into
    a later call that passes the default Thresholds — proving each scan
    gets its own configured sensitivity rather than mutating module-level
    state that concurrent scans could race on.
    """
    lenient = Thresholds(clip_flat_factor=90.0)
    strict_result = analysis.analyze_track_health(clipped_wav)
    lenient_result = analysis.analyze_track_health(clipped_wav, thresholds=lenient)
    default_result_again = analysis.analyze_track_health(clipped_wav)

    assert any("clip" in issue.lower() for issue in strict_result.track_issues)
    assert not any("clip" in issue.lower() for issue in lenient_result.track_issues)
    assert any("clip" in issue.lower() for issue in default_result_again.track_issues)
    assert analysis.CLIP_STEPS == (25.0, 20.0, 16.0, 8.0, 4.0, 2.0, 1.0, 0.5, 0.1, 0.05)


# --- Merged-Analysis Branch Selection ---

def test_phase_branch_is_skipped_for_mono_source(mono_wav):
    """@spec TH-BRANCH-001"""
    result = analysis.analyze_track_health(mono_wav)
    assert result.is_out_of_phase is None
    assert result.stereo_coherence is None


def test_phase_branch_runs_for_stereo_source(phase_inverted_wav):
    """@spec TH-BRANCH-001"""
    result = analysis.analyze_track_health(phase_inverted_wav)
    assert result.is_out_of_phase is not None


def test_hires_branch_is_skipped_below_the_sample_rate_threshold(clean_wav):
    """@spec TH-BRANCH-001"""
    result = analysis.analyze_track_health(clean_wav)
    assert result.spectral_energy_above_hires_cutoff_db is None


def test_hires_branch_runs_above_the_sample_rate_threshold(hires_wav):
    """@spec TH-BRANCH-001"""
    result = analysis.analyze_track_health(hires_wav)
    assert result.spectral_energy_above_hires_cutoff_db is not None
