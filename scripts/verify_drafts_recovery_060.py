"""Verify only the prepared synthetic instance, its vectors, restart and offline CLI."""
import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from hub.paths import checked_path
from hub.embedder import QdrantManager
from clients.memory_cli import call_hub
from scripts.runtime_control import start,stop,port_free


def verify(evidence):
    evidence=checked_path(evidence)
    spec=json.loads((evidence/'live-spec.json').read_text(encoding='utf-8'))
    runtime=checked_path(spec['runtime'])
    if spec['data_kind']!='synthetic-only' or runtime!=evidence/'live-synthetic' or not spec['prefix'].startswith('smh_drafts_060_'):
        raise ValueError('synthetic_instance_required')
    db=runtime/'data/sqlite/memory_hub.db'
    def snapshot():
        with closing(sqlite3.connect(f'{db.as_uri()}?mode=ro',uri=True)) as con:
            con.row_factory=sqlite3.Row
            rows=[dict(r) for r in con.execute('SELECT * FROM memory_drafts ORDER BY draft_id')]
            suggestions=[dict(r) for r in con.execute('SELECT * FROM draft_suggestions ORDER BY suggestion_id')]
            digest=hashlib.sha256(json.dumps([rows,suggestions],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            accepted=[dict(r) for r in con.execute("SELECT d.draft_id,d.revision AS draft_revision,d.result_memory_id,m.revision,m.indexed_revision,m.confidence,m.state,m.source_json,s.client_name,s.session_id,s.suggestion_id FROM memory_drafts d JOIN current_memory m ON m.memory_id=d.result_memory_id JOIN draft_suggestions s ON s.draft_id=d.draft_id AND s.status='accepted' WHERE d.status='applied'")]
            request_rows=[dict(r) for r in con.execute("SELECT request_key,result_json FROM draft_requests WHERE actor_id='codex' AND is_purged=0")]
            draft_ids=[r['draft_id'] for r in rows]
        return digest,accepted,request_rows,draft_ids
    before,accepted,requests,draft_ids=snapshot()
    assert len(accepted)>=2 and requests,'native Codex flow not present'
    with httpx.Client(trust_env=False) as client:
        health=client.get(spec['url']+'/v1/health').raise_for_status().json()
    assert health['qdrant']=='ok' and health['embedding_device']=='cuda'
    assert health['pending_index_work']==health['processing_index_work']==health['failed_index_work']==0
    vectors=QdrantManager(collection_prefix=spec['prefix'])
    try:points=vectors.scroll_all_payloads(health['active_index_generation'])
    finally:vectors._client.close()
    by_id={r['memory_id']:r for r in points}
    for row in accepted:
        assert row['revision']==row['indexed_revision']
        assert by_id[row['result_memory_id']]['revision']==row['revision']
        source=json.loads(row.pop('source_json'))
        assert source['session_id']==row['session_id'] and source['collaborator']==row['client_name']=='codex'
    assert not set(draft_ids)&set(by_id),'draft leaked into vector projection'
    stopped=stop(argparse.Namespace(runtime=str(runtime)))
    then=time.monotonic()
    proc=subprocess.run([sys.executable,'-X','utf8','-B',str(ROOT/'clients/draft_cli.py'),'--client','codex',
        '--runtime',str(runtime),'--base-url',spec['url'],'get','--id',accepted[0]['draft_id']],
        cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=15)
    offline={'exit_code':proc.returncode,'elapsed_seconds':round(time.monotonic()-then,3),'response':json.loads(proc.stdout),
             'port_remained_free':port_free(spec['port'])}
    assert proc.returncode!=0 and offline['port_remained_free']
    assert offline['response']['status']=='MEMORY_HUB_UNAVAILABLE'
    restarted=start(argparse.Namespace(runtime=str(runtime),port=spec['port'],qdrant_port=6333,prefix=spec['prefix'],
        device='cuda',python=None,model_dir=None,auth_dir=str(runtime/'config')))
    after,_,_,_=snapshot();assert before==after,'draft data changed across restart'
    for request in requests:
        response=call_hub('GET','/v1/draft-requests/'+request['request_key'],'codex',
            base_url=spec['url'],tokens_path=runtime/'config/client_tokens.json')
        expected=json.loads(request['result_json'])
        assert all(response.get(k)==v for k,v in expected.items()),'durable result mismatch'
    final_stop=stop(argparse.Namespace(runtime=str(runtime)))
    result={'status':'verified','data_kind':'synthetic-only','health':health,'collection_prefix':spec['prefix'],
        'accepted':accepted,'vector_payloads':points,'no_draft_vectors':True,'restart':restarted,
        'draft_snapshot_sha256':before,'restart_preserved_drafts':True,'persisted_request_results':len(requests),
        'offline':offline,'initial_stop':stopped,'final_stop':final_stop,
        'native_antigravity_session':'not_verified'}
    (evidence/'live-recovery-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',required=True,type=Path)
    r=verify(parser.parse_args().evidence)
    print(json.dumps({'status':r['status'],'accepted_count':len(r['accepted']),
        'restart_preserved_drafts':r['restart_preserved_drafts'],'offline':r['offline'],'final_stop':r['final_stop']},ensure_ascii=False))
