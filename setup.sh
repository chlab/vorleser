#!/usr/bin/env bash
# Sets up ebook2audiobook alongside this repo and applies the vorleser patches.
# Run once per machine. Safe to re-run — skips steps already done.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2A_DIR="$(dirname "$SCRIPT_DIR")/ebook2audiobook"

echo "=== vorleser setup ==="
echo "ebook2audiobook target: $E2A_DIR"
echo

# 1. Clone ebook2audiobook if not present
if [[ ! -d "$E2A_DIR" ]]; then
  echo "Cloning ebook2audiobook…"
  git clone https://github.com/DrewThomasson/ebook2audiobook.git "$E2A_DIR"
else
  echo "ebook2audiobook already cloned — skipping"
fi

# 2. Apply patches
echo
echo "Applying core.py patches…"
cd "$E2A_DIR"
if git apply --check "$SCRIPT_DIR/core.patch" 2>/dev/null; then
  git apply "$SCRIPT_DIR/core.patch"
  echo "Patches applied."
elif git apply --reverse --check "$SCRIPT_DIR/core.patch" 2>/dev/null; then
  echo "Patches already applied — skipping"
else
  echo "WARNING: patch did not apply cleanly. ebook2audiobook may have been updated."
  echo "Apply manually: cd $E2A_DIR && git apply $SCRIPT_DIR/core.patch"
fi

# 3. Bootstrap ebook2audiobook (installs python_env and dependencies)
echo
echo "Bootstrapping ebook2audiobook (this takes a while on first run)…"
cd "$E2A_DIR"
bash ebook2audiobook.command --headless --help > /dev/null 2>&1 || true
echo
echo "=== Setup complete ==="
echo
echo "Next steps:"
echo "  1. Drop your epub into:        $SCRIPT_DIR/ebooks/"
echo "  2. Drop a voice sample into:   $SCRIPT_DIR/voices/  (WAV, 30-60s recommended)"
echo "  3. Run:  python3 prepare_book.py ebooks/<book>.epub"
echo "  4. Convert — see README.md for the full command"
