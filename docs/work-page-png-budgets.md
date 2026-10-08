# v2 Work Page PNG resource budgets (Issue #336)

The Work-backed `POST /manga_autopilot/api/v2/works/{work_id}/pages/{page_id}/export/png`
accepts saved Work/Page IDs plus optional render settings: `background`,
`outer_border`, and `export_profile`. It **never** accepts browser-supplied
page geometry, input image file paths, output limits or an arbitrary budget.

## Named, server-enforced profiles

| `export_profile` | Page total pixels | Single candidate pixels | Aggregate candidate pixels | Encoded PNG max |
|---|---:|---:|---:|---:|
| `screen` (default) | 12,000,000 | 16,000,000 | 32,000,000 | 48 MiB |
| `print` (explicit opt-in) | 24,000,000 | 24,000,000 | 48,000,000 | 96 MiB |

Additionally, each saved Page dimension must be an integer from 1 to 16,384.
The combined pixel area is checked **before** the Pillow renderer is invoked,
and selected candidate metadata is checked against the profile before Pillow
decodes any panel input. Generated PNG file size is checked before Work Artifact
publication. The `print` profile is selected explicitly in Export Center;
the `screen` default is backwards compatible. Both named budgets are hard
server limits, not browser-adjustable knobs.

Exceeding a profile's limits results in HTTP 422
`export_precondition_failed`, with Work/Page context and an actionable
suggestion. Concurrent Work PNG renders within a Python worker are limited to
one, returning HTTP 429 `export_busy` with `Retry-After: 2` to other requests.
The HTTP handler offloads this synchronous CPU/file-intensive service to a
worker via `asyncio.to_thread()`. Thus a long Pillow render does **not**
block aiohttp's event loop: Page Editor and Export Center reads are still
served and a second concurrent HTTP POST can promptly return `429 export_busy`.
The rendering semaphore remains owned and released only by the service worker.

**Request cancellation (#349):** cancellation signals a `threading.Event`
to the active worker, then the HTTP coroutine waits for its cleanup before
propagating cancellation. Pillow itself is not forcibly interrupted; once the
current render/encode step exits, the export checks cancellation and refuses
publication. Another cancellation check runs inside the final
`BEGIN IMMEDIATE` commit-time source guard, before a READY Artifact row or
Work commit is created. The disposable render directory is removed and the
worker, not the cancelled waiter, releases the semaphore. A cancellation
arriving after the final accepted commit cannot undo that committed Artifact;
the durable Work record is authoritative. If cancellation occurs after
exclusive file publication but before the Work commit, the already-published
orphan file may remain for normal recovery, but no unverified READY row is
committed. Client TCP disconnection is not universally equivalent to aiohttp
task cancellation; the lifecycle check applies when the server cancels the
handler task.

**Multiple independent server processes must be provisioned with a separate
deployment-wide capacity limit**: the per-process semaphore cannot bound an
arbitrary number of external workers or hosts.

For `GET /manga_autopilot/api/v2/works/{work_id}/exports/{artifact_id}/png`,
the server pins the file descriptor, checks that it is a regular file and
incrementally copies a maximum of 96 MiB into a temporary snapshot with a
1 MiB in-memory spool threshold. It verifies exact byte count and SHA-256
*before* sending a response. Only this independently verified snapshot is
streamed to the client in 256 KiB chunks. A changed/replaced source path
cannot affect emitted bytes after verification. Two simultaneous download
spools are permitted per worker; additional requests get HTTP 429
`download_busy` with `Retry-After: 2`. This bounds application memory per
request and controls concurrent temporary-disk consumption; free disk capacity
must still be provisioned.

**HTTP cancellation during downloads (#353):** each slot is held until *all*
file-copy or streamed-file-read workers belonging to the request have actually
finished, even if aiohttp cancels its coroutine. `asyncio.to_thread()` does
not terminate a running thread; the handler awaits shielded workers through
cancellation and releases the spool semaphore only after they drain.
A verified `SpooledTemporaryFile` returned after cancellation is explicitly
closed even when no HTTP response consumes it. If cancellation occurs during
a streaming `snapshot.read()`, the handler completes that read before closing
the snapshot, never concurrently closing a file another thread is reading.
Repeated cancellation cannot create extra copy workers or release capacity
early. The copy is not forcibly interrupted (it may finish after the client
disconnects), and a TCP disconnect is not guaranteed to cancel the aiohttp
handler; the budget still bounds its work. Existing point-in-time freshness,
hash-verified snapshot, and immutable historical PNG semantics are unchanged.

These ceilings protect the **v2 Work PNG path**, not every legacy renderer,
remote executor or other unrelated image-generating route. GPU/real ComfyUI
resource limits are separate responsibilities.
