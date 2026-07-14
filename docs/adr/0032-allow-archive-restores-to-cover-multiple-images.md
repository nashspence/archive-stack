# ADR-0032: Allow Archive Restores to Cover Multiple Images

## Decision

Riverhog archive restores may cover multiple finalized images when their rebuild work shares restored collection archives.

## Reason

The operator-facing recovery unit should match the cold-archive work being restored, not merely one image row at a time.
