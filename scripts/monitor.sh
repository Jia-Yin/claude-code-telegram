#!/bin/bash
# Bot process monitor - auto-restart if crashed
# Add to crontab: */5 * * * * /path/to/monitor.sh

set -e

BOT_DIR="/home/jyw/workspace/claude-code-telegram"
PIDFILE="$BOT_DIR/.bot.pid"
LOGFILE="$BOT_DIR/logs/monitor.log"
TMUX_SESSION="claude-bot"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"
}

# Check if bot is running
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    log "Bot is running in tmux session '$TMUX_SESSION'"
    exit 0
fi

log "Bot not running! Restarting..."

# Start bot in tmux
cd "$BOT_DIR"
tmux new-session -d -s "$TMUX_SESSION" 'uv run claude-telegram-bot'

log "Bot restarted in tmux session '$TMUX_SESSION'"
