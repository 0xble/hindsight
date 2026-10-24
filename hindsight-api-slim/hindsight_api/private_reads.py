"""Task-local policy for reads that must not enter Hindsight diagnostic sinks.

This is not a global audit/tracing switch. Child tasks inherit the binding and
thread dispatch must copy it. Nothing here controls retention by remote services.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import partial, wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry import context as otel_context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import INVALID_SPAN, set_span_in_context

_private_read: ContextVar[bool] = ContextVar("hindsight_private_read", default=False)
P = ParamSpec("P")
T = TypeVar("T")


class PrivateReadUnsupportedError(RuntimeError):
    """A configured component cannot contain a private read's side effects."""


def is_private_read() -> bool:
    return _private_read.get()


@contextmanager
def private_read_scope() -> Iterator[None]:
    """Bind no-store through cancellation, nested calls, and inherited tasks.

    Mask an inherited recording span as well as suppressing SDK instrumentation,
    so a provider cannot attach an event to an unrelated caller's public span.
    """
    token = _private_read.set(True)
    context = otel_context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True)
    otel_token = otel_context.attach(set_span_in_context(INVALID_SPAN, context))
    try:
        yield
    finally:
        otel_context.detach(otel_token)
        _private_read.reset(token)


async def run_in_executor(executor: Executor | None, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Dispatch existing CPU work with its request policy, including after cancellation.

    Process executors cannot copy ContextVars and are not an eligible private
    boundary. Reject before submitting work, rather than leaking into a worker.
    """
    work = partial(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    if executor is not None and type(executor) is not ThreadPoolExecutor:
        if is_private_read():
            raise PrivateReadUnsupportedError("Private reads require an in-process executor")
        return await loop.run_in_executor(executor, work)
    return await loop.run_in_executor(executor, copy_context().run, work)


# A handler/root-logger filter is too late: QueueHandler.prepare already formats
# and copies exception text, and ancestor filters do not cover child loggers.
# Install once at the producer, before LogRecord creation, filters, or handlers.
# The wrapper consults only task-local state and preserves public logging config.
_original_log = logging.Logger._log
_original_handle = logging.Logger.handle


@wraps(_original_log)
def _private_log(
    self: logging.Logger,
    level: int,
    msg: object,
    args: Any,
    exc_info: Any = None,
    extra: Any = None,
    stack_info: bool = False,
    stacklevel: int = 1,
) -> None:
    if is_private_read():
        return
    _original_log(self, level, msg, args, exc_info, extra, stack_info, stacklevel + 1)


@wraps(_original_handle)
def _private_handle(self: logging.Logger, record: logging.LogRecord) -> None:
    # Also contain libraries that forward a pre-built LogRecord to a logger.
    if not is_private_read():
        _original_handle(self, record)


logging.Logger._log = _private_log
logging.Logger.handle = _private_handle
