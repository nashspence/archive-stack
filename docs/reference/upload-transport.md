# Upload Transport Reference

Riverhog uploads use bounded resumable request chunks, controlled by
`RIVERHOG_UPLOAD_CHUNK_BYTES`. The default request chunk is 8 MiB, and the
current CLI sends each chunk as one PATCH request body through the HTTP client.
The CLI uploads one file at a time by default; operators may raise
`RIVERHOG_UPLOAD_FILE_CONCURRENCY` for collections containing many small files.

## Current Upload Shape

The current upload contract is deliberately small:

- one bounded PATCH body per resumable chunk
- one logical file worker by default
- resumable retry after transient HTTP failures, dropped responses, and timeout
- server-authoritative offset re-check before sending more bytes after an
  interrupted chunk
- one reused HTTP connection pool per CLI invocation

## Validated Defaults

The default upload profile is:

```text
RIVERHOG_UPLOAD_CHUNK_BYTES=8388608
RIVERHOG_UPLOAD_FILE_CONCURRENCY=1
RIVERHOG_UPLOAD_FILE_LOG_BYTES=1048576
RIVERHOG_UPLOAD_TIMEOUT_SECONDS=300
RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS=5
RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS=0
```

This profile keeps retry cost and proxy body size bounded. Larger request chunks
can improve throughput by reducing per-request overhead, but they also increase:

- memory pressure in the CLI and API process
- the size of a chunk that must be retried after a transient failure
- the reverse-proxy `client_max_body_size` required for uploads

File concurrency is orthogonal to request chunk size. A higher
`RIVERHOG_UPLOAD_FILE_CONCURRENCY` opens multiple per-file upload workers in one
CLI process, each with its own API client and resumable file resource. This is
useful for photo and document sets with thousands of small files where
round-trip latency dominates. It does not change the server-side collection id
or per-file resume contract; rerunning the same slug and timestamp still resumes
the same logical collection upload. Increase it gradually and watch CLI retry
logs plus server load.

The default per-file log threshold keeps tiny-file uploads readable: files below
`RIVERHOG_UPLOAD_FILE_LOG_BYTES` are represented by throttled total progress
logs unless they hit a retry or error. Set the threshold to `0` when debugging a
specific small-file path.

The CLI treats transient transport failures and HTTP 408/425/429/5xx responses
as resumable. File upload session creation/resume checks retry indefinitely with
capped backoff and re-check the authoritative server offset before sending more
bytes. This is intentional: app restarts, proxy reloads, and brief network
outages should not force the operator to restart a long upload.

Riverhog passes tusd a deterministic staging file id through its pre-create
hook. The checked-in deployment uses tusd filesystem storage on a shared local
mount, so upload chunks land on disk first instead of passing through Garage.
Riverhog later streams those staged files into the encrypted Glacier archive and
then into committed hot storage for planning. The target path metadata sent to
tusd is itself base64 text, not the raw path, so upload metadata remains safe
for paths with literal spaces or other punctuation.

Do not benchmark more aggressive request chunks after a failed or aborted bulk
upload unless local stale sockets have cleared first; orphaned `FIN_WAIT_1`
sockets with queued send data can make an otherwise stable profile look broken.

After the final file chunk is accepted, the default `--wait finalized` mode
remains attached until Riverhog uploads and verifies the collection-native
Glacier archive package. Use `--wait staged` when the CLI should exit once
server custody has begun and background archival continues through operator
notifications and the API. `RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS=0` means
no CLI-side deadline for that finalized handoff.

## Proxy Guidance

Reverse proxies should allow upload request bodies larger than
`RIVERHOG_UPLOAD_CHUNK_BYTES`. A production proxy with the default 8 MiB request
chunk should set `client_max_body_size` to at least 16 MiB.

For nginx/SWAG deployments:

- keep request buffering enabled so the API receives complete bounded chunks
- keep request-body and proxy timeouts slightly above the CLI per-chunk timeout
- do not leave hour-long request-body timeouts for a bounded 8 MiB chunk path
- keep proxy read timeouts long enough for ISO download preparation; xorriso can
  spend more than a minute building metadata for images with many small files
  before the first response byte is available
- preserve HTTP/2 support, but expect HTTP/1.1 to work with the same bounded
  request bodies too

Example values:

```nginx
client_max_body_size 16m;
client_body_timeout 330s;
send_timeout 330s;

proxy_request_buffering on;
proxy_read_timeout 1h;
proxy_send_timeout 330s;
```

Operators who raise `RIVERHOG_UPLOAD_CHUNK_BYTES` must raise the proxy body
limit first.

## Diagnosing Upload Stalls

Do not rely on curl `size_upload` alone. That metric can mean "bytes accepted by
the local socket", not "bytes received by nginx or Riverhog".

Use several layers together:

- Riverhog CLI logs for the path, offset, retry, and recovered server offset
- nginx access logs to confirm whether the `PATCH` reached the proxy
- Riverhog app logs to confirm whether the API handled the `PATCH`
- server-side packet capture when proxy and app logs disagree
- local socket state when aborted uploads leave queued `FIN_WAIT_1` connections

A stalled upload where nginx has no `PATCH` access-log entry is below the HTTP
application layer. Investigate the network path, local socket state, request
chunk size, and timeout profile before changing Riverhog API logic.

## Debug Findings

The upload transport issue that shaped the current conservative chunk and
timeout defaults was reproduced and narrowed as follows:

- diagnostic paced httpx uploads succeeded through the public SWAG endpoint over
  both HTTP/1.1 and HTTP/2
- server-local curl through the same SWAG config succeeded with and without
  `Expect: 100-continue`
- unpaced curl from the Mac to the server could time out before nginx logged a
  `PATCH`
- raw Python socket transfers from the Mac to the server reproduced the same
  stall without HTTP, TLS, nginx, FastAPI, or tusd in the path
- the reverse server-to-Mac direction was healthy
- disabling macOS TCP segmentation offload did not fix the unpaced failure
- ten consecutive raw 64 MiB transfers succeeded with 64 KiB writes and a 0.01
  second delay
- Riverhog soak tests with 8 MiB request chunks and diagnostic 64 KiB paced
  writes completed 20 chunks over HTTP/1.1 and 20 chunks over HTTP/2 with exact
  offset progression
- later real-upload and synthetic probes exposed that previous failed attempts
  had left 73 orphaned local sockets to the SWAG endpoint, with about 103 MB of
  queued unsent data; those stale sockets survived Wi-Fi and interface toggles
  and required a reboot to clear
- after rebooting into a clean local TCP state, the same HTTP/2 path completed
  repeated 8 MiB chunk soak tests without Riverhog changing server-side upload
  semantics

This points to aggressive client-to-server bulk writes on the local network path,
not an nginx/SWAG HTTP body-size limitation and not a Riverhog/tusd partial-chunk
acceptance bug.
