# AGENTS.md

Read [README.md](README.md) for the product entrypoint.

## Boundaries

`riverhog/server` is the custody platform service. `riverhog/client` is its official direct
CLI; the client's `local` subtree is the reference external application and owns its local
materialization. Both are independent distributions and the client must not import server
implementation modules.

`companions/munchy` and `companions/jeb` are companion applications with independent
server and client distributions. Munchy owns generic media ingest. Jeb owns generic
watched-drop collection. Hardware-specific Munchy execution targets live beneath
`companions/munchy/server/targets`; they are server-owned components with separate images
for selective placement, not companion applications.

`utilities/mango-fish` and `utilities/gogurt` are Riverhog-agnostic utilities. Mango Fish
owns generic CloudEvents-to-webhook delivery. Gogurt maps mounted-volume markers to
configured actions. Implementation projects may depend on focused packages, but never
import one another's implementation modules; runtime integration crosses HTTP and
CloudEvents contracts. Keep public code generic; real identity and deployment topology
belong downstream.

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

Manual documentation is limited to:

- [architecture](docs/architecture.md) for the current mental model and boundaries;
- [archive operations](docs/archive-operations.md) for human custody judgment.

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
make build
```
