"""Offline compatibility experiment against CLI 1.5.8 private session storage.
Only writes disposable per-request directories. Never reads real account state.
"""
import json,os,sqlite3,subprocess,threading,time,uuid,traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parent
PARENT=ROOT.parent
CLI='/opt/antigravity/antigravity'
SEED=ROOT/'fixtures/session.db'
OLD_CID='00000000-0000-4000-8000-000000000001';OLD_TID='00000000-0000-4000-8000-000000000002'
OLD_WORK='/AGY_BRIDGE_WORK';OLD_SYS='AGY_BRIDGE_SYSTEM'
def vi(n):
 out=bytearray()
 while n>127:out.append((n&127)|128);n>>=7
 out.append(n);return bytes(out)
def field(n,w,v):
 if w==0:return vi(n<<3)+vi(v)
 if isinstance(v,str):v=v.encode()
 return vi((n<<3)|w)+(vi(len(v)) if w==2 else b'')+v
def fields(b):
 pos=0;out=[]
 def rv():
  nonlocal pos
  n=0;s=0
  while pos<len(b) and s<70:
   x=b[pos];pos+=1;n|=(x&127)<<s;s+=7
   if not x&128:return n
  raise ValueError('bad varint')
 while pos<len(b):
  t=rv();n=t>>3;w=t&7
  if not n:raise ValueError('zero tag')
  if w==0:v=rv()
  elif w in [1,5]:
   ln=8 if w==1 else 4;v=b[pos:pos+ln];pos+=ln
  elif w==2:ln=rv();v=b[pos:pos+ln];pos+=ln
  else:raise ValueError('bad wire')
  if pos>len(b):raise ValueError('truncated')
  out.append((n,w,v))
 return out
def remap(b,replacements):
 try:fs=fields(b)
 except ValueError:
  for old,new in replacements.items():b=b.replace(old.encode(),new.encode())
  return b
 return b''.join(field(n,w,remap(v,replacements) if w==2 else v) for n,w,v in fs)
def metadata(idx,role,cid,tid):
 now=time.time_ns();stamp=field(1,0,now//10**9)+field(2,0,now%10**9)
 ref=field(1,2,tid)+field(2,0,idx)+field(4,2,cid)
 return field(1,2,stamp)+field(3,0,4 if role=='user' else 2)+field(12,2,str(uuid.uuid4()))+field(20,2,ref)+field(35,2,uuid.uuid4().hex)
def native_history_order(history):
 # The pinned CLI converter pairs a parallel call group in declaration order.
 # Accept arbitrary caller result order, then match IDs within that single group;
 # preserve every body and never move results across an assistant/user boundary.
 result=[];i=0
 while i<len(history):
  m=history[i];result.append(m);i+=1
  if m['role']=='assistant' and m.get('tool_calls'):
   end=i
   while end<len(history) and history[end]['role']=='tool':end+=1
   by_id={r['tool_call_id']:r for r in history[i:end]}
   result.extend(by_id[c['id']] for c in m['tool_calls'])
   i=end
 return result

def account_session_ids(alias, expected_email):
 # Pin identifiers to the authenticated account, never its token, model,
 # request text, temporary path, or current account-manager selection.
 # Aliases for the same known email deliberately share one account identity.
 email=(expected_email or '').strip().casefold()
 identity=['email',email] if email else ['alias',alias]
 namespace=uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(['agy-cli-account-session-v1',*identity],ensure_ascii=False,separators=(',',':')))
 return str(uuid.uuid5(namespace,'cascade')),str(uuid.uuid5(namespace,'trajectory'))

def seed(home,work,system,history,*,session_ids=None):
 # Stable ids do not mean shared history: every request still gets its own
 # private HOME/database, seeded only with that request's supplied messages.
 cid,tid=session_ids if session_ids is not None else (str(uuid.uuid4()),str(uuid.uuid4()))
 dst=home/'.gemini/antigravity-cli/conversations'/f'{cid}.db';dst.parent.mkdir(parents=True,exist_ok=True)
 source=sqlite3.connect(f'file:{SEED}?mode=ro',uri=True);target=sqlite3.connect(dst);source.backup(target);source.close()
 for table in ['steps','gen_metadata','executor_metadata','parent_references','battle_mode_infos']:target.execute('delete from '+table)
 target.execute('update trajectory_meta set trajectory_id=?,cascade_id=?',(tid,cid))
 reps={OLD_CID:cid,OLD_TID:tid,OLD_WORK:str(work),OLD_SYS:system}
 for k,b in target.execute('select id,data from trajectory_metadata_blob').fetchall():target.execute('update trajectory_metadata_blob set data=? where id=?',(remap(b,reps),k))
 calls={}; call_executions={}
 def call_bytes(call):
  fn=call['function']
  return field(1,2,call['id'])+field(2,2,fn['name'])+field(3,2,fn['arguments'])+field(8,2,fn['arguments'])+field(9,2,fn['name'])
 for idx,m in enumerate(native_history_order(history)):
  role=m['role'];text=m.get('content') or '';meta=metadata(idx,role,cid,tid)
  if role=='user':
   typ=14;payload=field(2,2,text)+field(3,2,field(1,2,text));slot=19
  elif role=='assistant':
   typ=15;payload=field(1,2,text)+field(8,2,text)+field(12,0,2);slot=20
   for call in m.get('tool_calls',[]):
    calls[call['id']]=call;call_executions[call['id']]=next(v for n,w,v in fields(meta) if n==12);payload+=field(7,2,call_bytes(call))
  elif role=='tool':
   typ=38;slot=47;call=call_bytes(calls[m['tool_call_id']])
   # Tool results are caller-provided data, never a tool scheduled for execution.
   # Keep one invocation ID for the original assistant and all its results.
   # Timestamp decoration is disabled in the native conversion config.
   meta=b''.join(field(n,w,v) for n,w,v in fields(meta) if n != 12)+field(12,2,call_executions[m['tool_call_id']])+field(4,2,call)
   payload=field(1,2,'api_client')+field(2,2,call)+field(3,2,text)
  else:raise ValueError('unsupported history role '+role)
  msg=field(1,0,typ)+field(4,0,3)+field(5,2,meta)+field(slot,2,payload)
  target.execute('insert into steps(idx,step_type,status,has_subtrajectory,metadata,step_payload,step_format) values (?,?,3,0,?,?,0)',(idx,typ,meta,msg))
 target.commit();target.close();return cid
