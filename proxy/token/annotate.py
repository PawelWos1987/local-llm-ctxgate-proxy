"""Per-message token annotation using the model's HuggingFace tokenizer.

Provides annotate_message_usage() which attaches a _tokens field to each
message dict so downstream logic (shrink, budget decisions) can operate on
real tokenizer counts rather than character-based estimates.
"""
import logging

log = logging.getLogger("ctxgate-proxy")


def annotate_message_usage(messages: list[dict], model_name: str) -> list[dict]:
    """Annotate each message with its individual token count.

    For each message, tokenizes its content individually using the HF tokenizer.
    Attaches a _tokens field to each message dict with its individual token count.

    The total of all _tokens values approximates exact_count_messages() result
    (small difference due to chat template overhead / special tokens).

    Args:
        messages: List of message dicts ({role, content, ...}).
        model_name: HuggingFace model identifier for the tokenizer.

    Returns:
        The same list (mutated in place) with _tokens added to each message.
    """
    from proxy.token.exact import _get_tokenizer

    tok = _get_tokenizer(model_name)

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multi-part content (e.g., text + image blocks)
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            content = "\n".join(text_parts)

        if content:
            ids = tok.encode(str(content), add_special_tokens=False)
            msg["_tokens"] = len(ids)
        else:
            msg["_tokens"] = 0

    return messages
