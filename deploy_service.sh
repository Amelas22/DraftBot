#!/bin/bash

# Script to deploy DraftBot systemd service
# Usage: ./deploy_service.sh

set -e  # Exit on any error

SERVICE_NAME="draftbot.service"
SERVICE_FILE="systemd/$SERVICE_NAME"
SYSTEMD_DIR="/etc/systemd/system"
SYSTEMD_SERVICE="$SYSTEMD_DIR/$SERVICE_NAME"

echo "🚀 Deploying DraftBot systemd service..."

# Check if we're running as root or with sudo
if [[ $EUID -ne 0 ]]; then
    echo "❌ This script must be run as root or with sudo"
    echo "💡 Usage: sudo ./deploy_service.sh"
    exit 1
fi

# Check if service file exists in repo
if [[ ! -f "$SERVICE_FILE" ]]; then
    echo "❌ Service file not found: $SERVICE_FILE"
    echo "💡 Make sure you're running this from the DraftBot repository root"
    exit 1
fi

# Check if service is currently running
if systemctl is-active --quiet $SERVICE_NAME 2>/dev/null; then
    echo "⏸️  Stopping existing service..."
    systemctl stop $SERVICE_NAME
    SERVICE_WAS_RUNNING=true
else
    SERVICE_WAS_RUNNING=false
fi

# Copy service file
echo "📁 Copying service file to $SYSTEMD_DIR..."
cp "$SERVICE_FILE" "$SYSTEMD_SERVICE"

# Set proper permissions
chmod 644 "$SYSTEMD_SERVICE"

# Reload systemd
echo "🔄 Reloading systemd daemon..."
systemctl daemon-reload

# Enable service if not already enabled
if ! systemctl is-enabled --quiet $SERVICE_NAME 2>/dev/null; then
    echo "✅ Enabling service..."
    systemctl enable $SERVICE_NAME
else
    echo "ℹ️  Service already enabled"
fi

# Start service
if [[ "$SERVICE_WAS_RUNNING" == "true" ]] || systemctl is-enabled --quiet $SERVICE_NAME; then
    echo "▶️  Starting service..."
    systemctl start $SERVICE_NAME
    
    # Wait a moment and check status
    sleep 2
    if systemctl is-active --quiet $SERVICE_NAME; then
        echo "✅ Service deployed and started successfully!"
    else
        echo "⚠️  Service deployed but may have failed to start"
        echo "📋 Check status with: sudo systemctl status $SERVICE_NAME"
        echo "📋 Check logs with: sudo journalctl -u $SERVICE_NAME"
    fi
else
    echo "ℹ️  Service deployed but not started (was not previously running)"
    echo "💡 Start with: sudo systemctl start $SERVICE_NAME"
fi

echo ""
echo "🎉 Deployment complete!"
echo ""
echo "📋 Useful commands:"
echo "   sudo systemctl status $SERVICE_NAME     # Check status"
echo "   sudo systemctl restart $SERVICE_NAME    # Restart service" 
echo "   sudo journalctl -u $SERVICE_NAME -f     # Follow logs"