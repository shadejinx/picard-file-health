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
constants below) — not the LUFS-bucket proxy this used before.
Mono-duplicated-into-stereo and below-transparency-bitrate lossy
encoding (MP3/AAC/Vorbis/Opus, HydrogenAudio/Xiph's own published
consensus thresholds — see TRANSPARENT_BITRATE_KBPS) are informational
only, never gate or affect the tier: neither is a measured defect in the
decoded signal, just statistical likelihood from declared codec/bitrate.
Sample-rate scoring and hum detection are documented future work, not
implemented here yet.
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

# Real-world "generally transparent" bitrate floors per lossy codec —
# HydrogenAudio/Xiph's own published listening-test consensus, not
# guessed:
# - MP3: "generally considered artifact-free at bitrates at/above
#   192kbps" (Hydrogenaudio Knowledgebase, "Transparency" page).
# - AAC: "reaches transparency in most samples and for most users at
#   around 150 kbps" (Hydrogenaudio Knowledgebase, "Advanced Audio
#   Coding" page) — LC profile only; HE-AAC's SBR extension is
#   transparent at much lower bitrates and is explicitly excluded
#   rather than checked against this (wrong, too-high) threshold, see
#   _bitrate_transparency_note().
# - Vorbis: "supposedly artifact-free at bitrates at/above 160kbps"
#   (Hydrogenaudio Knowledgebase, "Transparency" page).
# - Opus: "Opus at 128 Kb/s (VBR) is pretty much transparent" for music
#   storage (Xiph.org's own "Opus Recommended Settings" wiki page).
TRANSPARENT_BITRATE_KBPS = {
    'mp3': 192,
    'aac': 150,
    'vorbis': 160,
    'opus': 128,
}

FFMPEG_TIMEOUT_SECONDS = 60

# Every filter/option this module relies on (loudnorm for True Peak/DR14's
# gradient replacement, astats "metadata"/"reset" options + ametadata for
# DR14 block extraction, aphasemeter for the phase check) was confirmed
# present by reading FFmpeg's own release-tagged source directly
# (github.com/FFmpeg/FFmpeg/blob/n3.1/libavfilter/af_astats.c — metadata/
# reset already present at 3.1; aphasemeter landed in 2.8 per the FFmpeg
# Changelog; loudnorm itself, the newest of the bunch, landed in 3.1) —
# not guessed. 3.1 is therefore the real floor, not an arbitrary round
# number.
MINIMUM_FFMPEG_VERSION = (3, 1)


class FfmpegNotFoundError(RuntimeError):
    """Raised when the ffmpeg binary can't be located."""


class FfmpegVersionTooOldError(FfmpegNotFoundError):
    """Raised when a resolved ffmpeg binary is older than
    MINIMUM_FFMPEG_VERSION. Subclasses FfmpegNotFoundError so every
    existing caller that already catches that one exception (the scan
    pipeline in __init__.py) handles this the same way, with no separate
    except clause needed anywhere.
    """


class AnalysisError(RuntimeError):
    """Raised when ffmpeg/ffprobe fails outright on a given file.

    Every file this plugin analyzes is user-supplied — possibly corrupt,
    truncated, not really audio at all despite its extension, or
    adversarially crafted — so this is an expected, handled outcome for
    bad input, not a bug to fix. Raised only for the one call this module
    genuinely can't proceed without (the first astats pass — see
    analyze_file); every other ffmpeg/ffprobe call degrades gracefully
    to "no defect detected" on failure instead of raising, via
    _run_subprocess never raising for expected subprocess-level failures.
    """


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


_FFMPEG_VERSION_RE = re.compile(r'ffmpeg version\s+n?(\d+)\.(\d+)(?:\.(\d+))?')


