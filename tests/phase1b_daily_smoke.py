"""新正式根的只读冒烟与有界启停；不创建合成记忆。"""
import argparse,importlib.util,json,subprocess,sys,time
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from clients.memory_cli import call_hub
from scripts.codex_daily_control import control,container_state

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);a=p.parse_args();runtime=ROOT/'runtime/codex'
    profile=json.loads((runtime/'profile.json').read_text(encoding='utf-8-sig'))
    assert profile['enabled'] and profile['mode']=='daily' and Path(profile['runtime_root'])==runtime
    started=time.monotonic()
    warm=call_hub('POST','/v1/memory/search',client_name='codex',payload={'workspace_path':str(ROOT),'query':'SharedMemoryHub Codex 接入','limit':5},base_url=profile['base_url'],timeout_sec=90,tokens_path=runtime/'config/client_tokens.json')
    assert warm.get('http_status')==200 and warm.get('bound_project_id')=='proj_shared_memory_hub',warm
    warm_seconds=round(time.monotonic()-started,3)
    adapter=Path(os.environ['SMH_CODEX_ADAPTER'])
    result=subprocess.run([sys.executable,'-X','utf8','-B',str(adapter),'--workspace',str(ROOT),'search','--query','Codex 接入'],capture_output=True,text=True,encoding='utf-8',timeout=20)
    normal=json.loads(result.stdout);assert result.returncode==0 and normal.get('bound_project_id')=='proj_shared_memory_hub',normal
    import httpx
    with httpx.Client(trust_env=False,timeout=5) as c:health=c.get(profile['base_url']+'/v1/health').json()
    assert health['embedding_model_loaded'] and health['embedding_device']=='cuda' and health['qdrant']=='ok' and health['vector_backend']=='docker_qdrant_rest',health
    # 新正式实例保持空库，合成记录只留隔离实例。
    assert health['active_memories']==0
    stopped=control('stop');assert stopped['ports_released'] and stopped['data_preserved']
    restarted=control('start');assert restarted['status']=='started' and restarted['health']['embedding_model_loaded']
    assert control('start')['status']=='already_running' and container_state()=='running'
    receipt={'evidence_level':'controlled-trial','formal_root':str(runtime),'user_initialized_locally':True,'credential_contents_inspected':False,'profile':profile,'cold_warmup_seconds':warm_seconds,'warmup_search':warm,'normal_adapter_search':normal,'health_after_real_inference':health,'manual_controller_stop':stopped,'restart':restarted,'idempotent_start':'already_running','daily_left_running':True,'synthetic_memories_in_formal_root':False,'old_data_migrated':False}
    (a.evidence/'daily-smoke.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'formal_smoke':True,'cuda_inference':True,'ports_released_on_stop':True,'daily_service_restarted':True,'active_memories':0,'warmup_seconds':warm_seconds},ensure_ascii=False))
