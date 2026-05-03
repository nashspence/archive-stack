# ADR-0044: Handle API Unreachability as Operator Preflight

## Decision

API-backed operator flows use a shared `api_unreachable` preflight state when the local CLI cannot reach the Riverhog API because the service is offline, refused, timed out, or otherwise unavailable.

The state applies to `arc.home`, `arc.upload`, `arc.hot_storage`, `arc.collection_status`, `arc.copy_management`, `arc.maintenance`, `arc_disc.guided`, `arc_disc.burn`, `arc_disc.recovery`, `arc_disc.fetch`, and `arc_disc.hot_recovery`.

Normal operator copy tells the user to check that the Riverhog API is running and that local configuration points to it. It must not expose raw transport exceptions as normal human guidance.

Server-side notification payload generation is out of scope because it does not call the local operator API client.

## Reason

API availability is a cross-cutting boundary condition. A shared preflight state prevents each command from inventing different transport-failure wording or misclassifying API outages as domain failures.
