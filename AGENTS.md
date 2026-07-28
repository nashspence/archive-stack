# AGENTS.md

Read [README.md](README.md) for the product entrypoint.

## Boundaries

The [architecture](docs/architecture.md) is the authority for component roles, boundaries,
and core terms. Enforce those boundaries in code: server, client, companion, target,
utility, and recovery implementations may share focused packages but never import one
another's implementation modules. Runtime integration crosses published HTTP and
CloudEvents contracts. `riverhog/recovery` must remain independent of the server, client,
and database. Hardware-specific Munchy targets remain server-owned even when separately
deployed. Keep public code generic; real identity and deployment topology belong downstream.

## Safety

- Treat configured archive stores as the durable authority.
- Keep ingress and retrieval-cache objects encrypted whenever they leave a client or
  Riverhog process.
- Preserve verified archive bytes, the encrypted manifest, and its proof together.
- Treat catalog and object-store mutations as one custody operation.
- Never expose secrets or private deployment identity in public code, fixtures, logs,
  examples, or generated contracts.

## Sources of truth

Exact behavior belongs in code and tests. Current API shape comes from the running
application's OpenAPI document, command syntax comes from `--help`, and configuration
shape comes from real parsers and checked executable examples. Do not add hand-maintained
inventories of those surfaces to `main`; release reference is generated from a tag.

Durable documentation has distinct roles:

- [README](README.md) is the human entrypoint and repository map;
- [architecture](docs/architecture.md) owns the stable mental model, boundaries, and terms;
- [archive operations](docs/archive-operations.md) owns human custody judgment;
- [recovery without Riverhog](docs/recovery-without-riverhog.md) owns the portable recovery
  procedure.

Licensing, security reporting, and contribution policy remain in their conventional
top-level files. Do not add release reference or duplicate executable contracts to `main`.

## Work

- Read the relevant implementation, tests, and executable contracts before editing.
- Use canonical current names without aliases, migrations, or compatibility shims unless
  a verified supported dependency requires one.
- Keep domain behavior in services, external effects behind ports, and HTTP or CLI
  formatting in adapters.
- Put reusable behavior in a focused package; never share code by importing across a
  server, client, companion, target, or utility implementation boundary.

## Validation

Run focused tests while iterating, then:

```bash
make lint
make unit
make spec
make dist-smoke
make build
```
