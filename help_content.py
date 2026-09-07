# File Health, a Picard plugin that scores audio files for structural
# and perceptual defects.
#
# Copyright (C) 2026 shadejinx
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""Static HTML content for the in-app Help dialog (Options page -> Help
button). Kept as plain data, separate from __init__.py's widget/wiring
code, since it's prose, not logic — a future rewording shouldn't require
touching the dialog plumbing, and vice versa.

Each entry in SECTIONS is (tab title, HTML body) — rendered one per tab
in a QTextBrowser. HTML is Qt's rich-text subset (headings, lists, bold,
tables) — no external stylesheet, no script, no network image loads.
"""

_SCANS_HTML = """
<h2>Two independent scans</h2>
<p>File Health checks whether the <b>file itself</b> is intact. Track Health
checks how the <b>audio actually sounds</b>. They run separately, store
separate results, and neither blocks nor requires the other: matching a
file to a track, or scanning one, doesn't touch the other's data.</p>

<h3>File Health</h3>
<p>A quick, structural-only scan: decode the file, look for corruption
signatures, check the embedded artwork/tag structure, compare the file's
declared size against a header's own byte count, and grade the codec/bitrate
against published transparency thresholds. There are no sensitivity sliders
for this one; a file either has a structural defect or it doesn't, so
there's nothing to tune.</p>
<p>Runs automatically on newly added files if "Automatically scan File
Health for newly added files" is checked above; otherwise (or at any time),
right-click any file, cluster, or track and choose <b>Scan File Health...</b>.
It's the only scan available on unmatched files, since it doesn't need a
matched track to run.</p>
<p><b>File Health tiers</b>, worst to best:</p>
<ul>
<li><b>Unplayable</b>: ffmpeg couldn't decode it at all (corrupt,
truncated, unsupported, or timed out).</li>
<li><b>Bad</b>: decodes, but has a genuine structural defect in the audio
data itself, such as an audio corruption signature or a file-size
mismatch against its own header — nothing short of re-encoding from a
clean source fixes these.</li>
<li><b>OK</b> / <b>Good</b> / <b>Excellent</b>: structurally sound,
graded purely by codec and bitrate. Corrupt embedded artwork or a
malformed tag structure cap a file at OK rather than forcing Bad, since
Picard itself fixes both by re-saving the file. Lossless formats (FLAC,
ALAC, WavPack, TTA, APE, uncompressed PCM) are always Excellent when
structurally sound. Lossy formats are graded against the published
"generally transparent" bitrate for that codec (see the Checks &amp;
Why tab): at or above it is Good, below it is OK.</li>
</ul>

