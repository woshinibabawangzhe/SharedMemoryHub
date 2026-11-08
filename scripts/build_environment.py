"""建立独立 Windows/Python3.13 CUDA 环境，下载 wheel 后以实际 SHA256 锁定安装。"""
import hashlib,json,os,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hub.paths import checked_path


def build():
    checked_path(ROOT)
    env=os.environ.copy()
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    for key,suffix in {'TEMP':'tmp','TMP':'tmp','PIP_CACHE_DIR':'pip','HF_HOME':'hf','TORCH_HOME':'torch','XDG_CACHE_HOME':'xdg'}.items():
        path=checked_path(ROOT/'cache'/suffix);path.mkdir(parents=True,exist_ok=True);env[key]=str(path)
    venv=checked_path(ROOT/'.venv-0.3.0')
    if not venv.exists():subprocess.run([sys.executable,'-B','-m','venv',str(venv)],env=env,check=True)
    cfg=(venv/'pyvenv.cfg').read_text()
    if 'include-system-site-packages = false' not in cfg:raise RuntimeError('environment_is_not_isolated')
    python=venv/'Scripts/python.exe'
    wheelhouse=checked_path(ROOT/'cache/wheels-0.3.0');wheelhouse.mkdir(exist_ok=True)
    torch='https://download-r2.pytorch.org/whl/cu128/torch-2.11.0%2Bcu128-cp313-cp313-win_amd64.whl#sha256=6f367e62fd81b75cdf23ca4b75ced834d2db2cf98d1588ac935bde345de9de23'
    local_torch=wheelhouse/'torch-2.11.0+cu128-cp313-cp313-win_amd64.whl'
    deps=['fastapi==0.136.3','uvicorn==0.44.0','httpx==0.28.1','numpy==2.4.3','transformers==5.22.2',str(local_torch) if local_torch.is_file() else torch]
    resolver_source=['--no-index'] if '--offline' in sys.argv else ['--index-url','https://pypi.org/simple']
    subprocess.run([str(python),'-B','-m','pip','download','--disable-pip-version-check','--no-cache-dir','--only-binary=:all:',
        *resolver_source,'--find-links',str(wheelhouse),'--dest',str(wheelhouse),*deps],check=True,env=env)
    locks=[];artifacts=[]
    for wheel in sorted(wheelhouse.glob('*.whl')):
        with wheel.open('rb') as source:
            digest=hashlib.file_digest(source,'sha256').hexdigest()
        with zipfile.ZipFile(wheel) as archive:
            metadata=archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))).decode()
        name=next(l[6:] for l in metadata.splitlines() if l.startswith('Name: '))
        version=next(l[9:] for l in metadata.splitlines() if l.startswith('Version: '))
        locks.append(f'{name}=={version} --hash=sha256:{digest}')
        artifacts.append({'wheel':wheel.name,'size':wheel.stat().st_size,'sha256':digest})
    (ROOT/'requirements.lock').write_text('# v0.3.0 Windows cp313; install from cache/wheels-0.3.0 with --require-hashes\n'+'\n'.join(locks)+'\n',encoding='utf-8')
    subprocess.run([str(python),'-B','-m','pip','install','--disable-pip-version-check','--no-index','--find-links',str(wheelhouse),
        '--require-hashes','-r',str(ROOT/'requirements.lock')],check=True,env=env)
    result=subprocess.run([str(python),'-B','-m','pip','check'],env=env,capture_output=True,text=True)
    (ROOT/'receipts/v0.3.0-implementation/environment.json').write_text(json.dumps({'artifacts':artifacts,'pip_check':result.stdout,'pip_check_exit':result.returncode,'isolated':True},indent=2),encoding='utf-8')
    result.check_returncode()

if __name__=='__main__':build()
