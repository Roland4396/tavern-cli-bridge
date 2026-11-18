"""Import only static protocol enum names and numbers supplied by the operator."""
import argparse,json,re
from pathlib import Path
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('mapping',type=Path);args=ap.parse_args()
    d=json.loads(args.mapping.read_text())
    if not isinstance(d,dict) or not d or not all(re.fullmatch(r'MODEL_[A-Z0-9_]+',k) and type(v) is int and 0<=v<2**31 for k,v in d.items()):
        ap.error('Expected only MODEL_NAME: integer entries')
    target=Path(__file__).resolve().parents[1]/'src/agy_bridge/fixtures/model-enums.local.json'
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(d,sort_keys=True)+'\n')
if __name__=='__main__':main()
