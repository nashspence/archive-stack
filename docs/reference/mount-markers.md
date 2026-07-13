# Mount Markers

Mount markers are Riverhog's generic way to bind a mounted volume to a local
operator action without teaching the caller about devices, routing, or private
workflow policy.

A mounted volume contains a marker file. The marker file's first line names a
configured route. The route maps to an executable action script and opaque
arguments. Riverhog's mount-marker layer only selects and invokes the action; it
does not interpret device ids, destinations, secrets, archive policy, or media
routing.

The public defaults are:

- launchd label: `io.github.nashspence.riverhog.mount-marker`
- marker file: `.riverhog.mount-marker`
- environment prefix: `RIVERHOG_MOUNT_MARKER_*`
- CLI group: `riverhog mount-marker`

## Route Config

```yaml
schema_version: 1
kind: riverhog.mount_marker_routes
routes:
  example-camera-card:
    script: fake-archive-device
    args:
      - example-camera
```

Each enabled route renders one trigger script named after the route key. The
generated trigger contract is:

```text
trigger MOUNT_POINT
```

The generated trigger validates the configured action script is executable and
then runs:

```text
SCRIPT MOUNT_POINT [args...]
```

Arguments are opaque strings. Private or downstream clients should keep real
device names, destinations, secret references, and workflow policy in their own
route config and action scripts.

## CLI

List configured routes:

```bash
riverhog mount-marker list --config mount-marker-routes.yaml
```

Render trigger scripts:

```bash
riverhog mount-marker render \
  --config mount-marker-routes.yaml \
  --scripts-dir scripts \
  --dest-dir mount-triggers
```

Write a marker file to a mounted volume:

```bash
riverhog mount-marker write example-camera-card /Volumes/CAMERA \
  --config mount-marker-routes.yaml
```

The macOS listener reads `RIVERHOG_MOUNT_MARKER_NAME` and
`RIVERHOG_MOUNT_MARKER_TRIGGERS_DIR`. If they are unset it uses the public
defaults above.
