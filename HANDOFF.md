# Handoff — File Health plugin for MusicBrainz Picard

Session hit a context limit; this captures state for continuation in a fresh session.

## What this is

A Picard v3 plugin (`~/Documents/code_repo/picard-file-health`, own git repo, 36 commits)
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
- `/tmp/dr14test2/{uniform,one_loud,ramp,stereo_uniform,short,one_loud_44k,
  ramp_44k}.wav` — synthetic multi-block sine WAVs (Python `wave`/`struct`, known
  per-block amplitudes) built specifically to exercise DR14's top-20%-by-RMS and
  2nd-highest-peak selection logic (a single loud block, a gradual ramp, ties, a
  sub-one-block file, both 48kHz and 44100Hz for the sample-rate-fudge quirk) —
  matched bit-for-bit against the reference `dr14meter` PyPI tool
  (`python3 -m venv /tmp/dr14venv && /tmp/dr14venv/bin/pip install dr14meter`,
  then `dr14meter -p -n -1 -b <dir>`) before being trusted.
- `/tmp/bitrate-test/{aac_96.m4a,aac_192.m4a,opus_64.opus,opus_160.opus}` — real
  ffmpeg encodes (`-c:a aac`/`-c:a libopus`) of `song-a.mp3` at known bitrates,
  above/below the AAC-LC (150kbps) and Opus (128kbps) transparency thresholds —
  validates `_bitrate_transparency_note()`'s threshold logic and the stream-vs-
  format bitrate fallback (Opus streams here don't report a stream-level
  `bit_rate` in ffprobe at all — format-level is the only source). `song-c.ogg`
  (already in the fixture set above, Vorbis 112kbps) covers the below-threshold
  Vorbis case; no above-threshold Vorbis fixture — this ffmpeg build has no
  `libvorbis` encoder. No real HE-AAC fixture either — no HE-AAC encoder
  available (`libfdk_aac` not compiled in); that skip path was validated via a
  synthetic `StreamInfo` instead (see commit history for the exact calls).
- `/tmp/samplerate-test/{fake_hires_from_16k,fake_hires_96k,genuine_hires_96k}.wav`
  — `fake_hires_from_16k.wav`: `lowpassed_16k.wav` (content only to 16kHz)
  resampled to 96kHz via `ffmpeg -ar 96000` — clean fake-hi-res case, measured
  -91dB above 24kHz. `genuine_hires_96k.wav`: a sine tone synthesized directly
  at 30kHz/96kHz (Python `wave`/`struct`) — real ultrasonic content, measured
  -14dB above 24kHz, confirms no false positive on genuine hi-res.
  `fake_hires_96k.wav`: `wideband_noise.wav` (broadband noise with strong
  energy right up to its own 22.05kHz Nyquist) resampled to 96kHz — an
  adversarial edge case where highpass-filter transition-band leakage from
  that near-Nyquist energy could plausibly cause a false negative; measured
  -18.3dB, correctly still not gated (above the -60dB silence threshold).
- `/tmp/hum-test/{clean_music,hum50,hum60,bassy_no_hum}.wav` — whole-file
  (no silence gap) synthetic fixtures used for the initial narrowband-vs-
  control-band technique validation; superseded by the gap fixtures below
  for the actual shipped (quiet-passage-scoped) design, kept as evidence
  the naive whole-file version was correctly rejected (see commit history).
- `/tmp/hum-test/{gap_no_hum,gap_hum50,gap_hum60,gap_bassnote_no_hum,
  full_pipeline_hum,full_pipeline_clean}.wav` — synthetic fixtures with a
  real 1.5s-playing / 1.5s-"quiet" structure (Python `wave`/`struct`).
  `gap_hum50`/`gap_hum60`: a 50Hz/60Hz tone present throughout, including
  the "quiet" half (classic hum signature) — correctly detected, ~14dB
  elevation. `gap_bassnote_no_hum`: a real bass note at 50Hz that stops
  along with the rest of the music (true silence in the second half) —
  correctly NOT flagged, the key case the naive whole-file approach
  couldn't distinguish from real hum. `full_pipeline_hum`/
  `full_pipeline_clean`: wideband content (real energy up to 18kHz, passes
  the existing spectral-cutoff gate) plus/minus the same hum signature —
  validates the full `analyze_file()` wiring end-to-end (hum note lands in
  `info` without touching `tier`).

