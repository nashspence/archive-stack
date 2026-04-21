# Verify a downloaded ISO before burning

Before burning a downloaded image, verify both the whole-session MD5 and the recorded per-file MD5s.

Assume the downloaded file is named `downloaded.iso`.

```bash
xorriso -abort_on FAILURE \
  -for_backup -md5 on \
  -indev downloaded.iso \
  -check_md5 FAILURE -- \
  -check_md5_r FAILURE / --
```

Expected success output includes both of these lines:

```text
Ok, session data match recorded md5.
File contents and their MD5 checksums match.
```

If the command exits non-zero, do not burn the image. Download it again and verify the new copy before proceeding.

## Notes

- `-check_md5 FAILURE --` verifies the recorded session checksum for the loaded ISO session.
- `-check_md5_r FAILURE / --` verifies the recorded MD5 of every data file in the image.
- `-for_backup` matters for downloaded file-backed ISO images. It makes `xorriso` load the image in the mode that exposes the recorded session checksum consistently.
