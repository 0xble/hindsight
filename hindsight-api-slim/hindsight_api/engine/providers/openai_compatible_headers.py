"""Shared HTTP headers for Hindsight's OpenAI-compatible transports."""

from hindsight_api import __version__

OPENAI_COMPATIBLE_USER_AGENT = f"hindsight/openai-compatible/{__version__}"


def with_openai_compatible_user_agent(default_headers: dict[str, str] | None) -> dict[str, str]:
    """Return client headers with a single canonical User-Agent key.

    Preserve an operator's case-insensitive override, otherwise use Hindsight's
    transport identity. Canonical spelling replaces the SDK's default value.
    """
    headers = dict(default_headers or {})
    # The SDK merges these with a case-sensitive dict containing User-Agent
    # before constructing HTTPX headers. Canonicalize an operator override so
    # it replaces the SDK value instead of producing duplicate wire headers.
    user_agent = OPENAI_COMPATIBLE_USER_AGENT
    for name in list(headers):
        if name.lower() == "user-agent":
            user_agent = headers.pop(name)
    headers["User-Agent"] = user_agent
    return headers
