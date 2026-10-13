"""Window state management: creation, validation, and epoch tracking."""
import hashlib
import logging
import os

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _norm_content(c) -> str:
    """Normalize a message's content for identity comparison (F2).

    Raw and prepped messages must give IDENTICAL anchors: None -> "" and a
    list of parts -> its joined text parts. _prep_messages does exactly this,
    so a raw (content=None / list) message now matches its prepped twin."""
    if c is None:
        return ""
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)

def _msg_anchor(m: dict) -> str:
    """Stable identity of a message: role + full normalized content + tool_call_id."""
    c = _norm_content(m.get("content"))
    return hashlib.sha256((str(m.get("role", "")) + "|" + c + "|" + str(m.get("tool_call_id", ""))).encode()).hexdigest()

def _seed_sig(messages: list) -> str:
    h = hashlib.sha1()
    for m in messages[:3]:
        c = _norm_content(m.get("content"))
        h.update((str(m.get("role", "")) + "|" + c).encode())
    return h.hexdigest()

def _new_window_state(cut: int, rest: list, seed: list, summarized_through: int) -> dict:
    ca = _msg_anchor(rest[cut]) if 0 <= cut < len(rest) else ""
    cp = _msg_anchor(rest[cut - 1]) if 0 <= cut - 1 < len(rest) else ""
    lu = _newest_user_idx(rest)
    live_hash = ""
    if lu is not None:
        live_hash = hashlib.sha256(_norm_content(rest[lu].get("content")).encode()).hexdigest()
    suffix_head_hash = ""
    suffix_tail_hash = ""
    if cut < len(rest):
        suffix_head_hash = hashlib.sha256(_norm_content(rest[cut].get("content")).encode()).hexdigest()
    if cut > 0:
        suffix_tail_hash = hashlib.sha256(_norm_content(rest[cut - 1].get("content")).encode()).hexdigest()
    return {
        "cut": cut,
        "cut_anchor": ca,
        "cut_prev_anchor": cp,
        "seed_sig": _seed_sig(seed),
        "dropped_total": cut,
        "summarized_through": summarized_through,
        "pending_cut": cut,
        "in_flight": False,
        "elide_idx": None,
        "injected_block": None,
        "injected_epoch": None,
        "live_prompt_hash": live_hash,
        "suffix_head_hash": suffix_head_hash,
        "suffix_tail_hash": suffix_tail_hash,
    }

def _window_valid(ws: dict, seed: list, rest: list) -> bool:
    """O(1) validation: seed signature + SHA-256 hashes at the stored cut.
    Uses suffix_head_hash and suffix_tail_hash (SHA-256 of full content)
    for stronger validation than the old 300-char anchors."""
    if not ws:
        return False
    cut = ws.get("cut", 0)
    if cut < 0 or cut > len(rest):
        return False
    if ws.get("seed_sig") != _seed_sig(seed):
        return False
    if ws.get("suffix_head_hash"):
        if cut < len(rest):
            h = hashlib.sha256(_norm_content(rest[cut].get("content")).encode()).hexdigest()
            if ws["suffix_head_hash"] != h:
                return False
    else:
        ca = _msg_anchor(rest[cut]) if cut < len(rest) else ""
        if ws.get("cut_anchor", "") != ca:
            return False
    if ws.get("suffix_tail_hash"):
        if cut > 0:
            h = hashlib.sha256(_norm_content(rest[cut - 1].get("content")).encode()).hexdigest()
            if ws["suffix_tail_hash"] != h:
                return False
    else:
        cp = _msg_anchor(rest[cut - 1]) if cut - 1 >= 0 else ""
        if ws.get("cut_prev_anchor", "") != cp:
            return False
    return True


def _window_epoch(ws: dict) -> str:
    """The epoch is defined by the cut anchor. Within the same epoch,
    the same messages are retained and injection content is stable.
    Only advances when the high watermark is crossed."""
    return ws.get('cut_anchor', '')


# Constant note appended to message 0 (system) of the FINAL request on EVERY request.
# MUST stay constant: no per-request / per-session data (keeps vLLM prefix cache stable
# across re-cuts). Empty string disables it. Applied ONLY by _with_compaction_note() in
# the request handler, never inside build_context (seed_sig must stay note-free).
COMPACTION_NOTE = os.environ.get(
    "CTXGATE_COMPACTION_NOTE",
    "Older conversation turns may have been compacted. If a TASK STATE block "
    "appears in the newest user message, treat it as background only; the newest "
    "user message is the live instruction.")
_NOTE_TOK_CACHE = None

def _newest_user_idx(rest: list):
    """Return the index of the newest user message in rest, or None."""
    for i in range(len(rest) - 1, -1, -1):
        if rest[i].get("role") == "user":
            return i
    return None


def _strip_proxy_metadata(messages: list) -> list:
    """Remove internal ctxgate_* and _tokens keys from messages before sending to vLLM."""
    cleaned = []
    for m in messages:
        if any(k.startswith("ctxgate_") for k in m) or "_tokens" in m:
            c = {k: v for k, v in m.items() if not k.startswith("ctxgate_") and k != "_tokens"}
            cleaned.append(c)
        else:
            cleaned.append(m)
    return cleaned


