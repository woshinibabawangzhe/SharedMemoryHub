"""正式实例手动启停；保持旧库、历史容器及全局配置。"""
import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from scripts.runtime_control import identity
IMAGE='qdrant/qdrant@sha256:318c11b72aaab96b36e9662ad244de3cabd0653a1b942d4e8191f18296c81af0'
CONTAINER='smh-codex-memory'
RUNTIME=ROOT/'runtime/codex'


def run(args, timeout=45):
    result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    if result.returncode:raise RuntimeError('command_failed: '+str(args[:2])+'; '+result.stderr[-400:])
    return result.stdout.strip()


def container_state():
    names=run(['docker','ps','-a','--filter','name=^/'+CONTAINER+'$','--format','{{.Names}}'])
    if not names:return None
    image=run(['docker','inspect','--format','{{.Config.Image}}',CONTAINER])
    mounts=json.loads(run(['docker','inspect','--format','{{json .Mounts}}',CONTAINER]))
    ports=json.loads(run(['docker','inspect','--format','{{json .HostConfig.PortBindings}}',CONTAINER]))
    if image!=IMAGE or not any(Path(m['Source']).absolute()==checked_path(RUNTIME/'qdrant') and m['Destination']=='/qdrant/storage' for m in mounts):raise RuntimeError('container_identity_mismatch')
    if ports.get('6333/tcp') != [{'HostIp':'127.0.0.1','HostPort':'6333'}]:raise RuntimeError('container_port_identity_mismatch')
    return run(['docker','inspect','--format','{{.State.Status}}',CONTAINER])


def ready_health():
    # 仅显式手动启动预热；普通记忆调用仍采用1.5/5秒超时且不启动服务。
    import httpx
    from clients.memory_cli import call_hub
    with httpx.Client(trust_env=False,timeout=5) as client:
        health=client.get('http://127.0.0.1:8765/v1/health').json()
    if not health.get('embedding_model_loaded'):
        result=call_hub('POST','/v1/memory/search',client_name='codex',payload={'workspace_path':str(ROOT),'query':'记忆中心启动就绪检查','limit':1},base_url='http://127.0.0.1:8765',timeout_sec=60,tokens_path=RUNTIME/'config/client_tokens.json')
        if result.get('http_status')!=200:raise RuntimeError('model_warmup_unconfirmed; service preserved for explicit stop')
        with httpx.Client(trust_env=False,timeout=5) as client:
            health=client.get('http://127.0.0.1:8765/v1/health').json()
    if health.get('qdrant')!='ok' or not health.get('embedding_model_loaded') or health.get('embedding_device')!='cuda':
        raise RuntimeError('daily_readiness_unconfirmed')
    return health


def control(action):
    checked_path(RUNTIME)
    if not (RUNTIME/'config/admin_auth.json').is_file():raise RuntimeError('请先双击“初始化Codex记忆中心.cmd”设置口令；不会读取或重置旧认证。')
    profile=json.loads((RUNTIME/'profile.json').read_text(encoding='utf-8-sig'))
    if not profile.get('enabled') or Path(profile['runtime_root']).absolute()!=RUNTIME or profile['base_url']!='http://127.0.0.1:8765':raise RuntimeError('daily_profile_not_activated')
    state=container_state()
    servicefile=RUNTIME/'service.json'
    if action=='start':
        checked_path(RUNTIME/'qdrant').mkdir(parents=True,exist_ok=True)
        if state is None:
            run(['docker','run','-d','--pull=never','--restart=no','--name',CONTAINER,'-p','127.0.0.1:6333:6333','--mount','type=bind,source='+str(RUNTIME/'qdrant')+',target=/qdrant/storage','-e','QDRANT__TELEMETRY_DISABLED=true',IMAGE])
        elif state!='running':run(['docker','start',CONTAINER])
        import httpx
        with httpx.Client(trust_env=False) as client:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                try:
                    if client.get('http://127.0.0.1:6333/healthz',timeout=1).is_success:break
                except httpx.HTTPError:pass
                time.sleep(.25)
            else:raise TimeoutError('qdrant_health_unconfirmed')
        if servicefile.is_file():
            previous=json.loads(servicefile.read_text(encoding='utf-8'))
            if identity(previous['pid'])==previous['identity']:return {'status':'already_running','health':ready_health(),'ui':'http://127.0.0.1:8765/ui'}
        result=json.loads(run([sys.executable,'-X','utf8','-B',str(ROOT/'scripts/runtime_control.py'),'start','--runtime',str(RUNTIME),'--port','8765','--qdrant-port','6333','--prefix','smh_codex_primary','--device','cuda'],65))
        result['health']=ready_health();result['ui']='http://127.0.0.1:8765/ui';return result
    result={'status':'not_started'}
    if servicefile.is_file():result=json.loads(run([sys.executable,'-X','utf8','-B',str(ROOT/'scripts/runtime_control.py'),'stop','--runtime',str(RUNTIME)],65))
    if state=='running':run(['docker','stop','--time','15',CONTAINER],30)
    for port in (8765,6333):
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(('127.0.0.1',port))==0:raise RuntimeError('port_release_unconfirmed')
    return {**result,'status':'stopped','ports_released':True,'data_preserved':True}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop']);a=p.parse_args()
    print(json.dumps(control(a.action),ensure_ascii=False,indent=2))
