# ADR-0046: Model arc-disc Device Availability

## Decision

`arc-disc` models optical device availability as accepted operator states before and during physical-media work.

The shared states are:

- `device_missing` when the configured device path does not exist or cannot be found
- `device_not_ready` when the device exists but media/state is not ready for the current step
- `device_permission_denied` when the operator cannot read or write the configured device
- `device_lost_during_work` when a device becomes unavailable after work starts

These states apply to `arc_disc.guided`, `arc_disc.burn`, `arc_disc.fetch`, `arc_disc.recovery`, and `arc_disc.hot_recovery` wherever those flows touch the optical device boundary.

Normal copy gives concrete next actions: check configuration, insert or wait for media, fix permissions, reconnect the drive, or retry from the last safe checkpoint. Real-device validation stays opt-in with optical-drive and human-operator capability tags.

## Reason

Optical hardware failures are common boundary states. Modeling them explicitly keeps operator guidance calm and prevents raw device-tool errors from becoming the normal product surface.
