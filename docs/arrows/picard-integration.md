# Arrow: picard-integration

Every Picard-facing surface: the two tree columns and their icon-painting delegates, the five scan/details actions, the Options page, the Details window, the spectrogram viewer, the Help dialog, and `~health_*` metadata persistence.

## Status

**OK** — fully coherent as of 2026-09-06 (git SHA `5e1a9941b2ca8133a1ebcc6cc217cf9082676678`; uncommitted working-tree changes beyond this SHA are included in this audit — see Key Findings). All 21 specs implemented and annotated at their code entry point; every spec has at least one test citing it (26 tests total, some specs covered by more than one). No coverage gaps, no orphan or reverse-orphan spec IDs found.

## References

### HLD
- `docs/high-level-design.md` § System Design (`__init__.py`, `help_content.py` bullets), § Key Design Decisions ("help_content.py is 100% static.")

### LLD
- `docs/intent/picard-integration/picard-integration-design.md`

### EARS
- `docs/intent/picard-integration/picard-integration-specs.md` (21 specs: `UI-COL-*` ×5, `UI-ACTION-*` ×6, `UI-OPTIONS-*` ×2, `UI-DETAILS-*` ×2, `UI-SPECTRO-*` ×2, `UI-HELP-*` ×2, `UI-META-*` ×2)

### Tests
- `tests/test_picard_integration.py` (26 tests, using a duck-typed Qt/Picard harness — see Key Findings)

### Code
- `__init__.py` — columns (`FileHealthProvider`/`TrackHealthProvider`/`_SingleHealthColumnDelegate`), actions (`ScanFileHealthAction`/`ScanTrackHealthAction`/`ShowFileHealthDetailsAction`/`ShowAllFileHealthDetailsAction`), `HealthOptionsPage`, `DetailsPanel`, `SpectrogramDialog`, `HelpDialog`, `enable()`/`disable()`, metadata persistence (`_file_health_scan_finished`/`_track_health_scan_finished`/`_cache_health_metadata`/`_restore_health_metadata_on_match`)
- `help_content.py` — Help dialog's static HTML content (`SECTIONS`)

## Architecture

**Purpose:** Wire the two independent scores into every place a Picard user would want to see or act on them, while keeping `__init__.py` itself free of any ffmpeg-invocation logic — every measurement call goes through `analysis.py`.

**Key Components:**
1. Two sortable/filterable tree columns (icon + itemized-issues tooltip, HTML-escaped) registered on both file and album views, with a staggered-retry header rebuild so already-open tree views pick up the new column without a Picard restart.
2. Five actions: File Health scan (everywhere), Track Health scan (matched tracks only), Details window (one file, or "all files" from the Tools menu) — every scan on a background thread via `run_task`, with pending-state and error-status-bar handling for a file removed from every visible tree mid-scan.
3. `HealthOptionsPage` — ffmpeg path configuration (validated via `analysis.find_ffmpeg`/`get_ffmpeg_version` before display, not blindly accepted) and the six sensitivity sliders (widget/position only — see the `track-health-scoring` segment for who owns the real calibrated values).
4. `DetailsPanel` — recomputes every stoplight cell live from stored raw measurements against the *current* slider settings, not the tier recorded at scan time; bolds an unambiguous per-group winner only when the File-then-Track tier ranking isn't tied.
5. `SpectrogramDialog` — renders on explicit request only, with the temp PNG deleted on every exit path (success, a failed render, and an unexpected exception).
6. `HelpDialog`/`help_content.py` — entirely static HTML, no file/tag interpolation.
7. `~health_*` metadata persistence, including a cross-match side-cache (`_health_metadata_cache`) that survives Picard's own `File.copy_metadata()` wipe on match/unmatch, and a content-hash comparison that flags "changed since scan" — including after a re-save that clears an OK-capped tag/artwork issue (now documented in the Help dialog — see Key Findings).

## Spec Coverage

| Category | Spec IDs | Implemented | Deferred | Gaps |
|----------|----------|-------------|----------|------|
| Columns | COL-001 to 005 | 5 | 0 | 0 |
| Actions | ACTION-001 to 006 | 6 | 0 | 0 |
| Options Page | OPTIONS-001, 002 | 2 | 0 | 0 |
| Details Window | DETAILS-001, 002 | 2 | 0 | 0 |
| Spectrogram Viewer | SPECTRO-001, 002 | 2 | 0 | 0 |
| Help Dialog | HELP-001, 002 | 2 | 0 | 0 |
| Metadata Persistence | META-001, 002 | 2 | 0 | 0 |

**Summary:** 21 of 21 active specs implemented; 0 deferred; 0 gaps.

## Key Findings

1. **One gap closed this session.** `UI-HELP-002` (documenting that re-saving a file to clear an OK-capped issue triggers the same "changed since scan" indicator as any other edit) was an active gap identified during Phase 4's edge audit; `help_content.py`'s Details-window tab now carries that explanation, and the closing test asserts on the real `SECTIONS` text rather than the prior gap-documenting `xfail`.
2. **New Qt/Picard test harness built this session, not previously present.** `tests/conftest.py` gained an offscreen `qapp` fixture and duck-typed `FakeFile`/`FakeMetadata`/`FakePluginApi` stand-ins (plus `patch_tagger_instance`/`synchronous_run_task`) so this segment's 18 already-implemented specs could get real automated tests instead of relying on manual smoke-testing — the user explicitly chose this over accepting manual verification when the fork was surfaced. A subtle bug surfaced and was fixed during that build: the plugin's `from . import analysis` relative import, loaded via `importlib.util.spec_from_file_location`, was creating a second, distinct `analysis` module instance from the top-level one tests already import — causing exception-class identity mismatches (a monkeypatched `analysis.FfmpegNotFoundError` raised via one module instance didn't match an `except` clause written against the other) that triggered a hard PyQt6 `abort()` when the mismatch surfaced inside a Qt slot. Fixed by pre-registering `sys.modules["file_health_plugin.analysis"]` as an alias for the already-imported top-level module before executing the plugin package.
3. **Code-level `@spec` annotations now complete.** All 21 specs are annotated at their entry point in `__init__.py` (`ScanFileHealthAction`/`ScanTrackHealthAction`, `_file_health_scan_finished`/`_track_health_scan_finished`, `HealthOptionsPage`, `_check_cells`/`_render_group`, `_scan_file_health_one`/`_scan_track_health_one`/`_show_spectrogram`, `FileHealthProvider`/`TrackHealthProvider`/`_SingleHealthColumnDelegate`, `enable`) and `help_content.py` (`SECTIONS`). Tests fully cite every spec too.
4. **`UI-COL-005` closed this session.** Track Health's `Unplayable` tier had no entry in `TRACK_TIERS`/`TRACK_TIER_ICON_LEVEL`, so a Track Health decode failure rendered no icon at all and sorted indistinguishably from an unscanned file (both fell into the same `-1` lookup-miss fallback). The LLD already stated the correct intent (both columns share one five-level tier vocabulary); only the code had drifted from it. Fixed by adding `Unplayable` to both constants, reusing File Health's own icon level 0 rather than a distinct one, since both represent the identical underlying fact. Three new tests verify icon-level parity, correct sort rank, and that "Unplayable" is now distinguishable from "not yet scanned."

## Work Required

### Must Fix
None — all specs implemented and test-verified.

### Should Fix
None.

### Nice to Have
1. Six in-code comments in `analysis.py`/`__init__.py` still cite a bare `HANDOFF.md` filename rather than an `@spec`/decision-doc reference — a known, deliberately-deferred cleanup item per `AGENTS.md`, not blocking anything functional.
