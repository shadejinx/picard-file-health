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
# from_score constants exactly. File Health has a terminal `Broken`
# tier Track Health can't reach (a file that won't decode has nothing
# to measure perceptually — see analysis.analyze_file's docstring).
FILE_TIERS = ("Broken", "Bad", "Good", "Great", "Excellent")
TRACK_TIERS = ("Bad", "Good", "Great", "Excellent")

# picard.ui.match_icons ships 6 bookmark levels (0=worst red, 5=best
# green). Mapped explicitly rather than evenly spread across all 6 so
# the visual jump from a real defect (Bad) to a clean file (Good) stays
# the same big red->green jump it was under the old 6-tier scale, with
# only the top three tiers (Good/Great/Excellent) using the finer
# upper-range distinctions. Track Health omits level 0 entirely — a
# perceptual "Bad" is still a real defect, but nothing it measures is
# as unambiguous as File Health's Broken (a file that won't even play).
FILE_TIER_ICON_LEVEL = {"Broken": 0, "Bad": 1, "Good": 3, "Great": 4, "Excellent": 5}
TRACK_TIER_ICON_LEVEL = {"Bad": 1, "Good": 3, "Great": 4, "Excellent": 5}


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


class _WeightSlider(QtWidgets.QFrame):
    """A plain 0-10 importance slider for one axis of the compare
    panel's weighted tie-break (_composite_winner in this module).

    `bands` is an ascending list of (min_value, hint) pairs — the
    slider shows whichever band's threshold the current value has
    reached or passed, live as it moves. Coarser than
    _SensitivitySlider's one-hint-per-step (11 distinct positions here
    don't each carry a meaningfully distinct story the way a gate
    threshold's calibrated steps do), but still says in plain language
    what a given weight actually does to the ranking rather than
    leaving the user to guess what "7/10" means.
    """

    def __init__(
        self,
        title: str,
        bands: tuple[tuple[int, str], ...],
        parent: QtWidgets.QWidget | None = None,
        tag: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._bands = bands
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
            tag_label = QtWidgets.QLabel(f"({tag})", self)
            tag_label.setStyleSheet("color: palette(mid);")
            header.addWidget(tag_label)
        self.value_label = QtWidgets.QLabel(self)
        header.addStretch(1)
        header.addWidget(self.value_label)
        layout.addLayout(header)

        self.slider = _NoWheelSlider(QtCore.Qt.Orientation.Horizontal, self)
        self.slider.setMinimum(0)
        self.slider.setMaximum(10)
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

        self.set_value(5)

    def _on_changed(self, value: int) -> None:
        self.value_label.setText("Ignored" if value == 0 else f"{value}/10")
        hint = ""
        for threshold, text in self._bands:
            if value >= threshold:
                hint = text
        self.hint_label.setText(hint)

    def value(self) -> int:
        return self.slider.value()

    def set_value(self, value: int) -> None:
        self.slider.setValue(value)


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
    (2.0, "Catches clearly audible inter-sample overshoot."),
    (1.5, "Catches overshoot well beyond normal mastering tolerance."),
    (1.0, "Catches overshoot beyond typical mastering headroom."),
    (0.8, "Slightly stricter than the meter's own accuracy margin."),
    (0.6, "Balanced default — just past the true-peak meter's own accuracy limit."),
    (0.4, "Tighter than the meter's documented accuracy — may flag compliant masters."),
    (0.2, "Close to full scale — likely to flag normally-mastered loud tracks."),
    (0.1, "Right at the meter's own noise floor — expect false positives."),
    (0.0, "Flags anything technically over full scale, including measurement noise."),
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


# Shifts analysis.DR14_POOR_THRESHOLD/OK/GOOD/GREAT together (see
# analysis.Thresholds.dr14_shift) rather than exposing four independent
# band-boundary sliders — keeps the manual-documented relative spacing
# between bands intact and just moves the whole scale's anchor point,
# instead of risking the bands crossing each other or drifting apart
# from what the TT DR Offline Meter manual actually documented.
# Lenient (positive shift) -> strict (negative shift), matching every
# other slider's left-to-right convention.
_DR14_SHIFT_STEPS: list[tuple[float, str]] = [
    (4.0, "Very lenient — a DR4 master won't read as Poor."),
    (3.0, "Lenient — a DR5 master won't read as Poor."),
    (2.0, "Somewhat lenient — a DR6 master won't read as Poor."),
    (1.0, "Slightly lenient — a DR7 master won't read as Poor."),
    (0.0, "Balanced default — matches the TT DR Offline Meter manual's own documented scale."),
    (-1.0, "Slightly stricter — needs DR9 to clear \"Poor\"."),
    (-2.0, "Stricter — needs DR10 to clear \"Poor\"."),
    (-3.0, "Strict — needs DR11 to clear \"Poor\"."),
    (-4.0, "Very strict — only DR12+ masters avoid \"Poor\"."),
]
_DR14_SHIFT_DEFAULT_INDEX = 4  # matches analysis.Thresholds.dr14_shift default (0)


# Comparison Priority weight-slider hints — banded (not one hint per
# integer step) since "how important is this" doesn't have 11 distinct
# stories the way a calibrated gate threshold does, but each band still
# says in plain language what that weight actually does to the ranking.
_RANK_BAND_TEMPLATE = (
    (0, "Ignored — {noun} won't affect which copy wins a tie."),
    (1, "Slight factor — only tips a tie when everything else is dead even."),
    (4, "Meaningful factor — {comparative} has a real edge."),
    (7, "Dominant factor — {comparative} usually wins outright."),
)


def _rank_bands(noun: str, comparative: str) -> tuple[tuple[int, str], ...]:
    return tuple((threshold, text.format(noun=noun, comparative=comparative)) for threshold, text in _RANK_BAND_TEMPLATE)


_BANDWIDTH_WEIGHT_BANDS = _rank_bands("bandwidth", "the file with more real high-frequency content")
_NOISE_FLOOR_WEIGHT_BANDS = _rank_bands("noise floor", "the quieter (cleaner) file")
_DR14_WEIGHT_BANDS = _rank_bands("dynamic range", "the less-compressed (more dynamic) file")
_COHERENCE_WEIGHT_BANDS = _rank_bands("stereo coherence", "the file with a more consistent stereo image")


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
    )


