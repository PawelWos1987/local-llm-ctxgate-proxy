"""Exact token counting using the model's HuggingFace tokenizer."""
import logging
import os

log = logging.getLogger("ctxgate-proxy")

_tokenizer = None
_tokenizer_model = None


def _get_tokenizer(model_name: str):
    """Lazy-load the HF tokenizer for the given model.

    Resolution order:
    1. CTXGATE_QWEN_TOKENIZER env var → use its parent directory
    2. model_name as a local directory path (if it exists on disk)
    3. model_name as a HuggingFace Hub identifier (fallback)
    """
    global _tokenizer, _tokenizer_model
    if _tokenizer is not None and _tokenizer_model == model_name:
        return _tokenizer

    from transformers import AutoTokenizer

    # Try local resolution first
    local_path = None
    tok_env = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")
    if tok_env:
        # Env var may point to a file (tokenizer.json) or a directory
        if os.path.isfile(tok_env):
            local_path = os.path.dirname(tok_env)
        elif os.path.isdir(tok_env):
            local_path = tok_env

    if local_path is None and os.path.isdir(model_name):
        local_path = model_name

    if local_path:
        _tokenizer = AutoTokenizer.from_pretrained(local_path)
        log.info("Loaded tokenizer from local path: %s (for model %s)", local_path, model_name)
    else:
        _tokenizer = AutoTokenizer.from_pretrained(model_name)
        log.info("Loaded tokenizer from HF hub: %s", model_name)

    _tokenizer_model = model_name
    return _tokenizer



def _sanitize_for_template(messages: list[dict]) -> list[dict]:
    """Strip non-standard keys from messages before applying chat template.

    Qwen3 Jinja templates iterate over msg.items() and choke on non-string
    values like _tokens (int). We keep only the standard chat-template keys.
    """
    allowed = {'role', 'content', 'tool_calls', 'name', 'tool_call_id'}
    cleaned = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        entry = {k: v for k, v in m.items() if k in allowed}
        c = entry.get('content')
        if c is None:
            entry['content'] = ''
        elif isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict):
                    parts.append(str(p.get('text', '')))
                elif isinstance(p, str):
                    parts.append(p)
            entry['content'] = ' '.join(parts)
        elif not isinstance(c, str):
            entry['content'] = str(c)
        cleaned.append(entry)
    return cleaned


def exact_count_messages(messages: list[dict], model_name: str) -> int:
    """Count EXACT tokens for a list of chat messages using the model tokenizer.

    Applies the chat template to get the true prompt that will be sent.
    """
    tok = _get_tokenizer(model_name)
    cleaned = _sanitize_for_template(messages)
    rendered = tok.apply_chat_template(cleaned, tokenize=False, add_generation_prompt=True)
    ids = tok.encode(rendered, add_special_tokens=False)
    return len(ids)


def exact_count_string(text: str, model_name: str) -> int:
    """Count exact tokens for a plain string."""
    tok = _get_tokenizer(model_name)
    return len(tok.encode(text, add_special_tokens=False))

