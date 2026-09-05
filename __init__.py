"""File Health plugin.

Surfaces one compact "Health" column (colored tier text, matching Picard's
own match-quality delegate-column pattern) with the itemized reasons in a
hover tooltip, rather than a separate always-visible text column — a wide
free-text column doesn't scale as more checks get added, isn't
sortable/filterable, and duplicates information better shown on demand.

Real analysis (clipping, spectral-cutoff/transcode detection, a coarse
LUFS-based loudness gradient, content-hash change detection) lives in
analysis.py, via ffmpeg. See that module's docstring for exactly what's
real vs. still-coarse-proxy vs. documented future work.
"""

import os
import tempfile
from collections.abc import Callable

from PyQt6 import (
    QtCore,
    QtGui,
    QtWidgets,
)

from picard import tagger_instance
from picard.file import File
from picard.item import Item
from picard.track import Track
from picard.plugin3.api import (
    BaseAction,
    OptionsPage,
    PluginApi,
)
from picard.ui.itemviews.custom_columns.factory import make_delegate_column
from picard.ui.itemviews.custom_columns.protocols import (
    ColumnValueProvider,
    DelegateProvider,
)
from picard.ui.itemviews.custom_columns.registry import registry
from picard.ui.itemviews.events import header_events
from picard.ui.match_icons import (
    load_match_icons,
    match_icons,
)
from picard.util import iter_files_from_objects
from picard.util.thread import run_task

from . import analysis


# Ordered worst-to-best, matching analysis.py's FILE_TIER_*/track_tier_
# from_score constants exactly. File Health has a terminal `Unplayable`
# tier Track Health can't reach (a file that won't decode has nothing
# to measure perceptually — see analysis.analyze_file's docstring).
FILE_TIERS = ("Unplayable", "Bad", "OK", "Good", "Excellent")
TRACK_TIERS = ("Bad", "OK", "Good", "Excellent")

# picard.ui.match_icons ships 6 bookmark levels (0=worst red, 5=best
# green). Mapped explicitly rather than evenly spread across all 6 so
# the visual jump from a real defect (Bad) to a clean file (OK) stays
# the same big red->green jump it was under the old 6-tier scale, with
# only the top three tiers (OK/Good/Excellent) using the finer
# upper-range distinctions. Track Health omits level 0 entirely — a
# perceptual "Bad" is still a real defect, but nothing it measures is
# as unambiguous as File Health's Unplayable (a file that won't even play).
FILE_TIER_ICON_LEVEL = {"Unplayable": 0, "Bad": 1, "OK": 3, "Good": 4, "Excellent": 5}
TRACK_TIER_ICON_LEVEL = {"Bad": 1, "OK": 3, "Good": 4, "Excellent": 5}


class _NoWheelSlider(QtWidgets.QSlider):
    """A QSlider that ignores mouse-wheel events instead of capturing
    them.

    Qt's default QSlider grabs the wheel event on hover and changes its
    own value — inside a scrollable Options page, that means scrolling
    silently stops and starts dragging whichever slider the cursor
    happens to be over. Calling event.ignore() here lets Qt propagate
    the event up to the enclosing scroll area instead, which every
    slider on this options page should do (none of them are meant to
    be wheel-adjustable).
    """

    def wheelEvent(self, event: QtGui.QWheelEvent | None) -> None:
        if event is not None:
            event.ignore()


class _SensitivitySlider(QtWidgets.QFrame):
    """A discrete step-based slider bound to a real gate threshold.

    Each step is a (value, hint) pair: `value` is the actual threshold
    passed to analysis.Thresholds, `hint` is a short plain-language
    description of what that step catches — swapped in live as the
    slider moves, so the user feels where a setting lands before
    committing to it rather than reading a bare, uncontextualized
    number. Steps are hand-picked non-linear points grounded in this
    plugin's own calibration data (see the step tables in
    HealthOptionsPage.__init__), not an even split of the numeric
    range — the meaningful transitions in each measurement (e.g.
    clipping's Flat factor) aren't evenly spaced either.

    Rendered as its own bordered frame (title, slider, hint) so
    adjacent sliders in an options-page column read as distinct
    controls rather than a wall of unattributed hint text.
    """

    def __init__(
        self,
        title: str,
        steps: list[tuple[float, str]],
        default_index: int,
        fmt: str,
        parent: QtWidgets.QWidget | None = None,
        tag: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._steps = steps
        self._fmt = fmt
        self._default_index = default_index

        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(2)

        header = QtWidgets.QHBoxLayout()
        title_label = QtWidgets.QLabel(title, self)
        title_label.setWordWrap(False)
        bold = title_label.font()
        bold.setBold(True)
        title_label.setFont(bold)
        header.addWidget(title_label)
        if tag:
            # A separate, independently-sized label rather than baking the
            # tag into the title string — a single long concatenated string
            # forces the whole header to fight the layout for width and can
            # wrap or get silently clipped; two small widgets don't.
            tag_label = QtWidgets.QLabel(f"({tag})", self)
            tag_label.setStyleSheet("color: palette(mid);")
            header.addWidget(tag_label)
        self.value_label = QtWidgets.QLabel(self)
        header.addStretch(1)
        header.addWidget(self.value_label)
        layout.addLayout(header)

        self.slider = _NoWheelSlider(QtCore.Qt.Orientation.Horizontal, self)
        self.slider.setMinimum(0)
        self.slider.setMaximum(len(steps) - 1)
        self.slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._on_changed)
        layout.addWidget(self.slider)

        self.hint_label = QtWidgets.QLabel(self)
        self.hint_label.setWordWrap(True)
        muted = self.hint_label.font()
        muted.setPointSize(max(muted.pointSize() - 1, 8))
        self.hint_label.setFont(muted)
        self.hint_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.hint_label)

        self.slider.setValue(default_index)
        self._on_changed(default_index)

    def _on_changed(self, index: int) -> None:
        value, hint = self._steps[index]
        self.value_label.setText(self._fmt.format(value))
        self.hint_label.setText(hint)

    def value(self) -> float:
        return self._steps[self.slider.value()][0]

    def set_value(self, value: float) -> None:
        closest = min(range(len(self._steps)), key=lambda i: abs(self._steps[i][0] - value))
        self.slider.setValue(closest)

    def reset_to_default(self) -> None:
        self.slider.setValue(self._default_index)



# Each gate's sensitivity slider has 10 hand-picked, non-linear steps —
# grounded in analysis.py's own calibration data (see the cited margins
# in each list) rather than an even split of the numeric range, since
# the meaningful transitions in each measurement aren't evenly spaced
# either. (value, hint) pairs, ordered lenient -> strict.

_CLIP_STEPS: list[tuple[float, str]] = [
    (25.0, "Only catches severe, obvious clipping — a wall of distortion."),
    (20.0, "Catches heavy clipping most listeners would notice immediately."),
    (16.0, "Catches clipping close to the mildest real case we've measured."),
    (8.0, "Catches moderate clipping — likely audible as harshness."),
    (4.0, "Catches light clipping — may be audible on close listening."),
    (2.0, "Catches subtle clipping most listeners wouldn't notice."),
    (1.0, "Balanced default — catches real clipping without flagging clean loud audio."),
    (0.5, "More sensitive than default — may flag some loud-but-clean audio."),
    (0.1, "Very sensitive — likely to flag loud, dense mixes that aren't clipped."),
    (0.05, "Extremely sensitive — expect false positives on loud modern masters."),
]
_CLIP_DEFAULT_INDEX = 6  # matches analysis.MIN_FLAT_FACTOR_FOR_CLIPPING (1.0)

_TRUE_PEAK_STEPS: list[tuple[float, str]] = [
    (3.0, "Only catches extreme overs — several dB past full volume."),
    (2.0, "Catches clearly audible overshoot between samples."),
    (1.5, "Catches overshoot well beyond what's normal for a finished master."),
    (1.0, "Catches overshoot beyond typical headroom for a finished master."),
    (0.8, "Slightly stricter than this measurement's own margin of error."),
    (0.6, "Balanced default — just past this measurement's own margin of error."),
    (0.4, "Tighter than this measurement can reliably tell apart — may flag otherwise-fine masters."),
    (0.2, "Close to full volume — likely to flag normally loud tracks."),
    (0.1, "Right at the edge of measurement noise — expect false positives."),
    (0.0, "Flags anything technically over full volume, including measurement noise."),
]
_TRUE_PEAK_DEFAULT_INDEX = 5  # matches analysis.TRUE_PEAK_THRESHOLD_DBTP (0.6)

_SPECTRAL_STEPS: list[tuple[float, str]] = [
    (-90.0, "Only catches files with near-total silence up top — very few false positives."),
    (-85.0, "Requires close to true silence above the cutoff."),
    (-75.0, "Requires strong silence above the cutoff frequency."),
    (-65.0, "Slightly more sensitive than default."),
    (-60.0, "Balanced default — matches real transcodes and fake hi-res files we've tested."),
    (-55.0, "Slightly more likely to flag quiet-but-real high frequencies."),
    (-50.0, "Moderately sensitive — may flag naturally soft treble."),
    (-45.0, "Sensitive — may flag mellow or bass-heavy mixes."),
    (-35.0, "Very sensitive — expect false positives on quiet acoustic material."),
    (-30.0, "Extremely sensitive — likely to flag many legitimate files."),
]
_SPECTRAL_DEFAULT_INDEX = 4  # matches analysis.SPECTRAL_SILENCE_THRESHOLD_DB (-60)

_PHASE_STEPS: list[tuple[float, str]] = [
    (179.0, "Only catches near-perfect phase inversion."),
    (177.0, "Requires almost exact inversion."),
    (174.0, "Slightly more sensitive than ffmpeg's own default."),
    (170.0, "ffmpeg's own default — catches clear phase problems."),
    (165.0, "Slightly more sensitive — may catch wide stereo effects."),
    (158.0, "Moderately sensitive — intentional stereo widening may trigger this."),
    (150.0, "Sensitive — likely to flag wide mixes or reverb-heavy tracks."),
    (140.0, "Very sensitive — many wide stereo mixes will trigger this."),
    (120.0, "Extremely sensitive — most stereo content will trigger this."),
    (95.0, "Nearly any decorrelated stereo signal will trigger this."),
]
_PHASE_DEFAULT_INDEX = 3  # matches analysis.PHASE_OUT_OF_PHASE_ANGLE_DEG (170)

