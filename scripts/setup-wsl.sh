#!/usr/bin/env bash
set -euo pipefail

echo "=== Hermes Finance Agent — WSL2 Environment Setup ==="
echo ""

# Update package list
sudo apt update

# Python 3.11+
if command -v python3.11 &>/dev/null; then
    echo "Python 3.11 already installed: $(python3.11 --version)"
else
    echo "Installing Python 3.11..."
    sudo apt install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt update
    sudo apt install -y python3.11 python3.11-venv python3.11-dev
fi

# pip
if ! command -v pip3 &>/dev/null; then
    echo "Installing pip..."
    sudo apt install -y python3-pip
fi

# Node.js 18+
if command -v node &>/dev/null; then
    NODE_VER=$(node --version | grep -oP '\d+' | head -1)
    if [ "$NODE_VER" -ge 18 ]; then
        echo "Node.js already installed: $(node --version)"
    else
        echo "Node.js version too old, upgrading..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt install -y nodejs
    fi
else
    echo "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt install -y nodejs
fi

# npm
if ! command -v npm &>/dev/null; then
    sudo apt install -y npm
fi

# uv (Python package manager)
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Clone hermes-agent if not present
HERMES_DIR="$HOME/hermes-agent"
if [ -d "$HERMES_DIR" ]; then
    echo "hermes-agent already cloned at $HERMES_DIR"
else
    echo "Cloning hermes-agent..."
    git clone https://github.com/alhazjm/hermes-agent.git "$HERMES_DIR"
fi

# Install hermes-agent with all extras
echo "Installing hermes-agent dependencies..."
cd "$HERMES_DIR"
uv venv venv --python 3.11 2>/dev/null || true
source venv/bin/activate
uv pip install -e ".[all]"

# Install additional deps for expense tracker
uv pip install gspread google-auth

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Run: bash hermes-config/install.sh"
echo "  2. Run: bash scripts/link-tools.sh"
echo "  3. Edit ~/.hermes/.env with your API keys"
echo "  4. Run: hermes setup"
