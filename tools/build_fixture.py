"""Build an empty CLI test fixture entirely from schema and synthetic constants."""
from pathlib import Path
import sqlite3, sys, tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agy_bridge.protocol import field, OLD_CID, OLD_TID

def wire(items):
    return b"".join(field(k,0 if isinstance(v,int) else 2,wire(v) if isinstance(v,list) else v) for k,v in items)
def main():
    work="file:///AGY_BRIDGE_WORK"; agent=work+"/.agents/agents/plain-chat.md"
    mix=[(2,[(1,[(1,"System Prompt"),(2,"AGY_BRIDGE_SYSTEM")])])]
    mix += [(5,[(1,key),(2,1)]) for key in ("message_continue_check","terminal_step_check","max_generator_invocations_check","empty_output_continuation_check","force_invocation","no_tool_call_check")]
    opts=[(1,mix),(4,[(1,[(2,1)]),(2,[(1,1)]),(4,[(1,1)]),(7,[(1,1)]),(8,agent),(9,[(1,1)])])]
    data=wire([(1,[(1,work),(3,[])]),(2,[(1,1),(2,0)]),(3,"00000000-0000-4000-8000-000000000003"),(4,[(1,"plain-chat"),(8,"Synthetic fixture."),(12,0),(13,1),(7,[(18,opts)])]),(6,OLD_CID),(7,work),(10,[(17,[(16,agent)]),(18,opts)]),(18,"default-cli-project")])
    dest=ROOT/"src/agy_bridge/fixtures/session.db";dest.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as td:
        out=Path(td)/"seed.db"; db=sqlite3.connect(out)
        db.executescript(Path(__file__).with_name("fixture-schema.sql").read_text())
        db.execute("insert into trajectory_meta values (?,?,4,17)",(OLD_TID,OLD_CID))
        db.execute("insert into trajectory_metadata_blob values ('main',?)",(data,))
        db.commit(); db.close(); out.replace(dest)
if __name__=="__main__": main()
