# Gogurt

Gogurt is Riverhog's generic way to bind a mounted volume to a local operator
action without teaching the caller about devices, routing, or private workflow
policy.

A mounted volume contains a Gogurt marker file. The marker file's first line
names a configured route. The route maps to an executable action script and
opaque arguments. Gogurt only selects and invokes the action; it does not
interpret device ids, destinations, secrets, archive policy, or media routing.

The public defaults are:

- launchd label: `io.github.nashspence.gogurt`
- marker file: `.gogurt`
- environment prefix: `GOGURT_*`
- CLI: `gogurt`
- canonical emoji: `🛹`

## Route Config

```yaml
schema_version: 1
kind: gogurt.routes
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
then logs a launch proposal:

```text
🛹 gogurt launch available: route=ROUTE action=SCRIPT mount=MOUNT_POINT
```

Autorun is opt-in. When the trigger has an interactive terminal it asks:

```text
🛹 gogurt run this action? [y/N]
```

Any answer other than `y` or `yes` exits without running the action. In
noninteractive contexts, the trigger also exits without running unless
`GOGURT_AUTORUN=1` is set explicitly. Once confirmed, it logs:

```text
🛹 gogurt launching: route=ROUTE action=SCRIPT mount=MOUNT_POINT
```

and runs:

```text
SCRIPT MOUNT_POINT [args...]
```

Arguments are opaque strings. Private or downstream clients should keep real
device names, destinations, secret references, and workflow policy in their own
route config and action scripts.

## CLI

List configured routes:

```bash
gogurt list --config gogurt-routes.yaml
```

Render trigger scripts:

```bash
gogurt render \
  --config gogurt-routes.yaml \
  --scripts-dir scripts \
  --dest-dir gogurt-triggers
```

Write a marker file to a mounted volume:

```bash
gogurt write example-camera-card /Volumes/CAMERA \
  --config gogurt-routes.yaml
```

The macOS listener reads `GOGURT_MARKER_NAME` and `GOGURT_TRIGGERS_DIR`. If they
are unset it uses the public defaults above.
