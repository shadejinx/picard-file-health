"""Real audio-signal analysis, via ffmpeg.

Every threshold/formula here was empirically validated against real ffmpeg
output before being written (see the plugin's development history) — not
guessed from documentation. Two genuinely reliable gate checks are
implemented: clipping and spectral-cutoff (transcode) detection. A third,
LUFS-based loudness gradient, is an honest coarse proxy for "how squashed
the master might be" referencing real industry loudness norms (streaming
platforms target ~-14 LUFS integrated, EBU R128 broadcast targets ~-23
LUFS, masters pushed louder than ~-8 LUFS integrated are the classic
"loudness war" sound) — it is NOT a true DR14 dynamic-range measurement,
which needs block-based peak-vs-RMS analysis. That, plus real bitrate-vs-
codec-transparency scoring, sample-rate scoring, hum detection, and phase
correlation, are documented future work, not implemented here yet.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import subprocess


# Below this level (relative to full scale), content above the cutoff
# frequency is considered "not really there" — empirically, a genuinely
# lowpassed test signal measured -91dB here, a clean wideband signal
# measured -12.7dB. -60dB sits well clear of both.
SPECTRAL_SILENCE_THRESHOLD_DB = -60.0
SPECTRAL_CUTOFF_FREQUENCY_HZ = 17000

# Below this overall peak level, a file has essentially no real audio
# content anywhere — the spectral-cutoff check becomes meaningless (a
# near-silent file trivially has "no content above 17kHz" for the same
# reason it has no content anywhere, not because of a lossy encoder's
# lowpass filter). Empirically found: a ~0.13s near-silent test fixture
# peaked at -74dB overall and falsely tripped the cutoff gate at -91dB
# above 17kHz before this guard was added.
MIN_PEAK_DB_FOR_SPECTRAL_CHECK = -40.0

# Minimum astats "Flat factor" to count as real clipping, not incidental
# same-value runs in loud content. Empirically calibrated: a clean quiet
# file measured 0.0, genuinely loud (but not clipped) white noise measured
# 0.0094, but even the mildest real clipping tested (samples just barely
# touching full scale, 2% overdrive) jumped to 16.06, up to 31.88 for hard
# clipping. 1.0 sits with ~100x margin below the noise case and ~16x
# margin below the mildest real-clipping case.
MIN_FLAT_FACTOR_FOR_CLIPPING = 1.0

# True Peak (dBTP) at or above this indicates inter-sample reconstruction
# overshoot — real digital-to-analog playback can clip even when no single
# *sample* is at full scale, which Flat factor alone can't see. Confirmed
# on a real file: white noise measured Flat factor 0.0094 (below the
# clipping threshold above, correctly not "clipping") but True Peak
# +3.71dBTP — a genuine, distinct defect Flat factor missed entirely.
TRUE_PEAK_THRESHOLD_DBTP = 0.0

# LUFS integrated-loudness thresholds for the coarse gradient (see module
# docstring for the real-world reference points these approximate).
LUFS_POOR_THRESHOLD = -8.0
LUFS_OK_THRESHOLD = -11.0
LUFS_GOOD_THRESHOLD = -16.0

FFMPEG_TIMEOUT_SECONDS = 60


class FfmpegNotFoundError(RuntimeError):
    """Raised when the ffmpeg binary can't be located."""


def find_ffmpeg(explicit_path: str | None = None) -> str:
    """Locate the ffmpeg binary.

    Checks an explicit configured path first (Options page), falls back to
    searching PATH, same pattern Picard's own core uses for the AcoustID
    fpcalc path.
    """
    if explicit_path:
        if os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK):
            return explicit_path
        raise FfmpegNotFoundError(f"Configured ffmpeg path is not an executable file: {explicit_path}")
    path = shutil.which('ffmpeg')
    if not path:
        raise FfmpegNotFoundError("ffmpeg not found on PATH")
    return path


def content_hash(filename: str) -> str:
    """Real hash of the file's current bytes on disk.

    Not yet isolated to just the audio stream — tag edits change this too,
    since tags live in the same file. Isolating to PCM-only content is
    real future work now that we do decode audio here anyway.
    """
    hasher = hashlib.blake2b(digest_size=16)
    with open(filename, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def _run_ffmpeg_filter(ffmpeg: str, filename: str, filter_str: str) -> str:
    """Runs ffmpeg with a given audio filter, discarding output, returns stderr.

    ffmpeg's analysis filters (astats, volumedetect, loudnorm) print their
    results to stderr as a side effect of decoding — this is the standard
    way to use them for measurement rather than transformation.
    """
    proc = subprocess.run(
        [ffmpeg, '-nostdin', '-hide_banner', '-i', filename, '-af', filter_str, '-f', 'null', '-'],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
        check=False,
    )
    return proc.stderr


def _parse_astats(stderr: str) -> tuple[float, float | None]:
    """Extract (flat_factor, peak_level_db) from astats' final "Overall" block.

    astats prints one block per channel, then a combined "Overall" block
    last — taking the last regex match of each field name lands on that
    combined block regardless of channel count.
    """
    flat_factor = 0.0
    peak_db = None
    for m in re.finditer(r'Flat factor:\s*([\-\d.]+)', stderr):
        flat_factor = float(m.group(1))
    for m in re.finditer(r'Peak level dB:\s*([\-\d.]+)', stderr):
        peak_db = float(m.group(1))
    return flat_factor, peak_db


def _parse_mean_volume(stderr: str) -> float | None:
    m = re.search(r'mean_volume:\s*([\-\d.]+)\s*dB', stderr)
    return float(m.group(1)) if m else None


def _parse_loudnorm(stderr: str) -> tuple[float | None, float | None]:
    """Returns (integrated_lufs, true_peak_dbtp)."""
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.DOTALL)
    if not m:
        return None, None
    try:
        data = json.loads(m.group(0))
        return float(data['input_i']), float(data['input_tp'])
    except (ValueError, KeyError, TypeError):
        return None, None


