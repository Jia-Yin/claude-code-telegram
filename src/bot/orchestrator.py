"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import anthropic

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("restart", self.agentic_restart),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))
        if self.settings.enable_scheduler:
            handlers.append(("schedule", self.agentic_schedule))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        # Schedule management callbacks
        if self.settings.enable_scheduler:
            app.add_handler(
                CallbackQueryHandler(
                    self._inject_deps(self._agentic_schedule_callback),
                    pattern=r"^sched:",
                )
            )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("restart", "Restart the bot process"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            if self.settings.enable_scheduler:
                commands.append(BotCommand("schedule", "管理自動排程任務"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        # Build scheduled jobs section
        scheduler = context.bot_data.get("scheduler")
        schedule_line = ""
        if scheduler is not None:
            try:
                jobs = await scheduler.list_jobs()
                if jobs:
                    lines = ["\n\n🗓 <b>自動排程任務：</b>"]
                    for job in jobs:
                        name = escape_html(job.get("job_name", "未命名"))
                        cron = escape_html(job.get("cron_expression", ""))
                        next_run = job.get("next_run_time")
                        next_str = (
                            next_run.strftime("%Y-%m-%d %H:%M %Z").strip()
                            if next_run
                            else "未排定"
                        )
                        job_id = escape_html(job.get("job_id", "")[:8])
                        lines.append(f"• <b>{name}</b> — <code>{cron}</code>")
                        lines.append(f"  ⏰ 下次執行：{next_str}　ID：<code>{job_id}</code>")
                    schedule_line = "\n".join(lines)
            except Exception:
                pass

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}"
            f"{schedule_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}"
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    async def agentic_restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Restart the bot process remotely."""
        import os
        from pathlib import Path
        from threading import Timer

        await update.message.reply_text(
            "🔄 Restarting bot...\n\n"
            "The bot will be back online in a few seconds.",
            parse_mode="HTML",
        )

        # Log the restart event
        audit_manager = context.bot_data.get("audit_manager")
        if audit_manager:
            user = update.effective_user
            await audit_manager.log_event(
                user_id=user.id if user else 0,
                event_type="restart",
                details={"triggered_by": user.username if user else "unknown"},
            )

        # Log restart request
        logger.info(
            "restart_requested",
            user_id=update.effective_user.id if update.effective_user else None,
            username=update.effective_user.username if update.effective_user else None,
        )

        # Create restart flag file
        restart_flag = Path.home() / ".claude-telegram-bot-restart"
        restart_flag.touch()

        # Hard exit after 1 second (let message send first)
        # The wrapper script or systemd will restart us
        def force_exit() -> None:
            os._exit(0)

        timer = Timer(1.0, force_exit)
        timer.daemon = True
        timer.start()

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        Returns None when verbose_level is 0 **and** no MCP image
        collection is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Capture tool calls for verbose log
            if update_obj.tool_calls and verbose_level >= 1:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    tool_log.append({"kind": "tool", "name": name, "detail": detail})

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text and verbose_level >= 1:
                    # Collapse to first meaningful line, cap length
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        tool_log.append({"kind": "text", "detail": first_line[:120]})

            # Throttle progress message edits to avoid Telegram rate limits
            if verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(new_text)
                    except Exception:
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)
        progress_msg = await update.message.reply_text("Working...")

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )

            claude_integration = context.bot_data.get("claude_integration")
            if not claude_integration:
                await progress_msg.edit_text(
                    "Claude integration not available. Check configuration."
                )
                return

            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

            # Check if /new was used — skip auto-resume for this first message.
            # Flag is only cleared after a successful run so retries keep the intent.
            force_new = bool(context.user_data.get("force_new_session"))

            verbose_level = self._get_verbose_level(context)
            tool_log: List[Dict[str, Any]] = []
            mcp_images_photo: List[ImageAttachment] = []
            on_stream = self._make_stream_callback(
                verbose_level,
                progress_msg,
                tool_log,
                time.time(),
                mcp_images=mcp_images_photo,
                approved_directory=self.settings.approved_directory,
            )

            heartbeat = self._start_typing_heartbeat(chat)
            try:
                claude_response = await claude_integration.run_command(
                    prompt=processed_image.prompt,
                    working_directory=current_dir,
                    user_id=user_id,
                    session_id=session_id,
                    on_stream=on_stream,
                    force_new=force_new,
                )
            finally:
                heartbeat.cancel()

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_photo

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )

    # --- Schedule management ---

    async def agentic_schedule(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Manage scheduled jobs via natural language.

        /schedule                       — list all active jobs
        /schedule <text>                — add a new job (AI parses time + task)
        /schedule update <id> <text>    — modify a job (AI re-parses)
        /schedule remove <id>           — remove a job by ID prefix
        """
        scheduler = context.bot_data.get("scheduler")
        if not scheduler:
            await update.message.reply_text(
                "排程功能未啟用。請設定 <code>ENABLE_SCHEDULER=true</code>。",
                parse_mode="HTML",
            )
            return

        raw_text = update.message.text or ""
        parts = raw_text.split(None, 1)
        args = parts[1].strip() if len(parts) > 1 else ""

        if not args:
            await self._schedule_list(update, context, scheduler)
            return

        lower = args.lower()
        if lower.startswith("remove ") or lower.startswith("刪除 "):
            sep = "remove " if lower.startswith("remove ") else "刪除 "
            job_id_prefix = args[len(sep):].strip()
            await self._schedule_remove_by_id(update, context, scheduler, job_id_prefix)
            return

        if lower.startswith("update ") or lower.startswith("修改 "):
            sep = "update " if lower.startswith("update ") else "修改 "
            rest = args[len(sep):].strip()
            rest_parts = rest.split(None, 1)
            if len(rest_parts) < 2:
                await update.message.reply_text(
                    "用法：<code>/schedule update &lt;ID前綴&gt; &lt;新排程描述&gt;</code>\n\n"
                    "例如：<code>/schedule update a1b2c3d4 每天晚上10點 整理今日工作日誌</code>",
                    parse_mode="HTML",
                )
                return
            job_id_prefix, new_description = rest_parts[0], rest_parts[1]
            await self._schedule_update(
                update, context, scheduler, job_id_prefix, new_description
            )
            return

        # Everything else is a natural-language add request
        await self._schedule_add(update, context, scheduler, args)

    async def _parse_schedule_nl(self, description: str) -> Dict[str, Any]:
        """Call Claude Haiku to parse natural language into a schedule definition.

        Returns a dict with: name, cron, prompt, valid (bool), error (str|None).
        """
        client = anthropic.AsyncAnthropic(
            api_key=self.settings.anthropic_api_key_str
        )
        system = (
            "You are a scheduling assistant. Parse the user's natural language schedule "
            "description into a structured JSON object. Reply with ONLY valid JSON, no markdown.\n\n"
            "Return this exact structure:\n"
            '{"name": "short descriptive job name (max 50 chars)",\n'
            ' "cron": "standard cron expression (5 fields)",\n'
            ' "prompt": "the prompt/task Claude will execute when the job fires",\n'
            ' "valid": true,\n'
            ' "error": null}\n\n'
            "Cron field order: minute hour day-of-month month day-of-week\n"
            "Common patterns:\n"
            '  daily at 9am       → "0 9 * * *"\n'
            '  weekdays at 9am    → "0 9 * * 1-5"\n'
            '  every hour         → "0 * * * *"\n'
            '  every Monday 10am  → "0 10 * * 1"\n'
            '  every 30 minutes   → "*/30 * * * *"\n\n'
            "If the time/schedule is ambiguous or missing, set valid=false and "
            "provide an error message in the same language as the user's input."
        )
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": description}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return dict(json.loads(raw.strip()))

    def _build_job_list_parts(
        self, jobs: List[Dict[str, Any]]
    ) -> "tuple[str, Optional[InlineKeyboardMarkup]]":
        """Build (html_text, keyboard) for a job list.

        Shared by _schedule_list and post-operation refreshes.
        """
        usage_hint = (
            "\n\n📖 <b>用法</b>\n"
            "<code>/schedule</code>  — 查看所有排程\n"
            "<code>/schedule &lt;描述&gt;</code>  — 新增排程（AI 解析時間與任務）\n"
            "<code>/schedule update &lt;ID&gt; &lt;描述&gt;</code>  — 修改排程\n"
            "<code>/schedule remove &lt;ID&gt;</code>  — 刪除排程\n\n"
            "範例：<code>/schedule 每天早上9點 列出今日 git 提交摘要</code>"
        )
        if not jobs:
            return (
                "目前沒有排程任務。" + usage_hint,
                None,
            )
        lines: List[str] = ["📋 <b>排程任務</b>\n"]
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        for job in jobs:
            job_id: str = job["job_id"]
            name = escape_html(job.get("job_name", "未命名"))
            cron = escape_html(job.get("cron_expression", ""))
            next_run = job.get("next_run_time")
            next_str = next_run.strftime("%m/%d %H:%M") if next_run else "未排定"
            prompt_preview = escape_html((job.get("prompt", ""))[:60])
            ellipsis = "..." if len(job.get("prompt", "")) > 60 else ""

            lines.append(f"• <b>{name}</b>")
            lines.append(f"  排程：<code>{cron}</code>　下次：{next_str}")
            lines.append(f"  任務：{prompt_preview}{ellipsis}")
            lines.append(f"  ID：<code>{escape_html(job_id[:8])}</code>\n")

            display_name = job.get("job_name", "")[:15]
            keyboard_rows.append(
                [InlineKeyboardButton(
                    f"🗑 刪除 {display_name}",
                    callback_data=f"sched:remove:{job_id}",
                )]
            )
        return "\n".join(lines).rstrip() + usage_hint, InlineKeyboardMarkup(keyboard_rows)

    async def _schedule_list(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        scheduler: Any,
    ) -> None:
        """Show all active scheduled jobs with inline remove buttons."""
        jobs = await scheduler.list_jobs()
        text, markup = self._build_job_list_parts(jobs)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _schedule_add(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        scheduler: Any,
        description: str,
    ) -> None:
        """Parse natural language description and prompt user to confirm adding a job."""
        progress = await update.message.reply_text("🔍 解析排程中...")
        try:
            parsed = await self._parse_schedule_nl(description)
        except Exception as e:
            logger.warning("Schedule NL parse failed", error=str(e))
            await progress.edit_text(
                "❌ 解析失敗，請稍後再試。", parse_mode="HTML"
            )
            return

        if not parsed.get("valid"):
            err = escape_html(str(parsed.get("error", "無法解析時間描述")))
            await progress.edit_text(
                f"❌ {err}\n\n"
                f"範例：<code>/schedule 每天早上9點 列出今日 git 提交摘要</code>",
                parse_mode="HTML",
            )
            return

        # Persist pending data for confirmation callback
        context.user_data["pending_schedule"] = {
            "name": parsed["name"],
            "cron": parsed["cron"],
            "prompt": parsed["prompt"],
            "target_chat_ids": [update.effective_chat.id],
            "created_by": update.effective_user.id,
        }

        name = escape_html(str(parsed["name"]))
        cron = escape_html(str(parsed["cron"]))
        prompt_text = str(parsed["prompt"])
        prompt_preview = escape_html(prompt_text[:120])
        ellipsis = "..." if len(prompt_text) > 120 else ""

        await progress.edit_text(
            f"📋 <b>新排程任務預覽</b>\n\n"
            f"名稱：<b>{name}</b>\n"
            f"排程：<code>{cron}</code>\n"
            f"任務：{prompt_preview}{ellipsis}\n\n"
            f"確認要新增此排程嗎？",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 確認", callback_data="sched:confirm_add"),
                InlineKeyboardButton("❌ 取消", callback_data="sched:cancel_add"),
            ]]),
        )

    async def _schedule_remove_by_id(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        scheduler: Any,
        job_id_prefix: str,
    ) -> None:
        """Remove a job matching the given ID prefix (from /schedule remove <id>)."""
        jobs = await scheduler.list_jobs()
        matched = [j for j in jobs if j["job_id"].startswith(job_id_prefix)]

        if not matched:
            await update.message.reply_text(
                f"找不到 ID 前綴為 <code>{escape_html(job_id_prefix)}</code> 的排程。\n"
                f"使用 <code>/schedule</code> 查看所有排程 ID。",
                parse_mode="HTML",
            )
            return

        if len(matched) > 1:
            await update.message.reply_text(
                f"前綴 <code>{escape_html(job_id_prefix)}</code> 符合多個排程，"
                f"請提供更長的 ID。",
                parse_mode="HTML",
            )
            return

        job = matched[0]
        try:
            await scheduler.remove_job(job["job_id"])
            deleted_name = escape_html(job.get("job_name", ""))
            remaining = await scheduler.list_jobs()
            list_text, list_markup = self._build_job_list_parts(remaining)
            await update.message.reply_text(
                f"🗑 已刪除排程：<b>{deleted_name}</b>\n\n{list_text}",
                parse_mode="HTML",
                reply_markup=list_markup,
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ 刪除失敗：{escape_html(str(e))}", parse_mode="HTML"
            )

    async def _schedule_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        scheduler: Any,
        job_id_prefix: str,
        description: str,
    ) -> None:
        """Parse new natural language description and prompt user to confirm updating a job."""
        jobs = await scheduler.list_jobs()
        matched = [j for j in jobs if j["job_id"].startswith(job_id_prefix)]

        if not matched:
            await update.message.reply_text(
                f"找不到 ID 前綴為 <code>{escape_html(job_id_prefix)}</code> 的排程。\n"
                f"使用 <code>/schedule</code> 查看所有排程 ID。",
                parse_mode="HTML",
            )
            return

        if len(matched) > 1:
            await update.message.reply_text(
                f"前綴 <code>{escape_html(job_id_prefix)}</code> 符合多個排程，"
                f"請提供更長的 ID。",
                parse_mode="HTML",
            )
            return

        old_job = matched[0]
        progress = await update.message.reply_text("🔍 解析新排程中...")
        try:
            # Provide existing job context so the parser can keep the original
            # prompt/name when the user only wants to change the schedule time.
            enriched_description = (
                f"現有排程名稱：{old_job.get('job_name', '')}\n"
                f"現有任務內容：{old_job.get('prompt', '')}\n\n"
                f"修改請求：{description}\n\n"
                f"注意：如果修改請求只提到時間變更，請保留現有的任務名稱和內容（prompt），只更新時間（cron）。"
            )
            parsed = await self._parse_schedule_nl(enriched_description)
        except Exception as e:
            logger.warning("Schedule NL parse failed", error=str(e))
            await progress.edit_text("❌ 解析失敗，請稍後再試。", parse_mode="HTML")
            return

        if not parsed.get("valid"):
            err = escape_html(str(parsed.get("error", "無法解析時間描述")))
            await progress.edit_text(
                f"❌ {err}\n\n"
                f"範例：<code>/schedule update {escape_html(job_id_prefix[:8])} 每天晚上10點 整理工作日誌</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["pending_schedule_update"] = {
            "old_job_id": old_job["job_id"],
            "old_name": old_job.get("job_name", ""),
            "old_cron": old_job.get("cron_expression", ""),
            "name": parsed["name"],
            "cron": parsed["cron"],
            "prompt": parsed["prompt"],
            "target_chat_ids": [update.effective_chat.id],
            "created_by": update.effective_user.id,
        }

        old_name = escape_html(old_job.get("job_name", ""))
        old_cron = escape_html(old_job.get("cron_expression", ""))
        new_name = escape_html(str(parsed["name"]))
        new_cron = escape_html(str(parsed["cron"]))
        prompt_text = str(parsed["prompt"])
        prompt_preview = escape_html(prompt_text[:100])
        ellipsis = "..." if len(prompt_text) > 100 else ""

        await progress.edit_text(
            f"✏️ <b>修改排程預覽</b>\n\n"
            f"原任務：<b>{old_name}</b> — <code>{old_cron}</code>\n"
            f"　　↓\n"
            f"新任務：<b>{new_name}</b> — <code>{new_cron}</code>\n"
            f"任務內容：{prompt_preview}{ellipsis}\n\n"
            f"確認修改？",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 確認修改", callback_data="sched:confirm_update"),
                InlineKeyboardButton("❌ 取消", callback_data="sched:cancel_update"),
            ]]),
        )

    async def _agentic_schedule_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle sched: inline keyboard callbacks.

        Supported actions:
          sched:confirm_add     — execute pending add, then show updated list
          sched:cancel_add      — discard pending add
          sched:confirm_update  — execute pending update (remove old + add new), show list
          sched:cancel_update   — discard pending update
          sched:remove:<id>     — remove job, edit message to show remaining list
        """
        query = update.callback_query
        await query.answer()

        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) < 2:
            return
        action = parts[1]

        scheduler = context.bot_data.get("scheduler")
        if not scheduler:
            await query.edit_message_text("排程功能未啟用。")
            return

        if action == "confirm_add":
            pending = context.user_data.pop("pending_schedule", None)
            if not pending:
                await query.edit_message_text("❌ 排程資料已過期，請重新操作。")
                return
            try:
                job_id = await scheduler.add_job(
                    job_name=pending["name"],
                    cron_expression=pending["cron"],
                    prompt=pending["prompt"],
                    target_chat_ids=pending["target_chat_ids"],
                    created_by=pending["created_by"],
                )
                name = escape_html(pending["name"])
                cron = escape_html(pending["cron"])
                await query.edit_message_text(
                    f"✅ <b>排程已新增並啟用</b>\n\n"
                    f"名稱：<b>{name}</b>\n"
                    f"排程：<code>{cron}</code>\n"
                    f"ID：<code>{escape_html(job_id[:8])}</code>",
                    parse_mode="HTML",
                )
                jobs = await scheduler.list_jobs()
                list_text, list_markup = self._build_job_list_parts(jobs)
                await query.message.reply_text(
                    list_text, parse_mode="HTML", reply_markup=list_markup
                )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ 新增失敗：{escape_html(str(e))}", parse_mode="HTML"
                )

        elif action == "cancel_add":
            context.user_data.pop("pending_schedule", None)
            await query.edit_message_text("已取消。")

        elif action == "confirm_update":
            pending = context.user_data.pop("pending_schedule_update", None)
            if not pending:
                await query.edit_message_text("❌ 排程資料已過期，請重新操作。")
                return
            try:
                await scheduler.remove_job(pending["old_job_id"])
                job_id = await scheduler.add_job(
                    job_name=pending["name"],
                    cron_expression=pending["cron"],
                    prompt=pending["prompt"],
                    target_chat_ids=pending["target_chat_ids"],
                    created_by=pending["created_by"],
                )
                old_name = escape_html(pending["old_name"])
                new_name = escape_html(pending["name"])
                new_cron = escape_html(pending["cron"])
                await query.edit_message_text(
                    f"✅ <b>排程已修改並重新啟用</b>\n\n"
                    f"原任務：<s>{old_name}</s>\n"
                    f"新名稱：<b>{new_name}</b>\n"
                    f"新排程：<code>{new_cron}</code>\n"
                    f"新 ID：<code>{escape_html(job_id[:8])}</code>",
                    parse_mode="HTML",
                )
                jobs = await scheduler.list_jobs()
                list_text, list_markup = self._build_job_list_parts(jobs)
                await query.message.reply_text(
                    list_text, parse_mode="HTML", reply_markup=list_markup
                )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ 修改失敗：{escape_html(str(e))}", parse_mode="HTML"
                )

        elif action == "cancel_update":
            context.user_data.pop("pending_schedule_update", None)
            await query.edit_message_text("已取消修改。")

        elif action == "remove" and len(parts) == 3:
            job_id = parts[2]
            try:
                await scheduler.remove_job(job_id)
                # Edit the list message in-place to reflect the removal
                remaining = await scheduler.list_jobs()
                list_text, list_markup = self._build_job_list_parts(remaining)
                await query.edit_message_text(
                    list_text, parse_mode="HTML", reply_markup=list_markup
                )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ 刪除失敗：{escape_html(str(e))}", parse_mode="HTML"
                )
