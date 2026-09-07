# Analysis Engine — EARS Specs

## Subprocess Invocation

- [x] **ENGINE-SUBPROC-001**: The system shall invoke every ffmpeg/ffprobe call with an argument list passed directly to the subprocess, never through a shell.
- [x] **ENGINE-SUBPROC-002**: The system shall pass a scanned file's name as the value of an explicit `-i` flag, never as a bare trailing positional argument.
- [x] **ENGINE-SUBPROC-003**: If an ffmpeg/ffprobe subprocess call times out or fails at the OS level, then the system shall return a synthetic non-zero-exit result rather than raising an exception.
- [x] **ENGINE-SUBPROC-004**: Where running on Windows, the system shall suppress the console window a subprocess call would otherwise display.
- [x] **ENGINE-SUBPROC-005**: If ffmpeg's stderr contains non-UTF8 byte sequences (e.g. from a crafted filename or corrupt tag metadata), then the system shall substitute the invalid bytes rather than raising a decode error.

## ffmpeg Version Resolution

- [x] **ENGINE-VERSION-001**: The system shall reject an ffmpeg binary confirmably older than version 3.1.
- [x] **ENGINE-VERSION-002**: If ffmpeg's version string cannot be parsed as a standard numeric release, then the system shall treat the binary as acceptable rather than rejecting it.
- [x] **ENGINE-VERSION-003**: If a user-configured ffmpeg path is not an existing, executable file, then the system shall reject that configuration rather than attempting to invoke it.
- [x] **ENGINE-VERSION-004**: Where no explicit ffmpeg path is configured, the system shall search the operating system's `PATH` for the binary.
- [x] **ENGINE-VERSION-005**: Where no explicit ffmpeg path is configured and the binary is not found via `PATH` search, on Windows the system shall additionally search WinGet's shim directory, WinGet's known ffmpeg package installation directories, and conventional manual-install locations before treating ffmpeg as not found.
- [x] **ENGINE-VERSION-006**: Where the Windows fallback search finds more than one version-suffixed build folder for the same WinGet package, the system shall select the binary from the most recently modified folder.
- [x] **ENGINE-VERSION-007**: Where both known WinGet ffmpeg packages have an installation directory present, the system shall search the `Gyan.FFmpeg` package's directory before the `BtbN.FFmpeg.GPL` package's directory.
- [x] **ENGINE-VERSION-008**: On a non-Windows platform, the system shall not perform the Windows fallback search.
- [x] **ENGINE-VERSION-009**: If an environment variable a Windows fallback search tier depends on is not set, the system shall skip that tier without raising an error.

## Defensive stderr Parsing

- [x] **ENGINE-STDERR-001**: The system shall scope every stderr parser that reads one filter's diagnostic output to only the lines carrying that filter's own tag marker.
- [x] **ENGINE-STDERR-002**: The system shall require a filter's tag marker to appear inside a line's own leading bracket, not merely anywhere in the line, when scoping stderr output (a scanned file's own tag values can otherwise appear in the same stderr stream and be mistaken for genuine filter output).
- [x] **ENGINE-STDERR-003**: The system shall anchor audio-corruption-signature detection to ffmpeg's own MP3 decoder log-line format, not a bare substring match.
- [x] **ENGINE-STDERR-004**: The system shall derive the ebur128 integrated-loudness and true-peak values only from stderr content at or after the last occurrence of that filter's own genuine tag marker.

## Corruption and Structural Checks

- [x] **ENGINE-CORRUPT-001**: When decoding an MP3 file, the system shall count occurrences of the decoder's bit-reservoir corruption diagnostic as an informational signal.
- [x] **ENGINE-CORRUPT-002**: The system shall compare a Xing/Info header's declared audio-stream byte count against the file's actual on-disk audio-stream size to detect truncation or concatenation.
- [x] **ENGINE-CORRUPT-003**: The system shall validate a file's tag structure via a strict, format-detecting parse independent of ffmpeg's own more lenient tag reading.
- [x] **ENGINE-CORRUPT-004**: The system shall report embedded artwork as corrupt when an attached-pic/video stream exists but its first frame cannot be decoded.

## Merged-Decode Architecture

- [x] **ENGINE-MERGE-001**: The system shall run every whole-file Track Health measurement that doesn't depend on another measurement's result through one merged ffmpeg invocation, rather than one invocation per check.

## Dynamic Range (DR14)

- [x] **ENGINE-DR14-001**: The system shall compute a DR14 dynamic-range value from ffmpeg's block-level RMS/Peak statistics using the Pleasurize Music Foundation TT DR Meter formula.
- [x] **ENGINE-DR14-002**: If an audio channel contains no full DR14 analysis block, then the system shall exclude that channel from the DR14 computation.
- [x] **ENGINE-DR14-003**: If no channel contains a full DR14 analysis block, then the system shall report DR14 as not measured rather than a computed value.

## Spectrogram Rendering

- [x] **ENGINE-SPECTRO-001**: The system shall render a spectrogram image only on explicit request, never as part of a routine scan.
- [x] **ENGINE-SPECTRO-002**: The system shall report spectrogram generation as failed when ffmpeg exits non-zero or the expected output file was not created.

## Security Posture

- [x] **ENGINE-SEC-001**: The system shall never evaluate, execute, or deserialize code constructed from file-derived or tag-derived data.
- [x] **ENGINE-SEC-002**: The system shall never open a network connection.
