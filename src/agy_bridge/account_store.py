#!/usr/bin/env python3
"""Explicit account selection for the pinned official Antigravity CLI.
No quota-based rotation, production credential import, or global logout.
"""
import argparse,base64,contextlib,fcntl,hashlib,json,os,re,signal,stat,subprocess,sys,tempfile,threading,time,uuid
from dataclasses import dataclass
from pathlib import Path

VERSION='0.1.2'
BINARY=Path(os.environ.get('AGY_CLI_BINARY', '/opt/antigravity/antigravity'))
BINARY_SHA='9db59b6371107b939befa9c507c0a24989fda974e75d81d8529f3689ef7322e5'
DEFAULT_ROOT=Path(os.environ.get('AGY_ACCOUNTS_ROOT', '/accounts'))
TOKEN_REL=Path('.gemini/antigravity-cli/antigravity-oauth-token')
ALIAS=re.compile(r'^[A-Za-z0-9_-]{1,32}$')
class AccountError(Exception):pass

def private_dir(p):
 if p.is_symlink():raise AccountError('Refusing symlink directory: '+str(p))
 p.mkdir(parents=True,exist_ok=True,mode=0o700)
 if p.stat().st_uid!=os.getuid():raise AccountError('Wrong directory owner')
 p.chmod(0o700)

def atomic_json(path,data):
 fd,tmp=tempfile.mkstemp(prefix='.write-',dir=path.parent)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
  d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(d)
  finally:os.close(d)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def atomic_bytes(path,data):
 fd,tmp=tempfile.mkstemp(prefix='.credential-',dir=path.parent)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

@contextlib.contextmanager
def lock(path,exclusive=True,wait=True):
 fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 try:
  try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|(0 if wait else fcntl.LOCK_NB))
  except BlockingIOError:raise AccountError('Account is busy; its current request/login was left untouched')
  yield
 finally:os.close(fd)

@dataclass(frozen=True)
class Snapshot:
 alias:str
 home:Path
 workspace:Path
 generation:int
 expected_email:str

def environment(snapshot,parent=None):
 """Allow only host transport/locale, never ambient model auth or user keyring."""
 p=os.environ if parent is None else parent
 keep=['PATH','LANG','LC_ALL','TERM','COLORTERM','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy','all_proxy','no_proxy','SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE','SSH_CONNECTION','SSH_CLIENT','SSH_TTY']
 e={k:p[k] for k in keep if k in p};e.setdefault('PATH','/usr/local/bin:/usr/bin:/bin');e.setdefault('LANG','C.UTF-8')
 if not e.get('SSH_CONNECTION') and not e.get('SSH_CLIENT'):raise AccountError('This validated launcher requires the SSH file-auth environment; shared desktop keyring mode is not supported')
 e.update({'HOME':str(snapshot.home),'XDG_CONFIG_HOME':str(snapshot.home/'.config'),'XDG_DATA_HOME':str(snapshot.home/'.local/share'),'XDG_CACHE_HOME':str(snapshot.home/'.cache'),'XDG_STATE_HOME':str(snapshot.home/'.local/state'),'AGY_CLI_DISABLE_AUTO_UPDATE':'true'})
 return e

