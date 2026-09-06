---
parent: high-level-design
prefix: ENGINE
---

# Analysis Engine

## Context and Design Philosophy

This is the shared foundation both File Health and Track Health scoring depend on: every ffmpeg/ffprobe invocation, every parser that reads their output, container/tag structure validation via `mutagen`, and on-demand spectrogram rendering. It lives entirely in `analysis.py`, has no Picard imports, and is callable and testable standalone.

The defining constraint on this component is that its input — a file on disk, chosen by the user, of unknown provenance — is adversarial in the general case. A file downloaded from an untrusted source can carry a crafted container structure, a malformed tag frame, or tag values engineered to look like ffmpeg's own diagnostic output. This engine's job is to measure such a file safely: never let its content escape into a shell, a file path, or a piece of trusted-looking output it wasn't supposed to produce.

## Subprocess Invocation

Every ffmpeg/ffprobe call goes through one shared helper (`_run_subprocess`), which passes argv as a list — never a shell string — and folds timeouts and OS-level failures into a synthetic non-zero-exit result rather than raising, so every caller's existing "ffmpeg found nothing" handling covers them for free. On Windows, the same helper suppresses the console window every subprocess call would otherwise flash (via `creationflags=subprocess.CREATE_NO_WINDOW`, a no-op dict on macOS/Linux where the flag doesn't exist).

A scanned file's own filename is always passed as the value to an explicit `-i` flag, never as a bare trailing positional argument — a filename beginning with `-` risks being misread as an option by ffmpeg/ffprobe's argument parser otherwise.

## ffmpeg Version Requirements

