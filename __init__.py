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


# Ordered worst-to-best so tier index doubles as a sort key.
TIERS = ("Bad", "Poor", "Ok", "Good", "Great", "Excellent")

FAKE_FLAGS = {
    "Bad": "Clipping detected; likely transcoded (16kHz cutoff)",
    "Poor": "Likely transcoded (17.5kHz cutoff)",
    "Ok": "Elevated noise floor (60Hz hum)",
    "Good": "",
    "Great": "",
    "Excellent": "",
}

TIER_COLORS = {
    "Bad": QtGui.QColor("#c0392b"),
    "Poor": QtGui.QColor("#e67e22"),
    "Ok": QtGui.QColor("#b7950b"),
    "Good": QtGui.QColor("#27ae60"),
    "Great": QtGui.QColor("#1e8449"),
    "Excellent": QtGui.QColor("#196f3d"),
}


def _fake_health_for(filename: str) -> tuple[str, str]:
    """Deterministic fake tier + flags derived from the filename.

    Stands in for a real analysis pipeline (clipping/spectral-cutoff/LUFS/
    etc.) so the same file always shows the same demo value across runs.
    """
    tier = TIERS[hash(filename) % len(TIERS)]
    return tier, FAKE_FLAGS[tier]


def _apply_fake_health(api: PluginApi, file: File) -> None:
    tier, flags = _fake_health_for(file.filename)
    file.metadata['~health_tier'] = tier
    file.metadata['~health_flags'] = flags
    file.update()


class HealthProvider(ColumnValueProvider, DelegateProvider):
    """Column that displays health tier as colored text with a flags tooltip."""

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

    def get_health_info(self, obj: Item) -> dict[str, str] | None:
        column_method = getattr(obj, 'column', None)
        if not callable(column_method):
            return None
        tier = column_method('~health_tier')
        if not tier:
            return None
        return {'tier': tier, 'flags': column_method('~health_flags')}

    def get_delegate_class(self) -> type[QtWidgets.QStyledItemDelegate]:
        return self._delegate_class


class HealthColumnDelegate(QtWidgets.QStyledItemDelegate):
    """Renders the health tier as colored text; flags appear only on hover."""

    def _get_info(self, index: QtCore.QModelIndex) -> dict[str, str] | None:
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
        color = TIER_COLORS.get(info['tier'], option.palette.text().color())
        painter.save()
        painter.setPen(QtGui.QPen(color))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            option.rect.adjusted(4, 0, -4, 0),
            int(QtCore.Qt.AlignmentFlag.AlignVCenter),
            info['tier'],
        )
        painter.restore()

    def helpEvent(
        self,
        event: QtGui.QHelpEvent | None,
        view: QtWidgets.QAbstractItemView | None,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> bool:
        info = self._get_info(index)
        if not info or not info['flags'] or event is None:
            return False
        QtWidgets.QToolTip.showText(event.globalPos(), info['flags'], view)
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


def enable(api: PluginApi) -> None:
    """Called when the plugin is enabled."""
    api.logger.info("File Health (demo) enabled")

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


def disable() -> None:
    """Called when the plugin is disabled."""
    registry.unregister(_HEALTH_COLUMN.key)
