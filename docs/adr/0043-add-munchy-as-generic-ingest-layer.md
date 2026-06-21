# 0043. Add Munchy As The Generic Media Ingest Layer

Date: 2026-06-05

## Status

Accepted

## Context

Riverhog is the custody system for finished archival collections. It should stay focused on
cataloging, durable storage, recovery, optical media planning, and collection upload acceptance.

Media offload, review generation, GPU transcode orchestration, source-artifact preservation, and
watch-directory ingestion happen before Riverhog accepts custody. Those capabilities are useful
outside any one private deployment, but real devices, hostnames, destinations, webhook recipients,
and deployment overlays are private operational configuration.

## Decision

Add a separate public `munchy` layer for generic media ingest and transcode orchestration.

The public Riverhog repository may contain:

- `src/munchy` for typed ingest/profile/source-artifact primitives.
- `src/munchy_cli` for the `munchy` CLI.
- `services/munchy-runner` for the non-GPU orchestration service.
- `services/munchy-av1-nvenc` for the GPU encode target.
- `src/jeb` and `services/jeb` for the generic watched-drop collector.
- `services/ftpd` for a generic FTP landing service that can feed Jeb.
- fake examples under `config/examples/munchy`.

The public repository must not contain real personal devices, real hostnames, real rclone remotes,
Home Assistant recipients, SMB paths, or deployment overlays for a specific machine.

Munchy jobs may either use explicit `<profile-group>/<file>` paths or structured uploads with
server-side profile routing. Profile-group names and routing rules are the stable boundary between
device-specific private configuration and generic public encode behavior.

The `munchy` CLI is intended to be usable directly, not only through private wrappers. It exposes
encode-profile inspection and runner job operations, including `munchy job start` for local
file/directory uploads, `munchy job list`, `munchy job show`, `munchy job watch`, and
`munchy job cancel`.

The canonical Munchy encode-profile contract is the runner/Jeb profile shape used in job configs:
flat `[archive]` AV1/NVENC settings with nested `[archive.audio]`, optionally collected under
`[profiles.<name>]` tables. The CLI profile commands validate and display that same contract so
private Jeb/family-archive generated configs and direct Munchy configs do not drift apart.

Munchy operator webhook payloads identify themselves with `source = "munchy"` and the canonical
emoji `🤤`.

## Consequences

Riverhog core remains the custody boundary. Munchy can call Riverhog's collection upload interface
after it has produced a finished collection, but Riverhog does not orchestrate pre-custody device
offload or GPU transcodes.

Private deployments can install or wrap `munchy` with their own convenience commands without
publishing private topology.
