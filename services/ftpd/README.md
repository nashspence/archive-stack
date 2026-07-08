# FTPD

Generic Pure-FTPd landing service for devices that can push files over FTP.

Each configured FTP user is jailed into a matching directory under `FTP_ROOT`.
Use the same slug in `JEB_ACCOUNTS` so Jeb watches that account directory and
submits scheduled batches to Munchy.

## Configuration

Set `FTPD_USERS` to a comma-separated list of usernames. For each username, set a
matching password variable using the uppercased username with non-alphanumeric
characters changed to underscores.

Example:

```sh
FTPD_USERS=device-a,device-b
FTPD_PASSWORD_DEVICE_A=replace-with-secret
FTPD_PASSWORD_DEVICE_B=replace-with-secret
```

Host-specific usernames, passwords, public hostnames, and volume paths belong in
private deployment configuration.
