# 0044. Add Jeb As The Generic Watched-Drop Collector

Date: 2026-06-15

## Status

Accepted

## Context

Some ingest sources are better modeled as server-side landing directories than as
interactive client uploads. Examples include FTP/SFTP camera uploads, phone-sync
dropboxes, and SMB-backed staging trees. These sources still need the same safety
properties as other ingest paths: durable resume after restarts, no data deletion
before target success, quiet infinite retry for transient failures, and concise
operator notifications for unrecoverable issues.

## Decision

Add `jeb` as a generic watched-drop collector in the public repository.

Jeb may:

- watch configured landing directories
- create durable SQLite-backed batches
- submit source-prefixed weekly batches to Munchy
- delete staged files only after configured target success
- send Riverhog-format operator webhooks with `source = "jeb"` and emoji `🤖`

Jeb must not contain private device names, FTP users, passwords, hostnames,
webhook URLs, or private deployment overlays. Those values belong in private
configuration.

## Consequences

Riverhog remains the custody boundary. Munchy remains the media conversion and
routing layer. Jeb becomes the generic bridge from watched landing
directories into Munchy.

Transient failures are operationally normal and retry silently. Unrecoverable
failures are durable, block the affected source, and send paced critical reminders
until resolved.
