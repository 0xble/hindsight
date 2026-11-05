"""Transport-only retry classification shared by file parsing boundaries."""


class FileParserTransportError(RuntimeError):
    """A sanitized transport failure returned by an isolated parser worker."""


def is_retryable_file_transport_error(error: BaseException) -> bool:
    """Find transport failures through parser wrappers, without retrying bad input.

    MarkItDown aggregates converter exceptions in attempts[].exc_info rather than
    always chaining them. Never infer retryability from its human-facing message.
    """
    import ssl

    import httpx  # noqa: TID251 -- classify transport errors from the OpenAI OCR SDK
    from openai import APIConnectionError

    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(
            current,
            (
                APIConnectionError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                ssl.SSLError,
                ConnectionError,
                TimeoutError,
                FileParserTransportError,
            ),
        ):
            return True
        # Follow the explicit cause in preference to an incidental context, just
        # as the terminal-metadata classifier does.
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        # Inspect the optional parser's public error shape without importing
        # markitdown (other parser installations need not include that package).
        if type(current).__name__ == "FileConversionException":
            for attempt in getattr(current, "attempts", None) or []:
                if attempt.exc_info is not None and isinstance(attempt.exc_info[1], BaseException):
                    pending.append(attempt.exc_info[1])
    return False
