# Upload Transport Reference

Riverhog uploads use two separate byte granularities:

- the resumable request chunk, controlled by `RIVERHOG_UPLOAD_CHUNK_BYTES`
- the client socket write slice, controlled by `RIVERHOG_UPLOAD_WRITE_CHUNK_BYTES`

The default request chunk is 8 MiB. The default socket write pacing is 256 KiB
sub-writes with a 0.005 second delay between sub-writes.

## Why Riverhog Paces Upload Writes

The CLI must not hand a whole upload request body to the local TCP stack as fast
as possible. On some LAN and Wi-Fi paths, aggressive client-side writes can stall
or lose progress below HTTP before the reverse proxy has received a complete
request body. In that failure mode:

- nginx/SWAG may not log a `PATCH` because no complete HTTP request body reached
  the proxy
- the Riverhog app and tusd never see the chunk
- curl may report `size_upload` equal to the body size even though those bytes
  were only accepted by the local client socket, not by the server
- the client can leave long-lived `FIN_WAIT_1` sockets with queued unsent data

Paced writes let the network path and reverse proxy apply normal backpressure.
Riverhog treats this pacing as part of the client upload contract, not as a
debug workaround.

## Validated Defaults

The default upload profile is:

```text
RIVERHOG_UPLOAD_CHUNK_BYTES=8388608
RIVERHOG_UPLOAD_WRITE_CHUNK_BYTES=262144
RIVERHOG_UPLOAD_WRITE_DELAY_SECONDS=0.005
RIVERHOG_UPLOAD_TIMEOUT_SECONDS=60
```

This profile keeps retry cost and proxy body size bounded while avoiding the
observed low-level stall. Larger request chunks can improve throughput by
reducing per-request overhead, but they also increase:

- memory pressure in the CLI and API process
- the size of a chunk that must be retried after a transient failure
- the reverse-proxy `client_max_body_size` required for uploads

Larger socket write slices or shorter write delays are riskier than larger
request chunks. Do not benchmark more aggressive values after a failed or
aborted bulk upload unless local stale sockets have cleared first; orphaned
`FIN_WAIT_1` sockets with queued send data can make an otherwise stable profile
look broken.

## Proxy Guidance

Reverse proxies should allow upload request bodies larger than
`RIVERHOG_UPLOAD_CHUNK_BYTES`. A production proxy with the default 8 MiB request
chunk should set `client_max_body_size` to at least 16 MiB.

For nginx/SWAG deployments:

- keep request buffering enabled so the API receives complete bounded chunks
- keep request-body and proxy timeouts slightly above the CLI per-chunk timeout
- do not leave hour-long request-body timeouts for a bounded 8 MiB chunk path
- preserve HTTP/2 support, but expect HTTP/1.1 to work with paced writes too

Example values:

```nginx
client_max_body_size 16m;
client_body_timeout 75s;
send_timeout 75s;

proxy_request_buffering on;
proxy_read_timeout 75s;
proxy_send_timeout 75s;
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
application layer. Tune client write pacing or the network path before changing
Riverhog API logic.

## Debug Findings

The upload transport issue that drove these defaults was reproduced and narrowed
as follows:

- paced httpx uploads succeeded through the public SWAG endpoint over both
  HTTP/1.1 and HTTP/2
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
- Riverhog soak tests with 8 MiB request chunks and 64 KiB paced writes completed
  20 chunks over HTTP/1.1 and 20 chunks over HTTP/2 with exact offset progression
- later real-upload and synthetic probes exposed that previous failed attempts
  had left 73 orphaned local sockets to the SWAG endpoint, with about 103 MB of
  queued unsent data; those stale sockets survived Wi-Fi and interface toggles
  and required a reboot to clear
- after rebooting into a clean local TCP state, the same HTTP/2 path completed a
  100-chunk 8 MiB soak with the conservative 64 KiB/0.01s pacing profile
- clean-state tuning then completed 100-chunk and 200-chunk soaks with
  256 KiB/0.005s socket pacing, averaging about 10 MiB/s with no retries and no
  stale local sockets afterward

This points to aggressive client-to-server bulk writes on the local network path,
not an nginx/SWAG HTTP body-size limitation and not a Riverhog/tusd partial-chunk
acceptance bug.
