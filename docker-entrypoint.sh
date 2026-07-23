#!/bin/sh
set -e

CONF_DIR="${PYMON_DATA_DIR:-/data}/conf"
mkdir -p "$CONF_DIR"

# ── Seed default config files if the volume is empty ─────────────────────
# Each file is only written once; manual edits are never overwritten.

seed() {
    local target="$CONF_DIR/$1"
    local content="$2"
    if [ ! -f "$target" ]; then
        printf '%s' "$content" > "$target"
        echo "pymon: seeded $target"
    fi
}

# Default admin user: admin / admin
# Change password immediately via the Account page or set PYMON_ADMIN_PASSWORD
# at first boot (before the container creates users.json).
if [ ! -f "$CONF_DIR/users.json" ]; then
    if [ -n "$PYMON_ADMIN_PASSWORD" ]; then
        hash=$(python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('$PYMON_ADMIN_PASSWORD'))")
    else
        hash=$(python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('admin'))")
        echo "pymon: WARNING — using default password 'admin'. Set PYMON_ADMIN_PASSWORD or change it in the UI."
    fi
    printf '{"admin":"%s"}\n' "$hash" > "$CONF_DIR/users.json"
    echo "pymon: seeded $CONF_DIR/users.json"
fi

seed "agents.json"       '{"agents":{},"groups":{}}'
seed "rules.json"        '{}'
seed "executors.json"    '{}'
seed "notifications.json" '{}'
seed "blackouts.json"    '{}'
seed "plugins.json"      '{}'
seed "snoozes.json"      '[]'

exec "$@"
