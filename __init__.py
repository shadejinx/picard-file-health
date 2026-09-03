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


# Ordered worst-to-best, matches picard.ui.match_icons' 6 bookmark levels
# and picard.file_health.analysis's tier names exactly.
TIERS = ("Bad", "Poor", "Ok", "Good", "Great", "Excellent")


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
        self.value_label = QtWidgets.QLabel(self)
        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(self.value_label)
        layout.addLayout(header)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
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

    Simpler than _SensitivitySlider — no per-step hints needed, since
    "how important is this axis to you" doesn't carry the same
    gate-specific meaning per position that a detection threshold does.
    0 means the axis is ignored entirely.
    """

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(2)

        header = QtWidgets.QHBoxLayout()
        title_label = QtWidgets.QLabel(title, self)
        title_label.setWordWrap(False)
        bold = title_label.font()
        bold.setBold(True)
        title_label.setFont(bold)
        self.value_label = QtWidgets.QLabel(self)
        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(self.value_label)
        layout.addLayout(header)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
        self.slider.setMinimum(0)
        self.slider.setMaximum(10)
        self.slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._on_changed)
        layout.addWidget(self.slider)

        self.set_value(5)

    def _on_changed(self, value: int) -> None:
        self.value_label.setText("Ignored" if value == 0 else f"{value}/10")

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
    except (analysis.FfmpegNotFoundError, analysis.AnalysisError) as exc:
        # FfmpegNotFoundError (incl. FfmpegVersionTooOldError): ffmpeg
        # itself is missing/unusable — an environment problem, fixed in
        # Options. AnalysisError: ffmpeg ran but couldn't process this
        # specific file — every file scanned here is user-supplied and
        # may be corrupt, truncated, or not really audio at all, so this
        # is expected, not a bug. Either way: surfaced to the user below,
        # never silently swallowed.
        return {'error': str(exc)}
    return {
        'tier': result.tier,
        'flags': "; ".join(result.issues),
        'info': "; ".join(result.info),
        'content_hash': result.content_hash,
        'bandwidth_hz': result.spectral_bandwidth_hz,
        'noise_floor_db': result.noise_floor_db,
        'dr14': result.dr14,
        'stereo_coherence': result.stereo_coherence,
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
        elif 'tier' in result:
            previous_hash = file.metadata['~health_content_hash']
            changed = bool(previous_hash) and previous_hash != result['content_hash']
            file.metadata['~health_tier'] = result['tier']
            file.metadata['~health_flags'] = result['flags']
            file.metadata['~health_info'] = result['info']
            file.metadata['~health_content_hash'] = result['content_hash']
            file.metadata['~health_changed_since_scan'] = '1' if changed else ''
            file.metadata['~health_bandwidth_hz'] = _encode_metric(result['bandwidth_hz'])
            file.metadata['~health_noise_floor_db'] = _encode_metric(result['noise_floor_db'])
            file.metadata['~health_dr14'] = _encode_metric(result['dr14'])
            file.metadata['~health_stereo_coherence'] = _encode_metric(result['stereo_coherence'])
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

        layout.addWidget(_section_header("Detection Sensitivity"))
        sensitivity_group, sensitivity_layout = _section_frame()
        sensitivity_intro = QtWidgets.QLabel(
            "Any one check below can flag a file \"Bad\" on its own, even if it sounds fine "
            "to you. Loosen a slider if it's flagging files that sound OK; tighten it if it's "
            "missing real problems.",
            self,
        )
        sensitivity_intro.setWordWrap(True)
        sensitivity_layout.addWidget(sensitivity_intro)

        self.clip_slider = _SensitivitySlider(
            "Clipping", _CLIP_STEPS, _CLIP_DEFAULT_INDEX, fmt="{:g}", parent=self,
        )
        sensitivity_layout.addWidget(self.clip_slider)
        sensitivity_layout.addSpacing(8)

        self.true_peak_slider = _SensitivitySlider(
            "True Peak", _TRUE_PEAK_STEPS, _TRUE_PEAK_DEFAULT_INDEX, fmt="{:+.1f} dBTP", parent=self,
        )
        sensitivity_layout.addWidget(self.true_peak_slider)
        sensitivity_layout.addSpacing(8)

        self.spectral_silence_slider = _SensitivitySlider(
            "Missing Treble", _SPECTRAL_STEPS, _SPECTRAL_DEFAULT_INDEX, fmt="{:.0f} dB", parent=self,
        )
        sensitivity_layout.addWidget(self.spectral_silence_slider)
        sensitivity_layout.addSpacing(8)

        self.phase_angle_slider = _SensitivitySlider(
            "Out-of-Phase Channels", _PHASE_STEPS, _PHASE_DEFAULT_INDEX, fmt="{:.0f}\u00b0", parent=self,
        )
        sensitivity_layout.addWidget(self.phase_angle_slider)

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

        self.rank_bandwidth_slider = _WeightSlider("Bandwidth", parent=self)
        priority_layout.addWidget(self.rank_bandwidth_slider)
        priority_layout.addSpacing(8)

        self.rank_noise_floor_slider = _WeightSlider("Noise Floor", parent=self)
        priority_layout.addWidget(self.rank_noise_floor_slider)
        priority_layout.addSpacing(8)

        self.rank_dr14_slider = _WeightSlider("Dynamic Range", parent=self)
        priority_layout.addWidget(self.rank_dr14_slider)
        priority_layout.addSpacing(8)

        self.rank_coherence_slider = _WeightSlider("Stereo Coherence", parent=self)
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
        self.api.plugin_config['rank_weight_bandwidth'] = self.rank_bandwidth_slider.value()
        self.api.plugin_config['rank_weight_noise_floor'] = self.rank_noise_floor_slider.value()
        self.api.plugin_config['rank_weight_dr14'] = self.rank_dr14_slider.value()
        self.api.plugin_config['rank_weight_coherence'] = self.rank_coherence_slider.value()

    def _reset_sensitivity_defaults(self) -> None:
        self.clip_slider.reset_to_default()
        self.true_peak_slider.reset_to_default()
        self.spectral_silence_slider.reset_to_default()
        self.phase_angle_slider.reset_to_default()

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


def _tier_rank(file: File) -> int:
    """Higher is better; -1 means not yet scanned."""
    try:
        return TIERS.index(file.metadata['~health_tier'])
    except ValueError:
        return -1


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
        self.resize(min(pixmap.width() + 40, 1000), min(pixmap.height() + 60, 700))


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
        self.resize(620, 380)
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
        self.tree.setHeaderLabels(["File", "Health", "Issues"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 90)
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

        unscanned = [f for f in group if not f.metadata['~health_tier']]
        if unscanned:
            for file in group:
                tier = file.metadata['~health_tier'] or "Not yet scanned"
                item = QtWidgets.QTreeWidgetItem([file.base_filename, tier, ""])
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

        for file in group:
            tier = file.metadata['~health_tier']
            stored_flags = file.metadata['~health_flags']
            issue_parts = stored_flags.split("; ") if stored_flags else []
            if file.metadata['~health_changed_since_scan']:
                issue_parts.insert(0, "Changed since last scan")
            issues = "; ".join(issue_parts) or "—"
            item = QtWidgets.QTreeWidgetItem([file.base_filename, tier, issues])
            item.setData(0, _FILE_ROLE, file)
            if file is winner:
                bold = item.font(0)
                bold.setBold(True)
                item.setFont(0, bold)
                item.setFont(1, bold)
                if len(top_files) > 1:
                    explanation = _rank_explanation(top_files, self._rank_weights)
                    if explanation:
                        item.setToolTip(1, explanation)
            header.addChild(item)
        header.setExpanded(True)

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
        self._run_scan([f for f in self._all_files() if not f.metadata['~health_tier']])

    def _rescan_all(self) -> None:
        self._run_scan(self._all_files())

    def _update_scan_button_states(self) -> None:
        files = self._all_files()
        self.rescan_all_button.setEnabled(bool(files))
        self.scan_unscanned_button.setEnabled(
            any(not f.metadata['~health_tier'] for f in files)
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
    from analysis.analyze_file, stored in ~health_flags and the
    ~health_* metric fields) are what actually inform that judgment.
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
    """Column that displays health tier as a bookmark icon with an issues tooltip."""

    def __init__(self) -> None:
        self._delegate_class = HealthColumnDelegate

    def evaluate(self, obj: Item) -> str:
        """Return the tier index as a string, for sorting worst-to-best."""
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return "-1"
        tier = column_method('~health_tier')
        try:
            return str(TIERS.index(tier))
        except ValueError:
            return "-1"

    def get_health_info(self, obj: Item) -> dict[str, object] | None:
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return None
        tier = column_method('~health_tier')
        if not tier:
            return None
        stored_flags = column_method('~health_flags')
        stored_info = column_method('~health_info')
        return {
            'tier': tier,
            'issues': stored_flags.split("; ") if stored_flags else [],
            'info': stored_info.split("; ") if stored_info else [],
            'changed_since_scan': bool(column_method('~health_changed_since_scan')),
        }

    def get_delegate_class(self) -> type[QtWidgets.QStyledItemDelegate]:
        return self._delegate_class


class HealthColumnDelegate(QtWidgets.QStyledItemDelegate):
    """Renders the health tier as a bookmark icon; hover shows every issue."""

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
        try:
            level = TIERS.index(info['tier'])
        except ValueError:
            return
        icon = match_icons[level]
        icon_size = 16
        icon_margin = 2
        x = option.rect.x() + icon_margin
        y = option.rect.y() + (option.rect.height() - icon_size) // 2
        icon.paint(painter, QtCore.QRect(x, y, icon_size, icon_size))

    def _format_tooltip(self, info: dict[str, object]) -> str:
        tier = info['tier']
        issues = info['issues']
        notes = info.get('info') or []
        parts = [f"<b>{tier}</b>"]
        if info.get('changed_since_scan'):
            parts.append(
                "<div style='color:#b7950b;'>Audio content changed since last scan</div>"
            )
        if issues:
            items = "".join(f"<li>{issue}</li>" for issue in issues)
            parts.append(f"<ul style='margin-left:-20px;'>{items}</ul>")
        else:
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
        return QtCore.QSize(70, 20)


# Registered at import time, not inside enable(), to match how Picard's own
# core columns register (ALBUMVIEW_COLUMNS.insert(...) at columns.py import
# time) — this must happen before any tree view builds its header, which
# plugin enable() apparently runs too late for.
_HEALTH_COLUMN = make_delegate_column(
    "Health",
    '~health_tier',
    HealthProvider(),
    width=70,
    size=QtCore.QSize(60, 16),
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
        '_health_tier',
        documentation="Health tier from the last scan (Bad..Excellent).",
        title="Health",
    )
    api.register_script_variable(
        '_health_flags',
        documentation="Itemized list of issues found by the last scan.",
        title="Health flags",
    )
    api.register_script_variable(
        '_health_info',
        documentation="Informational notes that don't affect the health tier (e.g. mono content in a stereo container).",
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
