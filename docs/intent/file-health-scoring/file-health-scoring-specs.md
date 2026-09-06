# File Health Scoring — EARS Specs

## Tier Assignment

- [x] **FH-TIER-001**: When a file cannot be decoded at all, the system shall assign it the Unplayable tier.
- [x] **FH-TIER-002**: While a file has at least one structural issue (an audio corruption signature or a Xing-header size mismatch), the system shall assign it the Bad tier regardless of any other issue present.
- [x] **FH-TIER-003**: While a file has at least one tag/artwork issue (corrupt embedded artwork or malformed tag structure) and no structural issue, the system shall assign it the OK tier.
- [x] **FH-TIER-004**: Where a file has neither a structural nor a tag/artwork issue, the system shall grade its tier from codec and bitrate rather than defaulting to Excellent.
- [x] **FH-TIER-005**: When a clean file (no structural or tag/artwork issue) uses a lossless codec, the system shall assign it the Excellent tier.
- [x] **FH-TIER-006**: When a clean file uses a lossy codec whose bitrate meets or exceeds the published transparency threshold for that codec, the system shall assign it the Good tier.
- [x] **FH-TIER-007**: When a clean file uses a lossy codec whose bitrate is below the published transparency threshold, or whose codec or bitrate is unknown, the system shall assign it the OK tier.

## Issue Categorization

- [x] **FH-ISSUE-001**: The system shall categorize each detected File Health issue as structural or tag/artwork at the point of detection, not by inspecting issue text afterward.
- [x] **FH-ISSUE-002**: The system shall report every detected issue (structural and tag/artwork) in a single itemized list, regardless of which category drove the tier.

## Unplayable Terminal State

- [x] **FH-BROKEN-001**: When a file is assigned the Unplayable tier, the system shall report a specific reason describing why the file could not be decoded.
- [x] **FH-BROKEN-002**: When a file is assigned the Unplayable tier, the system shall still compute its content hash from the file's raw bytes.
