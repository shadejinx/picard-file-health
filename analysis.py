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
import re
import shutil
import subprocess


# Below this level (relative to full scale), content above the cutoff
# frequency is considered "not really there" — empirically, a genuinely
# lowpassed test signal measured -91dB here, a clean wideband signal
# measured -12.7dB. -60dB sits well clear of both.
SPECTRAL_SILENCE_THRESHOLD_DB = -60.0
SPECTRAL_CUTOFF_FREQUENCY_HZ = 17000

# LUFS integrated-loudness thresholds for the coarse gradient (see module
# docstring for the real-world reference points these approximate).
LUFS_POOR_THRESHOLD = -8.0
LUFS_OK_THRESHOLD = -11.0
LUFS_GOOD_THRESHOLD = -16.0

FFMPEG_TIMEOUT_SECONDS = 60


class FfmpegNotFoundError(RuntimeError):
    """Raised when the ffmpeg binary can't be located."""


def find_ffmpeg() -> str:
    """Locate the ffmpeg binary.

    TODO: make this configurable via the Options page (explicit path
    setting + a guided find/download flow), the same pattern Picard's own
    core uses for the AcoustID fpcalc path. Noted, not yet built.
    """
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


def _parse_loudnorm_lufs(stderr: str) -> float | None:
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return float(data['input_i'])
    except (ValueError, KeyError, TypeError):
        return None


@dataclass
class AnalysisResult:
    tier: str
    issues: list[str]
    content_hash: str
    lufs: float | None
    clipping_flat_factor: float
    spectral_energy_above_cutoff_db: float | None


def analyze_file(filename: str) -> AnalysisResult:
    """Runs on a background thread — real decode + measurement work, not
    instant. Three separate ffmpeg passes for clarity/robustness; combining
    into one filtergraph (asplit into astats/volumedetect/loudnorm branches)
    is a real future optimization once this is proven correct.
    """
    ffmpeg = find_ffmpeg()

    astats_stderr = _run_ffmpeg_filter(ffmpeg, filename, 'astats')
    flat_factor, _peak_db = _parse_astats(astats_stderr)

    cutoff_stderr = _run_ffmpeg_filter(
        ffmpeg, filename, f'highpass=f={SPECTRAL_CUTOFF_FREQUENCY_HZ},volumedetect'
    )
    above_cutoff_db = _parse_mean_volume(cutoff_stderr)

    loudnorm_stderr = _run_ffmpeg_filter(ffmpeg, filename, 'loudnorm=print_format=json')
    lufs = _parse_loudnorm_lufs(loudnorm_stderr)

    issues: list[str] = []
    has_clipping = flat_factor > 0
    if has_clipping:
        issues.append("Clipping detected")

    has_cutoff = above_cutoff_db is not None and above_cutoff_db < SPECTRAL_SILENCE_THRESHOLD_DB
    if has_cutoff:
        issues.append(
            f"No real content above {SPECTRAL_CUTOFF_FREQUENCY_HZ / 1000:.0f}kHz "
            "(likely transcoded from a lossy source)"
        )

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
        content_hash=content_hash(filename),
        lufs=lufs,
        clipping_flat_factor=flat_factor,
        spectral_energy_above_cutoff_db=above_cutoff_db,
    )
