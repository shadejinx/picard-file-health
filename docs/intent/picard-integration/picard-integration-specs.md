# Picard Integration — EARS Specs

## Columns

- [x] **UI-COL-001**: The system shall display File Health and Track Health as sortable, filterable tree columns rendered as a tier icon with an itemized-issues tooltip, not as free text.
- [x] **UI-COL-002**: When the plugin enables, the system shall, in one synchronous pass, rebuild every already-open tree view's header, wire the icon-painting delegate onto it, and make both Health columns visible — so the columns appear correctly without a Picard restart and without depending on the column's post-rebuild default visibility.
- [x] **UI-COL-003**: The system shall left-align the tier status icon within its column cell to match the column header's own left-aligned label.
- [x] **UI-COL-004**: The system shall HTML-escape every issue and note string before building the itemized-issues tooltip, since an issue's text may originate from a crafted tag's raw exception text and the tooltip renders as HTML.
- [x] **UI-COL-005**: The system shall map the Track Health tier `Unplayable` to the same icon level and worst-of-all sort rank that the File Health column uses for `Unplayable`, so a Track Health scan that fails to decode is visually and sort-order indistinguishable in severity from a File Health decode failure.

## Actions

- [x] **UI-ACTION-001**: The system shall provide a File Health scan action available on unmatched files, clusters, and matched tracks.
- [x] **UI-ACTION-002**: The system shall provide a Track Health scan action reachable by right-clicking a Track node or a linked File node, that scans only files matched to a track, silently skipping any unmatched file present in the same selection.
- [x] **UI-ACTION-003**: The system shall run every scan on a background thread, never blocking the UI thread.
- [x] **UI-ACTION-004**: While a file's scan is in progress, the system shall mark that file pending.
- [x] **UI-ACTION-005**: When a scan completes with an unhandled error, the system shall clear the file's pending state and display a status-bar message naming the failure.
- [x] **UI-ACTION-006**: If a File Health or Track Health scan completes after its file has been removed from every visible tree, then the system shall not raise an error from the completion callback.
- [x] **UI-ACTION-007**: The system shall make the File Health Details window reachable by right-clicking a Track node or a linked File node, not only via the Tools-menu "scan everything" action.

## Options Page

- [x] **UI-OPTIONS-001**: The system shall let the user configure the ffmpeg binary path, validating it before accepting it.
- [x] **UI-OPTIONS-002**: The system shall expose the six Track Health sensitivity sliders (Clipping, True Peak, Spectral Cutoff, Out-of-Phase Channels, Compression Tolerance, Noise Floor) on the Options page.

## Details Window

- [x] **UI-DETAILS-001**: The system shall recompute each Details-window matrix cell from stored raw measurements against the currently configured slider settings, not the tier recorded at scan time.
- [x] **UI-DETAILS-002**: While a Details-window group's files are tied on both File Health and Track Health tier, the system shall visually distinguish the healthiest copy only when the ranking is unambiguous.
- [x] **UI-DETAILS-003**: The system shall include a Details-window matrix column for every check that contributes to the Track Health composite score.
- [x] **UI-DETAILS-004**: The system shall include every File Health and Track Health itemized issue in the Details window's Notes column, not only informational notes that don't affect a tier.

## Spectrogram Viewer

- [x] **UI-SPECTRO-001**: The system shall render a spectrogram only on explicit user request, never as part of a routine scan.
- [x] **UI-SPECTRO-002**: The system shall delete the temporary spectrogram image file on every exit path, including an unexpected exception during rendering.

## Help Dialog

- [x] **UI-HELP-001**: The system shall build the Help dialog's content entirely from static text, never interpolating a filename or tag value into it.
- [x] **UI-HELP-002**: The system shall document, in the Help dialog, that re-saving a file to clear a File Health OK-capped issue (a tag or artwork defect Picard's own re-save fixes) triggers the same "changed since scan" indicator as any other change to the file.

## Metadata Persistence

- [x] **UI-META-001**: The system shall persist scan results as Picard file metadata fields so they survive a Picard restart.
- [x] **UI-META-002**: The system shall compute a file's content hash from its raw bytes to detect whether the file changed since its last scan.

## Security Posture

- [x] **UI-SEC-001**: The system shall never open a network connection.
- [x] **UI-SEC-002**: The system shall document, in the README, the local resources the plugin accesses (subprocess execution of ffmpeg/ffprobe, a temporary spectrogram file, file metadata writes) and that it makes no network connections — so a user evaluating a Community- or Unregistered-trust install can assess it before installing.
