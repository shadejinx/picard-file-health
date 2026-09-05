"""Real audio-signal analysis, via ffmpeg.

Every threshold/formula here was empirically validated against real ffmpeg
output before being written (see the plugin's development history) — not
guessed from documentation. Gate checks implemented (force tier "Bad"):
clipping (astats Flat factor, calibrated threshold), True Peak
inter-sample overs, spectral-cutoff/transcode detection (confirmed, when
the file has one, against the LAME encoder's own embedded low-pass
setting — decisive rather than heuristic evidence of a prior lossy
generation), fake-hi-res detection (declared sample rate above 48kHz
with no real spectral content above 24kHz — upsampled from an ordinary
source, not genuine hi-res), and out-of-phase channels. When no gate
fires, the tier gradient (Poor/Ok/Good/Great/Excellent) is set by a
real DR14 dynamic-range measurement (Pleasurize Music Foundation "TT DR
Meter" algorithm, reimplemented against ffmpeg's own astats filter and
validated bit-for-bit against the open-source reference implementation
— see the DR14_* constants below) — not the LUFS-bucket proxy this used
before. Mono-duplicated-into-stereo, below-transparency-bitrate lossy
encoding (MP3/AAC/Vorbis/Opus, HydrogenAudio/Xiph's own published
consensus thresholds — see TRANSPARENT_BITRATE_KBPS), and possible
mains hum in a quiet passage (see HUM_* constants) are informational
only, never gate or affect the tier — none of these three is a
definitively measured defect the way clipping/cutoff/true-peak/fake-
hi-res are: bitrate is a statistical proxy from declared metadata, and
hum can't be told apart from a sustained musical note at the same
frequency with full certainty, only strong likelihood.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import mutagen
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

# "Hi-res" sample rates (88.2/96/176.4/192kHz and up) claim real content
# beyond ordinary CD/DVD-quality bandwidth. A file upsampled from an
# ordinary-rate source has a hard wall at that source's own Nyquist
# frequency — nothing genuinely new gets added by resampling. Checking
# above 24kHz (safely past both 44.1kHz-family's 22.05kHz Nyquist and
# 48kHz-family's 24kHz Nyquist, so real CD/DVD-quality masters of either
# family aren't penalized) catches this regardless of which "ordinary"
# rate the source was. Validated on real ffmpeg-resampled fixtures: a
# 16kHz-lowpassed 44.1kHz source resampled to 96kHz measured -91dB
# (silence) above 24kHz; a genuine 96kHz file with real content at
# 30kHz measured -14dB (clearly present) at the same threshold.
FAKE_HIRES_CHECK_FREQUENCY_HZ = 24000
# Only applies above ordinary DVD/video-audio rate (48kHz) — 44.1/48kHz
# files are covered by the existing 17kHz spectral-cutoff check instead.
FAKE_HIRES_MIN_SAMPLE_RATE_HZ = 48000

# Effective-bandwidth sweep: a real, continuous cutoff-frequency estimate
# (not the single-point binary check above), for the compare panel's
# "which copy has more real high-frequency content" ranking. Probe points
# span the cutoff range documented for real MP3 encoder profiles
# (D'Alessandro & Shi, "MP3 Bit Rate Quality Detection through Frequency
# Spectrum Analysis", ACM MM&Sec 2009: 128kbps≈16kHz, 192kbps≈17.5kHz,
# 256kbps≈18.5-19kHz, 320kbps≈20kHz — a PSD classifier over this exact
# band hit 97-99% accuracy on 2,512 real songs), finer near the middle of
# that range where most real encodes land.
BANDWIDTH_PROBE_FREQUENCIES_HZ = (
    12000, 13000, 14000, 15000, 16000, 16500, 17000, 17500,
    18000, 18500, 19000, 19500, 20000, 20500, 21000, 22000,
)
# Peak-relative, not absolute dBFS: the highest probe frequency whose
# measured level stays within this many dB of the track's own spectral
# peak counts as "real content" — a hard lowpass cliff drops far below
# this regardless of how loud or quiet the track is overall, so no
# separate near-silence guard is needed the way the absolute-threshold
# check above requires. Value matches the open-source `lossless-checker`
# tool's own published calibration (swept 45-75dB against a real library
# plus known-answer 128k/320k round-trip MP3 fakes; every 128k fake
# landed at 16.0-16.7kHz at this setting, genuine lossless clustered at
# 21-22kHz) rather than re-derived from scratch — a real library beats
# this project's own single synthetic sine/lowpass fixture.
BANDWIDTH_PEAK_RELATIVE_DB = 65.0

# File-integrity signature: ffmpeg's own MP3 decoder (mpegaudiodec) logs a
# "bits_left" diagnostic when a frame's Huffman/granule decode leaves an
# invalid (non-zero, often negative) number of bits after decoding a
# block — a genuine internal inconsistency in the bit-reservoir pointer
# (`main_data_begin`), not a guess. Requires `-err_detect compliant` on
# the decoder to surface (off by default).
#
# This replaces an earlier Max_difference/Mean_difference ratio
# heuristic that turned out to be fundamentally unsound: validated
# against a 500-file random real-library sample, it false-positived on
# 79% of ordinary music (real quiet-to-loud dynamic swings — a Tom
# Waits intro, a Miles Davis soundtrack cue — produce the same ratio
# signature as real corruption; there is no threshold that separates
# them, since the technique's own "confirmed corruption" calibration
# case sat at the 97th percentile of ordinary real content).
#
# `bits_left` was reached by direct experimentation against real
# fixtures, not guessed: engineered corruption of the MP3 bit-reservoir
# pointer (`main_data_begin`) was confirmed audible by ear and visible
# as a spectral dropout, and ffmpeg's own decoder logs exactly this
# condition (see mpegaudiodec_template.c: "some encoders generate an
# incorrect size for this part" is the softer, expected case logged as
# "overread" — tested separately and found present on ~5% of real
# files with thousands of occurrences and no audible/visible defect,
# unusable as a signal on its own). `bits_left` specifically, gated on
# `-err_detect compliant`, was validated against 600 real files spanning
# five different real libraries: 99.2% showed zero occurrences,
# including a file independently confirmed clean by ear despite 2142
# "overread" warnings — while every engineered corruption fixture
# (both a severe frame-desync case and a confirmed-audible one)
# produced at least one. Still informational only, never a gate: a
# nonzero count is a real decoder-level signal, not proof audible to a
# listener in every case (the mildest engineered fixture, a single
# faint click, didn't trigger it either — this catches moderate-to-
# severe cases, not every possible glitch).
CORRUPTION_BITS_LEFT_PATTERN = re.compile(r'bits_left=')

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
#
# Not set to 0.0dBTP (the theoretical full-scale ceiling): ffmpeg's
# loudnorm measures True Peak per ITU-R BS.1770-4 Annex 2, upsampling to
# 192kHz (~4x oversampling for 44.1/48kHz-family sources) before taking
# the peak. That Annex's own worked table of oversampling error gives a
# maximum theoretical under-read of 0.554dB at 4x oversampling — the
# table's own caption calls this row "probably covers the range of
# interest" — meaning a real, compliant master can legitimately measure
# a few tenths of a dB over 0dBTP purely from the meter's own documented
# accuracy limit, not because it's actually clipping on playback.
# Confirmed against two real commercial masters flagged "Bad" purely on
# this gate at +0.10dBTP/+0.12dBTP that the reporting user confirmed
# sound fine — well inside that uncertainty band, not evidence of a real
# defect. 0.6dBTP sits just outside the documented 0.554dB worst case,
# while staying two orders of magnitude below the +3.71dBTP/+5.0dBTP
# genuine-defect fixtures this gate was originally calibrated against —
# those margins are unaffected by this change.
TRUE_PEAK_THRESHOLD_DBTP = 0.6

# Degrees of phase-angle deviation from perfectly in-phase (180° = exact
# inversion) required before aphasemeter's own out-of-phase detector
# fires. Previously left implicit (ffmpeg's own default of 170°, from
# aphasemeter's `angle` option — a fairly narrow band near total
# inversion already); made explicit here so it's the same kind of named,
# overridable constant as every other gate threshold, not one silently
# inherited from ffmpeg's own defaults.
PHASE_OUT_OF_PHASE_ANGLE_DEG = 170.0


@dataclass
class Thresholds:
    """Every gate check's sensitivity, in one place, overridable per call.

    Defaults mirror the module-level constants above — those constants
    remain the single source of truth for the empirically-calibrated
    starting point; this dataclass exists so a caller (the Options page)
    can override any subset without touching module state, since
    analyze_file() may run concurrently on Picard's background-thread
    pool and mutable globals would race across files scanned at once.

    `spectral_silence_db` deliberately covers both the spectral-cutoff/
    transcode check and the fake-hi-res check — they're the same
    "no real content above a cutoff frequency" technique at two
    different frequencies, so one sensitivity knob covers both rather
    than asking the user to keep two numbers in sync.

    `dr14_shift` isn't a gate at all — it moves the DR14_* band
    boundaries below (see DR14_POOR_THRESHOLD etc.) that decide the
    Poor/Ok/Good/Great/Excellent gradient for gate-free files. Default
    0 matches the official TT DR Offline Meter manual's own documented
    scale; a listener whose typical material runs a few DR points
    lower (loudness-war-era masters) or higher than that scale's own
    anchor can shift it to match their own listening, without
    pretending this project has a better answer than the manual for
    where "compressed" starts.
    """

    clip_flat_factor: float = MIN_FLAT_FACTOR_FOR_CLIPPING
    true_peak_dbtp: float = TRUE_PEAK_THRESHOLD_DBTP
    spectral_silence_db: float = SPECTRAL_SILENCE_THRESHOLD_DB
    phase_angle_deg: float = PHASE_OUT_OF_PHASE_ANGLE_DEG
    dr14_shift: float = 0.0


# --- Track Health: weighted composite scoring ---
#
# Replaces the old OR-gate architecture (any one check firing forced the
# whole file to "Bad") — see HANDOFF.md's "The redesign" section for the
# full rationale (a single borderline True Peak reading, median only half
# a dB past its own threshold, was driving 58% of all "Bad" verdicts in a
# real 750-file sample). Every check below instead contributes a
# continuous 0..1 "defect score" (0 = comfortably clean, 1 = fails even
# the most lenient calibrated setting), weighted by how confidently that
# check's measurement maps to an actually-audible defect, then averaged.
#
# Each step table below is the same lenient->lenient-to-strict calibrated
# scale already used for the Options-page sensitivity sliders (see
# __init__.py's _SensitivitySlider) — reused here, not reinvented, so the
# composite score's granularity matches real, previously-validated
# threshold data rather than an arbitrary new curve. (value, hint) pairs,
# ordered lenient -> strict; only the values are used for scoring, the
# hints stay in __init__.py where the slider UI displays them.
CLIP_STEPS: tuple[float, ...] = (25.0, 20.0, 16.0, 8.0, 4.0, 2.0, 1.0, 0.5, 0.1, 0.05)
TRUE_PEAK_STEPS: tuple[float, ...] = (3.0, 2.0, 1.5, 1.0, 0.8, 0.6, 0.4, 0.2, 0.1, 0.0)
SPECTRAL_STEPS: tuple[float, ...] = (-90.0, -85.0, -75.0, -65.0, -60.0, -55.0, -50.0, -45.0, -35.0, -30.0)
PHASE_STEPS: tuple[float, ...] = (179.0, 177.0, 174.0, 170.0, 165.0, 158.0, 150.0, 140.0, 120.0, 95.0)


def _step_score(value: float, steps: tuple[float, ...], fails: Callable[[float, float], bool]) -> float:
    """Fraction of the calibrated lenient->strict steps this value would
    already fail, as a 0..1 defect score. Monotonic by construction (a
    stricter step is always at least as easy to fail as a more lenient
    one), so this is just "how far into the strict end of the scale does
    this value already sit" — 0.0 means it wouldn't even fail the
    strictest calibrated setting (genuinely clean), 1.0 means it fails
    even the most lenient one (unambiguous, severe defect). Reuses the
    same steps as the Options-page sliders rather than a separately
    invented curve, so a "0.7" score means the same thing here as it
    would sliding the matching slider 70% of the way to strict.
    """
    return sum(1 for step in steps if fails(value, step)) / len(steps)


def _dr14_score(dr14: int, dr14_shift: float) -> float:
    """DR14's contribution is a continuous gradient, not a step table:
    the official TT DR Offline Meter manual already documents a
    continuous red->green scale from DR8 (Poor) to DR14 (green), not a
    series of discrete calibrated gate thresholds the way the other
    checks have — linear interpolation between the shifted Poor/Great
    anchors matches that manual's own framing directly, clamped to 0..1
    outside the documented range.
    """
    poor = DR14_POOR_THRESHOLD - dr14_shift
    great = DR14_GREAT_THRESHOLD - dr14_shift
    if great <= poor:
        return 0.0
    return max(0.0, min(1.0, (great - dr14) / (great - poor)))


# Per-check weight in the Track Health composite — how confidently each
# check's measurement maps to an actually-audible defect, not how
# "serious" it sounds in isolation. Confirmed with the user: True Peak
# and Fake Hi-Res get a low weight rather than a separate non-gating
# bucket (median True Peak overs in a real 750-file sample sat just
# 0.5dB past threshold, inside the meter's own documented accuracy
# limit — see TRUE_PEAK_THRESHOLD_DBTP). Mains Hum is similarly
# downweighted: its own docstring already calls it "strong likelihood,
# not a definitive measurement" (can't fully rule out a real sustained
# musical drone at the same frequency). Clipping/Spectral Cutoff/
# Out-of-Phase/DR14 are direct, high-confidence measurements of the
# decoded signal and get full weight.
TRACK_HEALTH_WEIGHTS: dict[str, float] = {
    'clipping': 1.0,
    'spectral_cutoff': 1.0,
    'out_of_phase': 1.0,
    'dr14': 1.0,
    'true_peak': 0.3,
    'fake_hires': 0.3,
    'mains_hum': 0.5,
}


# Track Health tier boundaries on the 0..1 weighted composite score.
# Calibrated against a real 80-file random sample from the "master"
# corpus (see HANDOFF.md): scores ran continuously from 0.083 to
# 0.406 with no sharp natural gap, clustering around 0.29-0.30 (most
# ordinary loud modern masters share a similar True-Peak/Clipping
# profile) — so boundaries are placed by percentile, not a gap in the
# data. 0.35 (top ~6% of the sample) separated files with a real
# *compounding* pattern (True Peak overs + audible mains hum + a
# heavily compressed DR5-DR8 master, all at once) from the broad
# middle; 0.15 (bottom ~8%) separated the cleanest files (no hum, high
# DR, no true-peak overs). Yields Excellent 7.5% / Great 30% / Good
# 56% / Bad 6% on the calibration sample — Bad reserved for real,
# multi-factor degradation, not a single borderline reading, matching
# this redesign's whole reason for existing (see HANDOFF.md's "The
# redesign").
TRACK_HEALTH_BAD_THRESHOLD = 0.35
TRACK_HEALTH_GOOD_THRESHOLD = 0.25
TRACK_HEALTH_GREAT_THRESHOLD = 0.15


def track_tier_from_score(score: float) -> str:
    if score >= TRACK_HEALTH_BAD_THRESHOLD:
        return "Bad"
    if score >= TRACK_HEALTH_GOOD_THRESHOLD:
        return "Good"
    if score >= TRACK_HEALTH_GREAT_THRESHOLD:
        return "Great"
    return "Excellent"


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

# Mains hum: a sustained, narrow spectral tone at the local power-grid
# frequency (50Hz in most of the world, 60Hz in the Americas/parts of
# Asia) leaking into a recording via ground loops, unshielded cabling, or
# a bad power supply somewhere in the recording/transfer chain. A
# genuine musical note at the same frequency stops when the music does;
# hum doesn't — so this only looks inside ffmpeg's own silencedetect-
# identified quiet passages (where the *music* has dropped out), never
# the whole file, specifically to tell the two apart. Each mains
# frequency maps to a nearby "control" frequency with the same
# narrow-band width: real hum shows as an outsized peak at exactly its
# own frequency and nowhere nearby, while incidental musical content
# spreads energy more evenly across nearby frequencies.
HUM_FREQUENCIES_HZ = {50: 55, 60: 65}
HUM_BAND_WIDTH_HZ = 2
# Validated on synthetic fixtures with a real silent-vs-playing gap: a
# 50Hz tone persisting into an otherwise-silent passage measured ~14dB
# above its 55Hz control band; a genuine bass note at 50Hz that stopped
# along with the rest of the music (the normal case) left both bands at
# true silence in that passage — 8dB sits with real margin below the
# confirmed-hum case and above the confirmed-clean case (~0dB apart).
HUM_ELEVATION_THRESHOLD_DB = 8.0
# Below this narrowband RMS, there's nothing there to call hum regardless
# of the elevation ratio — avoids flagging noise-floor-level differences
# between two already-silent bands as a false "spike".
HUM_BAND_MIN_RMS_DB = -90.0
# "Quiet passage" here means the music itself has dropped out, not
# necessarily digital silence — real hum sits at an audible, non-trivial
# level. Confirmed empirically: a synthetic hum-only passage peaked at
# -26dBFS, well above what a strict silence threshold (e.g. -50dB) would
# recognize as silence; -25dB correctly caught it while still requiring a
# real, sustained drop from typical mixed-music loudness.
HUM_SILENCE_THRESHOLD_DB = -25.0
HUM_SILENCE_MIN_DURATION_SECONDS = 1.0

# Xing/Info VBR header "bytes" field: the byte-count of the MP3 audio
# stream (from the first frame through the last) the encoder itself
# recorded at encode time — used by players/seekers to map a seek
# percentage to a byte offset without a full duration scan. Comparing
# this declared count against the real on-disk audio-stream size (file
# size minus any ID3v2/ID3v1 wrapper) is a genuine structural fact
# about the container, independent of *why* they disagree — could be
# extra data appended after the original stream ended (concatenation),
# data missing from the end (truncation), or the encoder itself writing
# a wrong value. This module deliberately does not try to diagnose
# which — see _detect_size_mismatch's docstring for why that
# distinction isn't reliably recoverable from the header disagreement
# alone, unlike the (rejected) declared-vs-decoded-duration truncation
# heuristic this deliberately avoids repeating: that one compared
# *duration*, derived either from this same unreliable header or from a
# full decode, giving no independent signal to check either against.
# This instead compares the header's own field directly against
# `os.path.getsize()` — a fact no decode or duration math can get wrong.
#
# Ffmpeg's own mp3 demuxer (libavformat/mp3dec.c) does an equivalent
# comparison to log "invalid concatenated file detected"/"filesize and
# duration do not match" internally, but was confirmed empirically (see
# HANDOFF) to not reliably surface in this module's own invocations,
# and its raw log line conflates real container inconsistency with a
# separate false-positive mode this module already found and rejected
# in the truncation-check investigation. Reimplemented directly against
# mutagen's own Xing header parser (already a dependency, see
# _read_lame_header) instead of relying on that log line, both for
# reliability and to compute the real byte-level ratio rather than just
# a fired/not-fired boolean.
#
# Threshold calibrated against a real 500-file random sample of the
# user's library (see HANDOFF): 370 files carried a usable Xing "bytes"
# field; 366 landed at <=0.43% mismatch (floating-point/frame-rounding
# noise around the encoder's own byte accounting), then a clean gap to
# 4 real outliers at 21%-182%. Three of those four also independently
# showed CORRUPTION_BITS_LEFT_PATTERN hits (1, 1, and 181 occurrences)
# in the very same files — corroborating evidence this is catching a
# real structural anomaly, not encoder noise. 5% sits with >10x margin
# above the noise ceiling and well below every confirmed-real case.
SIZE_MISMATCH_RATIO_THRESHOLD = 0.05

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


def _run_ffmpeg_filter(
    ffmpeg: str,
    filename: str,
    filter_str: str,
    start: float | None = None,
    duration: float | None = None,
) -> tuple[str, int]:
    """Runs ffmpeg with a given audio filter, discarding output.

    Returns (stderr, returncode) — ffmpeg's analysis filters (astats,
    volumedetect, loudnorm) print their results to stderr as a side
    effect of decoding, the standard way to use them for measurement
    rather than transformation. Callers that can tolerate "found
    nothing" (every filter after the first) can ignore the returncode;
    it exists so the one call that can't (the first astats pass) can
    tell "ffmpeg ran and found nothing" apart from "ffmpeg couldn't
    process this file at all". `start`/`duration` scope the analysis to
    a specific window (input-side `-ss`/`-t`, before `-i` for fast+
    accurate seeking) — used by hum detection to look only inside a
    known quiet passage rather than the whole file.
    """
    args = [ffmpeg, '-nostdin', '-hide_banner']
    if start is not None:
        args += ['-ss', str(start)]
    if duration is not None:
        args += ['-t', str(duration)]
    args += ['-i', filename, '-af', filter_str, '-f', 'null', '-']
    proc = _run_subprocess(args)
    return proc.stderr, proc.returncode


def _filter_instance_output(stderr: str, tag: str) -> str:
    """Slices one named filter instance's lines back out of a merged
    multi-branch invocation's combined stderr (see _run_merged_analysis).

    ffmpeg tags every log line a `filtername@tag` instance prints with a
    stable `[filtername@tag @ 0xADDR]` prefix (confirmed empirically
    against a real multi-branch `asplit` filtergraph) — letting each
    existing single-filter parser (_parse_astats, _parse_mean_volume,
    ...) run unmodified against just its own branch's output, so two
    filters of the same type in one invocation (astats appears at most
    once here; volumedetect can appear twice, for the ordinary
    spectral-cutoff check and the fake-hi-res check) never contaminate
    each other's "last match wins" parsing. loudnorm is the one
    exception: it prints its tag once, then a raw untagged JSON blob —
    but only one loudnorm branch ever exists and its JSON shape is
    already unambiguous in the full combined stderr, so _parse_loudnorm
    is called against the whole string directly rather than through
    this slice.
    """
    marker = f'@{tag} @ '
    return '\n'.join(line for line in stderr.splitlines() if marker in line)


def _run_merged_analysis(
    ffmpeg: str,
    filename: str,
    include_phase_check: bool,
    include_hires_check: bool,
    phase_angle_deg: float,
    sample_rate: int | None,
) -> tuple[str, str, int]:
    """One ffmpeg invocation, one decode, for every whole-file measurement
    that doesn't need another measurement's result first: clipping/peak
    stats (astats), spectral-cutoff/transcode detection (highpass +
    volumedetect), true peak/LUFS (loudnorm), the multi-point effective-
    bandwidth sweep (see BANDWIDTH_PROBE_FREQUENCIES_HZ), and — when
    applicable — phase/mono-duplication (aphasemeter) and fake-hi-res
    spectral content (a second highpass + volumedetect at a higher
    cutoff). `asplit` feeds the same decoded audio into independent named
    filter chains; see _filter_instance_output() for how each branch's
    output gets pulled back out of the combined stderr. Replaces what
    used to be 3-5 separate ffmpeg process spawns (and file decodes) with
    exactly one.

    `include_phase_check`/`include_hires_check` decide which optional
    branches are even present in the graph — decided from ffprobe
    metadata (channel count, sample rate) the caller already has before
    this runs, not from anything this invocation measures itself. This
    matters for phase checking specifically: feeding aphasemeter a truly
    mono file doesn't error, but it does print a bare `mono_start: 0`
    line, and _parse_phasemeter's check is a value-blind substring test
    — so the branch must be structurally absent for mono files, not just
    ignored after the fact, or a mono file would falsely score "mono
    content duplicated into stereo". DR14 stays a separate, later pass
    (see _measure_dr14): it's skipped outright once an upstream gate has
    fired, a real perf saving that folding it into this always-run graph
    would give up.

    `sample_rate` bounds the bandwidth-sweep probe list to frequencies
    genuinely below this file's own Nyquist — confirmed empirically that
    ffmpeg accepts an out-of-range `highpass` cutoff without erroring,
    logs "Invalid frequency and/or width!", and then silently passes the
    branch's audio through unfiltered, which would misread as "full
    bandwidth" for any file whose real sample rate is below 44.1kHz (a
    real 32kHz-mono MP3 test fixture caught this live). A 500Hz margin
    below Nyquist keeps the topmost probe out of a resampler's own
    transition band.

    `-err_detect compliant` is set globally on the decoder — it costs
    nothing extra (same one decode) and makes ffmpeg's MP3 decoder log a
    "bits_left" diagnostic on frames with a genuinely invalid bit-
    reservoir pointer, which `_detect_corruption_signature` below reads
    straight out of this call's stderr (see CORRUPTION_* comment for why).

    Returns (stdout, stderr, returncode) for the whole invocation — a
    non-zero returncode means ffmpeg couldn't decode the file at all
    (every branch depends on the same decode succeeding), same meaning
    the first astats-only pass's returncode used to carry. stdout only
    ever carries the phase-check branch's per-frame
    `lavfi.aphasemeter.phase` metadata (via `ametadata=print:file=-`,
    routed there specifically so it never collides with any other
    branch's stderr output — confirmed empirically clean against the
    full multi-branch graph, not just the aphasemeter branch alone);
    empty when the phase branch isn't included.
    """
    branches: list[tuple[str | None, str]] = [
        ('main', 'astats@main'),
        ('cutoff', f'highpass=f={SPECTRAL_CUTOFF_FREQUENCY_HZ},volumedetect@cutoff'),
        (None, 'loudnorm=print_format=json'),
    ]
    if include_phase_check:
        branches.append(
            (None, f'aphasemeter=video=0:phasing=1:angle={phase_angle_deg},ametadata=print:file=-')
        )
    if include_hires_check:
        branches.append(('hires', f'highpass=f={FAKE_HIRES_CHECK_FREQUENCY_HZ},volumedetect@hires'))
    if sample_rate:
        nyquist_margin = sample_rate / 2 - 500
        for freq in BANDWIDTH_PROBE_FREQUENCIES_HZ:
            if freq < nyquist_margin:
                branches.append((f'bw{freq}', f'highpass=f={freq},volumedetect@bw{freq}'))

    split_labels = [f's{i}' for i in range(len(branches))]
    graph = [f"[0:a]asplit={len(branches)}" + ''.join(f'[{label}]' for label in split_labels)]
    out_labels = []
    for i, (_tag, chain) in enumerate(branches):
        out_label = f'o{i}'
        graph.append(f'[{split_labels[i]}]{chain}[{out_label}]')
        out_labels.append(out_label)

    args = [
        ffmpeg, '-nostdin', '-hide_banner', '-err_detect', 'compliant',
        '-i', filename, '-filter_complex', ';'.join(graph),
    ]
    for out_label in out_labels:
        args += ['-map', f'[{out_label}]', '-f', 'null', '-']
    proc = _run_subprocess(args)
    return proc.stdout, proc.stderr, proc.returncode


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


def _detect_corruption_signature(merged_stderr: str) -> str | None:
    """Informational only — never gates or affects the tier, the same
    tier of certainty as hum detection: a real decoder-level signal, not
    proof audible to a listener in every case (see the module-level
    comment above CORRUPTION_BITS_LEFT_PATTERN for how this was
    validated and why the ratio-based approach it replaces was dropped).

    Counts ffmpeg's own "bits_left" diagnostic in the merged decode's
    stderr — not scoped to any one named branch, since this is a
    decoder-level message, not something any particular filter emits.
    Requires `-err_detect compliant` on that decode (set in
    _run_merged_analysis) to be present at all.
    """
    count = len(CORRUPTION_BITS_LEFT_PATTERN.findall(merged_stderr))
    if count == 0:
        return None
    return (
        f"Possible data corruption — {count} decoded audio block"
        f"{'s' if count != 1 else ''} had an internally inconsistent bit-reservoir "
        "pointer (ffmpeg's own decoder-level sanity check), consistent with a "
        "scattered bit error rather than a normal encoding quirk"
    )


def _parse_mean_volume(stderr: str) -> float | None:
    m = re.search(r'mean_volume:\s*([\-\d.]+)\s*dB', stderr)
    return float(m.group(1)) if m else None


def _measure_bandwidth(merged_stderr: str, sample_rate: int | None, peak_db: float | None) -> float | None:
    """Highest bandwidth-sweep probe frequency whose measured level is
    still within BANDWIDTH_PEAK_RELATIVE_DB of the track's own peak —
    the effective-bandwidth estimate. Peak-relative rather than
    referenced against an absolute silence floor: a track's own peak
    level already accounts for how loud or quiet it is overall, so this
    needs no separate near-silence guard the way the absolute-threshold
    single-point check does.

    Returns None if there's nothing to measure (no sample rate, no peak)
    or if even the lowest probe frequency is already below the
    threshold — the file's real content doesn't reach the swept range at
    all, i.e. an aggressive cutoff below BANDWIDTH_PROBE_FREQUENCIES_HZ's
    own floor.
    """
    if not sample_rate or peak_db is None:
        return None
    threshold = peak_db - BANDWIDTH_PEAK_RELATIVE_DB
    nyquist_margin = sample_rate / 2 - 500
    highest: float | None = None
    for freq in BANDWIDTH_PROBE_FREQUENCIES_HZ:
        if freq >= nyquist_margin:
            break
        mean_volume = _parse_mean_volume(_filter_instance_output(merged_stderr, f'bw{freq}'))
        if mean_volume is not None and mean_volume > threshold:
            highest = float(freq)
    return highest


_PHASE_METADATA_RE = re.compile(r'lavfi\.aphasemeter\.phase=(-?[\d.]+)')


def _measure_stereo_coherence(merged_stdout: str) -> float | None:
    """Mean of aphasemeter's own per-frame phase-correlation value
    (`lavfi.aphasemeter.phase`, range [-1, 1]: 1 = perfectly in phase,
    -1 = fully inverted) across the whole track — a continuous,
    cause-agnostic stereo-coherence estimate for the compare panel's
    ranking, distinct from the binary out-of-phase gate above (which
    only fires on a sustained near-total inversion). A deliberately
    mixed stereo master tends to hold a high, stable positive mean; bad
    down/up-mixing, heavy decorrelation, or certain transcoding
    artifacts pull it down — without needing to know which. None when
    the phase branch wasn't run (mono source, or the value couldn't be
    parsed at all) rather than a misleading 0.0.
    """
    values = [float(m.group(1)) for m in _PHASE_METADATA_RE.finditer(merged_stdout)]
    if not values:
        return None
    return sum(values) / len(values)


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
class SizeMismatch:
    direction: str  # 'extra_data' (declared < actual) or 'missing_data' (declared > actual)
    ratio: float
    declared_bytes: int
    actual_bytes: int


def _id3v2_size(fh) -> int:
    """Bytes occupied by a leading ID3v2 tag, 0 if none present. Reads the
    synchsafe size field directly rather than via mutagen's ID3 class —
    only the byte count is needed here, not a parsed tag."""
    fh.seek(0)
    header = fh.read(10)
    if len(header) < 10 or header[:3] != b'ID3':
        return 0
    size = ((header[6] & 0x7F) << 21) | ((header[7] & 0x7F) << 14) | ((header[8] & 0x7F) << 7) | (header[9] & 0x7F)
    return 10 + size


def _id3v1_size(fh) -> int:
    """128 bytes for a trailing ID3v1 tag, 0 if none present."""
    fh.seek(0, os.SEEK_END)
    if fh.tell() < 128:
        return 0
    fh.seek(-128, os.SEEK_END)
    return 128 if fh.read(3) == b'TAG' else 0


def _detect_size_mismatch(filename: str) -> SizeMismatch | None:
    """Compares the Xing/Info header's own declared audio-stream byte
    count against the real on-disk audio-stream size (file size minus
    any ID3v2/ID3v1 wrapper) — see SIZE_MISMATCH_RATIO_THRESHOLD for the
    real-data calibration behind the threshold and why this deliberately
    doesn't try to label the cause (concatenation vs. truncation vs. a
    bad encoder) as anything more specific than direction + magnitude.

    None means "nothing to compare" — no Xing/Info header present (many
    valid MP3s have none, e.g. real CBR encodes), a non-MP3/non-layer-3
    frame, or an unreadable/corrupt header. Absence is not itself a
    defect; a corrupt header is more useful reported by the separate
    audio-corruption check, which reads real decode-time bitstream
    state rather than this file's static byte counts.
    """
    try:
        actual_bytes = os.path.getsize(filename)
        with open(filename, 'rb') as fh:
            id3v2 = _id3v2_size(fh)
            id3v1 = _id3v1_size(fh)
            fh.seek(0)
            skip_id3(fh)
            frame = MPEGFrame(fh)
            if frame.layer != 3:
                return None
            offset = XingHeader.get_offset(frame)
            fh.seek(frame.frame_offset + offset, 0)
            xing = XingHeader(fh)
    except (HeaderNotFoundError, XingHeaderError, OSError):
        return None
    if xing.bytes is None or xing.bytes <= 0:
        return None
    declared = xing.bytes
    audio_actual = actual_bytes - id3v2 - id3v1
    smaller = min(declared, audio_actual)
    if smaller <= 0:
        return None
    ratio = abs(declared - audio_actual) / smaller
    if ratio <= SIZE_MISMATCH_RATIO_THRESHOLD:
        return None
    direction = 'extra_data' if declared < audio_actual else 'missing_data'
    return SizeMismatch(direction=direction, ratio=ratio, declared_bytes=declared, actual_bytes=audio_actual)


def _detect_artwork_corruption(ffmpeg: str, ffprobe: str, filename: str) -> bool:
    """True if the file has an embedded artwork/attached-pic stream and
    ffmpeg's own decoder can't decode its first frame.

    Confirmed empirically that the real analyze_file pipeline (explicit
    `-filter_complex` + explicit `-map` for audio-only outputs, see
    _run_merged_analysis) never touches any video/attached-pic stream —
    this is a genuinely new code path, not something the existing audio
    checks already exercise incidentally.

    Validated against engineered fixtures: a JPEG with ~5% of its scan
    data zeroed out, or truncated to 20% of its original length but
    still containing its SOF/DQT/SOS header segments, decodes cleanly
    (ffmpeg's mjpeg decoder tolerates isolated bit errors and missing
    trailing data, emitting a garbled but "successfully decoded" frame
    with only a warning) — same class of limitation as the audio
    bit-reservoir check (`bits_left`): catches structural corruption,
    not every possible pixel-level defect. Truncating past the header
    segments entirely (missing SOF/quantization tables) reliably
    produces a real decoder error and non-zero exit code.
    """
    probe = _run_subprocess(
        [
            ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=index', '-of', 'csv=p=0', filename,
        ]
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return False
    decode = _run_subprocess(
        [
            ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-i', filename,
            '-map', '0:v:0', '-frames:v', '1', '-f', 'null', '-',
        ]
    )
    return decode.returncode != 0


def _detect_tag_structure_error(filename: str) -> str | None:
    """Parses every tag frame/block via mutagen's own generic
    `mutagen.File()` (format-detecting, used across all of Picard core's
    own tag reading) and returns a short description of the first
    exception raised while doing so, or None if tags parsed cleanly (or
    the file has no tags at all — absence is not itself a defect).

    A tag structure malformed enough to raise here is a genuine
    container-level defect independent of anything the audio-stream
    decode already checks: mutagen parses ID3/Vorbis-comment/APEv2/MP4
    atom structure directly, a different code path than ffmpeg's own
    (more lenient) container/tag reading.

    Validated against a real 100-file M4A sample plus this repo's own
    fixtures: 0 false positives, but one real fixture (a 3 Doors Down
    M4A) confirmed a genuine defect — its first `----` freeform atom
    goes straight from the atom header to a payload-less `data`
    sub-atom, skipping the `mean`/`name` sub-atoms the freeform-atom
    spec requires to identify which custom tag it is. ffmpeg's own MP4
    tag reader silently tolerates the malformed atom (every other tag
    on the file reads fine via ffprobe); mutagen's stricter parser
    correctly can't, which is exactly the kind of container-level
    defect this check exists to surface.
    """
    try:
        mutagen.File(filename)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        return f"{type(exc).__name__}: {exc}"
    return None


@dataclass
class StreamInfo:
    sample_rate: int | None
    codec_name: str | None
    profile: str | None
    bitrate_kbps: int | None
    channels: int | None


def _probe_stream_info(ffprobe: str, filename: str) -> StreamInfo:
    """Single ffprobe call for everything analyze_file needs from stream
    metadata: sample rate (DR14 block sizing), channel count (decides
    upfront, before any ffmpeg filter pass runs, whether the merged
    analysis graph's phase-check branch is worth including at all — see
    _run_merged_analysis), and codec/profile/bitrate (transparency
    scoring) — one call rather than one per use, since ffprobe reads
    container/stream headers only, not the full audio, so there's no
    reason to invoke it twice. Any failure (unparseable output, no audio
    stream found) yields an all-None StreamInfo; every caller already
    treats missing fields as "can't measure this", consistent with the
    rest of the module's graceful-degradation style.
    """
    proc = _run_subprocess(
        [
            ffprobe, '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name,profile,sample_rate,channels,bit_rate:format=bit_rate',
            '-of', 'json', filename,
        ]
    )
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return StreamInfo(None, None, None, None, None)
    streams = data.get('streams') or []
    if not streams:
        return StreamInfo(None, None, None, None, None)
    stream = streams[0]
    try:
        sample_rate = int(stream['sample_rate'])
    except (KeyError, ValueError, TypeError):
        sample_rate = None
    try:
        channels = int(stream['channels'])
    except (KeyError, ValueError, TypeError):
        channels = None
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
        channels=channels,
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


_SILENCE_START_RE = re.compile(r'silence_start:\s*([\-\d.]+)')
_SILENCE_END_RE = re.compile(r'silence_end:\s*([\-\d.]+)')


def _find_quiet_interval(ffmpeg: str, filename: str) -> tuple[float, float] | None:
    """Longest quiet interval per ffmpeg's own silencedetect, or None if
    none at least HUM_SILENCE_MIN_DURATION_SECONDS long was found — most
    loud modern masters never qualify, correctly: there's no quiet
    passage to check hum against, not "no hum".
    """
    stderr, _returncode = _run_ffmpeg_filter(
        ffmpeg, filename,
        f'silencedetect=noise={HUM_SILENCE_THRESHOLD_DB}dB:d={HUM_SILENCE_MIN_DURATION_SECONDS}',
    )
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(stderr)]
    intervals = list(zip(starts, ends))
    if not intervals:
        return None
    start, end = max(intervals, key=lambda iv: iv[1] - iv[0])
    return start, end - start


def _measure_narrowband_rms(
    ffmpeg: str, filename: str, frequency: int, start: float, duration: float
) -> float | None:
    stderr, _returncode = _run_ffmpeg_filter(
        ffmpeg, filename,
        f'bandpass=f={frequency}:width_type=h:w={HUM_BAND_WIDTH_HZ},astats',
        start=start, duration=duration,
    )
    m = re.search(r'RMS level dB:\s*(-?[\d.]+|-inf)', stderr)
    if not m:
        return None
    raw = m.group(1)
    return -200.0 if raw == '-inf' else float(raw)


def _measure_noise_floor(
    ffmpeg: str, filename: str, quiet_interval: tuple[float, float] | None
) -> float | None:
    """Broadband RMS level (dB) in the track's own longest quiet passage —
    an informational, cause-agnostic noise-floor estimate for the compare
    panel's ranking, not a defect gate. A clean digital master's quiet
    passages sit at a very low RMS (limited by dither/quantization); an
    elevated noise floor there reflects real energy the mastering chain
    left behind — mic self-noise, tape hiss, broadcast static, whatever
    the cause — without needing to identify which. Same scoped-window
    reasoning as hum detection (see _detect_hum): only the *quiet*
    portion is measured, never the whole track, so genuinely loud modern
    masters that never drop below HUM_SILENCE_THRESHOLD_DB simply have
    nothing to report here rather than a misleading "noise floor" pulled
    from a passage that was never actually quiet.
    """
    if quiet_interval is None:
        return None
    start, duration = quiet_interval
    stderr, _returncode = _run_ffmpeg_filter(ffmpeg, filename, 'astats', start=start, duration=duration)
    m = re.search(r'RMS level dB:\s*(-?[\d.]+|-inf)', stderr)
    if not m:
        return None
    raw = m.group(1)
    return -200.0 if raw == '-inf' else float(raw)


def _detect_hum(ffmpeg: str, filename: str, quiet_interval: tuple[float, float] | None) -> str | None:
    """Informational only — never gates or affects the tier. Even scoped
    to a quiet passage, this can't be told apart with full certainty from
    a sustained musical drone/pedal note at exactly the same frequency
    that happens to ring into an otherwise-quiet moment — strong
    likelihood, not a definitive measurement the way clipping/cutoff/
    true-peak/fake-hi-res are (see HUM_* constants for the validation
    that motivates the specific thresholds). Takes the quiet interval as
    a parameter (shared with _measure_noise_floor) rather than finding it
    again — one silencedetect pass serves both.
    """
    if quiet_interval is None:
        return None
    start, duration = quiet_interval
    for mains_hz, control_hz in HUM_FREQUENCIES_HZ.items():
        hum_rms = _measure_narrowband_rms(ffmpeg, filename, mains_hz, start, duration)
        control_rms = _measure_narrowband_rms(ffmpeg, filename, control_hz, start, duration)
        if hum_rms is None or control_rms is None or hum_rms < HUM_BAND_MIN_RMS_DB:
            continue
        elevation = hum_rms - control_rms
        if elevation >= HUM_ELEVATION_THRESHOLD_DB:
            return (
                f"Possible mains hum at {mains_hz}Hz — {elevation:.0f}dB above the "
                f"surrounding spectrum during a {duration:.1f}s quiet passage, "
                "persisting where the music itself has dropped out"
            )
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


# Spectrogram image size — matches the compare panel's own dialog sizing;
# see __init__.py's SpectrogramDialog. showspectrumpic adds fixed chrome
# around the requested size for axis labels/legend/colorbar (confirmed
# empirically: ~282px width, ~128px height, regardless of the requested
# size) — sized so the actual rendered PNG (this size plus that chrome)
# fits inside SpectrogramDialog's window without a scrollbar.
SPECTROGRAM_WIDTH = 550
SPECTROGRAM_HEIGHT = 350


def generate_spectrogram(
    filename: str, output_path: str, ffmpeg_path: str | None = None
) -> bool:
    """Renders a log-frequency spectrogram PNG for `filename` to
    `output_path` via ffmpeg's own showspectrumpic filter — on demand
    only (a "Spectrogram" button in the compare panel), never part of
    the regular scan pass: this is a real extra decode + image-render
    cost per call, distinct from every other measurement in this module
    which reuses the one merged decode.

    Doesn't compute or judge anything itself — the point is letting the
    user see the same evidence the numeric checks above already
    measured (a cutoff wall, an elevated noise floor, asymmetric
    stereo content) in one glance, the same way this project's own
    research turned up as standing advice from comparable tools: "always
    confirm a flagged file by eyeballing its spectrogram" rather than
    trusting a single number. `-update 1` (write the same file path
    every frame, keep only the last) is required by ffmpeg's image2
    muxer for a single-image PNG output; confirmed empirically that
    -frames:v 1 alone works too but leaves ffmpeg's own advisory warning
    in stderr about needing this flag.
    """
    ffmpeg = find_ffmpeg(ffmpeg_path)
    args = [
        ffmpeg, '-nostdin', '-hide_banner', '-y', '-i', filename,
        '-lavfi', f'showspectrumpic=s={SPECTROGRAM_WIDTH}x{SPECTROGRAM_HEIGHT}:mode=combined:legend=1:scale=log',
        '-update', '1', output_path,
    ]
    proc = _run_subprocess(args)
    return proc.returncode == 0 and os.path.exists(output_path)


def _tunable(slider_name: str) -> str:
    """Appended to a gate issue's message so a plain-language reason
    always points at the exact Options-page control that governs it —
    never a defect reported with no way to know it's adjustable at all.
    Plain text, not markup: this string ends up both in the compare
    panel's tooltip and in the `_health_flags` script variable, so it
    has to read fine in either.
    """
    return f' (Adjust in Options \u2192 File Health: "{slider_name}" slider)'


def _run_corruption_decode(ffmpeg: str, filename: str) -> tuple[str, int]:
    """Minimal ffmpeg decode for File Health's own purposes only: checks
    decodability (returncode) and surfaces `bits_left` diagnostics (see
    CORRUPTION_BITS_LEFT_PATTERN) via `-err_detect compliant`, with no
    filter graph at all. File Health doesn't need astats/loudnorm/
    highpass/aphasemeter — those are Track Health concerns with their
    own separate (heavier) decode in analyze_track_health. Kept
    deliberately independent so a File-Health-only scan (bulk/auto-scan
    use, or the left-pane unmatched-file view) never pays for
    perceptual measurements it doesn't use.

    `-map 0:a` is required, not optional: without an explicit map,
    ffmpeg's default stream selection also decodes an embedded
    attached-pic/video stream if present, so a file with genuinely
    fine audio but a separately-corrupt embedded image (see
    _detect_artwork_corruption, which already covers exactly this
    defect on its own terms) would otherwise fail this decode and be
    misreported as `Broken` — confirmed empirically on a real fixture
    (Chevelle - The Red.mp3, PNG-tagged artwork that's actually JPEG
    bytes): the bare `-i ... -f null -` invocation exits 69, `-map 0:a`
    exits 0 with the same file's genuinely fine audio.
    """
    proc = _run_subprocess(
        [ffmpeg, '-nostdin', '-hide_banner', '-err_detect', 'compliant', '-i', filename, '-map', '0:a', '-f', 'null', '-']
    )
    return proc.stderr, proc.returncode


FILE_TIER_BROKEN = "Broken"
FILE_TIER_BAD = "Bad"
FILE_TIER_GOOD = "Good"
FILE_TIER_GREAT = "Great"
FILE_TIER_EXCELLENT = "Excellent"


def _compute_file_tier(file_issues: list[str], stream_info: StreamInfo) -> str:
    """File Health has no sliders — see HANDOFF.md's "The redesign"
    section — so this mapping is fixed rather than threshold-tunable:
    any structural defect (audio/non-audio corruption, a malformed
    tag/artwork structure, a Xing-header size mismatch) is `Bad`
    regardless of bitrate; a structurally sound file is graded purely
    by codec/bitrate using the same HydrogenAudio/Xiph transparency
    consensus already validated for `_bitrate_transparency_note`.
    """
    if file_issues:
        return FILE_TIER_BAD
    codec_name = (stream_info.codec_name or '').lower()
    if _is_lossless_codec(codec_name):
        return FILE_TIER_EXCELLENT
    threshold = TRANSPARENT_BITRATE_KBPS.get(codec_name)
    is_he_aac = codec_name == 'aac' and stream_info.profile and 'he-aac' in stream_info.profile.lower()
    if is_he_aac or threshold is None or stream_info.bitrate_kbps is None:
        # No published transparency threshold to grade against (unknown
        # codec, missing bitrate data, or HE-AAC's SBR extension being
        # transparent at bitrates far below the plain-AAC-LC threshold —
        # see _bitrate_transparency_note) — can't confidently call this
        # "Great", but there's no structural defect either.
        return FILE_TIER_GOOD
    return FILE_TIER_GREAT if stream_info.bitrate_kbps >= threshold else FILE_TIER_GOOD


_LOSSLESS_CODECS = {'flac', 'alac', 'wavpack', 'tta', 'ape'}


def _is_lossless_codec(codec: str) -> bool:
    return codec in _LOSSLESS_CODECS or codec.startswith('pcm_')


@dataclass
class TrackHealthInputs:
    """Every Track Health check's raw value, decoupled from
    AnalysisResult so compute_track_score is independently testable.
    None means "not applicable/not measured" — excluded from the
    composite rather than treated as a clean 0.0, so an unmeasured
    check can't silently pull a score toward "better than it is".
    """
    flat_factor: float | None
    true_peak_dbtp: float | None
    has_signal: bool
    spectral_energy_above_cutoff_db: float | None
    is_hires: bool
    spectral_energy_above_hires_cutoff_db: float | None
    is_out_of_phase: bool | None
    dr14: int | None
    has_mains_hum: bool | None


def compute_track_score(inputs: TrackHealthInputs, thresholds: Thresholds) -> float | None:
    """Weighted-average composite in 0..1 (0 = clean, 1 = fails every
    applicable check at its most lenient calibrated setting) — see the
    "Track Health: weighted composite scoring" section above for the
    full design rationale. Returns None only when literally nothing
    could be measured (in practice this shouldn't happen for any file
    that reached this point — clipping/true-peak are always measured
    off the very first decode pass — but a caller has to handle "no
    Track Health tier" for a File Health `Broken` result regardless).
    """
    contributions: list[tuple[float, float]] = []
    if inputs.flat_factor is not None:
        score = _step_score(inputs.flat_factor, CLIP_STEPS, lambda v, t: v > t)
        contributions.append((score, TRACK_HEALTH_WEIGHTS['clipping']))
    if inputs.true_peak_dbtp is not None:
        score = _step_score(inputs.true_peak_dbtp, TRUE_PEAK_STEPS, lambda v, t: v >= t)
        contributions.append((score, TRACK_HEALTH_WEIGHTS['true_peak']))
    if inputs.has_signal and inputs.spectral_energy_above_cutoff_db is not None:
        score = _step_score(inputs.spectral_energy_above_cutoff_db, SPECTRAL_STEPS, lambda v, t: v < t)
        contributions.append((score, TRACK_HEALTH_WEIGHTS['spectral_cutoff']))
    if inputs.is_hires and inputs.has_signal and inputs.spectral_energy_above_hires_cutoff_db is not None:
        score = _step_score(inputs.spectral_energy_above_hires_cutoff_db, SPECTRAL_STEPS, lambda v, t: v < t)
        contributions.append((score, TRACK_HEALTH_WEIGHTS['fake_hires']))
    if inputs.is_out_of_phase is not None:
        contributions.append((1.0 if inputs.is_out_of_phase else 0.0, TRACK_HEALTH_WEIGHTS['out_of_phase']))
    if inputs.dr14 is not None:
        contributions.append((_dr14_score(inputs.dr14, thresholds.dr14_shift), TRACK_HEALTH_WEIGHTS['dr14']))
    if inputs.has_mains_hum is not None:
        contributions.append((1.0 if inputs.has_mains_hum else 0.0, TRACK_HEALTH_WEIGHTS['mains_hum']))
    if not contributions:
        return None
    total_weight = sum(w for _, w in contributions)
    return sum(s * w for s, w in contributions) / total_weight


@dataclass
class FileHealthResult:
    file_tier: str
    file_issues: list[str]
    info: list[str]
    content_hash: str
    stream_info: StreamInfo


def _broken_file_health_result(filename: str, reason: str) -> FileHealthResult:
    """A file ffmpeg can't decode at all is a real, persisted File Health
    fact (`Broken`, terminal), not a transient scan error. Only
    `content_hash` can still be computed (it hashes raw bytes, no decode
    needed) — everything else genuinely has nothing to report.
    """
    return FileHealthResult(
        file_tier=FILE_TIER_BROKEN,
        file_issues=[reason],
        info=[],
        content_hash=content_hash(filename),
        stream_info=StreamInfo(None, None, None, None, None),
    )


def analyze_file_health(filename: str, ffmpeg_path: str | None = None) -> FileHealthResult:
    """Runs on a background thread. Structural-only, no sliders (see
    HANDOFF.md's "The redesign") — deliberately independent of
    analyze_track_health's own (heavier) decode; each scan can run
    without the other's cost, per the user's own request to keep File
    Health and Track Health as fully separate scans, not two facets of
    one combined pass. Every check here answers "is this file
    structurally sound", not "does it sound good": decode success,
    `bits_left` bit-reservoir corruption, non-audio corruption (embedded
    artwork/tag structure), a Xing-header size mismatch, and
    codec/bitrate transparency grading.

    `FfmpegNotFoundError`/`FfmpegVersionTooOldError` still raise — an
    environment problem unrelated to any one file, not a per-file
    verdict — but a decode failure on this specific file is a real
    persisted `file_tier="Broken"` result (see _broken_file_health_result),
    not an exception.
    """
    ffmpeg = find_ffmpeg(ffmpeg_path)
    ffprobe = _find_ffprobe(ffmpeg)
    check_ffmpeg_version(ffmpeg)
    stream_info = _probe_stream_info(ffprobe, filename)

    stderr, returncode = _run_corruption_decode(ffmpeg, filename)
    if returncode != 0:
        return _broken_file_health_result(
            filename,
            f"ffmpeg couldn't decode {os.path.basename(filename)} as audio "
            "(corrupt, truncated, unsupported format, or a decode timeout)",
        )

    file_issues: list[str] = []
    info: list[str] = []

    bitrate_note = _bitrate_transparency_note(stream_info)
    if bitrate_note is not None:
        info.append(bitrate_note)

    corruption_note = _detect_corruption_signature(stderr)
    if corruption_note is not None:
        # A real decoder-level structural signal (see CORRUPTION_BITS_
        # LEFT_PATTERN) — presence (not count, see that constant's own
        # calibration note) is the validated signal.
        file_issues.append(corruption_note)

    size_mismatch = _detect_size_mismatch(filename)
    if size_mismatch is not None:
        declared_mb = size_mismatch.declared_bytes / 1_000_000
        actual_mb = size_mismatch.actual_bytes / 1_000_000
        if size_mismatch.direction == 'extra_data':
            file_issues.append(
                f"File has {size_mismatch.ratio * 100:.0f}% more audio data than its own VBR header "
                f"accounts for ({actual_mb:.1f}MB on disk vs. {declared_mb:.1f}MB declared) — "
                "consistent with extra data appended after the original track ended"
            )
        else:
            file_issues.append(
                f"File has {size_mismatch.ratio * 100:.0f}% less audio data than its own VBR header "
                f"declares ({actual_mb:.1f}MB on disk vs. {declared_mb:.1f}MB declared) — "
                "consistent with the file being truncated or rewritten after encoding"
            )

    if _detect_artwork_corruption(ffmpeg, ffprobe, filename):
        file_issues.append("Embedded artwork is corrupt — ffmpeg's own image decoder can't decode it")

    tag_error = _detect_tag_structure_error(filename)
    if tag_error is not None:
        file_issues.append(f"Malformed tag structure — {tag_error}")

    file_tier = _compute_file_tier(file_issues, stream_info)
    return FileHealthResult(
        file_tier=file_tier,
        file_issues=file_issues,
        info=info,
        content_hash=content_hash(filename),
        stream_info=stream_info,
    )


@dataclass
class TrackHealthResult:
    track_tier: str | None
    track_issues: list[str]
    info: list[str]
    track_score: float | None
    content_hash: str
    lufs: float | None
    true_peak_dbtp: float | None
    clipping_flat_factor: float | None
    spectral_energy_above_cutoff_db: float | None
    spectral_energy_above_hires_cutoff_db: float | None
    lame_lowpass_hz: int | None
    dr14: int | None
    spectral_bandwidth_hz: float | None
    noise_floor_db: float | None
    stereo_coherence: float | None
    stream_info: StreamInfo
    is_out_of_phase: bool | None
    is_mono_duplicated: bool | None
    has_mains_hum: bool | None
    peak_db: float | None


def _broken_track_health_result(filename: str, reason: str, stream_info: StreamInfo) -> TrackHealthResult:
    """Track Health is structurally impossible to measure on a file that
    won't decode — a real, terminal outcome (`track_tier=None`), not
    merely "not yet measured". `content_hash` and `stream_info` (from
    ffprobe, which doesn't need a successful full decode) are kept —
    everything that requires the decode itself is not.
    """
    return TrackHealthResult(
        track_tier=None,
        track_issues=[reason],
        info=[],
        track_score=None,
        content_hash=content_hash(filename),
        lufs=None,
        true_peak_dbtp=None,
        clipping_flat_factor=None,
        spectral_energy_above_cutoff_db=None,
        spectral_energy_above_hires_cutoff_db=None,
        lame_lowpass_hz=None,
        dr14=None,
        spectral_bandwidth_hz=None,
        noise_floor_db=None,
        stereo_coherence=None,
        stream_info=stream_info,
        is_out_of_phase=None,
        is_mono_duplicated=None,
        has_mains_hum=None,
        peak_db=None,
    )


def analyze_track_health(
    filename: str, ffmpeg_path: str | None = None, thresholds: Thresholds | None = None
) -> TrackHealthResult:
    """Runs on a background thread — real decode + measurement work, not
    instant. Deliberately independent of analyze_file_health's own
    (lighter) decode — see that function's docstring for why "separate
    scans" means genuinely separate, not two views onto one shared pass.
    Every whole-file measurement that doesn't depend on another
    measurement's result first runs in one merged ffmpeg invocation (see
    _run_merged_analysis). DR14/hum/noise-floor always run (Track
    Health's weighted composite needs every applicable check's real
    value, not a shortcut for "already forced Bad" the way the old
    OR-gate architecture had).

    `thresholds` defaults to the empirically-calibrated Thresholds()
    values when omitted — callers (the Options page sliders) override
    per scan rather than mutating module state, since scans may run
    concurrently across several files on Picard's background-thread pool.

    `FfmpegNotFoundError`/`FfmpegVersionTooOldError` still raise; a
    decode failure on this specific file is a real persisted
    `track_tier=None` result (see _broken_track_health_result), not an
    exception — mirrors analyze_file_health's own File Health verdict,
    though this function makes no assumption a File Health scan of the
    same file ever ran (fully independent scans, per design).
    """
    thresholds = thresholds or Thresholds()
    ffmpeg = find_ffmpeg(ffmpeg_path)
    ffprobe = _find_ffprobe(ffmpeg)
    check_ffmpeg_version(ffmpeg)
    stream_info = _probe_stream_info(ffprobe, filename)

    # Both decided upfront from ffprobe metadata alone, before any ffmpeg
    # filter pass runs, so the merged graph can include exactly the
    # branches worth running rather than always paying for every branch
    # or juggling a second invocation once "the file's actual channel
    # count" becomes known partway through.
    channels = stream_info.channels or 0
    is_hires = stream_info.sample_rate is not None and stream_info.sample_rate > FAKE_HIRES_MIN_SAMPLE_RATE_HZ

    merged_stdout, merged_stderr, merged_returncode = _run_merged_analysis(
        ffmpeg,
        filename,
        include_phase_check=channels >= 2,
        include_hires_check=is_hires,
        phase_angle_deg=thresholds.phase_angle_deg,
        sample_rate=stream_info.sample_rate,
    )
    if merged_returncode != 0:
        return _broken_track_health_result(
            filename,
            f"ffmpeg couldn't decode {os.path.basename(filename)} as audio "
            "(corrupt, truncated, unsupported format, or a decode timeout)",
            stream_info,
        )
    main_stderr = _filter_instance_output(merged_stderr, 'main')
    flat_factor, peak_db = _parse_astats(main_stderr)
    above_cutoff_db = _parse_mean_volume(_filter_instance_output(merged_stderr, 'cutoff'))
    lufs, true_peak = _parse_loudnorm(merged_stderr)
    # Informational only, for the details panel's ranking — doesn't feed
    # the tier (see BANDWIDTH_* constants for why this is a continuous,
    # cause-agnostic estimate rather than a defect gate).
    spectral_bandwidth_hz = _measure_bandwidth(merged_stderr, stream_info.sample_rate, peak_db)

    track_issues: list[str] = []
    info: list[str] = []

    has_clipping = flat_factor > thresholds.clip_flat_factor
    if has_clipping:
        track_issues.append(f"Sound is clipped — pushed past full volume and distorted{_tunable('Clipping')}")

    has_true_peak_overs = true_peak is not None and true_peak >= thresholds.true_peak_dbtp
    if has_true_peak_overs and not has_clipping:
        # Only report separately when Flat factor didn't already catch a
        # defect — both signals pointing at "this file clips" is redundant
        # to say twice, but true-peak-only is a genuinely distinct finding
        # (inter-sample overshoot with no sample actually at full scale).
        track_issues.append(
            f"Volume peaks go past full scale between samples (True Peak {true_peak:+.1f}dBTP) "
            f"— can distort on some playback equipment{_tunable('True Peak')}"
        )

    has_signal = peak_db is not None and peak_db > MIN_PEAK_DB_FOR_SPECTRAL_CHECK
    has_cutoff = (
        has_signal and above_cutoff_db is not None and above_cutoff_db < thresholds.spectral_silence_db
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
            track_issues.append(
                f"Confirmed lossy source: the encoder's own settings would have let sound up "
                f"to {lame_lowpass_hz / 1000:.1f}kHz through, but there's none above "
                f"{SPECTRAL_CUTOFF_FREQUENCY_HZ / 1000:.0f}kHz — this was compressed from an "
                f"already lossy source before reaching this file{_tunable('Spectral Cutoff')}"
            )
        else:
            track_issues.append(
                f"Missing all sound above {SPECTRAL_CUTOFF_FREQUENCY_HZ / 1000:.0f}kHz — likely "
                f"converted from a lossy file (like an MP3) at some point{_tunable('Spectral Cutoff')}"
            )

    above_hires_cutoff_db: float | None = None
    if is_hires and has_signal:
        # is_hires alone decided whether this branch was even in the
        # merged graph; has_signal (only knowable after that same decode)
        # decides whether its result is trustworthy to act on — a
        # near-silent hi-res-rate file trivially has "no content above
        # 24kHz" for the same reason it has no content anywhere.
        above_hires_cutoff_db = _parse_mean_volume(_filter_instance_output(merged_stderr, 'hires'))
        if above_hires_cutoff_db is not None and above_hires_cutoff_db < thresholds.spectral_silence_db:
            # A real measured defect, not a guess: genuine content captured
            # at this sample rate would extend past the check frequency
            # (see FAKE_HIRES_CHECK_FREQUENCY_HZ for the validated margin);
            # a hard wall there means the file was upsampled from an
            # ordinary-rate source, not actually recorded/mastered at its
            # declared rate.
            track_issues.append(
                f"Labeled as {stream_info.sample_rate}Hz hi-res audio, but has no real sound "
                f"above {FAKE_HIRES_CHECK_FREQUENCY_HZ / 1000:.0f}kHz — likely stretched up "
                f"from an ordinary file rather than genuine hi-res{_tunable('Spectral Cutoff')}"
            )

    # Phase/channel-identity checks need two channels to compare —
    # meaningless (and aphasemeter would just misbehave) on mono source
    # material, which is exactly why the branch was left out of the
    # merged graph entirely for those files rather than run and ignored.
    stereo_coherence: float | None = None
    is_mono_duplicated: bool | None = None
    is_out_of_phase: bool | None = None
    if channels >= 2:
        is_mono_duplicated, is_out_of_phase = _parse_phasemeter(merged_stderr)
        stereo_coherence = _measure_stereo_coherence(merged_stdout)
        if is_out_of_phase:
            # A real, audible defect — will cancel out when summed to mono
            # (many phone/laptop/car speakers do this).
            track_issues.append(
                f"Left and right channels cancel out — will sound hollow or vanish entirely on "
                f"mono speakers{_tunable('Out-of-Phase Channels')}"
            )
        if is_mono_duplicated:
            # Not a quality defect — a mono source duplicated into both
            # channels loses nothing, it's just wasteful. Informational,
            # doesn't affect the tier.
            info.append("Left/right channels are identical (mono content in a stereo container)")

    dr14 = _measure_dr14(ffmpeg, filename, stream_info.sample_rate)
    quiet_interval = _find_quiet_interval(ffmpeg, filename)
    hum_note = _detect_hum(ffmpeg, filename, quiet_interval)
    has_mains_hum = hum_note is not None if quiet_interval is not None else None
    if hum_note is not None:
        track_issues.append(hum_note)
    noise_floor_db = _measure_noise_floor(ffmpeg, filename, quiet_interval)

    ok_threshold = DR14_OK_THRESHOLD - thresholds.dr14_shift
    if dr14 is not None and dr14 < ok_threshold:
        track_issues.append(f"Compressed master (DR{dr14}){_tunable('Compression Tolerance')}")

    track_score = compute_track_score(
        TrackHealthInputs(
            flat_factor=flat_factor,
            true_peak_dbtp=true_peak,
            has_signal=has_signal,
            spectral_energy_above_cutoff_db=above_cutoff_db,
            is_hires=is_hires,
            spectral_energy_above_hires_cutoff_db=above_hires_cutoff_db,
            is_out_of_phase=is_out_of_phase,
            dr14=dr14,
            has_mains_hum=has_mains_hum,
        ),
        thresholds,
    )
    track_tier = track_tier_from_score(track_score) if track_score is not None else None

    return TrackHealthResult(
        track_tier=track_tier,
        track_issues=track_issues,
        info=info,
        track_score=track_score,
        content_hash=content_hash(filename),
        lufs=lufs,
        true_peak_dbtp=true_peak,
        clipping_flat_factor=flat_factor,
        spectral_energy_above_cutoff_db=above_cutoff_db,
        spectral_energy_above_hires_cutoff_db=above_hires_cutoff_db,
        lame_lowpass_hz=lame_lowpass_hz,
        dr14=dr14,
        spectral_bandwidth_hz=spectral_bandwidth_hz,
        noise_floor_db=noise_floor_db,
        stereo_coherence=stereo_coherence,
        stream_info=stream_info,
        is_out_of_phase=is_out_of_phase,
        is_mono_duplicated=is_mono_duplicated,
        has_mains_hum=has_mains_hum,
        peak_db=peak_db,
    )
