"""Ledger entry extraction from conversation messages."""
import hashlib as _hashlib
import json
import re


def _unbounded_replace_loop(code: str) -> bool:
    """Detect unbounded while(var.includes(X)) { var = var.replace(X, Y) } patterns.
    Returns True when the pattern is present AND no safety mechanism exists
    (no break, no counter bound, no replaceAll, no split.join)."""
    m = re.search(r"while\s*\(\s*(\w+)\.includes\(", code)
    if not m:
        return False
    var = m.group(1)
    if not re.search(re.escape(var) + r"\s*=\s*" + re.escape(var) + r"\.replace\(", code):
        return False
    if re.search(r"\bbreak\b", code):
        return False
    if re.search(r"for\s*\(\s*\w+\s*=\s*0\s*;\s*\w+\s*<\s*\d+", code):
        return False
    if re.search(re.escape(var) + r"\.replaceAll\(", code):
        return False
    if re.search(re.escape(var) + r"\.split\(", code):
        return False
    return True

_UNICODE_ESCAPE_RE = re.compile(r'\\u[0-9a-fA-F]{4}')





def _check_unicode_escapes(tc_accum) -> int:
    """Count unicode escapes in tool call arguments. Diagnostic only."""
    count = 0
    for _idx, _tc in tc_accum._calls.items():
        func = _tc.get("function", {})
        args_str = func.get("arguments", "")
        if isinstance(args_str, str):
            count += len(_UNICODE_ESCAPE_RE.findall(args_str))
    return count





def _extract_ledger_entries(msgs: list, slice_start: int, slice_end: int, task_uuid) -> list:
    """Deterministic extractor: scan messages for tool calls/results.
    
    Handles:
    - Traditional tools (write_file, shell, etc.)
    - Code Mode execute_typescript with McpFilesystemHttp writeFile/editFile
    - Delegate subagent results (files created by subagents)
    """
    from proxy.app import _clip_at_boundary
    entries = []
    tc_map = {}
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id", "")
                fn = tc.get("function", {})
                tc_map[tcid] = (fn.get("name", ""), fn.get("arguments", ""))
    import re as _re
    seen_paths = set()
    
    def _emit(kind, title, detail, evidence, source, path_v, fn_name):
        if path_v and path_v in seen_paths:
            return
        if path_v:
            seen_paths.add(path_v)
        dedupe = _hashlib.sha1(("|".join([kind, path_v, fn_name, detail[:100]])).encode()).hexdigest()
        entries.append({
            "kind": kind, "title": title, "detail": detail,
            "evidence": evidence, "source": source,
            "dedupe_hash": dedupe,
            "slice_start": slice_start, "slice_end": slice_end,
        })
    
    for m in msgs:
        if m.get("role") != "tool":
            continue
        tcid = m.get("tool_call_id", "")
        content_t = m.get("content", "")
        if not isinstance(content_t, str):
            content_t = json.dumps(content_t)
        if not content_t:
            continue
        fn_name, fn_args = tc_map.get(tcid, ("", ""))
        try:
            args = json.loads(fn_args) if fn_args else {}
        except Exception:
            args = {}
        
        # --- Collect all file paths from this tool call ---
        file_paths = []
        
        # Traditional tools: single path
        for key in ("path", "file_path", "filename", "file", "target"):
            if key in args and isinstance(args[key], str):
                file_paths.append(args[key])
                break
        if not file_paths and "command" in args:
            cmd = args["command"]
            m_path = _re.search(r"(?:>|>>|tee\s+|cp\s+\S+\s+|mv\s+\S+\s+)(/\S+)", cmd)
            if m_path:
                file_paths.append(m_path.group(1))
        
        # Code Mode: ALL writeFile/editFile paths in the code
        if fn_name == "execute_typescript":
            code = args.get("code", "")
            if code:
                q = chr(34) + chr(39)
                for wm in _re.finditer(r"writeFile\(\{\s*path:\s*[" + q + r"]([^" + q + r"]+)[" + q + r"]", code):
                    p = wm.group(1).strip()
                    if p.startswith("/") and len(p) > 3:
                        file_paths.append(p)
                for em in _re.finditer(r"editFile\(\{\s*path:\s*[" + q + r"]([^" + q + r"]+)[" + q + r"]", code):
                    p = em.group(1).strip()
                    if p.startswith("/") and len(p) > 3:
                        file_paths.append(p)
                for sm in _re.finditer(r"cat\s*>\s*(/[^\s" + q + r"]+)", code):
                    p = sm.group(1).strip()
                    if len(p) > 3:
                        file_paths.append(p)
        
        # Delegate: file paths in response
        if fn_name == "delegate":
            file_re = _re.compile(r"(/[^\s" + chr(34) + chr(39) + r"<>|]+\.(?:md|txt|json|ya?ml|xml|csv|py|java|sh|c|cpp|h))")
            for pm in file_re.findall(content_t):
                file_paths.append(pm)
        
        # --- Test/Build results (checked first, independent of file paths) ---
        test_match = _re.search(r"(Tests run:\s*(\d+).*?Failures:\s*(\d+)|\b(\d+)\s+passed\b|BUILD (SUCCESS|FAILURE))", content_t)
        if test_match:
            _emit("TEST_RESULT", "Test/Build result", content_t[:200],
                  fn_name + ":" + (file_paths[0] if file_paths else "shell"),
                  "tool:" + fn_name, file_paths[0] if file_paths else "", fn_name)
        
        # --- Failures ---
        if _re.search(r"(Error:|Exception:|FAILED|Traceback|non-zero exit)", content_t):
            _emit("FAILURE", "Error in " + (fn_name or "tool"), content_t[:200],
                  fn_name + ":" + (file_paths[0] if file_paths else ""),
                  "tool:" + fn_name, file_paths[0] if file_paths else "", fn_name)
        
        # --- Artifacts: one entry per file path ---
        is_write_tool = fn_name in ("write_file", "str_replace", "write", "edit", "create_file", "shell", "execute_typescript", "delegate")
        if is_write_tool:
            for p in file_paths:
                if p in seen_paths:
                    continue
                _emit("ARTIFACT", p, fn_name + " " + p, fn_name + ":" + p,
                      "tool:" + fn_name, p, fn_name)
    
    # ADDENDUM_1: INSTRUCTION entries from user messages
    for m in msgs:
        if m.get("role") != "user":
            continue
        uc = m.get("content", "")
        if not isinstance(uc, str):
            uc = json.dumps(uc)
        uc_clean = _re.sub(r"<turn-context>.*?</turn-context>", "", uc, flags=_re.DOTALL).strip()
        if len(uc_clean) < 30:
            continue
        title_u = _clip_at_boundary(uc_clean, 200)
        sha1_u = _hashlib.sha1(uc_clean.encode()).hexdigest()
        entries.append({
            "kind": "INSTRUCTION", "title": title_u,
            "detail": _clip_at_boundary(uc_clean, 600) + " (total %d chars, sha1=%s)" % (len(uc_clean), sha1_u[:12]),
            "evidence": "user_message",
            "source": "user", "dedupe_hash": sha1_u,
            "slice_start": slice_start, "slice_end": slice_end,
        })
    return entries



