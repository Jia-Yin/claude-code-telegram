#!/bin/bash
# Wrapper script - keeps restarting bot when it exits
# Ensures new code is always loaded

set -e

cd "$(dirname "$0")/.."

echo "🤖 Bot wrapper started (PID: $$)"
echo "   Press Ctrl+C to stop completely"
echo ""

while true; do
    echo "▶️  Starting bot..."

    # Run bot and capture exit code
    uv run claude-telegram-bot || EXIT_CODE=$?

    # If exit code is 0, it's a normal shutdown (not a restart request)
    if [ "${EXIT_CODE:-0}" -eq 0 ]; then
        # Check if restart was requested (flag file exists)
        if [ -f "$HOME/.claude-telegram-bot-restart" ]; then
            rm -f "$HOME/.claude-telegram-bot-restart"
            echo "🔄 Restart requested, reloading..."
            sleep 1
            continue
        else
            echo "✅ Bot stopped normally"
            break
        fi
    else
        echo "❌ Bot crashed with exit code $EXIT_CODE"
        echo "⏳ Waiting 5 seconds before restart..."
        sleep 5
    fi
done

echo "👋 Bot wrapper exited"
