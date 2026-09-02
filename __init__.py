"""File Health — demo plugin.

DEMO ONLY: wires up fake per-file "health" stats to a visible column so we
can see what the surfacing mechanism (Custom Columns + script variable)
actually looks like in the UI, before implementing any real audio analysis.

The "health tier" and "flags" below are computed from a hash of the
filename, not from decoding audio. Replace `_fake_health_for` with real
signal analysis (clipping, spectral cutoff, LUFS, etc.) once the demo is
validated.
"""

from picard.file import File
from picard.plugin3.api import PluginApi
from picard.ui.itemviews.custom_columns.storage import (
    CustomColumnKind,
    CustomColumnSpec,
    register_and_persist,
)


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

    # Auto-provision visible columns so the values show up without the user
    # having to open the Custom Columns manager themselves.
    register_and_persist(
        CustomColumnSpec(
            title="Health",
            key="health_demo_tier",
            kind=CustomColumnKind.FIELD,
            expression="~health_tier",
            width=80,
            always_visible=True,
        )
    )
    register_and_persist(
        CustomColumnSpec(
            title="Health Flags",
            key="health_demo_flags",
            kind=CustomColumnKind.FIELD,
            expression="~health_flags",
            width=260,
            always_visible=True,
        )
    )


def disable() -> None:
    """Called when the plugin is disabled."""
