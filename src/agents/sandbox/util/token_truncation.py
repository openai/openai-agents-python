from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import tiktoken

APPROX_BYTES_PER_TOKEN = 4

TruncationMode = Literal["bytes", "tokens"]


@dataclass(frozen=True)
class TruncationPolicy:
    mode: TruncationMode
    limit: int

    @classmethod
    def bytes(cls, limit: int) -> TruncationPolicy:
        return cls(mode="bytes", limit=max(0, limit))

    @classmethod
    def tokens(cls, limit: int) -> TruncationPolicy:
        return cls(mode="tokens", limit=max(0, limit))

    def token_budget(self) -> int:
        if self.mode == "bytes":
            return int(approx_tokens_from_byte_count(self.limit))
        return self.limit

    def byte_budget(self) -> int:
        if self.mode == "bytes":
            return self.limit
        return approx_bytes_for_tokens(self.limit)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def formatted_truncate_text(content: str, policy: TruncationPolicy) -> str:
    if _byte_len(content) <= policy.byte_budget():
        return content
    total_lines = len(content.splitlines())
    result = truncate_text(content, policy)
    return f"Total output lines: {total_lines}\n\n{result}"


def truncate_text(content: str, policy: TruncationPolicy) -> str:
    if policy.mode == "bytes":
        return truncate_with_byte_estimate(content, policy)
    truncated, _ = truncate_with_token_budget(content, policy)
    return truncated


def formatted_truncate_text_with_token_count(
    content: str, max_output_tokens: int | None
) -> tuple[str, int | None]:
    if max_output_tokens is None:
        return content, None

    policy = TruncationPolicy.tokens(max_output_tokens)
    if _byte_len(content) <= policy.byte_budget():
        return content, None

    truncated, original_token_count = truncate_with_token_budget(content, policy)
    total_lines = len(content.splitlines())
    return f"Total output lines: {total_lines}\n\n{truncated}", original_token_count


def truncate_with_token_budget(s: str, policy: TruncationPolicy) -> tuple[str, int | None]:
    if s == "":
        return "", None

    max_tokens = policy.token_budget()
    byte_len = _byte_len(s)
    if max_tokens > 0 and byte_len <= approx_bytes_for_tokens(max_tokens):
        return s, None

    truncated = truncate_with_byte_estimate(s, policy)
    approx_total = approx_token_count(s)
    if truncated == s:
        return truncated, None
    return truncated, approx_total


def truncate_with_byte_estimate(s: str, policy: TruncationPolicy) -> str:
    if s == "":
        return ""

    total_chars = len(s)
    max_bytes = policy.byte_budget()
    source_bytes = s.encode("utf-8")

    if max_bytes == 0:
        marker = format_truncation_marker(
            policy,
            removed_units_for_source(policy, len(source_bytes), total_chars),
        )
        return marker

    if len(source_bytes) <= max_bytes:
        return s

    left_budget, right_budget = split_budget(max_bytes)
    removed_chars, left, right = split_string(s, left_budget, right_budget)
    marker = format_truncation_marker(
        policy,
        removed_units_for_source(policy, len(source_bytes) - max_bytes, removed_chars),
    )
    return assemble_truncated_output(left, right, marker)


def split_string(s: str, beginning_bytes: int, end_bytes: int) -> tuple[int, str, str]:
    if s == "":
        return 0, "", ""

    source_bytes = s.encode("utf-8")
    length = len(source_bytes)
    tail_start_target = max(0, length - end_bytes)
    prefix_end = 0
    suffix_start = length
    removed_chars = 0
    suffix_started = False

    byte_idx = 0
    for ch in s:
        ch_len = len(ch.encode("utf-8"))
        char_end = byte_idx + ch_len
        if char_end <= beginning_bytes:
            prefix_end = char_end
            byte_idx = char_end
            continue

        if byte_idx >= tail_start_target:
            if not suffix_started:
                suffix_start = byte_idx
                suffix_started = True
            byte_idx = char_end
            continue

        removed_chars += 1
        byte_idx = char_end

    if suffix_start < prefix_end:
        suffix_start = prefix_end

    before = source_bytes[:prefix_end].decode("utf-8", errors="strict")
    after = source_bytes[suffix_start:].decode("utf-8", errors="strict")
    return removed_chars, before, after


