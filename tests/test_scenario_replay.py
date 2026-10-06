#!/usr/bin/env python3
"""Scenario replay: 3 seed + old user + newest user + 75 assistant/tool pairs
(~1000 tokens each). Runs through the REAL recut/shrink/output-budget functions
(AST-extracted) with the target limits. Reports kept tokens, headroom, max_tokens."""
import os, ast, collections

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "proxy", "app.py")

# target limits (mirror .env)
MAX_CONTEXT=84000; MAX_INPUT=58000; MAX_OUTPUT=22500
SAFETY_MARGIN=3500; MIN_OUTPUT=16000; TRIM_FRAC=0.70

def _extract(names):
    src = open(APP).read(); tree = ast.parse(src); chunks=[]
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names:
            chunks.append(ast.get_source_segment(src,node))
    return "\n\n".join(chunks)

def _cmt(m):
    c=m.get("content")
    if c is None: c=""
    if isinstance(c,list): c=" ".join(p.get("text","") for p in c if isinstance(p,dict))
    n=len(str(c))//4+4
    for t in (m.get("tool_calls") or []):
        n+=len(str(t.get("function",{}).get("arguments","")))//4
    return n
def _cmts(ms): return sum(_cmt(m) for m in ms)

def _load():
    names=["_recut_to","_recut","_msg_anchor","_seed_sig","_norm_content",
           "_new_window_state","_window_valid","_kept_messages","_prep_messages",
           "_newest_user_idx","_make_pinned_copy","_pinned_user_copy","_trim_target",
           "_emergency_shrink","_protected_indices","_output_budget"]
    ns=dict(count_message_tokens=_cmt,count_messages_tokens=_cmts,
            MAX_CONTEXT=MAX_CONTEXT,MAX_INPUT=MAX_INPUT,MAX_OUTPUT=MAX_OUTPUT,
            SAFETY_MARGIN=SAFETY_MARGIN,MIN_OUTPUT=MIN_OUTPUT,
            TRIM_TARGET_TOKENS=0,TRIM_TARGET_FRACTION=TRIM_FRAC,TRIM_TARGET_FLOOR=20000,
            PINNED_USER_MAX_CHARS=16000,
            STUB_TEXT="[COMPACTED HISTORY] earlier turns archived; see TASK STATE",
            metrics=collections.defaultdict(int))
    import hashlib,re,json
    ns.update(hashlib=hashlib,re=re,json=json)
    ns["log"]=type("L",(),{"warning":staticmethod(lambda *a:None),"info":staticmethod(lambda *a:None),"error":staticmethod(lambda *a:None)})()
    exec(compile(_extract(names),"extracted","exec"),ns)
    return ns
NS=_load()

def build_scenario():
    msgs=[
        {"role":"system","content":"You are a helpful assistant. (seed 1)"},
        {"role":"user","content":"seed 2 constant stub context"},
        {"role":"assistant","content":"seed 3 assistant ack"},
        {"role":"user","content":"old user message from earlier in the conversation"},   # old user
    ]
    for i in range(75):
        msgs.append({"role":"assistant","content":"A"*2000,
                     "tool_calls":[{"id":f"tc{i}_0","type":"function",
                                   "function":{"name":"tool","arguments":"{}"}}]})
        msgs.append({"role":"tool","tool_call_id":f"tc{i}_0","content":"T"*2000})
    msgs.append({"role":"user","content":"newest user message: what is the final answer?"})  # newest user
    return msgs

def main():
    msgs=build_scenario()
    total=_cmts(msgs)
    trim_target=NS["_trim_target"]()
    cut=NS["_recut_to"](msgs,trim_target)
    # split into seed/rest like the real flow: seed=first3, rest=rest
    seed=msgs[:3]; rest=msgs[3:]
    kept=NS["_kept_messages"](seed,rest,cut)
    ceiling=min(MAX_INPUT,MAX_CONTEXT-SAFETY_MARGIN-MIN_OUTPUT)
    before=_cmts(kept)
    kept=NS["_emergency_shrink"](kept,ceiling)
    kept_tok=_cmts(kept)
    max_tokens=NS["_output_budget"](kept_tok)
    headroom_vs_input=MAX_INPUT-kept_tok
    headroom_vs_ctx=MAX_CONTEXT-SAFETY_MARGIN-MIN_OUTPUT-kept_tok
    print(f"total input (pre-trim) = {total}")
    print(f"trim_target           = {trim_target}")
    print(f"cut index             = {cut}")
    print(f"kept tokens (post)    = {kept_tok}")
    print(f"  (after recut, pre-shrink = {before})")
    print(f"headroom vs MAX_INPUT(58000)   = {headroom_vs_input}")
    print(f"headroom vs ctx-margin-minout  = {headroom_vs_ctx}")
    print(f"max_tokens            = {max_tokens}")
    print(f"constraints: max_tokens<={MAX_OUTPUT}: {max_tokens<=MAX_OUTPUT} ; >=MIN_OUTPUT({MIN_OUTPUT}): {max_tokens>=MIN_OUTPUT}")
    assert max_tokens<=MAX_OUTPUT, "max_tokens exceeds MAX_OUTPUT"
    assert max_tokens>=MIN_OUTPUT, "max_tokens below MIN_OUTPUT"
    print("PASS: scenario replay (max_tokens within [16000, 22500])")

if __name__=="__main__":
    main()
