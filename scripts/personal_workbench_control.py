"""Manual personal archive service. Does not enable any Harness routing."""
import argparse,json,subprocess,sys,webbrowser
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from scripts.runtime_control import start,stop,port_free,identity,IMAGE

RUNTIME=checked_path(ROOT/'runtime/personal-0.4.0')
AUTH=checked_path(ROOT/'runtime/codex/config')
NAME='smh-personal-workbench-040'
PREFIX='smh_personal_040'

def container_info():
    r=subprocess.run(['docker','inspect','--type','container',NAME],capture_output=True,text=True,encoding='utf-8')
    if r.returncode:return None
    d=json.loads(r.stdout)[0]
    expected=checked_path(RUNTIME/'data/qdrant')
    mounts=[m for m in d['Mounts'] if m['Destination']=='/qdrant/storage']
    ports=d['HostConfig']['PortBindings'].get('6333/tcp',[])
    if len(mounts)!=1 or Path(mounts[0]['Source']).resolve()!=expected.resolve() or ports!=[{'HostIp':'127.0.0.1','HostPort':'6333'}] or d['Config']['Image']!=IMAGE:
        raise RuntimeError('container_identity_mismatch; no changes made')
    return d

def control(action,open_page=False):
    args=argparse.Namespace(runtime=str(RUNTIME),port=8765,qdrant_port=6333,prefix=PREFIX,device='cuda',python=None,model_dir=None,auth_dir=str(AUTH))
    if action=='start':
        if not (RUNTIME/'data/sqlite/memory_hub.db').is_file():raise RuntimeError('personal_archive_not_prepared')
        if not (AUTH/'admin_auth.json').is_file() or not (AUTH/'client_tokens.json').is_file():raise RuntimeError('existing_authentication_missing; will not reset')
        info=container_info()
        pidfile=RUNTIME/'service.json'
        if pidfile.is_file():
            old=json.loads(pidfile.read_text(encoding='utf-8'))
            if identity(old['pid'])==old['identity']:
                if open_page:webbrowser.open('http://127.0.0.1:8765/ui')
                return {'status':'already_running','url':'http://127.0.0.1:8765/ui'}
        if not port_free(8765):raise RuntimeError('port_8765_in_use; no process killed')
        if not info:
            if not port_free(6333):raise RuntimeError('port_6333_in_use; no container stopped')
            subprocess.run(['docker','image','inspect',IMAGE],check=True,capture_output=True)
            storage=checked_path(RUNTIME/'data/qdrant');storage.mkdir(parents=True,exist_ok=True)
            subprocess.run(['docker','create','--name',NAME,'--restart=no','-p','127.0.0.1:6333:6333','-e','QDRANT__TELEMETRY_DISABLED=true','--mount',f'type=bind,source={storage},target=/qdrant/storage',IMAGE],check=True,capture_output=True)
            info=container_info()
        if not info['State']['Running']:
            if not port_free(6333):raise RuntimeError('port_6333_in_use')
            subprocess.run(['docker','start',NAME],check=True,capture_output=True)
        result=start(args)
        if open_page:webbrowser.open('http://127.0.0.1:8765/ui')
        return result
    result=stop(args);info=container_info()
    if info and info['State']['Running']:subprocess.run(['docker','stop',NAME],check=True,capture_output=True)
    result['qdrant_port_released']=port_free(6333)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop']);p.add_argument('--open',action='store_true');a=p.parse_args()
    print(json.dumps(control(a.action,a.open),ensure_ascii=False))
