# Fulfill a fetch from optical media

The `arc-disc` CLI is the recovery client for a machine with an optical drive.

## Flow

1. Read the fetch manifest.
2. Determine which copy to insert.
3. Read encrypted payloads from the selected optical copy.
4. Decrypt recovered bytes.
5. Upload recovered files for each manifest entry.
6. Complete the fetch.

CLI example:

```bash
arc-disc fetch fx_01JV8W5J8M8F3J5V4A8Q --json
```

The command should exit successfully only if the fetch reaches `done`.