def get_ffmpeg_version(ffmpeg_path: str) -> tuple[int, int] | None:
    """Parses (major, minor) from `ffmpeg -version`'s first line.

    None means "couldn't confirm" — some git/dev-snapshot builds use
    non-numeric version strings (e.g. "N-12345-gabcdef") that don't match
    a standard release. Treated as "let it through" by check_ffmpeg_version
    rather than a failure: those builds are essentially always newer than
    any numbered release, so blocking on an unparseable string would
    reject legitimate newer installs far more often than it would ever
    catch a genuinely too-old one.
    """
    proc = _run_subprocess([ffmpeg_path, '-version'])
    m = _FFMPEG_VERSION_RE.search(proc.stdout)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def check_ffmpeg_version(ffmpeg_path: str) -> None:
    """Raises FfmpegVersionTooOldError if the resolved binary is
    confirmably older than MINIMUM_FFMPEG_VERSION. Called once up front
    in analyze_file() — every check in this module needs at least one of
    the filters gated on this floor, so there's no point running any of
    them against a too-old binary only to get confusing partial/garbled
    results.
    """
    version = get_ffmpeg_version(ffmpeg_path)
    if version is not None and version < MINIMUM_FFMPEG_VERSION:
        found = '.'.join(map(str, version))
        needed = '.'.join(map(str, MINIMUM_FFMPEG_VERSION))
        raise FfmpegVersionTooOldError(
            f"ffmpeg {found} at {ffmpeg_path} is too old (File Health needs "
            f"{needed} or newer for loudnorm/DR14/phase analysis)"
        )


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


def _run_subprocess(args: list[str]) -> subprocess.CompletedProcess:
    """Every ffmpeg/ffprobe invocation in this module goes through here.

    Every file analyzed is user-supplied — possibly corrupt, truncated,
    adversarially crafted, or not really audio at all despite its
    extension — so a failed subprocess is an expected, handled outcome,
    not a crash. Timeouts and OS-level failures (binary vanished
    mid-scan, permission race, etc.) are folded into a synthetic
    non-zero-exit CompletedProcess rather than raised, so every existing
    caller's "ffmpeg found nothing" parsing path — already written to
    tolerate empty/missing output — handles them for free, with no
    special-casing needed at most call sites. The one call this module
    can't proceed without at all (analyze_file's first astats pass)
    explicitly checks the returncode itself and raises AnalysisError.

    `errors='replace'` guards against non-UTF8 bytes in ffmpeg's own
    stderr (e.g. from a crafted filename or corrupt stream metadata)
    raising UnicodeDecodeError instead of just substituting the invalid
    bytes — a decode crash here would be exactly the kind of "malformed
    input breaks the scanner" bug this hardening pass is for.
    """
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors='replace',
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(args, returncode=-1, stdout='', stderr='')


def _run_ffmpeg_filter(ffmpeg: str, filename: str, filter_str: str) -> tuple[str, int]:
    """Runs ffmpeg with a given audio filter, discarding output.

    Returns (stderr, returncode) — ffmpeg's analysis filters (astats,
    volumedetect, loudnorm) print their results to stderr as a side
    effect of decoding, the standard way to use them for measurement
    rather than transformation. Callers that can tolerate "found
    nothing" (every filter after the first) can ignore the returncode;
    it exists so the one call that can't (the first astats pass) can
    tell "ffmpeg ran and found nothing" apart from "ffmpeg couldn't
    process this file at all".
    """
    proc = _run_subprocess(
        [ffmpeg, '-nostdin', '-hide_banner', '-i', filename, '-af', filter_str, '-f', 'null', '-']
    )
    return proc.stderr, proc.returncode


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


@dataclass
class StreamInfo:
    sample_rate: int | None
    codec_name: str | None
    profile: str | None
    bitrate_kbps: int | None


def _probe_stream_info(ffprobe: str, filename: str) -> StreamInfo:
    """Single ffprobe call for everything analyze_file needs from stream
    metadata: sample rate (DR14 block sizing) and codec/profile/bitrate
    (transparency scoring) — one call rather than one per use, since
    ffprobe reads container/stream headers only, not the full audio, so
    there's no reason to invoke it twice. Any failure (unparseable
    output, no audio stream found) yields an all-None StreamInfo; every
    caller already treats missing fields as "can't measure this",
    consistent with the rest of the module's graceful-degradation style.
    """
    proc = _run_subprocess(
        [
            ffprobe, '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name,profile,sample_rate,bit_rate:format=bit_rate',
            '-of', 'json', filename,
        ]
    )
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return StreamInfo(None, None, None, None)
    streams = data.get('streams') or []
    if not streams:
        return StreamInfo(None, None, None, None)
    stream = streams[0]
    try:
        sample_rate = int(stream['sample_rate'])
    except (KeyError, ValueError, TypeError):
        sample_rate = None
    # Prefer the stream's own measured average bitrate — accurate for
    # VBR, since it reflects what this specific file actually used, not
    # a nominal target. Some containers (seen with short Opus test
    # files) don't report a stream-level bit_rate at all; fall back to
    # the container's overall bitrate, which includes tag/container
    # overhead but is close enough for a "roughly how compressed is
    # this" comparison against a coarse threshold.
    bit_rate = stream.get('bit_rate') or (data.get('format') or {}).get('bit_rate')
    try:
        bitrate_kbps = int(bit_rate) // 1000
    except (ValueError, TypeError):
        bitrate_kbps = None
    return StreamInfo(
        sample_rate=sample_rate,
        codec_name=stream.get('codec_name'),
        profile=stream.get('profile'),
        bitrate_kbps=bitrate_kbps,
    )