def format_truncation_marker(policy: TruncationPolicy, removed_count: int) -> str:
    if policy.mode == "tokens":
        return f"…{removed_count} tokens truncated…"
    return f"…{removed_count} chars truncated…"


def split_budget(budget: int) -> tuple[int, int]:
    left = budget // 2
    return left, budget - left


def removed_units_for_source(
    policy: TruncationPolicy, removed_bytes: int, removed_chars: int
) -> int:
    if policy.mode == "tokens":
        return int(approx_tokens_from_byte_count(removed_bytes))
    return removed_chars


def assemble_truncated_output(prefix: str, suffix: str, marker: str) -> str:
    return f"{prefix}{marker}{suffix}"


def approx_token_count(text: str) -> int:
    byte_len = _byte_len(text)
    return (byte_len + (APPROX_BYTES_PER_TOKEN - 1)) // APPROX_BYTES_PER_TOKEN


def conservative_token_count(text: str) -> int:
    """Return an upper bound on the OpenAI token count for ``text``.

    OpenAI tokenizers (for example ``cl100k_base`` and ``o200k_base``) are byte-level BPE
    encoders: the text is split into its UTF-8 bytes and merges only ever combine adjacent
    tokens, so the token count can never exceed the number of UTF-8 bytes. The byte length is
    therefore a safe upper bound that never undercounts token-dense JSON or code, unlike the
    average-case :func:`approx_token_count` heuristic. Use this when a budget check must not
    allow a request to exceed the model's context window and ``tiktoken`` is not available.
    """
    return _byte_len(text)


@lru_cache(maxsize=32)
def _tiktoken_encoding_for_model(model: str | None) -> tiktoken.Encoding | None:
    """Return the ``tiktoken`` encoding for ``model``, or ``None`` if ``tiktoken`` is missing.

    Falls back to the current default ``o200k_base`` encoding for model names ``tiktoken``
    does not recognize. Cached because loading BPE ranks is comparatively expensive.
    """
    try:
        import tiktoken
    except ImportError:
        return None
    if model:
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            pass
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def openai_token_count(text: str, *, model: str | None = None) -> int:
    """Count tokens for OpenAI input using accounting that never undercounts.

    When ``tiktoken`` is installed this returns the exact count from the model's own encoding
    (falling back to ``o200k_base`` for unrecognized model names), which cannot undercount
    supported OpenAI input. When ``tiktoken`` is unavailable it returns the conservative
    UTF-8 byte-length upper bound from :func:`conservative_token_count`. Either way the result
    never undercounts the way the average-case :func:`approx_token_count` heuristic can, so it
    is safe for budget checks that must keep a request within a model's context window.
    """
    encoding = _tiktoken_encoding_for_model(model)
    if encoding is None:
        return conservative_token_count(text)
    # ``disallowed_special=()`` treats special-token strings (for example ``<|endoftext|>``)
    # as ordinary text instead of raising, which also cannot undercount their token cost.
    return len(encoding.encode(text, disallowed_special=()))


def approx_bytes_for_tokens(tokens: int) -> int:
    return max(0, tokens) * APPROX_BYTES_PER_TOKEN


def approx_tokens_from_byte_count(byte_count: int) -> int:
    if byte_count <= 0:
        return 0
    return (byte_count + (APPROX_BYTES_PER_TOKEN - 1)) // APPROX_BYTES_PER_TOKEN


__all__ = [
    "APPROX_BYTES_PER_TOKEN",
    "TruncationMode",
    "TruncationPolicy",
    "approx_bytes_for_tokens",
    "approx_token_count",
    "approx_tokens_from_byte_count",
    "assemble_truncated_output",
    "conservative_token_count",
    "format_truncation_marker",
    "formatted_truncate_text",
    "formatted_truncate_text_with_token_count",
    "openai_token_count",
    "removed_units_for_source",
    "split_budget",
    "split_string",
    "truncate_text",
    "truncate_with_byte_estimate",
    "truncate_with_token_budget",
]
