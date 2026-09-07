# Arrow: analysis-engine

The pure-Python, ffmpeg-shelling measurement engine underneath both health scores — subprocess invocation, ffmpeg version resolution, defensive stderr parsing, structural corruption/tag checks, the merged-decode architecture, DR14 computation, and spectrogram rendering.

## Status

**OK** — fully coherent as of 2026-09-07 (git SHA `c2f338ce7d56efa6ed2ded7c2b9a9c7f2b154649`). All 30 specs implemented and annotated at their code entry point; every spec has at least one test citing it (39 tests total, some specs covered by more than one). No coverage gaps, no orphan or reverse-orphan spec IDs found.

## References

### HLD
- `docs/high-level-design.md` § System Design (`analysis.py` bullet) and § Key Design Decisions ("Single merged ffmpeg decode...", "No shell, ever.", "ffmpeg's own stderr diagnostic output is parsed defensively...")

### LLD
- `docs/intent/analysis-engine/analysis-engine-design.md`

### EARS
- `docs/intent/analysis-engine/analysis-engine-specs.md` (30 specs: `ENGINE-SUBPROC-*` ×5, `ENGINE-VERSION-*` ×9, `ENGINE-STDERR-*` ×4, `ENGINE-CORRUPT-*` ×4, `ENGINE-MERGE-001`, `ENGINE-DR14-*` ×3, `ENGINE-SPECTRO-*` ×2, `ENGINE-SEC-*` ×2)

### Tests
- `tests/test_analysis_engine.py` (39 tests)
- `tests/test_fixtures_smoke.py` (8 tests — fixture-generation smoke tests, not spec-tracing; confirms conftest.py's synthetic fixtures produce the conditions the real spec tests assume)
- `tests/conftest.py` (shared synthetic-audio fixtures)

### Code
- `analysis.py` — the entire engine lives in this one module

## Architecture

**Purpose:** Everything that shells out to ffmpeg/ffprobe or parses their output, decoupled from Picard entirely — importable and testable standalone (confirmed by this segment's own test suite, which never imports Picard).

**Key Components:**
1. Subprocess helper (`_run_ffmpeg`/`_run_ffprobe`-style wrappers) — list-argv only, never a shell; Windows console suppression; non-UTF8 stderr tolerance; synthetic failure on timeout/OS-level error.
2. ffmpeg version/path resolution (`find_ffmpeg`, `check_ffmpeg_version`, `_find_ffmpeg_windows_fallback`) — PATH search, explicit-path validation, version-string parsing with a graceful "unparseable → accept" fallback, and a Windows-only third tier (WinGet shim/package directories, conventional install directories) when PATH search fails.
3. Defensive stderr parsers, each scoped to one filter's own tagged log lines (not a bare substring match) — this is the segment's core security property, since a scanned file's own tag values appear in the same stderr stream.
4. Structural/corruption checks: bit-reservoir corruption counting, Xing-header size-mismatch detection, strict tag-structure validation (independent of ffmpeg's own lenient reader), embedded-artwork decode validation.
5. Merged-decode graph builder (`_run_merged_analysis`) — one ffmpeg invocation running every whole-file measurement that doesn't depend on another's result, via `asplit` into independently-tagged branches.
6. DR14 computation (Pleasurize Music Foundation TT DR Meter formula) over ffmpeg's own block-level RMS/Peak `astats` output.
7. Spectrogram rendering (`generate_spectrogram`) — on-demand only, reports failure on non-zero exit or a missing output file.

## Spec Coverage

| Category | Spec IDs | Implemented | Deferred | Gaps |
|----------|----------|-------------|----------|------|
| Subprocess Invocation | SUBPROC-001 to 005 | 5 | 0 | 0 |
| ffmpeg Version Resolution | VERSION-001 to 009 | 9 | 0 | 0 |
| Defensive stderr Parsing | STDERR-001 to 004 | 4 | 0 | 0 |
| Corruption/Structural Checks | CORRUPT-001 to 004 | 4 | 0 | 0 |
| Merged-Decode Architecture | MERGE-001 | 1 | 0 | 0 |
| Dynamic Range (DR14) | DR14-001 to 003 | 3 | 0 | 0 |
| Spectrogram Rendering | SPECTRO-001, 002 | 2 | 0 | 0 |
| Security Posture | SEC-001, 002 | 2 | 0 | 0 |

**Summary:** 30 of 30 active specs implemented; 0 deferred; 0 gaps.

## Key Findings

1. **Code-level `@spec` annotations now complete.** All 30 specs are annotated at their entry point in `analysis.py` (`_run_subprocess`, `_run_ffmpeg_filter`, `find_ffmpeg`, `check_ffmpeg_version`, `_filter_instance_output`, `_detect_corruption_signature`, `_parse_ebur128`, `_detect_size_mismatch`, `_detect_artwork_corruption`, `_detect_tag_structure_error`, `_run_merged_analysis`, `_compute_dr14`, `generate_spectrogram`). Tests fully cite every spec too.
2. **`ENGINE-SEC-001`/`ENGINE-SEC-002` added 2026-09-06**, cascaded from the HLD's new Security Model section: static guards confirming this engine never evaluates, executes, or deserializes anything constructed from a scanned file's own content, and never opens a network connection.
3. **`ENGINE-VERSION-005` through `-009` added 2026-09-07**: `find_ffmpeg`'s Windows-only fallback search (WinGet shim directory, WinGet per-package directories for the two community ffmpeg packages, conventional install directories) for when PATH search fails, working around a known WinGet PATH-registration defect.

## Work Required

### Must Fix
None — all specs implemented and test-verified.

### Should Fix
None.

### Nice to Have
None noted.