class Accounts:
 def __init__(self,root=DEFAULT_ROOT):
  self.root=Path(root).absolute();private_dir(self.root);private_dir(self.root/'profiles');self.state_path=self.root/'state.json'
  with lock(self.root/'state.lock'):
   if not self.state_path.exists():atomic_json(self.state_path,{'version':1,'generation':0,'active':None,'accounts':{}})
 def read(self):
  if self.state_path.is_symlink():raise AccountError('State file cannot be a symlink')
  s=json.loads(self.state_path.read_text())
  if s.get('version')!=1:raise AccountError('Unsupported state version')
  return s
 def _alias(self,a):
  if not ALIAS.fullmatch(a):raise AccountError('Account name must use 1–32 letters, digits, hyphens or underscores')
  return a
 def _paths(self,a):
  a=self._alias(a);p=self.root/'profiles'/a
  for x in [p,p/'home',p/'workspace']:
   if x.is_symlink():raise AccountError('Profile path cannot be a symlink')
  return p,p/'home',p/'workspace'
 def add(self,a,email=''):
  self._alias(a)
  if email and ('@' not in email or any(c.isspace() for c in email)):raise AccountError('Invalid expected email')
  with lock(self.root/'state.lock'):
   s=self.read()
   if a in s['accounts']:raise AccountError('Account already exists')
   p,h,w=self._paths(a)
   for x in [p,h,w,h/'.gemini',h/'.gemini/antigravity-cli',h/'.config',h/'.cache',h/'.local/share',h/'.local/state']:private_dir(x)
   s['accounts'][a]={'expected_email':email,'authorized_local':False,'observed_email':None,'created_at':time.time()};atomic_json(self.state_path,s)
 def _ready(self,a,record):
  p,h,w=self._paths(a);token=h/TOKEN_REL
  if not record.get('authorized_local'):return False
  try:st=token.lstat()
  except FileNotFoundError:return False
  return stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and not st.st_mode&0o077 and st.st_size>0
 def list(self):
  with lock(self.root/'state.lock',False):
   s=self.read();return {'active':s['active'],'generation':s['generation'],'accounts':[{'alias':a,**r,'ready':self._ready(a,r)} for a,r in s['accounts'].items()]}
 def use(self,a):
  self._alias(a)
  with lock(self.root/'state.lock'):
   s=self.read()
   if a not in s['accounts']:raise AccountError('Unknown account: '+a)
   if not self._ready(a,s['accounts'][a]):raise AccountError('Account '+a+' has not completed its independent official login; current selection unchanged')
   previous=s['active']
   if previous!=a:s['active']=a;s['generation']+=1;atomic_json(self.state_path,s)
   return {'previous':previous,'active':a,'generation':s['generation'],'running_requests_untouched':True}
 def snapshot(self,a=None,require_ready=True):
  with lock(self.root/'state.lock',False):
   s=self.read();a=self._alias(a or s['active'] or '')
   if a not in s['accounts']:raise AccountError('No selected/registered account')
   r=s['accounts'][a]
   if require_ready and not self._ready(a,r):raise AccountError('Account '+a+' needs official login; no automatic fallback')
   p,h,w=self._paths(a)
   return Snapshot(a,h,w,s['generation'],r['expected_email'])
 def _credential_email(self,path):
  try:
   data=json.loads(path.read_text());stack=[data]
   while stack:
    item=stack.pop()
    if not isinstance(item,dict):continue
    for k,v in item.items():
     if k=='id_token_email' and isinstance(v,str) and '@' in v:return v
     if k.lower() in ['id_token','idtoken'] and isinstance(v,str) and v.count('.')==2:
      q=v.split('.')[1];payload=json.loads(base64.urlsafe_b64decode(q+'='*((-len(q))%4)));email=payload.get('email')
      if isinstance(email,str):return email
     if isinstance(v,dict):stack.append(v)
  except (ValueError,KeyError,UnicodeError):pass
  return None
 def _record_login(self,snap,success):
  token=snap.home/TOKEN_REL;email=None;ready=False
  if success and token.exists() and not token.is_symlink() and token.stat().st_size:
   token.chmod(0o600);email=self._credential_email(token);ready=True
   if email and snap.expected_email and email.casefold()!=snap.expected_email.casefold():ready=False
  with lock(self.root/'state.lock'):
   state=self.read();r=state['accounts'][snap.alias];r.update({'authorized_local':ready,'observed_email':email,'last_login_at':time.time(),'last_login_success':ready})
   if not ready and state['active']==snap.alias:state['active']=None;state['generation']+=1
   atomic_json(self.state_path,state)
  if success and not ready:raise AccountError('Login did not produce the expected isolated credentials/account; profile remains unavailable')
  return ready
 def _check_binary(self):
  if hashlib.sha256(BINARY.read_bytes()).hexdigest()!=BINARY_SHA:raise AccountError('CLI binary changed; revalidate before using stored credentials')
 def _child(self,snap,args,timeout=None):
  self._check_binary();env=environment(snap);started=time.monotonic()
  mode=os.environ.get('AGY_CLI_PROC_MOUNT','1')
  if mode not in ('0','1'):raise AccountError('AGY_CLI_PROC_MOUNT must be 0 or 1')
  cmd=['unshare','-Urpf','--kill-child=TERM']+(['--mount-proc'] if mode=='1' else [])+[str(BINARY),*args]
  proc=subprocess.Popen(cmd,cwd=snap.workspace,env=env,start_new_session=True,umask=0o077)
  interrupted=False;old_term=None
  if threading.current_thread() is threading.main_thread():
   old_term=signal.getsignal(signal.SIGTERM)
   def stop_requested(signum,frame):raise KeyboardInterrupt
   signal.signal(signal.SIGTERM,stop_requested)
  try:
   return proc.wait(timeout=timeout)
  except KeyboardInterrupt:
   interrupted=True;return 130
  except subprocess.TimeoutExpired:
   interrupted=True;return 124
  finally:
   if old_term is not None:signal.signal(signal.SIGTERM,old_term)
   if proc.poll() is None:
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
     os.killpg(proc.pid,signal.SIGKILL);proc.wait()
   # Metadata only: never prompt contents, argv, output, or credential bytes.
   with lock(self.root/'audit.lock'):
    fd=os.open(self.root/'audit.jsonl',os.O_APPEND|os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(json.dumps({'alias':snap.alias,'selection_generation':snap.generation,'exit':proc.returncode,'seconds':round(time.monotonic()-started,3),'interrupted':interrupted})+'\n')
 def login(self,a,timeout=900):
  snap=self.snapshot(a,False);p,_,_=self._paths(a)
  with lock(p/'run.lock',wait=False):
   # Reauthorization is staged: cancellation/wrong-account login cannot destroy a saved session.
   with tempfile.TemporaryDirectory(prefix='login-',dir=p) as td:
    h=Path(td)/'home';w=Path(td)/'workspace'
    for x in [h,w,h/'.gemini/antigravity-cli',h/'.config',h/'.cache',h/'.local/share',h/'.local/state']:private_dir(x)
    staged=Snapshot(a,h,w,snap.generation,snap.expected_email)
    rc=self._child(staged,['acp','--login'],timeout)
    if rc!=0:return rc
    token=h/TOKEN_REL
    if not token.is_file() or token.is_symlink() or not token.stat().st_size:raise AccountError('Official login produced no isolated token file; existing profile unchanged')
    email=self._credential_email(token)
    if email and snap.expected_email and email.casefold()!=snap.expected_email.casefold():raise AccountError('Signed in to a different account; existing profile unchanged')
    rels=[TOKEN_REL,Path('.config/antigravity/credential-user-scope')]
    backups={};changed=[]
    try:
     for rel in rels:
      src=h/rel;dst=snap.home/rel
      if not src.exists():continue
      if src.is_symlink() or dst.is_symlink():raise AccountError('Credential path cannot be a symlink')
      private_dir(dst.parent);backups[rel]=dst.read_bytes() if dst.exists() else None
      atomic_bytes(dst,src.read_bytes());changed.append(rel)
     self._record_login(snap,True)
    except Exception:
     for rel in changed:
      dst=snap.home/rel;old=backups[rel]
      if old is None:dst.unlink(missing_ok=True)
      else:atomic_bytes(dst,old)
     raise
    return 0
 def run(self,args,alias=None):
  if not args:raise AccountError('Pass a CLI command after --')
  blocked=['--app-data-dir','--app_data_dir','--gemini-dir','--gemini_dir','--cli_dir_name','--remote-agent','--remote-control']
  if args[0] in ['update','install','remote-control'] or any(a.split('=',1)[0] in blocked for a in args):raise AccountError('This command would escape the isolated launcher scope')
  snap=self.snapshot(alias);p,_,_=self._paths(snap.alias)
  # Serialize each account, while allowing other accounts to finish independently.
  with lock(p/'run.lock'):
   self.snapshot(snap.alias) # recheck auth after waiting, never resolve active again
   return self._child(snap,args)

def main(argv=None):
 parser=argparse.ArgumentParser(description='Explicit, isolated Antigravity account switching')
 parser.add_argument('--root',type=Path,default=DEFAULT_ROOT);sub=parser.add_subparsers(dest='command',required=True)
 p=sub.add_parser('add');p.add_argument('alias');p.add_argument('--email',default='')
 sub.add_parser('list');sub.add_parser('current')
 p=sub.add_parser('use');p.add_argument('alias',nargs='?')
 p=sub.add_parser('login');p.add_argument('alias')
 p=sub.add_parser('run');p.add_argument('--account');p.add_argument('args',nargs=argparse.REMAINDER)
 args=parser.parse_args(argv)
 try:
  store=Accounts(args.root)
  if args.command=='add':store.add(args.alias,args.email);print('Created isolated profile '+args.alias+'; run login once.');return 0
  if args.command=='list':print(json.dumps(store.list(),ensure_ascii=False,indent=2));return 0
  if args.command=='current':print(store.list()['active'] or '(none)');return 0
  if args.command=='use':
   a=args.alias
   if a is None:
    if not sys.stdin.isatty():raise AccountError('Pass an account name in noninteractive mode')
    rows=store.list()['accounts']
    for i,r in enumerate(rows,1):print(f"{i}. {r['alias']} — {r['expected_email']} — {'ready' if r['ready'] else 'needs login'}")
    answer=input('Select number: ').strip()
    if not answer.isdigit() or not 1<=int(answer)<=len(rows):raise AccountError('Invalid selection')
    a=rows[int(answer)-1]['alias']
   r=store.use(a);print(f"Selected {r['active']}; only new launches use this account. Existing requests are unchanged.");return 0
  if args.command=='login':return store.login(args.alias)
  if args.command=='run':return store.run(args.args[1:] if args.args[:1]==['--'] else args.args,args.account)
 except (AccountError,OSError,ValueError) as e:print('Error: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
