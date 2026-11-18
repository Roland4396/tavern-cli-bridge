"""Original transparent proxy + endpoint rewrite + response-side keepalives."""
import argparse,asyncio,base64,json,logging,logging.handlers,os,re,signal,time
from pathlib import Path
from urllib.parse import urlsplit
from mitmproxy import options,http
from mitmproxy.certs import CertStore
from mitmproxy.tools.dump import DumpMaster
from mitmproxy.proxy import server as proxy_server
from .core import upstream_error,BridgeError
from .models import NONSTREAM_MODELS
from .nonstream_keepalive import install

HOST='daily-cloudcode-pa.googleapis.com'
_TimeoutWatchdog=proxy_server.TimeoutWatchdog
class NoTcpIdleDeadline(_TimeoutWatchdog):
    async def watch(self):
        if self.timeout!=proxy_server.TCP_TIMEOUT:return await super().watch()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:return
proxy_server.TimeoutWatchdog=NoTcpIdleDeadline
install()

class Adapter:
    def __init__(self,state,models):
        self.state,self.models=Path(state),frozenset(models);self.clients={}
        self.log=logging.getLogger('nonstream_metadata');self.log.propagate=False;self.log.setLevel(logging.INFO)
        h=logging.handlers.RotatingFileHandler(state/'metadata.jsonl',maxBytes=1048576,backupCount=2);h.setFormatter(logging.Formatter('%(message)s'));self.log.addHandler(h)
    def event(self,event,**details):
        try:self.log.info(json.dumps({'event':event,'at':time.time(),**details},ensure_ascii=False))
        except Exception:pass
    def registered(self,rid):return bool(rid and (self.state/'registrations'/rid).is_file())
    def running(self):self.event('ready',port=17863,models=sorted(self.models),body_recording=False,keepalive_seconds=15,forwarding='mitmproxy-native')
    def http_connect(self,flow):
        auth=flow.request.headers.pop('Proxy-Authorization','')
        try:rid=base64.b64decode(auth.removeprefix('Basic '),validate=True).decode().split(':',1)[0]
        except Exception:return
        if re.fullmatch('[a-f0-9]{32}',rid) and self.registered(rid):self.clients[flow.client_conn.id]=rid
    def client_disconnected(self,client):self.clients.pop(client.id,None)
    def request(self,flow):
        if flow.request.host!=HOST or urlsplit(flow.request.url).path!='/v1internal:streamGenerateContent':return
        try:
            body=json.loads(flow.request.get_content(strict=False));model=body.get('model') if isinstance(body,dict) else None
        except (ValueError,TypeError,UnicodeDecodeError):return
        if model not in self.models:return
        rid=self.clients.get(flow.client_conn.id)
        if not self.registered(rid):
            flow.response=http.Response.make(502,b'{"error":{"code":502,"message":"Local nonstream request registration missing"}}',{'Content-Type':'application/json'});return
        raw=flow.request.raw_content;query=flow.request.path.partition('?')[2]
        remaining=[v for v in query.split('&') if v and v!='alt=sse']
        flow.request.path='/v1internal:generateContent'+('?'+'&'.join(remaining) if remaining else '')
        # No destination, header, transfer-encoding or request-body rewriting.
        flow.metadata['nonstream_adapter']={'rid':rid,'model':model,'at':time.monotonic(),'keepalives':0}
        self.event('generation_start',request_id=rid,model=model,request_body_unchanged=flow.request.raw_content==raw,request_body_bytes=len(raw or b''),request_transfer_encoding=flow.request.headers.get('Transfer-Encoding',''),forwarding='mitmproxy-native')
    def responseheaders(self,flow):flow.response.stream=not bool(flow.metadata.get('nonstream_adapter'))
    def publish_error(self,flow,error):
        info=flow.metadata['nonstream_adapter'];rid=info['rid']
        if not self.registered(rid):return
        p=self.state/'errors'/f'{rid}.json';tmp=p.with_suffix('.tmp')
        tmp.write_text(json.dumps({'status':error.status,'code':error.code,'message':error.message,'details':error.details},ensure_ascii=False));tmp.chmod(0o600);tmp.replace(p)
    def response(self,flow):
        info=flow.metadata.get('nonstream_adapter')
        if not info:return
        status=flow.response.status_code;raw=flow.response.get_content(strict=False) or b''
        try:payload=json.loads(raw)
        except (ValueError,UnicodeDecodeError):payload=None
        success=200<=status<300 and isinstance(payload,dict) and not payload.get('error')
        if success:frame=('data: '+json.dumps(payload,ensure_ascii=False,separators=(',',':'))+'\n\n').encode()
        else:
            token=flow.request.headers.get('Authorization','')
            error=upstream_error({'code':status,'error':payload or {'message':raw.decode(errors='replace')},'retry_after':flow.response.headers.get('Retry-After')},[token,token.removeprefix('Bearer ')]) if not 200<=status<300 or (isinstance(payload,dict) and payload.get('error')) else BridgeError(502,'invalid_upstream_json','The nonstream upstream returned an invalid JSON object',source='bridge_transport')
            self.publish_error(flow,error)
            # If no keepalive headers were needed, preserve the real HTTP error.
            if not info['keepalives']:
                self.finish(info,status,False);return
            frame=b''
        flow.response.headers.pop('Content-Encoding',None);flow.response.headers.pop('Transfer-Encoding',None)
        flow.response.headers['Content-Type']='text/event-stream';flow.response.content=frame
        self.finish(info,status,success)
    def finish(self,info,status,wrapped):
        self.event('generation_end',request_id=info['rid'],model=info['model'],seconds=round(time.monotonic()-info['at'],3),upstream_status=status,keepalive_blank_lines=info['keepalives'],json_to_sse=wrapped,body_recording=False,forwarding='mitmproxy-native')
    def error(self,flow):
        info=flow.metadata.get('nonstream_adapter')
        if info:
            self.publish_error(flow,BridgeError(502,'cli_transport_error','Nonstream upstream connection ended before completion',source='bridge_transport'))
            self.event('transport_error',request_id=info['rid'])
    def done(self):self.event('stopped')

async def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--models',required=True);a=p.parse_args()
    os.umask(0o077);a.state.mkdir(parents=True,exist_ok=True,mode=0o700);a.state.chmod(0o700)
    for d in ('registrations','errors'):(a.state/d).mkdir(mode=0o700,exist_ok=True)
    models=json.loads(a.models);assert models and set(models)<=NONSTREAM_MODELS
    ca=a.state/'ca';ca.mkdir(mode=0o700,exist_ok=True);CertStore.from_store(ca,'mitmproxy',2048)
    trust=a.state/'trust.pem';trust.write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+b'\n'+(ca/'mitmproxy-ca-cert.pem').read_bytes());trust.chmod(0o600)
    logging.getLogger().setLevel(logging.CRITICAL)
    opts=options.Options(listen_host='127.0.0.1',listen_port=17863,confdir=str(ca),ssl_insecure=False,http2=True,allow_hosts=[r'^daily-cloudcode-pa\.googleapis\.com(?::443)?$'])
    master=DumpMaster(opts,with_termlog=False,with_dumper=False);master.options.update(connection_strategy='lazy');master.addons.add(Adapter(a.state,models))
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,master.shutdown)
    await master.run()
if __name__=='__main__':asyncio.run(main())
