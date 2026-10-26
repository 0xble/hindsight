"""Pure ASGI private-read boundary, outside parsing, dependencies and diagnostics."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..engine.private_reads import check_private_read_extensions
from ..private_reads import PrivateReadUnsupportedError, private_read_scope

if TYPE_CHECKING:
    from ..engine.memory_engine import MemoryEngine

# Keep decoded bank IDs within 1 nonempty path segment. A slash-spanning bank
# match misclassified public document IDs as private and suppressed their audit.
# Allow mount prefixes, but never consume an earlier bank API namespace in one:
# document IDs can themselves contain an entire private-looking API path.
PRIVATE_READ_PATH = re.compile(
    r"^(?:(?!/v1/default/banks/).)*/v1/default/banks/[^/]+/private/(?:recall|reflect)/?\Z", re.DOTALL
)
# OTel's FastAPI instrumentor wraps the *whole* Starlette stack, not just user
# middleware. Exclude this path there too, regardless of operator exclusions.
# Keep DOTALL scoped to this branch: OTel joins operator exclusions and this
# expression with '|', where an unscoped '(?s)' is invalid after another branch.
PRIVATE_READ_TRACE_EXCLUSION = (
    r"(?s:^(?:(?!/v1/default/banks/)[^?])*/v1/default/banks/[^/]+/private/(?:recall|reflect)/?(?:\?.*)?\Z)"
)


class PrivateReadMiddleware:
    def __init__(self, app: ASGIApp, memory: MemoryEngine, http_extension: bool = False) -> None:
        self.app = app
        self.memory = memory
        self.http_extension = http_extension

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or PRIVATE_READ_PATH.search(scope["path"]) is None:
            await self.app(scope, receive, send)
            return

        with private_read_scope():
            started = False

            async def private_send(message: Message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    headers = list(message.get("headers", []))
                    headers.append((b"cache-control", b"no-store"))
                    if 200 <= message["status"] < 300:
                        headers.append((b"x-hindsight-private-read", b"1"))
                    message = {**message, "headers": headers}
                await send(message)

            try:
                if self.http_extension:
                    raise PrivateReadUnsupportedError("HTTP extensions do not support private reads")
                check_private_read_extensions(self.memory)
                await self.app(scope, receive, private_send)
            except PrivateReadUnsupportedError:
                if not started:
                    await JSONResponse(
                        {"detail": "Configured component does not support private reads"}, status_code=503
                    )(scope, receive, private_send)
            except asyncio.CancelledError:
                # A cancellation reason can itself contain a provider error or
                # query. Do not hand that reason to the outer server logger.
                raise asyncio.CancelledError() from None
            except Exception:
                # Do not re-raise query-bearing errors into the outer server's
                # exception logger after our task-local scope has unwound. The
                # native handlers still own their normal HTTP error responses.
                if not started:
                    await JSONResponse({"detail": "Private read failed"}, status_code=500)(scope, receive, private_send)
