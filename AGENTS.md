# AGENTS.md

Read [README.md](README.md) for the product entrypoint.

## Boundaries

Server, client, companion, target, utility, and recovery implementations may share focused
packages but never import one another's implementation modules. Runtime integration crosses
published HTTP and CloudEvents contracts. `riverhog/recovery` must remain independent of the
server, client, and database. stove0 core remains content-opaque; observers interpret bytes,
targets perform declared transformations, and protocol adapters only produce collections.
Hardware-specific targets remain server-owned even when separately deployed. Keep public code
generic; real identity and deployment topology belong downstream.

## Safety

- Treat configured archive stores as the durable authority.
- Default plaintext ingress to authenticated TLS. Permit plaintext HTTP only through an
  explicit operator opt-in for a trusted single-tenant LAN. Keep final archive and
  retrieval-cache objects encrypted whenever they leave a Riverhog process.
- Preserve verified archive bytes, the encrypted canonical root, its bounded volume
  authorities, authenticated terminator, and recovery descriptor together.
- Preserve archive recovery without Riverhog or its database using standard tools;
  `riverhog/recovery` remains an independent reference implementation.
- Treat catalog and object-store mutations as one archive mutation.
- Never expose secrets or private deployment identity in public code, fixtures, logs,
  examples, or generated contracts.

## Sources of truth

Exact behavior belongs in code and tests. Current API shape comes from the running
application's OpenAPI document, command syntax comes from `--help`, and configuration
shape comes from real parsers and checked executable examples. Do not add hand-maintained
inventories of those surfaces to `main`; release reference is generated from a tag.

Durable documentation has distinct roles:

- [README](README.md) is the human entrypoint;
- [architecture](docs/architecture.md) owns the authority and implementation-boundary
  mental model and repository map.

Licensing and security reporting remain in their conventional top-level files. Do not add
release reference or duplicate executable contracts to `main`.

## Work

- Read the relevant implementation, tests, and executable contracts before editing.
- Use canonical current names without aliases or compatibility shims unless a verified
  supported dependency requires one. Before v1, revise the baseline rather than preserving
  retired schema.
- Keep domain behavior in services, external effects behind ports, and HTTP or CLI
  formatting in adapters.
- Put reusable behavior in a focused package; never share code by importing across a
  server, client, companion, target, or utility implementation boundary.
- Install a mise-owned standalone container tool with `mise install --locked` in a
  digest-pinned disposable build stage, then copy only its required runtime artifacts
  forward.
- Keep each durable relational database under one application owner and one linear,
  forward-only migration history. Runtime startup validates state; deployment applies
  upgrades explicitly.
- Treat application databases as durable operational state, projections and
  format-versioned caches as rebuildable, and archive stores as the separate durable
  archive authority.
- Keep each release's current state baseline and verification evidence exact. State
  compatibility is not implied by a revision name or fixture; any transition is an
  explicitly scoped release operation.

## Validation

Run focused tests while iterating, then:

```bash
make lint
make unit
make spec
make dist-smoke
make build
```

After pushing, watch the pushed commit's GitHub Actions checks through completion. Required
GitHub checks are part of complete validation; fix any failure before handing work back.
`release.toml` owns the release-governance policy. Direct commits to `main` are the current
pre-v1 contract-convergence rail; validate every pushed SHA through GitHub Actions. Keep the
protected `release/v1` branch pinned as an ancestor until the maintainer explicitly selects a
new, fully green freeze candidate for synchronization through its pull-request rail. Provider
qualification stays disabled while that release checkpoint is pinned. Publication
requires its protected GitHub environment, a complete governance check with an
administrator-readable GitHub token, and never moves a v1 tag. GitHub Actions runs the narrower
`actions-observable` governance scope because its least-privilege token cannot read repository
administration settings.
