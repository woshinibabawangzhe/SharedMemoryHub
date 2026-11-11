"""Controlled CUDA/Qdrant workbench fixture. Never targets a business collection."""
import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from hub.db import MemoryDatabase
from hub.auth import AuthManager
from scripts.runtime_control import IMAGE, start, stop, port_free

EVIDENCE=checked_path(ROOT/'receipts/workbench-040-20261109')
RUNTIME=checked_path(EVIDENCE/'live-runtime')
PORT=18865;QPORT=16333;PREFIX='smh_workbench_040_1109';CONTAINER='smh-workbench-040-1109'
# Public, disposable synthetic test fixture, never a personal administrator password.
FIXTURE_PASSCODE='Synthetic-only-workbench-040'

def db():return MemoryDatabase(RUNTIME/'data/sqlite/memory_hub.db',RUNTIME/'data/tombstones/purge_registry.jsonl',RUNTIME/'exports',RUNTIME/'backups')
def auth():return AuthManager(RUNTIME/'config/client_tokens.json',RUNTIME/'config/admin_auth.json',initial_passcode=FIXTURE_PASSCODE,allowed_origin=f'http://127.0.0.1:{PORT}')
def args():return argparse.Namespace(runtime=str(RUNTIME),port=PORT,qdrant_port=QPORT,prefix=PREFIX,device='cuda',python=None,model_dir=None)
def record(name,data):
    (EVIDENCE/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(data,ensure_ascii=False))

def setup():
    if RUNTIME.exists():raise RuntimeError('fixture_already_exists; inspect before rerun')
    if not port_free(PORT) or not port_free(QPORT):raise RuntimeError('fixture_port_in_use')
    RUNTIME.mkdir(parents=True);storage=checked_path(RUNTIME/'qdrant');storage.mkdir()
    subprocess.run(['docker','run','-d','--name',CONTAINER,'--restart=no','-p',f'127.0.0.1:{QPORT}:6333','-e','QDRANT__TELEMETRY_DISABLED=true','--mount',f'type=bind,source={storage},target=/qdrant/storage',IMAGE],check=True,capture_output=True)
    d=db();auth();ws=RUNTIME/'workspace';ws.mkdir();d.register_project_binding('fixture-040',ws)
    source=d.register_source_snapshot('fixture-040-source','合成来源 · 管理闭环','合成资料，不包含个人信息。\nHTML样文本 <img src=x onerror=alert(1)> 必须按纯文本呈现。')
    ids=[]
    for i in range(30):
        p={'request_key':f'fixture040-record-{i}','operation':'create','subject':'user','scope':'user_global','kind':'fact','domain_id':'tools','tags':['合成验收'],'title':f'合成档案 {i:02d}｜代理与工具','content':f'第{i}条合成正文。代理配置只描述无敏感背景。路径 E:/合成资料/正文.md。\n<img src=x onerror=alert(1)>','source':{'origin_type':'admin_manual',**source}}
        ids.append(d.write_memory('user_admin','user','web_ui',p)['memory_id'])
    p={'request_key':'fixture040-proposal','operation':'create','subject':'user','scope':'user_global','kind':'preference','domain_id':'communication','title':'合成偏好｜等待人工批准','content':'合成建议正文：喜欢简体中文。','source':{'origin_type':'user_explicit','session_id':'synthetic-fixture-040','evidence_ref':'合成明确原话，仅供验收。'}}
    proposal=d.write_memory('agent_client','codex','codex',p)
    record('live-fixture.json',{'runtime':str(RUNTIME),'port':PORT,'qdrant_port':QPORT,'prefix':PREFIX,'container':CONTAINER,'memory_ids':ids+[proposal['memory_id']],'proposal_id':proposal['proposal_id']})
    record('live-start.json',start(args()))

def check():
    import httpx
    with httpx.Client(base_url=f'http://127.0.0.1:{PORT}',trust_env=False,timeout=35) as client:
        headers={'Origin':f'http://127.0.0.1:{PORT}'}
        session=client.post('/v1/admin/login',headers=headers,json={'passcode':FIXTURE_PASSCODE});session.raise_for_status();headers['X-CSRF-Token']=session.json()['csrf_token']
        health=client.get('/v1/health').json()
        assert health['qdrant']=='ok' and health['embedding_device']=='cuda' and health['embedding_model_loaded'],health
        deadline=time.monotonic()+60
        while health['pending_index_work'] or health['processing_index_work']:
            if time.monotonic()>deadline:raise RuntimeError('worker_not_drained')
            time.sleep(.5);health=client.get('/v1/health').json()
        assert health['failed_index_work']==0,health
        query=client.post('/v1/admin/query',headers=headers,json={'filters':{'query':'代理','mode':'semantic'},'limit':25});query.raise_for_status();q=query.json()
        assert q['items'] and not q['degraded_mode'],q
        from hub.embedder import QdrantManager
        vec=QdrantManager(base_url=f'http://127.0.0.1:{QPORT}',collection_prefix=PREFIX)
        payloads=vec.scroll_all_payloads(health['active_index_generation']);vec.close()
        assert payloads and all(p['generation']==health['active_index_generation'] for p in payloads)
        sources=client.get('/v1/admin/sources',headers=headers);sources.raise_for_status()
        record('live-cuda-qdrant.json',{'status':'passed','health':health,'vector_points':len(payloads),'literal_plus_semantic_results':q['total'],'source_versions':len(sources.json()['items'])})

def cleanup():
    # Service is stopped first; only this exact isolated fixture is opened offline.
    stop_result=stop(args())
    d=db();fixture=json.loads((EVIDENCE/'live-fixture.json').read_text(encoding='utf-8'))
    from hub.embedder import QdrantManager
    vec=QdrantManager(base_url=f'http://127.0.0.1:{QPORT}',collection_prefix=PREFIX)
    generations=vec.list_generations();results=[]
    con=d.connect();ids=[(r[0],r[1]) for r in con.execute('SELECT memory_id,revision FROM current_memory')];con.close()
    for mid,revision in ids:
        results.append(d.admin_purge(mid,f'PURGE {mid} r{revision}',revision,lambda m:all(vec.delete_point(g,m) for g in generations)))
    assert all(r['status']=='purged' for r in results)
    assert d.workbench_list({'state':'deleted'})['total']==0 and d.workbench_list()['total']==0
    for g in generations:assert not vec.scroll_all_payloads(g)
    vec.close();subprocess.run(['docker','stop',CONTAINER],check=True,capture_output=True)
    record('synthetic-cleanup.json',{'status':'passed','removed_fixture_records':len(results),'remaining_sqlite_records':0,'remaining_vector_points':0,'service_stop':stop_result,'container_stopped':True,'business_data_touched':False})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['setup','check','cleanup']);a=p.parse_args();globals()[a.action]()
