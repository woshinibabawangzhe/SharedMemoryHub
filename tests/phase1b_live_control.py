"""1B独立合成真机夹具；不读取或修改默认业务数据库和认证。"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hub.paths import checked_path

IMAGE='qdrant/qdrant@sha256:318c11b72aaab96b36e9662ad244de3cabd0653a1b942d4e8191f18296c81af0'


def readmeta(evidence):
    return json.loads((evidence/'live-instance.json').read_text(encoding='utf-8'))


def admin_client(meta):
    import httpx
    client=httpx.Client(base_url=meta['origin'],trust_env=False,timeout=30)
    response=client.post('/v1/admin/login',headers={'Origin':meta['origin']},json={'passcode':'Synthetic-phase1b-'+meta['run_id']})
    response.raise_for_status()
    client.headers.update({'Origin':meta['origin'],'X-CSRF-Token':response.json()['csrf_token']})
    return client


def prepare(evidence):
    from hub.auth import AuthManager
    from hub.db import MemoryDatabase
    evidence=checked_path(evidence)
    meta_path=evidence/'live-instance.json'
    if meta_path.exists():raise RuntimeError('existing_fixture_preserved')
    runid=uuid.uuid4().hex[:10]
    runtime=checked_path(evidence/'live-runtime')
    workspace=checked_path(evidence/'workspace')
    workspace.mkdir(parents=True)
    (workspace/'README.md').write_text('# 合成项目：报告验收演练\n\n目标：验证项目报告方式在新会话中的可靠执行。\n当前工作：分析报告验收方案，给出简短的测试安排与风险判断。\n这是独立合成资料，不包含真实个人记忆或生产工作。\n',encoding='utf-8')
    AuthManager(runtime/'config/client_tokens.json',runtime/'config/admin_auth.json',initial_passcode='Synthetic-phase1b-'+runid,allowed_origin='http://127.0.0.1:18789')
    db=MemoryDatabase(db_path=runtime/'data/sqlite/memory_hub.db',tombstone_path=runtime/'data/tombstones/purge_registry.jsonl',exports_dir=runtime/'exports',backups_dir=runtime/'backups')
    db.register_project_binding('proj_smh1b_synthetic',workspace)
    (runtime/'qdrant').mkdir()
    meta={'run_id':runid,'runtime_root':str(runtime),'workspace':str(workspace),'origin':'http://127.0.0.1:18789','qdrant_port':16379,'container':'smh1b-'+runid,'prefix':'smh1b_'+runid,'image':IMAGE,'project_id':'proj_smh1b_synthetic'}
    meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    profile={'version':'0.1.0','enabled':True,'mode':'controlled_trial','runtime_root':str(runtime),'base_url':meta['origin'],'registered_workspaces':[str(workspace)],'evidence_root':str(evidence)}
    profile_path=ROOT/'runtime/codex/profile.json'
    checked_path(profile_path).parent.mkdir(parents=True,exist_ok=True)
    if profile_path.exists():raise RuntimeError('existing_profile_preserved')
    profile_path.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    (evidence/'trial-profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    result=subprocess.run(['docker','run','-d','--pull=never','--name',meta['container'],'-p','127.0.0.1:16379:6333','--mount','type=bind,source='+str(runtime/'qdrant')+',target=/qdrant/storage','-e','QDRANT__TELEMETRY_DISABLED=true',IMAGE],capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('isolated_qdrant_start_failed: '+result.stderr[-600:])
    import httpx
    with httpx.Client(trust_env=False) as client:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                if client.get('http://127.0.0.1:16379/healthz',timeout=1).is_success:break
            except httpx.HTTPError:pass
            time.sleep(.25)
        else:raise TimeoutError('isolated_qdrant_not_healthy')
    service=subprocess.run([sys.executable,'-X','utf8','-B',str(ROOT/'scripts/runtime_control.py'),'start','--runtime',str(runtime),'--port','18789','--qdrant-port','16379','--prefix',meta['prefix'],'--device','cuda'],capture_output=True,text=True,encoding='utf-8',timeout=60)
    (evidence/'service-start.json').write_text(service.stdout,encoding='utf-8')
    if service.returncode:raise RuntimeError('isolated_hub_start_failed: '+service.stderr[-600:])
    return seed(evidence)


def seed(evidence):
    meta=readmeta(evidence)
    workspace=Path(meta['workspace']);runid=meta['run_id'];runtime=Path(meta['runtime_root']);meta_path=evidence/'live-instance.json'
    with closing(admin_client(meta)) as client:
        created=client.post('/v1/memory/write',json={'request_key':'seed-'+runid,'operation':'create','subject':'project','scope':'project','workspace_path':str(workspace),'kind':'preference','topic_key':'report.acceptance','title':'项目验收报告的结尾校验词','content':'本项目的验收简报使用简体中文，先给当前判断，再列测试和风险。最终校验词为：山茶初版。','confidence':'confirmed','source':{'origin_type':'admin_manual','summary':'独立合成验收种子'}})
        created.raise_for_status();seed=created.json()
        meta['seed_memory_id']=seed['memory_id']
        meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
        # 模型首次加载与CUDA热身单列，不能将冷启动耗时算作日常客户端离线。
        warmed=client.post('/v1/memory/search',json={'query':'报告 验收 校验词','workspace_path':str(workspace),'limit':5})
        warmed.raise_for_status()
        (evidence/'warmup-search.json').write_text(json.dumps(warmed.json(),ensure_ascii=False,indent=2),encoding='utf-8')
    return {'status':'prepared','runtime':str(runtime),'workspace':str(workspace),'origin':meta['origin'],'seed_memory_id':meta['seed_memory_id']}


def edit_lock(evidence):
    meta=readmeta(evidence)
    with closing(admin_client(meta)) as client:
        old=client.get('/v1/memory/'+meta['seed_memory_id'],params={'workspace_path':meta['workspace']}).json()
        row=old.get('memory',old.get('item',old))
        response=client.post('/v1/memory/write',json={'request_key':'edit-'+meta['run_id'],'operation':'update','memory_id':meta['seed_memory_id'],'expected_revision':row['revision'],'content':'本项目的验收简报使用简体中文，先给当前判断，再列测试和风险。最终校验词已由管理员更新为：银杏新版。'})
        response.raise_for_status()
        result=response.json()
        (evidence/'admin-edit-lock.json').write_text(json.dumps({'method':'real_admin_http','human_clicked':False,'result':result},ensure_ascii=False,indent=2),encoding='utf-8')
        return result


def stop(evidence):
    meta=readmeta(evidence)
    result=subprocess.run([sys.executable,'-X','utf8','-B',str(ROOT/'scripts/runtime_control.py'),'stop','--runtime',meta['runtime_root']],capture_output=True,text=True,encoding='utf-8',timeout=60)
    if result.returncode:raise RuntimeError('service_stop_unconfirmed')
    return json.loads(result.stdout)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','seed','edit-lock','stop']);p.add_argument('--evidence',required=True,type=Path);a=p.parse_args()
    root=checked_path(a.evidence)
    print(json.dumps({'prepare':prepare,'seed':seed,'edit-lock':edit_lock,'stop':stop}[a.action](root),ensure_ascii=False,indent=2))
