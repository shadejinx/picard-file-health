---
parent: high-level-design
prefix: TH
---

# Track Health Scoring

## Context and Design Philosophy

Track Health answers a different question than File Health: given a file that plays back fine, how does it actually sound? Every check here is a mastering/perceptual-quality judgment, and unlike File Health's structural checks, several of these admit genuine disagreement about where the line between "fine" and "a problem" sits — hence every threshold is a user-adjustable slider, not a fixed constant.

Track Health runs its own, heavier decode independently of File Health's — deliberately, so either scan can run without paying the other's cost, matching the product decision to keep File Health and Track Health as fully separate scans rather than two facets of one combined pass.

## Weighted Composite Scoring

Rather than the OR-gate architecture File Health uses (any one structural issue forces `Bad`), Track Health combines every applicable check into a single continuous 0..1 weighted composite score, then maps that score to a tier via fixed boundaries. Each check contributes a 0 (clean) to 1 (fails even the most lenient calibrated setting) sub-score, weighted by how confidently that check's measurement maps to an actually-audible defect:

| Check | Weight | Measurement |
|---|---|---|
| Clipping | 1.0 | astats Flat factor |
| True Peak | (downweighted) | ebur128 inter-sample peak |
| Spectral Cutoff | 1.0 | volumedetect above a lowpass, vs. a lossy-source cutoff frequency |
| Fake Hi-Res | (downweighted) | volumedetect above a higher cutoff, for declared >48kHz files |
| Out-of-Phase | 1.0 | aphasemeter's own phasing classification |
| DR14 | 1.0 | reimplemented Pleasurize Music Foundation dynamic-range algorithm |
| Mains Hum | (downweighted) | narrowband RMS elevation in a quiet passage |
| Noise Floor | (downweighted) | broadband RMS in a quiet passage |

True Peak, Fake Hi-Res, Mains Hum, and Noise Floor are downweighted relative to Clipping/Spectral-Cutoff/Out-of-Phase/DR14 — the latter four are direct, high-confidence measurements of a specific defect; the former four either sit inside a meter's own documented accuracy limit (True Peak) or can't be told apart with full certainty from a legitimate non-defective signal (mains hum vs. a sustained musical drone; a raised noise floor vs. quiet room tone; True Peak overs that are mostly the meter's own accuracy band, not genuine clipping).

Whether the phase and fake-hi-res branches even run is decided upfront from `stream_info`'s channel count and sample rate; if `stream_info` itself is unprobeable (`ffprobe` failed), those fields read as falsy and the branches are silently skipped — the same graceful-degradation convention the analysis engine uses everywhere else (see that LLD), not a distinct "unknown" state.

Clipping and True Peak overs can both be present on the same file; when they are, only one explanation (clipping) is shown in the itemized issue text, to avoid saying the same underlying problem twice. Scoring is not deduplicated the same way — both checks still contribute their full weighted score independently, since a file that's clipped *and* has separately-measured inter-sample overs is a more severe compounding case than either alone, even though the text only needs to name it once.

## Issue Reporting

Alongside the composite score, the system reports every fired check as an itemized issue string (the tooltip's content) — the same "report everything detected" convention File Health uses, though Track Health's issue list is never categorized into sub-groups the way File Health's structural/tag-artwork split is, since every Track Health issue is the same kind of thing (a perceptual/mastering judgment) rather than differing in fixability. The clipping/True-Peak deduplication above is the one exception where the score and the text diverge in what they count.

## Tier Boundaries

Five tiers: `Unplayable`, `Bad`, `OK`, `Good`, `Excellent` — the same names File Health uses, for consistent interpretation across both columns. `Unplayable` is assigned when the file's shared decode fails outright (the same physical fact File Health's own `Unplayable` tier reports, discovered independently by Track Health's own decode attempt, never by reading File Health's stored result — see Decisions & Alternatives). Above `Unplayable`, the remaining four tiers are decided by the 0..1 composite score, boundaries placed by percentile against a real 80-file calibration sample (scores clustered continuously with no sharp natural gap) rather than by a gap in the data:

- `score >= 0.35` → `Bad` (top ~6% of the calibration sample — reserved for a genuinely compounding pattern across multiple checks at once, not a single borderline reading)
- `score >= 0.25` → `OK`
- `score >= 0.15` → `Good`
- else → `Excellent`

## Sensitivity Thresholds

Six sensitivity controls: Clipping, True Peak, Spectral Cutoff, Out-of-Phase Channels, Compression Tolerance (DR14), Noise Floor. Each control's real calibrated values and explanatory hint text are owned here (Track Health), not by the Picard Integration leaf — the UI leaf owns only the slider widget itself, presenting the user a normalized position from 1 (most lenient) to 10 (most strict) and asking this leaf for the (value, hint) at whichever position the user picks, so the two leaves can't silently drift out of sync about what a given position actually means. Compression Tolerance (DR14) is a symmetric ±4 whole-DR-unit shift around its zero-shift "matches the official reference scale" center, which mathematically yields 9 positions, one short of the other five controls' 10; resolved by extending the lenient side to +5 (dropping the symmetry) rather than the strict side, since the default `Poor` cutoff (DR8) already flags a large share of ordinary, intentionally loud modern masters — a user is far more likely to want to loosen this control further than tighten an already-strict default. Each control's positions preserve the same relative spacing real calibration data validated — moving a slider shifts which raw measurement counts as how severe a defect, not just which issues get listed as text.

## Terminal State

When Track Health's own decode fails (the same underlying fact File Health's `Unplayable` tier reports), the system assigns the `Unplayable` tier directly rather than a bare absent value — a distinct, labeled terminal outcome from "not yet scanned," and one a user can interpret the same way regardless of which column they're reading.

