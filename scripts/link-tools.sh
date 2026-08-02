#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HERMES_DIR="$HOME/hermes-agent"

echo "=== Linking custom tools into Hermes ==="

if [ ! -d "$HERMES_DIR" ]; then
    echo "Error: hermes-agent not found at $HERMES_DIR"
    echo "Run setup-wsl.sh first."
    exit 1
fi

TOOLS_DIR="$HERMES_DIR/tools"

# Symlink sheets_client.py
if [ -L "$TOOLS_DIR/sheets_client.py" ] || [ -f "$TOOLS_DIR/sheets_client.py" ]; then
    echo "sheets_client.py already exists — updating symlink"
    rm -f "$TOOLS_DIR/sheets_client.py"
fi
ln -s "$PROJECT_DIR/tools/sheets_client.py" "$TOOLS_DIR/sheets_client.py"
echo "Linked: sheets_client.py → $TOOLS_DIR/"

# Symlink expense_sheets_tool.py
if [ -L "$TOOLS_DIR/expense_sheets_tool.py" ] || [ -f "$TOOLS_DIR/expense_sheets_tool.py" ]; then
    echo "expense_sheets_tool.py already exists — updating symlink"
    rm -f "$TOOLS_DIR/expense_sheets_tool.py"
fi
ln -s "$PROJECT_DIR/tools/expense_sheets_tool.py" "$TOOLS_DIR/expense_sheets_tool.py"
echo "Linked: expense_sheets_tool.py → $TOOLS_DIR/"

# Add import to model_tools.py if not already present
MODEL_TOOLS="$HERMES_DIR/model_tools.py"
IMPORT_LINE="import tools.expense_sheets_tool  # expense tracker custom tools"

if grep -q "expense_sheets_tool" "$MODEL_TOOLS" 2>/dev/null; then
    echo "Import already present in model_tools.py"
else
    echo "" >> "$MODEL_TOOLS"
    echo "$IMPORT_LINE" >> "$MODEL_TOOLS"
    echo "Added import to model_tools.py"
fi

echo ""
echo "=== Done ==="
echo "Custom tools are now available to Hermes."
echo "Verify with: hermes tools"
