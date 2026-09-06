# Arrow: picard-integration

Every Picard-facing surface: the two tree columns and their icon-painting delegates, the five scan/details actions, the Options page, the Details window, the spectrogram viewer, the Help dialog, and `~health_*` metadata persistence.

## Status

**OK** — fully coherent as of 2026-09-06 (git SHA `59b11440fca982ea4de546b570620398eb7ba800`). All 24 specs implemented and annotated at their code entry point; every spec has at least one test citing it (31 tests total, some specs covered by more than one). No coverage gaps, no orphan or reverse-orphan spec IDs found.

## References

### HLD
- `docs/high-level-design.md` § System Design (`__init__.py`, `help_content.py` bullets), § Key Design Decisions ("help_content.py is 100% static.")

### LLD
- `docs/intent/picard-integration/picard-integration-design.md`

### EARS
- `docs/intent/picard-integration/picard-integration-specs.md` (24 specs: `UI-COL-*` ×5, `UI-ACTION-*` ×6, `UI-OPTIONS-*` ×2, `UI-DETAILS-*` ×3, `UI-SPECTRO-*` ×2, `UI-HELP-*` ×2, `UI-META-*` ×2, `UI-SEC-*` ×2)

### Tests
- `tests/test_picard_integration.py` (31 tests, using a duck-typed Qt/Picard harness — see Key Findings)

### Code
- `__init__.py` — columns (`FileHealthProvider`/`TrackHealthProvider`/`_SingleHealthColumnDelegate`), actions (`ScanFileHealthAction`/`ScanTrackHealthAction`/`ShowFileHealthDetailsAction`/`ShowAllFileHealthDetailsAction`), `HealthOptionsPage`, `DetailsPanel`, `SpectrogramDialog`, `HelpDialog`, `enable()`/`disable()`, metadata persistence (`_file_health_scan_finished`/`_track_health_scan_finished`/`_cache_health_metadata`/`_restore_health_metadata_on_match`)
- `help_content.py` — Help dialog's static HTML content (`SECTIONS`)

## Architecture

**Purpose:** Wire the two independent scores into every place a Picard user would want to see or act on them, while keeping `__init__.py` itself free of any ffmpeg-invocation logic — every measurement call goes through `analysis.py`.

**Key Components:**
1. Two sortable/filterable tree columns (icon + itemized-issues tooltip, HTML-escaped) registered on both file and album views, with a single synchronous reconciliation call at the end of `enable()` (header rebuild, delegate wiring, forced visibility) so already-open tree views pick up the new column without a Picard restart — see Key Findings for why this replaced an earlier staggered-retry approach.
2. Five actions: File Health scan (everywhere), Track Health scan (matched tracks only), Details window (one file, or "all files" from the Tools menu) — every scan on a background thread via `run_task`, with pending-state and error-status-bar handling for a file removed from every visible tree mid-scan.
3. `HealthOptionsPage` — ffmpeg path configuration (validated via `analysis.find_ffmpeg`/`get_ffmpeg_version` before display, not blindly accepted) and the six sensitivity sliders (widget/position only — see the `track-health-scoring` segment for who owns the real calibrated values).
4. `DetailsPanel` — recomputes every stoplight cell live from stored raw measurements against the *current* slider settings, not the tier recorded at scan time, with one matrix column for every check that contributes to the Track Health composite score (including checks with no dedicated slider — Fake Hi-Res, Mains Hum); bolds an unambiguous per-group winner only when the File-then-Track tier ranking isn't tied.
5. `SpectrogramDialog` — renders on explicit request only, with the temp PNG deleted on every exit path (success, a failed render, and an unexpected exception).
6. `HelpDialog`/`help_content.py` — entirely static HTML, no file/tag interpolation.
7. `~health_*` metadata persistence, including a cross-match side-cache (`_health_metadata_cache`) that survives Picard's own `File.copy_metadata()` wipe on match/unmatch, and a content-hash comparison that flags "changed since scan" — including after a re-save that clears an OK-capped tag/artwork issue (now documented in the Help dialog — see Key Findings).
8. Security posture documentation — the README states plainly what the plugin touches on a user's machine (local ffmpeg/ffprobe subprocess execution, one temp file, file metadata writes) and that it makes no network connections, per the HLD's Security Model.

## Spec Coverage

| Category | Spec IDs | Implemented | Deferred | Gaps |
|----------|----------|-------------|----------|------|
| Columns | COL-001 to 005 | 5 | 0 | 0 |
| Actions | ACTION-001 to 006 | 6 | 0 | 0 |
| Options Page | OPTIONS-001, 002 | 2 | 0 | 0 |
| Details Window | DETAILS-001 to 003 | 3 | 0 | 0 |
| Spectrogram Viewer | SPECTRO-001, 002 | 2 | 0 | 0 |
| Help Dialog | HELP-001, 002 | 2 | 0 | 0 |
| Metadata Persistence | META-001, 002 | 2 | 0 | 0 |
| Security Posture | SEC-001, 002 | 2 | 0 | 0 |

