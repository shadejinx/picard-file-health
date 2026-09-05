# Handoff — File Health plugin, two-tier rewrite COMPLETE (as of session 5)

**Five sessions total.** Session 1 designed the two-tier redesign (see
"The redesign" below); session 2 built the two new File Health checks;
session 3 built both scoring engines and the two-tier UI; session 4
split File Health and Track Health into fully independent scans,
columns, and menus; session 5 (this one) did a UI/UX polish pass —
fixed two real bugs (sliders not moving the score, File Health data
lost on file-to-track matching), renamed the tier vocabulary, removed
the now-nonsensical Comparison Priority section, and improved tooltip
accuracy/explanatory depth. See "Session 5 — UI/UX polish" below for
exactly what changed; treat "Session 4" and "Session 3" below as
historical except where they still describe the current architecture
(the independent-scans split itself is unchanged).

**Repo**: `~/Documents/code_repo/picard-file-health`, own git repo.
**HEAD**: `5ca8cd1` "UI/UX polish: fix real scoring/persistence bugs,
rename tiers, remove obsolete comparison feature" — working tree
clean, nothing uncommitted. Installed and confirmed loading cleanly in
a real running Picard instance at this exact commit (see verification
section).
**Tier vocabulary as of this commit**: File Health =
Unplayable/Bad/OK/Good/Excellent (`FILE_TIERS`); Track Health =
Bad/OK/Good/Excellent (`TRACK_TIERS`) — renamed from the old
Broken/Bad/Good/Great/Excellent scale (see "Session 5" below). DR14's
own internal Poor/Ok/Good/Great/Excellent dynamic-range rating band is
a separate, deliberately-unrenamed vocabulary — it describes an
absolute DR measurement, not the file/track tier.
**`analysis.py` has no `analyze_file()`/`AnalysisResult` anymore** —
two independent entry points, `analyze_file_health()` →
`FileHealthResult` and `analyze_track_health()` → `TrackHealthResult`,
each with its own minimal-vs-heavier decode and its own terminal
"can't measure" result type.

## What this is

A Picard v3 plugin that analyzes audio files for real quality/integrity
defects. Two fully independent checks, each with its own scan action, its
own column, and its own tier vocabulary:

- **File Health** — structural (decode success, corruption, tag/artwork
  integrity, bitrate/codec grading). No sliders. Available everywhere
  (left/unmatched-file view and right/matched-track view alike).
- **Track Health** — perceptual (clipping, true peak, spectral cutoff,
  phase, dynamic range, hum). Has the sensitivity sliders. Right side
  (matched-track context) only — see "Session 4" below for exactly why
  and how that's enforced.

A "File Health Details" window (any files, not just confirmed
duplicates) shows both side by side in a full matrix for deciding which
copy of a matched recording is better, or just inspecting one file.

- `__init__.py` — plugin registration, all UI (both column delegates,
  scan/details actions, details-matrix panel, options page).
- `analysis.py` — the real analysis engine, shells out to `ffmpeg`/`ffprobe`.
  No Picard imports; reusable standalone.
- `assets/` — real, found-in-the-wild bad tracks pulled from the user's own
  library tonight (see "Real test corpus" below), plus `BADNESS_MANIFEST.json`
  describing why each one was flagged. Use these for regression testing new
  heuristics — they're real, not synthetic.

## Session 5 — UI/UX polish (bugs, tier renaming, tooltip accuracy)

User's direction this session: "let's polish the UI/UX" — a list of 7
concrete complaints (Options page no longer making sense post-session-4
split, whether the sensitivity sliders are actually wired, File Health
data lost when a file moves from the left/unmatched pane into a
matched track, tier wording, a tooltip contradiction, gate tooltips
not explaining *why*, and using Picard's own match context — e.g. a
"Live" release — to inform tooltip wording).

**Real bug #1 — File Health metadata wiped on match/unmatch.**
Picard's `Track.add_file()`/`remove_file()` both call
`File.copy_metadata()`, which replaces `file.metadata` wholesale with
the matched track's (or the file's own `orig_metadata`'s) tags — see
`picard/file.py`. Only tags in Picard's own core tag registry
(`is_calculated`/`is_preserved` `TagVar` flags, driven by
`picard/tags/tagvars.yaml`) survive that replacement automatically;
our plugin-defined `~health_*` tags aren't registered there at all, so
a scanned file's health data was silently wiped every time it got
matched to a track or unmatched back, forcing a full rescan. Fixed
with a plugin-owned side-cache (`_health_metadata_cache`, module-level
dict keyed by the file's absolute path — the `File` object itself
survives a match/unmatch unchanged, only its `.metadata` gets
replaced) populated right after every scan writes its metadata
(`_cache_health_metadata`), and re-applied via
`api.register_file_post_addition_to_track_processor`/
`register_file_post_removal_from_track_processor` — both hooks Picard
runs *after* `copy_metadata()` already did the damage (confirmed by
reading `Track.add_file()`/`remove_file()` directly: `copy_metadata()`
then `run_file_post_addition_to_track_processors()`, in that order).

