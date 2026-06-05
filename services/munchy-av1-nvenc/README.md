# Munchy AV1 NVENC Target

`munchy-av1-nvenc` is the generic GPU encode target for AV1/Opus constant-quality media workflows.

It is intended to be run by a deployment-specific GPU service manager or compose stack that provides
exclusive GPU access. This directory should stay generic: no private hostnames, device names, real
profiles, webhook recipients, or rclone destinations.

Encode behavior is configured by typed `munchy` profiles. Private deployments provide the actual
profile files and map upload subdirectories to profile names.