**Summary:** 24 of 24 active specs implemented; 0 deferred; 0 gaps.

## Key Findings

1. **One gap closed this session.** `UI-HELP-002` (documenting that re-saving a file to clear an OK-capped issue triggers the same "changed since scan" indicator as any other edit) was an active gap identified during Phase 4's edge audit; `help_content.py`'s Details-window tab now carries that explanation, and the closing test asserts on the real `SECTIONS` text rather than the prior gap-documenting `xfail`.
2. **New Qt/Picard test harness built this session, not previously present.** `tests/conftest.py` gained an offscreen `qapp` fixture and duck-typed `FakeFile`/`FakeMetadata`/`FakePluginApi` stand-ins (plus `patch_tagger_instance`/`synchronous_run_task`) so this segment's 18 already-implemented specs could get real automated tests instead of relying on manual smoke-testing — the user explicitly chose this over accepting manual verification when the fork was surfaced. A subtle bug surfaced and was fixed during that build: the plugin's `from . import analysis` relative import, loaded via `importlib.util.spec_from_file_location`, was creating a second, distinct `analysis` module instance from the top-level one tests already import — causing exception-class identity mismatches (a monkeypatched `analysis.FfmpegNotFoundError` raised via one module instance didn't match an `except` clause written against the other) that triggered a hard PyQt6 `abort()` when the mismatch surfaced inside a Qt slot. Fixed by pre-registering `sys.modules["file_health_plugin.analysis"]` as an alias for the already-imported top-level module before executing the plugin package.
3. **Code-level `@spec` annotations now complete.** All 24 specs are annotated at their entry point in `__init__.py` (`ScanFileHealthAction`/`ScanTrackHealthAction`, `_file_health_scan_finished`/`_track_health_scan_finished`, `HealthOptionsPage`, `_check_cells`/`_render_group`, `_scan_file_health_one`/`_scan_track_health_one`/`_show_spectrogram`, `FileHealthProvider`/`TrackHealthProvider`/`_SingleHealthColumnDelegate`, `enable`) and `help_content.py` (`SECTIONS`). Tests fully cite every spec too.
4. **`UI-COL-005` closed this session.** Track Health's `Unplayable` tier had no entry in `TRACK_TIERS`/`TRACK_TIER_ICON_LEVEL`, so a Track Health decode failure rendered no icon at all and sorted indistinguishably from an unscanned file (both fell into the same `-1` lookup-miss fallback). The LLD already stated the correct intent (both columns share one five-level tier vocabulary); only the code had drifted from it. Fixed by adding `Unplayable` to both constants, reusing File Health's own icon level 0 rather than a distinct one, since both represent the identical underlying fact. Three new tests verify icon-level parity, correct sort rank, and that "Unplayable" is now distinguishable from "not yet scanned."
5. **`UI-COL-002` reworked this session — the staggered-retry mechanism was solving a race that never existed.** A user report (Windows: columns unchecked by default, blank once manually checked, fixed only by "Restore default columns") led to tracing Picard's actual startup sequence (`tagger.py`, `plugin3/manager/lifecycle.py`): `MainWindow` and its tree views are always fully constructed before the plugin manager imports a plugin module or calls `enable()`, on every code path — there was never a timing window the four-attempt 0/250/1000/3000ms schedule needed to survive. The real gap was that no attempt ever forced column *visibility*, only the delegate — Picard's header-rebuild handler only reads back current Qt visibility, never applying `is_default`. Replaced the timer loop with one synchronous call at the end of `enable()` that rebuilds the header, wires the delegate, and force-shows both columns.
6. **`UI-DETAILS-003` added and closed this session.** Investigating a real-library report (5 same-session-encoded files, one reading Track Health `Bad` with no visible reason in the Details window) found Mains Hum — a real, weighted, documented composite-score check — had no matrix column at all, and `_scan_track_health_one` didn't even forward `has_mains_hum` into persisted metadata for one to read back. The Details-window matrix previously only covered checks with a dedicated Options-page slider; fixed by adding the missing metadata field and a `Mains Hum` column, and by writing the spec to require every composite-score check to have a column regardless of slider ownership, closing the same class of gap for good.
7. **`UI-SEC-001`/`UI-SEC-002` added this session**, cascaded from the HLD's new Security Model section: a static no-network-import guard, and a README capability-disclosure requirement for users evaluating a Community/Unregistered-trust install per Picard's own trust-based plugin security model.

## Work Required

### Must Fix
None — all specs implemented and test-verified.

### Should Fix
None.

### Nice to Have
1. Six in-code comments in `analysis.py`/`__init__.py` still cite a bare `HANDOFF.md` filename rather than an `@spec`/decision-doc reference — a known, deliberately-deferred cleanup item per `AGENTS.md`, not blocking anything functional.
