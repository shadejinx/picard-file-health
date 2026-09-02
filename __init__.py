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
from picard.track import Track
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


_FILE_ROLE = QtCore.Qt.ItemDataRole.UserRole


class CompareResultsPanel(QtWidgets.QDialog):
    """Non-modal panel listing every file in each shared-identity group.

    Doesn't declare a winner — presents every file's tier and issues side
    by side, per group, and only bolds whichever scored higher within its
    own group as a subtle cue. The user decides; we show the data.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Health Comparison (Demo)")
        self.setModal(False)
        self.resize(620, 380)

        layout = QtWidgets.QVBoxLayout(self)

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
        self.remove_button = QtWidgets.QPushButton("Remove from Picard", self)
        self.remove_button.clicked.connect(self._remove_from_picard)
        self.trash_button = QtWidgets.QPushButton("Move to Trash…", self)
        self.trash_button.clicked.connect(self._trash_file)
        action_row.addWidget(self.show_button)
        action_row.addWidget(self.remove_button)
        action_row.addWidget(self.trash_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._update_button_states()

    def add_group(self, key: str, group: list[File]) -> None:
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
        tie = len({r for r in ranks.values()}) == 1

        for file in group:
            tier = file.metadata['~health_tier']
            issues = "; ".join(FAKE_ISSUES.get(tier, ())) or "—"
            item = QtWidgets.QTreeWidgetItem([file.base_filename, tier, issues])
            item.setData(0, _FILE_ROLE, file)
            if not tie and ranks[file] == best_rank:
                bold = item.font(0)
                bold.setBold(True)
                item.setFont(0, bold)
                item.setFont(1, bold)
            header.addChild(item)
        header.setExpanded(True)

    def _current_file(self) -> File | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, _FILE_ROLE)

    def _update_button_states(self) -> None:
        has_file = self._current_file() is not None
        self.show_button.setEnabled(has_file)
        self.remove_button.setEnabled(has_file)
        self.trash_button.setEnabled(has_file)

    def _remove_row_for(self, file: File) -> None:
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


def _open_compare_panel(files: list[File], parent: QtWidgets.QWidget) -> CompareResultsPanel | None:
    """Groups by Track (Picard's own matching decision, not our own tag
    comparison), opens the panel if there's anything to compare.
    """
    groups = {track: group for track, group in _group_by_track(files).items() if len(group) > 1}
    if not groups:
        return None
    panel = CompareResultsPanel(parent)
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
    file scored higher within its group, as a subtle cue, leaving the
    actual decision to the user. Matches the design decided earlier: a
    perceptual-distance metric like ViSQOL/Zimtohrli would tell you the
    files differ, but not which one is better; the directional gate-field
    reasons (what FAKE_ISSUES stands in for here) are what actually
    inform that judgment.
    """

    TITLE = "Compare File Health (Demo)…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        window = tagger_instance().window
        panel = _open_compare_panel(files, window)
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

    TITLE = "Compare All File Health (Demo)…"

    def callback(self, objs) -> None:
        window = tagger_instance().window
        panel = _open_compare_panel(_all_loaded_files(), window)
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
