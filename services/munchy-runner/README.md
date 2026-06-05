# Munchy Runner

`munchy-runner` is the generic non-GPU orchestration service for pre-custody media ingest.

It is intended to own work until the final Riverhog handoff:

- receive resumable source uploads
- validate `<profile-group>/<file>` input shape
- persist job state and retry state
- submit GPU encode work to a configured encode target
- upload review outputs to a configured review destination
- hand finished archive collections to Riverhog
- clean up source spools and scratch data according to configured TTLs

This service directory intentionally contains no deployment-specific hostnames, device names,
webhook recipients, rclone remotes, or private paths. Those belong in a private config/deploy repo.