`find_ffmpeg`/`_find_ffprobe` resolve the binary: an explicit user-configured path is validated as an existing, executable file (rejecting anything else, closing off "configure an arbitrary non-executable path" as a way to redirect what gets run); otherwise `shutil.which` searches `PATH`. `ffprobe` is looked for alongside the resolved `ffmpeg` binary first (same directory, matching every mainstream distribution's layout), falling back to a bare `PATH` search. A minimum ffmpeg version (3.1) is enforced — the floor `astats`' `metadata`/`reset` options need. A version string that doesn't parse (some git/dev-snapshot builds report a non-numeric string like `N-12345-gabcdef` instead of a release number) is treated as "let it through" rather than a failure: those builds are essentially always newer than any numbered release, so blocking on an unparseable string would reject far more legitimate newer installs than it would ever catch a genuinely too-old one.

## Merged-Decode Architecture

Track Health's checks (clipping, true peak, spectral cutoff, fake-hi-res, phase, loudness, the bandwidth sweep) all need the same decoded PCM. One ffmpeg invocation (`_run_merged_analysis`) uses `asplit` to feed a single decode into independently-tagged parallel filter branches (`astats@main`, `highpass=...,volumedetect@cutoff`, `ebur128@loud=peak=true`, and so on), rather than one process per check. DR14 remains a separate pass (its own block-windowed decode needs different structure), and the spectrogram render is separate too (an on-demand, not-part-of-every-scan operation).

## Defensive stderr Parsing

ffmpeg prints a file's own container/ID3 tag values to stderr at its default loglevel, before any filter or decode output — the same stream every filter's diagnostic output (the numbers this engine actually wants) also lands in. Raising the loglevel to suppress that dump also suppresses the filter diagnostics this engine depends on, so it isn't an available fix; every parser is instead scoped to trust only its own filter's genuine output.

`_filter_instance_output(stderr, tag)` slices out only the lines belonging to one named filter instance, matched by requiring the `@tag @ ` marker to appear inside a line's own leading `[...]` bracket — not merely anywhere in the line. A metadata-dump line can never start at column zero with `[` the way a real filter log line always does, so a tag value crafted to contain the marker text can't be spliced into the "genuine" output ahead of the real measurement. (An earlier version of this scoping used a bare substring test across the whole line, which a crafted tag value could pass — since `_parse_mean_volume` takes the first regex match and the metadata dump always precedes real filter output, that version was a live exploit, not just a theoretical one; see Decisions & Alternatives.)

`ebur128`'s Summary block is an exception this scoping can't cover — only its header line is tagged, while the actual "Integrated loudness"/"True peak" numbers below it aren't. That parser instead anchors to everything after the *last* occurrence of the filter's own tag marker in stderr, which only ffmpeg's own `ebur128` filter can emit and which — because ffmpeg always prints a file's tag dump before any decode/filter output runs — is guaranteed to be the genuine Summary block, not an earlier crafted decoy.

`CORRUPTION_BITS_LEFT_PATTERN` (the MP3 bit-reservoir corruption signal) is a decoder-level diagnostic with no filter-instance tag of its own; it's instead anchored to ffmpeg's own `[mp3float @ 0xADDR] bits_left=N` log-line format, which a metadata-dump line (never starting a line with `[`) can't produce.

## Stream Probing

`_probe_stream_info` runs one `ffprobe` call per file for everything the rest of the pipeline needs from container/stream metadata (sample rate, codec, profile, bitrate, channel count) — read once, not once per use, since ffprobe only reads headers. Any failure (unparseable output, no audio stream) yields an all-`None` result; every caller already treats missing fields as "can't measure this" — an unprobeable stream degrades every check that needs that field to "doesn't run" rather than blocking the whole scan, the same graceful-degradation convention used for every other optional measurement in this engine. There is deliberately no separate "unknown" state distinct from "confirmed not applicable" anywhere in this engine; introducing one for probe failures specifically would be a new, inconsistent special case rather than a fix.

## Corruption and Structural Checks

- **Audio corruption signature** — a `bits_left` count from ffmpeg's own MP3 decoder (gated on `-err_detect compliant`, at zero extra decode cost since it's read from the same merged-analysis pass). MP3-specific by nature (the bit-reservoir concept this detects doesn't exist in other codecs); a non-MP3 file simply never produces this signature, which reads as "nothing found" — the same non-distinguishing treatment "no signal" gets everywhere else in this engine. Informational only: presence is a real decoder-level signal, not proof of an audible defect in every case.
- **Xing-header size mismatch** — the Xing/Info header's own declared audio-stream byte count compared against the real on-disk audio-stream size. A large mismatch indicates truncation or concatenation independent of *why* the two disagree.
- **Tag structure validation** — `mutagen.File()`'s own (stricter, format-detecting) parse of ID3/Vorbis-comment/APEv2/MP4 atom structure; an exception here is a genuine container-level defect independent of whether ffmpeg's own (more lenient) tag reading tolerates the same file.
- **Embedded artwork corruption** — probes for an attached-pic/video stream via `ffprobe`, then attempts to decode its first frame via `ffmpeg`; a stream that exists but won't decode is corrupt artwork, distinguished from "no artwork present at all" (not a defect).

## Dynamic Range (DR14)

ffmpeg has no native DR14 filter; block-level RMS/Peak numbers from `astats` are combined into the DR14 formula in Python, reimplemented against the Pleasurize Music Foundation "TT DR Meter" algorithm and validated bit-for-bit against its open-source reference implementation. A file too short to contain even one full analysis block yields no usable blocks for that channel; if every channel is too short, DR14 is reported as not-measured (`None`) rather than a degenerate value computed from zero real data.

## Spectrogram Rendering

On-demand only (a Details-window button), never part of a regular scan — a real extra decode-and-render cost distinct from every other measurement, which all reuse the one merged decode. Renders to a caller-supplied temp file path via ffmpeg's `showspectrumpic` filter; the caller owns the temp file's lifecycle (creation and cleanup on every exit path, including an unexpected exception mid-render — confirmed the ffmpeg-binary-vanishes-mid-call case specifically, since `find_ffmpeg` can raise before this function's own subprocess call ever runs, and the caller's cleanup path is exception-safe against that). A non-zero exit or a missing output file are both treated as failure regardless of what partial state ffmpeg may have left behind.

## Security Posture

No code in this engine ever evaluates, executes, or deserializes anything constructed from a scanned file's own content — every value read from ffmpeg/ffprobe output or `mutagen`'s tag parse is treated as inert data (a string, a number, a boolean), never as something to `eval`, `exec`, or unpickle. This engine also never opens a network connection of its own — every measurement is a local `ffmpeg`/`ffprobe` subprocess call against a file already on disk. This is the concrete implementation of the HLD's Security Model for the engine that touches untrusted file content directly; the shell-avoidance and stderr-scoping properties documented above are the other half of that same posture.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Subprocess invocation style | Argv list via `subprocess.run`, never a shell string | Shell string with manual quoting | [inferred] A scanned filename is adversary-controlled in the general case; shell interpretation of untrusted filename content is a class of bug this design refuses to introduce outright rather than mitigate via careful quoting. |
| Windows console suppression | `creationflags=CREATE_NO_WINDOW`, platform-gated | Accept the console flash; suppress loglevel instead | [inferred] A scan makes several subprocess calls, so the flash is visible and repeated; suppressing ffmpeg's own loglevel was rejected because the filter diagnostics this engine parses are emitted at the same level as the noise it would suppress. |
| stderr scoping mechanism | Marker required inside a line's own leading `[...]` bracket | Bare substring test for the marker anywhere in the line | Confirmed exploitable in practice: a crafted tag value containing the marker text passed the bare substring test and, because the metadata dump always precedes real filter output and `_parse_mean_volume` takes the first regex match, returned the attacker's fabricated value instead of the genuine measurement. |
| Corruption-signature detection | `bits_left` diagnostic count, gated on `-err_detect compliant` | A `Max_difference`/`Mean_difference` ratio heuristic | [inferred] The ratio heuristic false-positived on ~79% of ordinary music in a real-library validation sample (quiet-to-loud dynamic swings produce the same ratio signature as genuine corruption); `bits_left` was validated against 600 real files with a clean separation between confirmed-corrupt and confirmed-clean cases. |
| True Peak/loudness measurement filter | `ebur128=peak=true` | `loudnorm` (measures the same quantities but performs an unused normalization pass) | [inferred] `loudnorm` and `ebur128` agree to within ~0.04dB on average in a 100-file validation sample; `ebur128`'s pure-metering pass roughly halved total scan time by dropping the unused normalization work. |
| DR14 implementation | Reimplemented in Python from `astats`' block-level RMS/Peak | A hand-rolled PCM decode + windowed RMS/peak pass | [inferred] ffmpeg has no native DR14 filter, but already computes the underlying block statistics via `astats`; reimplementing PCM decode by hand would duplicate work ffmpeg already does. |

## Open Questions & Future Decisions

### Deferred
1. `content_hash` hashes the whole file's raw bytes (not isolated to the audio stream), so a tag-only edit changes the hash even though the audio itself didn't change. Isolating to PCM-only content is future work now that the pipeline already decodes audio for other purposes.
2. The stderr-scoping hardening covers every parser currently in the engine; a new check added later that reads stderr must independently apply the same scoping discipline — there's no structural guard (a lint rule, a wrapper type) that would catch a new unscoped parser being added by mistake.
3. If a future ffmpeg version changed the exact format of its own diagnostic log lines (the `[toolname @ address] message` shape every anti-spoofing fix in this engine relies on), nothing would crash — every affected check would just quietly stop finding anything, as if every file were clean. Dismissed as not worth defending against now: this format has been stable for years, and the rest of this engine already accepts the same class of risk elsewhere (the MP3 corruption check is tied to one specific decoder's log message with no format-change tripwire either) without it ever having mattered in practice.
4. In principle, a byte-replacement from `errors='replace'` handling a file's own garbled tag text could land on exactly the right character to erase part of a genuine ffmpeg diagnostic line, hiding a real defect. Dismissed as not a real risk: the replacement only ever touches text that came from the *file's* tags, never ffmpeg's own diagnostic output (which is engine-generated, plain-ASCII text with nothing to replace) — the two never overlap in the same string.

## References

- ffmpeg filter documentation: `astats`, `volumedetect`, `ebur128`, `aphasemeter`, `silencedetect`, `showspectrumpic`.
- Pleasurize Music Foundation "TT DR Meter" specification.
- `docs/high-level-design.md` § Security Model — the trust-based posture this engine's shell-avoidance, stderr-scoping, and no-dynamic-execution properties implement.
- `.omp/HANDOFF.md` — pre-LID narrative history for this component's evolution (corruption-detection redesign, the `ebur128`/`loudnorm` swap, the stderr-spoofing hardening pass); not part of the arrow.
