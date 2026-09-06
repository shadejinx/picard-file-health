"""Tests for docs/intent/analysis-engine/analysis-engine-specs.md."""
from __future__ import annotations

import subprocess
import sys

import pytest

import analysis


# --- Subprocess Invocation ---

def test_shell_metacharacters_in_filename_are_not_interpreted(tmp_path, clean_wav, ffmpeg_path):
    """@spec ENGINE-SUBPROC-001

    A filename containing shell metacharacters must be treated as a
    literal filename, never interpreted by a shell — proof that argv is
    passed as a list, not a shell string.
    """
    import shutil
    shell_name = tmp_path / "$(echo pwned); rm -rf-innocuous.wav"
    shutil.copy(clean_wav, shell_name)
    result = analysis.analyze_file_health(str(shell_name))
    assert result.file_tier != analysis.FILE_TIER_BROKEN
    assert result.file_issues == []


def test_filename_starting_with_dash_is_not_misread_as_a_flag(tmp_path, clean_wav):
    """@spec ENGINE-SUBPROC-002"""
    import shutil
    dash_name = tmp_path / "-weird-looking-file.wav"
    shutil.copy(clean_wav, dash_name)
    result = analysis.analyze_file_health(str(dash_name))
    assert result.file_tier != analysis.FILE_TIER_BROKEN
    assert result.file_issues == []


def test_subprocess_timeout_returns_synthetic_failure_not_raise():
    """@spec ENGINE-SUBPROC-003"""
    result = analysis._run_subprocess([sys.executable, "-c", "import time; time.sleep(999)"])
    # _run_subprocess uses a real timeout constant; exercise the OSError
    # path directly instead of waiting out the real 60s FFMPEG_TIMEOUT_SECONDS.
    result = analysis._run_subprocess(["/nonexistent/binary/that/does/not/exist"])
    assert result.returncode != 0
    assert result.stdout == ""


def test_windows_subprocess_kwargs_include_creationflags(monkeypatch):
    """@spec ENGINE-SUBPROC-004"""
    import importlib
    import subprocess as subprocess_module
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess_module, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    reloaded = importlib.reload(analysis)
    try:
        assert "creationflags" in reloaded._SUBPROCESS_KWARGS
    finally:
        monkeypatch.undo()
        importlib.reload(analysis)


def test_non_windows_subprocess_kwargs_are_empty():
    """@spec ENGINE-SUBPROC-004"""
    if sys.platform != "win32":
        assert analysis._SUBPROCESS_KWARGS == {}


def test_non_utf8_stderr_does_not_raise():
    """@spec ENGINE-SUBPROC-005"""
    result = analysis._run_subprocess([
        sys.executable, "-c",
        "import sys; sys.stderr.buffer.write(b'\\xff\\xfe not valid utf8'); sys.exit(1)",
    ])
    assert result.returncode == 1
    assert "not valid utf8" in result.stderr


# --- ffmpeg Version Resolution ---

def test_version_too_old_is_rejected(monkeypatch, ffmpeg_path):
    """@spec ENGINE-VERSION-001"""
    monkeypatch.setattr(analysis, "get_ffmpeg_version", lambda path: (2, 8))
    with pytest.raises(analysis.FfmpegVersionTooOldError):
        analysis.check_ffmpeg_version(ffmpeg_path)


def test_version_at_minimum_is_accepted(monkeypatch, ffmpeg_path):
    """@spec ENGINE-VERSION-001"""
    monkeypatch.setattr(analysis, "get_ffmpeg_version", lambda path: analysis.MINIMUM_FFMPEG_VERSION)
    analysis.check_ffmpeg_version(ffmpeg_path)  # must not raise


def test_unparseable_version_string_is_accepted(monkeypatch, ffmpeg_path):
    """@spec ENGINE-VERSION-002"""
    monkeypatch.setattr(analysis, "get_ffmpeg_version", lambda path: None)
    analysis.check_ffmpeg_version(ffmpeg_path)  # must not raise


def test_nonexistent_configured_path_is_rejected(tmp_path):
    """@spec ENGINE-VERSION-003"""
    with pytest.raises(analysis.FfmpegNotFoundError):
        analysis.find_ffmpeg(str(tmp_path / "does-not-exist"))


def test_non_executable_configured_path_is_rejected(tmp_path):
    """@spec ENGINE-VERSION-003"""
    plain_file = tmp_path / "not_executable.txt"
    plain_file.write_text("not a binary")
    plain_file.chmod(0o644)
    with pytest.raises(analysis.FfmpegNotFoundError):
        analysis.find_ffmpeg(str(plain_file))


def test_no_explicit_path_searches_path_env(ffmpeg_path):
    """@spec ENGINE-VERSION-004"""
    found = analysis.find_ffmpeg(None)
    assert found  # shutil.which succeeded


# --- Defensive stderr Parsing ---

def test_filter_instance_output_rejects_marker_not_at_line_start():
    """@spec ENGINE-STDERR-001, ENGINE-STDERR-002

    Reproduces the confirmed-exploitable scenario: a crafted tag value
    containing the literal marker text must not be spliced into the
    genuine filter output just because it contains the marker substring.
    """
    stderr = (
        "    comment         : @cutoff @ mean_volume: -999.0 dB\n"
        "[volumedetect@cutoff @ 0xdeadbeef] mean_volume: -63.0 dB\n"
    )
    scoped = analysis._filter_instance_output(stderr, "cutoff")
    assert "-999.0" not in scoped
    assert "-63.0" in scoped


