"""Offline token counting for the optional Codex hook tokenizer runtime."""

import base64
import hashlib
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Callable, Optional

TIKTOKEN_VERSION = "0.12.0"
ENCODING_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
# o200k_base pattern from tiktoken 0.12.0 (MIT). Keep this and the pinned
# dependency/asset in get-codex synchronized when changing tokenizer versions.
O200K_PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)


@dataclass(frozen=True)
class TokenBudget:
    count: Callable[[str], int]
    method: str


def _count_utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def load_token_budget(asset_path: Optional[Path] = None) -> TokenBudget:
    """Load only verified local encoding bytes, otherwise use a byte bound.

    Never call get_encoding or tiktoken's remote loader from an ephemeral
    hook. A missing or corrupt asset must not trigger a network download.
    """
    fallback = TokenBudget(_count_utf8_bytes, "utf8-byte-upper-bound")
    if asset_path is None:
        asset_path = Path(__file__).resolve().parents[2] / "assets" / "o200k_base.tiktoken"
    try:
        if version("tiktoken") != TIKTOKEN_VERSION:
            return fallback
        contents = asset_path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != ENCODING_SHA256:
            return fallback
        import tiktoken

        ranks = {
            base64.b64decode(token): int(rank)
            for token, rank in (line.split() for line in contents.splitlines() if line)
        }
        encoding = tiktoken.Encoding(
            name="o200k_base",
            pat_str=O200K_PATTERN,
            mergeable_ranks=ranks,
            special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
        )
    except (ImportError, OSError, ValueError):
        return fallback

    def count_tokens(text: str) -> int:
        return len(encoding.encode_ordinary(text))

    return TokenBudget(count_tokens, "o200k_base")
