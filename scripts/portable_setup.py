"""换机部署 v0.1.0；check不下载，install显式安装锁定依赖，prepare只建非敏感配置。"""
import argparse,hashlib,importlib.metadata,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).absolute().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path

def process_env():
    env=os.environ.copy()
    for name,suffix in {'TEMP':'tmp','TMP':'tmp','PIP_CACHE_DIR':'pip','HF_HOME':'hf','TORCH_HOME':'torch','XDG_CACHE_HOME':'xdg'}.items():
        path=checked_path(ROOT/'cache'/suffix);path.mkdir(parents=True,exist_ok=True);env[name]=str(path)
    env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    return env

def check():
    lock=(ROOT/'requirements.lock').read_text(encoding='utf-8');missing=[]
    for line in lock.splitlines():
        if not line or line.startswith('#'):continue
        name,version=line.split()[0].split('==')
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:actual=None
        if actual!=version:missing.append({'package':name,'expected':version,'actual':actual})
    return {'python':sys.version.split()[0],'python_313':sys.version_info[:2]==(3,13),'dependency_mismatches':missing,'project_root':str(ROOT),'status':'verified' if not missing and sys.version_info[:2]==(3,13) else 'setup_required'}

def verify_model():
    lock=json.loads((ROOT/'model.lock.json').read_text(encoding='utf-8'))
    for relative,record in lock['observed_local_files'].items():
        path=checked_path(ROOT/'models/Qwen3-Embedding-0.6B'/relative)
        with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=record['sha256']:raise RuntimeError('pinned_model_mismatch: '+relative)
    return len(lock['observed_local_files'])

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['check','install','prepare','activate-restored']);p.add_argument('--download-model',action='store_true');p.add_argument('--restored-runtime');a=p.parse_args()
    checked_path(ROOT)
    if a.action=='check':result=check()
    elif a.action=='install':
        if os.name!='nt' or sys.version_info[:2]!=(3,13):raise SystemExit('锁文件仅支持Windows x64/Python3.13；不自动更换Python或全局设置。')
        env=process_env();venv=checked_path(ROOT/'.venv-0.3.0')
        if not venv.exists():subprocess.run([sys.executable,'-B','-m','venv',str(venv)],env=env,check=True)
        if 'include-system-site-packages = false' not in (venv/'pyvenv.cfg').read_text():raise SystemExit('environment_is_not_isolated')
        python=venv/'Scripts/python.exe'
        subprocess.run([str(python),'-B','-m','pip','install','--disable-pip-version-check','--require-hashes','--only-binary=:all:','--index-url','https://pypi.org/simple','--extra-index-url','https://download.pytorch.org/whl/cu128','-r',str(ROOT/'requirements.lock')],env=env,check=True)
        subprocess.run([str(python),'-B','-m','pip','check'],env=env,check=True)
        if a.download_model:
            subprocess.run([str(python),'-B',str(ROOT/'scripts/download_model.py')],env=env,check=True)
            verify_model()
        result={'status':'installed','environment':str(venv),'model_download_requested':a.download_model}
    elif a.action=='activate-restored':
        from hub.db import MemoryDatabase
        from hub.worker import SingleInstanceFileLock
        runtime=checked_path(a.restored_runtime or ROOT/'runtime/codex')
        if not (runtime/'restore-receipt.json').is_file():raise SystemExit('verified_restore_receipt_required')
        if not all((runtime/'config'/name).is_file() for name in ('admin_auth.json','client_tokens.json')):raise SystemExit('user_authentication_setup_required')
        with SingleInstanceFileLock(runtime/'data/hub.lock'):
            profile_path=runtime/'profile.json';profile=json.loads(profile_path.read_text(encoding='utf-8'))
            if Path(profile['runtime_root']).absolute()!=runtime or profile.get('enabled'):raise SystemExit('profile_mismatch_or_already_enabled')
            db=MemoryDatabase(runtime/'data/sqlite/memory_hub.db',runtime/'data/tombstones/purge_registry.jsonl',runtime/'exports',runtime/'backups')
            db.verify_and_reconcile_purge_registry()
            # 仅当前服务项目的明确恢复映射；其他项目保持原记录，待用户核对。
            registry=db.get_meta('project_registry',{})
            db.set_meta('project_registry',{k:v for k,v in registry.items() if v!='proj_shared_memory_hub'})
            db.register_project_binding('proj_shared_memory_hub',ROOT)
            profile.update(enabled=True,mode='daily',setup_status='restored_initialized')
            profile_path.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        result={'status':'activated','runtime':str(runtime),'credentials_inspected':False,'service_started':False,'other_project_bindings_require_review':True}
    else:
        runtime=checked_path(a.restored_runtime or ROOT/'runtime/codex');profile=runtime/'profile.json'
        if profile.exists():raise SystemExit('existing_profile_preserved')
        runtime.mkdir(parents=True,exist_ok=True)
        result={'version':'0.1.0','enabled':False,'mode':'setup','runtime_root':str(runtime),'base_url':'http://127.0.0.1:8765','registered_workspaces':[str(ROOT)],'setup_status':'awaiting_user_setup'}
        with profile.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
        result={'status':'prepared','profile':str(profile),'credentials_created':False}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
