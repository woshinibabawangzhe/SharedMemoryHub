"""有界启停、进程身份与端口核验。无全局设置修改，无按端口强杀。"""
import argparse,ctypes,json,os,socket,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path, checked_asset
IMAGE='qdrant/qdrant@sha256:318c11b72aaab96b36e9662ad244de3cabd0653a1b942d4e8191f18296c81af0'


def identity(pid):
    # Windows kernel image path + creation FILETIME protect against PID reuse.
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:return None
    try:
        path=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(path))
        created=wintypes.FILETIME();exited=wintypes.FILETIME();kt=wintypes.FILETIME();ut=wintypes.FILETIME()
        if not kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)):return None
        if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kt),ctypes.byref(ut)):return None
        return {'exe':path.value.casefold(),'created':(created.dwHighDateTime<<32)+created.dwLowDateTime}
    finally:kernel.CloseHandle(handle)


def port_free(port):
    with socket.socket() as s:
        try:s.connect(('127.0.0.1',port));return False
        except OSError:return True


def start(a):
    runtime=checked_path(a.runtime);runtime.mkdir(parents=True,exist_ok=True)
    if not port_free(a.port):raise RuntimeError('port_already_in_use; no process was killed')
    pidfile=runtime/'service.json'
    if pidfile.exists():
        old=json.loads(pidfile.read_text())
        if identity(old['pid'])==old['identity']:raise RuntimeError('instance_already_running')
    if (runtime/'stop.request').exists():(runtime/'stop.request').unlink() # own control marker
    log=runtime/'logs';log.mkdir(exist_ok=True)
    python=checked_asset(getattr(a,'python',None) or ROOT/'.venv-0.3.0/Scripts/python.exe')
    cmd=[str(python),'-X','utf8','-B',str(ROOT/'scripts/serve_hub.py'),'--runtime',str(runtime),'--port',str(a.port),
        '--qdrant-port',str(a.qdrant_port),'--prefix',a.prefix,'--device',a.device]
    if getattr(a,'model_dir',None):cmd+=['--model-dir',str(checked_asset(a.model_dir))]
    with (log/'service.stdout.log').open('ab') as out,(log/'service.stderr.log').open('ab') as err:
        proc=subprocess.Popen(cmd,cwd=ROOT,stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW)
    info={'pid':proc.pid,'identity':identity(proc.pid),'port':a.port,'runtime':str(runtime),'prefix':a.prefix,'image':IMAGE}
    if info['identity'] is None:raise RuntimeError('process_identity_unknown')
    pidfile.write_text(json.dumps(info,indent=2),encoding='utf-8')
    import httpx
    deadline=time.monotonic()+90
    with httpx.Client(trust_env=False) as client:
        while time.monotonic()<deadline:
            if proc.poll() is not None:raise RuntimeError('service_start_failed; inspect runtime/logs/service.stderr.log')
            try:
                response=client.get(f'http://127.0.0.1:{a.port}/v1/health',timeout=min(4,deadline-time.monotonic()))
                response.raise_for_status();health=response.json()
                return {'status':'started','pid':proc.pid,'port':a.port,'health':health}
            except Exception:time.sleep(.25)
    # Unknown partial startup is preserved for the matching explicit stop command.
    raise TimeoutError('startup_health_timeout; instance recorded in service.json')


def stop(a):
    runtime=checked_path(a.runtime);info=json.loads((runtime/'service.json').read_text(encoding='utf-8'))
    actual=identity(info['pid'])
    if actual is not None and actual!=info['identity']:raise RuntimeError('process_identity_mismatch; refusing stop')
    if actual is not None:
        (runtime/'stop.request').write_text('stop',encoding='utf-8')
        for _ in range(100):
            if identity(info['pid']) is None:break
            time.sleep(.5)
        else:raise TimeoutError('graceful_exit_unconfirmed; no force kill performed')
    if not port_free(info['port']):raise RuntimeError('port_not_released')
    return {'status':'stopped','port_released':True,'identity_verified':True}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop']);p.add_argument('--runtime',default=str(ROOT));p.add_argument('--port',type=int,default=8765)
    p.add_argument('--qdrant-port',type=int,default=6333);p.add_argument('--prefix',default='memories');p.add_argument('--device',choices=['cuda','cpu'],default='cuda')
    p.add_argument('--python',help='明确的E盘解释器；不复制既有虚拟环境');p.add_argument('--model-dir',help='明确的E盘模型只读位置')
    a=p.parse_args();print(json.dumps(start(a) if a.action=='start' else stop(a),ensure_ascii=False))
