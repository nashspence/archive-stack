# Browse hot files over WebDAV

Use a read-only WebDAV server over the committed `collections/` prefix when you
want day-to-day browse and download access to hot files.

Example `rclone` command:

```bash
rclone serve webdav s3remote:riverhog/collections \
  --read-only \
  --user riverhog \
  --pass "$RIVERHOG_WEBDAV_PASSWORD" \
  --addr :8080
```

The checked-in compose harness exposes this surface at
`http://127.0.0.1:8080` by default. Production deployments should keep the
sidecar behind a LAN/VPN-only route and use HTTP Basic Auth on the WebDAV server
itself, not a browser-oriented SSO flow. Basic Auth is compatible with ordinary
WebDAV clients such as Finder, Kodi, Immich import tooling, and `rclone`.

For SWAG/nginx, publish the sidecar on a dedicated name such as
`riverhog-webdav.example.com`, pass the `Authorization` header through to the
sidecar, and keep writes disabled at the sidecar:

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name riverhog-webdav.*;

    include /config/nginx/ssl.conf;

    location / {
        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;

        set $upstream_app riverhog-webdav;
        set $upstream_port 8080;
        set $upstream_proto http;

        proxy_buffering off;
        proxy_request_buffering off;
        proxy_set_header Authorization $http_authorization;
        proxy_pass $upstream_proto://$upstream_app:$upstream_port;
    }
}
```

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
