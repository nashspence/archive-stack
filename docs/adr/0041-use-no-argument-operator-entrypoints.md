# ADR-0041: Use No-Argument Operator Entry Points

## Decision

Riverhog uses `arc` with no arguments as the general operator home for non-physical attention items and at-will software workflows.

Riverhog uses `arc-disc` with no arguments as the guided physical-media and recovery backlog clearer.

Detail subcommands may remain for scripting, inspection, tests, and explicit operator choice, but action-needed notifications do not require subcommands.

## Reason

Operators need a stable way to make progress from a notification without choosing among internal commands or inspecting archive state first.
