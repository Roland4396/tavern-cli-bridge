"""Local status only; never starts a CLI, queries quota, or sends inference."""
import http.client,json,socket
from pathlib import Path
cfg=json.loads(Path('/config/bridge.json').read_text())
class Unix(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(3);self.sock.connect(cfg['socket'])
c=Unix('localhost',timeout=3)
try:
 c.request('GET','/healthz',headers={'Authorization':'Bearer '+Path(cfg['api_key_file']).read_text().strip()})
 r=c.getresponse();value=json.loads(r.read(262144));assert r.status==200 and value.get('ok')
finally:c.close()
