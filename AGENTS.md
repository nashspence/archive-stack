# AGENTS.md

## Identity

You are working in the public `riverhog` repository. Keep it generic: Riverhog
is the archive custody/API layer, Munchy is the media ingest layer, Jeb is the
watched-drop scheduler/uploader, and Djdan is the disc workflow CLI.

## Critical Risks

Archive custody and source cleanup are the dangerous parts. Be careful around
upload finalization, catalog migrations, archive restores, optical-media
layout, Munchy source artifacts, Jeb cleanup after target success, webhook
notifications, and storage credentials.

Do not add private deployment details to this repository. Real hostnames,
device names, operator identities, webhook recipients, rclone remotes,
credentials, and household-specific examples belong in downstream private
configuration. Public tests and examples should use role-based fake names.

## Normal Work

Read the relevant docs and nearby tests before changing behavior. Prefer shared
core code in `src/riverhog_core/`; keep FastAPI routers and CLIs thin. Keep
Munchy and Jeb boundaries narrow: Jeb may preflight a complete account batch and
upload it, while Munchy owns routing, profile selection, metadata projection,
archive/leave/cull behavior, and Riverhog archive contents.

Use structured config schemas and public examples instead of deployment-specific
special cases. If a feature is useful with fake config, it belongs here; if it
needs a real deployment identity to make sense, make it configuration supplied
by the downstream operator.

## Validation

Use the Makefile lanes through the repo-selected mise/uv toolchain:

```bash
mise install
make lint
make unit
```

Run `make test` for the serial aggregate lane. Run `make spec` when changing
acceptance features, external contracts, recovery layout, or fixture-backed
behavior. If the spec lane is running and source must change, run
`make stop-spec`, edit, then restart it.

## Ownership and Routes

- Human map: `README.md`.
- Architecture and code layout: `docs/explanation/architecture-overview.md` and
  `docs/explanation/codebase-layout.md`.
- API/config/domain references: `docs/reference/api.md`,
  `docs/reference/configuration.md`, and `docs/reference/domain-model.md`.
- Munchy/Jeb/Gogurt boundaries: `docs/reference/munchy.md`,
  `docs/reference/jeb.md`, and `docs/reference/gogurt.md`.
- Acceptance and local stack workflows: `docs/how-to/run-acceptance-tests.md`
  and `docs/how-to/run-the-compose-stack.md`.
- Architecture decisions: `docs/adr/README.md`.

Keep entrypoints concise and current. Retired names, migration history, and
abandoned rationale belong in issues or ADRs, not permanent entrypoint prose.
