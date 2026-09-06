# File Health

A [MusicBrainz Picard](https://picard.musicbrainz.org/) plugin that scores every track on two independent axes:

- **File Health** — can this file be trusted structurally? Decode success, audio-stream corruption, and tag/artwork parseability. No configuration — a structural defect either exists or it doesn't.
- **Track Health** — how does this file actually sound? A weighted composite of clipping, true peak, spectral cutoff (lossy-transcode detection), fake hi-res upsampling, out-of-phase/mono-duplicated content, dynamic range (DR14), mains hum, and noise floor. Six sensitivity sliders let you tune how strict each check is.

Both scores are calibrated against measurements from real music libraries, not synthetic fixtures alone — see [`docs/high-level-design.md`](docs/high-level-design.md) for the full design rationale and empirical basis behind each threshold.

## What you get

- Two sortable, filterable tree columns (File Health, Track Health) on both file and album views, with an itemized-issues tooltip.
- Right-click scan actions — File Health on any selection, Track Health on matched tracks — plus a Tools-menu action to scan everything currently loaded.
- A non-modal Details window for side-by-side comparison of candidate files, recomputed live against your current slider settings rather than the tier stored at scan time.
- An on-demand spectrogram viewer for visually confirming a measurement.
- An in-app Help dialog explaining every check and which slider controls it.

## Requirements

- Picard 3.0+ (Plugin API `3.0`)
- [ffmpeg](https://ffmpeg.org/) on your `PATH`, or configured explicitly via the plugin's Options page

## Installation

Once published to the [official plugin registry](https://github.com/metabrainz/picard-plugins-registry), install File Health directly from Picard's plugin manager (Options → Plugins).

Until then, install from this repository:

```bash
picard-cli plugins install https://github.com/shadejinx/picard-file-health
```

Or, for local development:

```bash
picard-cli plugins install /path/to/picard-file-health
```

## Configuration

Open **Options → Plugins → File Health** to:

- Set an explicit ffmpeg path (validated before it's accepted).
- Enable automatic File Health scanning when a file loads.
- Tune the six Track Health sensitivity sliders.

## Development

Validate the plugin manifest:

```bash
picard-cli plugins validate .
```

Run the test suite (requires `pytest`, `mutagen`, and `PyQt6`, plus a
[Picard v3 checkout](https://github.com/metabrainz/picard) on `PYTHONPATH` —
`tests/test_picard_integration.py` imports `__init__.py` directly, which in
turn imports real `picard.*` modules):

```bash
PYTHONPATH=/path/to/picard python3 -m pytest tests/
```

This project follows [Linked-Intent Development](docs/high-level-design.md) — design docs, requirements, and their tests live under `docs/` and `tests/`, tracing from intent through to code.

## Reporting bugs

<https://github.com/shadejinx/picard-file-health/issues>

## License

[GPL-2.0-or-later](LICENSE)
