
import importlib.util, asyncio, json, sys, os
spec = importlib.util.spec_from_file_location("ctxgate_nofix", "_app_nofix.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["ctxgate_nofix"] = mod
spec.loader.exec_module(mod)
print("IMPORT OK (no-fix)")

class FakeResponse:
    def __init__(self, status_code, body=b"", lines=None):
        self.status_code = status_code; self._body = body
        self._lines = lines if lines is not None else []
    async def aread(self): return self._body
    async def aiter_lines(self):
        for l in self._lines: yield l
class FakeCtx:
    def __init__(self, r): self._r = r
    async def __aenter__(self): return self._r
    async def __aexit__(self, *a): return False
class StubClient:
    def __init__(self, s): self.s=list(s); self.calls=0
    def stream(self, m,u,json=None):
        self.calls+=1
        item = self.s.pop(0) if self.s else FakeResponse(200, lines=["data: [DONE]"])
        return FakeCtx(item)
def sse(*o): return ["data: "+json.dumps(x) for x in o]+["data: [DONE]"]

async def drive(scripted, input_tokens):
    mod._vllm_client = StubClient(scripted)
    body = {"model": mod.VLLM_MODEL, "messages":[{"role":"user","content":"hello world, please say hi"}], "max_tokens":50}
    resp = await mod.stream_to_vllm(body, input_tokens, "probe")
    chunks=[]
    async for c in resp.body_iterator: chunks.append(c)
    return chunks

async def main():
    scripted=[FakeResponse(200, lines=sse(
        {"id":"x","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":None}]},
        {"id":"x","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"stop"}],
         "usage":{"prompt_tokens":0,"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":2}}},
    ))]
    try:
        chunks = await drive(scripted, 123)
        print("NO UNBOUND: chunks=", len(chunks))
    except UnboundLocalError as e:
        print("REPRODUCED UnboundLocalError:", e)
asyncio.run(main())
