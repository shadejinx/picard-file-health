"""File Health — demo plugin.

DEMO ONLY: wires up fake per-file "health" stats to a visible column so we
can see what the surfacing mechanism actually looks like in the UI, before
implementing any real audio analysis.

Surfaces one compact "Health" column (colored tier text, matching Picard's
own match-quality delegate-column pattern) with the itemized reasons in a
hover tooltip, rather than a separate always-visible text column — a wide
free-text column doesn't scale as more checks (clipping, transcode, hum,
phase...) get added, isn't sortable/filterable, and duplicates information
better shown on demand.

The "health tier" and "flags" below are computed from a hash of the
filename, not from decoding audio. Replace `_fake_health_for` with real
signal analysis (clipping, spectral cutoff, LUFS, etc.) once the demo is
validated.
"""

import time

from PyQt6 import (
    QtCore,
    QtGui,
    QtWidgets,
)

from picard import tagger_instance
from picard.file import File
from picard.item import Item
from picard.plugin3.api import (
    BaseAction,
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


# Ordered worst-to-best so tier index doubles as a sort key.
TIERS = ("Bad", "Poor", "Ok", "Good", "Great", "Excellent")

# Per-tier list of demo issues. Only "Excellent" is genuinely clean — every
# other tier has to have a reason it isn't, even if the reason is minor.
FAKE_ISSUES: dict[str, tuple[str, ...]] = {
    "Bad": ("Clipping detected", "Likely transcoded (16kHz cutoff)"),
    "Poor": ("Likely transcoded (17.5kHz cutoff)",),
    "Ok": ("Elevated noise floor (60Hz hum)",),
    "Good": ("Bitrate below transparency threshold for codec",),
    "Great": ("Slightly reduced dynamic range (DR9)",),
    "Excellent": (),
}


def _fake_health_for(filename: str) -> tuple[str, str]:
    """Deterministic fake tier + flags derived from the filename.

    Stands in for a real analysis pipeline (clipping/spectral-cutoff/LUFS/
    etc.) so the same file always shows the same demo value across runs.
    """
    tier = TIERS[hash(filename) % len(TIERS)]
    return tier, "; ".join(FAKE_ISSUES[tier])


def _scan_one(filename: str) -> tuple[str, str]:
    """Run on a background thread. Simulates real analysis (decode + measure)
    taking noticeable time, instead of computing instantly inline.
    """
    time.sleep(0.5)
    return _fake_health_for(filename)


def _scan_finished(file: File, result: tuple[str, str] | None, error: BaseException | None) -> None:
    """Runs back on the main thread once _scan_one completes."""
    if result and not error:
        tier, flags = result
        file.metadata['~health_tier'] = tier
        file.metadata['~health_flags'] = flags
    file.clear_pending()
    file.update()


class ScanHealthAction(BaseAction):
    """Right-click action that triggers the (fake) health scan on demand.

    Not automatic on file load — real analysis needs to decode audio, which
    is neither instant nor safe to run inline on the file-load callback.
    Each file's scan runs on a background thread via run_task, same pattern
    Picard's own AcoustID fingerprinting uses for fpcalc.
    """

    TITLE = "Scan File Health (Demo)…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        if not files:
            return
        tagger_instance().window.set_statusbar_message(
            "Scanning file health for %(count)d file(s)…",
            {'count': len(files)},
            echo=None,
        )
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file: _scan_one(f.filename),
                lambda result=None, error=None, f=file: _scan_finished(f, result, error),
            )


def _identity_key(file: File) -> str | None:
    """Best available "same recording" signal for a file.

    Prefers AcoustID (audio-fingerprint match, set after Scan/AcoustID
    lookup) over the MusicBrainz recording MBID (set after a plain
    metadata Lookup) — AcoustID is evidence about the actual audio content,
    the recording MBID is evidence about the tag-matched identity, which is
    weaker but still meaningful for grouping.
    """
    acoustid = file.metadata['acoustid_id']
    if acoustid:
        return f"acoustid:{acoustid}"
    recording_id = file.metadata['musicbrainz_recordingid']
    if recording_id:
        return f"recording:{recording_id}"
    return None