Always validate new ffmpeg-filter-based logic directly against real ffmpeg output
first (`ffmpeg -i FILE -af FILTER -f null -`, read stderr) before writing parsing code
— this caught multiple wrong assumptions during the build (see commit history).

## Current real capabilities (all empirically calibrated, not guessed)

Eight checks in `analysis.analyze_file()`:

**Gate checks** (any one present → tier forced to "Bad"):
1. **Clipping** — `astats` "Flat factor" > `MIN_FLAT_FACTOR_FOR_CLIPPING` (1.0).
   Calibrated against 5 severities: clean=0.0, loud-but-not-clipped noise=0.0094,
   mildest real clip (2% overdrive)=16.06, hard clip=31.88.
2. **True Peak** — `loudnorm`'s `input_tp` ≥ `TRUE_PEAK_THRESHOLD_DBTP` (0.6dBTP,
   not the theoretical 0dBTP full-scale ceiling — see below). Catches inter-sample
   DAC reconstruction overshoot that Flat factor structurally cannot see (validated:
   the white-noise test file has Flat factor 0.0094 — below the clipping threshold,
   correctly not "clipping" — but True Peak +3.71dBTP, a real distinct defect).
   Raised from 0.0 to 0.6dBTP after a user reported two real commercial masters
   (`assets/*.m4a`, +0.10/+0.12dBTP) flagged "Bad" that don't sound bad. Root cause:
   `loudnorm`'s True Peak measurement follows ITU-R BS.1770-4 Annex 2, which
   upsamples to 192kHz (~4x oversampling for 44.1/48kHz-family sources) before
   taking the peak — and that Annex's own worked table of oversampling error gives
   a maximum theoretical under-read of 0.554dB at 4x oversampling (the table's own
   caption calls this row "probably covers the range of interest"). A reading a few
   tenths of a dB over 0dBTP is within the standard's own documented measurement
   uncertainty, not reliable evidence of real playback clipping. 0.6dBTP sits just
   outside that 0.554dB worst case while staying two orders of magnitude below the
   +3.71dBTP/+5.0dBTP genuine-defect fixtures this gate was originally calibrated
   against. Re-validated against the full fixture catalogue after the change:
   zero output differences — no existing fixture's True Peak measurement fell
   between 0.0 and 0.6dBTP, so this was a surgical fix, not a regression risk. The
   two `assets/*.m4a` files now correctly fall through to the DR14 gradient
   instead (DR7, "Poor" — a real, independent, unrelated finding: heavily
   compressed dynamics are common in modern loudness-war masters and aren't what
   the user was disputing).
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
5. **Fake hi-res / upsampled** — same technique as check 3, parameterized
   differently: `highpass=f=24000,volumedetect` `mean_volume` < -60dB, but only
   evaluated when `stream_info.sample_rate > 48000` (declared "hi-res":
   88.2/96/176.4/192kHz+) and `has_signal` (reusing the same near-silence guard
   as check 3). A file upsampled from an ordinary-rate source has a hard wall at
   that source's own Nyquist — resampling adds no genuinely new content — so no
   real energy above 24kHz (chosen to sit safely past both 44.1kHz-family's
   22.05kHz Nyquist and 48kHz-family's 24kHz Nyquist, so real CD/DVD-quality
   masters of either family aren't penalized) means the file isn't what its
   sample rate claims. Validated on real ffmpeg-resampled fixtures: a
   16kHz-lowpassed 44.1kHz source resampled to 96kHz measured -91dB (silence)
   above 24kHz (correctly gated); a genuine 96kHz file with real content
   synthesized at 30kHz measured -14dB (correctly NOT gated); a 96kHz upsample
   of unfiltered wideband noise (an adversarial edge case — the source has
   strong energy right up to its own 22.05kHz Nyquist, which leaks into the
   highpass filter's transition band) measured -18.3dB, still correctly NOT
   gated — no false positive even on that harder case. This is a real measured
   defect, not a proxy, so it's a gate like check 3, unlike the informational
   bitrate-transparency check below (which infers likelihood from declared
   codec/bitrate metadata rather than measuring the decoded signal directly).

**Gradient** (only evaluated if no gate fired): a real DR14 dynamic-range
measurement (Pleasurize Music Foundation "TT DR Meter" algorithm — non-
overlapping 3-second blocks, top 20% loudest by RMS averaged in the power
domain, compared against the *second*-highest peak across all blocks),
replacing the old coarse LUFS-bucket proxy. `analysis._measure_dr14()`
reimplements the algorithm against ffmpeg's own `astats` filter
(`asetnsamples` to frame exact 3-second blocks, `reset=1` so each block's
stats are independent, `ametadata=print` to dump per-block RMS_level/
Peak_level) rather than decoding PCM and doing the math by hand — ffmpeg
has no native DR14 filter, so the block-level numbers it already knows
how to compute get combined into the real formula in Python. Needs one
extra `ffprobe` call (sample rate, to size the 3-second block in
samples) — `analysis._find_ffprobe()` looks for it alongside the
configured `ffmpeg` binary, same as every mainstream ffmpeg distribution
ships it. Validated bit-for-bit (not just "looks plausible") against the
open-source reference implementation (`dr14meter`/`dr14_t.meter`, itself
tested identical to the official Windows tool) on synthetic multi-block
WAV fixtures with known per-block levels — 7/7 exact matches including a
deliberately non-obvious case (an undocumented +60 samples/sec block-size
quirk the reference tool applies *only* at exactly 44100Hz — confirmed
empirically that omitting it flips the rounded DR value on boundary-case
audio, so it's replicated rather than assumed to be a no-op artifact).
Quality bands (Poor/Ok/Good/Great/Excellent) are grounded in the official
TT DR Offline Meter manual's own documented color scale (red < DR8,
green ≥ DR14, yellow between) and worked examples (DR9 called a "market-
oriented compromise", DR12-14 called "more desirable") — not guessed.
Finally puts the "Great" tier to use; it existed in `TIERS` but the old
4-bucket LUFS gradient never produced it.

**Informational only** (surfaced but never affects tier — neither is a measured
defect in the decoded signal, just a statistical proxy): mono content duplicated
into both stereo channels (wasteful, not a defect); and, new this pass,
below-transparency-bitrate lossy encoding. `analysis._probe_stream_info()`
(one ffprobe call, also supplies DR14's sample rate — consolidated from two
separate probes) reads codec_name/profile/bitrate, compared against
`TRANSPARENT_BITRATE_KBPS` — HydrogenAudio/Xiph's own published
listening-test consensus, fetched directly from primary sources (not the
placeholder numbers this roadmap item started with — see below):
MP3 192kbps, AAC(LC) 150kbps, Vorbis 160kbps, Opus 128kbps. HE-AAC
(detected via the `profile` field containing "HE-AAC") is explicitly
skipped rather than checked against the LC threshold — its SBR extension
is transparent at much lower bitrates, so applying the LC number would be
a wrong comparison, not just an imprecise one; untested against a real
HE-AAC file since this ffmpeg build has no HE-AAC encoder (no libfdk_aac),
so the skip logic is validated via a direct call with a synthetic
`StreamInfo` instead. Lossless codecs and any codec without a published
threshold are silently skipped, not guessed at.

**Possible mains hum** (new this pass): `analysis._detect_hum()` — a
sustained narrow tone at 50Hz (most of the world) or 60Hz (Americas/parts
of Asia) leaking in via ground loops/unshielded cabling/a bad power
supply somewhere in the chain. A genuine musical note at the same
frequency stops when the music does; hum doesn't — so this only looks
inside ffmpeg's own `silencedetect`-identified quiet passages (where the
*music* has dropped out, not necessarily digital silence — a real hum
passage sits at an audible, non-trivial level, `HUM_SILENCE_THRESHOLD_DB`
= -25dB rather than a strict silence cutoff), never the whole file,
specifically to tell the two apart. Within the longest such passage,
compares narrowband RMS (`bandpass=f=50:width_type=h:w=2,astats`) at the
mains frequency against a nearby "control" frequency (55Hz/65Hz) with the
same bandwidth — real hum shows as an outsized, narrow peak exactly at
its own frequency (`HUM_ELEVATION_THRESHOLD_DB` = 8dB above control) and
nowhere nearby; incidental musical content spreads more evenly. No quiet
passage found → no claim either way (not "no hum", just "nothing to
check against" — most loud modern masters never qualify). Validated on
synthetic fixtures with a real playing/silent gap: hum persisting into an
otherwise-silent passage measured ~14dB above its control band; a
genuine bass note at the same frequency that stopped along with the rest
of the music (the normal case) left both bands at true silence in that
passage — clean separation, no false positive. Kept informational (like
bitrate-transparency) rather than a gate: even with the quiet-passage
scoping, a sustained drone/pedal note that happens to ring into a quiet
moment is a possible (if rarer) false positive, so this is strong
likelihood, not the kind of certainty clipping/cutoff/true-peak/fake-
hi-res have.

**Also real, not a "check" per se**: `analysis.content_hash()` — blake2b hash of the
file's raw bytes, compared scan-to-scan to detect "this file's bytes changed since
last scan" (caveat: whole-file, so tag edits also trigger it, not just audio changes).

## Not yet built (explicit roadmap, in priority order as last discussed)

1. Isolating `content_hash()` to just the decoded PCM stream (skip tag blocks) now
   that we decode audio anyway for the real checks — removes the false-positive
   "changed" flag on pure tag edits.

Done this pass: the astats/spectral-cutoff/loudnorm/phase/fake-hi-res
whole-file measurements now run as one merged ffmpeg invocation
(`analysis._run_merged_analysis()`, `asplit` into named filter instances —
`astats@main`, `volumedetect@cutoff`, `loudnorm`, `aphasemeter`,
`volumedetect@hires`), replacing what used to be 3-5 separate process
spawns/decodes per file with one. `StreamInfo` gained a `channels` field
(one more entry on the existing single ffprobe call, no new probe) so
whether to even include the phase-check/hires-check branches is decided
upfront from container metadata, before any ffmpeg filter pass runs —
necessary because feeding `aphasemeter` a genuinely mono file doesn't
error but does print a bare `mono_start: 0` line that the existing
value-blind `'mono_start' in stderr` parser would misread as "mono
content duplicated into stereo", so that branch must be structurally
absent for mono files, not just ignored after the fact (confirmed
empirically before trusting the merge, per this repo's own standing
discipline). Two same-type filters in one invocation (astats appears
once; volumedetect can appear twice) print indistinguishable output
*unless* each instance is explicitly named (`filtername@tag`) — ffmpeg
then tags every line from that instance `[filtername@tag @ 0xADDR]`,
which `analysis._filter_instance_output()` slices stderr on so each
existing single-filter parser runs unmodified against just its own
branch. DR14's own astats pass and hum detection (silencedetect + up to
4 scoped bandpass passes) stay separate, later passes — both are
skipped outright once `issues` is non-empty, a real perf saving folding
them into the always-run merged graph would give up; hum detection
additionally can't join it at all, since it needs `silencedetect`'s
quiet-window result before it even knows where to run its scoped
bandpass filters. Validated bit-for-bit against every existing fixture
(same tier/issues/info/dr14/lufs/true-peak/flat-factor/cutoff/hires
values, old code vs. new, across the full fixture catalogue above) —
pure perf change, not correctness, confirmed rather than assumed.


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
  (`tagger.trash_files`, confirmed first). Also has its own "Scan Unscanned"/
  "Rescan All" buttons (dispatch the same `_scan_one`/`run_task` pattern as
  `ScanHealthAction`) that redraw the panel in place via `refresh()` when scans
  complete — no more closing and reopening the panel to see fresh results. The
  panel tracks its own `(label, files)` groups so `refresh()` doesn't need the
  caller to re-derive track groupings.
- **Options page**: auto-scan toggle + ffmpeg path config (explicit path field +
  Browse/Detect Automatically/Get ffmpeg… buttons — the last opens the official
  download page in a browser, deliberately NOT a silent auto-download-and-execute
  of an untrusted binary). Status label shows the resolved binary's parsed
  version and flags it (red, bolded "too old") if below
  `analysis.MINIMUM_FFMPEG_VERSION` (3.1 — every filter/option this plugin
  uses confirmed present as of that release by reading FFmpeg's own
  release-tagged source, not guessed). This is a proactive heads-up only —
  the real enforcement is `analysis.check_ffmpeg_version()`, called at the
  top of every `analyze_file()`, which refuses to scan at all against a
  too-old binary (raises `FfmpegVersionTooOldError`, a subclass of
  `FfmpegNotFoundError` so it flows through the exact same error-surfacing
  path). Layout is functional but not final — user said "organize the
  options panel when we finish with all the things we're going to add", so
  expect more controls to land there before a final layout pass.

## Error handling / abuse-resistance (every input is user-supplied)

Every file this plugin scans could be corrupt, truncated, adversarially
crafted, or not really audio at all despite its extension — treated as an
expected, handled outcome, not a bug, throughout `analysis.py`:

- All ffmpeg/ffprobe calls go through `analysis._run_subprocess()`, which
  never lets `subprocess.TimeoutExpired` or `OSError` (binary vanished
  mid-scan, permission race, etc.) escape as a raw exception — folds them
  into a synthetic failed `CompletedProcess` instead, so every existing
  call site's "ffmpeg found nothing" parsing (already written defensively
  throughout this module) handles them for free. Also passes
  `errors='replace'` to guard against non-UTF8 bytes in ffmpeg's own
  stderr raising `UnicodeDecodeError`.
- The *first* ffmpeg call in `analyze_file()` (astats) is the one
  exception: its returncode is checked explicitly, and a non-zero exit
  raises `analysis.AnalysisError` — every other measurement depends on
  astats having actually decoded the file, so silently falling through to
  "no defects found" for a file ffmpeg couldn't even open would be a false
  clean bill of health, not graceful degradation. Confirmed live: a text
  file renamed `.mp3`, 1KB of `/dev/urandom` renamed `.mp3`, a truncated
  real FLAC, and a nonexistent path all correctly raise `AnalysisError`
  instead of scoring "Excellent" (the pre-fix behavior — genuinely
  undecodable input has no clipping/cutoff/true-peak matches in empty
  stderr, so every gate silently passed).
- `AnalysisError`/`FfmpegNotFoundError` (which `FfmpegVersionTooOldError`
  subclasses) both flow through `__init__.py`'s existing
  `_scan_one`/`_scan_finished` error-surfacing path to a statusbar
  message — see gotcha #6 below for a real bug this pass caught and fixed
  in that path.
- Minimum ffmpeg version gating (`analysis.MINIMUM_FFMPEG_VERSION = (3, 1)`,
  `analysis.check_ffmpeg_version()`) — see the Options page entry above.
- Accepted, documented tradeoff, not fixed: ffmpeg's own stderr for a
  pathological file that spams warnings (e.g. "Invalid NAL unit" on
  every corrupt frame) is still fully buffered in memory
  (`capture_output=True`) — `-loglevel error` would suppress that, but
  also fully suppresses the astats/loudnorm/volumedetect stat lines this
  plugin depends on (confirmed empirically: `-loglevel error` produces
  zero stat output). The `FFMPEG_TIMEOUT_SECONDS` (60s) bound is the only
  cap on this, not a hard memory limit.

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
6. **`picard.util.thread.run_task`'s `Runnable.run()` catches every
   `BaseException`** from the background-thread function and delivers it
   as `error=` to the completion callback — so a raw uncaught exception in
   `_scan_one`/`analysis.analyze_file()` was never going to crash Picard.
   But `__init__.py`'s `_scan_finished(file, result, error)` originally
   only handled `error is None` — any real exception that escaped
   `_scan_one`'s own `try/except` silently did nothing but
   `file.clear_pending()`/`file.update()`, leaving the file looking
   permanently un-scanned with zero indication anything went wrong (only
   visible in Picard's internal debug log via `Runnable.run()`'s own
   `log.error(traceback.format_exc())`). Fixed by giving `_scan_finished`
   an explicit `if error is not None:` branch that posts a statusbar
   message — defense in depth underneath `_scan_one`'s now-broader
   `except (FfmpegNotFoundError, AnalysisError)`, for whatever a future
   change might still let slip through uncaught.

## Immediate next action if resuming heuristics work

Hum/mains-noise detection and ffmpeg filtergraph consolidation (former
roadmap items 1 and the top "not yet built" entry) are both done — see
"Possible mains hum" and the "Done this pass" note above. The only
remaining roadmap item is `content_hash()` PCM isolation — a small
self-contained fix (decode-and-hash instead of read-raw-bytes-and-hash),
no ffmpeg-filter-validation discipline needed.