# Calibrated against this project's own 19-fixture real-track sample
# (see analysis.NOISE_FLOOR_THRESHOLD_DB) rather than a much larger
# library sweep — narrower validation than the other gates here, worth
# widening if real-world use turns up false positives or misses.
_NOISE_FLOOR_STEPS: list[tuple[float, str]] = [
    (-20.0, "Only catches obviously loud background noise."),
    (-25.0, "Catches clearly audible hiss or static."),
    (-30.0, "Catches moderately audible background noise."),
    (-35.0, "Balanced default — sits just above every clean file in our real-track sample."),
    (-40.0, "Slightly more sensitive — may flag quiet room tone on live recordings."),
    (-42.5, "Moderately sensitive — may flag reverb tails as noise."),
    (-45.0, "Sensitive — may flag ordinary quiet passages on loud modern masters."),
    (-47.5, "Very sensitive — expect false positives on many real files."),
    (-50.0, "Extremely sensitive — most real masters will trigger this."),
    (-55.0, "Nearly any measurable quiet-passage noise will trigger this."),
]
_NOISE_FLOOR_DEFAULT_INDEX = 3  # matches analysis.NOISE_FLOOR_THRESHOLD_DB (-35)


# Shifts analysis.DR14_POOR_THRESHOLD/OK/GOOD/GREAT together (see
# analysis.Thresholds.dr14_shift) rather than exposing four independent
# band-boundary sliders — keeps the manual-documented relative spacing
# between bands intact and just moves the whole scale's anchor point,
# instead of risking the bands crossing each other or drifting apart
# from what the TT DR Offline Meter manual actually documented.
# Lenient (positive shift) -> strict (negative shift), matching every
# other slider's left-to-right convention.
_DR14_SHIFT_STEPS: list[tuple[float, str]] = [
    (4.0, "Very lenient — a very squashed-sounding track (DR4) won't read as Poor."),
    (3.0, "Lenient — a heavily squashed track (DR5) won't read as Poor."),
    (2.0, "Somewhat lenient — a squashed track (DR6) won't read as Poor."),
    (1.0, "Slightly lenient — a fairly squashed track (DR7) won't read as Poor."),
    (0.0, "Balanced default — matches the official reference scale for this measurement."),
    (-1.0, "Slightly stricter — needs a bit more dynamic range (DR9) to clear \"Poor\"."),
    (-2.0, "Stricter — needs noticeably more dynamic range (DR10) to clear \"Poor\"."),
    (-3.0, "Strict — needs a lot more dynamic range (DR11) to clear \"Poor\"."),
    (-4.0, "Very strict — only tracks with very open dynamics (DR12+) avoid \"Poor\"."),
]
_DR14_SHIFT_DEFAULT_INDEX = 4  # matches analysis.Thresholds.dr14_shift default (0)


def _section_header(title: str) -> QtWidgets.QLabel:
    """A bold, enlarged QLabel used as a section heading above a bordered
    content frame — direct QFont mutation, not a stylesheet, because
    QGroupBox::title styling proved unreliable under macOS's native Qt
    style (confirmed: it rendered no different from the native default).
    This is the same technique _SensitivitySlider already uses
    successfully for its own title/hint labels.
    """
    label = QtWidgets.QLabel(title)
    font = label.font()
    font.setBold(True)
    font.setPointSize(font.pointSize() + 3)
    label.setFont(font)
    return label


def _section_frame() -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    """A bordered content frame to place under a _section_header(),
    replacing QGroupBox entirely so the (unreliable) native title
    rendering never enters the picture.
    """
    frame = QtWidgets.QFrame()
    frame.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
    frame_layout = QtWidgets.QVBoxLayout(frame)
    return frame, frame_layout


def _thresholds_from_config(plugin_config) -> analysis.Thresholds:
    return analysis.Thresholds(
        clip_flat_factor=plugin_config['clip_flat_factor'],
        true_peak_dbtp=plugin_config['true_peak_dbtp'],
        spectral_silence_db=plugin_config['spectral_silence_db'],
        phase_angle_deg=plugin_config['phase_angle_deg'],
        dr14_shift=plugin_config['dr14_shift'],
        noise_floor_db=plugin_config['noise_floor_db'],
    )


def _scan_file_health_one(filename: str, ffmpeg_path: str | None) -> dict[str, object]:
    """Runs on a background thread — the lightweight, structural-only
    decode (see analysis.analyze_file_health). No sliders/thresholds:
    File Health has none by design.
    """
    try:
        result = analysis.analyze_file_health(filename, ffmpeg_path=ffmpeg_path)
    except analysis.FfmpegNotFoundError as exc:
        return {'error': str(exc)}
    return {
        'file_tier': result.file_tier,
        'file_flags': "; ".join(result.file_issues),
        'info': "; ".join(result.info),
        'content_hash': result.content_hash,
        'codec_name': result.stream_info.codec_name,
        'profile': result.stream_info.profile,
        'bitrate_kbps': result.stream_info.bitrate_kbps,
        'sample_rate': result.stream_info.sample_rate,
        'channels': result.stream_info.channels,
    }


def _scan_track_health_one(filename: str, ffmpeg_path: str | None, thresholds: analysis.Thresholds) -> dict[str, object]:
    """Runs on a background thread — the full perceptual decode (see
    analysis.analyze_track_health), independent of any File Health scan
    of the same file.
    """
    try:
        result = analysis.analyze_track_health(filename, ffmpeg_path=ffmpeg_path, thresholds=thresholds)
    except analysis.FfmpegNotFoundError as exc:
        return {'error': str(exc)}
    return {
        'track_tier': result.track_tier,
        'track_flags': "; ".join(result.track_issues),
        'info': "; ".join(result.info),
        'track_score': result.track_score,
        'content_hash': result.content_hash,
        'bandwidth_hz': result.spectral_bandwidth_hz,
        'noise_floor_db': result.noise_floor_db,
        'dr14': result.dr14,
        'stereo_coherence': result.stereo_coherence,
        'true_peak_dbtp': result.true_peak_dbtp,
        'clipping_flat_factor': result.clipping_flat_factor,
        'spectral_cutoff_db': result.spectral_energy_above_cutoff_db,
        'hires_cutoff_db': result.spectral_energy_above_hires_cutoff_db,
        'is_out_of_phase': result.is_out_of_phase,
        'is_mono_duplicated': result.is_mono_duplicated,
        'peak_db': result.peak_db,
        'codec_name': result.stream_info.codec_name,
        'profile': result.stream_info.profile,
        'bitrate_kbps': result.stream_info.bitrate_kbps,
        'sample_rate': result.stream_info.sample_rate,
        'channels': result.stream_info.channels,
    }


def _encode_metric(value: float | None) -> str:
    """Metadata tags are text — a plain str() round-trips cleanly through
    float()/int() on read, and an empty string means "not measured"
    (distinct from a real 0.0/0), consistent with how every other
    optional field on this file's metadata already handles "unknown".
    """
    return '' if value is None else str(value)


