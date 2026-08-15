import json

# A single SSE line should never be this long. If it is, the stream is not what
# we think it is, so drop what we buffered rather than grow without bound.
MAX_BUFFERED_LINE = 1_048_576


def usage_from_body(body: bytes) -> tuple[int | None, int | None]:
    """Token usage from a non-streamed Anthropic response body."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None

    if not isinstance(payload, dict):
        return None, None

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None

    return _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))


class StreamUsage:
    """Reads token usage out of an SSE stream while the bytes pass through untouched.

    Input tokens arrive once in message_start; output tokens are cumulative and
    are restated by every message_delta, so the last one wins.
    """

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._read_line(line)
        if len(self._buffer) > MAX_BUFFERED_LINE:
            self._buffer = b""

    def _read_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return

        try:
            payload = json.loads(line[len(b"data:") :])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(payload, dict):
            return

        event = payload.get("type")
        if event == "message_start":
            message = payload.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            self._read_usage(usage)
        elif event == "message_delta":
            self._read_usage(payload.get("usage"))

    def _read_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return

        input_tokens = _int(usage.get("input_tokens"))
        if input_tokens is not None:
            self.input_tokens = input_tokens

        output_tokens = _int(usage.get("output_tokens"))
        if output_tokens is not None:
            self.output_tokens = output_tokens


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
