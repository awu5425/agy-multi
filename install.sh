#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN="$HOME/.local/bin"

mkdir -p "$LOCAL_BIN"

# 1. Install main agy-multi wrapper
cat << EOF > "$LOCAL_BIN/agy-multi"
#!/usr/bin/env bash
SCRIPT_DIR="$SCRIPT_DIR"
PYTHONPATH="\$SCRIPT_DIR:\$PYTHONPATH" exec python3 -m agy_multi.cli "\$@"
EOF

chmod +x "$LOCAL_BIN/agy-multi"
echo "✓ Installed $LOCAL_BIN/agy-multi"

# 1.1 Install backward-compatible gemini-switch alias
cat << EOF > "$LOCAL_BIN/gemini-switch"
#!/usr/bin/env bash
exec "$LOCAL_BIN/agy-multi" "\$@"
EOF

chmod +x "$LOCAL_BIN/gemini-switch"
echo "✓ Installed alias: $LOCAL_BIN/gemini-switch -> agy-multi"

# 2. Run agy-multi install to generate/update shortcuts
"$LOCAL_BIN/agy-multi" install

echo "=== agy-multi installation complete! ==="