def _channel_count(astats_stderr: str) -> int:
    """astats prints one 'DC offset' line per channel, plus one for the
    combined "Overall" block — count() - 1 gives the channel count without
    a second ffprobe call or subprocess.
    """
    return max(0, astats_stderr.count('DC offset') - 1)


def _parse_phasemeter(stderr: str) -> tuple[bool, bool]:
    """Returns (is_mono_duplicated, is_out_of_phase).

    aphasemeter's own phasing=1 mode does the classification (tolerance/
    angle thresholds are ffmpeg's own, not something calibrated here) —
    presence of these markers in stderr is the whole signal.
    """
    return 'mono_start' in stderr, 'out_phase_start' in stderr


@dataclass
class AnalysisResult:
    tier: str
    issues: list[str]
    info: list[str]
    content_hash: str
    lufs: float | None
    true_peak_dbtp: float | None
    clipping_flat_factor: float
    spectral_energy_above_cutoff_db: float | None


def analyze_file(filename: str, ffmpeg_path: str | None = None) -> AnalysisResult:
    """Runs on a background thread — real decode + measurement work, not
    instant. Separate ffmpeg passes for clarity/robustness; combining into
    one filtergraph (asplit into astats/volumedetect/loudnorm/aphasemeter
    branches) is a real future optimization once this is proven correct.
    """
    ffmpeg = find_ffmpeg(ffmpeg_path)

    astats_stderr = _run_ffmpeg_filter(ffmpeg, filename, 'astats')
    flat_factor, peak_db = _parse_astats(astats_stderr)
    channels = _channel_count(astats_stderr)

    cutoff_stderr = _run_ffmpeg_filter(
        ffmpeg, filename, f'highpass=f={SPECTRAL_CUTOFF_FREQUENCY_HZ},volumedetect'
    )
    above_cutoff_db = _parse_mean_volume(cutoff_stderr)

    loudnorm_stderr = _run_ffmpeg_filter(ffmpeg, filename, 'loudnorm=print_format=json')
    lufs, true_peak = _parse_loudnorm(loudnorm_stderr)

    issues: list[str] = []
    info: list[str] = []

    has_clipping = flat_factor > MIN_FLAT_FACTOR_FOR_CLIPPING
    if has_clipping:
        issues.append("Clipping detected")

    has_true_peak_overs = true_peak is not None and true_peak >= TRUE_PEAK_THRESHOLD_DBTP
    if has_true_peak_overs and not has_clipping:
        # Only report separately when Flat factor didn't already catch a
        # defect — both signals pointing at "this file clips" is redundant
        # to say twice, but true-peak-only is a genuinely distinct finding
        # (inter-sample overshoot with no sample actually at full scale).
        issues.append(f"Inter-sample peaks exceed full scale (True Peak {true_peak:+.1f}dBTP)")

    has_signal = peak_db is not None and peak_db > MIN_PEAK_DB_FOR_SPECTRAL_CHECK
    has_cutoff = (
        has_signal and above_cutoff_db is not None and above_cutoff_db < SPECTRAL_SILENCE_THRESHOLD_DB
    )
    if has_cutoff:
        issues.append(
            f"No real content above {SPECTRAL_CUTOFF_FREQUENCY_HZ / 1000:.0f}kHz "
            "(likely transcoded from a lossy source)"
        )

    # Phase/channel-identity checks need two channels to compare — meaningless
    # (and aphasemeter would just misbehave) on mono source material.
    if channels >= 2:
        phase_stderr = _run_ffmpeg_filter(ffmpeg, filename, 'aphasemeter=video=0:phasing=1')
        is_mono_duplicated, is_out_of_phase = _parse_phasemeter(phase_stderr)
        if is_out_of_phase:
            # A real, audible defect — will cancel out when summed to mono
            # (many phone/laptop/car speakers do this) — a genuine gate
            # issue, not just informational.
            issues.append("Channels are out of phase (cancels out when played back in mono)")
        if is_mono_duplicated:
            # Not a quality defect — a mono source duplicated into both
            # channels loses nothing, it's just wasteful. Informational,
            # doesn't affect the tier.
            info.append("Left/right channels are identical (mono content in a stereo container)")

    if issues:
        tier = "Bad"
    elif lufs is None:
        tier = "Excellent"
    elif lufs > LUFS_POOR_THRESHOLD:
        tier = "Poor"
        issues.append(f"Heavily compressed master (~{lufs:.1f} LUFS integrated)")
    elif lufs > LUFS_OK_THRESHOLD:
        tier = "Ok"
        issues.append(f"Compressed master (~{lufs:.1f} LUFS integrated)")
    elif lufs > LUFS_GOOD_THRESHOLD:
        tier = "Good"
        issues.append(f"Moderately loud master (~{lufs:.1f} LUFS integrated)")
    else:
        tier = "Excellent"

    return AnalysisResult(
        tier=tier,
        issues=issues,
        info=info,
        content_hash=content_hash(filename),
        lufs=lufs,
        true_peak_dbtp=true_peak,
        clipping_flat_factor=flat_factor,
        spectral_energy_above_cutoff_db=above_cutoff_db,
    )
