#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN="$HOME/.local/bin"

mkdir -p "$LOCAL_BIN"

# 1. Install main agy-multi wrapper
cat << EOF > "$LOCAL_BIN/agy-multi"
#!/usr/bin/env bash
if [ -f "\$HOME/.config/agy-multi/env" ]; then
    set -a
    source "\$HOME/.config/agy-multi/env"
    set +a
fi
SCRIPT_DIR="$SCRIPT_DIR"
PYTHONPATH="\$SCRIPT_DIR:\$PYTHONPATH" exec python3 -m agy_multi.cli "\$@"
EOF

chmod +x "$LOCAL_BIN/agy-multi"
echo "✓ Installed $LOCAL_BIN/agy-multi"

# 1.1 Clean up legacy gemini-switch command if present
if [ -f "$LOCAL_BIN/gemini-switch" ]; then
    rm -f "$LOCAL_BIN/gemini-switch"
    echo "✓ Cleaned up deprecated $LOCAL_BIN/gemini-switch"
fi

# 2. Run agy-multi install to generate/update shortcuts
"$LOCAL_BIN/agy-multi" install

# 3. Auto-discover Google Antigravity OAuth client credentials for 24/7 background token refresh
echo "=== Configuring background token refresh credentials ==="
"$LOCAL_BIN/agy-multi" creds --save || true

echo "=== agy-multi installation complete! ==="