**Real bug #2 — sensitivity sliders didn't move the actual score.**
User asked "are these sliders wired?" — direct test confirmed **half**
wired: setting every Track Health slider (Clipping, True Peak,
Spectral Cutoff, Fake Hi-Res) to its most lenient possible value left
`compute_track_score`'s result completely unchanged, because each
check's contribution was scored via `_step_score(value, FIXED_STEPS,
...)` against the hardcoded calibrated STEPS tuple, never against
`thresholds.clip_flat_factor` etc. The sliders only affected which
issue *text* got listed (the separate `has_clipping = flat_factor >
thresholds.clip_flat_factor` gate used for messages), not the tier a
user actually sees. Only DR14 (via `dr14_shift`) was wired correctly.
Fixed with `_anchored_steps(steps, default, current)`: translates a
calibrated STEPS scale by `current - default`, preserving the
scale's own relative spacing (validated calibration data isn't
discarded) while making the user's actual chosen threshold the new
anchor point. Verified directly: an all-lenient `Thresholds` now
measurably changes both the score and the tier on a real fixture.

**Comparison Priority section removed entirely.** With File/Track
Health now fully independent absolute scores (session 4), the
weighted duplicate-tie-break "which copy wins a tie" ranking this
section drove no longer makes sense as a *comparison* concept —
confirmed with the user (`shouldn't the comparison heuristics be
folded into the track and file health categories?`). Removed:
`_WeightSlider`, the whole Comparison Priority options-page section,
`rank_weight_*` config options, `_RANK_AXES`/`_composite_winner`/
`_RANK_LABELS`/`_rank_explanation`/`_RANK_COLUMN_INFO`/
`_RANK_COLUMNS`/`_rank_cells`, and the Details window's tie-break
winner-explanation tooltip. The Details window still bolds a lone
best-tiered file within a matched group as a subtle cue but never
breaks a tie.

Of the three measurements that lived under Comparison Priority:
- **Noise Floor** folded into Track Health as a real calibrated
  absolute check — own sensitivity slider (tag `NSF`, reused from the
  old ranking column), weight 0.5 (same downweight rationale as Mains
  Hum: a raised reading can be a genuine defect but can't be told
  apart with full certainty from legitimate quiet-passage content like
  room tone). Threshold (`NOISE_FLOOR_THRESHOLD_DB = -35.0`) calibrated
  against this project's own 19-fixture real-track sample (measured
  range -66.4dB to -37.5dB) — narrower validation than the True
  Peak/Clipping gates' 500-750-file sweeps (sandbox access to the
  user's real library wasn't available this session), documented as
  such in the constant's own comment; worth widening with a larger
  sample if real-world use turns up false positives or misses.
- **Bandwidth** and **Stereo Coherence** convert to plain
  informational notes in the Details panel's Notes column
  (`_bandwidth_stereo_notes`) — no slider, no column, no scoring.
  Both are cause-agnostic continuous measurements (a naturally
  treble-light acoustic recording or an intentionally wide stereo mix
  reads the same as a real defect) that Spectral Cutoff and
  Out-of-Phase already cover with sharper, absolute logic — turning
  them into gates would just duplicate those checks with more false
  positives.

**Tier vocabulary renamed**: Broken/Bad/Good/Great/Excellent ->
Unplayable/Bad/OK/Good/Excellent, throughout both files
(`FILE_TIERS`/`TRACK_TIERS`, icon-level maps, `analysis.py`'s
`FILE_TIER_*` constants/`track_tier_from_score`, every
docstring/comment/message referencing a tier by name). DR14's own
internal Poor/Ok/Good/Great/Excellent *dynamic-range rating band*
(`_dr14_band_cell`, a completely different, more specialized vocabulary
for describing an absolute DR measurement) is deliberately left as-is
— it isn't "the tier system" the user meant.

**Tooltip said "No issues detected" next to a non-Excellent tier**
(reported with a screenshot: `File Health: Good` / `No issues
detected` / Note: `178kbps MP3 — below the ~192kbps...`). A file
graded below the top tier purely on bitrate has an empty `issues` list
(bitrate grading was never a "file_issues" message, only an
informational `info` note) — the tooltip's hardcoded "No issues
detected" line fired anyway, contradicting the tier and the note right
below it. Fixed in `_SingleHealthColumnDelegate._format_tooltip`: that
line now only appears when there are truly no issues *and* no notes.

**Gate-check tooltips now explain *why*, not just threshold
arithmetic** (reported example: CLP showing only "0.0 — no clipping
detected"). `_gate_cell` now takes `fail_reason`/`pass_reason` plain-
language strings specific to each check (e.g. Clipping: "No clipping
detected — audio stays cleanly under full volume (0.0, comfortably
clear of the 1.0 threshold)"), threaded through all five call sites in
`_check_cells` (Clipping, True Peak, Spectral Cutoff, Fake Hi-Res,
Noise Floor).

**Match-context-aware notes.** Added `_is_live_context(file)` —
checks Picard's own matched `releasetype` tag (`file.metadata.getall
('releasetype')`, populated from the release group's MB secondary
type) for `"live"`, falling back to a `"(Live"`/`"[Live"` substring in
the title for files not yet matched. Wired into the two checks most
likely to false-positive on live material: Spectral Cutoff's fail
tooltip now notes a live PA/broadcast feed can naturally roll off
highs without any lossy transcoding, and Noise Floor's fail tooltip
notes audience/room noise as a likely real cause; the Stereo Coherence
info note also flags a wide/room-mic'd image as expected on live
recordings. Verified directly against the real "Pearl Jam ... (Live)"
asset fixture — both caveats fire correctly.

**Verification performed**: full compile check on both files; a
headless functional smoke test (`QT_QPA_PLATFORM=offscreen`, loading
the plugin module as a real package the same way prior sessions did)
covering a real scan round-trip, tooltip/cell generation for every
gate, the live-context caveats firing on the Pearl Jam fixture, and a
simulated match-metadata-wipe-then-restore cycle — all passed; a
direct before/after `Thresholds` comparison proving the anchored-steps
fix actually changes the score; the full 22-file real-`assets/`
regression re-run with zero errors and every tier correctly renamed;
installed into a real running Picard instance via `picard-cli
--reinstall`, byte-diff confirmed, live log confirms clean `enable()`
with no traceback at `5ca8cd1` (one unrelated traceback in the log is
Picard core's own MP4 parser choking on a pre-existing malformed
`.m4a` fixture from an earlier session — not this plugin).

**Not yet done / possible follow-up**: the Noise Floor threshold's
19-fixture calibration sample is much smaller than the True
Peak/Clipping gates' — sandbox restrictions this session prevented
access to the user's real library (`master`/`archive` directories
referenced in earlier sessions) the way prior calibration passes did;
worth re-running against a larger real sample if Noise Floor ever
looks over/under-sensitive in practice. No other outstanding items
from this session's request list.


## Session 4 — independent scans, columns, and menus

User's explicit direction, verbatim from the session: separate File
Health and Track Health into genuinely independent scans (not two tiers
computed by one shared decode), separate columns, and a left/right menu
split where the left (unmatched-file) view only ever shows File Health.

**`analysis.py`**: `analyze_file()`/`AnalysisResult` removed entirely,
replaced by two independent entry points:
- `analyze_file_health(filename, ffmpeg_path=None) -> FileHealthResult`
  — a minimal decode (`_run_corruption_decode`: `-err_detect compliant
  -map 0:a -f null -`, no filter graph at all) plus the existing
  independent artwork/tag/size-mismatch checks. Cheap, no sliders.
- `analyze_track_health(filename, ffmpeg_path=None, thresholds=None) ->
  TrackHealthResult` — the existing heavier merged-filter-graph decode
  (astats/loudnorm/highpass/aphasemeter) + DR14/hum. Has sliders.

Neither depends on the other having run. Each has its own terminal
"can't measure" result (`_broken_file_health_result` /
`_broken_track_health_result`) instead of a shared one.

**Real regression caught and fixed while validating this split**: the
first version of `_run_corruption_decode` had no explicit `-map`, so
ffmpeg's default stream selection also tried decoding an embedded
attached-pic/video stream when present. A file with genuinely fine
audio but a separately-corrupt embedded image (already its own defect,
covered by `_detect_artwork_corruption`) got misreported as `Broken`
for the wrong reason — caught via the full asset regression (the
Chevelle fixture flipped from `Bad` to `Broken`), confirmed directly
(`-i ... -f null -` exits 69, `-map 0:a` exits 0 on the same file).
Fixed by adding `-map 0:a`. **Lesson for next session**: always rerun
the full `assets/` regression after touching any decode invocation,
even a "shouldn't matter" flag change — this one did.

**`__init__.py`**:
- `ScanFileHealthAction` ("Scan File Health…") and
  `ScanTrackHealthAction` ("Scan Track Health…") are fully separate
  actions/background tasks now, backed by
  `_scan_file_health_one`/`_scan_track_health_one` and
  `_file_health_scan_finished`/`_track_health_scan_finished`. Metadata
  split: `~health_file_content_hash`/`~health_file_changed_since_scan`/
  `~health_file_info` vs. the `~health_track_*` equivalents — each
  scan's own staleness is now tracked independently.
- Auto-scan-on-file-load now runs File Health only (cheap, appropriate
  for bulk/automatic use). Track Health stays manual/on-demand only.
- Two separate registered columns: `FileHealthProvider`/
  `FileHealthColumnDelegate` and `TrackHealthProvider`/
  `TrackHealthColumnDelegate` (sharing paint/tooltip logic via
  `_SingleHealthColumnDelegate`), replacing the old single dual-icon
  column. **Left/right split, exactly as directed**: File Health column
  registered on both `FILE_VIEW` and `ALBUM_VIEW`; Track Health column
  `ALBUM_VIEW` only.
- **Action registration, exactly as directed**: `ScanFileHealthAction`
  registered as a file + cluster + track action (available everywhere —
  the *only* action the left/unmatched-file view ever shows).
  `ScanTrackHealthAction` and the new Details action registered as
  **track actions only** (right side/matched-recording context only).
  `ShowAllFileHealthDetailsAction` stays a Tools-menu action (global,
  unaffected by the left/right per-item-context split).
- "Compare File Health" renamed and generalized into "File Health
  Details" (`DetailsPanel`, `ShowFileHealthDetailsAction`/
  `ShowAllFileHealthDetailsAction`, title "File Health Details…"/"All
  File Health Details…"). No longer requires a confirmed duplicate:
  `_group_files_for_details` groups matched files by Track as before,
  but now also emits a singleton group per completely unmatched file —
  any file's health (scanned or not, independently per scan type) can
  be inspected. Rows always render now (dropped the old special-cased
  "any file in the group unscanned -> collapse the whole group into a
  bare two-column list" branch) — File Tier/Track Tier cells just show
  "Not yet scanned" or "—" (Broken file, Track Health structurally N/A)
  directly per file, independently. The panel's own scan buttons split
  into "Scan File Health"/"Scan Track Health" (each scans every listed
  file for that one type), replacing "Scan Unscanned"/"Rescan All".
- Options page: "File Health Sensitivity" section renamed "Track Health
  Sensitivity" (the sliders were always Track Health checks — File
  Health has none by design, this was a leftover naming mismatch from
  session 3). Auto-scan checkbox label/description updated to say
  explicitly it's File Health only.

**Verification performed**: headless module import; independent-scan
round trip confirmed directly via metadata inspection (scanning File
Health alone leaves every Track Health field/column blank, and vice
versa); both split columns and the generalized Details panel (mixed
scanned/partially-scanned/fully-unscanned-singleton rows in one panel)
rendered offscreen via Qt (`QT_QPA_PLATFORM=offscreen`, `import
picard.resources` for icons) and visually inspected; full 22-file
`assets/` regression (0 errors after the `-map 0:a` fix); a 150-file
real-library regression (0 errors, exactly one genuine pre-existing
`Broken` file confirmed by hand — an unrelated container misdetection
that fails identically with or without the new decode flag, not a
regression); installed into a real running Picard instance via
`picard-cli --reinstall`, byte-diff confirmed, live log confirms clean
`enable()` with no traceback at HEAD.

**Not yet done / possible follow-up**: the Details panel's own
Comparison Priority tie-break logic and per-check matrix cells are
unchanged from session 3 (still fine — they already read whichever
scan's metrics are present and degrade to "na"/"not yet measured"
gracefully). Nobody has manually exercised the actual right-click
context menus in a live Picard window yet (only registration code +
offscreen rendering were verified) — worth a real click-through next
session if anything about the menu split looks off in practice.

## Session 3 — rewrite complete

Every step of session 2's build plan is done, in order — **note**: this
section's references to a single `analyze_file()`/`AnalysisResult` and
one dual-icon column are now historical; see "Session 4" above for the
current architecture.

1. **Two new File Health checks** — done in session 2 (Xing-header size
   mismatch, non-audio corruption).
2. **Track Health composite scorer** (`analysis.py`: `CLIP_STEPS`/
   `TRUE_PEAK_STEPS`/`SPECTRAL_STEPS`/`PHASE_STEPS`, `_step_score`,
   `_dr14_score`, `TRACK_HEALTH_WEIGHTS`, `TrackHealthInputs`,
   `compute_track_score`, `track_tier_from_score`). Weighted average in
   0..1 across Clipping/Spectral Cutoff/Out-of-Phase/DR14 (weight 1.0),
   True Peak/Fake Hi-Res (weight 0.3, confirmed with the user — folds in
   as a normal low-weight input, not a separate bucket), Mains Hum
   (weight 0.5). Tier boundaries calibrated against a real 80-file
   sample from the `master` corpus: `TRACK_HEALTH_BAD_THRESHOLD = 0.35`,
   `_GOOD = 0.25`, `_GREAT = 0.15` — see the constant's own comment for
   the full percentile breakdown and why (no sharp natural gap in the
   real distribution, so placed by percentile + inspecting what
   actually drove the top-scoring real files: compounding True Peak +
   mains hum + heavy DR compression, not a single borderline reading).
3. **File Health composite scorer** (`analysis.py`: `_compute_file_tier`).
   No sliders, per the redesign: `Bad` if any structural defect present
   (bits_left corruption, non-audio corruption, Xing-header size
   mismatch) regardless of bitrate; otherwise graded by codec/bitrate
   transparency (lossless→`Excellent`, transparent-bitrate lossy→
   `Great`, below-transparent lossy→`Good`).
4. **Decode failure reclassified**: `_broken_result` returns a real
   `file_tier="Broken"`/`track_tier=None` `AnalysisResult` instead of
   raising. `AnalysisError` class removed entirely (dead code — nothing
   raises it anymore). `FfmpegNotFoundError`/`FfmpegVersionTooOldError`
   still raise, unchanged (environment problems, not per-file).
5. **`analyze_file` rewritten**: splits `file_issues`/`track_issues`
   per the check-categorization table above; DR14/hum/noise-floor now
   *always* measured (the old OR-gate's "skip if already Bad" shortcut
   no longer makes sense — Track Health's composite needs every
   applicable check's real value regardless of File Health's verdict).
6. **Metadata schema split**: `~health_tier`/`~health_flags` →
   `~health_file_tier`/`~health_track_tier`/`~health_file_flags`/
   `~health_track_flags`. No migration shim — these are Picard `~`
   hidden/computed tags, never written to actual file tags on disk, so
   there's nothing on a user's library to migrate; a file just needs a
   rescan to get the new schema, same as any other metric change.
7. **Tree column UI**: `HealthColumnDelegate` now paints two bookmark
   icons side by side (File Health left, Track Health right) in the
   same column, combined tooltip breaking out both tiers' issues
   separately. `FILE_TIER_ICON_LEVEL`/`TRACK_TIER_ICON_LEVEL` map onto
   `match_icons`' 6 levels. Visually verified via offscreen Qt
   rendering (`QT_QPA_PLATFORM=offscreen`, `import picard.resources`
   to register icons outside the real app, `tree.grab()` → PNG) across
   every tier combination — screenshots confirmed correct red→green
   icon pairs, including the Broken case (single icon, no second one).
8. **Compare-matrix UI**: `Tier` column → `File Tier` + `Track Tier`
   columns; `_tier_rank` returns `(file_rank, track_rank)` — File
   Health ranks first when picking a group's winner (a structural
   defect is a harder fact than a perceptual one). Visually verified
   offscreen against two real scanned files (Ray Price: Bad/Great;
   Korn: Good/Good, correctly bolded as the group winner).
9. **Options page copy**: sensitivity-sliders intro rewritten (the old
   "any one check can flag Bad on its own" claim is gone — replaced
   with the actual weighted-scoring framing). `Missing Treble` slider
   label and every message string renamed `Spectral Cutoff` throughout
   both files.

**Verification performed** (see commit `f4b6ff3`/`1d77889` messages for
full detail): headless module import; full `_scan_one`/`_scan_finished`
round trip against a real fixture with metadata assertions; `HealthProvider`
sort-key and tooltip output checked directly; both UI surfaces rendered
offscreen and visually inspected; full regression across all 22 real
`assets/` fixtures (0 errors, tiers span all four Track Health levels and
both Bad/non-Bad File Health outcomes); **installed into a real running
Picard instance via `picard-cli --reinstall`, byte-diff confirmed against
the installed copy, and the live app's own log confirmed `File Health
enabled` with no traceback** at both `f4b6ff3` and the final `1d77889`.

**Known follow-up, not blocking**: the 80-file Track Health calibration
sample is modest — worth re-checking against a larger/more diverse sample
(other corpora, not just `master`) if the tier distribution ever looks off
in real use. The Options-page step-tables (`_CLIP_STEPS` etc. in
`__init__.py`, with UI hint text) still duplicate `analysis.py`'s plain
scoring copies (`CLIP_STEPS` etc.) — same calibrated values, kept as two
copies because they serve different structures (hint-bearing tuples for
the slider UI vs. plain floats for scoring) rather than merged under time
pressure; a future pass could unify them if the duplication ever drifts.

## Dev loop

```bash
cd ~/Documents/code_repo/picard
source .venv/bin/activate
picard-cli --yes plugins install ~/Documents/code_repo/picard-file-health --reinstall
# ALWAYS byte-diff after install — picard-cli installs from git HEAD, not the
# working tree, and gives no warning if you forgot to commit:
diff -q "/Users/jinx/Library/Application Support/MusicBrainz/Picard/plugins3/file_health_e9df52ff-636f-45cb-8020-c8dbb71991fb/analysis.py" analysis.py
pkill -9 -f "\.venv/bin/picard"; sleep 1
# relaunch via the `hub` tool (op:start, name:"picard", persist so it survives)
```

For headless component testing without a full Picard relaunch: load the
plugin module as a real package via `importlib.util.spec_from_file_location`
with `submodule_search_locations` pointing at the repo dir (makes `from . import
analysis` resolve), then instantiate classes directly with a fake `File`
object exposing just `.metadata` (a real `picard.metadata.Metadata()` works
standalone) and no-op `clear_pending()`/`update()`. Used extensively tonight
to verify Qt rendering (`.grab()` → PNG → `read` tool) without relaunching.

**Long-running background scans**: bare `nohup ... &`/`disown` from the bash
tool did **not** survive between tool calls in this environment — processes
got killed. Use the `hub` tool's `op:"start"` with `persist:true` instead;
it's the only thing that reliably ran unattended for 20+ minutes tonight.

## Tonight's session, in order

### 1. Wired the comparison-matrix redesign into the real panel (commits
`e68ff90` → `2b08d78`)
Replaced `CompareResultsPanel`'s old 3-column (File/Health/Issues) tree with a
real matrix: one emoji-stoplight column per gate check, one per Comparison
Priority ranking axis, a merged Format column (codec+bitrate+rate+channels),
a Notes column for non-gate info. Amber band reuses the sliders' own
calibrated step tables ("would this fail one notch stricter") rather than a
new invented margin — moves live with the slider, no rescan needed.

### 2. Found and fixed two real bugs during verification (`a99fe15`)
- **Dynamic Range column contradicted Tier**: colored relative-to-group like
  the three purely-relative axes (Bandwidth/Noise Floor/Stereo Coherence),
  but DR14 has its own *absolute* Poor/Ok/Good/Great/Excellent band that
  actually decides Tier — a "Poor" file could show a green checkmark for
  being merely less-bad than its sibling. Fixed with `_dr14_band_cell`
  (absolute band, not relative). **Audited the other 8 columns**: grepped
  every use of bandwidth/noise-floor/coherence in `analysis.py` and confirmed
  they never feed tier — relative coloring is genuinely correct for those
  three, DR14 was the only miscategorized one.
- **Real crash**: `_rank_cells`' ternary `max(x) if higher_is_better else
  min(x) if x else None` evaluates unconditionally when `higher_is_better`
  is `True`, raising `ValueError: max() iterable argument is empty` whenever
  no file in a compare group has that axis measured (e.g. two mono files,
  no `stereo_coherence` at all). Found by testing, not inspection — always
  construct the actual crash scenario, don't just read the code and assume.

### 3. Corruption-detection heuristic — full redesign (`71b2349`)
The single biggest investigation tonight. **Read this before touching
corruption detection again.**

**The old heuristic was provably broken.** `Max_difference/Mean_difference`
ratio (astats), threshold 20.0x. Validated against **500 random real files**
from the user's library: **79% false-positive rate**. Root cause: it measures
*dynamic range*, not corruption — a quiet passage followed by a real
transient (Tom Waits spoken intro, Miles Davis soundtrack cue) produces the
identical signature to real corruption. The technique's own "confirmed
corruption" synthetic calibration case (74.8x) sat at the **97th percentile**
of ordinary real content's own natural ratio distribution — no threshold
separates them.

**Rejected replacement candidates, each with real evidence, in order tried:**
1. `adeclick` (ffmpeg's vinyl-click AR-model filter) — threshold sweep found
   no window separating real corruption from real content; wrong artifact
   shape (targets damped analog-click impulse response, not digital
   bit-reservoir desync).
2. Raw `overread` ffmpeg decoder message, presence or count — **ffmpeg's own
   source comment** (`mpegaudiodec_template.c` line ~857) explicitly says
   this also fires for "some encoders [that] generate an incorrect size for
   this part" — confirmed empirically on real files with thousands of
   occurrences (Blind Melon, James Taylor — the latter independently
   **confirmed clean by ear**) and no audible/visible defect. Not usable
   alone, at any count.
3. Strict decode abort (`-err_detect explode -xerror`) — catches only
   catastrophic full frame-desync; **misses the confirmed-audible engineered
   corruption case entirely** (exit 0, decodes clean). Under-sensitive.
   `foobar2000`'s own File Integrity Verifier documents this exact same
   limitation ("accuracy is limited to detecting errors that abort the
   decoding process... in MP3, small errors are not caught") — this isn't a
   gap in our own tooling, it's a known industry-wide boundary.
4. `mpck` (Checkmate MP3 Checker) — real, legitimate tool, but purely
   structural/frame-sync level; **redundant** with what ffmpeg's own default
   decode already logs for free (`Header missing` / `Invalid data found`)
   for the one case it catches. Would add a whole new external dependency
   for zero net new detection power. Dropped.

**What shipped**: `bits_left` diagnostic from ffmpeg's own MP3 decoder
(`mpegaudiodec`), surfaced via `-err_detect compliant` added to the existing
merged decode (**zero extra cost**, same one decode). This is the decoder's
own bit-reservoir sanity check.

- Reached by **engineering real corruption first**: corrupting a raw
  PCM sample didn't reproduce the target artifact. Corrupting a *contiguous
  2KB block* (simulating a bad sector) caused a full frame-desync ("skip
  forward" symptom) — real, but not what the user described. Scattered
  single-byte flips in the MP3 bitstream had **zero effect** (absorbed by
  Huffman coding's redundancy). The actual reproduction: corrupting the
  `main_data_begin` bit-reservoir pointer (first 9 bits of a frame's side
  info, right after the 4-byte header) across **8 consecutive frames**
  reliably produced the user-confirmed "sci-fi bloop" — verified by ear
  *and* visually (a distinct dark dropout stripe in a spectrogram, absent in
  a clean reference at the same position).
- **Validated against 600 real files spanning 5 corpora** (`master`,
  `old/master`, `old/archive`, `old/dirty`, `old/working`): **99.2% zero
  occurrences**, including the file independently confirmed clean by ear
  despite 2142 unrelated `overread` warnings. Every engineered corruption
  fixture (both severities) produced at least one `bits_left`.
- **Known, documented limitation**: the mildest engineered fixture (a single
  faint click) didn't trigger `bits_left` either — catches moderate-to-severe
  bit-reservoir corruption, not every possible glitch. Informational only,
  never gates (though see the rewrite below — "gates" are going away
  entirely).

**A follow-up idea, scoped but not built**: characterizing corruption as
"systemic" (spread evenly — an encoder quirk) vs. "burst" (localized — real
damage) as a *comparison* signal, not a gate. **Blocked on a real, confirmed
technical wall**: MP3's bit-reservoir means any segment boundary — however
precisely frame-aligned — starts with zero prior context, so re-decoding
segments *always* produces spurious `bits_left` hits at every boundary
regardless of real corruption. Log-message ordering doesn't reliably reflect
real-time decode position either (verified: messages can appear batched near
EOF regardless of where the actual issue is in the file), and `-progress`
timestamps have no useful resolution for files that decode in a fraction of
a real second. Getting real positional data would need a from-scratch MP3
frame/bit-reservoir parser in Python — a genuinely separate, bigger project.

### 4. Ran the whole suite against a real 750-file sample of the user's
library (equal 150/corpus across all 5)
Full results in `/tmp/badness_results.json` (ephemeral — regenerate via
`/tmp/badness_scan.py` if needed, or rebuild from scratch; the exact script
is reproduced below since `/tmp` won't survive to next session).

**Tier distribution**: Bad 407 (54.5%), Poor 53 (7.1%), Ok 82 (11.0%), Good
102 (13.7%), Great 76 (10.2%), Excellent 27 (3.6%). Only **27.4%** landed
Good-or-above.

**Bad-tier breakdown by corpus** — `master` (the curated/active library) was
notably healthiest at 39.3% Bad; all four `old/*` corpora clustered at
54-62% Bad, including `old/archive` (expected to be "excellent quality") at
61.3%, indistinguishable from `old/master`.

**Which gate actually drives Bad** (a file can fire more than one):
**True Peak 58.2%**, Missing Treble 23.3%, Clipping 19.4%. Out-of-Phase and
Fake Hi-Res: **zero** occurrences in the whole 750-file sample.

**This finding is what triggered the whole redesign below.** Pulled the
actual measured values behind the two dominant gates:
- **True Peak**: median of flagged values was **+1.1dBTP**, just half a dB
  past the 0.6dBTP threshold — sitting inside the "meter's own accuracy
  limit" zone the threshold's *own* calibration comment already worried
  about. Genuinely over-sensitive for real-world mastering practice.
- **Clipping**: median flagged `flat_factor` was **5.93**, deep inside the
  calibration data's own "confirmed real clipping" range (16-32 for hard
  clipping fixtures, vs. 0.0094 for genuinely loud-but-clean noise). **Not**
  over-sensitive — this is real, this library genuinely has a lot of clipped
  masters (unfortunately common in loudness-war-era mastering). Don't loosen
  this one based on tonight's evidence.

Also tested (and rejected) two more File Health candidates the user
specifically asked about:
- **Truncation** (declared vs. decoded duration mismatch) — **confirmed
  unsafe**, exactly matching the user's stated historical concern about old
  VBR-header inaccuracies. A real, intact 1998 Placebo track showed a 12%
  "missing" duration purely from an old encoder's own miscalculated Xing
  header frame count (`bit_rate=161243`, `probe_score=52` — classic
  signature). The 5 files in the test sample with near-100% mismatch turned
  out to be genuine decode failures already caught by the existing
  `AnalysisError` path, not something this check would add coverage for.
  **Do not add naive header-vs-decoded duration truncation detection.** If
  revisited, it needs to compare against the file's *own* embedded VBR frame
  count specifically, not container-estimated duration.
- **`mpck`'s other capabilities** (beyond frame-sync, already covered) —
  just `--namecheck`/`--maxname` (filename hygiene: strange characters,
  length). A completely different concern from audio/file integrity, out of
  scope for this plugin.

### 5. Top-20 real bad-track corpus saved to `assets/`
Ranked by `defect_count = len(issues) + count of real-defect info notes`
(excluding "mono duplicated," which the code's own comments already treat as
wasteful-not-defective). Copied with `Artist - filename.ext` naming plus
`BADNESS_MANIFEST.json` (source path, tier, full issue/info breakdown per
file). Use these for validating any rewrite — they're real, confirmed
multi-indicator files, not synthetic.

## The redesign: why, and what's agreed

**The core problem, stated plainly by the user**: a single borderline
heuristic (True Peak, median only 0.5dB over threshold — arguably
inaudible) can force a file to the same "Bad" verdict as genuine, obvious
corruption or severe clipping. The OR-gate architecture (any one gate check
fires → tier = "Bad", full stop) conflates two completely different kinds of
claims: "this file is structurally broken" and "this audio might sound
slightly off to a trained ear." Tonight's own real-library data proved this
isn't theoretical — 58% of all "Bad" verdicts in a 750-file real sample came
from a check whose own measured values sit in its documented uncertainty
band.

**Decision: replace OR-gate tiering entirely** with two independent 5-tier
weighted scores. This is a deliberate near-complete rewrite of
`analyze_file`'s tier logic — the user explicitly said not to be afraid to
throw out the existing gate-based approach if that's more efficient than
adapting it. It almost certainly is; the gate logic and the scoring logic
don't share much structure.

### Two independent scores, not one

1. **File Health** — structural, objective, near-binary. **No Options-page
   sliders at all** (explicitly decided — nothing here should need user
   sensitivity tuning).
2. **Track Health** — psychoacoustic/perceptual. **Keeps the existing
   sensitivity sliders**, but their role changes: they're no longer gate
   thresholds that can singlehandedly force a verdict, they're **weight
   inputs into a composite score**.

**Both use the same 5-tier vocabulary, explicitly chosen over the old
6-tier Bad/Poor/Ok/Good/Great/Excellent for having "no filler":**

```
Broken → Bad → Good → Great → Excellent
```

Relationship between the two scores: **no artificial cross-constraint
needed.** File Health naturally constrains Track Health, because a decode
failure (File Health = Broken) makes Track Health *structurally impossible*
to measure — nothing to build, it already can't compute anything today. This
is itself an architecture change: a decode failure currently *raises
`AnalysisError`* (a transient scan error, never persisted). Under the new
model **it should become a real, persisted `file_tier="Broken"` result with
`track_tier=None`** — a file that won't play is a legitimate, storable File
Health fact, not just an error toast. (The *separate* `FfmpegNotFoundError`
— ffmpeg itself missing/too old, an environment problem unrelated to any one
file — stays an exception, correctly distinct from a per-file verdict.)

### Check categorization (confirmed, this exact list)

**File Health** (structural — decode failure, integrity, provenance-at-the-
container-level):
- Decode failure → `Broken` (terminal)
- Audio corruption (`bits_left`)
- **Non-audio corruption** (new — malformed embedded artwork / tag
  structure; user approved building this)
- **Concatenated-file detection** (new — user approved; ffmpeg's own
  demuxer already logs `"invalid concatenated file detected"`, confirmed
  real in the wild tonight on an Armin Van Buuren track found during the
  severe-marker sweep)
- Bitrate/codec (continuous, gradable — reuse the already-validated
  `TRANSPARENT_BITRATE_KBPS` HydrogenAudio/Xiph thresholds)

**Track Health** (psychoacoustic — perceptible, weighted by sliders):
- Clipping
- **Spectral Cutoff** (renamed from "Missing Treble" — user's words: "Missing
  Treble sounds like someone just needs to adjust the EQ," this is actually
  "a flaw in the encode or source material." **Rename everywhere**: slider
  label, `TRB` tag references, message text, `Thresholds`/constant doc
  comments.)
- **Fake Hi-Res** — user's own distinguishing test: if it's "the header
  doesn't match the data" → File Health; if it's "the data doesn't sound
  like the header describes it" → Track Health. Verified against the actual
  implementation: it measures *decoded spectral content*, not a metadata
  field, so it's the second case → **Track Health**, grouped with Spectral
  Cutoff (they already share `spectral_silence_db`).
- Out-of-Phase
- Dynamic Range / Compression Tolerance (DR14)
- Mains Hum

**True Peak's bucket was confirmed with the user in session 2**: it folds
into Track Health as a normal weighted input at a lower default weight,
not a separate "imperceptible" bucket. This thread is now fully closed —
no need to re-ask.

### Scoring mechanics — designed, not yet implemented

**Track Health**: reuse the step-table "distance past threshold" logic
already built and validated for the compare-matrix's `_gate_cell` (amber =
"would fail one slider notch stricter"). Per check: 0 = comfortably clear,
partial = close to threshold, 1.0+ = failing, scaled by how many notches
past. Weight and sum across the *perceptible* checks (Clipping, Spectral
Cutoff, Out-of-Phase, Mains Hum, DR14 — plus, per the note above, True Peak
and Fake Hi-Res at a lower/separate weight rather than excluded outright).
Map the composite onto the 5-tier scale. **Tier boundaries need real-data
calibration** — do this the same way everything else got calibrated
tonight: compute the composite score's real distribution across a broad
library sample, don't guess cutoffs.

**File Health**: no sliders, so the mapping can be more fixed, but should
still be evidence-grounded:
- `Broken`: decode failure.
- `Bad`: any structural defect present (audio corruption, non-audio
  corruption, or concatenated-file detected) — regardless of bitrate.
- `Good`/`Great`/`Excellent`: no structural defects, graded by
  bitrate/codec — lossless → `Excellent`; lossy at/above the codec's own
  transparency threshold → `Great`; lossy below it → `Good`.
- **This mapping is a first draft, not confirmed** — was mid-design when the
  session ended. `bits_left` *count* was explicitly flagged as an unreliable
  severity proxy (tonight's own data: Blind Melon's 12 counts showed no
  confirmed defect, while the confirmed corruption fixture showed only 1) —
  don't scale severity by count without new evidence; presence/absence is
  the validated signal, not magnitude.

### Two new File Health checks — DONE, built and validated in session 2

Both are real, committed, wired into the current `analyze_file()`'s
`issues` list (HEAD `463effe`). Full rationale and validation evidence is
in the code comments (`analysis.py`, `SIZE_MISMATCH_RATIO_THRESHOLD` and
`_detect_artwork_corruption`/`_detect_tag_structure_error` docstrings) —
summary here:

**Concatenated-file detection became "Xing/VBR header size mismatch"
instead** — the originally-planned approach (keying off ffmpeg's own
`"invalid concatenated file detected"` demuxer message) didn't survive
contact with real data. Confirmed the message no longer reproduces on the
original Armin Van Buuren reference file via any invocation this module
uses. More importantly, reading FFmpeg's own `libavformat/mp3dec.c` source
showed that message is generated from comparing the Xing header's own
*encoder-written* `header_filesize` field against the real on-disk size —
the exact same signal source already proven unreliable in the (rejected)
truncation-detection investigation (old encoders can write a wrong value
with nothing structurally wrong). The user's call: don't try to force a
binary concatenated/truncated verdict out of that ambiguity — report
**direction and magnitude** of the header/actual-size disagreement as a
structural fact in its own right, regardless of root cause. Implemented as
`_detect_size_mismatch()`, reading the Xing header directly via mutagen
(not ffmpeg's log line) for determinism. Calibrated against a real
500-file random sample: 366/370 files with a usable Xing header landed at
<=0.43% (floating-point noise), then a clean gap to 4 real outliers at
21%-182% — three of which independently show `bits_left` corruption hits.
Zero false positives.

**Non-audio corruption** (embedded artwork + tag structure) shipped as
designed, with two sub-checks:
- `_detect_artwork_corruption`: ffprobe for an attached-pic stream, then
  attempt to decode its first frame via ffmpeg; non-zero exit = corrupt.
  Validated: mild corruption (scattered bit flips, or truncating to 20%
  while keeping SOF/DQT/SOS header segments) decodes silently with only a
  warning — same class of limitation as `bits_left`. Truncating past the
  header segments entirely reliably produces a real decode error. Found a
  genuine, previously-undetected real defect in this repo's own Chevelle
  fixture: its embedded art is tagged PNG but is actually JPEG bytes.
- `_detect_tag_structure_error`: `mutagen.File()` wrapped in try/except —
  a different parser/code path than ffmpeg's own more-lenient container
  reading. Validated against a 100-file real M4A sample (0 false
  positives) plus this repo's fixtures: found a genuine malformed `----`
  freeform atom (missing its required `mean`/`name` sub-atoms) in a real
  3 Doors Down M4A already in the corpus — confirmed by hand-walking the
  atom structure, not assumed; ffmpeg's own tag reader silently tolerates
  it, which is exactly why this is a real gap mutagen's stricter parser
  fills.

## Concrete build plan for session 3

Work through in this order — each is a real checkpoint, validate before
moving to the next (same discipline both prior sessions used throughout):

1. ~~Finish the two new File Health checks.~~ **DONE** (session 2, HEAD
   `463effe`) — see "Two new File Health checks" above.
2. **Build the Track Health composite scorer.** Move `_gate_cell`'s
   step-table distance logic from `__init__.py` (currently Qt-adjacent UI
   code) into `analysis.py` — it's framework-agnostic (plain floats/tuples,
   no Qt types) and is about to drive the core tier, not just a UI cell
   color; `__init__.py`'s compare-matrix should import and reuse the same
   function rather than keep a second copy. Reuse the existing step tables
   (`_CLIP_STEPS`, `_TRUE_PEAK_STEPS`, `_SPECTRAL_STEPS`, `_PHASE_STEPS`,
   `_DR14_SHIFT_STEPS`) as the distance scale for each check's contribution.
   Sum weighted per-check scores (Clipping, Spectral Cutoff, Out-of-Phase,
   Mains Hum, DR14 at full weight; True Peak and Fake Hi-Res at a lower
   weight — confirmed with the user in session 2, see above) into one
   composite. **Tier boundaries need real-data calibration**: compute the
   composite's actual distribution across a broad library sample (reuse
   this session's `/tmp/all_master_mp3s.txt` enumeration and sampling
   approach if still on disk, or regenerate — see the enumeration commands
   at the bottom of this doc) and place the 5 tier boundaries from that
   distribution, not a guess.
3. **Build the File Health composite scorer.** No sliders, so simpler, but
   still evidence-grounded for the exact Bad/Good boundary: `bits_left`
   presence, the two new session-2 checks (size mismatch, non-audio
   corruption), and codec/bitrate transparency all feed in — confirm the
   combined signal doesn't over/under-trigger against a real sample before
   finalizing, the same way each individual check already was.
4. **Reclassify decode failure**: `AnalysisError` → persisted
   `file_tier="Broken"` result, not an exception. Note `content_hash()`
   doesn't need a successful decode (it hashes raw bytes) so it can still
   be computed and stored even for a Broken file. Audit every caller in
   `__init__.py` that currently catches `AnalysisError` (`_scan_one`) — the
   error-message statusbar path likely goes away entirely for this specific
   case. `FfmpegNotFoundError`/`FfmpegVersionTooOldError` stay exceptions —
   unrelated environment problems, not a per-file verdict.
5. **Rewrite `analyze_file`** to assemble and return both tiers plus every
   existing raw value (nothing measured should be lost, just recombined).
   This means splitting the current single `issues`/`info` lists by which
   score they feed — the check-categorization table above is the exact
   split to use. Regression-test against the real files in `assets/` (all
   still real "Bad"-tier fixtures) and this session's own engineered
   fixtures if still on disk (`/tmp/concat_test.mp3`,
   `/tmp/corrupt_art*.mp3`, `/tmp/corrupt_tag.mp3`) — every one should still
   land at a low File or Track tier under the new scoring, even though the
   exact tier label/number will change.
6. **Metadata schema**: `~health_tier` → two keys (`~health_file_tier`,
   `~health_track_tier` or similar). Decide the migration story for already-
   scanned files (old single-tier metadata goes stale until rescanned — is
   that acceptable, or does it need a version marker?).
7. **UI — main tree column**: `HealthProvider`/`HealthColumnDelegate` need
   two indicators instead of one. Design not started.
8. **UI — compare-matrix**: two Tier columns instead of one (`Tier` →
   `File Tier` + `Track Tier`). Per-check cells mostly stay as-is — they're
   already individual, this only affects the two summary columns.
9. **Options page**: reframe intro copy — "Any one check below can flag a
   file 'Bad' on its own" (current text) is no longer true under weighted
   scoring, needs rewriting. Decide whether to keep per-check sliders
   labeled the same way or reframe as "weight" language matching the new
   mental model. `Missing Treble` → `Spectral Cutoff` rename touches this
   page's slider label too.

## Real test corpus reference (paths, for regenerating if `/tmp` is gone)

Five corpora on the mounted volume `/Volumes/audio/music/`:
`master` (curated/active, ~39.8k files), `old/master` (~4.3k), `old/archive`
(~2k, expected-but-not-confirmed "excellent quality"), `old/dirty` (a beets-
style import staging area: `00_inbox`/`01_unidentified`/`02a_complete`/
`02b_incomplete`, ~11.7k), `old/working` (a freshly-synced large personal
library, ~4.2k). Enumerate with:

```bash
find /Volumes/audio/music/<corpus> -type f \
  \( -iname "*.mp3" -o -iname "*.flac" -o -iname "*.m4a" -o -iname "*.ogg" -o -iname "*.wav" \) \
  > /tmp/corpus_<name>.txt
```

`old/master` and `old/archive` are small enough to enumerate directly (~1
min each over the network mount); `master`/`old/dirty`/`old/working` are
large enough that a single combined `find` across all of them can exceed a
300s foreground timeout — enumerate the big ones separately, or expect to
background it via `hub start`.

Known real fixtures worth remembering: Creedence Clearwater Revival's
`Chronicle`/`Chronicle, Vol. 1`/`Cosmo's Factory` give a genuine 9-track
triple-overlap corpus (same songs, MP3 in `master`+`old/master`, FLAC in
`old/archive`) spanning 252-320kbps MP3 and true lossless — good for any
future bitrate/transparency work. The Blind Melon/James Taylor/Miles Davis
files referenced throughout the corruption investigation remain on disk at
their original library paths if `bits_left` scoring ever needs revisiting.