def _group_by_identity(files: list[File]) -> dict[str, list[File]]:
    groups: dict[str, list[File]] = {}
    for file in files:
        key = _identity_key(file)
        if key:
            groups.setdefault(key, []).append(file)
    return groups


def _tier_rank(file: File) -> int:
    """Higher is better; -1 means not yet scanned."""
    try:
        return TIERS.index(file.metadata['~health_tier'])
    except ValueError:
        return -1


class CompareHealthAction(BaseAction):
    """Right-click action that compares files sharing the same recording.

    Groups the selection by AcoustID (falling back to the MusicBrainz
    recording MBID), then for each group with more than one file and a
    health-tier gap, names a recommended file and the specific reason the
    other one lost — not a bare distance number. Matches the design
    decided earlier: a perceptual-distance metric like ViSQOL/Zimtohrli
    would tell you the files differ, but not which one is better; the
    directional gate fields (what FAKE_ISSUES stands in for here) are what
    actually decide a winner.
    """

    TITLE = "Compare File Health (Demo)…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        groups = {key: group for key, group in _group_by_identity(files).items() if len(group) > 1}
        window = tagger_instance().window
        if not groups:
            window.set_statusbar_message(
                "No two selected files share the same AcoustID or recording.",
                echo=None,
            )
            return

        sections = []
        for key, group in groups.items():
            unscanned = [f for f in group if not f.metadata['~health_tier']]
            if unscanned:
                names = ", ".join(f.base_filename for f in unscanned)
                sections.append(f"{key}: not all files scanned yet ({names}) — run Scan File Health first.")
                continue

            ranked = sorted(group, key=_tier_rank, reverse=True)
            best, worst = ranked[0], ranked[-1]
            best_tier = best.metadata['~health_tier']
            worst_tier = worst.metadata['~health_tier']

            if best_tier == worst_tier:
                sections.append(f"{key}: {len(group)} files, all rated {best_tier} — no clear winner.")
                continue

            reasons = FAKE_ISSUES.get(worst_tier, ())
            reason_text = reasons[0] if reasons else "unspecified"
            sections.append(
                f"{key}:\n"
                f"  Recommended: {best.base_filename} ({best_tier})\n"
                f"  Over: {worst.base_filename} ({worst_tier}) — {reason_text}"
            )

        QtWidgets.QMessageBox.information(window, "File Health Comparison (Demo)", "\n\n".join(sections))


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
        return {'tier': tier, 'issues': FAKE_ISSUES.get(tier, ())}

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
        if issues:
            items = "".join(f"<li>{issue}</li>" for issue in issues)
            body = f"<b>{tier}</b><ul style='margin-left:-20px;'>{items}</ul>"
        else:
            body = f"<b>{tier}</b><br>No issues detected"
        return f"<div style='white-space:nowrap;'>{body}</div>"

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
    api.logger.info("File Health (demo) enabled")

    # Picard's own match_icons list is populated lazily; Picard core likely
    # already loaded it, but don't rely on load order — reusing Picard's
    # own bookmark icons (not new plugin-bundled assets) needs this.
    load_match_icons()
    api.register_script_variable(
        '_health_tier',
        documentation="Demo-only fake health tier (Bad..Excellent).",
        title="Health",
    )
    api.register_script_variable(
        '_health_flags',
        documentation="Demo-only fake list of detected issues.",
        title="Health flags",
    )

    api.register_file_action(ScanHealthAction)
    api.register_track_action(ScanHealthAction)
    api.register_cluster_action(ScanHealthAction)

    api.register_file_action(CompareHealthAction)
    api.register_track_action(CompareHealthAction)
    api.register_cluster_action(CompareHealthAction)

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
