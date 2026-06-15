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
- fake examples under `config/examples/munchy`.

The public repository must not contain real personal devices, real hostnames, real rclone remotes,
Home Assistant recipients, SMB paths, or deployment overlays for a specific machine.

Munchy jobs require uploaded paths to be shaped as `<profile-group>/<file>`. Profile-group names
are the stable boundary between device-specific private configuration and generic public encode
behavior.

Munchy operator webhook payloads identify themselves with `source = "munchy"` and the canonical
emoji `🤤`.

## Consequences

Riverhog core remains the custody boundary. Munchy can call Riverhog's collection upload interface
after it has produced a finished collection, but Riverhog does not orchestrate pre-custody device
offload or GPU transcodes.

Private deployments can install or wrap `munchy` with their own convenience commands without
publishing private topology.