def _decode_metric(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _encode_bool(value: bool | None) -> str:
    """Tri-state, same round-trip convention as _encode_metric: '' means
    not applicable/not measured (e.g. a mono file has no phase check to
    report), distinct from a real True/False result.
    """
    if value is None:
        return ''
    return '1' if value else '0'


def _decode_bool(raw: str) -> bool | None:
    if raw == '1':
        return True
    if raw == '0':
        return False
    return None

# --- Cross-match metadata persistence ---
#
# Picard's Track.add_file()/remove_file() call File.copy_metadata(), which
# replaces file.metadata wholesale with the matched track's (or the file's
# own orig_metadata's) tags — see picard/file.py's copy_metadata(). Only
# tags Picard's own core tag registry marks "calculated" or "preserved"
# survive that replacement automatically; our plugin-defined `~health_*`
# tags aren't registered there at all, so a scanned file's health data was
# silently wiped every time it got matched to a track (or unmatched back),
# forcing a full rescan. Fixed by keeping our own side-cache keyed by the
# file's absolute path (the File object itself survives a match/unmatch
# unchanged, only its .metadata gets replaced) and re-applying it via the
# file-post-addition/removal-to-track hooks, which Picard runs right after
# copy_metadata() already did the damage.
_HEALTH_METADATA_KEYS: tuple[str, ...] = (
    '~health_file_tier', '~health_file_flags', '~health_file_info',
    '~health_file_content_hash', '~health_file_changed_since_scan',
    '~health_track_tier', '~health_track_flags', '~health_track_info',
    '~health_track_score', '~health_track_content_hash', '~health_track_changed_since_scan',
    '~health_bandwidth_hz', '~health_noise_floor_db', '~health_dr14', '~health_stereo_coherence',
    '~health_true_peak_dbtp', '~health_clip_flat_factor', '~health_spectral_cutoff_db',
    '~health_hires_cutoff_db', '~health_is_out_of_phase', '~health_is_mono_duplicated',
    '~health_peak_db', '~health_codec_name', '~health_profile', '~health_bitrate_kbps',
    '~health_sample_rate', '~health_channels',
)

_health_metadata_cache: dict[str, dict[str, str]] = {}


def _cache_health_metadata(file: File) -> None:
    """Snapshots every currently-set health tag for later restoration —
    call this right after writing scan results onto file.metadata. Merges
    into any existing entry rather than replacing it, so a File Health-only
    scan doesn't blank out already-cached Track Health fields or vice versa.
    """
    snapshot = _health_metadata_cache.setdefault(file.filename, {})
    for key in _HEALTH_METADATA_KEYS:
        value = file.metadata[key]
        if value:
            snapshot[key] = value


def _restore_health_metadata_on_match(api: PluginApi, track: Track, file: File) -> None:
    """Runs after Picard matches or unmatches a file against a track —
    both call File.copy_metadata() first, which wipes our health tags (see
    the module comment above). Re-applies the cached values, if any.
    """
    snapshot = _health_metadata_cache.get(file.filename)
    if not snapshot:
        return
    for key, value in snapshot.items():
        file.metadata[key] = value
    file.update()




def _file_health_scan_finished(file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
    """Runs back on the main thread once _scan_file_health_one completes."""
    if error is not None:
        # Anything _scan_file_health_one's own try/except didn't already
        # turn into a result['error'] string — a genuine bug rather than
        # a handled ffmpeg/input failure. Still needs to be visible:
        # silently doing nothing here would leave the file looking
        # permanently "pending" with no clue why, which is worse than a
        # slightly generic message.
        tagger_instance().window.set_statusbar_message(
            "File Health: scan failed for %(file)s: %(error)s",
            {'file': file.base_filename, 'error': str(error)},
            echo=None,
        )
    elif result:
        if 'error' in result:
            tagger_instance().window.set_statusbar_message(
                "File Health: %(error)s (configure ffmpeg in Options → Plugins → File Health)",
                {'error': result['error']},
                echo=None,
            )
        elif 'file_tier' in result:
            previous_hash = file.metadata['~health_file_content_hash']
            changed = bool(previous_hash) and previous_hash != result['content_hash']
            file.metadata['~health_file_tier'] = result['file_tier']
            file.metadata['~health_file_flags'] = result['file_flags']
            file.metadata['~health_file_info'] = result['info']
            file.metadata['~health_file_content_hash'] = result['content_hash']
            file.metadata['~health_file_changed_since_scan'] = '1' if changed else ''
            file.metadata['~health_codec_name'] = result['codec_name'] or ''
            file.metadata['~health_profile'] = result['profile'] or ''
            file.metadata['~health_bitrate_kbps'] = _encode_metric(result['bitrate_kbps'])
            file.metadata['~health_sample_rate'] = _encode_metric(result['sample_rate'])
            file.metadata['~health_channels'] = _encode_metric(result['channels'])
            _cache_health_metadata(file)
    file.clear_pending()
    file.update()


def _track_health_scan_finished(file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
    """Runs back on the main thread once _scan_track_health_one completes."""
    if error is not None:
        tagger_instance().window.set_statusbar_message(
            "File Health: scan failed for %(file)s: %(error)s",
            {'file': file.base_filename, 'error': str(error)},
            echo=None,
        )
    elif result:
        if 'error' in result:
            tagger_instance().window.set_statusbar_message(
                "File Health: %(error)s (configure ffmpeg in Options → Plugins → File Health)",
                {'error': result['error']},
                echo=None,
            )
        elif 'track_tier' in result:
            previous_hash = file.metadata['~health_track_content_hash']
            changed = bool(previous_hash) and previous_hash != result['content_hash']
            file.metadata['~health_track_tier'] = result['track_tier'] or ''
            file.metadata['~health_track_flags'] = result['track_flags']
            file.metadata['~health_track_info'] = result['info']
            file.metadata['~health_track_score'] = _encode_metric(result['track_score'])
            file.metadata['~health_track_content_hash'] = result['content_hash']
            file.metadata['~health_track_changed_since_scan'] = '1' if changed else ''
            file.metadata['~health_bandwidth_hz'] = _encode_metric(result['bandwidth_hz'])
            file.metadata['~health_noise_floor_db'] = _encode_metric(result['noise_floor_db'])
            file.metadata['~health_dr14'] = _encode_metric(result['dr14'])
            file.metadata['~health_stereo_coherence'] = _encode_metric(result['stereo_coherence'])
            file.metadata['~health_true_peak_dbtp'] = _encode_metric(result['true_peak_dbtp'])
            file.metadata['~health_clip_flat_factor'] = _encode_metric(result['clipping_flat_factor'])
            file.metadata['~health_spectral_cutoff_db'] = _encode_metric(result['spectral_cutoff_db'])
            file.metadata['~health_hires_cutoff_db'] = _encode_metric(result['hires_cutoff_db'])
            file.metadata['~health_is_out_of_phase'] = _encode_bool(result['is_out_of_phase'])
            file.metadata['~health_is_mono_duplicated'] = _encode_bool(result['is_mono_duplicated'])
            file.metadata['~health_peak_db'] = _encode_metric(result['peak_db'])
            # Track Health's own (heavier) decode probes stream info too —
            # harmless to refresh these shared, scan-type-agnostic fields
            # even if a File Health scan already set them.
            file.metadata['~health_codec_name'] = result['codec_name'] or ''
            file.metadata['~health_profile'] = result['profile'] or ''
            file.metadata['~health_bitrate_kbps'] = _encode_metric(result['bitrate_kbps'])
            file.metadata['~health_sample_rate'] = _encode_metric(result['sample_rate'])
            file.metadata['~health_channels'] = _encode_metric(result['channels'])
            _cache_health_metadata(file)
    file.clear_pending()
    file.update()


def _maybe_auto_scan(api: PluginApi, file: File) -> None:
    """File-post-load hook, always registered — checks the option live so
    toggling it in Options takes effect immediately, no restart needed.
    File Health only: cheap and structural, appropriate for a bulk/
    automatic check on every newly added file. Track Health's heavier
    perceptual decode stays manual/on-demand (see ScanTrackHealthAction).
    """
    if not api.plugin_config['auto_scan']:
        return
    file.set_pending()
    ffmpeg_path = api.plugin_config['ffmpeg_path'] or None
    run_task(
        lambda f=file, p=ffmpeg_path: _scan_file_health_one(f.filename, p),
        lambda result=None, error=None, f=file: _file_health_scan_finished(f, result, error),
    )



class HealthOptionsPage(OptionsPage):
    NAME = "file_health"
    TITLE = "File Health"
    PARENT = "plugins"

    def __init__(self) -> None:
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        self.auto_scan_checkbox = QtWidgets.QCheckBox("Automatically scan File Health for newly added files", self)
        layout.addWidget(self.auto_scan_checkbox)
        auto_scan_detail = QtWidgets.QLabel(
            "Runs the same quick check as the manual \"Scan File Health…\" action, just "
            "automatically. Track Health's slower, more detailed scan always stays manual "
            "— use \"Scan Track Health…\" from the right-click menu, or the File Health "
            "Details window.",
            self,
        )
        auto_scan_detail.setWordWrap(True)
        layout.addWidget(auto_scan_detail)

        layout.addWidget(_section_header("ffmpeg Location"))
        ffmpeg_group, ffmpeg_layout = _section_frame()

        path_row = QtWidgets.QHBoxLayout()
        self.ffmpeg_path_edit = QtWidgets.QLineEdit(self)
        self.ffmpeg_path_edit.setPlaceholderText("Leave blank to search PATH automatically")
        self.ffmpeg_path_edit.textChanged.connect(self._refresh_ffmpeg_status)
        browse_button = QtWidgets.QPushButton("Browse…", self)
        browse_button.clicked.connect(self._browse_ffmpeg)
        path_row.addWidget(self.ffmpeg_path_edit)
        path_row.addWidget(browse_button)
        ffmpeg_layout.addLayout(path_row)

        button_row = QtWidgets.QHBoxLayout()
        detect_button = QtWidgets.QPushButton("Detect Automatically", self)
        detect_button.clicked.connect(self._detect_ffmpeg)
        download_button = QtWidgets.QPushButton("Get ffmpeg…", self)
        download_button.clicked.connect(self._open_ffmpeg_download_page)
        button_row.addWidget(detect_button)
        button_row.addWidget(download_button)
        button_row.addStretch(1)
        ffmpeg_layout.addLayout(button_row)

        self.ffmpeg_status_label = QtWidgets.QLabel(self)
        self.ffmpeg_status_label.setWordWrap(True)
        ffmpeg_layout.addWidget(self.ffmpeg_status_label)

        layout.addWidget(ffmpeg_group)

        layout.addWidget(_section_header("Track Health Sensitivity"))
        sensitivity_group, sensitivity_layout = _section_frame()
        sensitivity_intro = QtWidgets.QLabel(
            "These sliders weight how much each check contributes to a file's Track Health "
            "score — no single check can force a \"Bad\" verdict on its own anymore. Loosen a "
            "slider if it's flagging files that sound OK to you; tighten it if it's missing "
            "real problems.",
            self,
        )
        sensitivity_intro.setWordWrap(True)
        sensitivity_layout.addWidget(sensitivity_intro)

        self.clip_slider = _SensitivitySlider(
            "Clipping", _CLIP_STEPS, _CLIP_DEFAULT_INDEX, fmt="{:g}", parent=self, tag="CLP",
        )
        sensitivity_layout.addWidget(self.clip_slider)
        sensitivity_layout.addSpacing(8)

        self.true_peak_slider = _SensitivitySlider(
            "True Peak", _TRUE_PEAK_STEPS, _TRUE_PEAK_DEFAULT_INDEX, fmt="{:+.1f} dB", parent=self, tag="TPK",
        )
        sensitivity_layout.addWidget(self.true_peak_slider)
        sensitivity_layout.addSpacing(8)

        self.spectral_silence_slider = _SensitivitySlider(
            "Spectral Cutoff", _SPECTRAL_STEPS, _SPECTRAL_DEFAULT_INDEX, fmt="{:.0f} dB", parent=self, tag="TRB",
        )
        sensitivity_layout.addWidget(self.spectral_silence_slider)
        sensitivity_layout.addSpacing(8)

        self.phase_angle_slider = _SensitivitySlider(
            "Out-of-Phase Channels", _PHASE_STEPS, _PHASE_DEFAULT_INDEX, fmt="{:.0f}\u00b0", parent=self, tag="PHS",
        )
        sensitivity_layout.addWidget(self.phase_angle_slider)
        sensitivity_layout.addSpacing(8)

        self.dr14_shift_slider = _SensitivitySlider(
            "Compression Tolerance", _DR14_SHIFT_STEPS, _DR14_SHIFT_DEFAULT_INDEX, fmt="{:+.0f} DR", parent=self, tag="DYN",
        )
        sensitivity_layout.addWidget(self.dr14_shift_slider)
        sensitivity_layout.addSpacing(8)

        self.noise_floor_slider = _SensitivitySlider(
            "Noise Floor", _NOISE_FLOOR_STEPS, _NOISE_FLOOR_DEFAULT_INDEX, fmt="{:.1f} dB", parent=self, tag="NSF",
        )
        sensitivity_layout.addWidget(self.noise_floor_slider)

        sensitivity_reset_row = QtWidgets.QHBoxLayout()
        sensitivity_reset_button = QtWidgets.QPushButton("Reset to Calibrated Defaults", self)
        sensitivity_reset_button.clicked.connect(self._reset_sensitivity_defaults)
        sensitivity_reset_row.addStretch(1)
        sensitivity_reset_row.addWidget(sensitivity_reset_button)
        sensitivity_layout.addLayout(sensitivity_reset_row)

        layout.addWidget(sensitivity_group)
        layout.addStretch(1)

    def load(self) -> None:
        self.auto_scan_checkbox.setChecked(self.api.plugin_config['auto_scan'])
        self.ffmpeg_path_edit.setText(self.api.plugin_config['ffmpeg_path'])
        self._refresh_ffmpeg_status()
        self.clip_slider.set_value(self.api.plugin_config['clip_flat_factor'])
        self.true_peak_slider.set_value(self.api.plugin_config['true_peak_dbtp'])
        self.spectral_silence_slider.set_value(self.api.plugin_config['spectral_silence_db'])
        self.phase_angle_slider.set_value(self.api.plugin_config['phase_angle_deg'])
        self.dr14_shift_slider.set_value(self.api.plugin_config['dr14_shift'])
        self.noise_floor_slider.set_value(self.api.plugin_config['noise_floor_db'])

    def save(self) -> None:
        self.api.plugin_config['auto_scan'] = self.auto_scan_checkbox.isChecked()
        self.api.plugin_config['ffmpeg_path'] = self.ffmpeg_path_edit.text().strip()
        self.api.plugin_config['clip_flat_factor'] = self.clip_slider.value()
        self.api.plugin_config['true_peak_dbtp'] = self.true_peak_slider.value()
        self.api.plugin_config['spectral_silence_db'] = self.spectral_silence_slider.value()
        self.api.plugin_config['phase_angle_deg'] = self.phase_angle_slider.value()
        self.api.plugin_config['dr14_shift'] = self.dr14_shift_slider.value()
        self.api.plugin_config['noise_floor_db'] = self.noise_floor_slider.value()

    def _reset_sensitivity_defaults(self) -> None:
        self.clip_slider.reset_to_default()
        self.true_peak_slider.reset_to_default()
        self.spectral_silence_slider.reset_to_default()
        self.phase_angle_slider.reset_to_default()
        self.dr14_shift_slider.reset_to_default()
        self.noise_floor_slider.reset_to_default()

    def _browse_ffmpeg(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Locate ffmpeg")
        if path:
            self.ffmpeg_path_edit.setText(path)

    def _detect_ffmpeg(self) -> None:
        try:
            found = analysis.find_ffmpeg(None)  # PATH-only lookup, ignoring current text
        except analysis.FfmpegNotFoundError:
            self.ffmpeg_status_label.setText("Not found on PATH. Try Browse… or Get ffmpeg…")
            return
        self.ffmpeg_path_edit.setText(found)

    def _open_ffmpeg_download_page(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://ffmpeg.org/download.html"))

    def _refresh_ffmpeg_status(self) -> None:
        path = self.ffmpeg_path_edit.text().strip() or None
        try:
            resolved = analysis.find_ffmpeg(path)
        except analysis.FfmpegNotFoundError as exc:
            self.ffmpeg_status_label.setText(str(exc))
            return
        version = analysis.get_ffmpeg_version(resolved)
        min_version = '.'.join(map(str, analysis.MINIMUM_FFMPEG_VERSION))
        if version is None:
            self.ffmpeg_status_label.setText(
                f"Found: {resolved} (version could not be determined — scans will "
                "still be attempted)"
            )
        elif version < analysis.MINIMUM_FFMPEG_VERSION:
            found = '.'.join(map(str, version))
            self.ffmpeg_status_label.setText(
                "<div style='color:#c0392b;'>Found: "
                f"{resolved} (ffmpeg {found} — <b>too old</b>. File Health needs "
                f"ffmpeg {min_version} or newer to run its checks; scans with this "
                "version will fail with an explanatory error. Use Get ffmpeg… below "
                "for a current build.)</div>"
            )
        else:
            found = '.'.join(map(str, version))
            self.ffmpeg_status_label.setText(f"Found: {resolved} (ffmpeg {found} — OK)")


class ScanFileHealthAction(BaseAction):
    """Right-click action that triggers the File Health scan on demand.

    Available everywhere (unmatched files, clusters, and matched tracks
    on either side of the main window) — File Health's cheap structural
    check is always relevant regardless of matching state. Manual by
    default — real analysis needs to decode audio, which is neither
    instant nor safe to run inline on the file-load callback. An opt-in
    automatic mode is available via Options (off by default, same
    background-threaded scan either way). Each scan runs on a
    background thread via run_task, same pattern Picard's own AcoustID
    fingerprinting uses for fpcalc.
    """

    TITLE = "Scan File Health…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        if not files:
            return
        tagger_instance().window.set_statusbar_message(
            "Scanning file health for %(count)d file(s)…",
            {'count': len(files)},
            echo=None,
        )
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path: _scan_file_health_one(f.filename, p),
                lambda result=None, error=None, f=file: _file_health_scan_finished(f, result, error),
            )


class ScanTrackHealthAction(BaseAction):
    """Right-click action that triggers the Track Health scan on demand.

    Registered as a track-only action (see enable()) — Track Health's
    heavier perceptual scan is a right-side-only concern, evaluated in
    the context of a matched recording, unlike File Health's file-level
    structural check which applies everywhere.
    """

    TITLE = "Scan Track Health…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        if not files:
            return
        tagger_instance().window.set_statusbar_message(
            "Scanning track health for %(count)d file(s)…",
            {'count': len(files)},
            echo=None,
        )
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        thresholds = _thresholds_from_config(self.api.plugin_config)
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path, t=thresholds: _scan_track_health_one(f.filename, p, t),
                lambda result=None, error=None, f=file: _track_health_scan_finished(f, result, error),
            )


def _group_by_track(files: list[File]) -> dict[Track, list[File]]:
    """Group files by the Track Picard has already matched them to.

    Deliberately not re-deriving "same recording" from tags (AcoustID,
    recording MBID) — Picard already decided which files belong to the
    same track, using its own configured match_min_similarity/margin
    thresholds during Lookup/Scan. Reusing that decision means we never
    disagree with what the user already sees grouped together in the
    main window, and never need our own separate confidence threshold.
    """
    groups: dict[Track, list[File]] = {}
    for file in files:
        parent = getattr(file, 'parent_item', None)
        if isinstance(parent, Track):
            groups.setdefault(parent, []).append(file)
    return groups


def _tier_rank(file: File) -> tuple[int, int]:
    """Higher is better; (-1, -1) means not yet scanned. File Health
    ranks first — a structural defect is a hard, objective fact that
    should dominate a merely-perceptual Track Health difference between
    otherwise-tied files, not average out against it.
    """
    try:
        file_rank = FILE_TIERS.index(file.metadata['~health_file_tier'])
    except ValueError:
        return -1, -1
    try:
        track_rank = TRACK_TIERS.index(file.metadata['~health_track_tier'])
    except ValueError:
        track_rank = -1
    return file_rank, track_rank


def _read_metric(file: File, key: str) -> float | None:
    return _decode_metric(file.metadata[key] or '')



# --- Comparison-matrix stoplight logic ---

_STATE_COLORS: dict[str, tuple[QtGui.QColor, QtGui.QColor]] = {
    'pass': (QtGui.QColor('#c8e6c9'), QtGui.QColor('#1b5e20')),
    'amber': (QtGui.QColor('#ffe0b2'), QtGui.QColor('#8d5500')),
    'fail': (QtGui.QColor('#ffcdd2'), QtGui.QColor('#b71c1c')),
    'na': (QtGui.QColor('#eeeeee'), QtGui.QColor('#9e9e9e')),
}
_STATE_EMOJI: dict[str, str] = {'pass': '✅', 'amber': '⚠️', 'fail': '❌', 'na': '➖'}

# check label -> 3-4 letter tag matching its Options-page slider (see
# HealthOptionsPage). Fake Hi-Res has no dedicated slider of its own — it
# shares Spectral Cutoff's spectral-silence threshold.
_CHECK_TAGS: dict[str, str] = {
    'Clipping': 'CLP',
    'True Peak': 'TPK',
    'Spectral Cutoff': 'TRB',
    'Out-of-Phase': 'PHS',
    'Fake Hi-Res': 'HRS',
    'Noise Floor': 'NSF',
}
_CHECK_COLUMNS = list(_CHECK_TAGS)

_DR14_TAG = 'DYN'

_LOSSLESS_CODECS = {'flac', 'alac', 'wavpack', 'tta', 'ape'}


def _is_lossless_codec(codec: str) -> bool:
    return codec in _LOSSLESS_CODECS or codec.startswith('pcm_')


def _next_stricter_step(steps: list[tuple[float, str]], current: float) -> float | None:
    """The step past `current` in a slider's own lenient->strict ordering —
    "one notch stricter than wherever the slider sits right now."
    """
    values = [v for v, _ in steps]
    idx = min(range(len(values)), key=lambda i: abs(values[i] - current))
    return values[idx + 1] if idx + 1 < len(values) else None


def _gate_cell(
    value: float | None,
    applies: bool,
    current: float,
    steps: list[tuple[float, str]],
    fails: Callable[[float, float], bool],
    unit: str,
    na_reason: str,
    fail_reason: str,
    pass_reason: str,
) -> tuple[str, str]:
    """One gate check's cell: (state, tooltip). `fail_reason`/`pass_reason`
    are plain-language explanations of what this measurement actually
    means for the audio (e.g. "Audio is clipped — pushed past full volume
    and distorted"), not just the raw number versus the threshold — a
    number alone doesn't tell a general user why it matters. Amber means
    "would fail if the matching Options-page slider moved one notch
    stricter" — reuses the slider's own calibrated steps rather than a
    new invented margin, so the amber band moves live with the slider
    instead of sitting at a fixed offset unrelated to what the user
    actually configured.
    """
    if not applies or value is None:
        return 'na', na_reason
    if fails(value, current):
        return 'fail', f"{fail_reason} ({value:.1f}{unit}, past the {current:.1f}{unit} threshold)."
    next_stricter = _next_stricter_step(steps, current)
    if next_stricter is not None and fails(value, next_stricter):
        return 'amber', (
            f"{pass_reason} ({value:.1f}{unit}), but only just — one slider notch stricter "
            f"({next_stricter:.1f}{unit}) would flag this file."
        )
    return 'pass', f"{pass_reason} ({value:.1f}{unit}, comfortably clear of the {current:.1f}{unit} threshold)."


def _is_live_context(file: File) -> bool:
    """True when Picard's own matched-release metadata (or the track's
    own title) marks this as a live recording — used to add context to
    checks that can look like a defect on live material for legitimate
    reasons (PA/broadcast-feed roll-off, audience/room noise, a wider or
    less consistent stereo image from room mic placement) rather than
    actual damage or a lossy transcode.
    """
    release_types = [t.lower() for t in file.metadata.getall('releasetype')]
    if 'live' in release_types:
        return True
    title = (file.metadata['title'] or '').lower()
    return '(live' in title or '[live' in title


def _check_cells(file: File, thresholds: analysis.Thresholds) -> dict[str, tuple[str, str]]:
    """Every gate-check column's (state, tooltip) for one file, computed
    live from its stored raw measurements against the *current* slider
    settings — not the tier baked in at last scan time, which only
    updates on rescan since these are independent File Health/Track
    Health scans now (see DetailsPanel's docstring for that split).
    """
    channels = _read_metric(file, '~health_channels')
    peak_db = _read_metric(file, '~health_peak_db')
    sample_rate = _read_metric(file, '~health_sample_rate')
    has_signal = peak_db is not None and peak_db > analysis.MIN_PEAK_DB_FOR_SPECTRAL_CHECK
    is_hires = sample_rate is not None and sample_rate > analysis.FAKE_HIRES_MIN_SAMPLE_RATE_HZ

    cells: dict[str, tuple[str, str]] = {
        'Clipping': _gate_cell(
            _read_metric(file, '~health_clip_flat_factor'), True,
            thresholds.clip_flat_factor, _CLIP_STEPS, lambda v, t: v > t, "", "Not yet measured.",
            "Audio is clipped — pushed past full volume and distorted",
            "No clipping detected — audio stays cleanly under full volume",
        ),
        'True Peak': _gate_cell(
            _read_metric(file, '~health_true_peak_dbtp'), True,
            thresholds.true_peak_dbtp, _TRUE_PEAK_STEPS, lambda v, t: v >= t, "dB", "Not yet measured.",
            "Peaks between samples overshoot full volume — can distort on some playback equipment "
            "even though no single sample clips",
            "Peaks stay safely under full volume, including the overshoot that happens between samples",
        ),
        'Spectral Cutoff': _gate_cell(
            _read_metric(file, '~health_spectral_cutoff_db'), has_signal,
            thresholds.spectral_silence_db, _SPECTRAL_STEPS, lambda v, t: v < t, "dB",
            "Near-silent file — not enough signal to measure high-frequency content.",
            "No real sound above the cutoff frequency — likely converted from a lossy file (like an MP3) "
            "at some point" + (
                ", though a live recording's PA system or broadcast feed can also naturally roll off "
                "high frequencies without any lossy transcoding involved" if _is_live_context(file) else ""
            ),
            "Real high-frequency content extends above the cutoff, consistent with a genuine full-bandwidth source",
        ),
        'Fake Hi-Res': _gate_cell(
            _read_metric(file, '~health_hires_cutoff_db'), is_hires and has_signal,
            thresholds.spectral_silence_db, _SPECTRAL_STEPS, lambda v, t: v < t, "dB",
            "Not a hi-res-rate file — check doesn't apply." if not is_hires else
            "Near-silent file — not enough signal to measure.",
            "Labeled as hi-res audio but has no real content above the check frequency — likely stretched "
            "up from an ordinary file rather than genuine hi-res",
            "Real content exists above the check frequency, consistent with genuine hi-res audio",
        ),
        'Noise Floor': _gate_cell(
            _read_metric(file, '~health_noise_floor_db'), True,
            thresholds.noise_floor_db, _NOISE_FLOOR_STEPS, lambda v, t: v >= t, "dB",
            "Not yet measured, or the file has no quiet passage to measure.",
            "Background noise (hiss/static) is audible in the quietest passage — likely left over from an "
            "analog transfer or a noisy recording environment" + (
                ", though audience and room noise on a live recording is often the real cause rather than "
                "a bad transfer" if _is_live_context(file) else ""
            ),
            "Quiet passages are close to silent, with no audible background noise",
        ),
    }
    is_out_of_phase = _decode_bool(file.metadata['~health_is_out_of_phase'])
    if channels is not None and channels < 2:
        cells['Out-of-Phase'] = ('na', "Mono file — no second channel to compare.")
    elif is_out_of_phase is None:
        cells['Out-of-Phase'] = ('na', "Not yet measured.")
    elif is_out_of_phase:
        cells['Out-of-Phase'] = (
            'fail', "Left and right channels cancel out — will sound hollow or vanish entirely on mono speakers."
        )
    else:
        cells['Out-of-Phase'] = ('pass', "Channels are correlated normally.")
    return cells




def _dr14_band_cell(file: File, thresholds: analysis.Thresholds) -> tuple[str, str]:
    """Dynamic Range's cell has its own absolute Poor/Ok/Good/Great/
    Excellent band (it's one weighted input into the Track Health
    composite score — see analysis.compute_track_score/_dr14_score),
    colored by that band directly rather than relative to other files.
    """
    dr14 = _read_metric(file, '~health_dr14')
    if dr14 is None:
        return 'na', "Not yet scanned."
    poor = analysis.DR14_POOR_THRESHOLD - thresholds.dr14_shift
    ok = analysis.DR14_OK_THRESHOLD - thresholds.dr14_shift
    good = analysis.DR14_GOOD_THRESHOLD - thresholds.dr14_shift
    great = analysis.DR14_GREAT_THRESHOLD - thresholds.dr14_shift
    if dr14 < poor:
        state, band = 'fail', "Poor"
    elif dr14 < ok:
        state, band = 'amber', "Ok"
    elif dr14 < good:
        state, band = 'amber', "Good"
    elif dr14 < great:
        state, band = 'pass', "Great"
    else:
        state, band = 'pass', "Excellent"
    return state, f"Dynamic range: {band} (technical rating DR{int(dr14)})."


def _format_cell(file: File) -> tuple[str, str]:
    """Merges codec/bitrate/sample-rate/channels into the single reading
    people are already used to seeing together, e.g. "MP3 320 kbps · 44.1
    kHz · Stereo" or "FLAC (Lossless) · 96 kHz · Stereo".
    """
    codec = file.metadata['~health_codec_name'] or ''
    profile = file.metadata['~health_profile'] or ''
    bitrate = _read_metric(file, '~health_bitrate_kbps')
    sample_rate = _read_metric(file, '~health_sample_rate')
    channels = _read_metric(file, '~health_channels')
    if not codec:
        return "Not yet scanned", "No format information yet — scan this file first."

    codec_display = codec.upper()
    if 'he-aac' in profile.lower():
        codec_display += " (HE-AAC)"
    lossless = _is_lossless_codec(codec)
    if lossless:
        codec_part = f"{codec_display} (Lossless)"
    elif bitrate is not None:
        codec_part = f"{codec_display} {int(bitrate)} kbps"
    else:
        codec_part = codec_display
    sr_display = f"{sample_rate / 1000:.1f} kHz" if sample_rate else "?"
    ch_display = {1: "Mono", 2: "Stereo"}.get(
        int(channels) if channels is not None else -1,
        f"{int(channels)}ch" if channels is not None else "?",
    )
    text = f"{codec_part} · {sr_display} · {ch_display}"
    tooltip_bitrate = "Lossless" if lossless else (f"{int(bitrate)} kbps" if bitrate is not None else "unknown")
    tooltip = f"Codec: {codec_display}\nBitrate: {tooltip_bitrate}\nSample rate: {sr_display}\nChannels: {ch_display}"
    return text, tooltip


def _bandwidth_stereo_notes(file: File) -> list[str]:
    """Plain informational lines for Bandwidth and Stereo Coherence —
    continuous measurements with no absolute pass/fail meaning of their
    own (a naturally treble-light acoustic recording or an intentionally
    wide stereo mix would measure the same as a real defect), so they're
    shown as reference numbers in Notes rather than colored gate columns.
    Spectral Cutoff and Out-of-Phase already cover the real defect
    versions of "no real high end"/"channels don't correlate" with
    sharper, absolute logic.
    """
    notes: list[str] = []
    bandwidth_hz = _read_metric(file, '~health_bandwidth_hz')
    if bandwidth_hz is not None:
        notes.append(f"Bandwidth: real content up to ~{bandwidth_hz / 1000:.1f}kHz")
    coherence = _read_metric(file, '~health_stereo_coherence')
    if coherence is not None:
        note = f"Stereo image: {coherence:.2f} left/right correlation (1.0 = identical, 0.0 = fully independent)"
        if coherence < 0.5 and _is_live_context(file):
            note += " — a wider, less-correlated image is common on live recordings from room/audience mic'ing"
        notes.append(note)
    return notes


_FILE_ROLE = QtCore.Qt.ItemDataRole.UserRole


class SpectrogramDialog(QtWidgets.QDialog):
    """Shows one rendered spectrogram PNG, scrollable at native size.

    Deliberately dumb — no judgment, no thresholds, just the same
    evidence the numeric checks already computed, in a form a human can
    read in one glance (a cutoff wall, an elevated noise floor, an
    asymmetric stereo image) — see analysis.generate_spectrogram().
    """

    def __init__(self, title: str, image_path: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Spectrogram — {title}")
        pixmap = QtGui.QPixmap(image_path)
        label = QtWidgets.QLabel(self)
        label.setPixmap(pixmap)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidget(label)
        scroll.setWidgetResizable(False)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(scroll)
        # analysis.SPECTROGRAM_WIDTH/HEIGHT are sized so the actual
        # rendered PNG (requested size plus showspectrumpic's own fixed
        # axis/legend chrome) fits well under this ceiling — the clamp
        # is a safety net for unusual aspect ratios, not the normal path.
        self.resize(min(pixmap.width() + 40, 1100), min(pixmap.height() + 60, 800))


class DetailsPanel(QtWidgets.QDialog):
    """Non-modal panel showing File Health/Track Health details for any
    selected files — matched duplicates grouped together for comparison
    (same as before), but also singleton and completely unmatched files,
    since inspecting one file's own health doesn't require a duplicate
    to compare it against.

    Doesn't declare a winner — presents every file's File Tier, Track
    Tier, and issues side by side, per group, and only bolds whichever
    scored higher within its own group (File Health first, then Track
    Health — see _tier_rank) as a subtle cue. The user decides; we show
    the data.
    """

    def __init__(
        self,
        ffmpeg_path: str | None,
        thresholds: analysis.Thresholds,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Health Details")
        self.setModal(False)
        self.resize(1350, 420)
        self._ffmpeg_path = ffmpeg_path
        self._thresholds = thresholds
        # (label, files) per group, in display order — kept around so a scan
        # triggered from this panel can redraw in place without the caller
        # re-deriving track groupings or the user closing/reopening it.
        self._groups: list[tuple[str, list[File]]] = []

        layout = QtWidgets.QVBoxLayout(self)

        scan_row = QtWidgets.QHBoxLayout()
        self.scan_file_health_button = QtWidgets.QPushButton("Scan File Health", self)
        self.scan_file_health_button.setToolTip("Scan every file below for File Health — checks the file itself for damage or corruption.")
        self.scan_file_health_button.clicked.connect(self._scan_file_health)
        self.scan_track_health_button = QtWidgets.QPushButton("Scan Track Health", self)
        self.scan_track_health_button.setToolTip("Scan every file below for Track Health — checks how the audio actually sounds.")
        self.scan_track_health_button.clicked.connect(self._scan_track_health)
        scan_row.addWidget(self.scan_file_health_button)
        scan_row.addWidget(self.scan_track_health_button)
        scan_row.addStretch(1)
        layout.addLayout(scan_row)

        self.tree = QtWidgets.QTreeWidget(self)
        full_names = ['File', 'File Tier', 'Track Tier', 'Format'] + _CHECK_COLUMNS + ['Dynamic Range', 'Notes']
        headers = ['File', 'File Tier', 'Track Tier', 'Format']
        headers += [_CHECK_TAGS[c] for c in _CHECK_COLUMNS]
        headers += [_DR14_TAG]
        headers += ['Notes']
        self.tree.setHeaderLabels(headers)
        for col, full_name in enumerate(full_names):
            self.tree.headerItem().setToolTip(col, full_name)
        self.tree.setColumnWidth(0, 190)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 70)
        self.tree.setColumnWidth(3, 220)
        for col in range(4, len(headers) - 1):
            self.tree.setColumnWidth(col, 55)
        self.tree.setColumnWidth(len(headers) - 1, 280)
        self.tree.setRootIsDecorated(True)
        self.tree.itemSelectionChanged.connect(self._update_button_states)
        self.tree.itemDoubleClicked.connect(lambda *_: self._show_in_list())
        layout.addWidget(self.tree)

        action_row = QtWidgets.QHBoxLayout()
        self.show_button = QtWidgets.QPushButton("Show in List", self)
        self.show_button.clicked.connect(self._show_in_list)
        self.spectrogram_button = QtWidgets.QPushButton("Spectrogram…", self)
        self.spectrogram_button.setToolTip(
            "Show a visual picture of the sound — an easy way to spot problems like a "
            "missing top end, background noise, or an unbalanced left/right mix, rather "
            "than trusting a single number."
        )
        self.spectrogram_button.clicked.connect(self._show_spectrogram)
        self.remove_button = QtWidgets.QPushButton("Remove from Picard", self)
        self.remove_button.clicked.connect(self._remove_from_picard)
        self.trash_button = QtWidgets.QPushButton("Move to Trash…", self)
        self.trash_button.clicked.connect(self._trash_file)
        action_row.addWidget(self.show_button)
        action_row.addWidget(self.spectrogram_button)
        action_row.addWidget(self.remove_button)
        action_row.addWidget(self.trash_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._update_button_states()
        self._update_scan_button_states()

    def add_group(self, key: str, group: list[File]) -> None:
        self._groups.append((key, group))
        self._render_group(key, group)
        self._update_scan_button_states()

    def refresh(self) -> None:
        """Redraws every group from current file metadata — called after a
        scan triggered from this panel's own buttons finishes, so results
        land in place instead of requiring the user to close and reopen.
        """
        selected = self._current_file()
        self.tree.clear()
        for key, group in self._groups:
            self._render_group(key, group)
        if selected is not None:
            self._select_file(selected)
        self._update_scan_button_states()

    def _render_group(self, key: str, group: list[File]) -> None:
        header = QtWidgets.QTreeWidgetItem([key])
        header.setFirstColumnSpanned(True)
        italic = header.font(0)
        italic.setItalic(True)
        header.setFont(0, italic)
        self.tree.addTopLevelItem(header)

        # Only bolds a lone best-tiered file as a subtle cue — never
        # picks a favorite among files tied on both File Tier and Track
        # Tier (see HANDOFF.md's "Session 5" for why the old weighted
        # tie-break was removed: it doesn't make sense once File/Track
        # Health are fully independent absolute scores, not a ranking).
        ranks = {file: _tier_rank(file) for file in group}
        best_rank = max(ranks.values())
        top_files = [f for f in group if ranks[f] == best_rank]
        winner = top_files[0] if (len(group) >= 2 and best_rank != (-1, -1) and len(top_files) == 1) else None

        for file in group:
            file_tier = file.metadata['~health_file_tier'] or "Not yet scanned"
            # An Unplayable file has no Track Health to show (see
            # analysis.analyze_track_health's docstring) — nothing to
            # measure perceptually, distinct from "not yet scanned".
            if file.metadata['~health_track_tier']:
                track_tier = file.metadata['~health_track_tier']
            elif file.metadata['~health_file_tier'] == analysis.FILE_TIER_BROKEN:
                track_tier = "—"
            else:
                track_tier = "Not yet scanned"
            format_text, format_tooltip = _format_cell(file)
            item = QtWidgets.QTreeWidgetItem([file.base_filename, file_tier, track_tier, format_text])
            item.setToolTip(3, format_tooltip)
            item.setData(0, _FILE_ROLE, file)

            col = 4
            check_cells = _check_cells(file, self._thresholds)
            for check_name in _CHECK_COLUMNS:
                state, tooltip = check_cells[check_name]
                self._paint_matrix_cell(item, col, state, tooltip)
                col += 1
            dr14_state, dr14_tooltip = _dr14_band_cell(file, self._thresholds)
            self._paint_matrix_cell(item, col, dr14_state, dr14_tooltip)
            col += 1

            info_parts = []
            for info_key in ('~health_file_info', '~health_track_info'):
                if file.metadata[info_key]:
                    info_parts.extend(file.metadata[info_key].split("; "))
            info_parts.extend(_bandwidth_stereo_notes(file))
            if file.metadata['~health_file_changed_since_scan']:
                info_parts.insert(0, "File Health: changed since last scan")
            if file.metadata['~health_track_changed_since_scan']:
                info_parts.insert(0, "Track Health: changed since last scan")
            notes = "; ".join(info_parts)
            item.setText(col, notes or "—")
            if notes:
                item.setToolTip(col, notes.replace("; ", "\n"))

            if file is winner:
                bold = item.font(0)
                bold.setBold(True)
                item.setFont(0, bold)
                item.setFont(1, bold)
                item.setFont(2, bold)
            header.addChild(item)
        header.setExpanded(True)

    def _paint_matrix_cell(self, item: QtWidgets.QTreeWidgetItem, col: int, state: str, tooltip: str) -> None:
        item.setText(col, _STATE_EMOJI[state])
        item.setTextAlignment(col, QtCore.Qt.AlignmentFlag.AlignCenter)
        bg, fg = _STATE_COLORS[state]
        item.setBackground(col, QtGui.QBrush(bg))
        item.setForeground(col, QtGui.QBrush(fg))
        item.setToolTip(col, tooltip)

    def _select_file(self, file: File) -> None:
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                child = header.child(j)
                if child.data(0, _FILE_ROLE) is file:
                    self.tree.setCurrentItem(child)
                    return

    def _all_files(self) -> list[File]:
        return [file for _, group in self._groups for file in group]

    def _run_file_health_scan(self, files: list[File]) -> None:
        if not files:
            return
        tagger_instance().window.set_statusbar_message(
            "Scanning file health for %(count)d file(s)…",
            {'count': len(files)},
            echo=None,
        )
        ffmpeg_path = self._ffmpeg_path
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path: _scan_file_health_one(f.filename, p),
                lambda result=None, error=None, f=file: self._on_file_health_scan_finished(f, result, error),
            )

    def _run_track_health_scan(self, files: list[File]) -> None:
        if not files:
            return
        tagger_instance().window.set_statusbar_message(
            "Scanning track health for %(count)d file(s)…",
            {'count': len(files)},
            echo=None,
        )
        ffmpeg_path = self._ffmpeg_path
        thresholds = self._thresholds
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path, t=thresholds: _scan_track_health_one(f.filename, p, t),
                lambda result=None, error=None, f=file: self._on_track_health_scan_finished(f, result, error),
            )

    def _on_file_health_scan_finished(self, file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
        _file_health_scan_finished(file, result, error)
        self.refresh()

    def _on_track_health_scan_finished(self, file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
        _track_health_scan_finished(file, result, error)
        self.refresh()

    def _scan_file_health(self) -> None:
        self._run_file_health_scan(self._all_files())

    def _scan_track_health(self) -> None:
        self._run_track_health_scan(self._all_files())

    def _update_scan_button_states(self) -> None:
        has_files = bool(self._all_files())
        self.scan_file_health_button.setEnabled(has_files)
        self.scan_track_health_button.setEnabled(has_files)

    def _current_file(self) -> File | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, _FILE_ROLE)

    def _update_button_states(self) -> None:
        has_file = self._current_file() is not None
        self.show_button.setEnabled(has_file)
        self.spectrogram_button.setEnabled(has_file)
        self.remove_button.setEnabled(has_file)
        self.trash_button.setEnabled(has_file)

    def _remove_row_for(self, file: File) -> None:
        self._groups = [(key, [f for f in group if f is not file]) for key, group in self._groups]
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                if header.child(j).data(0, _FILE_ROLE) is file:
                    header.removeChild(header.child(j))
                    return

    def _show_in_list(self) -> None:
        """Select and scroll to this file in whichever tree is showing it.

        Solves the correlation problem directly instead of relying on the
        user visually matching names — once files are matched to a track,
        the main window's own list shows the shared track title, not the
        filename, so eyeballing which row is which isn't reliable.
        """
        file = self._current_file()
        if file is None:
            return
        ui_item = file.ui_item
        if ui_item is None:
            return
        tree = ui_item.treeWidget()
        if tree is None:
            return
        tree.setCurrentItem(ui_item)
        tree.scrollToItem(ui_item)
        tree.setFocus()

    def _show_spectrogram(self) -> None:
        """Renders on a background thread (a real ffmpeg decode + image
        render, same as a scan) and opens a SpectrogramDialog on success.
        The rendered PNG lives in a temp file only long enough for
        QPixmap's constructor to read it — that read is synchronous, so
        it's safe to delete right after building the dialog.
        """
        file = self._current_file()
        if file is None:
            return
        tagger_instance().window.set_statusbar_message(
            "Rendering spectrogram for %(file)s…",
            {'file': file.base_filename},
            echo=None,
        )
        ffmpeg_path = self._ffmpeg_path
        filename = file.filename
        title = file.base_filename

        def render() -> str | None:
            fd, path = tempfile.mkstemp(suffix='.png', prefix='file_health_spectrogram_')
            os.close(fd)
            if analysis.generate_spectrogram(filename, path, ffmpeg_path=ffmpeg_path):
                return path
            try:
                os.remove(path)
            except OSError:
                pass
            return None

        def on_done(result: str | None = None, error: BaseException | None = None) -> None:
            if error is not None or result is None:
                tagger_instance().window.set_statusbar_message(
                    "File Health: couldn't render a spectrogram for %(file)s",
                    {'file': title},
                    echo=None,
                )
                return
            dialog = SpectrogramDialog(title, result, self)
            dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.show()
            try:
                os.remove(result)
            except OSError:
                pass

        run_task(render, on_done)

    def _remove_from_picard(self) -> None:
        file = self._current_file()
        if file is None:
            return
        tagger_instance().remove([file])
        self._remove_row_for(file)

    def _trash_file(self) -> None:
        file = self._current_file()
        if file is None:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "Move to Trash",
            f"Move {file.base_filename} to the system trash?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            tagger_instance().trash_files([file])
            self._remove_row_for(file)


def _all_loaded_files() -> list[File]:
    """Every file currently in Picard: unclustered, in a cluster, or matched
    into an album track. Lets the library-wide compare action find every
    duplicate-track group without the user hand-picking files first.
    """
    tagger = tagger_instance()
    files = list(tagger.unclustered_files.files)
    for cluster in tagger.clusters:
        files.extend(cluster.files)
    for album in tagger.albums.values():
        for track in album.tracks:
            files.extend(track.files)
    return files


def _track_label(track: Track) -> str:
    title = track.metadata['title'] or "Unknown title"
    artist = track.metadata['artist']
    return f"{artist} – {title}" if artist else title


def _group_files_for_details(files: list[File]) -> list[tuple[str, list[File]]]:
    """Groups by Track when matched (preserves duplicate-comparison —
    same Track means Picard already considers them the same recording),
    otherwise one singleton group per unmatched file — there's nothing
    to group them by, but the whole point of the Details window is
    being able to inspect *any* file's health, not just ones with a
    confirmed duplicate.
    """
    by_track = _group_by_track(files)
    groups: list[tuple[str, list[File]]] = []
    grouped_files: set[File] = set()
    for track, group in by_track.items():
        groups.append((_track_label(track), group))
        grouped_files.update(group)
    for f in files:
        if f not in grouped_files:
            groups.append((f.base_filename, [f]))
    return groups


def _open_details_panel(
    files: list[File],
    parent: QtWidgets.QWidget,
    ffmpeg_path: str | None,
    thresholds: analysis.Thresholds,
) -> DetailsPanel | None:
    """Groups by Track where matched, singleton otherwise (see
    _group_files_for_details), opens the panel if any files were given.
    """
    groups = _group_files_for_details(files)
    if not groups:
        return None
    panel = DetailsPanel(ffmpeg_path, thresholds, parent)
    for label, group in groups:
        panel.add_group(label, group)
    panel.show()
    return panel


class ShowFileHealthDetailsAction(BaseAction):
    """Right-click action that opens the File Health Details window for
    the current selection — any files, not just ones with a confirmed
    duplicate. Files matched to the same Track are still grouped
    together for side-by-side comparison (Picard's own matching
    decision, not our own tag comparison); everything else gets its own
    row.

    Doesn't declare a hard winner in the results — only bolds whichever
    file scored higher within its group (File Health tier first, then
    Track Health tier) as a subtle cue when one file is unambiguously
    ahead; ties are left unbolded rather than broken by a secondary
    ranking, since File/Track Health are independent absolute scores,
    not a comparison. Matches the design decided earlier: a
    perceptual-distance metric like ViSQOL/Zimtohrli would tell you the
    files differ, but not which one is better; the directional gate-
    field reasons and continuous fidelity axes (real, from
    analysis.analyze_file_health/analyze_track_health, stored in
    ~health_file_flags/~health_track_flags and the ~health_* metric
    fields) are what actually inform that judgment.
    """

    TITLE = "File Health Details…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        window = tagger_instance().window
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        thresholds = _thresholds_from_config(self.api.plugin_config)
        panel = _open_details_panel(files, window, ffmpeg_path, thresholds)
        if panel is None:
            window.set_statusbar_message("No files selected.", echo=None)
            return
        self._panel = panel


class ShowAllFileHealthDetailsAction(BaseAction):
    """Tools-menu action: opens the File Health Details window for every
    file currently loaded in Picard at once — the "whole library" case,
    not limited to a manual selection.
    """

    TITLE = "All File Health Details…"

    def callback(self, objs) -> None:
        window = tagger_instance().window
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        thresholds = _thresholds_from_config(self.api.plugin_config)
        panel = _open_details_panel(_all_loaded_files(), window, ffmpeg_path, thresholds)
        if panel is None:
            window.set_statusbar_message("No files currently loaded in Picard.", echo=None)
            return
        self._panel = panel


class FileHealthProvider(ColumnValueProvider, DelegateProvider):
    """Column that displays File Health tier as a bookmark icon with an
    itemized-issues tooltip. Registered on both sides of the main window
    (see enable()) — File Health's structural check is always relevant
    regardless of matching state.
    """

    def __init__(self) -> None:
        self._delegate_class = FileHealthColumnDelegate

    def evaluate(self, obj: Item) -> str:
        """Tier index as a string, for sorting worst-to-best."""
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return "-1"
        try:
            return str(FILE_TIERS.index(column_method('~health_file_tier')))
        except ValueError:
            return "-1"

    def get_health_info(self, obj: Item) -> dict[str, object] | None:
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return None
        tier = column_method('~health_file_tier')
        if not tier:
            return None
        stored_flags = column_method('~health_file_flags')
        stored_info = column_method('~health_file_info')
        return {
            'tier': tier,
            'file_tier': tier,
            'issues': stored_flags.split("; ") if stored_flags else [],
            'info': stored_info.split("; ") if stored_info else [],
            'changed_since_scan': bool(column_method('~health_file_changed_since_scan')),
        }

    def get_delegate_class(self) -> type[QtWidgets.QStyledItemDelegate]:
        return self._delegate_class


class TrackHealthProvider(ColumnValueProvider, DelegateProvider):
    """Column that displays Track Health tier as a bookmark icon with an
    itemized-issues tooltip. Registered on the right side only (see
    enable()) — Track Health is evaluated in the context of a matched
    recording, unlike File Health's file-level structural check.
    """

    def __init__(self) -> None:
        self._delegate_class = TrackHealthColumnDelegate

    def evaluate(self, obj: Item) -> str:
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return "-1"
        try:
            return str(TRACK_TIERS.index(column_method('~health_track_tier')))
        except ValueError:
            return "-1"

    def get_health_info(self, obj: Item) -> dict[str, object] | None:
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return None
        file_tier = column_method('~health_file_tier')
        track_tier = column_method('~health_track_tier')
        if not file_tier and not track_tier:
            return None
        stored_flags = column_method('~health_track_flags')
        stored_info = column_method('~health_track_info')
        return {
            'tier': track_tier or None,
            'file_tier': file_tier or None,
            'issues': stored_flags.split("; ") if stored_flags else [],
            'info': stored_info.split("; ") if stored_info else [],
            'changed_since_scan': bool(column_method('~health_track_changed_since_scan')),
        }

    def get_delegate_class(self) -> type[QtWidgets.QStyledItemDelegate]:
        return self._delegate_class


class _SingleHealthColumnDelegate(QtWidgets.QStyledItemDelegate):
    """Shared single-icon paint + tooltip logic for one health tier
    column. Subclasses set `_icon_level_map`/`_label` for which tier
    scale and heading text to use.
    """

    _icon_level_map: dict[str, int] = {}
    _label: str = ""

    def _get_info(self, index: QtCore.QModelIndex) -> dict[str, object] | None:
        tree_widget = self.parent()
        if not tree_widget:
            return None
        item = tree_widget.itemFromIndex(index)
        obj = getattr(item, 'obj', None)
        if not obj:
            return None
        columns = getattr(item, 'columns', None)
        column_index = index.column()
        if not columns or column_index >= len(columns):
            return None
        provider = getattr(columns[column_index], 'delegate_provider', None)
        if provider is None:
            return None
        return provider.get_health_info(obj)

    def paint(
        self,
        painter: QtGui.QPainter | None,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        self.initStyleOption(option, index)
        if painter is None:
            return
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            painter.fillRect(option.rect, option.palette.base())

        info = self._get_info(index)
        if not info:
            return
        level = self._icon_level_map.get(info.get('tier'))
        if level is None:
            return
        icon_size = 16
        x = option.rect.x() + (option.rect.width() - icon_size) // 2
        y = option.rect.y() + (option.rect.height() - icon_size) // 2
        match_icons[level].paint(painter, QtCore.QRect(x, y, icon_size, icon_size))

    def _format_tooltip(self, info: dict[str, object]) -> str:
        tier = info.get('tier')
        issues = info['issues']
        notes = info.get('info') or []
        if tier is not None:
            header = f"<b>{self._label}: {tier}</b>"
        elif info.get('file_tier') == analysis.FILE_TIER_BROKEN:
            header = f"<b>{self._label}: N/A</b> — file can't be played"
        else:
            header = f"<b>{self._label}: not yet scanned</b>"
        parts = [header]
        if info.get('changed_since_scan'):
            parts.append(
                "<div style='color:#b7950b;'>Audio content changed since last scan</div>"
            )
        if issues:
            items = "".join(f"<li>{issue}</li>" for issue in issues)
            parts.append(f"<ul style='margin-left:-20px;'>{items}</ul>")
        elif tier is not None and not notes:
            parts.append("<br>No issues detected")
        if notes:
            # Informational only — doesn't affect the tier (e.g. mono
            # content in a stereo container isn't a defect, just a note).
            note_items = "".join(f"<li>{note}</li>" for note in notes)
            parts.append(
                f"<div style='color:#7f8c8d;'>Note:<ul style='margin-left:-20px;'>{note_items}</ul></div>"
            )
        return f"<div style='white-space:nowrap;'>{''.join(parts)}</div>"

    def helpEvent(
        self,
        event: QtGui.QHelpEvent | None,
        view: QtWidgets.QAbstractItemView | None,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> bool:
        info = self._get_info(index)
        if not info or event is None:
            return False
        QtWidgets.QToolTip.showText(event.globalPos(), self._format_tooltip(info), view)
        return True

    def sizeHint(
        self,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> QtCore.QSize:
        return QtCore.QSize(50, 20)


class FileHealthColumnDelegate(_SingleHealthColumnDelegate):
    _icon_level_map = FILE_TIER_ICON_LEVEL
    _label = "File Health"


class TrackHealthColumnDelegate(_SingleHealthColumnDelegate):
    _icon_level_map = TRACK_TIER_ICON_LEVEL
    _label = "Track Health"


# Registered at import time, not inside enable(), to match how Picard's own
# core columns register (ALBUMVIEW_COLUMNS.insert(...) at columns.py import
# time) — this must happen before any tree view builds its header, which
# plugin enable() apparently runs too late for.
_FILE_HEALTH_COLUMN = make_delegate_column(
    "File Health",
    '~health_file_tier',
    FileHealthProvider(),
    width=50,
    size=QtCore.QSize(40, 16),
)
_FILE_HEALTH_COLUMN.is_default = True
registry.register(_FILE_HEALTH_COLUMN, add_to={'FILE_VIEW', 'ALBUM_VIEW'})

# Right side (ALBUM_VIEW) only — Track Health is evaluated in the
# context of a matched recording; the left/unmatched-file view never
# shows this column at all (see enable()'s action registration for the
# matching menu-item split).
_TRACK_HEALTH_COLUMN = make_delegate_column(
    "Track Health",
    '~health_track_tier',
    TrackHealthProvider(),
    width=50,
    size=QtCore.QSize(40, 16),
)
_TRACK_HEALTH_COLUMN.is_default = True
registry.register(_TRACK_HEALTH_COLUMN, add_to={'ALBUM_VIEW'})

_HEALTH_COLUMNS = (
    (_FILE_HEALTH_COLUMN, FileHealthColumnDelegate),
    (_TRACK_HEALTH_COLUMN, TrackHealthColumnDelegate),
)


def _install_delegate_on_live_views() -> None:
    """Attach the icon-painting delegates to any already-open tree widgets.

    ``setItemDelegateForColumn`` is only ever called once, inside each tree
    view's own ``_init_header()``, over whatever columns existed at that
    exact moment. Adding a column afterward (which is the only timing a
    plugin can realistically achieve) grows the header and column count via
    ``header_events.headers_updated``, but never gets a delegate wired up on
    an already-open window — so the cell falls back to Qt's default (text)
    renderer, which has nothing to paint for a delegate column. This
    manually finishes that wiring on whatever tree widgets already exist.
    """
    app = QtWidgets.QApplication.instance()
    if not app:
        return
    for widget in app.allWidgets():
        columns = getattr(widget, 'columns', None)
        if columns is None:
            continue
        columns_list = list(columns)
        set_delegate = getattr(widget, 'setItemDelegateForColumn', None)
        if not callable(set_delegate):
            continue
        for column, delegate_class in _HEALTH_COLUMNS:
            try:
                index = columns_list.index(column)
            except ValueError:
                continue
            set_delegate(index, delegate_class(widget))


def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    api.logger.info("File Health enabled")

    # Picard's own match_icons list is populated lazily; Picard core likely
    # already loaded it, but don't rely on load order — reusing Picard's
    # own bookmark icons (not new plugin-bundled assets) needs this.
    load_match_icons()
    api.register_script_variable(
        '_health_file_tier',
        documentation="File Health tier from the last scan (Unplayable/Bad/OK/Good/Excellent).",
        title="File Health",
    )
    api.register_script_variable(
        '_health_track_tier',
        documentation="Track Health tier from the last scan (Bad/OK/Good/Excellent); empty if the file is Unplayable.",
        title="Track Health",
    )
    api.register_script_variable(
        '_health_file_flags',
        documentation="Itemized list of structural (File Health) issues found by the last scan.",
        title="File Health flags",
    )
    api.register_script_variable(
        '_health_file_info',
        documentation="Informational notes from the last File Health scan that don't affect its tier.",
        title="File Health info",
    )
    api.register_script_variable(
        '_health_track_info',
        documentation="Informational notes from the last Track Health scan that don't affect its tier (e.g. mono content in a stereo container).",
        title="Track Health info",
    )
    api.register_script_variable(
        '_health_file_content_hash',
        documentation="Hash of the file's bytes as of the last File Health scan.",
        title="File Health content hash",
    )
    api.register_script_variable(
        '_health_track_content_hash',
        documentation="Hash of the file's bytes as of the last Track Health scan.",
        title="Track Health content hash",
    )
    api.register_script_variable(
        '_health_file_changed_since_scan',
        documentation="Non-empty if the file's bytes changed since the last File Health scan.",
        title="File Health changed since scan",
    )
    api.register_script_variable(
        '_health_track_changed_since_scan',
        documentation="Non-empty if the file's bytes changed since the last Track Health scan.",
        title="Track Health changed since scan",
    )
    api.plugin_config.register_option('auto_scan', False)
    api.plugin_config.register_option('ffmpeg_path', '')
    api.plugin_config.register_option('clip_flat_factor', analysis.MIN_FLAT_FACTOR_FOR_CLIPPING)
    api.plugin_config.register_option('true_peak_dbtp', analysis.TRUE_PEAK_THRESHOLD_DBTP)
    api.plugin_config.register_option('spectral_silence_db', analysis.SPECTRAL_SILENCE_THRESHOLD_DB)
    api.plugin_config.register_option('phase_angle_deg', analysis.PHASE_OUT_OF_PHASE_ANGLE_DEG)
    api.plugin_config.register_option('dr14_shift', 0.0)
    api.plugin_config.register_option('noise_floor_db', analysis.NOISE_FLOOR_THRESHOLD_DB)
    api.register_file_post_load_processor(_maybe_auto_scan)
    api.register_file_post_addition_to_track_processor(_restore_health_metadata_on_match)
    api.register_file_post_removal_from_track_processor(_restore_health_metadata_on_match)
    api.register_options_page(HealthOptionsPage)

    # File Health: available everywhere — unmatched files/clusters on
    # the left, and matched files/tracks on the right. This is the ONLY
    # scan action the left side ever shows (see the column registration
    # above for the matching column-visibility split).
    api.register_file_action(ScanFileHealthAction)
    api.register_cluster_action(ScanFileHealthAction)
    api.register_track_action(ScanFileHealthAction)

    # Track Health and Details: right side (matched-track context) only.
    api.register_track_action(ScanTrackHealthAction)
    api.register_track_action(ShowFileHealthDetailsAction)
    api.register_tools_menu_action(ShowAllFileHealthDetailsAction)

    # Force any already-open tree views to rebuild their header (column
    # count + labels) and recompute every existing row's cell text for the
    # new column. Without this, the column is registered and even shows as
    # checked in the header menu, but never actually renders — the tree
    # widget's Qt column count is fixed at construction and isn't rebuilt
    # just by mutating the shared columns list or toggling visibility.
    #
    # Deferred by one event-loop tick (singleShot(0, ...)) rather than
    # emitted immediately: enable() runs synchronously during startup, and
    # whether MainWindow's tree views already exist (and are already
    # connected to this signal) at that exact point is not guaranteed —
    # an immediate emit with zero listeners connected yet is a silent
    # no-op, not queued for later delivery.
    QtCore.QTimer.singleShot(0, header_events.headers_updated.emit)
    QtCore.QTimer.singleShot(0, _install_delegate_on_live_views)


def disable() -> None:
    """Called when the plugin is disabled."""
    registry.unregister(_FILE_HEALTH_COLUMN.key)
    registry.unregister(_TRACK_HEALTH_COLUMN.key)
