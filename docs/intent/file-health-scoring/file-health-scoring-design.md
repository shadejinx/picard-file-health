---
parent: high-level-design
prefix: FH
---

# File Health Scoring

## Context and Design Philosophy

File Health answers one question: can this file be trusted structurally? It has no user-configurable sensitivity — whether a file decodes, whether its audio stream is internally consistent, and whether its tag/artwork structure parses cleanly are not matters of taste the way Track Health's mastering-quality judgments are. The scan is deliberately lighter than Track Health's (structural checks only, via the shared merged decode from the analysis engine) so it can run independently and cheaply — a user can check "is this file even trustworthy" without paying for the heavier perceptual-quality decode.

## Tier Scale

Five tiers, worst to best: `Unplayable`, `Bad`, `OK`, `Good`, `Excellent`. `Unplayable` is terminal — assigned when ffmpeg can't decode the file at all, distinct from every other tier in that no further measurement is meaningful (Track Health is also structurally impossible to measure on such a file; see Decisions & Alternatives on why the two scores stay independent regardless).

Above `Unplayable`, the tier is decided by two categorized issue lists:

- **Structural issues** — an audio corruption signature (the analysis engine's `bits_left` diagnostic) or a Xing-header size mismatch. Either forces `Bad` regardless of anything else, since nothing short of re-encoding from a clean source fixes them.
- **Tag/artwork issues** — corrupt embedded artwork or a malformed tag structure. These cap the tier at `OK` rather than forcing `Bad`, since Picard's own re-save already fixes both (re-adding artwork, or re-writing tags, clears the underlying defect).

A file with both a structural issue and a tag/artwork issue resolves to `Bad` — the structural check is evaluated first and, once forced, nothing recovers the tier upward. A file with only tag/artwork issues and no structural ones tops out at `OK` — but `OK` is not exclusively the issue-cap tier; see Clean-File Grading below for the other path that lands there.

## Clean-File Grading

A file with *no* structural or tag/artwork issues at all is graded by codec and bitrate rather than defaulting straight to `Excellent`: a lossless codec (FLAC, ALAC, WavPack, TTA, APE, or any `pcm_*`) is `Excellent`; a lossy codec is `Good` if its bitrate clears the published HydrogenAudio/Xiph "generally transparent" threshold for that codec, `OK` if it doesn't. A file with an unknown codec, missing bitrate data, or an unprobeable stream (`ffprobe` itself failed) also lands at `OK` — the safe, non-committal tier — rather than a false `Excellent`, since there's no structural defect to report but also no confident basis for a better verdict.

## Issue Categorization

`analyze_file_health` builds the two issue lists (`structural_issues`, `tag_artwork_issues`) directly at the point each detector runs, then passes both separately into tier computation — never by pattern-matching issue text after the fact. The two lists are unioned back together (`structural_issues + tag_artwork_issues`) for the result's flat `file_issues` field, so every issue still displays in tooltips/scripts regardless of which list it came from; only the tier computation itself sees the split.

## Result Fields

`FileHealthResult` carries: `file_tier`, `file_issues` (the flat itemized list — for `Unplayable`, this is a single specific reason string, e.g. why ffmpeg couldn't decode the file, never a silent empty list), `info` (informational notes that don't affect the tier — currently just the below-transparency-bitrate note), `content_hash` (for change detection — see the Picard Integration LLD), and `stream_info` (sample rate, codec, profile, bitrate, channels, reused by Track Health rather than re-probed).

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Severity split for tag/artwork issues | Cap at `OK`, categorized at detection time via two separate lists | Force `Bad` uniformly for every File Health issue (the original design); pattern-match issue text after the fact to decide severity | A `Bad` verdict for a problem Picard's own re-save already fixes trains users to distrust the tool's own severity signal. Categorizing at the detection call site (which already knows which detector fired) avoids fragile string-matching where structured data is already available. |
| No user-configurable sensitivity | Fixed thresholds, no sliders | Sliders matching Track Health's pattern | [inferred] "Is this file structurally sound" doesn't admit a taste-based answer the way "how much compression is too much" does — a corrupted decode or a size mismatch is decisive evidence, not a threshold judgment. |
| `Unplayable` as a terminal tier | No File Health issues computed past a decode failure; `Unplayable` short-circuits everything else | Attempt partial measurement (whatever ffmpeg can extract before failing) | [inferred] A decode failure is an environment/file fact orthogonal to what any specific check would report — nothing below it is meaningfully measurable, so reporting partial results would imply a confidence the data doesn't support. |

## Open Questions & Future Decisions

### Deferred
1. Whether a third category (e.g. "recoverable via a different tool, not Picard's re-save") is worth splitting out further has not come up in practice yet — the two-category split (structural vs. tag/artwork) has covered every check implemented so far.
2. Corrupt-artwork detection works by asking ffmpeg to decode the embedded cover image; a failure is reported as "corrupt." A valid image in a format ffmpeg's build genuinely doesn't support would look identical. Not worth resolving further: this only ever caps the tier at `OK` (never forces `Bad`), and the suggested fix — re-adding artwork in Picard — is the same regardless of which case it actually is.
3. When a user re-saves a file specifically to clear an `OK`-capped issue (the fix this design encourages), `content_hash` changes and "changed since last scan" will always read true afterward, even though that's the expected, desired outcome — indistinguishable from an unrelated, unexpected change. This is a presentation question (how the Picard Integration leaf's UI communicates the flag), not a scoring question; tracked there, not here.

## References

- `docs/intent/analysis-engine/analysis-engine-design.md` — the corruption-signature and size-mismatch detectors this scoring layer consumes.
- `.omp/HANDOFF.md` — pre-LID narrative for the severity-split redesign; not part of the arrow.
