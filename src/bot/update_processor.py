"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially -- one at a time.
Priority interrupts (stop:* callbacks and /cancel commands) bypass the queue
and run immediately so they can interrupt the currently-running handler.
"""

import asyncio
from typing import Any, Awaitable

from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor that lets priority interrupts bypass sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority interrupts (``stop:*`` and ``/cancel``): we just
    ``await coroutine`` -- runs immediately.
    For everything else: we acquire ``_sequential_lock`` first -- only one
    runs at a time.

    A stop callback arrives while a text handler holds the lock -> stop
    callback runs concurrently -> fires the ``asyncio.Event`` -> the watcher
    task inside ``execute_command()`` calls ``client.interrupt()`` -> Claude
    stops -> ``run_command()`` returns -> handler finishes -> lock released.
    """

    _PRIORITY_PREFIXES = ("stop:",)
    _PRIORITY_COMMANDS = ("/cancel",)

    def __init__(self) -> None:
        # High limit so priority callbacks are never blocked by semaphore
        super().__init__(max_concurrent_updates=256)
        self._sequential_lock = asyncio.Lock()

    @classmethod
    def _is_priority_callback(cls, update: object) -> bool:
        """Return True if the update is a priority callback query."""
        if not isinstance(update, Update):
            return False
        cb = update.callback_query
        return (
            cb is not None
            and cb.data is not None
            and cb.data.startswith(cls._PRIORITY_PREFIXES)
        )

    @classmethod
    def _is_priority_command(cls, update: object) -> bool:
        """Return True if the update is a priority command message."""
        if not isinstance(update, Update):
            return False

        message = update.effective_message
        text = getattr(message, "text", None)
        if not isinstance(text, str) or not text:
            return False

        normalized = text.strip().split(None, 1)[0]
        return any(
            normalized == command or normalized.startswith(f"{command}@")
            for command in cls._PRIORITY_COMMANDS
        )

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        """Process an update, applying sequential lock for non-priority updates."""
        if self._is_priority_callback(update) or self._is_priority_command(update):
            # Run immediately -- no sequential lock
            await coroutine
        else:
            # One at a time for everything else
            async with self._sequential_lock:
                await coroutine

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