<h3>Track Health</h3>
<p>A heavier scan: a full decode plus several ffmpeg audio-analysis passes
measuring clipping, inter-sample peaks, spectral cutoff, out-of-phase
channels, background noise floor, and dynamic range. It's always manual;
right-click a matched track and choose <b>Scan Track Health...</b>, or use
the buttons in the Details window. It never runs automatically, since it's
meaningfully slower than File Health.</p>
<p><b>Track Health tiers</b>, worst to best: <b>Unplayable</b>, <b>Bad</b>,
<b>OK</b>, <b>Good</b>, <b>Excellent</b>. Unplayable means the same thing
it does for File Health &mdash; ffmpeg couldn't decode the file at all, so
there was nothing left to measure &mdash; it's not a separate verdict,
just the same underlying fact reported on both scans rather than leaving
Track Health with no result at all.</p>
<p>Every check contributes a 0-1 "defect score" (0 = comfortably clean, 1 =
fails even the most lenient setting), weighted by how confidently that
check's measurement maps to something actually audible, then averaged into
one composite score. A single borderline reading, such as True Peak sitting
just a fraction of a dB over its threshold, can't push a file to "Bad" on
its own; that verdict is reserved for files where several checks agree, or
one is badly wrong. The sliders under "Track Health Sensitivity" above each
move their check's own threshold directly, which reshapes the composite
score the next time you scan.</p>
"""

_CHECKS_HTML = """
<h2>File Health checks (structural, no sliders)</h2>
<ul>
<li><b>Decode failure.</b> ffmpeg can't decode the file as audio at all:
corrupt, truncated, unsupported, or timed out. Forces
Unplayable.</li>
<li><b>Audio corruption signature.</b> ffmpeg's own MP3 decoder logs a
<code>bits_left</code> diagnostic when a frame's bit-reservoir pointer
decodes to an impossible value, a genuine internal inconsistency rather
than a guess. Validated against 600 files spanning five audio
libraries: 99.2% showed zero occurrences (including one file independently
confirmed clean by ear despite thousands of unrelated, harmless
"overread" warnings), while every engineered corruption fixture produced
at least one.</li>
<li><b>File-size mismatch.</b> Compares the Xing/Info VBR header's own
declared audio-stream byte count against the actual bytes on disk (minus any
ID3 tag wrapper). A mismatch beyond 5%, independent of any decode, is a
structural fact that the file was altered after encoding: data
appended after the original stream ended, or the file cut
short. Calibrated against a 500-file sample: ordinary files landed
within 0.43% (encoder rounding noise); four outliers sat at
21%-182%, three of which also independently showed the corruption
signature above.</li>
<li><b>Corrupt embedded artwork.</b> ffmpeg's own image decoder can't
decode the file's embedded picture. Caps the file at OK rather than
forcing Bad — Picard itself fixes this by re-adding artwork.</li>
<li><b>Malformed tag structure.</b> Mutagen's own generic tag-frame parser
fails to parse a tag block. Also caps at OK rather than Bad, for the same
reason: re-saving the file's tags in Picard fixes it.</li>
<li><b>Bitrate/codec transparency.</b> Informational grading, not a
defect: HydrogenAudio/Xiph's own published listening-test consensus for
"generally transparent" is MP3 at/above 192kbps, plain AAC-LC at/above
150kbps (HE-AAC's SBR extension is transparent much lower and is exempted
from this check entirely), Vorbis at/above 160kbps, Opus at/above
128kbps VBR.</li>
</ul>

<h2>Track Health checks (perceptual, each with a slider)</h2>
<ul>
<li><b>Clipping (CLP).</b> astats' "Flat factor": runs of identical
consecutive samples at/near full scale. Genuine clipping produces a sharp,
distinct signature (16-32) versus even loud, non-clipped white noise
(0.0094). Default threshold: 1.0.</li>
<li><b>True Peak (TPK).</b> loudnorm's own ITU-R BS.1770-4 inter-sample
peak measurement, which catches reconstruction overshoot that can clip on
playback even when no single sample is at full scale (something Flat
factor alone can't see). Default threshold: +0.6dBTP, deliberately above
the theoretical 0dBTP ceiling to stay outside that same standard's own
documented ~0.554dB measurement margin at 4x oversampling, so a
technically compliant master isn't penalized for the meter's own
uncertainty.</li>
<li><b>Spectral Cutoff (TRB) / Fake Hi-Res (HRS).</b> Checks for
energy above 17kHz (ordinary-rate files) or above 24kHz (hi-res-rate files
above 48kHz). A hard silence wall there is the signature of an earlier
lossy encode (a transcode) or of upsampling from an ordinary-rate source
("fake hi-res"; genuine hi-res content extends past 24kHz). When
the file carries a LAME encoder header, the check also confirms against
that encoder's own recorded low-pass setting for decisive, not just
heuristic, evidence that the missing content isn't this encode's own
doing. Fake Hi-Res shares this same slider and has no separate control of
its own.</li>
<li><b>Out-of-Phase Channels (PHS).</b> aphasemeter's phase-angle
measurement between left and right. Channels canceling out near-total
inversion will sound hollow or vanish entirely when summed to mono (many
phone/laptop/car speakers do this). Default trigger: 170&deg; from
perfectly in-phase.</li>
<li><b>Noise Floor (NSF).</b> Broadband RMS level measured in the track's
own longest quiet passage (found via silencedetect). An elevated level
there (hiss, static, or mastering-chain self-noise) that persists even
once the music itself has dropped out counts as background noise. Default
threshold: -35dB, calibrated against this project's own sample of tracks
(measured noise floors ranged -66.4dB to -37.5dB).</li>
<li><b>Mains Hum (HUM).</b> A sustained, narrow tone at the local
power-grid frequency (50Hz or 60Hz), measured specifically inside the same
quiet passage Noise Floor uses — never checked across the whole file,
precisely because an actual musical note at the same pitch would stop
when the rest of the music does, while hum doesn't. Flagged when a
narrowband spike measures 8dB or more above a nearby control frequency.
No dedicated slider of its own — this is a fixed binary detection, not a
tunable threshold.</li>
<li><b>Dynamic Range / DR14 (DYN, "Compression Tolerance").</b> The
Pleasurize Music Foundation "TT DR Meter" algorithm: the top 20% loudest
of non-overlapping 3-second blocks, compared against the second-highest
peak. Grounded in that meter's own documented color bands (red below DR8,
green at DR14+). Unlike the other checks, this isn't pass/fail. It's a
continuous gradient that shifts the score smoothly as compression gets
heavier, and its one slider shifts every band together rather than
exposing each boundary separately.</li>
</ul>
<p><b>Why some checks count more than others:</b> Clipping, Spectral
Cutoff, Out-of-Phase, and DR14 get full weight in the composite score.
They're direct, high-confidence measurements of the decoded signal. True
Peak and Fake Hi-Res get reduced weight, since practical readings for
both tend to sit close to their own meters' documented uncertainty
margins. Noise Floor and Mains Hum get medium weight, since an
elevated reading can be a genuine defect but can't always be told apart
with full certainty from legitimate content such as room tone, a reverb
tail, or a sustained musical drone at the same pitch.</p>

