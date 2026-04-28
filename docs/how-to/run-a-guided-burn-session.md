# Run a guided burn session

The `arc-disc burn` command walks the current burn backlog from the fullest ready image downward.
If a finalized image has lost all protected copies, Riverhog tracks that image through a Glacier-backed recovery
session instead; `arc-disc burn` reports that recovery handoff and does not treat it as ordinary replacement backlog.

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

CLI example:

```bash
arc-disc burn --device /dev/sr0
```

Optional staging-root example:

```bash
arc-disc burn --device /dev/sr0 --staging-dir /operator/arc-disc-staging
```
