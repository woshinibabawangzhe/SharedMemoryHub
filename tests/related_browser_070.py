"""Disposable synthetic HTTP/browser fixture; never opens the personal runtime."""
import os,sys,json,socket,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from hub.paths import checked_path
from hub.db import MemoryDatabase
from hub.auth import AuthManager
from hub.app import create_hub_app
from test_safety_030 import EmbeddingFixture,VectorFixture
import uvicorn

def req(**kw):return {'request_key':uuid.uuid4().hex,**kw}
def main():
    evidence=checked_path(ROOT/'receipts/related-070-20261110-120143')
    if '--resume' in sys.argv:
        manifest=json.loads((evidence/'browser-current.json').read_text(encoding='utf-8'))
        runtime=checked_path(manifest['runtime']);port=manifest['port'];origin=manifest['origin']
        os.environ.update(TEMP=str(runtime/'tmp'),TMP=str(runtime/'tmp'),PYTHONDONTWRITEBYTECODE='1')
        db=MemoryDatabase(runtime/'data/sqlite/memory_hub.db',runtime/'data/tombstones/purge_registry.jsonl',runtime/'exports',runtime/'backups')
        auth=AuthManager(runtime/'config/client_tokens.json',runtime/'config/admin_auth.json',allowed_origin=origin)
        app=create_hub_app(db,auth,EmbeddingFixture(),VectorFixture(),lock_path=runtime/'data/hub.lock')
        print(json.dumps({'status':'resumed','origin':origin}),flush=True)
        uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning');return
    runtime=checked_path(evidence/('browser-'+uuid.uuid4().hex[:8]));runtime.mkdir()
    temp=runtime/'tmp';temp.mkdir();os.environ.update(TEMP=str(temp),TMP=str(temp),PYTHONDONTWRITEBYTECODE='1')
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];origin=f'http://127.0.0.1:{port}'
    db=MemoryDatabase(runtime/'data/sqlite/memory_hub.db',runtime/'data/tombstones/purge_registry.jsonl',runtime/'exports',runtime/'backups')
    auth=AuthManager(runtime/'config/client_tokens.json',runtime/'config/admin_auth.json',initial_passcode='Synthetic-browser-070',allowed_origin=origin)
    memories=[]
    for i,label in enumerate(('合成工具甲','合成工具乙','合成工具丙')):
        memories.append(db.write_memory('user_admin','user','web_ui',req(operation='create',title=label,content=f'Codex 旧入口。保留本条独有资料{i}。<img src=x onerror=alert(1)>',scope='user_global',subject='user',kind='context',domain_id='tools',source={'origin_type':'admin_manual'})))
    pending=db.write_memory('agent_client','codex','codex',req(operation='create',title='合成待核实待批',content='合成需要人工核实的偏好',scope='user_global',subject='user',review_due_at='2020-01-01',source={'origin_type':'user_explicit','session_id':'synthetic-browser-070'}))
    draft=db.admin_draft_create(req(raw_text='合成变化：桌面端为主，保留三条独有资料。',members=[{'memory_id':m['memory_id'],'base_revision':1} for m in memories]))
    grant=db.admin_draft_action(draft['draft_id'],'grant',req(expected_revision=1,clients=['codex','antigravity']))
    changes=[{'memory_id':m['memory_id'],'base_revision':1,'action':'update','fields':{'content':f'Codex 桌面端为主。保留本条独有资料{i}。'},'reason':'合成纠正分工，保留独有信息',**({'related_ids':[memories[0]['memory_id']]} if i else {})} for i,m in enumerate(memories)]
    db.draft_suggest(draft['draft_id'],'codex','codex',req(base_draft_revision=grant['revision'],session_id='synthetic-browser-070',kind='ready',reason='合成逐条整理建议',changes=changes))
    single=db.admin_draft_create(req(raw_text='合成答复偏好',fields={'title':'合成单条草稿','content':'合成旧建议','confidence':'hypothesis'}))
    sg=db.admin_draft_action(single['draft_id'],'grant',req(expected_revision=1,clients=['codex']))
    db.draft_suggest(single['draft_id'],'codex','codex',req(base_draft_revision=sg['revision'],session_id='synthetic-browser-070',kind='ready',fields={'title':'合成单条建议','content':'合成建议正文'},reason='合成建议'))
    manifest={'runtime':str(runtime),'origin':origin,'port':port,'group_draft':draft['draft_id'],'single_draft':single['draft_id'],'memories':[m['memory_id'] for m in memories],'pending':pending['memory_id'],'boundary':'synthetic HTTP and browser; fixture embedding/vector, not real multi-client acceptance'}
    (evidence/'browser-current.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'origin':origin,'runtime':str(runtime)},ensure_ascii=False),flush=True)
    app=create_hub_app(db,auth,EmbeddingFixture(),VectorFixture(),lock_path=runtime/'data/hub.lock')
    uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning')).run(sockets=[sock])
if __name__=='__main__':main()
