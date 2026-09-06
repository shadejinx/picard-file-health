# File Health — Quickstart

You've installed the plugin (see [README.md](README.md) if you haven't). This guide walks you through everything from opening Picard for the first time afterward to reading your first spectrogram.

Everything here is also available in-app: **Options → Plugins → File Health → Help**. That dialog is the detailed reference (every check explained, every threshold's rationale); this guide is the "what do I actually click" version. Where it's useful, this guide points you at the matching Help tab by name.

## 1. Confirm ffmpeg is found

File Health needs [ffmpeg](https://ffmpeg.org/) to do any actual analysis. It doesn't decode audio itself.

1. Open **Options → Plugins → File Health**.
2. Look at the **ffmpeg Location** section. If ffmpeg is already on your system `PATH`, you'll see a green **Found: ... (ffmpeg X.Y — OK)** status without doing anything.
3. If it's not found:
   - Click **Detect Automatically** to have the plugin search common install locations.
   - Or click **Browse…** and point it directly at your `ffmpeg` binary.
   - No ffmpeg installed yet? Click **Get ffmpeg…** to open the download page.
4. Leave the ffmpeg path field blank if `PATH` detection works. You only need to set it explicitly if ffmpeg lives somewhere non-standard.

You can't get a File Health or Track Health result without this step working. If a scan fails later, this is the first thing to check.

## 2. Decide whether to auto-scan new files

Still on the Options page, at the top:

- **"Automatically scan File Health for newly added files"**: check this if you want every file you add to Picard to get a structural scan automatically, without clicking anything. This only applies to File Health (the fast, structural-only check). **Track Health always requires a manual scan**, since it's a heavier decode you probably don't want running on every file you add.

Leave it unchecked if you'd rather scan deliberately, in bulk, once you've got a batch of files loaded.

## 3. Run your first scan

With some files loaded in Picard:

- **File Health**: right-click any file, cluster, or track (matched or not) and choose **Scan File Health…**. This is the only scan available on unmatched files.
- **Track Health**: right-click a *matched* track and choose **Scan Track Health…**. (Track Health needs a real decode-and-measure pass, so it's only offered where Picard already has a track context.)

Either way, the file's icon briefly shows a pending state while the scan runs in the background. Picard stays fully responsive.

Once it finishes, look at the two new columns in your file list: **File Health** and **Track Health**. Each shows a colored icon representing that file's tier. Hover over either icon to see a tooltip listing the exact issues found (or confirming there weren't any).

Not sure what a tier or an issue means? Open **Options → Plugins → File Health → Help → How the Scans Work** for the full tier breakdown, or **Checks and Why** for what each individual check measures and why it's calibrated the way it is.

## 4. Tune the sensitivity sliders

Track Health's six checks each have a sensitivity slider, in **Options → Plugins → File Health**, under **Track Health Sensitivity**:

| Slider | What it controls |
|---|---|
| **Clipping** | How much flat/clipped-sample evidence counts as real clipping |
| **True Peak** | How far past 0dBTP a reconstructed peak has to go to count as an over |
| **Spectral Cutoff** | How sharp a high-frequency rolloff counts as a lossy-transcode signature |
| **Out-of-Phase Channels** | How close to fully inverted the left/right phase angle has to be |
| **Compression Tolerance** | Where the DR14 dynamic-range gradient starts penalizing a track |
| **Noise Floor** | How loud the quiet-passage background noise has to be to count |

**Loosen a slider** (toward the lenient end) to reduce false positives. This helps if a check keeps flagging material you already know is fine for its genre or mastering style. **Tighten a slider** to catch more, if you want File Health-grade strictness applied to perceptual quality too.

Moving a slider doesn't retroactively change results already shown; it reshapes the composite score **the next time you scan** that file. If you've been tuning sliders and want to see the effect on files you already scanned, either rescan them or open the Details window (next section), which recomputes every check live against your *current* slider settings regardless of when the file was last scanned.

Changed your mind about a slider? **Reset to Calibrated Defaults** puts all six back to their empirically-calibrated starting points in one click.

## 5. Open the Details window

For a closer look at one or more files (especially useful when comparing duplicates or deciding which copy of a track to keep), right-click a matched track and choose **File Health Details…**, or use the Tools menu's **Show File Health Details for All Files** to see everything currently loaded at once.

The Details window shows, per file:

- Both tiers (File Health, Track Health) and basic format info (codec, bitrate, sample rate, channels).
- One "stoplight" cell per perceptual check, including Mains Hum (which has no slider of its own) — green = comfortably clear, amber = passes but would fail one slider notch stricter, red = fails, gray = not applicable or not yet measured. Hover any cell for the exact measured value.
- A Dynamic Range band (Poor/Ok/Good/Great/Excellent).
- A Notes column with everything informational: bitrate grading, bandwidth, stereo coherence, live-recording context, and "changed since last scan" warnings.

This window stays open and updates live as scans finish, so there's no need to close and reopen it. Full details: **Help → The Details Window**.

## 6. Review a spectrogram

If a number alone doesn't settle it, look at the audio directly:

1. In the Details window, select a file and click **Spectrogram…**.
2. Read the axes: **time** runs left to right, **frequency** runs bottom (bass) to top (treble), and **brightness** shows loudness at that instant. Stereo files show two stacked bands, one per channel.
3. What to look for:
   - A flat, sharp dark band across the top, unchanging over time → a spectral cutoff wall (a prior lossy transcode, or upsampled "fake hi-res").
   - A persistent haze along the bottom during quiet passages → an elevated noise floor.
   - A thin, bright vertical stripe → a likely click, pop, or localized damage (compare against a known-clean copy if you're unsure it's just a percussive hit).
   - One channel duller or narrower than the other → a channel-specific problem.
   - No cutoff, even noise floor, matching left/right bands → nothing visually wrong.

The spectrogram is a sanity check on the numeric measurements, not a replacement for them: some things (an exact True Peak reading, the precise DR14 rating, the exact phase angle) aren't reliably read off a picture. Full details, including more examples of what each visual pattern means: **Help → Reading a Spectrogram**.

## You're set

That's the full loop: scan, read the columns, tune sliders to your taste, cross-check anything ambiguous in the Details window or a spectrogram. For the *why* behind any specific number or threshold, the in-app Help dialog is the authoritative reference. This guide won't try to duplicate it.

Found a bug, or a check that doesn't seem right for your files? [Open an issue](https://github.com/shadejinx/picard-file-health/issues).
