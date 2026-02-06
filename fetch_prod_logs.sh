#!/bin/bash

# Script to fetch production logs from remote server
# Usage: ./fetch_prod_logs.sh

set -e  # Exit on any error

# Configuration
REMOTE_HOST="my-droplet"
REMOTE_LOGS_PATH="/root/DraftBot/logs"
LOCAL_LOGS_DIR="./prod_logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FETCH_DIR="$LOCAL_LOGS_DIR/$TIMESTAMP"
NUM_FILES=5  # Number of most recent log files to fetch

echo "🔄 Fetching production logs from remote server..."

# Create prod_logs directory if it doesn't exist
if [ ! -d "$LOCAL_LOGS_DIR" ]; then
    echo "📁 Creating prod_logs directory..."
    mkdir -p "$LOCAL_LOGS_DIR"
fi

# Create timestamped directory for this fetch
echo "📁 Creating directory for this fetch: $FETCH_DIR"
mkdir -p "$FETCH_DIR"

# Get list of most recent log files from remote server
echo "🔍 Finding $NUM_FILES most recent log files on $REMOTE_HOST..."
RECENT_FILES=$(ssh "$REMOTE_HOST" "cd $REMOTE_LOGS_PATH && ls -t | head -n $NUM_FILES")

if [ -z "$RECENT_FILES" ]; then
    echo "❌ No log files found in $REMOTE_LOGS_PATH"
    rm -rf "$FETCH_DIR"
    exit 1
fi

echo "📋 Files to download:"
echo "$RECENT_FILES"
echo ""

# Fetch the selected log files
echo "⬇️  Downloading $NUM_FILES most recent log files from $REMOTE_HOST:$REMOTE_LOGS_PATH..."
for file in $RECENT_FILES; do
    echo "  ⬇️  $file"
    scp "$REMOTE_HOST:$REMOTE_LOGS_PATH/$file" "$FETCH_DIR/"
done

# Verify the download
if [ -d "$FETCH_DIR" ] && [ "$(ls -A $FETCH_DIR)" ]; then
    echo "✅ Logs downloaded successfully!"
    echo "📊 Total size: $(du -sh $FETCH_DIR | cut -f1)"
    echo "📁 Logs stored in: $FETCH_DIR"
    echo ""
    echo "📋 Files downloaded:"
    ls -lh "$FETCH_DIR"
else
    echo "❌ Failed to download logs"
    rm -rf "$FETCH_DIR"
    exit 1
fi

echo ""
echo "🎉 Production logs fetch completed!"
echo "💡 Logs are stored in: $FETCH_DIR"
