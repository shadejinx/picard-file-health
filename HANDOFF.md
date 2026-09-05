# Handoff — File Health plugin, near-complete rewrite in progress

Session hit a context limit mid-rewrite. This replaces the previous HANDOFF.md
entirely — tonight's session concluded that a large piece of the existing
architecture (OR-gate tiering) needs to be replaced, not patched, so treat the
*design decisions* below as the source of truth going forward, not the old
gate-based code still sitting in `analysis.py` at HEAD.

**Repo**: `~/Documents/code_repo/picard-file-health`, own git repo.
**HEAD**: `71b2349` "Replace corruption-signature heuristic: bit-reservoir
bits_left, not ratio" — working tree clean, nothing uncommitted.
**Do not assume anything about `analysis.py`'s tier logic beyond what's
described here is still correct** — this doc describes what ships at HEAD
*and* the redesign that supersedes big parts of it.

## What this is

A Picard v3 plugin that analyzes audio files for real quality/integrity
defects and surfaces them as an icon column in Picard's file/album tree, plus
a duplicate-comparison panel (a full matrix now, not a 3-column list — see
below) for deciding which copy of a matched recording is better.

- `__init__.py` — plugin registration, all UI (health column delegate,
  scan/compare actions, comparison-matrix panel, options page).
- `analysis.py` — the real analysis engine, shells out to `ffmpeg`/`ffprobe`.
  No Picard imports; reusable standalone.
- `assets/` — real, found-in-the-wild bad tracks pulled from the user's own
  library tonight (see "Real test corpus" below), plus `BADNESS_MANIFEST.json`
  describing why each one was flagged. Use these for regression testing new
  heuristics — they're real, not synthetic.

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

**True Peak was discussed as a candidate for a third "imperceptible" bucket**
(technically real, not audible — median value tonight sits in the meter's
own documented accuracy-limit zone) alongside Fake Hi-Res and the bitrate
transparency note. **This got superseded by the "no gates, just weighted
scoring" decision** — instead of a separate imperceptible-non-gating bucket,
True Peak just becomes a Track Health input with a low weight/contribution
in the composite score, same mechanism as everything else. Re-confirm this
resolution with the user at the start of next session if picking this back
up — it was the last architectural thread before the handoff, not fully
closed out in as many words.

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

### Two new File Health checks — designed, partially tested, not built

**Non-audio corruption**: check embedded artwork/tag structure. Design intent
was to reuse ffmpeg itself (already a hard dependency, avoid adding Pillow)
— probe for a video/attached-pic stream via `ffprobe -select_streams v:0`,
and if present, attempt `-map 0:v:0 -frames:v 1 -f null -`; a non-zero
return code means the embedded image is corrupt. Confirmed empirically
tonight that the *real* `analyze_file` pipeline (explicit `-filter_complex` +
explicit `-map` for audio-only outputs) **never touches the video stream at
all** — so this is a genuinely new code path, not something already
accidentally covered, and safe to add without risk of contaminating the
existing audio corruption check (verified: a file with a known-corrupted
embedded PNG scored clean today, tier driven only by DR14). Tag-structure
validation via `mutagen.File(filename)` wrapped in try/except was sketched
but not tested.

**Concatenated-file detection**: reuse ffmpeg's own `"invalid concatenated
file detected"` demuxer message. **Confirmed tonight that plain `ffprobe -v
warning -i file` does NOT surface this warning** (tested against the known
Armin Van Buuren file — silent) — it only appeared during a full `ffmpeg -i
file -f null -` decode in the earlier severe-marker sweep. **Next step,
where the session was cut off**: confirm exactly which invocation surfaces
it reliably (a decode-only pass may be needed, not probe-only) and whether
it's already visible for free in the existing merged decode's stderr before
adding any new subprocess call.

## Concrete build plan for next session

Work through in this order — each is a real checkpoint, validate before
moving to the next (same discipline as tonight throughout):

1. **Finish the two new File Health checks.** Resolve the concatenated-file
   invocation question above. Build and test non-audio corruption detection
   against the Michael Jackson file (known-corrupted PNG) and a clean
   control. Validate both against a real library sample for false positives,
   same rigor as `bits_left` got tonight — don't skip this step, every
   heuristic shipped tonight that skipped real-data validation first had to
   be reworked.
2. **Build the Track Health composite scorer.** Reuse `_gate_cell`'s
   step-table distance logic (currently living in `__init__.py`'s
   comparison-matrix code — consider whether it belongs in `analysis.py`
   now that it's driving the core tier, not just a UI cell color). Compute
   real score distributions across a library sample to place the 5 tier
   boundaries with evidence, not guesses.
3. **Build the File Health composite scorer.** Same real-data-first
   discipline for the Bad/Good boundary specifically — `bits_left` presence
   is validated, but confirm the full combined signal doesn't over- or
   under-trigger before finalizing.
4. **Reclassify decode failure**: `AnalysisError` → persisted
   `file_tier="Broken"` result, not an exception. Audit every caller in
   `__init__.py` that currently catches `AnalysisError` (`_scan_one`) — the
   error-message statusbar path likely goes away entirely for this specific
   case.
5. **Rewrite `analyze_file`** to assemble and return both tiers plus every
   existing raw value (nothing measured should be lost, just recombined).
   Regression-test against `/tmp/run_fixtures.py`'s fixture catalogue (if
   still on disk) and the real files in `assets/` — the top-20 badness list
   should still land at low tiers under the new scoring, even though the
   exact number/label will change.
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
