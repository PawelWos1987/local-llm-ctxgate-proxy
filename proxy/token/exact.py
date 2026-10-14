"""Exact token counting using the model's HuggingFace tokenizer."""
import json
import logging
import os

log = logging.getLogger("ctxgate-proxy")

_tokenizer = None
_tokenizer_model = None
_gt_tokenizer = None


def _get_tokenizer(model_name: str):
    """Lazy-load the HF tokenizer for the given model.

    Resolution order:
    1. CTXGATE_QWEN_TOKENIZER env var → use its parent directory
    2. model_name as a local directory path (if it exists on disk)
    3. model_name as a HuggingFace Hub identifier (fallback)
    """
    global _tokenizer, _tokenizer_model, _gt_tokenizer
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

    # Attempt to enable gigatoken accelerator for the encode step
    try:
        import gigatoken as gt
        _gt_tokenizer = gt.Tokenizer(_tokenizer).as_hf()
        log.info("Gigatoken accelerator enabled for tokenizer")
    except ImportError:
        _gt_tokenizer = None
        log.warning("gigatoken not available, using pure HF tokenizer")

    _tokenizer_model = model_name
    return _tokenizer


def _fast_encode(text: str, add_special_tokens: bool = False) -> list:
    """Encode text using gigatoken if available, else fall back to HF tokenizer."""
    if _gt_tokenizer is not None:
        return _gt_tokenizer.encode(text, add_special_tokens=add_special_tokens)
    return _tokenizer.encode(text, add_special_tokens=add_special_tokens)


def _flatten_content(content) -> str:
    """Flatten multimodal content (list of parts) into a plain string.

    Handles OpenAI-style content parts: [{"type":"text","text":...}, {"type":"image_url",...}]
    Only text-type parts contribute; image/audio/video are skipped.
    """
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                ptype = p.get('type', '')
                if ptype == 'text':
                    parts.append(str(p.get('text', '')))
                elif 'text' in p:
                    parts.append(str(p['text']))
                # Skip image_url, audio, video — they don't contribute text tokens
            elif isinstance(p, str):
                parts.append(p)
        return ' '.join(parts)
    return str(content)


def _sanitize_for_template(messages: list[dict]) -> list[dict]:
    """Strip non-standard keys and normalize values before applying chat template.

    Qwen3 Jinja templates iterate over msg.items() and choke on non-string
    values like _tokens (int), list-valued content, or string-valued
    tool_calls.function.arguments. We keep only standard keys and ensure
    all values are types the template expects.
    """
    allowed = {'role', 'content', 'tool_calls', 'name', 'tool_call_id'}
    cleaned = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        entry = {k: v for k, v in m.items() if k in allowed}

        # Normalize content: list → flat string, None → '', other → str
        c = entry.get('content')
        entry['content'] = _flatten_content(c)

        # Normalize tool_calls: ensure function.arguments is a dict, not a JSON string
        tcs = entry.get('tool_calls')
        if isinstance(tcs, list):
            normalized_tcs = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                nc = dict(tc)  # shallow copy
                fn = nc.get('function')
                if isinstance(fn, dict):
                    args = fn.get('arguments')
                    if isinstance(args, str):
                        try:
                            fn_args_parsed = json.loads(args)
                            if isinstance(fn_args_parsed, dict):
                                nc['function'] = {**fn, 'arguments': fn_args_parsed}
                            else:
                                # Parsed to non-dict (e.g., list); wrap it
                                nc['function'] = {**fn, 'arguments': {'value': fn_args_parsed}}
                        except (json.JSONDecodeError, TypeError):
                            # Leave as-is; template might handle it
                            pass
                normalized_tcs.append(nc)
            entry['tool_calls'] = normalized_tcs

        cleaned.append(entry)
    return cleaned


def exact_count_messages(messages: list[dict], model_name: str) -> int:
    """Count EXACT tokens for a list of chat messages using the model tokenizer.

    Applies the chat template to get the true prompt that will be sent.
    If the template fails (e.g., no user-role message present), falls back
    to direct gigatoken encoding of each message's content — still an
    exact count, never a rough character-based estimate.
    """
    if not messages:
        return 0
    tok = _get_tokenizer(model_name)
    cleaned = _sanitize_for_template(messages)
    try:
        rendered = tok.apply_chat_template(cleaned, tokenize=False, add_generation_prompt=True)
        ids = _fast_encode(rendered, add_special_tokens=False)
        return len(ids)
    except Exception as e:
        # Chat template failed (e.g. "No user query found in messages").
        # Count tokens by directly encoding each message's text via gigatoken/
        # HF tokenizer. This is still an exact count, just without the template
        # delimiters — acceptable when the template cannot process this shape.
        log.debug("Chat template failed (%s); using direct per-message encode", e)
        total = 0
        for m in cleaned:
            c = m.get('content', '')
            if isinstance(c, str) and c:
                total += len(_fast_encode(c, add_special_tokens=False))
            tcs = m.get('tool_calls')
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        fn = tc.get('function', {})
                        if isinstance(fn, dict):
                            args = fn.get('arguments', '')
                            if isinstance(args, str) and args:
                                total += len(_fast_encode(args, add_special_tokens=False))
                            elif isinstance(args, dict):
                                total += len(_fast_encode(json.dumps(args), add_special_tokens=False))
        return total


def exact_count_string(text: str, model_name: str) -> int:
    """Count exact tokens for a plain string."""
    tok = _get_tokenizer(model_name)
    return len(_fast_encode(text, add_special_tokens=False))

