#!/usr/bin/env bash
#
# sync.sh — Sync Hermes plugins from ~/.hermes/plugins/ to this repo
#
# Usage:
#   ./sync.sh              # sync all plugins (default)
#   ./sync.sh agnes-ai     # sync a specific plugin directory only
#   ./sync.sh --dry        # dry-run, show what would change
#   ./sync.sh --dry agnes-ai  # dry-run a specific plugin
#
set -euo pipefail

SRC_DIR="$HOME/.hermes/plugins"
DST_DIR="$(cd "$(dirname "$0")" && pwd)"

# Plugins to sync (subdirectory names under ~/.hermes/plugins/)
# Add or remove entries here to control what gets synced.
SYNC_PLUGINS=(
    "agnes-ai"
    "model-providers"
)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DRY=false
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry) DRY=true; shift ;;
        *)     TARGET="$1"; shift ;;
    esac
done

if [[ -n "$TARGET" ]]; then
    SYNC_PLUGINS=("$TARGET")
fi

echo -e "${GREEN}==>${NC} Syncing Hermes plugins"
echo -e "    Source: ${SRC_DIR}"
echo -e "    Target: ${DST_DIR}"
echo -e "    Plugins: ${SYNC_PLUGINS[*]}"
echo ""

if $DRY; then
    echo -e "${YELLOW}    (dry-run mode — no files will be written)${NC}"
    echo ""
fi

synced=0
errors=0

for plugin in "${SYNC_PLUGINS[@]}"; do
    src="${SRC_DIR}/${plugin}"
    dst="${DST_DIR}/${plugin}"

    if [[ ! -d "$src" ]]; then
        echo -e "  ${RED}✗${NC} ${plugin}: source not found (${src})"
        errors=$((errors + 1))
        continue
    fi

    if $DRY; then
        echo -e "  ${YELLOW}~${NC} ${plugin}: (dry-run)"
        rsync -avn --delete \
            --exclude='__pycache__/' \
            --exclude='*.pyc' \
            "$src/" "$dst/" 2>/dev/null | head -20
        synced=$((synced + 1))
        continue
    fi

    if rsync -a --delete \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        "$src/" "$dst/" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} ${plugin}: synced"
        synced=$((synced + 1))
    else
        echo -e "  ${RED}✗${NC} ${plugin}: rsync failed"
        errors=$((errors + 1))
    fi
done

echo ""
echo -e "${GREEN}==>${NC} Done: ${synced} synced, ${errors} errors"

if [[ $synced -gt 0 ]] && ! $DRY; then
    echo ""
    echo "Changes:"
    cd "$DST_DIR"
    git status --short
    echo ""
    echo "To commit: git add -A && git commit -m 'sync plugins from ~/.hermes/plugins'"
fi