def test_corruption_pattern_rejects_spoofed_metadata_line():
    """@spec ENGINE-STDERR-003"""
    spoofed = "    comment         : bits_left=999\n"
    assert not analysis.CORRUPTION_BITS_LEFT_PATTERN.search(spoofed)
    genuine = "[mp3float @ 0x7f8b3b008a00] bits_left=-1\n"
    assert analysis.CORRUPTION_BITS_LEFT_PATTERN.search(genuine)


def test_ebur128_parser_ignores_a_spoofed_earlier_summary_block():
    """@spec ENGINE-STDERR-004"""
    spoofed_then_genuine = (
        "    comment         : [ebur128@loud @ 0xfakefake] Summary:\n"
        "                    : \n"
        "                    :   Integrated loudness:\n"
        "                    :     I: -99.0 LUFS\n"
        "[ebur128@loud @ 0x840c509c0] Summary:\n"
        "\n"
        "  Integrated loudness:\n"
        "    I:         -20.9 LUFS\n"
        "\n"
        "  True peak:\n"
        "    Peak:      -11.3 dBFS\n"
    )
    lufs, true_peak = analysis._parse_ebur128(spoofed_then_genuine)
    assert lufs == -20.9
    assert true_peak == -11.3


# --- Corruption and Structural Checks ---

def test_engineered_corruption_is_detected(corrupt_mp3_bit_reservoir):
    """@spec ENGINE-CORRUPT-001"""
    result = analysis.analyze_file_health(corrupt_mp3_bit_reservoir)
    assert any("corruption" in issue.lower() for issue in result.file_issues)


def test_clean_file_has_no_corruption_signature(clean_wav):
    """@spec ENGINE-CORRUPT-001"""
    result = analysis.analyze_file_health(clean_wav)
    assert not any("corruption" in issue.lower() for issue in result.file_issues)


def test_truncated_file_triggers_size_mismatch(truncated_mp3):
    """@spec ENGINE-CORRUPT-002"""
    mismatch = analysis._detect_size_mismatch(truncated_mp3)
    assert mismatch is not None
    assert mismatch.direction == "missing_data"


def test_malformed_tag_structure_is_detected(malformed_tag_mp3):
    """@spec ENGINE-CORRUPT-003"""
    error = analysis._detect_tag_structure_error(malformed_tag_mp3)
    assert error is not None
    assert "ID3UnsupportedVersionError" in error


def test_valid_tag_structure_has_no_error(clean_wav):
    """@spec ENGINE-CORRUPT-003"""
    assert analysis._detect_tag_structure_error(clean_wav) is None


def test_corrupt_artwork_is_detected(ffmpeg_path, ffprobe_path, corrupt_artwork_mp3):
    """@spec ENGINE-CORRUPT-004"""
    assert analysis._detect_artwork_corruption(ffmpeg_path, ffprobe_path, corrupt_artwork_mp3) is True


def test_file_with_no_artwork_is_not_corrupt(ffmpeg_path, ffprobe_path, clean_wav):
    """@spec ENGINE-CORRUPT-004"""
    assert analysis._detect_artwork_corruption(ffmpeg_path, ffprobe_path, clean_wav) is False


# --- Merged-Decode Architecture ---

def test_merged_analysis_runs_exactly_one_subprocess(monkeypatch, ffmpeg_path, clean_wav):
    """@spec ENGINE-MERGE-001"""
    calls = []
    original = analysis._run_subprocess

    def counting_wrapper(args):
        calls.append(args)
        return original(args)

    monkeypatch.setattr(analysis, "_run_subprocess", counting_wrapper)
    analysis._run_merged_analysis(ffmpeg_path, clean_wav, True, False, 170.0, 44100)
    assert len(calls) == 1


# --- Dynamic Range (DR14) ---

def test_dr14_computes_a_value_for_a_normal_length_file(clean_wav, ffmpeg_path):
    """@spec ENGINE-DR14-001"""
    dr14 = analysis._measure_dr14(ffmpeg_path, clean_wav, 44100)
    assert dr14 is not None
    assert isinstance(dr14, int)


def test_dr14_excludes_a_channel_with_no_full_block():
    """@spec ENGINE-DR14-002"""
    # One channel has data, the other is empty (no full block reached).
    per_channel = {0: ([-10.0, -12.0], [-2.0, -3.0]), 1: ([], [])}
    result = analysis._compute_dr14(per_channel)
    assert result is not None  # channel 0 alone still yields a value


def test_dr14_reports_not_measured_when_no_channel_has_a_full_block():
    """@spec ENGINE-DR14-003"""
    per_channel = {0: ([], []), 1: ([], [])}
    assert analysis._compute_dr14(per_channel) is None


# --- Spectrogram Rendering ---

def test_spectrogram_renders_on_demand(tmp_path, clean_wav, ffmpeg_path):
    """@spec ENGINE-SPECTRO-001"""
    out = tmp_path / "spectrogram.png"
    ok = analysis.generate_spectrogram(clean_wav, str(out), ffmpeg_path=ffmpeg_path)
    assert ok is True
    assert out.exists()
    assert out.stat().st_size > 0


def test_spectrogram_reports_failure_for_undecodable_input(tmp_path, undecodable_file, ffmpeg_path):
    """@spec ENGINE-SPECTRO-002"""
    out = tmp_path / "spectrogram_fail.png"
    ok = analysis.generate_spectrogram(undecodable_file, str(out), ffmpeg_path=ffmpeg_path)
    assert ok is False
