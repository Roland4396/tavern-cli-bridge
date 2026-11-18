"""Bounded byte adapter around the pinned httpx-sse parser core.

The upstream parser implements SSE CR/LF/multiline semantics. This adapter owns
only UTF-8/BOM decoding, resource limits and preserving keep-alive comments.
It does not treat EOF as a successful message or manufacture an end marker.
"""
import codecs
from dataclasses import dataclass
from .core import BridgeError
from ._vendor.httpx_sse._decoders import SSEDecoder, SSELineDecoder


@dataclass(frozen=True)
class Frame:
    data: str = None
    comment: str = None


class EventStreamDecoder:
    def __init__(self, max_event_bytes=8000000):
        self.utf8 = codecs.getincrementaldecoder('utf-8-sig')('strict')
        self.lines = SSELineDecoder()
        self.events = SSEDecoder()
        self.limit = max_event_bytes
        self.event_bytes = 0
        self.closed = False

    def _bound(self, size):
        if size > self.limit:
            raise BridgeError(502, 'stream_event_too_large', 'Gproxy SSE event exceeds the local bound')

    def feed(self, chunk=b'', *, final=False):
        if self.closed:
            raise RuntimeError('SSE decoder is already closed')
        self.closed = final
        try:
            text = self.utf8.decode(chunk, final=final)
        except UnicodeError:
            raise BridgeError(502, 'invalid_upstream_sse', 'Gproxy returned invalid SSE UTF-8') from None
        lines = self.lines.decode(text) if text else []
        if final:
            lines += self.lines.flush()
        out = []
        for line in lines:
            self.event_bytes += len(line.encode('utf-8')) + 1
            self._bound(self.event_bytes)
            if line.startswith(':'):
                out.append(Frame(comment=line[1:]))
            event = self.events.decode(line)
            if not line:
                self.event_bytes = 0
            if event is not None and event.data:
                out.append(Frame(data=event.data))
        # The line decoder may retain an unfinished line across arbitrary byte
        # chunks. Bound that storage too, not just completed data fields.
        buffered = sum(len(part.encode('utf-8')) for part in self.lines.buffer)
        self._bound(self.event_bytes + buffered + int(self.lines.trailing_cr))
        return out
