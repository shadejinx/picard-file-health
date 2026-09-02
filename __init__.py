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

from PyQt6 import (
    QtCore,
    QtGui,
    QtWidgets,
)

from picard.file import File
from picard.item import Item
from picard.plugin3.api import PluginApi
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


def _apply_fake_health(api: PluginApi, file: File) -> None:
    tier, flags = _fake_health_for(file.filename)
    file.metadata['~health_tier'] = tier
    file.metadata['~health_flags'] = flags
    file.update()


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
            return f"<b>{tier}</b><ul style='margin-left:-20px;'>{items}</ul>"
        return f"<b>{tier}</b><br>No issues detected"

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

    api.register_file_post_load_processor(_apply_fake_health)

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