def _rank_weights_from_config(plugin_config) -> dict[str, int]:
    return {
        'rank_weight_bandwidth': plugin_config['rank_weight_bandwidth'],
        'rank_weight_noise_floor': plugin_config['rank_weight_noise_floor'],
        'rank_weight_dr14': plugin_config['rank_weight_dr14'],
        'rank_weight_coherence': plugin_config['rank_weight_coherence'],
    }


def _scan_one(filename: str, ffmpeg_path: str | None, thresholds: analysis.Thresholds) -> dict[str, object]:
    """Runs on a background thread — real decode + measurement work via
    ffmpeg (see analysis.py), not simulated.
    """
    try:
        result = analysis.analyze_file(filename, ffmpeg_path=ffmpeg_path, thresholds=thresholds)
    except analysis.FfmpegNotFoundError as exc:
        # Includes FfmpegVersionTooOldError: ffmpeg itself is missing or
        # unusable — an environment problem, fixed in Options, not a
        # per-file result. A single specific file failing to decode is
        # no longer an exception (see analysis.analyze_file's docstring)
        # — it's the real `file_tier="Broken"` result handled below.
        return {'error': str(exc)}
    return {
        'file_tier': result.file_tier,
        'track_tier': result.track_tier,
        'file_flags': "; ".join(result.file_issues),
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
        'codec_name': result.stream_info.codec_name,
        'profile': result.stream_info.profile,
        'bitrate_kbps': result.stream_info.bitrate_kbps,
        'sample_rate': result.stream_info.sample_rate,
        'channels': result.stream_info.channels,
        'peak_db': result.peak_db,
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



def _scan_finished(file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
    """Runs back on the main thread once _scan_one completes."""
    if error is not None:
        # Anything _scan_one's own try/except didn't already turn into a
        # result['error'] string — a genuine bug rather than a handled
        # ffmpeg/input failure. Still needs to be visible: silently doing
        # nothing here would leave the file looking permanently "pending"
        # with no clue why, which is worse than a slightly generic message.
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
            previous_hash = file.metadata['~health_content_hash']
            changed = bool(previous_hash) and previous_hash != result['content_hash']
            file.metadata['~health_file_tier'] = result['file_tier']
            file.metadata['~health_track_tier'] = result['track_tier'] or ''
            file.metadata['~health_file_flags'] = result['file_flags']
            file.metadata['~health_track_flags'] = result['track_flags']
            file.metadata['~health_info'] = result['info']
            file.metadata['~health_track_score'] = _encode_metric(result['track_score'])
            file.metadata['~health_content_hash'] = result['content_hash']
            file.metadata['~health_changed_since_scan'] = '1' if changed else ''
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
            file.metadata['~health_codec_name'] = result['codec_name'] or ''
            file.metadata['~health_profile'] = result['profile'] or ''
            file.metadata['~health_bitrate_kbps'] = _encode_metric(result['bitrate_kbps'])
            file.metadata['~health_sample_rate'] = _encode_metric(result['sample_rate'])
            file.metadata['~health_channels'] = _encode_metric(result['channels'])
            file.metadata['~health_peak_db'] = _encode_metric(result['peak_db'])
    file.clear_pending()
    file.update()


def _maybe_auto_scan(api: PluginApi, file: File) -> None:
    """File-post-load hook, always registered — checks the option live so
    toggling it in Options takes effect immediately, no restart needed.
    """
    if not api.plugin_config['auto_scan']:
        return
    file.set_pending()
    ffmpeg_path = api.plugin_config['ffmpeg_path'] or None
    thresholds = _thresholds_from_config(api.plugin_config)
    run_task(
        lambda f=file, p=ffmpeg_path, t=thresholds: _scan_one(f.filename, p, t),
        lambda result=None, error=None, f=file: _scan_finished(f, result, error),
    )


class HealthOptionsPage(OptionsPage):
    NAME = "file_health"
    TITLE = "File Health"
    PARENT = "plugins"

    def __init__(self) -> None:
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        self.auto_scan_checkbox = QtWidgets.QCheckBox("Automatically scan newly added files", self)
        layout.addWidget(self.auto_scan_checkbox)
        auto_scan_detail = QtWidgets.QLabel(
            "Runs the same background-threaded scan as the manual action.", self
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

        layout.addWidget(_section_header("File Health Sensitivity"))
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
            "True Peak", _TRUE_PEAK_STEPS, _TRUE_PEAK_DEFAULT_INDEX, fmt="{:+.1f} dBTP", parent=self, tag="TPK",
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

        sensitivity_reset_row = QtWidgets.QHBoxLayout()
        sensitivity_reset_button = QtWidgets.QPushButton("Reset to Calibrated Defaults", self)
        sensitivity_reset_button.clicked.connect(self._reset_sensitivity_defaults)
        sensitivity_reset_row.addStretch(1)
        sensitivity_reset_row.addWidget(sensitivity_reset_button)
        sensitivity_layout.addLayout(sensitivity_reset_row)

        layout.addWidget(sensitivity_group)

        layout.addWidget(_section_header("Comparison Priority"))
        priority_group, priority_layout = _section_frame()
        priority_intro = QtWidgets.QLabel(
            "When two files score the same tier, the compare panel breaks the tie using "
            "these weights — each axis only contributes its relative rank among the tied "
            "files (1st, 2nd, ...), scaled by its weight here, never a raw number "
            "combined across unrelated units. Set a weight to 0 to ignore that axis "
            "entirely.",
            self,
        )
        priority_intro.setWordWrap(True)
        priority_layout.addWidget(priority_intro)

        self.rank_bandwidth_slider = _WeightSlider("Bandwidth", _BANDWIDTH_WEIGHT_BANDS, parent=self, tag="BND")
        priority_layout.addWidget(self.rank_bandwidth_slider)
        priority_layout.addSpacing(8)

        self.rank_noise_floor_slider = _WeightSlider("Noise Floor", _NOISE_FLOOR_WEIGHT_BANDS, parent=self, tag="NSF")
        priority_layout.addWidget(self.rank_noise_floor_slider)
        priority_layout.addSpacing(8)

        self.rank_dr14_slider = _WeightSlider("Dynamic Range", _DR14_WEIGHT_BANDS, parent=self, tag="DYN")
        priority_layout.addWidget(self.rank_dr14_slider)
        priority_layout.addSpacing(8)

        self.rank_coherence_slider = _WeightSlider("Stereo Coherence", _COHERENCE_WEIGHT_BANDS, parent=self, tag="COH")
        priority_layout.addWidget(self.rank_coherence_slider)

        priority_reset_row = QtWidgets.QHBoxLayout()
        priority_reset_button = QtWidgets.QPushButton("Reset to Equal Weights", self)
        priority_reset_button.clicked.connect(self._reset_priority_defaults)
        priority_reset_row.addStretch(1)
        priority_reset_row.addWidget(priority_reset_button)
        priority_layout.addLayout(priority_reset_row)

        layout.addWidget(priority_group)
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
        self.rank_bandwidth_slider.set_value(self.api.plugin_config['rank_weight_bandwidth'])
        self.rank_noise_floor_slider.set_value(self.api.plugin_config['rank_weight_noise_floor'])
        self.rank_dr14_slider.set_value(self.api.plugin_config['rank_weight_dr14'])
        self.rank_coherence_slider.set_value(self.api.plugin_config['rank_weight_coherence'])

    def save(self) -> None:
        self.api.plugin_config['auto_scan'] = self.auto_scan_checkbox.isChecked()
        self.api.plugin_config['ffmpeg_path'] = self.ffmpeg_path_edit.text().strip()
        self.api.plugin_config['clip_flat_factor'] = self.clip_slider.value()
        self.api.plugin_config['true_peak_dbtp'] = self.true_peak_slider.value()
        self.api.plugin_config['spectral_silence_db'] = self.spectral_silence_slider.value()
        self.api.plugin_config['phase_angle_deg'] = self.phase_angle_slider.value()
        self.api.plugin_config['dr14_shift'] = self.dr14_shift_slider.value()
        self.api.plugin_config['rank_weight_bandwidth'] = self.rank_bandwidth_slider.value()
        self.api.plugin_config['rank_weight_noise_floor'] = self.rank_noise_floor_slider.value()
        self.api.plugin_config['rank_weight_dr14'] = self.rank_dr14_slider.value()
        self.api.plugin_config['rank_weight_coherence'] = self.rank_coherence_slider.value()

    def _reset_sensitivity_defaults(self) -> None:
        self.clip_slider.reset_to_default()
        self.true_peak_slider.reset_to_default()
        self.spectral_silence_slider.reset_to_default()
        self.phase_angle_slider.reset_to_default()
        self.dr14_shift_slider.reset_to_default()

    def _reset_priority_defaults(self) -> None:
        self.rank_bandwidth_slider.set_value(5)
        self.rank_noise_floor_slider.set_value(5)
        self.rank_dr14_slider.set_value(5)
        self.rank_coherence_slider.set_value(5)

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
                f"ffmpeg {min_version} or newer for loudnorm/DR14/phase analysis; "
                "scans against this binary will fail with an explanatory error. "
                "Use Get ffmpeg… below for a current build.)</div>"
            )
        else:
            found = '.'.join(map(str, version))
            self.ffmpeg_status_label.setText(f"Found: {resolved} (ffmpeg {found} — OK)")


class ScanHealthAction(BaseAction):
    """Right-click action that triggers the health scan on demand.

    Manual by default — real analysis needs to decode audio, which is
    neither instant nor safe to run inline on the file-load callback.
    An opt-in automatic mode is available via Options (off by default,
    same background-threaded scan either way). Each scan runs on a
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
        thresholds = _thresholds_from_config(self.api.plugin_config)
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path, t=thresholds: _scan_one(f.filename, p, t),
                lambda result=None, error=None, f=file: _scan_finished(f, result, error),
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


# (metadata key, plugin_config weight key, higher-value-is-better)
_RANK_AXES: tuple[tuple[str, str, bool], ...] = (
    ('~health_bandwidth_hz', 'rank_weight_bandwidth', True),
    ('~health_noise_floor_db', 'rank_weight_noise_floor', False),
    ('~health_dr14', 'rank_weight_dr14', True),
    ('~health_stereo_coherence', 'rank_weight_coherence', True),
)


def _composite_winner(files: list[File], rank_weights: dict[str, int]) -> File | None:
    """Weighted rank-sum (Borda count) tie-break among tier-tied files.

    Deliberately NOT a single weighted score combining raw Hz/dB/DR-point/
    correlation values — that would need arbitrary unit-conversion
    factors this project has no defensible basis for (bandwidth is in
    Hz, noise floor in dB, dynamic range in DR-points, coherence a
    [-1,1] ratio; there's no principled way to say "1kHz of bandwidth is
    worth how many dB of noise floor"). Each axis instead only
    contributes each file's *relative rank position within this group*
    — 1st place, 2nd place, etc. — scaled by the user's own importance
    weight for that axis, so no unit conversion is ever needed.

    A file missing a measurement on some axis gets the worst possible
    rank on that axis (one past the last measured file), so missing
    data never accidentally looks best. An axis is skipped entirely
    when its weight is 0 or fewer than two files in the group have a
    value for it — nothing to legitimately compare.

    Returns None when no axis contributed anything, or when the
    weighted totals still end in an exact tie — this plugin's standing
    policy is to not declare a winner without a real signal to back it.
    """
    if len(files) < 2:
        return None
    scores: dict[File, float] = dict.fromkeys(files, 0.0)
    any_axis_used = False
    for metadata_key, weight_key, higher_is_better in _RANK_AXES:
        weight = rank_weights.get(weight_key, 0)
        if weight <= 0:
            continue
        measured = {f: v for f in files if (v := _read_metric(f, metadata_key)) is not None}
        if len(measured) < 2:
            continue
        any_axis_used = True
        ordered = sorted(measured, key=lambda f: measured[f], reverse=higher_is_better)
        for rank_index, f in enumerate(ordered, start=1):
            scores[f] += rank_index * weight
        worst_rank = len(ordered) + 1
        for f in files:
            if f not in measured:
                scores[f] += worst_rank * weight
    if not any_axis_used:
        return None
    best_score = min(scores.values())
    winners = [f for f, s in scores.items() if s == best_score]
    return winners[0] if len(winners) == 1 else None


_RANK_LABELS: dict[str, str] = {
    'rank_weight_bandwidth': "Bandwidth",
    'rank_weight_noise_floor': "Noise floor",
    'rank_weight_dr14': "Dynamic range",
    'rank_weight_coherence': "Stereo coherence",
}


def _rank_explanation(files: list[File], rank_weights: dict[str, int]) -> str:
    """Plain-text breakdown of the values the weighted tie-break actually
    used, attached as the winning row's tooltip — a rank position is
    never shown without the numbers behind it.
    """
    lines: list[str] = []
    for metadata_key, weight_key, _higher in _RANK_AXES:
        weight = rank_weights.get(weight_key, 0)
        if weight <= 0:
            continue
        values = [(f.base_filename, _read_metric(f, metadata_key)) for f in files]
        if sum(1 for _name, v in values if v is not None) < 2:
            continue
        label = _RANK_LABELS.get(weight_key, weight_key)
        parts = ", ".join(f"{name}={v:.1f}" if v is not None else f"{name}=?" for name, v in values)
        lines.append(f"{label} (weight {weight}): {parts}")
    if not lines:
        return ""
    return "Ranked ahead of tied files by:\n" + "\n".join(lines)


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
}
_CHECK_COLUMNS = list(_CHECK_TAGS)

# rank axis label -> (metadata key, plugin_config weight key, tag,
# higher-value-is-better) — for the three axes with no absolute
# pass/fail meaning of their own anywhere in analysis.py (only relative
# to whichever files are being compared right now). Dynamic Range is
# deliberately NOT here — see _dr14_band_cell for why it needs
# different treatment: unlike these three, it has a real absolute band
# that decides Tier, so relative-in-group coloring would show "pass"
# for the best-in-group file even when every file in the group is
# absolutely Poor.
_RANK_COLUMN_INFO: dict[str, tuple[str, str, str, bool]] = {
    'Bandwidth': ('~health_bandwidth_hz', 'rank_weight_bandwidth', 'BND', True),
    'Noise Floor': ('~health_noise_floor_db', 'rank_weight_noise_floor', 'NSF', False),
    'Stereo Coherence': ('~health_stereo_coherence', 'rank_weight_coherence', 'COH', True),
}
_RANK_COLUMNS = list(_RANK_COLUMN_INFO)
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
) -> tuple[str, str]:
    """One gate check's cell: (state, tooltip). Amber means "would fail if
    the matching Options-page slider moved one notch stricter" — reuses
    the slider's own calibrated steps rather than a new invented margin,
    so the amber band moves live with the slider instead of sitting at a
    fixed offset unrelated to what the user actually configured.
    """
    if not applies or value is None:
        return 'na', na_reason
    if fails(value, current):
        return 'fail', f"{value:.1f}{unit} — already past the current threshold ({current:.1f}{unit})."
    next_stricter = _next_stricter_step(steps, current)
    if next_stricter is not None and fails(value, next_stricter):
        return 'amber', (
            f"{value:.1f}{unit} — clear of the current threshold ({current:.1f}{unit}), but would fail "
            f"one slider notch stricter ({next_stricter:.1f}{unit})."
        )
    return 'pass', f"{value:.1f}{unit} — comfortably clear of the threshold ({current:.1f}{unit})."


def _check_cells(file: File, thresholds: analysis.Thresholds) -> dict[str, tuple[str, str]]:
    """Every gate-check column's (state, tooltip) for one file, computed
    live from its stored raw measurements against the *current* slider
    settings — not the tier baked in at last scan time, which only
    updates on rescan since which gates fired decides whether DR14 was
    even measured (see CompareResultsPanel's docstring for that split).
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
        ),
        'True Peak': _gate_cell(
            _read_metric(file, '~health_true_peak_dbtp'), True,
            thresholds.true_peak_dbtp, _TRUE_PEAK_STEPS, lambda v, t: v >= t, "dBTP", "Not yet measured.",
        ),
        'Spectral Cutoff': _gate_cell(
            _read_metric(file, '~health_spectral_cutoff_db'), has_signal,
            thresholds.spectral_silence_db, _SPECTRAL_STEPS, lambda v, t: v < t, "dB",
            "Near-silent file — not enough signal to measure high-frequency content.",
        ),
        'Fake Hi-Res': _gate_cell(
            _read_metric(file, '~health_hires_cutoff_db'), is_hires and has_signal,
            thresholds.spectral_silence_db, _SPECTRAL_STEPS, lambda v, t: v < t, "dB",
            "Not a hi-res-rate file — check doesn't apply." if not is_hires else
            "Near-silent file — not enough signal to measure.",
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


def _rank_cells(group: list[File], rank_weights: dict[str, int]) -> dict[File, dict[str, tuple[str, str]]]:
    """Every ranking-axis column's (state, tooltip) for every file in one
    compare group — relative *within this group* (best measured value
    green, worst red, the rest amber), since these axes have no absolute
    pass/fail threshold of their own, only a tie-break weight. A weight
    of 0 dims the whole column to "na" regardless of the measured values
    — it isn't currently used to break ties.
    """
    result: dict[File, dict[str, tuple[str, str]]] = {f: {} for f in group}
    for label, (metadata_key, weight_key, _tag, higher_is_better) in _RANK_COLUMN_INFO.items():
        weight = rank_weights.get(weight_key, 0)
        values = {f: _read_metric(f, metadata_key) for f in group}
        measured = {f: v for f, v in values.items() if v is not None}
        if measured:
            best = max(measured.values()) if higher_is_better else min(measured.values())
            worst = min(measured.values()) if higher_is_better else max(measured.values())
        else:
            best = worst = None
        for f in group:
            v = values[f]
            if weight <= 0:
                result[f][label] = ('na', "Weight is 0 — this axis is not currently used to break ties.")
            elif v is None:
                result[f][label] = ('na', "Not measured (skipped this scan, or not yet scanned).")
            elif len(measured) < 2 or best == worst:
                result[f][label] = ('pass', f"{v:.1f} — nothing else in the group to compare against.")
            elif v == best:
                result[f][label] = ('pass', f"{v:.1f} — best in the group (weight {weight}/10).")
            elif v == worst:
                result[f][label] = ('fail', f"{v:.1f} — worst in the group (weight {weight}/10).")
            else:
                result[f][label] = ('amber', f"{v:.1f} — middle of the group (weight {weight}/10).")
    return result


def _dr14_band_cell(file: File, thresholds: analysis.Thresholds, rank_weight: int) -> tuple[str, str]:
    """Dynamic Range's cell, unlike the three purely-relative ranking
    axes: DR14 has its own absolute Poor/Ok/Good/Great/Excellent band
    (it's one weighted input into the Track Health composite score —
    see analysis.compute_track_score/_dr14_score), so it's colored by
    that band, not relative to whichever other files happen to be in
    this compare group. Relative coloring would show "best in group"
    as green even when every file compared is absolutely Poor — a
    real contradiction with the Track Tier column, not just a
    cosmetic quibble.

    The Comparison Priority weight still matters for tie-breaking among
    same-tier files (see _composite_winner), so it's noted in the
    tooltip, but never dims this column to "na" the way a 0 weight does
    for the purely-relative axes — DR14 affects Tier regardless of
    whether its weight is used for tie-breaking.
    """
    dr14 = _read_metric(file, '~health_dr14')
    if dr14 is None:
        return 'na', "Skipped this scan (file already gated \"Bad\" by another check), or not yet scanned."
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
    weight_note = (
        f" Comparison Priority weight: {rank_weight}/10." if rank_weight
        else " Weight is 0 — not currently used to break ties."
    )
    return state, f"DR{int(dr14)} — {band} on the TT DR Offline Meter scale.{weight_note}"


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


class CompareResultsPanel(QtWidgets.QDialog):
    """Non-modal panel listing every file in each shared-identity group.

    Doesn't declare a winner — presents every file's tier and issues side
    by side, per group, and only bolds whichever scored higher within its
    own group as a subtle cue. The user decides; we show the data.
    """

    def __init__(
        self,
        ffmpeg_path: str | None,
        thresholds: analysis.Thresholds,
        rank_weights: dict[str, int],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Health Comparison")
        self.setModal(False)
        self.resize(1350, 420)
        self._ffmpeg_path = ffmpeg_path
        self._thresholds = thresholds
        self._rank_weights = rank_weights
        # (label, files) per group, in display order — kept around so a scan
        # triggered from this panel can redraw in place without the caller
        # re-deriving track groupings or the user closing/reopening it.
        self._groups: list[tuple[str, list[File]]] = []

        layout = QtWidgets.QVBoxLayout(self)

        scan_row = QtWidgets.QHBoxLayout()
        self.scan_unscanned_button = QtWidgets.QPushButton("Scan Unscanned", self)
        self.scan_unscanned_button.setToolTip("Scan every file below that hasn't been scanned yet.")
        self.scan_unscanned_button.clicked.connect(self._scan_unscanned)
        self.rescan_all_button = QtWidgets.QPushButton("Rescan All", self)
        self.rescan_all_button.setToolTip("Re-scan every file below, including already-scanned ones.")
        self.rescan_all_button.clicked.connect(self._rescan_all)
        scan_row.addWidget(self.scan_unscanned_button)
        scan_row.addWidget(self.rescan_all_button)
        scan_row.addStretch(1)
        layout.addLayout(scan_row)

        self.tree = QtWidgets.QTreeWidget(self)
        full_names = ['File', 'File Tier', 'Track Tier', 'Format'] + _CHECK_COLUMNS + ['Dynamic Range'] + _RANK_COLUMNS + ['Notes']
        headers = ['File', 'File Tier', 'Track Tier', 'Format']
        headers += [_CHECK_TAGS[c] for c in _CHECK_COLUMNS]
        headers += [_DR14_TAG]
        headers += [_RANK_COLUMN_INFO[c][2] for c in _RANK_COLUMNS]
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
        self.tree.setColumnWidth(len(headers) - 1, 200)
        self.tree.setRootIsDecorated(True)
        self.tree.itemSelectionChanged.connect(self._update_button_states)
        self.tree.itemDoubleClicked.connect(lambda *_: self._show_in_list())
        layout.addWidget(self.tree)

        action_row = QtWidgets.QHBoxLayout()
        self.show_button = QtWidgets.QPushButton("Show in List", self)
        self.show_button.clicked.connect(self._show_in_list)
        self.spectrogram_button = QtWidgets.QPushButton("Spectrogram…", self)
        self.spectrogram_button.setToolTip(
            "Render a log-frequency spectrogram for the selected file — see a cutoff "
            "wall, elevated noise floor, or asymmetric stereo content directly, rather "
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

        unscanned = [f for f in group if not f.metadata['~health_file_tier']]
        if unscanned:
            for file in group:
                file_tier = file.metadata['~health_file_tier'] or "Not yet scanned"
                track_tier = file.metadata['~health_track_tier'] or ("" if file.metadata['~health_file_tier'] else "Not yet scanned")
                item = QtWidgets.QTreeWidgetItem([file.base_filename, file_tier, track_tier])
                item.setData(0, _FILE_ROLE, file)
                header.addChild(item)
            header.setExpanded(True)
            return

        ranks = {file: _tier_rank(file) for file in group}
        best_rank = max(ranks.values())
        top_files = [f for f in group if ranks[f] == best_rank]
        if len(group) < 2:
            winner = None
        elif len(top_files) == 1:
            winner = top_files[0]
        else:
            winner = _composite_winner(top_files, self._rank_weights)

        rank_cells_by_file = _rank_cells(group, self._rank_weights)

        for file in group:
            file_tier = file.metadata['~health_file_tier']
            # A Broken file has no Track Health to show (see
            # analysis.analyze_file's docstring) — nothing to measure
            # perceptually, not a blank "not yet scanned" state.
            track_tier = file.metadata['~health_track_tier'] or (
                "—" if file_tier == analysis.FILE_TIER_BROKEN else ""
            )
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
            dr14_state, dr14_tooltip = _dr14_band_cell(
                file, self._thresholds, self._rank_weights.get('rank_weight_dr14', 0)
            )
            self._paint_matrix_cell(item, col, dr14_state, dr14_tooltip)
            col += 1
            for label in _RANK_COLUMNS:
                state, tooltip = rank_cells_by_file[file][label]
                self._paint_matrix_cell(item, col, state, tooltip)
                col += 1

            info_parts = file.metadata['~health_info'].split("; ") if file.metadata['~health_info'] else []
            if file.metadata['~health_changed_since_scan']:
                info_parts.insert(0, "Changed since last scan")
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
                if len(top_files) > 1:
                    explanation = _rank_explanation(top_files, self._rank_weights)
                    if explanation:
                        item.setToolTip(1, explanation)
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

    def _run_scan(self, files: list[File]) -> None:
        if not files:
            return
        tagger_instance().window.set_statusbar_message(
            "Scanning file health for %(count)d file(s)…",
            {'count': len(files)},
            echo=None,
        )
        ffmpeg_path = self._ffmpeg_path
        thresholds = self._thresholds
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path, t=thresholds: _scan_one(f.filename, p, t),
                lambda result=None, error=None, f=file: self._on_scan_finished(f, result, error),
            )

    def _on_scan_finished(self, file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
        _scan_finished(file, result, error)
        self.refresh()

    def _scan_unscanned(self) -> None:
        self._run_scan([f for f in self._all_files() if not f.metadata['~health_file_tier']])

    def _rescan_all(self) -> None:
        self._run_scan(self._all_files())

    def _update_scan_button_states(self) -> None:
        files = self._all_files()
        self.rescan_all_button.setEnabled(bool(files))
        self.scan_unscanned_button.setEnabled(
            any(not f.metadata['~health_file_tier'] for f in files)
        )

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


def _open_compare_panel(
    files: list[File],
    parent: QtWidgets.QWidget,
    ffmpeg_path: str | None,
    thresholds: analysis.Thresholds,
    rank_weights: dict[str, int],
) -> CompareResultsPanel | None:
    """Groups by Track (Picard's own matching decision, not our own tag
    comparison), opens the panel if there's anything to compare.
    """
    groups = {track: group for track, group in _group_by_track(files).items() if len(group) > 1}
    if not groups:
        return None
    panel = CompareResultsPanel(ffmpeg_path, thresholds, rank_weights, parent)
    for track, group in groups.items():
        panel.add_group(_track_label(track), group)
    panel.show()
    return panel


class CompareHealthAction(BaseAction):
    """Right-click action that compares files Picard has matched to the
    same track, within the current selection.

    Grouping reuses Picard's own matching decision (which track a file is
    linked to, decided via its configured match_min_similarity/margin
    thresholds) rather than a separate tag-based identity check — if
    Picard considers two files the same recording, so do we, and never
    disagrees with what the main window already shows grouped together.

    Doesn't declare a hard winner in the results — only bolds whichever
    file scored higher within its group (by tier, then a user-weighted
    rank-sum across bandwidth/noise-floor/dynamic-range/stereo-coherence
    for tier-tied files — see _composite_winner), as a subtle cue,
    leaving the actual decision to the user. Matches the design decided
    earlier: a perceptual-distance metric like ViSQOL/Zimtohrli would
    tell you the files differ, but not which one is better; the
    directional gate-field reasons and continuous fidelity axes (real,
    from analysis.analyze_file, stored in ~health_file_flags/
    ~health_track_flags and the ~health_* metric fields) are what
    actually inform that judgment.
    """

    TITLE = "Compare File Health…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        window = tagger_instance().window
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        thresholds = _thresholds_from_config(self.api.plugin_config)
        rank_weights = _rank_weights_from_config(self.api.plugin_config)
        panel = _open_compare_panel(files, window, ffmpeg_path, thresholds, rank_weights)
        if panel is None:
            window.set_statusbar_message(
                "None of the selected files are matched to the same track.",
                echo=None,
            )
            return
        self._panel = panel


class CompareAllHealthAction(BaseAction):
    """Tools-menu action: compares every matched-duplicate track across the
    entire loaded library at once — the "many files" case, not limited to
    a manual selection.
    """

    TITLE = "Compare All File Health…"

    def callback(self, objs) -> None:
        window = tagger_instance().window
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        thresholds = _thresholds_from_config(self.api.plugin_config)
        rank_weights = _rank_weights_from_config(self.api.plugin_config)
        panel = _open_compare_panel(_all_loaded_files(), window, ffmpeg_path, thresholds, rank_weights)
        if panel is None:
            window.set_statusbar_message(
                "No tracks in the library currently have more than one matched file.",
                echo=None,
            )
            return
        self._panel = panel


class HealthProvider(ColumnValueProvider, DelegateProvider):
    """Column that displays both health tiers as a pair of bookmark icons
    with an itemized-issues tooltip.
    """

    def __init__(self) -> None:
        self._delegate_class = HealthColumnDelegate

    def evaluate(self, obj: Item) -> str:
        """Combined sort key, worst-to-best: File Health rank first (a
        structural defect is a harder, more objective fact than a
        perceptual one — see _tier_rank), Track Health rank second.
        Zero-padded so lexicographic (TEXT) sorting matches numeric
        order for every rank pair.
        """
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return "-1"
        try:
            file_rank = FILE_TIERS.index(column_method('~health_file_tier'))
        except ValueError:
            return "-1"
        try:
            track_rank = TRACK_TIERS.index(column_method('~health_track_tier'))
        except ValueError:
            track_rank = -1
        return f"{file_rank:02d}{track_rank + 1:02d}"

    def get_health_info(self, obj: Item) -> dict[str, object] | None:
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return None
        file_tier = column_method('~health_file_tier')
        if not file_tier:
            return None
        track_tier = column_method('~health_track_tier')
        stored_file_flags = column_method('~health_file_flags')
        stored_track_flags = column_method('~health_track_flags')
        stored_info = column_method('~health_info')
        return {
            'file_tier': file_tier,
            'track_tier': track_tier or None,
            'file_issues': stored_file_flags.split("; ") if stored_file_flags else [],
            'track_issues': stored_track_flags.split("; ") if stored_track_flags else [],
            'info': stored_info.split("; ") if stored_info else [],
            'changed_since_scan': bool(column_method('~health_changed_since_scan')),
        }

    def get_delegate_class(self) -> type[QtWidgets.QStyledItemDelegate]:
        return self._delegate_class


class HealthColumnDelegate(QtWidgets.QStyledItemDelegate):
    """Renders both health tiers as a pair of bookmark icons (File Health
    left, Track Health right); hover shows every issue from both.
    """

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
        icon_size = 16
        icon_margin = 2
        y = option.rect.y() + (option.rect.height() - icon_size) // 2
        file_level = FILE_TIER_ICON_LEVEL.get(info['file_tier'])
        if file_level is not None:
            x = option.rect.x() + icon_margin
            match_icons[file_level].paint(painter, QtCore.QRect(x, y, icon_size, icon_size))
        track_tier = info.get('track_tier')
        track_level = TRACK_TIER_ICON_LEVEL.get(track_tier) if track_tier else None
        if track_level is not None:
            x = option.rect.x() + icon_margin * 2 + icon_size
            match_icons[track_level].paint(painter, QtCore.QRect(x, y, icon_size, icon_size))

    def _format_tooltip(self, info: dict[str, object]) -> str:
        file_tier = info['file_tier']
        track_tier = info.get('track_tier')
        parts = [f"<b>File Health: {file_tier}</b>"]
        if info.get('changed_since_scan'):
            parts.append(
                "<div style='color:#b7950b;'>Audio content changed since last scan</div>"
            )
        file_issues = info['file_issues']
        if file_issues:
            items = "".join(f"<li>{issue}</li>" for issue in file_issues)
            parts.append(f"<ul style='margin-left:-20px;'>{items}</ul>")
        else:
            parts.append("<br>No structural defects")
        if track_tier is None:
            parts.append("<br><b>Track Health: not measured</b> — file won't decode")
        else:
            parts.append(f"<br><b>Track Health: {track_tier}</b>")
            track_issues = info['track_issues']
            if track_issues:
                items = "".join(f"<li>{issue}</li>" for issue in track_issues)
                parts.append(f"<ul style='margin-left:-20px;'>{items}</ul>")
            else:
                parts.append("<br>No perceptible issues")
        notes = info.get('info') or []
        if notes:
            # Informational only — doesn't affect either tier (e.g. mono
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
        return QtCore.QSize(90, 20)


# Registered at import time, not inside enable(), to match how Picard's own
# core columns register (ALBUMVIEW_COLUMNS.insert(...) at columns.py import
# time) — this must happen before any tree view builds its header, which
# plugin enable() apparently runs too late for.
_HEALTH_COLUMN = make_delegate_column(
    "Health",
    '~health_combined_tier',
    HealthProvider(),
    width=90,
    size=QtCore.QSize(80, 16),
)
_HEALTH_COLUMN.is_default = True
registry.register(_HEALTH_COLUMN, add_to={'FILE_VIEW', 'ALBUM_VIEW'})


def _install_delegate_on_live_views() -> None:
    """Attach the icon-painting delegate to any already-open tree widgets.

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
        try:
            index = list(columns).index(_HEALTH_COLUMN)
        except ValueError:
            continue
        set_delegate = getattr(widget, 'setItemDelegateForColumn', None)
        if callable(set_delegate):
            set_delegate(index, HealthColumnDelegate(widget))


def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    api.logger.info("File Health enabled")

    # Picard's own match_icons list is populated lazily; Picard core likely
    # already loaded it, but don't rely on load order — reusing Picard's
    # own bookmark icons (not new plugin-bundled assets) needs this.
    load_match_icons()
    api.register_script_variable(
        '_health_file_tier',
        documentation="File Health tier from the last scan (Broken/Bad/Good/Great/Excellent).",
        title="File Health",
    )
    api.register_script_variable(
        '_health_track_tier',
        documentation="Track Health tier from the last scan (Bad/Good/Great/Excellent); empty if the file is Broken.",
        title="Track Health",
    )
    api.register_script_variable(
        '_health_file_flags',
        documentation="Itemized list of structural (File Health) issues found by the last scan.",
        title="File Health flags",
    )
    api.register_script_variable(
        '_health_track_flags',
        documentation="Itemized list of perceptual (Track Health) issues found by the last scan.",
        title="Track Health flags",
    )
    api.register_script_variable(
        '_health_info',
        documentation="Informational notes that don't affect either health tier (e.g. mono content in a stereo container).",
        title="Health info",
    )
    api.register_script_variable(
        '_health_content_hash',
        documentation="Hash of the file's bytes as of the last health scan.",
        title="Health content hash",
    )
    api.register_script_variable(
        '_health_changed_since_scan',
        documentation="Non-empty if the file's bytes changed since the last health scan.",
        title="Health changed since scan",
    )
    api.plugin_config.register_option('auto_scan', False)
    api.plugin_config.register_option('ffmpeg_path', '')
    api.plugin_config.register_option('clip_flat_factor', analysis.MIN_FLAT_FACTOR_FOR_CLIPPING)
    api.plugin_config.register_option('true_peak_dbtp', analysis.TRUE_PEAK_THRESHOLD_DBTP)
    api.plugin_config.register_option('spectral_silence_db', analysis.SPECTRAL_SILENCE_THRESHOLD_DB)
    api.plugin_config.register_option('phase_angle_deg', analysis.PHASE_OUT_OF_PHASE_ANGLE_DEG)
    api.plugin_config.register_option('dr14_shift', 0.0)
    api.plugin_config.register_option('rank_weight_bandwidth', 5)
    api.plugin_config.register_option('rank_weight_noise_floor', 5)
    api.plugin_config.register_option('rank_weight_dr14', 5)
    api.plugin_config.register_option('rank_weight_coherence', 5)
    api.register_file_post_load_processor(_maybe_auto_scan)
    api.register_options_page(HealthOptionsPage)

    api.register_file_action(ScanHealthAction)
    api.register_track_action(ScanHealthAction)
    api.register_cluster_action(ScanHealthAction)

    api.register_file_action(CompareHealthAction)
    api.register_track_action(CompareHealthAction)
    api.register_cluster_action(CompareHealthAction)
    api.register_tools_menu_action(CompareAllHealthAction)

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
    registry.unregister(_HEALTH_COLUMN.key)
