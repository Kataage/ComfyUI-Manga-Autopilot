# Managed Storage Path Portability

Manga Autopilot validates user-controlled managed IDs before using them as
filesystem path components. The rule is platform-independent: a Work created on
Linux or macOS must remain usable if its directory is later moved to Windows.

The shared `_safe_path_component()` rule currently applies to Work IDs, legacy
project IDs, and legacy run IDs.

## Windows reserved device names

The following device-name stems are rejected case-insensitively, including when
an extension follows the stem:

- `CON`, `PRN`, `AUX`, `NUL`
- `COM1` through `COM9`
- `LPT1` through `LPT9`
- `COM¹`, `COM²`, `COM³`
- `LPT¹`, `LPT²`, `LPT³`

The superscript digits are U+00B9, U+00B2, and U+00B3. Examples such as
`con.txt`, `COM¹.txt`, and `LPT².log` are therefore also rejected.

Managed IDs additionally reject path separators, absolute/relative traversal
segments, NUL/control characters, Windows-invalid filename characters, and
trailing dots or spaces. Legitimate Unicode names that do not violate those
rules remain valid.
