#!/bin/bash

# Script to fetch production database from remote server
# Usage: ./fetch_prod_db.sh

set -e  # Exit on any error

# Configuration
REMOTE_HOST="my-droplet"
REMOTE_PATH="/root/DraftBot/drafts.db"
LOCAL_PATH="./drafts.db"
BACKUP_PATH="./drafts.db.backup.$(date +%Y%m%d_%H%M%S)"

echo "🔄 Fetching production database from remote server..."

# Create backup of current local database if it exists
if [ -f "$LOCAL_PATH" ]; then
    echo "📦 Backing up current local database to: $BACKUP_PATH"
    cp "$LOCAL_PATH" "$BACKUP_PATH"
    echo "✅ Backup created successfully"
else
    echo "ℹ️  No existing local database found, skipping backup"
fi

# Fetch the production database
echo "⬇️  Downloading production database from $REMOTE_HOST:$REMOTE_PATH..."
scp "$REMOTE_HOST:$REMOTE_PATH" "$LOCAL_PATH"

# Verify the download
if [ -f "$LOCAL_PATH" ]; then
    echo "✅ Database downloaded successfully!"
    echo "📊 Database size: $(du -h $LOCAL_PATH | cut -f1)"
    echo "🕒 Database last modified: $(stat -c %y $LOCAL_PATH)"
else
    echo "❌ Failed to download database"
    exit 1
fi

# Optional: Show basic database info
echo ""
echo "📋 Database info:"
sqlite3 "$LOCAL_PATH" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" | head -10

echo ""
echo "🎉 Production database fetch completed!"
echo "💡 Your previous database was backed up to: $BACKUP_PATH"