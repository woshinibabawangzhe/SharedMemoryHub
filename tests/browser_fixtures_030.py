"""只为 current-live.json 指定的合成实例准备提案并核验浏览器操作的持久结果。"""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx
from hub.paths import checked_path


def run(action):
    evidence=ROOT/'receipts/v0.3.0-implementation'
    target=json.loads((evidence/'current-live.json').read_text())
    runtime=checked_path(target['runtime'])
    if runtime.parent!=evidence or not runtime.name.startswith('live-') or target['port']==8765:
        raise RuntimeError('not_an_isolated_synthetic_target')
    origin=f"http://127.0.0.1:{target['port']}"
    manifest=runtime/'browser-fixtures.json'
    with httpx.Client(base_url=origin,trust_env=False,timeout=10) as c:
        if action=='prepare':
            if manifest.exists():raise RuntimeError('fixtures_already_prepared; inspect before retrying')
            token=json.loads((runtime/'config/client_tokens.json').read_text())['CODEX_CLIENT_TOKEN']
            candidates={}
            for outcome,label in [('accept','采纳'),('reject','拒绝'),('merge','合并')]:
                response=c.post('/v1/memory/write',headers={'Authorization':'Bearer '+token},json={
                    'request_key':'browser-global-030-'+outcome,'operation':'create','scope':'user_global','subject':'user',
                    'kind':'preference','title':'浏览器提案-'+label,'content':'合成的全局偏好候选-'+label,
                    'source':{'origin_type':'user_explicit','session_id':'browser-synthetic-030','evidence_ref':'合成用户明确原话'}})
                response.raise_for_status();item=response.json()
                assert item['current_state']=='paused' and item['proposal_id']
                candidates[outcome]=item
            manifest.write_text(json.dumps({'evidence_level':'controlled-trial','candidates':candidates},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'created':3,'all_paused_with_proposals':True}))
            return
        login=c.post('/v1/admin/login',headers={'Origin':origin},json={'passcode':'Synthetic-fixture-030'})
        login.raise_for_status()
        all_items=c.get('/v1/admin/memories').json()['items']
        main=next(m for m in all_items if m['title']=='浏览器合成卡片')
        detail=c.get('/v1/admin/memory/'+main['memory_id']+'/detail').json()
        assert main['revision']==7 and main['locked'] and main['state']=='active'
        assert main['topic_key']=='browser-test' and main['scope']=='project'
        assert main['content']=='<img src=x onerror="alert(1)"> 合成记忆原文'
        revisions=sorted(detail['revisions'],key=lambda x:x['revision'])
        assert [r['operation'] for r in revisions]==['create','update','unlock','lock','soft_delete','undo','restore']
        assert revisions[1]['content']=='合成修订正文：保留适用范围与完整版本。'
        assert revisions[4]['state']=='deleted' and revisions[5]['state']=='active'
        checks={'create_edit_metadata_lock_delete_undo_restore':True,'main_memory':main['memory_id'],'revision':7}
        candidates=json.loads(manifest.read_text(encoding='utf-8'))['candidates']
        for outcome,created in candidates.items():
            d=c.get('/v1/admin/memory/'+created['memory_id']+'/detail').json();m=d['item']
            proposal=next(p for p in d['proposals'] if p['proposal_id']==created['proposal_id'])
            assert proposal['status']!='pending' and m['revision']==2
            if outcome=='reject':assert m['state']=='paused'
            else:assert m['state']=='active' and m['locked'] and m['confidence']=='confirmed'
            if outcome=='merge':assert m['content']=='合成手动合并后的最终正文。'
            checks[outcome]={'state':m['state'],'proposal_status':proposal['status'],'revision':m['revision']}
        (runtime/'browser-backend-verification.json').write_text(json.dumps({'status':'已验证','evidence_level':'controlled-trial',
            'scope':'持久结果核验，浏览器交互证据另见 snapshot/screenshot/console','checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(checks,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','verify'])
    run(parser.parse_args().action)
