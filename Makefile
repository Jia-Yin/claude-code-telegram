.PHONY: install dev test lint format clean help run run-remote remote-attach remote-stop
.PHONY: systemd-install systemd-start systemd-stop systemd-restart systemd-status systemd-logs

# Default target
help:
	@echo "Available commands:"
	@echo "  install    - Install production dependencies"
	@echo "  dev        - Install development dependencies"
	@echo "  test       - Run tests"
	@echo "  lint       - Run linting checks"
	@echo "  format     - Format code"
	@echo "  clean      - Clean up generated files"
	@echo "  run        - Run the bot"
	@echo "  run-loop   - Run with auto-restart (ensures code reload)"
	@echo "  run-remote - Start bot in tmux on remote Mac (unlocks keychain)"
	@echo "  remote-attach - Attach to running bot tmux session"
	@echo "  remote-stop   - Stop the bot tmux session"
	@echo ""
	@echo "Systemd deployment (Linux):"
	@echo "  systemd-install - Install systemd service"
	@echo "  systemd-start   - Start bot service"
	@echo "  systemd-stop    - Stop bot service"
	@echo "  systemd-restart - Restart bot service"
	@echo "  systemd-status  - Show service status"
	@echo "  systemd-logs    - Show service logs (follow)"

install:
	uv sync

dev:
	uv sync --extra dev
	uv run pre-commit install --install-hooks || echo "pre-commit not configured yet"

test:
	uv run pytest

lint:
	uv run black --check src tests
	uv run isort --check-only src tests
	uv run flake8 src tests
	uv run mypy src

format:
	uv run black src tests
	uv run isort src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run:
	uv run claude-telegram-bot

# For debugging
run-debug:
	uv run claude-telegram-bot --debug

# Run with auto-restart wrapper (ensures code reload)
run-loop:
	./scripts/run-bot-loop.sh

# Remote Mac Mini (SSH session)
run-remote:  ## Start bot on remote Mac in tmux (persists after SSH disconnect)
	security unlock-keychain ~/Library/Keychains/login.keychain-db
	tmux new-session -d -s claude-bot 'uv run claude-telegram-bot'
	@echo "Bot started in tmux session 'claude-bot'"
	@echo "  Attach: make remote-attach"
	@echo "  Stop:   make remote-stop"

remote-attach:  ## Attach to running bot tmux session
	tmux attach -t claude-bot

remote-stop:  ## Stop the bot tmux session
	tmux kill-session -t claude-bot

# Systemd deployment (Linux)
systemd-install:  ## Install systemd service (requires sudo)
	@echo "Installing systemd service..."
	@if [ ! -f scripts/claude-telegram-bot.service ]; then \
		echo "Error: scripts/claude-telegram-bot.service not found!"; \
		exit 1; \
	fi
	@echo "Please edit scripts/claude-telegram-bot.service to set your username first!"
	@echo "Then run: sudo cp scripts/claude-telegram-bot.service /etc/systemd/system/"
	@echo "         sudo systemctl daemon-reload"
	@echo "         sudo systemctl enable claude-telegram-bot"

systemd-start:  ## Start bot service
	sudo systemctl start claude-telegram-bot
	@echo "Bot started. Check status with: make systemd-status"

systemd-stop:  ## Stop bot service
	sudo systemctl stop claude-telegram-bot

systemd-restart:  ## Restart bot service
	sudo systemctl restart claude-telegram-bot
	@echo "Bot restarted. Check status with: make systemd-status"

systemd-status:  ## Show service status
	sudo systemctl status claude-telegram-bot

systemd-logs:  ## Show and follow service logs
	sudo journalctl -u claude-telegram-bot -f
