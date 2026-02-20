#!/bin/bash
# Quick systemd installation script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_FILE="$SCRIPT_DIR/claude-telegram-bot.service"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="claude-telegram-bot.service"

echo "=== Claude Telegram Bot - Systemd Installation ==="
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "❌ Please do NOT run this script as root!"
    echo "   Run as your normal user. It will prompt for sudo when needed."
    exit 1
fi

# Check if service file exists
if [ ! -f "$SERVICE_FILE" ]; then
    echo "❌ Service file not found: $SERVICE_FILE"
    exit 1
fi

# Get current user
CURRENT_USER=$(whoami)
CURRENT_HOME=$HOME

echo "Current user: $CURRENT_USER"
echo "Home directory: $CURRENT_HOME"
echo "Project directory: $PROJECT_DIR"
echo ""

# Create temporary service file with replaced variables
TEMP_SERVICE="/tmp/claude-telegram-bot.service.tmp"
sed -e "s|%YOUR_USERNAME%|$CURRENT_USER|g" \
    -e "s|/home/jyw|$CURRENT_HOME|g" \
    -e "s|/home/jyw/workspace/claude-code-telegram|$PROJECT_DIR|g" \
    "$SERVICE_FILE" > "$TEMP_SERVICE"

echo "Generated service file:"
echo "---"
cat "$TEMP_SERVICE"
echo "---"
echo ""

read -p "Install this service? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled."
    rm "$TEMP_SERVICE"
    exit 0
fi

# Install service
echo ""
echo "Installing service (requires sudo)..."
sudo cp "$TEMP_SERVICE" "$SYSTEMD_DIR/$SERVICE_NAME"
rm "$TEMP_SERVICE"

# Reload systemd
echo "Reloading systemd..."
sudo systemctl daemon-reload

# Enable service
echo "Enabling service to start on boot..."
sudo systemctl enable "$SERVICE_NAME"

# Ask if user wants to start now
echo ""
read -p "Start the service now? (Y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    echo "Starting service..."
    sudo systemctl start "$SERVICE_NAME"
    sleep 2
    echo ""
    echo "Service status:"
    sudo systemctl status "$SERVICE_NAME" --no-pager
else
    echo "Service installed but not started."
    echo "To start manually, run: sudo systemctl start $SERVICE_NAME"
fi

echo ""
echo "✅ Installation complete!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status claude-telegram-bot    # Check status"
echo "  sudo systemctl restart claude-telegram-bot   # Restart"
echo "  sudo journalctl -u claude-telegram-bot -f    # View logs"
echo "  make systemd-restart                         # Quick restart (via Makefile)"
