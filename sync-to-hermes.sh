#!/usr/bin/env bash
#
# sync-to-hermes.sh — Sync plugins from this repo to ~/.hermes/plugins/
#                     and all profile plugins directories.
#
# The project repo is the single source of truth. This script deploys
# plugins to every Hermes profile so changes are picked up on /restart.
#
# Profile-specific plugins (e.g. cspm/ponytail) are preserved — we only
# sync plugins that exist in this repo, never deleting extra files.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Plugins to sync (top-level directories in the repo, excluding non-plugin files)
PLUGINS=()
for d in "$REPO_DIR"/*/; do
    name=$(basename "$d")
    case "$name" in
        .git|.gitignore) continue ;;
    esac
    PLUGINS+=("$name")
done

# Collect all target plugin directories:
#   1. ~/.hermes/plugins/  (default profile)
#   2. ~/.hermes/profiles/*/plugins/  (each profile)
TARGETS=("$HERMES_HOME/plugins")
while IFS= read -r d; do
    TARGETS+=("$d")
done < <(find "$HERMES_HOME/profiles" -maxdepth 2 -mindepth 2 -type d -name plugins 2>/dev/null | sort)

echo "==> Syncing plugins to Hermes"
echo "    Repo:   $REPO_DIR"
echo "    Hermes: $HERMES_HOME"
echo "    Targets: ${#TARGETS[@]}"
echo ""

# List plugins to sync
for plugin in "${PLUGINS[@]}"; do
    echo "  • $plugin"
done
echo ""

for target in "${TARGETS[@]}"; do
    if [[ ! -d "$target" ]]; then
        continue
    fi

    # Determine the profile name for display
    if [[ "$target" == "$HERMES_HOME/plugins" ]]; then
        label="default"
    else
        label=$(basename "$(dirname "$target")")
    fi
    echo "[$label] $target"

    for plugin in "${PLUGINS[@]}"; do
        src="$REPO_DIR/$plugin"
        dst="$target/$plugin"
        rsync -a \
            --exclude='__pycache__/' \
            --exclude='*.pyc' \
            "$src/" "$dst/"
        echo "  ✓ $plugin"
    done
    echo ""
done

echo "==> Done"
echo ""
echo "Restart Hermes to pick up changes:"
echo "  hermes gateway restart   # or /restart in your platform"
