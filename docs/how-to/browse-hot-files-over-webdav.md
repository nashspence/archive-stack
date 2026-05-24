# Browse hot files over WebDAV

Use a read-only WebDAV server over the committed `collections/` prefix when you
want day-to-day browse and download access to hot files.

Example `rclone` command:

```bash
rclone serve webdav s3remote:riverhog/collections \
  --read-only \
  --addr :8080
```

The checked-in compose harness exposes this surface at
`http://127.0.0.1:8080` by default.

Rules for the supported surface:

- expose only the committed `collections/` namespace
- do not expose the bucket root
- do not expose `.riverhog/` staging paths
- use the surface only for browse and download of completed hot files
- reject writes through WebDAV

Protect the surface with one of:

- localhost-only binding
- VPN-only access
- reverse-proxy authentication
