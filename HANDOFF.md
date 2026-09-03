# Handoff — File Health plugin for MusicBrainz Picard

Session hit a context limit; this captures state for continuation in a fresh session.

## What this is

A Picard v3 plugin (`~/Documents/code_repo/picard-file-health`, own git repo, 30 commits)
that analyzes audio files for real quality defects — clipping, transcoding, phase issues,
loudness — and surfaces them as an icon column in Picard's file/album tree, with a
duplicate-comparison panel for deciding which copy of a matched recording is better.

It grew out of a long design conversation (in the parent Picard repo checkout at
`~/Documents/code_repo/picard`) about reverse-engineering an old Windows tool called
"Similarity" and researching real DSP/psychoacoustic measurement techniques. That
conversation is not reproducible here — treat this repo and its commit history as the
source of truth for *why* things are built the way they are; commit messages are
deliberately detailed (rationale, calibration data, what was rejected and why).

## Repo layout

- `__init__.py` — plugin registration, all UI (health column delegate, scan/compare
  actions, comparison panel, options page).
- `analysis.py` — the real analysis engine, shells out to `ffmpeg`. No Picard imports;
  could theoretically be reused standalone.
- `MANIFEST.toml` — plugin metadata (uuid, name, description).

## How to test (dev loop used throughout this whole build)

```bash
cd ~/Documents/code_repo/picard
source .venv/bin/activate   # dev venv already set up, Picard installed editable

# after editing the plugin:
picard-cli --yes plugins install ~/Documents/code_repo/picard-file-health --reinstall

# launch with test files (via hub tool, or plain background process):
picard /tmp/health-demo-files/song-a.mp3 ...
```

Test/calibration audio files live in `/tmp` (NOT in the repo, will be gone after a
machine restart — regenerate if needed, code to do so is in this session's transcript
history, or recreate similarly):
- `/tmp/health-demo-files/{song-a,song-b,song-c,song-d,song-e,song-f,test,test}.{mp3,flac,ogg}`
  — copies of Picard's own tiny test fixtures (`test/data/test.{mp3,flac,ogg}`), some
  duplicated to create matchable-to-same-track groups. `song-a.mp3` etc. are ~0.13s,
  near-silent (~-74dB peak) — good for testing "does the near-silence guard work",
  bad for testing anything needing real signal.
- `/tmp/genuinely_clipped.wav`, `/tmp/mild_clip_{1.02x,1.1x,1.3x}.wav` — synthesized
  hard/soft-clipped sine waves (Python `wave`/`struct`, values hard-clamped to
  int16 range) — used to calibrate the clipping threshold.
- `/tmp/lowpassed_16k.wav` — sine + `ffmpeg lowpass=f=16000` — validates spectral-cutoff
  detection.
- `/tmp/wideband_noise.wav` — `ffmpeg anoisesrc` — validates True Peak catches
  inter-sample overs that Flat-factor-based clipping detection misses.
- `/tmp/real_stereo.wav`, `/tmp/fake_stereo_mono_dup.wav`, `/tmp/phase_inverted.wav` —
  independent-channel / mono-duplicated / phase-inverted noise — validates phase
  correlation detection.
- `/tmp/health-demo-files/{wideband_v0,transcode_v0,honest_96,honest_forced_lowpass,
  honest_low_lowpass}.mp3` — real `lame`-CLI encodes (not ffmpeg's `libmp3lame`
  wrapper, which doesn't write a parseable LAME tag) at known settings (`-V0`,
  `--lowpass N`, plain CBR) of `wideband_noise.wav`/`lowpassed_16k.wav` — validates
  the LAME-header low-pass cross-check's decisive-vs-heuristic-only boundary.

Always validate new ffmpeg-filter-based logic directly against real ffmpeg output
first (`ffmpeg -i FILE -af FILTER -f null -`, read stderr) before writing parsing code
— this caught multiple wrong assumptions during the build (see commit history).

## Current real capabilities (all empirically calibrated, not guessed)

Six checks in `analysis.analyze_file()`:

**Gate checks** (any one present → tier forced to "Bad"):
1. **Clipping** — `astats` "Flat factor" > `MIN_FLAT_FACTOR_FOR_CLIPPING` (1.0).
   Calibrated against 5 severities: clean=0.0, loud-but-not-clipped noise=0.0094,
   mildest real clip (2% overdrive)=16.06, hard clip=31.88.
