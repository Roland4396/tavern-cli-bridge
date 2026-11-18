"""Response-only keepalives inside the existing pinned mitmproxy HTTP stack.
Request parsing, chunking, TLS, routing and forwarding remain mitmproxy-owned.
"""
from mitmproxy import http
from mitmproxy.proxy import commands, events
from mitmproxy.proxy.layers import http as stack

_BaseStream=stack.HttpStream
class KeepaliveStream(_BaseStream):
    interval=15
    early_headers=False
    def state_consume_request_body(self,event):
        yield from super().state_consume_request_body(event)
        if isinstance(event,stack.RequestEndOfMessage) and self.flow.metadata.get('nonstream_adapter') and self.flow.live:
            yield commands.RequestWakeup(self.interval)
    def _handle_event(self,event):
        if isinstance(event,events.Wakeup):
            if not self.flow.live or self.server_state in (self.state_done,self.state_errored):return
            if not self.flow.metadata.get('nonstream_adapter'):return
            if not self.early_headers:
                response=http.Response.make(200,b'',{'Content-Type':'text/event-stream','Cache-Control':'no-cache'})
                response.headers.pop('Content-Length',None)
                response.headers['Transfer-Encoding']='chunked'
                self.early_headers=True
                yield stack.SendHttp(stack.ResponseHeaders(self.stream_id,response,end_stream=False),self.context.client)
            yield stack.SendHttp(stack.ResponseData(self.stream_id,b'\n\n'),self.context.client)
            self.flow.metadata['nonstream_adapter']['keepalives']+=1
            yield commands.RequestWakeup(self.interval)
        else:yield from super()._handle_event(event)
    def _after_early_headers(self,generator,drop_body=False):
        # Forward blocking hook replies too; do not replace the HTTP state machine.
        reply=None
        while True:
            try:command=generator.send(reply)
            except StopIteration as end:return end.value
            skip=self.early_headers and isinstance(command,stack.SendHttp) and command.connection==self.context.client and isinstance(command.event,((stack.ResponseHeaders,stack.ResponseData) if drop_body else (stack.ResponseHeaders,)))
            reply=None if skip else (yield command)
    def send_response(self,already_streamed=False):
        yield from self._after_early_headers(super().send_response(already_streamed))
    def handle_protocol_error(self,event):
        yield from self._after_early_headers(super().handle_protocol_error(event),drop_body=True)

def install():stack.HttpStream=KeepaliveStream
