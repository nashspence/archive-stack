# Upload transport

The Riverhog CLI sends bounded resumable chunks to tusd. Defaults favor predictable retry cost and proxy compatibility:

```text
RIVERHOG_UPLOAD_CHUNK_BYTES=8388608
RIVERHOG_UPLOAD_FILE_CONCURRENCY=1
RIVERHOG_UPLOAD_FILE_LOG_BYTES=1048576
RIVERHOG_UPLOAD_TIMEOUT_SECONDS=300
RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS=5
RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS=0
```

Each file has one upload resource. After a timeout or transient HTTP failure, the CLI re-reads the authoritative server offset before sending more bytes. One CLI invocation reuses its HTTP connection pool; optional file concurrency creates independent file workers.

The tusd pre-create hook receives a deterministic staging id. Upload chunks land in shared staging storage. Riverhog later verifies each logical file, streams it into the encrypted collection archive, and publishes verified bytes into hot storage.

## Proxy sizing

Set the maximum request body above `RIVERHOG_UPLOAD_CHUNK_BYTES` and keep request and upstream timeouts above the CLI chunk timeout. With the default 8 MiB chunk, a 16 MiB body limit is appropriate.

```nginx
client_max_body_size 16m;
client_body_timeout 330s;
send_timeout 330s;
proxy_request_buffering on;
proxy_read_timeout 330s;
proxy_send_timeout 330s;
```

Raise the proxy body limit before increasing the CLI chunk size. Diagnose stalls with CLI offsets, proxy access logs, API logs, and tusd state; socket write completion alone does not prove server receipt.
