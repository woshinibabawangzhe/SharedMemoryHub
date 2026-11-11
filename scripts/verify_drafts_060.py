"""受控真机草稿验收实例；只有合成内容，使用独立运行目录及向量集合。"""
import argparse
import json
import socket
import sys
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from hub.auth import AuthManager
from hub.db import MemoryDatabase
from scripts.runtime_control import start,stop

FIXTURE_PASSCODE='Synthetic-memory-drafts-060'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['start','stop']);parser.add_argument('--evidence',required=True,type=Path)
    args=parser.parse_args();evidence=checked_path(args.evidence);evidence.mkdir(parents=True,exist_ok=True)
    runtime=checked_path(evidence/'live-synthetic')
    if args.action=='stop':
        result=stop(argparse.Namespace(runtime=str(runtime)));print(json.dumps(result));return
    if (runtime/'service.json').exists():raise RuntimeError('already_prepared; inspect existing instance instead of starting again')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    prefix='smh_drafts_060_'+uuid.uuid4().hex[:12]
    AuthManager(runtime/'config/client_tokens.json',runtime/'config/admin_auth.json',initial_passcode=FIXTURE_PASSCODE,allowed_origin=f'http://127.0.0.1:{port}')
    db=MemoryDatabase(runtime/'data/sqlite/memory_hub.db',runtime/'data/tombstones/purge_registry.jsonl',runtime/'exports',runtime/'backups')
    workspace=runtime/'fixture-workspace';workspace.mkdir();db.register_project_binding('synthetic-drafts',workspace)
    target=db.write_memory('user_admin','user','web_ui',{'request_key':uuid.uuid4().hex,'operation':'create','subject':'user','scope':'user_global','kind':'preference','title':'合成资料｜回答偏好','content':'合成用户此前偏好简洁回答，仅为隔离验收。','source':{'origin_type':'admin_manual'}})
    spec={'runtime':str(runtime),'url':f'http://127.0.0.1:{port}','port':port,'prefix':prefix,'workspace':str(workspace),'target_memory_id':target['memory_id'],'data_kind':'synthetic-only','auth_kind':'disposable synthetic fixture; no personal credentials'}
    (evidence/'live-spec.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf-8')
    result=start(argparse.Namespace(runtime=str(runtime),port=port,qdrant_port=6333,prefix=prefix,device='cuda',python=None,model_dir=None,auth_dir=str(runtime/'config')))
    (evidence/'live-start.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**spec,'health':result['health']},ensure_ascii=False))


if __name__=='__main__':main()