<h2>Informational only (shown as notes, never affect the tier)</h2>
<ul>
<li><b>Mono content in a stereo container.</b> Left and right channels are
identical. That's not a defect, just a note that no unique stereo
information actually exists.</li>
<li><b>Bandwidth / Stereo Coherence.</b> Plain-language notes describing
how much high-frequency content is present and how correlated the
left/right channels are. Both are cause-agnostic (a naturally
treble-light acoustic recording or an intentionally wide mix reads
identically to an actual defect), so Spectral Cutoff and Out-of-Phase remain
the actual gates; these are just supporting context.</li>
<li><b>Live-recording context.</b> When Picard's own matched release type
(or the track title) says "Live", the Spectral Cutoff, Noise Floor, and
Stereo Coherence notes add a caveat: PA/broadcast roll-off, audience/room
noise, and a wider room-mic'd image are all expected on live material,
not necessarily damage or a lossy transcode.</li>
</ul>
"""

_DETAILS_HTML = """
<h2>Opening it</h2>
<p>Right-click a matched track and choose <b>File Health Details...</b> for
just that selection, or use the Tools menu's <b>Show File Health Details
for All Files</b> to see everything currently loaded in Picard at once.</p>

<h2>Layout</h2>
<p>One row per file, grouped by the track Picard has matched it to, so
duplicate copies of the same recording line up side by side for direct
comparison. Files with no match yet appear as their own singleton
group.</p>
<table cellpadding="4" border="1" style="border-collapse:collapse;">
<tr><th>Column</th><th>Shows</th></tr>
<tr><td>File / File Tier / Track Tier</td><td>Filename and both tiers'
current verdicts (or "Not yet scanned").</td></tr>
<tr><td>Format</td><td>Codec, bitrate, sample rate, and channel count.</td></tr>
<tr><td>CLP / TPK / TRB / PHS / HRS / HUM / NSF</td><td>One stoplight per
perceptual check. Most tags match the sensitivity slider that controls
them on the Options page; HRS shares TRB's slider and HUM has no slider
at all (see Checks).</td></tr>
<tr><td>DYN</td><td>Dynamic Range band (Poor/Ok/Good/Great/Excellent,
DR14's own rating scale).</td></tr>
<tr><td>Notes</td><td>Every itemized File Health and Track Health issue
(the same text the tree column's own tooltip shows — decode/corruption/
tag/artwork problems, fired perceptual checks), plus informational notes
that don't affect a tier (bitrate grading, bandwidth, stereo coherence,
live-recording caveats, stale-scan warnings).</td></tr>
</table>
<p><b>"Changed since last scan"</b> in Notes means the file's bytes are
different from what they were the last time that scan ran &mdash; including
after re-saving the file in Picard just to clear an OK-capped tag or
artwork issue (see the Scans tab). That re-save is itself a real change to
the file's bytes, so it triggers the same indicator as any other edit;
rescan to confirm the fix and clear the warning.</p>
<h2>Stoplight colors</h2>
<ul>
<li>&#9989; <b>Green</b>: comfortably clear of the threshold.</li>
<li>&#9888;&#65039; <b>Amber</b>: passes right now, but one slider
notch stricter would flag this file. The amber band always moves together
with the slider, rather than sitting at a fixed offset.</li>
<li>&#10060; <b>Red</b>: fails; past the threshold.</li>
<li>&#10134; <b>Gray</b>: not applicable (for example, a mono file
has no Out-of-Phase reading) or not yet measured.</li>
</ul>
<p>Hover any stoplight cell for the exact measured value and a
plain-language explanation of what it means for the audio.</p>

