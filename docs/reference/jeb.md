# Jeb

`jeb` is a generic watched-drop collector for directories fed by FTP, SFTP, SMB,
phone-sync tools, or similar sources. It watches configured source directories,
creates durable batches when thresholds are met, and hands each batch to either
Munchy or Riverhog.

`jeb` is generic public code. Real source names, users, passwords, hostnames,
webhook URLs, and deployment overlays belong in private configuration.

## Behavior

- Each source has exactly one active batch at a time.
- Files must be stable before they are eligible.
- A source crosses threshold by total eligible bytes, or by `max_age` when set.
- Root files can be mapped into a profile group with `root_group`.
- Batch state is stored in SQLite with WAL enabled.
- Service restarts resume from the current batch state.
- Transient target and network errors retry forever without operator webhook noise.
- Unrecoverable errors move the batch to `failed_notified` and send a critical
  operator webhook.
- `failed_notified` batches remain active and send a paced critical reminder,
  defaulting to once per day, until the operator resolves them.
- Source files are only removed from the batch staging directory after the target
  has reported success and the source config uses `cleanup = "after_target_success"`.

## Targets

`type = "munchy"` submits profile-group shaped paths to a Munchy runner. When
cleanup is enabled, `wait_for_safe_delete` must remain true so `jeb` waits for
Munchy to report that downstream Riverhog custody is safe.

`type = "riverhog"` uploads files directly through Riverhog collection-upload
sessions. When cleanup is enabled, `wait = "finalized"` is required.

## Webhooks

`jeb` uses the Riverhog operator webhook contract with:

- `source = "jeb"`
- `actor = "jeb"`
- emoji `🤖`
- event `jeb.issue`

Only unrecoverable or operator-blocking failures send webhooks. Transient upload
or target errors retry silently.

See [`config/examples/jeb/jeb.toml`](../../config/examples/jeb/jeb.toml) for the
generic configuration shape.
