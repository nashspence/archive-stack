# Run a guided burn session

The `arc-disc burn` command walks the current burn backlog from the fullest ready image downward.
If a finalized image has lost all protected copies, Riverhog tracks that image
through an `image_rebuild` recovery session instead; `arc-disc burn` reports
that handoff and does not treat it as ordinary replacement backlog.

## Host requirements

- Install `xorriso` on the operator machine.
- Run the command as a user that can write to and read from the optical device path, such as `/dev/sr0`.
- Insert blank writable media when prompted. The default backend burns the staged ISO with `xorriso -as cdrecord`.
- After burning, keep the same disc available in the drive. `arc-disc` verifies the burned media by reading the first
  ISO-sized byte range back from the device and comparing it to the staged ISO.

## Flow

1. Select the fullest ready backlog item.
2. Finalize it if it is still only a provisional candidate.
3. Download the ISO into the local staging directory.
4. Verify the staged ISO before each burn step that still needs it.
5. Burn one required copy.
6. Verify the burned media.
7. Show the exact disc label text and storage guidance.
8. Wait for explicit confirmation that the disc is labeled.
9. Record the storage location and register the copy only after that confirmation.
10. Repeat until every required copy is finished, then move to the next backlog item.

If the session stops after a burn or burned-media verification but before label confirmation, a later `arc-disc burn`
run first asks whether that unlabeled disc is still available. If it is, the session resumes from the earliest
unfinished checkpoint for that copy: burned-media verification if the burn was not verified yet, otherwise label
confirmation. If it is not, `arc-disc burn` discards that local checkpoint and burns a replacement copy instead.
Riverhog does not register or count the copy toward coverage until the operator confirms that the disc is labeled.

If the staged ISO is missing or no longer matches the last verified staged copy, `arc-disc burn` downloads the ISO
again before continuing.

Expected failures include a missing `xorriso` executable, insufficient device permissions, non-blank or incompatible
media, a drive that cannot burn the inserted media type, and a burned-media byte comparison that does not match the
staged ISO.

Device-missing, not-ready, permission-denied, and unavailable-during-work outcomes are operator states. They should tell
the operator to check configuration, insert or wait for media, fix permissions, reconnect the drive, or retry from the
last safe checkpoint.

CLI example:

```bash
arc-disc burn --device /dev/sr0
```

Optional staging-root example:

```bash
arc-disc burn --device /dev/sr0 --staging-dir /operator/arc-disc-staging
```

## Recover an image rebuild session

Use `arc-disc recover` when `arc-disc burn` reports that ordinary backlog is
clear but image rebuild work remains.

1. Run `arc-disc recover` with no session id to list the active recovery sessions.
2. Run `arc-disc recover <session-id>` once to approve the restore request if the session is still
   `pending_approval`.
3. Wait until the session reports `ready`.
4. Run `arc-disc recover <session-id> --device /dev/sr0` to rebuild and stage the
   ISO data from restored collection archives, then burn the needed replacement
   copies.
5. If that run is interrupted after staging or after partial burn work, run the same command again to resume from the
   local checkpoints and staged ISO artifacts.

Examples:

```bash
arc-disc recover
arc-disc recover rs-20260420T040001Z-rebuild-1 --device /dev/sr0
```
