# Track Health Scoring — EARS Specs

## Weighted Composite Scoring

- [x] **TH-SCORE-001**: The system shall combine every applicable Track Health check into a single continuous weighted composite score rather than forcing a tier from any one check alone.
- [x] **TH-SCORE-002**: The system shall exclude an inapplicable or unmeasurable check from the composite score rather than treating it as a clean (zero-defect) value.
- [x] **TH-SCORE-003**: The system shall weight Clipping, Spectral Cutoff, Out-of-Phase, and DR14 at full weight in the composite score.
- [x] **TH-SCORE-004**: The system shall weight True Peak, Fake Hi-Res, Mains Hum, and Noise Floor below Clipping/Spectral-Cutoff/Out-of-Phase/DR14 in the composite score, since each can't be told apart from a legitimate non-defective signal with full certainty.
- [x] **TH-SCORE-005**: When a file exhibits both clipping and true-peak overs, the system shall include both checks' full weighted contribution in the composite score, even though the itemized issue text names only clipping.

## Issue Reporting

- [x] **TH-ISSUE-001**: The system shall report every fired Track Health check as an itemized issue string, alongside the composite score.

## Tier Mapping

- [x] **TH-TIER-001**: Where a file's Track Health decode succeeds, the system shall map the composite score to one of four tiers (Bad/OK/Good/Excellent) using fixed, percentile-calibrated score boundaries.
- [x] **TH-TIER-002**: If Track Health's own decode fails, then the system shall assign the Unplayable tier, independently of any File Health result for the same file.

## Sensitivity Thresholds

- [x] **TH-SLIDER-001**: The system shall let the user adjust six sensitivity thresholds (Clipping, True Peak, Spectral Cutoff, Out-of-Phase Channels, Compression Tolerance, Noise Floor) by a normalized position from 1 (most lenient) to 10 (most strict), owning the real calibrated value and hint text at each position independently of the UI leaf that presents the control.
- [x] **TH-SLIDER-002**: The system shall apply the currently configured thresholds independently per scan rather than mutating shared state, so concurrent scans of different files cannot interfere with each other.

## Merged-Analysis Branch Selection

- [x] **TH-BRANCH-001**: The system shall decide whether to include the phase-check and fake-hi-res branches in the merged analysis from the file's probed channel count and sample rate, before any filter pass runs.
