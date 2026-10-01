"""Bound the complete Codex memory injection without external dependencies."""

from dataclasses import dataclass

from lib.token_budget import load_token_budget


@dataclass(frozen=True)
class RecallContext:
    context: str = ""
    result_count: int = 0
    token_upper_bound: int = 0
    counting_method: str = ""


def build_recall_context(memory_blocks: list[str], preamble: str, current_time: str, max_tokens: int) -> RecallContext:
    """Pack whole ranked facts under the complete injection token budget.

    Prefer the managed offline o200k tokenizer. Standalone python3 can still
    run without it, using a conservative UTF-8 byte bound for byte-BPE tokens.
    Previously the API budget covered fact text only, allowing the preamble,
    tags, dates and wrappers to overflow the configured injection budget.
    """
    if type(max_tokens) is not int or max_tokens <= 0:
        return RecallContext()

    prefix = f"<hindsight_memories>\n{preamble}\nCurrent time - {current_time}\n\n"
    suffix = "\n</hindsight_memories>"
    budget = load_token_budget()
    if budget.count(prefix + suffix) >= max_tokens:
        return RecallContext()

    kept: list[str] = []
    for block in memory_blocks:
        candidate = prefix + "\n\n".join([*kept, block]) + suffix
        # BPE can merge across separators, so count the complete candidate
        # rather than adding independently tokenized fact and wrapper counts.
        if budget.count(candidate) > max_tokens:
            # Never cut off a fact's qualification or attribution. A later,
            # smaller fact can still fit, with the surviving order preserved.
            continue
        kept.append(block)

    if not kept:
        return RecallContext()
    context = prefix + "\n\n".join(kept) + suffix
    return RecallContext(context, len(kept), budget.count(context), budget.method)
