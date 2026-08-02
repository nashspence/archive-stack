# Public interface conventions

Riverhog, Munchy, and Jeb remain separate applications. Equal HTTP and CLI concepts use
the following observable contracts; application-specific workflows remain owned by their
application.

Jeb submits work through Munchy's generic submission, upload, status, cancellation, and
submission-failure operations. Munchy does not expose a Jeb-specific interface. Raw
workflow steps need not become CLI commands when an existing application command already
composes them into an operator action.

## HTTP and official clients

- `GET /health/live` reports process life. `GET /health/ready` checks required runtime
  dependencies. Both return only `{"service": "<name>", "status": "ok"}` on success.
- Public errors use `{"error": {"code": "<code>", "message": "<message>",
  "details": {...}}}`. `details` is omitted when empty. Official client exceptions retain
  the server code, HTTP status, message, and details.
- Management authentication uses `Authorization: Bearer <token>`. Health endpoints do not
  require management credentials. Internal upload hooks keep their transport-owned
  authentication and response formats.
- Clients read `<APP>_BASE_URL`, `<APP>_TOKEN`, `<APP>_TLS_VERIFY`, `<APP>_HTTP2`, and
  `<APP>_HTTP_TIMEOUT_SECONDS`, where `<APP>` is `RIVERHOG`, `MUNCHY`, or `JEB`. TLS
  verification and HTTP/2 default to enabled; the management timeout defaults to 300
  seconds. Constructor arguments override base URL and token environment values.
- Paged lists use one-based `page`, bounded `per_page` (1 through 100), optional `q`,
  `sort`, and `order` where those concepts apply, plus `all`. Responses include `page`,
  `pages`, `per_page`, and `total`. CloudEvents use their ordered cursor contract instead
  of page numbers.

## Official CLIs

- Human results go to standard output. Diagnostics, progress, and transition notices go
  to standard error.
- `--json` emits one compact JSON result to standard output. API and transport failures
  emit the public error document there with no human diagnostic mixed in.
- Watch commands emit only the final document in JSON mode. A successfully completed
  operation exits `0`; a service failure, transport failure, or unsuccessful terminal
  operation exits `1`; command-line usage errors exit `2`.
- List commands never expose credential material. A key-creation command may return its
  newly created token once; later show and list operations do not. Credential inputs are
  never repeated in human or JSON output.

The repository tests derive parity from each live OpenAPI document, official client
metadata, and installed `--help` output. Those executable contracts are the release
reference on `main`.
