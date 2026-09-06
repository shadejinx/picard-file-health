# Arrow: track-health-scoring

The perceptual, fully slider-configurable Track Health score: a weighted composite from clipping, true peak, spectral cutoff, fake-hi-res, out-of-phase, DR14, mains hum, and noise floor, mapped to a Bad/OK/Good/Excellent tier (or Unplayable on decode failure) — plus the six sensitivity sliders' calibration data and per-scan threshold isolation.

## Status

**OK** — fully coherent as of 2026-09-06 (git SHA `5b8d2e44cb567106c32d743bd49e25e17eba4583`). All 11 specs implemented and annotated at their code entry point; every spec has exactly one test citing it (18 tests total, some specs covered by more than one). No coverage gaps, no orphan or reverse-orphan spec IDs found.

## References

### HLD
- `docs/high-level-design.md` § System Design (`analysis.py` bullet) and § Key Design Decisions ("Two independent scores, not one.")

### LLD
- `docs/intent/track-health-scoring/track-health-scoring-design.md`

### EARS
- `docs/intent/track-health-scoring/track-health-scoring-specs.md` (11 specs: `TH-SCORE-*` ×5, `TH-ISSUE-001`, `TH-TIER-*` ×2, `TH-SLIDER-*` ×2, `TH-BRANCH-001`)

### Tests
- `tests/test_track_health_scoring.py` (18 tests)

### Code
- `analysis.py` — `analyze_track_health`, `compute_track_score`, `_broken_track_health_result`, `track_tier_from_score`, `sensitivity_step`/`sensitivity_default_position`, `TRACK_HEALTH_WEIGHTS`
- `__init__.py` — `HealthOptionsPage`'s six `_SensitivitySlider` instances (widget/position only; real calibrated values come from `analysis.py`)

## Architecture

**Purpose:** Answer "how does this file actually sound?" as one continuous weighted composite (0=clean, 1=fails every applicable check at its most lenient setting) rather than any single check forcing a tier — the redesign that replaced an old OR-gate architecture where one borderline True Peak reading alone drove most "Bad" verdicts.

**Key Components:**
1. `compute_track_score` — per-check 0..1 defect scores via calibrated step tables (clipping, true peak, spectral cutoff, fake-hi-res, noise floor) or continuous interpolation (DR14) or binary (out-of-phase, mains hum), weighted-averaged; excludes (not zero-fills) any unmeasured check.
2. `TRACK_HEALTH_WEIGHTS` — full weight (1.0) for high-confidence direct measurements (clipping, spectral cutoff, out-of-phase, DR14); reduced weight (0.3–0.5) for checks that can't be told apart from a legitimate non-defective signal with full certainty (true peak, fake hi-res, mains hum, noise floor).
3. `sensitivity_step`/`sensitivity_default_position` — analysis.py's own ownership of the six sliders' real calibrated (value, hint) pairs at 10 lenient→strict positions, and each slider's empirically-calibrated default position (closed out this session; see Key Findings).
4. `_broken_track_health_result` — the Unplayable terminal state, matching File Health's own tier name for the same underlying "won't decode" fact, without either scan reading the other's stored result (closed out this session).
5. `_run_merged_analysis`'s branch-selection inputs (`include_phase_check`, `include_hires_check`) — decided from ffprobe-probed channel count and sample rate before any filter pass runs.

## Spec Coverage

| Category | Spec IDs | Implemented | Deferred | Gaps |
|----------|----------|-------------|----------|------|
| Weighted Composite Scoring | SCORE-001 to 005 | 5 | 0 | 0 |
| Issue Reporting | ISSUE-001 | 1 | 0 | 0 |
| Tier Mapping | TIER-001, 002 | 2 | 0 | 0 |
| Sensitivity Thresholds | SLIDER-001, 002 | 2 | 0 | 0 |
| Merged-Analysis Branch Selection | BRANCH-001 | 1 | 0 | 0 |

**Summary:** 11 of 11 active specs implemented; 0 deferred; 0 gaps.

## Key Findings

1. **Two gaps closed this session.** `TH-TIER-002` (decode failure now assigns `FILE_TIER_BROKEN`/"Unplayable" instead of `None`) and `TH-SLIDER-001` (the six sliders' calibrated value+hint data moved from `__init__.py` into `analysis.py`, with `__init__.py` now deriving its widget lists from `analysis.sensitivity_step()` rather than keeping a separate hardcoded copy) were both identified as active gaps during this project's Phase 4 edge audit, red-tested during Phase 5, and implemented during Phase 6 — see both functions' own docstrings and `@spec` annotations for the design rationale.
2. **DR14 shift slider extended from 9 to 10 positions** as part of closing `TH-SLIDER-001` — a new most-lenient `+5.0` step was added so this slider has the same 1..10 range as the other five, per an LLD decision already recorded before this session's Phase 6 work (`track-health-scoring-design.md`'s Decisions table).
3. **Code-level `@spec` annotations now complete.** All 11 specs are annotated at their entry point in `analysis.py` (`compute_track_score`, `TRACK_HEALTH_WEIGHTS`, `track_tier_from_score`, `analyze_track_health`, plus `_broken_track_health_result`/`sensitivity_step`/`sensitivity_default_position` from this session's earlier gap closures). Tests fully cite every spec too.

## Work Required

### Must Fix
None — all specs implemented and test-verified.

### Should Fix
None.

### Nice to Have
None noted.