2. **True Peak** — `loudnorm`'s `input_tp` ≥ 0dBTP. Catches inter-sample DAC
   reconstruction overshoot that Flat factor structurally cannot see (validated: the
   white-noise test file has Flat factor 0.0094 — below the clipping threshold,
   correctly not "clipping" — but True Peak +3.71dBTP, a real distinct defect).
3. **Spectral cutoff / likely transcode** — `highpass=f=17000,volumedetect`
   `mean_volume` < -60dB. Guarded by `MIN_PEAK_DB_FOR_SPECTRAL_CHECK` (-40dB overall
   peak) — a near-silent file trivially has "no content above 17kHz" for the same
   reason it has no content anywhere; this guard was added after the check
   false-positived on Picard's own tiny test fixture live during testing.

   **LAME header cross-check** (added on top of the above): when the file has a
   LAME encoder tag (`analysis._read_lame_header()`, parses the embedded
   Info/Xing frame per http://gabriel.mp3-tech.org/mp3infotag.html, via
   `mutagen.mp3` — bundled with Picard, no new dependency), and LAME's own
   recorded low-pass filter value is *above* 17kHz, the "likely transcoded"
   wording upgrades to "Confirmed transcode" — LAME's own filter can't
   explain the missing content, so the source was already lossy before this
   encode. Validated against real `lame`-CLI-encoded fixtures (`-V0`,
   `--lowpass N`), not just ffmpeg's `libmp3lame` wrapper — that wrapper
   writes its own `"Lavc…"` version string instead of `"LAME…"`, so it
   never has the extended tag this check needs; only genuinely LAME-encoded
   files get the upgrade, everything else silently falls back to the
   existing heuristic wording.
4. **Out-of-phase channels** — ffmpeg's own `aphasemeter=phasing=1` mode reports
   `out_phase_start`/`out_phase_duration` directly; no manual correlation math.
   Gated on channel count ≥ 2 (derived free from counting "DC offset" occurrences
   in the already-fetched `astats` output — no extra ffprobe call).

**Gradient** (only evaluated if no gate fired): coarse LUFS-integrated-loudness
bucketing (Poor < -8, Ok < -11, Good < -16, else Excellent), referencing real
industry loudness norms (streaming ~-14 LUFS, EBU broadcast ~-23 LUFS, loudness-war
masters ~-8 LUFS or louder). Explicitly documented as NOT a true DR14 dynamic-range
measurement (that needs block-based peak-vs-RMS analysis — not built).

**Informational only** (surfaced but never affects tier): mono content duplicated
into both stereo channels — wasteful, not a defect.

**Also real, not a "check" per se**: `analysis.content_hash()` — blake2b hash of the
file's raw bytes, compared scan-to-scan to detect "this file's bytes changed since
last scan" (caveat: whole-file, so tag edits also trigger it, not just audio changes).

## Not yet built (explicit roadmap, in priority order as last discussed)

1. Real DR14 (block-based peak-vs-RMS dynamic range) — replace the coarse LUFS proxy.
2. Bitrate-vs-codec-transparency scoring (MP3 ~256-320kbps, AAC ~192-256, Vorbis
   ~160-192, Opus ~96-128 — HydrogenAudio/Xiph consensus reference points).
3. Sample-rate scoring.
4. Hum/mains-noise detection (50/60Hz spike in quiet passages via FFT).
5. Combining the now-4 separate `ffmpeg` subprocess calls per file into one
   filtergraph (`asplit` into astats/volumedetect/loudnorm/aphasemeter branches) —
   pure perf optimization, not correctness.
6. Isolating `content_hash()` to just the decoded PCM stream (skip tag blocks) now
   that we decode audio anyway for the real checks — removes the false-positive
   "changed" flag on pure tag edits.

