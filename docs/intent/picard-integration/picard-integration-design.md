---
parent: high-level-design
prefix: UI
---

# Picard Integration

## Context and Design Philosophy

This leaf owns every point of contact between the two scoring engines (File Health, Track Health) and Picard's own UI: tree columns, right-click actions, the Options page, the Details window, the spectrogram viewer, and the in-app Help dialog. It lives entirely in `__init__.py` and contains no ffmpeg-invocation logic of its own — every measurement call goes through the analysis engine.

These six sub-features share one parent intent (surfacing and acting on health results inside Picard's existing workflow) rather than being six unrelated concerns bundled together, which is why they're kept as facets of one leaf rather than six separate LLDs. Given `__init__.py`'s size, this leaf is an explicit candidate for future promotion to a sub-HLD with each facet below becoming its own child LLD, if it outgrows itself further — not a decision made now, since none of the six facets has yet grown past what a shared leaf can hold coherently.

## Facet: Columns

Two sortable/filterable tree columns (File Health, Track Health), each rendered by a shared icon-painting delegate that shows the tier as a colored bookmark-style icon (reusing Picard's own match-quality icon set) with an itemized-issues tooltip on hover, rather than a wide always-visible text column — text doesn't scale as more checks get added, isn't sortable/filterable the way a tier-ranked icon is, and duplicates information better shown on demand. Both columns use the same five-level icon set (`Unplayable`/`Bad`/`OK`/`Good`/`Excellent`) — legitimate now that both scores share that exact tier vocabulary (see the Track Health LLD), not merely styled to look parallel while meaning different things.

Columns are registered late (during plugin `enable()`, after Picard's own tree views are already constructed at startup), which means an already-open tree view's Qt column count doesn't automatically grow to match — a `header_events.headers_updated` signal, emitted at staggered delays (0/250/1000/3000ms after `enable()`) rather than once, forces every live tree view to rebuild its header and pick up the new column. A single immediate or single deferred emit isn't reliable: a slow/cold startup (slower disk, the plugin registry's own network fetch) can push a tree view's actual construction past a single deferred tick, leaving the emit a silent no-op with zero listeners yet connected. The fixed four-attempt schedule has no final "did the column actually appear" verification step — accepted as-is: confirmed working against a real slow-startup case during development, and a genuine verification step would mean inspecting a live window's rendered state from outside its own paint cycle, which nothing else in this codebase does.

The delegate-attachment step that wires icon painting onto already-open tree widgets identifies a Health column by duck-typing (any widget with a `.columns` attribute matching a known column object). That attribute belongs to Picard core's own custom-column base class, not something this plugin defines — a collision would mean another plugin is already imitating a Picard-core internal convention, which is a problem for that plugin and Picard core, not something this leaf needs its own defense against.

The status icon is left-aligned within its cell (a small fixed left padding), matching the column header's own left-aligned label — centering (the original behavior) looked fine at a narrow default column width but visibly drifted from the header label once a user widened the column, which is common for this column since its tooltip carries the itemized issue list.

## Facet: Actions

Five actions: File Health scan (available everywhere — unmatched files/clusters and matched tracks, the only scan the left/unmatched side ever shows), Track Health scan (matched-track context only, since Track Health's per-check sliders are framed around a track's context), a Details-window launcher, and a Tools-menu "scan everything" action. Every scan runs on a background thread via Picard's own thread pool (`run_task`, the same mechanism Picard's core AcoustID fingerprinting uses) — never blocking the UI thread — and marks the file `pending` for the duration, clearing it and posting a status-bar message on completion or on any unexpected error (an unhandled exception surfacing an error message rather than leaving the file silently stuck in `pending` forever). A scan that completes after its file was removed from every visible tree (or Picard is closing) safely no-ops: Picard core's own `File.update_item()` only touches the UI when a `ui_item` still exists, and `clear_pending()` only acts while the file is actually still in the `PENDING` state — this plugin relies on that existing core guarantee rather than adding its own.

## Facet: Options Page

Configures the ffmpeg binary path (validated against `PATH` with a manual override, with a "Get ffmpeg" link for a user who has none installed) and the six Track Health sensitivity sliders (Clipping, True Peak, Spectral Cutoff, Out-of-Phase Channels, Compression Tolerance, Noise Floor). This leaf owns only the slider widgets themselves — each presents the user a position from 1 (most lenient) to 10 (most strict) and asks the Track Health Scoring LLD for the real calibrated value and hint text at that position, rather than holding those values itself (see that LLD for why). Labels and tooltips are kept short by convention; a slider's numeric threshold value is appended by the shared rendering template automatically rather than spelled out in each tooltip's own text.

## Facet: Details Window

A non-modal panel showing File Health/Track Health details for any selected files, grouping matched duplicates together for side-by-side comparison (as well as singleton and completely unmatched files) — the primary use case is deciding which of several near-duplicate files to keep. Per-check matrix cells recompute live from each file's stored raw measurements against the *current* slider settings, not the tier baked in at last scan time — so adjusting a slider updates the Details window's display immediately without requiring a re-scan. An old scan whose stored data predates a check added later simply has no raw value for that check, which the scoring function already excludes the same way it excludes any other unmeasured input — no special "insufficient data" case needed beyond the graceful-degradation convention every check already follows. The healthiest copy in a group of duplicates is subtly bolded as a visual cue when ranking is unambiguous.

## Facet: Spectrogram Viewer

An on-demand visual check (a button in the Details window) for cases where a number alone doesn't settle a judgment call — seeing an actual frequency-content cutoff or a pop/click's spectral signature directly. Rendering happens on a background thread (a real extra decode-and-render cost); the temp PNG this leaf's own `render()` closure creates is cleaned up on every exit path, including an ffmpeg-not-found or other unexpected exception mid-render, not just the two normal-return paths (success, or a failed render).

## Facet: Help Dialog

A four-tab in-app reference (what each tier means for both scores, what each check does, the Details window's own UI conventions, troubleshooting) built entirely from static HTML strings in `help_content.py` — no filename, tag value, or any other file-derived string is ever interpolated into it. This is a deliberate, structural guarantee: the Help dialog explains the checks and thresholds themselves, never any one file's specific results, so it's immune by construction to any injection concern a crafted file's tag content might otherwise raise. Enforced by convention only today (no test asserts the Help content is input-independent); a future regression test is reasonable but not urgent.

## Metadata Persistence

Scan results persist as `~health_*` Picard metadata fields on the file itself: tier, itemized issues, informational notes, a content hash (to detect "file changed since last scan" — computed from the file's raw bytes, so a tag-only edit after a scan is detectable as a byte-level change even though the audio itself is unchanged), and per-metric raw values the Details window re-scores live. Persisting to metadata (rather than a separate plugin-private store) means results survive a Picard restart and travel with the file's own tags; `_restore_health_metadata_on_match`/`_maybe_auto_scan` hooks keep this metadata coherent as files move between clusters and matched tracks. Picard loads each file path into exactly one `File` object, so two independent, potentially-divergent copies of the same underlying file's health metadata isn't a reachable scenario this leaf needs to guard against.

**The "changed since scan" flag after a fixability-driven re-save.** When a user re-saves a file specifically to clear a File Health `OK`-capped issue (re-adding artwork, fixing a tag), the content hash changes and this flag will read true afterward — indistinguishable from an unrelated, unexpected change, even though it's exactly the outcome this design encourages. Resolved as a documentation fix rather than new runtime behavior: distinguishing the two cases live would need the UI to correlate a file's specific prior issue category with its specific new hash, real added complexity for a case that's a minor workflow surprise, not a trust problem — the flag is still technically correct, just not maximally reassuring. The Help Dialog facet's Metadata/troubleshooting content explains this instead (see `UI-HELP-002`).

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Result display | One compact tier column + hover tooltip per score | A wide always-visible free-text column | A free-text column doesn't scale as more checks get added, isn't sortable/filterable, and duplicates information better shown on demand. |
| Column-visibility rebuild trigger | Header-update signal re-emitted at staggered delays (0/250/1000/3000ms) | A single deferred emit at plugin-enable time | A single deferred tick assumes Picard's own tree views are already constructed and connected by the very next event-loop tick; a slow/cold startup can push that construction later, making a single-shot emit a silent no-op. Re-emitting is idempotent once the columns are already correctly wired, so retrying costs nothing. |
| Status icon alignment | Left-aligned, small fixed indent | Centered in the cell | Centering visibly drifted from the column's own left-aligned header label once a user widened the column — a common thing to do on this column given its tooltip content. |
| Details-window re-scoring | Live recomputation from stored raw measurements against current slider settings | Redisplay the tier baked in at last scan time | Adjusting a slider should update the Details window immediately without forcing a re-scan — the raw measurements are already stored; only the threshold comparison needs to be redone. |
| Help dialog content | 100% static HTML, zero dynamic interpolation | Interpolate the currently-selected file's own results into contextual help text | [inferred] Keeping this surface static makes it structurally immune to any HTML/injection concern a crafted file's tag content could otherwise raise, at the cost of the help text never being able to reference "your specific file's result" directly. |
| Track Health Unplayable icon level | Reuse File Health's own level 0 | A distinct icon level for Track Health's Unplayable | Both represent the identical underlying fact (the file won't decode at all), not two different severities that merely look similar — a distinct level would visually imply a difference that doesn't exist. |

## Open Questions & Future Decisions

### Deferred
1. Whether/when this leaf should promote to a sub-HLD (splitting Columns/Actions/Options/Details/Spectrogram/Help into their own child LLDs) — not yet triggered, since none of the six facets has outgrown what this shared leaf can hold coherently.
2. Six code comments elsewhere in the codebase cite `.omp/HANDOFF.md` (a gitignored, non-shipped file) for rationale that properly belongs in this LLD's or a sibling LLD's Decisions & Alternatives table — the user has confirmed these will be replaced with `@spec`/decision-doc references in a later phase rather than now.
3. Track Health's composite score returns `None` for two different causes (nothing was measurable at all, or the file is File-Health-`Unplayable`) — checked against actual usage: in practice a `None` track_tier reaching this UI is always the `Unplayable` case; the "nothing measurable" path is a theoretical fallback that hasn't been observed. Dismissed for now — nothing in the current UI needs to disambiguate the two, and adding a distinction with no live case to justify it would be speculative.

## References

- `docs/intent/file-health-scoring/file-health-scoring-design.md`, `docs/intent/track-health-scoring/track-health-scoring-design.md` — the two scores this leaf's UI surfaces.
- `.omp/HANDOFF.md` — pre-LID narrative for the column-rebuild race fix, the icon-alignment fix, and the Details-window/spectrogram history; not part of the arrow.
