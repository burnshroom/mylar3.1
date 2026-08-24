#!/usr/bin/env bash
set -e

# Default PUID / PGID / UMASK
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-002}"

# Validate PUID/PGID as positive numeric values
if ! [[ "$PUID" =~ ^[0-9]+$ ]] || [ "$PUID" -le 0 ]; then
    echo "[WARN] Invalid PUID '$PUID'; falling back to 1000" >&2
    PUID=1000
fi

if ! [[ "$PGID" =~ ^[0-9]+$ ]] || [ "$PGID" -le 0 ]; then
    echo "[WARN] Invalid PGID '$PGID'; falling back to 1000" >&2
    PGID=1000
fi

# Apply umask
umask "$UMASK"

# If running as root, configure user/group and fix /config permissions
if [ "$(id -u)" = "0" ]; then
    # Group setup
    if ! getent group mylar >/dev/null 2>&1; then
        existing_group=$(getent group "$PGID" | cut -d: -f1 || true)
        if [ -n "$existing_group" ]; then
            GROUP_NAME="$existing_group"
        else
            addgroup -g "$PGID" mylar >/dev/null 2>&1 || true
            GROUP_NAME="mylar"
        fi
    else
        GROUP_NAME="mylar"
        groupmod -o -g "$PGID" mylar >/dev/null 2>&1 || true
    fi

    # User setup
    if ! getent passwd mylar >/dev/null 2>&1; then
        existing_user=$(getent passwd "$PUID" | cut -d: -f1 || true)
        if [ -n "$existing_user" ]; then
            USER_NAME="$existing_user"
        else
            adduser -u "$PUID" -G "$GROUP_NAME" -D -h /app/mylar3 -s /sbin/nologin mylar >/dev/null 2>&1 || true
            USER_NAME="mylar"
        fi
    else
        USER_NAME="mylar"
        usermod -o -u "$PUID" -g "$PGID" mylar >/dev/null 2>&1 || true
    fi

    # Ensure /config exists and is owned by target UID:GID
    mkdir -p /config
    chown "$PUID:$PGID" /config

    # Ensure /comics and /downloads exist if mount points are present (never recursively chown)
    mkdir -p /comics /downloads 2>/dev/null || true

    # Validate /config writability
    if ! su-exec "$PUID:$PGID" test -w /config; then
        echo "[ERROR] Mount path /config is not writable by UID $PUID (GID $PGID)" >&2
        exit 1
    fi

    # Check command to execute
    if [ "$#" -eq 0 ]; then
        set -- python3 /app/mylar3/Mylar.py --nolaunch --quiet --datadir /config
    elif [[ "$1" == -* ]]; then
        set -- python3 /app/mylar3/Mylar.py --nolaunch --quiet --datadir /config "$@"
    fi

    exec su-exec "$PUID:$PGID" "$@"
else
    # Running already as non-root
    if ! test -w /config; then
        echo "[ERROR] Mount path /config is not writable by current user $(id -u)" >&2
        exit 1
    fi

    if [ "$#" -eq 0 ]; then
        set -- python3 /app/mylar3/Mylar.py --nolaunch --quiet --datadir /config
    elif [[ "$1" == -* ]]; then
        set -- python3 /app/mylar3/Mylar.py --nolaunch --quiet --datadir /config "$@"
    fi

    exec "$@"
fi