Explicitly dropped from scope (user decision, don't resurrect without re-asking):
- A whole-library health report — Picard is a tagger operating on a working
  session, not a persistent library index; that's Similarity's model, not Picard's.
- Album-row health aggregation, pre-save gate hook — deferred, not rejected, just
  deprioritized in favor of heuristics work.

## UI architecture (built, stable, working — don't need to redo)

- **Health column**: `DelegateColumn` (not the simpler `CustomColumnSpec`/
  `register_and_persist` path — that only supports plain text FIELD/SCRIPT columns,
  no icon rendering). Registered at **module import time** (not inside `enable()`),
  matching how Picard's own core match-quality column does it in
  `picard/ui/itemviews/columns.py`.
- Reuses Picard's own `picard.ui.match_icons.match_icons` bookmark icons (6 levels,
  exactly matches this plugin's `TIERS` tuple) rather than bundling new image assets
  or painting shapes.
- **Scan trigger**: right-click action (`ScanHealthAction`, file/track/cluster) +
  opt-in auto-scan-on-load (`_maybe_auto_scan`, toggled via Options, checks the
  setting live so no restart needed). Both run the real analysis via
  `picard.util.thread.run_task` (background thread), never inline.
- **Compare panel**: `CompareResultsPanel` (non-modal `QDialog`), grouped by
  `Track` linkage (`file.parent_item`) — i.e. reuses Picard's own already-computed
  matching decision, not a separate tag-based identity re-derivation. Shows every
  file in a group side by side (tier + issues), bolds the higher-scoring one as a
  *subtle cue only* — deliberately never declares a hard "Recommended" verdict, per
  explicit user pushback earlier in the build. Per-row buttons: "Show in List"
  (uses `file.ui_item` to select+scroll+focus the real tree row — solves the
  problem of matched files sharing a display title, filename doesn't help
  correlate), "Remove from Picard" (`tagger.remove`), "Move to Trash…"
  (`tagger.trash_files`, confirmed first).
- **Options page**: auto-scan toggle + ffmpeg path config (explicit path field +
  Browse/Detect Automatically/Get ffmpeg… buttons — the last opens the official
  download page in a browser, deliberately NOT a silent auto-download-and-execute
  of an untrusted binary). Layout is functional but not final — user said "organize
  the options panel when we finish with all the things we're going to add", so
  expect more controls to land there before a final layout pass.

## Non-obvious gotchas discovered the hard way (do not re-derive, just remember)

1. **`header_events.headers_updated.emit()` is the actual fix for live column
   registration.** Neither `is_default=True`/`always_visible=True`, nor
   import-time-vs-enable()-time registration timing, nor manually toggling the
   header's column-picker menu ever made a plugin-added column render in an
   already-open window. The real mechanism: `picard.ui.itemviews.events.
   header_events` is a public `QObject` singleton every `BaseTreeView` subscribes
   to (`_on_header_updated`), which rebuilds the Qt column count AND recomputes
   every existing row's cell text. Emit it (deferred via `QTimer.singleShot(0, …)`
   to avoid racing tree-view construction) after registering a column.
2. **`setItemDelegateForColumn` only runs once**, inside each tree view's own
   `_init_header()`, over whatever columns existed at that exact moment. The
   `header_events` signal above fixes column *count*/*labels*, but NOT per-column
   delegate wiring for a column added after that point. Had to manually iterate
   `QApplication.instance().allWidgets()`, find tree widgets whose `.columns`
   attribute matches the shared `FILEVIEW_COLUMNS`/`ALBUMVIEW_COLUMNS` list, and
   call `setItemDelegateForColumn` on them directly (see
   `_install_delegate_on_live_views`).
3. **`Track.column(key)` only falls back to a linked file's value when
   `num_linked_files == 1`.** Duplicate files matched to the same track (more than
   one linked file) show nothing for a custom column, by design — Picard doesn't
   guess which file's value to show. Not a bug to fix; comparison logic must expect
   this.
4. **`api.plugin_config` has no `.get()` method** despite the official
   `docs/PLUGINSV3/API.md` example using it — `AttributeError`. Use plain `[...]`
   indexing (returns the registered default via `register_option`). This broke the
   options page's `load()` silently (checkbox never stayed checked) until traced
   via the log's caught-and-logged `_maybe_auto_scan` exception.
5. **`ProfileConfigSection`** (that's what `api.plugin_config` actually is at
   runtime) — don't trust the API docs' method list without a live check; grep
   Picard's own source (`picard/config.py` or wherever it's defined) if something
   throws `AttributeError` on a documented-looking call.

## Immediate next action if resuming heuristics work

LAME header inspection (item 2 of the old roadmap) is done — see the "Spectral
cutoff / likely transcode" entry above. Pick up at item 1 of the current "not
yet built" list (real DR14, block-based peak-vs-RMS dynamic range, replacing
the coarse LUFS proxy). Follow the same validate-against-real-output-before-
writing-code discipline used for every check so far — for DR14 that means a
known-DR-value reference file (e.g. from a published DR database) or a
synthesized signal with a hand-computed expected DR value, not just "looks
reasonable."
