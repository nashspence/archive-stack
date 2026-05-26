# Run a guided burn session

The `djdan burn` command walks the current burn backlog from the fullest ready image downward.
If a finalized image has lost all protected copies, Riverhog tracks that image
through an `image_rebuild` recovery session instead; `djdan burn` reports
that handoff and does not treat it as ordinary replacement backlog.

## Host requirements

- Install `xorriso` on Linux-style operator machines. On macOS, `djdan` uses the system `hdiutil burn` image-burn
  tool.
- Run the command as a user that can write to and read from the optical device path, such as `/dev/sr0` on Linux or
  `/dev/disk4` on macOS.
- Insert blank writable media when prompted. The default backend burns the staged ISO image with `hdiutil burn` on
  macOS and `xorriso -as cdrecord` elsewhere.
- For hardware smoke tests before spending media, use `--simulate` to run the native non-writing burn mode with the
  drive's laser off.
- After burning, keep the same disc available in the drive. `djdan` verifies the burned media by reading the first
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

If the session stops after a burn or burned-media verification but before label confirmation, a later `djdan burn`
run first asks whether that unlabeled disc is still available. If it is, the session resumes from the earliest
unfinished checkpoint for that copy: burned-media verification if the burn was not verified yet, otherwise label
confirmation. If it is not, `djdan burn` discards that local checkpoint and burns a replacement copy instead.
Riverhog does not register or count the copy toward coverage until the operator confirms that the disc is labeled.

If the staged ISO is missing or no longer matches the last verified staged copy, `djdan burn` downloads the ISO
again before continuing.

Expected failures include a missing `xorriso` executable, insufficient device permissions, non-blank or incompatible
media, a drive that cannot burn the inserted media type, and a burned-media byte comparison that does not match the
staged ISO.

CLI example:

```bash
djdan burn --device /dev/sr0
```

Optional staging-root example:

```bash
djdan burn --device /dev/sr0 --staging-dir /operator/djdan-staging
```

macOS device example:

```bash
djdan burn --device /dev/disk4 --staging-dir /operator/djdan-staging
```

On macOS, the `--device` value is validated with `diskutil`, but `djdan` lets
`hdiutil burn` select the system optical burner. This matches the local
`burniso` wrapper behavior and avoids the unreliable `hdiutil -device` path for
USB Blu-ray drives.

If an operator needs to force a native hdiutil target, pass the target reported
by `hdiutil burn -list` as `--device 'hdiutil:IOService:...'`.

## Simulate a burn on real hardware

Use `--simulate` to verify that `djdan`, the platform burn tool, the selected
optical device, and the inserted media can run the burn path before writing a
real disc:

```bash
djdan burn --simulate --device /dev/sr0 --staging-dir /operator/djdan-staging
```

On macOS, pass the disk device reported by `diskutil`:

```bash
djdan burn --simulate --device /dev/disk4 --staging-dir /operator/djdan-staging
```

The simulated run stages and verifies the ISO, then invokes
the platform's non-writing burn command for the next pending copy:
`hdiutil burn -testburn` on macOS and `xorriso -as cdrecord -dummy`
elsewhere. If the next burn item is still a ready provisional candidate, the
command finalizes it first so the normal finalized-image ISO and generated copy
id are used.

macOS native test burns depend on the media family and drive support exposed by
DiscRecording. Some Blu-ray drives can burn BD-R media normally but do not expose
native BD-R test-burn support; in that case `djdan burn --simulate` fails before
starting the burn command and the real burn path must be tested with expendable
media.

Because no bytes are written to disc, simulated burns intentionally stop before
burned-media verification, label confirmation, copy registration, and copy
checkpoint updates. A successful simulated burn does not protect the image and
does not clear burn backlog.

## Recover an image rebuild session

Use `djdan recover` when `djdan burn` reports that ordinary backlog is
clear but image rebuild work remains.

1. Run `djdan recover` with no session id to list the active recovery sessions.
2. Run `djdan recover <session-id>` once to approve the restore request if the session is still
   `pending_approval`.
3. Wait until the session reports `ready`.
4. Run `djdan recover <session-id> --device /dev/sr0` to rebuild and stage the
   ISO data from restored collection archives, then burn the needed replacement
   copies.
5. If that run is interrupted after staging or after partial burn work, run the same command again to resume from the
   local checkpoints and staged ISO artifacts.

Examples:

```bash
djdan recover
djdan recover rs-20260420T040001Z-rebuild-1 --device /dev/sr0
```
