"""Real audio-signal analysis, via ffmpeg.

Every threshold/formula here was empirically validated against real ffmpeg
output before being written (see the plugin's development history) — not
guessed from documentation. Gate checks implemented (force tier "Bad"):
clipping (astats Flat factor, calibrated threshold), True Peak
inter-sample overs, spectral-cutoff/transcode detection (confirmed, when
the file has one, against the LAME encoder's own embedded low-pass
setting — decisive rather than heuristic evidence of a prior lossy
generation), and out-of-phase channels. When no gate fires, the tier
gradient (Poor/Ok/Good/Great/Excellent) is set by a real DR14 dynamic-
range measurement (Pleasurize Music Foundation "TT DR Meter" algorithm,
reimplemented against ffmpeg's own astats filter and validated bit-for-
bit against the open-source reference implementation — see the DR14_*
constants below) — not the LUFS-bucket proxy this used before. Mono-
duplicated-into-stereo is informational only, not a gate issue.
Bitrate-vs-codec-transparency scoring, sample-rate scoring, and hum
detection are documented future work, not implemented here yet.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from mutagen.mp3 import (
    HeaderNotFoundError,
    MPEGFrame,
    XingHeader,
    XingHeaderError,
    skip_id3,
)

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

# Real-world DR14 quality bands, grounded in the official TT DR Offline
# Meter manual's own documented color scale (red below DR8, green at
# DR14+, yellow in between) and its worked examples (DR9 called a
# "market-oriented compromise", DR12-14 called "more desirable") — not
# guessed. Puts the previously-unused "Great" tier to work, since DR's
# wider practical range (0-20+) has more useful resolution than LUFS did.
DR14_POOR_THRESHOLD = 8
DR14_OK_THRESHOLD = 10
DR14_GOOD_THRESHOLD = 12
DR14_GREAT_THRESHOLD = 14

# DR14 algorithm parameters (Pleasurize Music Foundation "TT DR Meter"):
# non-overlapping 3-second blocks, top 20% loudest (by RMS) compared
# against the second-highest peak across all blocks.
DR14_BLOCK_SECONDS = 3
DR14_TOP_FRACTION = 0.2
# The reference implementation widens the block by 60 samples/sec, but
# ONLY at exactly 44100 Hz — an undocumented quirk of the official tool's
# own block sizing. Confirmed empirically (synthetic boundary-case audio)
# that omitting it flips the rounded DR value at 44.1kHz, the single most
# common consumer sample rate, so it's replicated rather than dropped as
# a presumed no-op historical artifact.
DR14_SAMPLE_RATE_FUDGE_HZ = {44100: 60}

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


def _find_ffprobe(ffmpeg_path: str) -> str:
    """ffprobe ships alongside ffmpeg in every mainstream distribution
    (Homebrew, apt, the official static builds, Windows installers) —
    same directory, same install. Falls back to a bare PATH search for a
    non-standard install where that sibling binary isn't there.
    """
    ffprobe_name = 'ffprobe.exe' if ffmpeg_path.lower().endswith('.exe') else 'ffprobe'
    sibling = os.path.join(os.path.dirname(ffmpeg_path), ffprobe_name)
    if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
        return sibling
    found = shutil.which(ffprobe_name)
    if found:
        return found
    raise FfmpegNotFoundError("ffprobe not found (expected alongside ffmpeg)")


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
class LameHeader:
    version: str
    lowpass_hz: int | None


def _read_lame_header(filename: str) -> LameHeader | None:
    """Reads the LAME encoder's own embedded Info/Xing tag from the first
    MP3 frame, per the "Mp3 Info Tag rev1" spec
    (http://gabriel.mp3-tech.org/mp3infotag.html) — byte $A6 is the exact
    low-pass filter frequency (Hz/100) LAME itself applied for this
    specific encode, not a guess derived from bitrate tables. Uses
    mutagen's frame/Xing parser (bundled with Picard, same library Picard
    core already uses for every format's tag reading) rather than
    hand-rolled bit parsing — validated against files produced by the
    real `lame` CLI at known settings (`-V0`, `--lowpass N`) before this
    was wired into analyze_file().

    None means "no usable LAME tag" — not present (many valid MP3s have
    none, e.g. non-LAME encoders or a stripped tag), the wrong frame type,
    or a truncated/corrupt header. Absence is not itself a defect.
    """
    try:
        with open(filename, 'rb') as fh:
            skip_id3(fh)
            frame = MPEGFrame(fh)
            if frame.layer != 3:
                return None
            offset = XingHeader.get_offset(frame)
            fh.seek(frame.frame_offset + offset, 0)
            xing = XingHeader(fh)
    except (HeaderNotFoundError, XingHeaderError, OSError):
        return None
    if xing.lame_header is None:
        return None
    lowpass = xing.lame_header.lowpass_filter
    return LameHeader(version=xing.lame_version_desc, lowpass_hz=lowpass or None)


def _probe_sample_rate(ffprobe: str, filename: str) -> int | None:
    """Only ffprobe call in the module — needed to size DR14's 3-second
    blocks (in samples) before invoking ffmpeg's windowed astats pass.
    """
    proc = subprocess.run(
        [
            ffprobe, '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=sample_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1', filename,
        ],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
    )
    try:
        return int(proc.stdout.strip())
    except (ValueError, TypeError):
        return None


def _dr14_block_samples(sample_rate: int) -> int:
    fudge = DR14_SAMPLE_RATE_FUDGE_HZ.get(sample_rate, 0)
    return DR14_BLOCK_SECONDS * (sample_rate + fudge)


_DR14_METRIC_RE = re.compile(r'lavfi\.astats\.(\d+)\.(RMS_level|Peak_level)=(-?[\d.]+|-?inf)')


def _run_dr14_metadata(ffmpeg: str, filename: str, block_samples: int) -> str:
    """One ffmpeg pass, framed into non-overlapping DR14 blocks
    (`asetnsamples`, unpadded so a short final block isn't diluted with
    zeros), astats reset every block so each block's stats are
    independent (not cumulative), dumped as text metadata to stdout —
    ffmpeg has no native DR14 filter, so the block-level RMS/Peak numbers
    it already knows how to compute are combined into the real DR14
    formula in Python (_compute_dr14), rather than reimplementing PCM
    decode + windowed RMS/peak by hand.
    """
    proc = subprocess.run(
        [
            ffmpeg, '-nostdin', '-hide_banner', '-i', filename,
            '-af',
            f'asetnsamples=n={block_samples}:p=0,astats=metadata=1:reset=1,ametadata=print:file=-',
            '-f', 'null', '-',
        ],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
    )
    return proc.stdout


def _parse_dr14_blocks(stdout: str) -> dict[int, tuple[list[float], list[float]]]:
    """Per channel: (per-block RMS_level dB, per-block Peak_level dB), one
    pair of entries per DR14 block, in file order.
    """
    per_channel: dict[int, tuple[list[float], list[float]]] = {}
    for frame_text in stdout.split('frame:')[1:]:
        for m in _DR14_METRIC_RE.finditer(frame_text):
            channel = int(m.group(1))
            raw = m.group(3)
            value = -200.0 if raw in ('-inf', 'inf') else float(raw)
            rms_list, peak_list = per_channel.setdefault(channel, ([], []))
            (rms_list if m.group(2) == 'RMS_level' else peak_list).append(value)
    return per_channel


def _compute_dr14(per_channel: dict[int, tuple[list[float], list[float]]]) -> int | None:
    """Pleasurize Music Foundation DR14 formula, per channel then averaged:
    top DR14_TOP_FRACTION of blocks by RMS (power-domain average, RMS
    scaled by sqrt(2) — the official meter's own "+3dB so a sine wave
    reads the same as its own peak" convention) compared against the
    *second*-highest peak across all blocks (not the single highest, so
    one outlier sample can't dominate). Validated bit-for-bit against the
    open-source reference implementation (dr14meter/dr14_t.meter, itself
    tested identical to the official Windows tool) on synthetic
    multi-block fixtures with known per-block levels — see commit history.
    """
    channel_values: list[float] = []
    for rms_db, peak_db in per_channel.values():
        seg_cnt = len(rms_db)
        if seg_cnt == 0:
            continue
        n_blk = max(1, math.floor(seg_cnt * DR14_TOP_FRACTION))
        # Squaring folds the sqrt(2) "dr_rms" convention into a factor of
        # 2 on each power-domain term.
        dr_rms_sq_sorted = sorted(2.0 * (10.0 ** (db / 10.0)) for db in rms_db)
        peak_linear_sorted = sorted(10.0 ** (db / 20.0) for db in peak_db)
        rms_sum = sum(dr_rms_sq_sorted[-n_blk:])
        if rms_sum <= 0:
            channel_values.append(0.0)
            continue
        rms_quadratic_mean = math.sqrt(rms_sum / n_blk)
        peak_index = -2 if len(peak_linear_sorted) >= 2 else -1
        peak_second_highest = peak_linear_sorted[peak_index]
        if peak_second_highest <= 0:
            channel_values.append(0.0)
            continue
        channel_values.append(-20.0 * math.log10(rms_quadratic_mean / peak_second_highest))
    if not channel_values:
        return None
    return round(sum(channel_values) / len(channel_values))


def _measure_dr14(ffmpeg: str, ffprobe: str, filename: str) -> int | None:
    sample_rate = _probe_sample_rate(ffprobe, filename)
    if not sample_rate:
        return None
    block_samples = _dr14_block_samples(sample_rate)
    stdout = _run_dr14_metadata(ffmpeg, filename, block_samples)
    per_channel = _parse_dr14_blocks(stdout)
    return _compute_dr14(per_channel)


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
    lame_lowpass_hz: int | None
    dr14: int | None


def analyze_file(filename: str, ffmpeg_path: str | None = None) -> AnalysisResult:
    """Runs on a background thread — real decode + measurement work, not
    instant. Separate ffmpeg passes for clarity/robustness; combining into
    one filtergraph (asplit into astats/volumedetect/loudnorm/aphasemeter
    branches) is a real future optimization once this is proven correct.
    """
    ffmpeg = find_ffmpeg(ffmpeg_path)
    ffprobe = _find_ffprobe(ffmpeg)

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
    lame_lowpass_hz: int | None = None
    if has_cutoff:
        lame_header = _read_lame_header(filename)
        if lame_header is not None:
            lame_lowpass_hz = lame_header.lowpass_hz
        if lame_lowpass_hz is not None and lame_lowpass_hz > SPECTRAL_CUTOFF_FREQUENCY_HZ:
            # Decisive, not heuristic: the LAME encoder's own recorded
            # low-pass setting (read straight from its embedded Info tag,
            # not guessed from a bitrate table) let content through well
            # past our check frequency — so this encode's own filtering
            # cannot explain the measured silence up there. The source
            # feeding this encode was already missing that content.
            issues.append(
                f"Confirmed transcode: LAME's own settings allowed content up to "
                f"{lame_lowpass_hz / 1000:.1f}kHz through unfiltered, but none exists "
                f"above {SPECTRAL_CUTOFF_FREQUENCY_HZ / 1000:.0f}kHz — the source was "
                "already lossy before this encode"
            )
        else:
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

    dr14: int | None = None
    if not issues:
        # Only worth the extra ffprobe + windowed-astats pass when no gate
        # already forced "Bad" — a defective file's dynamic range doesn't
        # change its tier either way.
        dr14 = _measure_dr14(ffmpeg, ffprobe, filename)

    if issues:
        tier = "Bad"
    elif dr14 is None:
        tier = "Excellent"
    elif dr14 < DR14_POOR_THRESHOLD:
        tier = "Poor"
        issues.append(f"Heavily compressed master (DR{dr14})")
    elif dr14 < DR14_OK_THRESHOLD:
        tier = "Ok"
        issues.append(f"Compressed master (DR{dr14})")
    elif dr14 < DR14_GOOD_THRESHOLD:
        tier = "Good"
        issues.append(f"Moderately compressed master (DR{dr14})")
    elif dr14 < DR14_GREAT_THRESHOLD:
        tier = "Great"
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
        lame_lowpass_hz=lame_lowpass_hz,
        dr14=dr14,
    )
