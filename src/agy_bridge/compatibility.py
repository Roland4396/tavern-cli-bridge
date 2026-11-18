"""Single-dispatch Gproxy transport; cancellation and semantic SSE completion."""
import copy
import http.client
import json
import os
import socket
import threading
from .core import BridgeError, upstream_error
from .sse import EventStreamDecoder

def serve_gproxy(self, app, body, check):
    conn=http.client.HTTPConnection(app.url.hostname,app.url.port or 80,timeout=app.timeout or None)
    stop=threading.Event();cancelled=[];headers_sent=False;submitted=False
    cancel_socket=None;response=None
    # Close even a blocking header/body read when the downstream disconnects.
    def watch():
        while not stop.wait(.05):
            try:check()
            except BridgeError as exc:
                cancelled.append(exc)
                try:
                    if cancel_socket:cancel_socket.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                conn.close();return
    thread=threading.Thread(target=watch,daemon=True);thread.start()
    def sse(value):
        self.wfile.write(('data: '+(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False))+'\n\n').encode());self.wfile.flush()
    def error_from(value,status):
        err=upstream_error(value,(app.gproxy_key,self.server.token))
        err.details.update(source='gproxy',status_source='gproxy_http',gproxy_status=status,model_request_submitted=True)
        # Normalizer may classify an error but cannot invent provider HTTP provenance.
        if err.details.get('upstream_status') is None:err.details.pop('upstream_status',None)
        return err
    try:
        check();conn.connect()
        cancel_socket=socket.socket(fileno=os.dup(conn.sock.fileno()))
        check();submitted=True
        wire_body=dict(body,model=app.gproxy_models[body['model']])
        conn.request('POST',app.url.path,json.dumps(wire_body,ensure_ascii=False).encode(),
                     {'Authorization':'Bearer '+app.gproxy_key,'Content-Type':'application/json','Connection':'close'})
        response=conn.getresponse();check()
        media=response.getheader('Content-Type','').split(';')[0].strip().lower()
        if response.status>=400:
            raw=response.read(1048577)
            if len(raw)>1048576:raw=raw[:1048576]
            try:value=json.loads(raw)
            except ValueError:value=raw.decode(errors='replace')
            err=error_from(value,response.status)
            retry=response.getheader('Retry-After')
            if retry and retry.isdigit():err.details['retry_after']=max(1,min(86400,int(retry)))
            app.record_error(response.status,err)
            self.completion_result=err.code
            return self.reply(response.status,err.public(),{'X-AGY-Dispatch':'submitted-or-uncertain'})
        if bool(body.get('stream'))!=(media=='text/event-stream'):
            raise BridgeError(502,'upstream_mode_mismatch','Gproxy returned the wrong response mode')
        if not body.get('stream'):
            raw=response.read(10000001);check()
            if len(raw)>10000000:raise BridgeError(502,'output_too_large','Gproxy response exceeds the local bound')
            try:value=json.loads(raw)
            except ValueError:raise BridgeError(502,'invalid_upstream_json','Gproxy returned invalid JSON') from None
            if isinstance(value,dict) and value.get('error'):
                err=error_from(value,response.status);app.record_error(err.status,err)
                self.completion_result=err.code
                return self.reply(err.status,err.public())
            if not isinstance(value,dict) or not value.get('choices'):
                raise BridgeError(502,'invalid_upstream_response','Gproxy returned no completion choices')
            if 'model' in value:value['model']=body['model']
            self.completion_result='ok'
            return self.reply(response.status,value)
        self.send_response(response.status);self.send_header('Content-Type','text/event-stream')
        self.send_header('Cache-Control','no-cache');self.send_header('Connection','close');self.send_header('X-Accel-Buffering','no')
        self.end_headers();headers_sent=True
        decoder=EventStreamDecoder();finish=None;had_error=False;done=False
        while not done:
            check();chunk=response.read1(65536)
            for frame in decoder.feed(chunk,final=not chunk):
                if frame.comment is not None:
                    self.wfile.write((':'+frame.comment+'\n\n').encode());self.wfile.flush();continue
                data=frame.data
                if data=='[DONE]':
                    if not had_error and finish is None:raise BridgeError(502,'missing_finish','Gproxy ended without a successful finish reason')
                    if finish is not None and not had_error:sse(finish)
                    sse('[DONE]');done=True;break
                try:value=json.loads(data)
                except (ValueError,UnicodeError):raise BridgeError(502,'invalid_upstream_sse','Gproxy returned malformed SSE data') from None
                if not isinstance(value,dict):raise BridgeError(502,'invalid_upstream_sse','Gproxy returned a non-object SSE event')
                if value.get('error'):
                    err=error_from(value,response.status);app.record_error(err.status,err)
                    self.completion_result=err.code
                    had_error=True;finish=None;sse(err.public());continue
                if had_error:continue
                if 'model' in value:value['model']=body['model']
                choices=value.get('choices',[])
                if not isinstance(choices,list) or any(not isinstance(c,dict) for c in choices):
                    raise BridgeError(502,'invalid_upstream_sse','Gproxy returned malformed completion choices')
                terminals=[c for c in choices if c.get('finish_reason') is not None]
                if terminals:
                    if any(c['finish_reason'] not in ('stop','length','tool_calls','function_call','content_filter') for c in terminals):
                        raise BridgeError(502,'invalid_finish','Gproxy returned an invalid finish reason')
                    finish=copy.deepcopy(value)
                    finish['choices']=[dict(c,delta={}) for c in terminals]
                    value=copy.deepcopy(value)
                    for c in value.get('choices',[]):c['finish_reason']=None
                    finish.pop('usage',None)
                sse(value)
            if not chunk:break
        if not done:raise BridgeError(502,'incomplete_stream','Gproxy stream ended before DONE')
        if not had_error:self.completion_result='ok'
    except (OSError,http.client.HTTPException,BridgeError) as exc:
        err=cancelled[0] if cancelled else exc if isinstance(exc,BridgeError) else BridgeError(504,'timeout','Gproxy request timed out; request was not retried') if isinstance(exc,TimeoutError) else BridgeError(502,'gproxy_transport_error','Gproxy connection failed; request was not retried')
        self.completion_result=err.code
        err.details.update(source='gproxy_transport',model_request_submitted=submitted)
        if err.status!=499:
            try:
                if headers_sent:sse(err.public());sse('[DONE]')
                else:self.reply(err.status,err.public(),{'X-AGY-Dispatch':'submitted-or-uncertain' if submitted else 'not-submitted'})
            except OSError:pass
    finally:
        stop.set();conn.close();thread.join(timeout=2)
        if response:response.close()
        if cancel_socket:cancel_socket.close()
