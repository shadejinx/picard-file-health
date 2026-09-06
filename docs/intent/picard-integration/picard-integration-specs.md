# Picard Integration — EARS Specs

## Columns

- [x] **UI-COL-001**: The system shall display File Health and Track Health as sortable, filterable tree columns rendered as a tier icon with an itemized-issues tooltip, not as free text.
- [x] **UI-COL-002**: When the plugin enables after Picard's own tree views already exist, the system shall retrigger a header rebuild on those views at staggered delays so the new columns become visible without requiring a Picard restart.
- [x] **UI-COL-003**: The system shall left-align the tier status icon within its column cell to match the column header's own left-aligned label.
- [x] **UI-COL-004**: The system shall HTML-escape every issue and note string before building the itemized-issues tooltip, since an issue's text may originate from a crafted tag's raw exception text and the tooltip renders as HTML.

## Actions

- [x] **UI-ACTION-001**: The system shall provide a File Health scan action available on unmatched files, clusters, and matched tracks.
- [x] **UI-ACTION-002**: The system shall provide a Track Health scan action available only on matched tracks.
- [x] **UI-ACTION-003**: The system shall run every scan on a background thread, never blocking the UI thread.
- [x] **UI-ACTION-004**: While a file's scan is in progress, the system shall mark that file pending.
- [x] **UI-ACTION-005**: When a scan completes with an unhandled error, the system shall clear the file's pending state and display a status-bar message naming the failure.
- [x] **UI-ACTION-006**: If a File Health or Track Health scan completes after its file has been removed from every visible tree, then the system shall not raise an error from the completion callback.

## Options Page

- [x] **UI-OPTIONS-001**: The system shall let the user configure the ffmpeg binary path, validating it before accepting it.
- [x] **UI-OPTIONS-002**: The system shall expose the six Track Health sensitivity sliders (Clipping, True Peak, Spectral Cutoff, Out-of-Phase Channels, Compression Tolerance, Noise Floor) on the Options page.

## Details Window

- [x] **UI-DETAILS-001**: The system shall recompute each Details-window matrix cell from stored raw measurements against the currently configured slider settings, not the tier recorded at scan time.
- [x] **UI-DETAILS-002**: While a Details-window group's files are tied on both File Health and Track Health tier, the system shall visually distinguish the healthiest copy only when the ranking is unambiguous.

## Spectrogram Viewer

- [x] **UI-SPECTRO-001**: The system shall render a spectrogram only on explicit user request, never as part of a routine scan.
- [x] **UI-SPECTRO-002**: The system shall delete the temporary spectrogram image file on every exit path, including an unexpected exception during rendering.

## Help Dialog

- [x] **UI-HELP-001**: The system shall build the Help dialog's content entirely from static text, never interpolating a filename or tag value into it.
- [x] **UI-HELP-002**: The system shall document, in the Help dialog, that re-saving a file to clear a File Health OK-capped issue (a tag or artwork defect Picard's own re-save fixes) triggers the same "changed since scan" indicator as any other change to the file.

## Metadata Persistence

- [x] **UI-META-001**: The system shall persist scan results as Picard file metadata fields so they survive a Picard restart.
- [x] **UI-META-002**: The system shall compute a file's content hash from its raw bytes to detect whether the file changed since its last scan.
