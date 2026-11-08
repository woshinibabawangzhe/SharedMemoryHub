"""受控真机合成验收。Docker 缺口如实记录；不切换向量后端。"""
import argparse,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
for key,suffix in {'TEMP':'tmp','TMP':'tmp','HF_HOME':'hf','TORCH_HOME':'torch','XDG_CACHE_HOME':'xdg'}.items():
    path=ROOT/'cache'/suffix;path.mkdir(parents=True,exist_ok=True);os.environ[key]=str(path)
import httpx
from hub.db import MemoryDatabase
from hub.auth import AuthManager
from hub.embedder import QdrantManager,EmbeddingEngine


def run(a):
    root=ROOT/'receipts/v0.3.0-implementation'/('live-'+uuid.uuid4().hex[:12]);root.mkdir(exist_ok=False)
    origin=f'http://127.0.0.1:{a.port}';prefix='smh030_'+uuid.uuid4().hex[:12]
    fixture_passcode='Synthetic-fixture-030'
    auth=AuthManager(root/'config/client_tokens.json',root/'config/admin_auth.json',initial_passcode=fixture_passcode,allowed_origin=origin)
    db=MemoryDatabase(root/'data/sqlite/memory_hub.db',root/'data/tombstones/purge_registry.jsonl',root/'exports',root/'backups')
    workspace=root/'workspace';workspace.mkdir();db.register_project_binding('synthetic_live',workspace)
    result={'version':'0.3.0','evidence_level':'controlled-trial','runtime':str(root),'port':a.port,'prefix':prefix,'steps':[]}
    target=ROOT/'receipts/v0.3.0-implementation/current-live.json'
    target.write_text(json.dumps({'runtime':str(root),'port':a.port,'prefix':prefix,'qdrant_port':a.qdrant_port},indent=2),encoding='utf-8')
    control=[sys.executable,'-X','utf8','-B',str(ROOT/'scripts/runtime_control.py')]
    def control_call(action):
        cmd=control+[action,'--runtime',str(root),'--port',str(a.port),'--qdrant-port',str(a.qdrant_port),'--prefix',prefix]
        r=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',timeout=55)
        if r.returncode:raise RuntimeError(r.stderr[-1800:])
        return json.loads(r.stdout)
    def cli(client,command):
        cmd=[sys.executable,'-X','utf8','-B',str(ROOT/'clients/memory_cli.py'),'--client',client,'--tokens-path',str(root/'config/client_tokens.json'),'--base-url',origin,*command]
        r=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',timeout=10)
        if r.returncode:raise RuntimeError(r.stdout[-1500:]+r.stderr[-500:])
        return json.loads(r.stdout)
    started=False
    try:
        result['start']=control_call('start');started=True
        result['steps'].append('real service startup and bounded health')
        ids=[]
        for client,label in [('antigravity','甲端合成偏好'),('codex','乙端合成偏好')]:
            r=cli(client,['write','--op','create','--request-key',uuid.uuid4().hex,'--workspace',str(workspace),'--kind','preference',
                '--title',label,'--content','合成偏好：请使用简体中文，引用可核验来源。','--origin-type','user_explicit','--session-id','live-030','--evidence-ref','合成用户明确原话'])
            ids.append(r['memory_id'])
        for client in ('antigravity','codex'):
            r=cli(client,['search','--workspace',str(workspace),'--query','简体中文'])
            assert set(ids)<=set(m['memory_id'] for m in r['items'])
            result['steps'].append(client+' CLI write and shared recall')
        with httpx.Client(base_url=origin,trust_env=False,timeout=10) as c:
            session=c.post('/v1/admin/login',headers={'Origin':origin},json={'passcode':fixture_passcode});session.raise_for_status()
            hdr={'Origin':origin,'X-CSRF-Token':session.json()['csrf_token']}
            q=QdrantManager(base_url=f'http://127.0.0.1:{a.qdrant_port}',collection_prefix=prefix)
            result['docker_available']=q.is_healthy()
            if result['docker_available']:
                deadline=time.monotonic()+75
                while time.monotonic()<deadline:
                    items=c.get('/v1/admin/memories').json()['items']
                    if all(m['index_status']=='synced' for m in items):break
                    time.sleep(.5)
                else:raise AssertionError('automatic worker did not synchronize in time')
                assert set(ids)<=set(x['memory_id'] for x in q.scroll_all_payloads('v1'))
                result['steps'].append('CUDA automatic worker -> Docker Qdrant -> payload verification')
            else:
                result['blocked']=['Docker Qdrant isolated container did not become reachable; no alternate vector backend used']
                r=c.post('/v1/memory/search',headers={'Authorization':'Bearer '+auth.get_client_tokens()['CODEX_CLIENT_TOKEN']},json={'query':'简体中文','workspace_path':str(workspace)})
                assert r.json()['degraded_mode']=='keyword_only';result['steps'].append('real keyword-only degradation')
            q.close()
        result['first_stop']=control_call('stop');started=False
        result['restart']=control_call('start');started=True
        health=httpx.get(origin+'/v1/health',trust_env=False).json();assert health['maintenance_mode']=='normal'
        result['steps'].append('manual stop releases port; real restart recovers service')
        result['status']='部分验证' if result.get('blocked') else '已验证'
        if a.leave_running:
            result['left_running_for_browser']=True;started=False
    except Exception as error:
        result['status']='部分验证';result['error']=type(error).__name__+': '+str(error)
        raise
    finally:
        if started:result['final_stop']=control_call('stop')
        (root/'live-receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'runtime':str(root),'status':result.get('status'),'blocked':result.get('blocked'),'error':result.get('error')},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18769);p.add_argument('--qdrant-port',type=int,default=16339);p.add_argument('--leave-running',action='store_true')
    run(p.parse_args())
