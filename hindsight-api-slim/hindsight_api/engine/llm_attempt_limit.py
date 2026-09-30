"""Internal, opt-in single-completion guard for changed-prompt corrections.

max_retries=0 alone is insufficient: OAuth providers can retry after refreshing
credentials without consuming their normal retry budget. Gate the actual attempt
boundary too, without changing normal provider/authentication policy.
"""

from asyncio import CancelledError
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .llm_interface import ProviderRateLimitResetError


class CompletionAttemptLimitError(RuntimeError):
    retryable = False


@dataclass
class _SingleCompletion:
    on_start: Callable[[], None]
    started: bool = False
    error: BaseException | None = None

    def start(self) -> None:
        if self.started:
            # Preserve control signals (especially auth/quota) if the provider
            # tries a hidden retry. Never convert them into a batch failure.
            if self.error is not None:
                raise self.error
            raise CompletionAttemptLimitError("correction permits only one completion")
        self.on_start()
        self.started = True


_limit: ContextVar[_SingleCompletion | None] = ContextVar("hindsight_single_completion", default=None)


@contextmanager
def single_completion(on_start: Callable[[], None]) -> Iterator[_SingleCompletion]:
    limit = _SingleCompletion(on_start)
    token = _limit.set(limit)
    try:
        try:
            yield limit
        except CancelledError:
            # Cancellation during reactive refresh must win over a stored auth
            # error; the worker owns cancellation and must see the exact signal.
            raise
        except BaseException:
            # Some providers wrap auth errors after reactive refresh. Preserve the
            # original HTTP signal rather than letting that wrapper trigger bisection.
            if limit.error is not None and (
                isinstance(limit.error, ProviderRateLimitResetError)
                or getattr(limit.error, "status_code", None) in (401, 403)
            ):
                raise limit.error
            raise
    finally:
        _limit.reset(token)


def completion_limit_active() -> bool:
    return _limit.get() is not None


@contextmanager
def completion_attempt() -> Iterator[None]:
    limit = _limit.get()
    if limit is not None:
        limit.start()
    try:
        yield
    except BaseException as exc:
        if limit is not None:
            limit.error = exc
        raise
