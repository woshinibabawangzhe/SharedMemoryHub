"""同机新源码目录、真实Docker/CUDA恢复；复用已锁定环境和模型，不能称真正换机。"""
import argparse,hashlib,json,secrets,socket,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).absolute().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path,checked_asset
from hub.auth import AuthManager
from scripts.portable_data import restore
from clients.memory_cli import call_hub
import httpx

IMAGE='qdrant/qdrant@sha256:318c11b72aaab96b36e9662ad244de3cabd0653a1b942d4e8191f18296c81af0'
def run(args,timeout=70):
    result=subprocess.run([str(x) for x in args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    if result.returncode:raise RuntimeError('controlled_command_failed: '+str(args[:2]))
    return result.stdout

def port_free(port):
    with socket.socket() as sock:
        sock.settimeout(1);return sock.connect_ex(('127.0.0.1',port))!=0

def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',required=True);p.add_argument('--minimum-purge-seq',type=int,required=True)
    p.add_argument('--queries',required=True);p.add_argument('--model-dir',required=True);p.add_argument('--port',type=int,default=8777);p.add_argument('--qdrant-port',type=int,default=6337)
    a=p.parse_args();bundle=checked_path(a.bundle);queries=checked_path(a.queries)
    if not port_free(a.port) or not port_free(a.qdrant_port):raise RuntimeError('isolated_port_already_in_use')
    tag=uuid.uuid4().hex[:12];runtime=checked_path(ROOT/'runtime'/('restore-test-'+tag));name='smh-restore-test-'+tag;prefix='smh_restore_'+tag
    result={'scope':'same-machine fresh source checkout; actual Docker and CUDA; reused locked interpreter/model','evidence_level':'controlled-trial','port':a.port,'qdrant_port':a.qdrant_port,'collection_prefix':prefix,'container':name,'main_instance_touched':False}
    started=False;container_created=False
    try:
        result['restore']=restore(bundle,runtime,minimum_purge_seq=a.minimum_purge_seq)
        # 专用测试认证在程序内部产生，不读取旧凭据，不打印或复制认证。
        AuthManager(runtime/'config/client_tokens.json',runtime/'config/admin_auth.json',initial_passcode=secrets.token_urlsafe(40),allowed_origin=f'http://127.0.0.1:{a.port}')
        checked_path(runtime/'qdrant').mkdir()
        run(['docker','run','-d','--pull=never','--restart=no','--name',name,'--label','smh.controlled-restore=true','-p',f'127.0.0.1:{a.qdrant_port}:6333','--mount',f'type=bind,source={runtime / "qdrant"},target=/qdrant/storage','-e','QDRANT__TELEMETRY_DISABLED=true',IMAGE]);container_created=True
        deadline=time.monotonic()+30
        with httpx.Client(trust_env=False) as client:
            while time.monotonic()<deadline:
                try:
                    if client.get(f'http://127.0.0.1:{a.qdrant_port}/healthz',timeout=1).is_success:break
                except httpx.HTTPError:pass
                time.sleep(.25)
            else:raise RuntimeError('isolated_qdrant_not_ready')
        control=ROOT/'scripts/runtime_control.py'
        run([sys.executable,'-X','utf8','-B',control,'start','--runtime',runtime,'--port',a.port,'--qdrant-port',a.qdrant_port,'--prefix',prefix,'--device','cuda','--python',sys.executable,'--model-dir',checked_asset(a.model_dir)],timeout=110);started=True
        deadline=time.monotonic()+120
        with httpx.Client(trust_env=False) as client:
            while time.monotonic()<deadline:
                health=client.get(f'http://127.0.0.1:{a.port}/v1/health',timeout=5).json()
                if health['pending_index_work']==0 and health['processing_index_work']==0 and health['failed_index_work']==0 and health['embedding_model_loaded']:break
                time.sleep(.5)
            else:raise RuntimeError('restored_worker_rebuild_not_confirmed')
        assert health['vector_backend']=='docker_qdrant_rest' and health['embedding_device']=='cuda' and health['qdrant']=='ok'
        result['health']=health;rows=[]
        for item in json.loads(queries.read_text(encoding='utf-8')):
            options={'client_name':'codex','base_url':f'http://127.0.0.1:{a.port}','tokens_path':runtime/'config/client_tokens.json','timeout_sec':20}
            got=call_hub('GET','/v1/memory/'+item['memory_id'],**options)
            if item.get('purged'):
                assert got['http_status']==410 and got['error_code']=='MemoryPurged'
                rows.append({'memory_id':item['memory_id'],'status':'expected_purged'});continue
            row=got['item'];assert got['http_status']==200 and row['revision']==item['revision']
            assert hashlib.sha256(row['content'].encode()).hexdigest()==item['content_sha256']
            found=call_hub('POST','/v1/memory/search',payload={'query':item['query'],'limit':5},**options)
            ids=[x['memory_id'] for x in found['items']]
            assert found['http_status']==200 and found.get('degraded_mode') is None and item['memory_id'] in ids
            rows.append({'memory_id':item['memory_id'],'revision':row['revision'],'rank':ids.index(item['memory_id'])+1,'status':'passed'})
        result.update(status='已验证',rows=rows)
    finally:
        cleanup_errors=[]
        if started or (runtime/'service.json').is_file():
            try:result['stop']=json.loads(run([sys.executable,'-X','utf8','-B',ROOT/'scripts/runtime_control.py','stop','--runtime',runtime]))
            except Exception as error:cleanup_errors.append({'operation':'service_stop','error_type':type(error).__name__})
        if container_created:
            try:run(['docker','stop','--time','15',name],timeout=30)
            except Exception as error:cleanup_errors.append({'operation':'container_stop','error_type':type(error).__name__})
        result['cleanup_errors']=cleanup_errors
        result['ports_released']=port_free(a.port) and port_free(a.qdrant_port)
        result['credentials_exposed']=False;result['actual_other_computer_validated']=False
        checked_path(runtime).mkdir(parents=True,exist_ok=True)
        (runtime/'live-restore-receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert result.get('status')=='已验证' and result['ports_released'] and not result['cleanup_errors']
    print(json.dumps({'status':result['status'],'runtime':str(runtime),'receipt':str(runtime/'live-restore-receipt.json'),'checked_records':len(result['rows']),'ports_released':True},ensure_ascii=False))

if __name__=='__main__':main()