<h2>Buttons</h2>
<ul>
<li><b>Scan File Health / Scan Track Health</b>: scans every file
currently listed in the window.</li>
<li><b>Show in List</b>: selects and scrolls to the selected file in
Picard's own main window (useful once files are matched and the main list
shows track titles instead of filenames).</li>
<li><b>Spectrogram...</b>: renders a visual spectrogram for the
selected file. See the next tab.</li>
<li><b>Remove from Picard</b> / <b>Move to Trash...</b>: act on the
selected file directly, without needing to find it in the main window
first.</li>
</ul>
<p>The window stays open and redraws in place as scans finish; there's no
need to close and reopen it to see fresh results.</p>
"""

_SPECTROGRAM_HTML = """
<h2>What it is</h2>
<p>The <b>Spectrogram...</b> button in the Details window renders a
picture of the same audio the numeric checks already measured, a way to
visually double-check a flagged (or clean) file rather than trusting a
single number alone.</p>

<h2>Reading the axes</h2>
<ul>
<li><b>Horizontal axis: time.</b> Left edge is the start of the track,
right edge is the end.</li>
<li><b>Vertical axis: frequency.</b> Low frequencies (bass) at the bottom,
rising to high frequencies (treble) at the top.</li>
<li><b>Color/brightness: loudness.</b> Bright/warm at a point means that
frequency was loud at that moment; dark means quiet or silent.</li>
<li>Stereo files render as two stacked bands, one per channel, so
left/right differences are visible directly in the same image.</li>
</ul>

<h2>What to look for</h2>
<ul>
<li><b>A flat, sharply-cut dark band across the top of the whole image,
unchanging over time</b>: a spectral cutoff wall. This is the classic
visual signature of a prior lossy transcode, or an upsampled "fake
hi-res" file, and exactly what the Spectral Cutoff / Fake Hi-Res checks
measure numerically.</li>
<li><b>A persistent haze or glow along the bottom during otherwise-quiet
passages</b>, instead of clean black: an elevated noise floor
(hiss, static, self-noise). This is what the Noise Floor check
measures.</li>
<li><b>A thin, bright vertical stripe spanning many frequencies at one
instant</b>: a broadband click or pop, a likely sign of localized
damage (bitrot, a bad edit, a transfer glitch). A legitimate percussive
hit (a cymbal crash, a snare) can look similar at a glance; a genuine defect
is usually much shorter than an actual drum hit and looks disconnected
from the surrounding music rather than part of its natural attack.
Zooming in, or comparing the same instant against a known-clean copy,
helps tell the two apart.</li>
<li><b>One channel's band consistently duller or narrower than the
other's</b>: a channel-specific problem, whether a bad transfer, a
damaged mic or cable at recording time, or a mono source improperly
widened.</li>
<li><b>No sharp cutoff, an even noise floor, and roughly matching
left/right bands</b>: nothing visually wrong, matching what a clean
numeric scorecard would already say.</li>
</ul>

<h2>What it isn't for</h2>
<p>This is a gut-check on the same measurements the checks already made;
it doesn't replace them. Some things, including a precise True Peak
reading, the exact DR14 rating, and the exact Out-of-Phase angle, aren't
reliably eyeballed from a picture. Use the Details window's own numbers
and tooltips for anything you need to act on with confidence; use the
spectrogram to sanity-check what those numbers are telling you.</p>
"""
# @spec UI-HELP-001, UI-HELP-002

SECTIONS: list[tuple[str, str]] = [
    ("How the Scans Work", _SCANS_HTML),
    ("Checks and Why", _CHECKS_HTML),
    ("The Details Window", _DETAILS_HTML),
    ("Reading a Spectrogram", _SPECTROGRAM_HTML),
]
