"""CPU-only adapter child, kept alive while the parent drains active requests."""
import json,os,signal,socket,subprocess,sys,threading,time
from pathlib import Path

class NonstreamRuntime:
    def __init__(self,state,models):
        self.state=Path(state);self.models=tuple(sorted(models));self.proc=None;self.log=None;self.ready=False;self.stop=threading.Event();self.thread=None
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700);self.state.chmod(0o700)
    def _spawn(self):
        self.ready=False
        if self.log:self.log.close()
        p=self.state/'service.log'
        if p.exists() and p.stat().st_size>262144:p.replace(self.state/'service.log.old')
        self.log=p.open('a');p.chmod(0o600)
        env={'PATH':os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'),'LANG':'C.UTF-8','HOME':'/tmp','PYTHONPATH':'/app'}
        args=[sys.executable,str(Path(__file__).with_name('guard.py')),str(os.getpid()),sys.executable,'-m','agy_bridge.nonstream_proxy','--state',str(self.state),'--models',json.dumps(self.models)]
        self.proc=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=self.log,stderr=self.log,env=env,start_new_session=True,umask=0o077)
        # This is a startup readiness bound, not an inference or idle deadline.
        until=time.monotonic()+30
        while not self.stop.is_set() and time.monotonic()<until:
            if self.proc.poll() is not None:raise RuntimeError('Nonstream adapter exited during startup')
            try:
                with socket.create_connection(('127.0.0.1',17863),timeout=.2):pass
                if (self.state/'trust.pem').is_file():self.ready=True;return
            except OSError:pass
            time.sleep(.1)
        raise RuntimeError('Nonstream adapter did not become ready')
    def start(self):
        self._spawn();self.thread=threading.Thread(target=self._watch,name='nonstream-supervisor',daemon=True);self.thread.start();return self
    def _watch(self):
        while not self.stop.wait(.5):
            if self.proc.poll() is None:continue
            self.ready=False
            if self.stop.wait(1):return
            try:self._spawn()
            except Exception:
                self.ready=False
                if self.proc and self.proc.poll() is None:
                    try:self.proc.terminate();self.proc.wait(timeout=4)
                    except Exception:pass
                self.stop.wait(2)
    def status(self):return {'models':list(self.models),'ready':bool(self.ready and self.proc and self.proc.poll() is None),'mode':'google-nonstream-json','body_recording':False,'tcp_idle_timeout':0}
    def close(self):
        self.stop.set();self.ready=False
        if self.thread:self.thread.join(timeout=2)
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=2)
        if self.log:self.log.close()
