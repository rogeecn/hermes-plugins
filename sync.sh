#!/usr/bin/env bash
#
# sync.sh — Sync Hermes plugins from ~/.hermes/plugins/ to this repo
#
set -euo pipefail

SRC_DIR="$HOME/.hermes/plugins"
DST_DIR="$(cd "$(dirname "$0")" && pwd)"

# Plugins to sync (subdirectory names under ~/.hermes/plugins/)
SYNC_PLUGINS=(
    "agnes-ai"
    "model-providers"
)

echo "==> Syncing Hermes plugins"
echo "    Source: ${SRC_DIR}"
echo "    Target: ${DST_DIR}"
echo "    Plugins: ${SYNC_PLUGINS[*]}"
echo ""

for plugin in "${SYNC_PLUGINS[@]}"; do
    src="${SRC_DIR}/${plugin}"
    dst="${DST_DIR}/${plugin}"

    if [[ ! -d "$src" ]]; then
        echo "  ✗ ${plugin}: source not found (${src})"
        continue
    fi

    rsync -a --delete \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        "$src/" "$dst/"
    echo "  ✓ ${plugin}: synced"
done

echo ""
echo "==> Done"
echo ""
echo "Changes:"
cd "$DST_DIR"
git status --short
echo ""
echo "To commit: git add -A && git commit -m 'sync plugins from ~/.hermes/plugins'"
