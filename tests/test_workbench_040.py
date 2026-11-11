"""Live workbench contracts on disposable SQLite; vector fixture is not CUDA evidence."""
import json
import sqlite3
import sys
import threading
import unittest
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hub.db import MemoryDatabase, HubError, SCHEMA_VERSION
from scripts.migrate_schema import migrate


class Workbench(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'receipts/workbench-040-20261109/synthetic'/uuid.uuid4().hex
        self.db=MemoryDatabase(self.root/'data/sqlite/memory_hub.db',self.root/'data/tombstones/purge_registry.jsonl',self.root/'exports',self.root/'backups')
        self.ws=self.root/'workspace';self.ws.mkdir()
        self.db.register_project_binding('fixture',self.ws)

    def payload(self,**changes):
        p={'operation':'create','request_key':uuid.uuid4().hex,'scope':'project','subject':'user','workspace_path':str(self.ws),'title':'中文检索路径','content':'中文短词：代理。路径 E:/合成资料/正文.md。','domain_id':'tools','tags':['合成'],
           'source':{'origin_type':'user_explicit','session_id':'fixture-040','evidence_ref':'合成用户明确陈述，仅用于验收。'}}
        p.update(changes);return p

    def write(self,p):return self.db.write_memory('agent_client','codex','codex',p)

    def test_concurrent_duplicate_and_replay(self):
        out=[]
        def put():out.append(self.write(self.payload()))
        ts=[threading.Thread(target=put) for _ in range(2)]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertEqual(len(out),2);self.assertEqual(out[0]['memory_id'],out[1]['memory_id'])
        self.assertEqual(self.db.workbench_list()['total'],1)
        alias=self.payload();alias['idempotency_key']=alias.pop('request_key')
        self.assertTrue(self.write(alias)['duplicate'])
        p=self.payload();r=self.write(p);replay=self.write(p)
        self.assertEqual((r['status'],replay['status']),('duplicate','duplicate'))
        p['content']='不同请求正文'
        with self.assertRaises(HubError) as e:self.write(p)
        self.assertEqual(e.exception.error_code,'IdempotencyKeyPayloadMismatch')

    def test_pending_reuse_not_approval(self):
        p=self.payload(scope='user_global');a=self.write(p);b=self.write(self.payload(scope='user_global'))
        self.assertEqual(a['memory_id'],b['memory_id']);self.assertEqual(b['status'],'paused_pending_approval')
        self.assertEqual(self.write(p)['status'],'paused_pending_approval')
        self.assertEqual(self.db.workbench_list({'pending':True})['total'],1)
        with self.assertRaises(HubError):self.db.admin_set_state(a['memory_id'],'active',1)

    def test_project_mismatch_before_duplicate(self):
        self.write(self.payload())
        with self.assertRaises(HubError) as e:self.write(self.payload(project_id='other'))
        self.assertEqual(e.exception.error_code,'ProjectScopeMismatch')

    def test_cursor_literals_filters(self):
        for i in range(28):self.write(self.payload(title=f'代理合成记录{i}'))
        a=self.db.workbench_list({'query':'代理','domain_id':'tools'},limit=25)
        self.assertEqual((a['total'],len(a['items'])),(28,25))
        b=self.db.workbench_list({'query':'代理','domain_id':'tools'},cursor=a['next_cursor'],limit=25)
        self.assertEqual(len(b['items']),3)
        self.assertFalse({x['memory_id'] for x in a['items']}&{x['memory_id'] for x in b['items']})
        with self.assertRaises(HubError):self.db.workbench_list({'query':'其他'},cursor=a['next_cursor'],limit=25)
        self.assertEqual(self.db.workbench_list({'query':'E:/合成资料/正文.md'})['total'],28)
        self.assertEqual(self.db.workbench_list({'query':'无答案短词'})['total'],0)

    def test_full_metadata_undo_restore(self):
        r=self.write(self.payload());mid=r['memory_id']
        self.db.write_memory('user_admin','user','web_ui',{'request_key':uuid.uuid4().hex,'operation':'update','memory_id':mid,'expected_revision':1,'domain_id':'learning','tags':['新标签'],'review_due_at':'2026-11-01'})
        self.db.admin_undo(mid,2);m=self.db.format_memory_item(self.db._admin_row(mid));self.assertEqual((m['domain_id'],m['tags'],m['review_due_at']),('tools',['合成'],None))
        self.db.admin_restore(mid,2,3);m=self.db.format_memory_item(self.db._admin_row(mid));self.assertEqual((m['domain_id'],m['tags']),('learning',['新标签']))

    def test_proposal_fields_live_contract_and_stale(self):
        r=self.write(self.payload(scope='user_global'));mid=r['memory_id'];pid=r['proposal_id']
        with self.assertRaises(HubError):self.db.admin_resolve_conflict(mid,pid,'edit',1,'建议标题','建议正文',final_fields={'scope':'project','project_id':'fixture'})
        self.db.admin_resolve_conflict(mid,pid,'edit',1,'建议标题','建议正文',final_fields={'domain_id':'learning','tags':['已审'],'scope':'project','project_id':'fixture'},confirm_scope_change=True)
        m=self.db._admin_row(mid);self.assertEqual((m['title'],m['content'],m['domain_id'],m['scope']),('建议标题','建议正文','learning','project'))
        r=self.write(self.payload(scope='user_global',title='另一个提案'))
        self.db.admin_set_lock(r['memory_id'],True,1)
        for resolution in ('accept','edit'):
            with self.assertRaises(HubError) as e:self.db.admin_resolve_conflict(r['memory_id'],r['proposal_id'],resolution,2,'新','新正文')
            self.assertEqual(e.exception.error_code,'proposal_not_pending')
        with self.db.connect() as con:
            pending=con.execute("SELECT proposal_id,base_revision FROM conflict_proposals WHERE memory_id=? AND status='pending'",(r['memory_id'],)).fetchone()
            self.assertEqual(pending['base_revision'],2)
        self.db.admin_resolve_conflict(r['memory_id'],pending['proposal_id'],'reject',2)
        self.assertEqual(self.db._admin_row(r['memory_id'])['title'],'另一个提案')

    def test_source_versions_purge_and_block_reimport(self):
        src=self.db.register_source_snapshot('fixture-source','合成来源','来源第一版')
        self.assertEqual(self.db.register_source_snapshot('fixture-source','合成来源','来源第一版')['source_revision'],1)
        self.db.register_source_snapshot('fixture-source','合成来源','来源第二版')
        p=self.payload();p['source'].update(src);r=self.write(p);mid=r['memory_id']
        self.db.admin_purge(mid,f'PURGE {mid} r1',1,lambda _:True)
        self.assertEqual(self.db.workbench_sources()['items'],[])
        with self.assertRaises(HubError):self.db.register_source_snapshot('fixture-source','合成来源','来源第一版')
        with self.assertRaises(HubError):self.write(p)

    def test_vector_revision_and_generation(self):
        r=self.write(self.payload());mid=r['memory_id']
        for rev,generation in [(0,'v1'),(1,'wrong')]:
            data=self.db.workbench_list({'query':'非字面问题','mode':'semantic'},vector_search_fn=lambda *a:([{'memory_id':mid,'revision':rev,'generation':generation}],False))
            self.assertEqual(data['total'],0)
        data=self.db.workbench_list({'query':'非字面问题','mode':'semantic'},vector_search_fn=lambda *a:([{'memory_id':mid,'revision':1,'generation':'v1'}],False))
        self.assertEqual(data['items'][0]['memory_id'],mid)

    def test_explicit_migration_and_missing_tombstone(self):
        r=self.write(self.payload());con=self.db.connect()
        for col in ('domain_id','tags_json','review_due_at'):con.execute(f'ALTER TABLE current_memory DROP COLUMN {col}')
        con.execute('ALTER TABLE requests DROP COLUMN result_json')
        for table in ('domains','source_versions','source_links','saved_views','source_redactions'):con.execute(f'DROP TABLE {table}')
        con.execute('PRAGMA user_version=300');con.close()
        with self.assertRaises(HubError):MemoryDatabase(self.db.db_path,self.db.tombstone_path,self.db.exports_dir,self.db.backups_dir)
        result=migrate(self.root);self.assertEqual(result['schema'],SCHEMA_VERSION);self.assertTrue(Path(result['backup']).is_file())
        self.db=MemoryDatabase(self.db.db_path,self.db.tombstone_path,self.db.exports_dir,self.db.backups_dir)
        self.assertEqual(self.db._admin_row(r['memory_id'])['content'],self.payload()['content'])
        self.db.tombstone_path.unlink()
        with self.assertRaises(HubError):migrate(self.root)

    def test_two_level_domain_and_views(self):
        child=self.db.workbench_domain({'name':'工具接入','parent_id':'tools'})['domain_id']
        with self.assertRaises(HubError):self.db.workbench_domain({'name':'第三层','parent_id':child})
        self.write(self.payload(domain_id=child));self.assertEqual(self.db.workbench_list({'domain_id':'tools'})['total'],1)
        self.db.workbench_save_view({'name':'工具','filters':{'domain_id':'tools'}})
        self.assertEqual(self.db.workbench_catalog()['views'][0]['filters'],{'domain_id':'tools'})

    def test_live_ui_has_no_unsafe_sinks_or_cache(self):
        js=(ROOT/'hub/workbench-v0.7.0/workbench.js').read_text(encoding='utf-8')
        for term in ('innerHTML','outerHTML','insertAdjacentHTML','eval(','localStorage.','sessionStorage.'):
            self.assertNotIn(term,js)

if __name__=='__main__':unittest.main(verbosity=2)
