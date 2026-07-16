#!/usr/bin/env sh
set -eu

PROJECTION="${JEB_FTP_PROJECTION:-/state/ingress/ftp/passwd}"
PASSWD_DIR="/etc/pure-ftpd/passwd"
PASSWD_FILE="$PASSWD_DIR/pureftpd.passwd"
PUREDB_FILE="$PASSWD_DIR/pureftpd.pdb"
MAX_CLIENTS="${JEB_FTP_MAX_CLIENTS:-40}"
MAX_CONNECTIONS="${JEB_FTP_MAX_CONNECTIONS:-8}"
PUBLICHOST="${JEB_FTP_PUBLIC_HOST:-}"

if [ -z "$PUBLICHOST" ]; then
    echo "JEB_FTP_PUBLIC_HOST must be set" >&2
    exit 1
fi

install -d -m 700 "$PASSWD_DIR"

project() {
    if [ ! -f "$PROJECTION" ]; then
        echo "Jeb FTP credential projection is not ready: $PROJECTION" >&2
        return 1
    fi
    cp "$PROJECTION" "$PASSWD_FILE.next"
    chmod 600 "$PASSWD_FILE.next"
    mv "$PASSWD_FILE.next" "$PASSWD_FILE"
    pure-pw mkdb "$PUREDB_FILE.next" -f "$PASSWD_FILE"
    mv "$PUREDB_FILE.next" "$PUREDB_FILE"
}

project
fingerprint="$(sha256sum "$PROJECTION")"

/run.sh \
    -c "$MAX_CLIENTS" \
    -C "$MAX_CONNECTIONS" \
    -l "puredb:$PUREDB_FILE" \
    -E \
    -j \
    -R \
    -P "$PUBLICHOST" &
server_pid="$!"

trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid"' INT TERM

while kill -0 "$server_pid" 2>/dev/null; do
    current="$(sha256sum "$PROJECTION" 2>/dev/null || true)"
    if [ -n "$current" ] && [ "$current" != "$fingerprint" ]; then
        project
        fingerprint="$current"
    fi
    sleep 2
done

wait "$server_pid"