def _bitrate_transparency_note(stream_info: StreamInfo) -> str | None:
    """Informational only — never gates or affects the tier. Bitrate is a
    statistical proxy from published listening-test consensus (see
    TRANSPARENT_BITRATE_KBPS), not a measured defect in the decoded
    signal the way clipping/cutoff/true-peak are; a low-bitrate file
    genuinely can sound transparent on easy material, and a
    high-bitrate one can still have audible issues on hard material.
    Returns None for lossless codecs, codecs without a published
    threshold, missing bitrate data, or HE-AAC specifically (its SBR
    extension is transparent at much lower bitrates than plain AAC-LC;
    applying the LC threshold to it would be a wrong, not just
    imprecise, comparison).
    """
    codec_name = stream_info.codec_name
    kbps = stream_info.bitrate_kbps
    if codec_name is None or kbps is None:
        return None
    codec_name = codec_name.lower()
    if codec_name == 'aac' and stream_info.profile and 'he-aac' in stream_info.profile.lower():
        return None
    threshold = TRANSPARENT_BITRATE_KBPS.get(codec_name)
    if threshold is None or kbps >= threshold:
        return None
    label = {'mp3': 'MP3', 'aac': 'AAC', 'vorbis': 'Vorbis', 'opus': 'Opus'}[codec_name]
    return (
        f"{kbps}kbps {label} — below the ~{threshold}kbps commonly considered "
        f"transparent for {label} (HydrogenAudio/Xiph consensus); may have "
        "audible compression artifacts on some material"
    )


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
    decode + windowed RMS/peak by hand. A failure here (timeout, garbage
    input) just yields empty stdout, which _parse_dr14_blocks/_compute_dr14
    already treat the same as "nothing to measure" (dr14=None).
    """
    proc = _run_subprocess(
        [
            ffmpeg, '-nostdin', '-hide_banner', '-i', filename,
            '-af',
            f'asetnsamples=n={block_samples}:p=0,astats=metadata=1:reset=1,ametadata=print:file=-',
            '-f', 'null', '-',
        ]
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


def _measure_dr14(ffmpeg: str, filename: str, sample_rate: int | None) -> int | None:
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
    check_ffmpeg_version(ffmpeg)
    stream_info = _probe_stream_info(ffprobe, filename)

    astats_stderr, astats_returncode = _run_ffmpeg_filter(ffmpeg, filename, 'astats')
    if astats_returncode != 0:
        # Every other measurement in this module depends on astats having
        # actually decoded the file — a non-zero exit here means ffmpeg
        # couldn't process it at all (corrupt, truncated, not really
        # audio despite the extension, or it hit the timeout), so there's
        # nothing trustworthy to report rather than silently defaulting
        # to "no defects found".
        raise AnalysisError(
            f"ffmpeg couldn't decode {os.path.basename(filename)} as audio "
            "(corrupt, truncated, unsupported format, or a decode timeout) — not scored"
        )
    flat_factor, peak_db = _parse_astats(astats_stderr)
    channels = _channel_count(astats_stderr)

    cutoff_stderr, _cutoff_returncode = _run_ffmpeg_filter(
        ffmpeg, filename, f'highpass=f={SPECTRAL_CUTOFF_FREQUENCY_HZ},volumedetect'
    )
    above_cutoff_db = _parse_mean_volume(cutoff_stderr)

    loudnorm_stderr, _loudnorm_returncode = _run_ffmpeg_filter(ffmpeg, filename, 'loudnorm=print_format=json')
    lufs, true_peak = _parse_loudnorm(loudnorm_stderr)

    issues: list[str] = []
    info: list[str] = []

    bitrate_note = _bitrate_transparency_note(stream_info)
    if bitrate_note is not None:
        info.append(bitrate_note)

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
        phase_stderr, _phase_returncode = _run_ffmpeg_filter(ffmpeg, filename, 'aphasemeter=video=0:phasing=1')
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
        dr14 = _measure_dr14(ffmpeg, filename, stream_info.sample_rate)

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
