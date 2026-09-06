# Arrow: file-health-scoring

The structural-only, no-sliders File Health score: tier assignment (Unplayable/Bad/OK/Good/Excellent) from decode success, structural corruption checks, tag/artwork issue categorization, and codec/bitrate transparency grading.

## Status

**OK** — fully coherent as of 2026-09-06 (git SHA `5b8d2e44cb567106c32d743bd49e25e17eba4583`). All 11 specs implemented and annotated at their code entry point; every spec has exactly one test citing it (11 tests total). No coverage gaps, no orphan or reverse-orphan spec IDs found.

## References

### HLD
- `docs/high-level-design.md` § Key Design Decisions ("File Health issues split by fixability, not just detected-vs-not.")

### LLD
- `docs/intent/file-health-scoring/file-health-scoring-design.md`

### EARS
- `docs/intent/file-health-scoring/file-health-scoring-specs.md` (11 specs: `FH-TIER-*` ×7, `FH-ISSUE-*` ×2, `FH-BROKEN-*` ×2)

### Tests
- `tests/test_file_health_scoring.py` (11 tests)

### Code
- `analysis.py` — `analyze_file_health`, `_broken_file_health_result`, `_compute_file_tier` and its structural/tag-artwork helper checks

## Architecture

**Purpose:** Answer "can this file be trusted structurally?" with no user-configurable sensitivity — a file either has a structural defect or it doesn't.

**Key Components:**
1. `analyze_file_health` — the entry point: decode check, corruption-signature detection, size-mismatch detection, artwork/tag validation, then tier computation.
2. Issue categorization at detection time into two lists (structural vs. tag/artwork) — the split that lets a tag/artwork-only issue cap at OK instead of forcing Bad.
3. `_compute_file_tier` — structural issues force Bad regardless of anything else; tag/artwork issues alone cap at OK; a clean file grades by codec/bitrate (lossless → Excellent, lossy above/below the published transparency threshold → Good/OK).
4. `_broken_file_health_result` — the Unplayable terminal state: a specific decode-failure reason, plus a content hash computed from raw bytes even though nothing else could be measured.

## Spec Coverage

| Category | Spec IDs | Implemented | Deferred | Gaps |
|----------|----------|-------------|----------|------|
| Tier Assignment | TIER-001 to 007 | 7 | 0 | 0 |
| Issue Categorization | ISSUE-001, 002 | 2 | 0 | 0 |
| Unplayable Terminal State | BROKEN-001, 002 | 2 | 0 | 0 |

**Summary:** 11 of 11 active specs implemented; 0 deferred; 0 gaps.

## Key Findings

1. **Code-level `@spec` annotations now complete.** All 11 specs are annotated at their entry point in `analysis.py` (`_broken_file_health_result`, `_compute_file_tier`, `analyze_file_health`). Tests fully cite every spec too.
2. **Precedence verified directly, not just documented** — `FH-TIER-002`'s "structural forces Bad regardless of any other issue" was tested with a file carrying *both* a structural corruption signature and a malformed-tag issue simultaneously, confirming Bad wins rather than relying on each check being tested only in isolation.

## Work Required

### Must Fix
None — all specs implemented and test-verified.

### Should Fix
None.

### Nice to Have
None noted.
