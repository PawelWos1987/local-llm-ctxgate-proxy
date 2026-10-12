"""Tool call accumulator for streaming SSE responses."""
import logging
import json

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


class ToolCallAccumulator:
    """Tracks per-tool-call streaming deltas and validates completeness.

    A tool-call set is EXECUTABLE only when every emitted call is complete
    AND its arguments are valid JSON.
    """
    def __init__(self):
        self._calls = {}  # index -> {id, name, arguments}

    def add_delta(self, tool_calls_piece: list):
        """Merge streaming deltas by index."""
        if not tool_calls_piece:
            return
        for piece in tool_calls_piece:
            if not isinstance(piece, dict):
                continue
            idx = piece.get("index", 0)
            if idx not in self._calls:
                self._calls[idx] = {"id": "", "name": "", "arguments": ""}
            call = self._calls[idx]
            if piece.get("id"):
                call["id"] = piece["id"]
            fn = piece.get("function") or {}
            if fn.get("name"):
                call["name"] += fn["name"]
            if fn.get("arguments"):
                call["arguments"] += fn["arguments"]

    def is_complete(self) -> bool:
        """Every seen call has a non-empty name and its arguments parse as valid JSON."""
        if not self._calls:
            return False
        for call in self._calls.values():
            if not call["name"]:
                return False
            if not call["arguments"]:
                return False
            try:
                json.loads(call["arguments"])
            except (json.JSONDecodeError, ValueError):
                return False
        return True

    def is_valid(self) -> bool:
        """Alias for is_complete (every call is structurally valid)."""
        return self.is_complete()

    def to_tool_calls(self) -> list:
        """The complete OpenAI-format list of tool calls."""
        result = []
        for idx in sorted(self._calls.keys()):
            call = self._calls[idx]
            result.append({
                "id": call["id"] or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call["arguments"],
                },
            })
        return result

    def count(self) -> int:
        return len(self._calls)