A separate, theoretical case remains: a scan whose decode *succeeds* but produces literally no applicable measurement (every check's raw input came back unmeasurable). In practice every file that reaches Track Health's scoring already has at minimum clipping/true-peak measured off the first decode pass, so this path has never been observed; it is not currently assigned a distinct tier of its own, and would need one if it's ever actually reached.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Scoring architecture | Weighted composite (continuous 0..1 score, tier from fixed boundaries) | OR-gate (any one check firing forces `Bad`), matching File Health's own model | [inferred] The OR-gate architecture drove 58% of all `Bad` verdicts from a single borderline True Peak reading (median only half a dB past its own threshold) in a real 750-file validation sample — a single narrowly-missed threshold shouldn't carry the same verdict as a file failing multiple checks at once. |
| Tier count | 5 tiers (`Unplayable`/`Bad`/`OK`/`Good`/`Excellent`), matching File Health's names | 4 tiers (`Bad`/`OK`/`Good`/`Excellent`) with the decode-failure case left as an unlabeled absent value (the original design); 5 tiers with new percentile boundaries re-derived for the measurable range | Promoting the existing decode-failure case to a real, named `Unplayable` tier gives both scores the same vocabulary for a user reading both columns, without inventing new calibration data — the 4 measurable tiers' existing boundaries are untouched. Re-deriving new boundaries for a 5-way split of the measurable range was rejected: no real-library sample exists to validate a 5th measurable tier honestly. |
| Check weighting | Downweight True-Peak/Fake-Hi-Res/Mains-Hum/Noise-Floor relative to the other four | Equal weighting across all checks | [inferred] The downweighted checks each have a documented reason their measurement can't fully distinguish a genuine defect from a legitimate non-defective signal (meter accuracy limits, or an inherent ambiguity with normal musical content); equal weighting would let an ambiguous signal carry the same verdict-shaping power as a decisive one. |
| Independence from File Health | Two fully separate scores and scans, no shared tier scale, no cross-influence on scoring | A single combined score; File Health status gating Track Health's tier | See the HLD's Key Design Decisions — "can I trust this file" and "does this sound good" are different questions a user asks at different moments, and conflating them would force one file property to determine the answer to both. |
| Concurrency safety | Every scan builds its own local filter-branch list and thresholds per call, no shared mutable state | A shared/cached branch-list or thresholds object reused across calls | Confirmed no data race is possible under Picard's real concurrent background-thread pool: the merged-analysis filter-branch list is a fresh function-local value on every call, and thresholds are passed as a per-call parameter rather than mutated module state. |
| Sensitivity-value ownership | Track Health owns each slider's real calibrated values and hint text; the UI leaf owns only the widget, addressing positions by a normalized 1-10 scale | Leave the real values defined in the UI leaf (the original layout); have the UI leaf own a copy synced by convention | The original layout let the UI leaf silently drift out of sync with Track Health's own scoring if the two were ever edited independently, since both read from the same in-UI-file constants only by accident of implementation, not by an owned contract. A normalized position number is a stable contract neither leaf needs to renegotiate as calibration data changes. |
| Compression Tolerance step count | Extend to 10 positions by adding a +5 (more lenient) step, breaking the ±4 symmetry around the zero-shift reference point | Add a stricter -5 step instead; interpolate a half-step; accept 9 positions as the real contract | The default `Poor` cutoff (DR8) already flags a large share of ordinary, intentionally loud modern masters (per this project's own repeated real-library finding that loud modern mastering dominates typical libraries); a user reaching for this control is far more likely to need more tolerance than less, so the added position goes on the lenient side. |

## Open Questions & Future Decisions

### Deferred
1. The `Bad` threshold (0.35) was calibrated against an 80-file sample from one corpus ("master"); whether it generalizes to meaningfully different libraries (e.g. one with a very different genre/era mix) hasn't been re-validated. No recalibration is planned speculatively — this is a data/validation question that needs a fresh real-library sample if and when a systematically skewed distribution is actually observed, not something to pre-emptively adjust.

## References

- `docs/intent/analysis-engine/analysis-engine-design.md` — the underlying measurements (astats, ebur128, aphasemeter, DR14) this scoring layer combines.
- Pleasurize Music Foundation "TT DR Meter" specification — the DR14 algorithm and its documented quality bands (red below DR8, green at DR14+).
- `.omp/HANDOFF.md` — pre-LID narrative for the OR-gate-to-weighted-composite redesign; not part of the arrow.
