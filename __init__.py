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


def _scan_one(filename: str, ffmpeg_path: str | None) -> dict[str, object]:
    """Runs on a background thread — real decode + measurement work via
    ffmpeg (see analysis.py), not simulated.
    """
    try:
        result = analysis.analyze_file(filename, ffmpeg_path=ffmpeg_path)
    except analysis.FfmpegNotFoundError as exc:
        return {'error': str(exc)}
    return {
        'tier': result.tier,
        'flags': "; ".join(result.issues),
        'info': "; ".join(result.info),
        'content_hash': result.content_hash,
    }


def _scan_finished(file: File, result: dict[str, object] | None, error: BaseException | None) -> None:
    """Runs back on the main thread once _scan_one completes."""
    if result and not error:
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
    run_task(
        lambda f=file, p=ffmpeg_path: _scan_one(f.filename, p),
        lambda result=None, error=None, f=file: _scan_finished(f, result, error),
    )


class HealthOptionsPage(OptionsPage):
    NAME = "file_health"
    TITLE = "File Health (Demo)"
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

        ffmpeg_group = QtWidgets.QGroupBox("ffmpeg location", self)
        ffmpeg_layout = QtWidgets.QVBoxLayout(ffmpeg_group)

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
        layout.addStretch(1)

    def load(self) -> None:
        self.auto_scan_checkbox.setChecked(self.api.plugin_config['auto_scan'])
        self.ffmpeg_path_edit.setText(self.api.plugin_config['ffmpeg_path'])
        self._refresh_ffmpeg_status()

    def save(self) -> None:
        self.api.plugin_config['auto_scan'] = self.auto_scan_checkbox.isChecked()
        self.api.plugin_config['ffmpeg_path'] = self.ffmpeg_path_edit.text().strip()

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
            self.ffmpeg_status_label.setText(f"Found: {resolved}")
        except analysis.FfmpegNotFoundError as exc:
            self.ffmpeg_status_label.setText(str(exc))


class ScanHealthAction(BaseAction):
    """Right-click action that triggers the health scan on demand.

    Manual by default — real analysis needs to decode audio, which is
    neither instant nor safe to run inline on the file-load callback.
    An opt-in automatic mode is available via Options (off by default,
    same background-threaded scan either way). Each scan runs on a
    background thread via run_task, same pattern Picard's own AcoustID
    fingerprinting uses for fpcalc.
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
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path: _scan_one(f.filename, p),
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

    def __init__(self, ffmpeg_path: str | None, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Health Comparison (Demo)")
        self.setModal(False)
        self.resize(620, 380)
        self._ffmpeg_path = ffmpeg_path
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
        tie = len({r for r in ranks.values()}) == 1

        for file in group:
            tier = file.metadata['~health_tier']
            stored_flags = file.metadata['~health_flags']
            issue_parts = stored_flags.split("; ") if stored_flags else []
            if file.metadata['~health_changed_since_scan']:
                issue_parts.insert(0, "Changed since last scan")
            issues = "; ".join(issue_parts) or "—"
            item = QtWidgets.QTreeWidgetItem([file.base_filename, tier, issues])
            item.setData(0, _FILE_ROLE, file)
            if not tie and ranks[file] == best_rank:
                bold = item.font(0)
                bold.setBold(True)
                item.setFont(0, bold)
                item.setFont(1, bold)
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
        for file in files:
            file.set_pending()
            run_task(
                lambda f=file, p=ffmpeg_path: _scan_one(f.filename, p),
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
    files: list[File], parent: QtWidgets.QWidget, ffmpeg_path: str | None
) -> CompareResultsPanel | None:
    """Groups by Track (Picard's own matching decision, not our own tag
    comparison), opens the panel if there's anything to compare.
    """
    groups = {track: group for track, group in _group_by_track(files).items() if len(group) > 1}
    if not groups:
        return None
    panel = CompareResultsPanel(ffmpeg_path, parent)
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
    reasons (real, from analysis.analyze_file, stored in ~health_flags)
    are what actually inform that judgment.
    """

    TITLE = "Compare File Health (Demo)…"

    def callback(self, objs) -> None:
        files = list(iter_files_from_objects(objs))
        window = tagger_instance().window
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        panel = _open_compare_panel(files, window, ffmpeg_path)
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
        ffmpeg_path = self.api.plugin_config['ffmpeg_path'] or None
        panel = _open_compare_panel(_all_loaded_files(), window, ffmpeg_path)
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
    api.logger.info("File Health (demo) enabled")

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
