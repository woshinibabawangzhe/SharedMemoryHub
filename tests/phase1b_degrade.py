"""只停止本轮独立容器，核对降级和网关手动停止。"""
import argparse,contextlib,json,socket,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tests'))
from phase1b_live_control import admin_client,readmeta,stop

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--evidence',required=True,type=Path);a=p.parse_args();m=readmeta(a.evidence)
    assert m['container'].startswith('smh1b-') and m['prefix'].startswith('smh1b_') and m['qdrant_port']==16379
    with contextlib.closing(admin_client(m)) as c:
        before=c.get('/v1/health').json()
        full=c.get('/v1/admin/memory/'+m['seed_memory_id']+'/detail').json()
        assert full['item']['revision']==2 and full['item']['locked'] and full['item']['indexed_revision']==2
        subprocess.run(['docker','stop','--time','15',m['container']],check=True,capture_output=True,timeout=30)
        started=time.monotonic();r=c.post('/v1/memory/search',json={'query':'报告 校验词','workspace_path':m['workspace'],'limit':5});r.raise_for_status();result=r.json()
        assert result['degraded_mode']=='keyword_only' and m['seed_memory_id'] in [x['memory_id'] for x in result['items']]
        record={'evidence_level':'controlled-trial','before_health':before,'edited_item':full['item'],'qdrant_stopped_only':m['container'],'keyword_search':result,'command_seconds':round(time.monotonic()-started,3)}
    record['gateway_stop']=stop(a.evidence)
    for port in (18789,16379):
        with socket.socket() as s:s.settimeout(1);assert s.connect_ex(('127.0.0.1',port))!=0
    record['ports_released']=[18789,16379]
    (a.evidence/'degrade-stop.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'keyword_only':True,'seed_latest_synced':True,'gateway_stopped':True,'ports_released':True},ensure_ascii=False))
