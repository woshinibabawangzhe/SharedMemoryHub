"""Independent synthetic memory-draft transactions, access, tombstones and API contracts."""
import json
import sqlite3
import sys
import uuid
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from hub.db import MemoryDatabase, HubError, SCHEMA_VERSION
from hub.auth import AuthManager
from hub.app import create_hub_app
from scripts.migrate_schema import migrate
from fastapi.testclient import TestClient
from test_safety_030 import EmbeddingFixture, VectorFixture


def request(**fields):return {'request_key':uuid.uuid4().hex,**fields}


class Drafts(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'receipts/drafts-060-20261110-091851/synthetic'/uuid.uuid4().hex
        self.db=MemoryDatabase(self.root/'data/sqlite/memory_hub.db',self.root/'data/tombstones/purge_registry.jsonl',self.root/'exports',self.root/'backups')
        self.ws=self.root/'workspace';self.ws.mkdir();self.db.register_project_binding('fixture',self.ws)

    def create(self,**kw):return self.db.admin_draft_create(request(raw_text='合成原话：我考虑以后用更简短的答复。',**kw))
    def grant(self,d,clients=None):
        r=self.db.admin_draft_action(d['draft_id'],'grant',request(expected_revision=d['revision'],clients=clients or ['codex','antigravity']))
        return {'draft_id':d['draft_id'],'revision':r['revision']}
    def suggest(self,d,client='codex',**extra):
        item=self.db.draft_detail(d['draft_id'])
        p=request(session_id='synthetic-session-'+client,base_draft_revision=d['revision'],base_memory_revision=item['base_memory_revision'],
                  fields={'title':'合成沟通意向','content':'合成用户考虑采用简短答复，尚未决定。'},reason='保留原话中的意向和不确定性。')
        p.update(extra)
        return self.db.draft_suggest(d['draft_id'],client,client,p)
    def accept(self,d,s,**kw):return self.db.admin_draft_action(d['draft_id'],'accept',request(expected_revision=d['revision'],suggestion_id=s['suggestion_id'],**kw))
    def memory(self,**kw):return self.db.write_memory('user_admin','user','web_ui',request(operation='create',title='原始合成记忆',content='原正文',subject='user',scope='user_global',source={'origin_type':'admin_manual'},**kw))

    def test_drafts_never_index_or_recall(self):
        d=self.create();self.grant(d)
        with closing(self.db.connect()) as c:
            for table in ('current_memory','memory_fts','index_work'):self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.db.workbench_list()['total'],0)

    def test_access_grants_scope_and_revocation(self):
        d=self.create(fields={'scope':'project','project_id':'fixture'})
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'],'codex','codex',str(self.ws))
        d=self.grant(d,['codex'])
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'],'antigravity','antigravity',str(self.ws))
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'],'codex','codex',None)
        self.assertEqual(self.db.draft_detail(d['draft_id'],'codex','codex',str(self.ws))['revision'],2)
        self.db.admin_draft_action(d['draft_id'],'grant',request(expected_revision=2,clients=[]))
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'],'codex','codex',str(self.ws))

    def test_multi_client_stale_and_original_versions(self):
        d=self.grant(self.create());a=self.suggest(d);self.suggest(d,'antigravity')
        self.assertEqual(len(self.db.draft_detail(d['draft_id'])['suggestions']),2)
        self.db.admin_draft_action(d['draft_id'],'edit',request(expected_revision=2,raw_text='合成用户已澄清：只在忙碌时简短。'))
        with self.assertRaises(HubError) as e:self.accept({'draft_id':d['draft_id'],'revision':3},a)
        self.assertEqual(e.exception.error_code,'stale_suggestion')
        self.assertIn('考虑',self.db.draft_detail(d['draft_id'])['versions'][-1]['snapshot']['raw_text'])

    def test_atomic_create_failure_rolls_back_and_same_key_recovers(self):
        d=self.grant(self.create());s=self.suggest(d);p=request(expected_revision=2,suggestion_id=s['suggestion_id'])
        with patch.object(self.db,'_draft_finish',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):self.db.admin_draft_action(d['draft_id'],'accept',p)
        self.assertEqual(self.db.workbench_list()['total'],0)
        self.assertEqual(self.db.draft_detail(d['draft_id'])['status'],'awaiting_review')
        a=self.db.admin_draft_action(d['draft_id'],'accept',p);b=self.db.admin_draft_action(d['draft_id'],'accept',p)
        self.assertEqual(a['memory_id'],b['memory_id']);self.assertTrue(b['idempotent_replay']);self.assertEqual(self.db.workbench_list()['total'],1)
        self.assertEqual(self.db.draft_request_result(p['request_key'],'user')['memory_id'],a['memory_id'])
        with self.assertRaises(HubError):self.db.draft_request_result(p['request_key'],'codex','codex')

    def test_locked_update_atomic_and_stale_target(self):
        m=self.memory();d=self.grant(self.create(target_memory_id=m['memory_id'],expected_memory_revision=1));s=self.suggest(d)
        with self.assertRaises(HubError) as e:self.accept(d,s,expected_memory_revision=1)
        self.assertEqual(e.exception.error_code,'LockedConflict')
        with patch.object(self.db,'_draft_finish',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):self.accept(d,s,expected_memory_revision=1,confirm_locked_edit=True)
        self.assertEqual(self.db._admin_row(m['memory_id'])['revision'],1)
        self.db.admin_set_state(m['memory_id'],'paused',1)
        with self.assertRaises(HubError):self.accept(d,s,expected_memory_revision=2,confirm_locked_edit=True)
        self.assertEqual(self.db._admin_row(m['memory_id'])['revision'],2)

    def test_concurrent_accept_has_one_atomic_result(self):
        d=self.grant(self.create());s=self.suggest(d)
        p=request(expected_revision=d['revision'],suggestion_id=s['suggestion_id'])
        with ThreadPoolExecutor(max_workers=2) as workers:
            a,b=list(workers.map(lambda _:self.db.admin_draft_action(d['draft_id'],'accept',p),range(2)))
        self.assertEqual(a['memory_id'],b['memory_id'])
        self.assertEqual(self.db.workbench_list()['total'],1)
        self.assertEqual(self.db.draft_detail(d['draft_id'])['status'],'applied')

    def test_correction_preserves_private_owner(self):
        m=self.db.write_memory('agent_client','codex','codex',request(operation='create',
            title='合成私有记忆',content='仅Codex可见的合成正文',subject='user',scope='agent_private',
            kind='context',session_id='synthetic-private-session',source={'origin_type':'user_explicit',
                'session_id':'synthetic-private-session','evidence':'合成用户明确确认'}))
        d=self.grant(self.create(target_memory_id=m['memory_id'],expected_memory_revision=1),['codex'])
        self.assertEqual(self.db.draft_detail(d['draft_id'],'codex','codex')['target']['memory_id'],m['memory_id'])
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'],'antigravity','antigravity')
        s=self.suggest(d);self.accept(d,s,expected_memory_revision=1)
        row=self.db._admin_row(m['memory_id'])
        self.assertEqual(row['created_by_actor_id'],'codex')
        self.assertEqual(row['scope'],'agent_private')

    def test_legacy_tombstone_remains_verifiable(self):
        import hashlib
        m=self.memory();genesis=json.loads(self.db.tombstone_path.read_text().splitlines()[0])
        item={'seq':1,'prev_hash':genesis['entry_hash'],'memory_id':m['memory_id'],
              'purged_content_sha256':hashlib.sha256(b'synthetic').hexdigest(),
              'request_keys':[],'actor_id':'user','purged_at':'2026-11-10T00:00:00+00:00'}
        item['entry_hash']=hashlib.sha256(f"{item['prev_hash']}:1:{item['memory_id']}:{item['purged_content_sha256']}".encode()).hexdigest()
        with self.db.tombstone_path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(item)+'\n')
        self.db.verify_and_reconcile_purge_registry()
        self.assertEqual(self.db.workbench_list()['total'],0)

    def test_duplicate_rule_hypothesis_and_handoff(self):
        for kind in ('context','rule_candidate','handoff'):
            fields={'kind':kind,'confidence':'hypothesis'}
            d=self.grant(self.create(fields=fields));s=self.suggest(d);a=self.accept(d,s)
            m=self.db._admin_row(a['memory_id'])
            self.assertEqual(m['confidence'],'hypothesis')
            if kind=='rule_candidate':self.assertEqual(m['state'],'paused')
            if kind=='handoff':self.assertIsNotNone(m['expires_at'])
        d=self.grant(self.create(fields={'confidence':'confirmed'}));s=self.suggest(d);a=self.accept(d,s)
        d=self.grant(self.create(fields={'confidence':'confirmed'}));s=self.suggest(d);b=self.accept(d,s)
        self.assertEqual(a['memory_id'],b['memory_id']);self.assertTrue(b['duplicate'])

    def test_boundary_and_missing_session_guards(self):
        d=self.grant(self.create());s=self.suggest(d,fields={'title':'合成扩大范围','content':'合成正文','scope':'global'})
        with self.assertRaises(HubError):self.accept(d,s)
        a=self.accept(d,s,confirm_scope_change=True);self.assertEqual(self.db._admin_row(a['memory_id'])['scope'],'global')
        d=self.grant(self.create())
        with self.assertRaises(HubError):self.suggest(d,session_id='')
        s=self.suggest(d,kind='clarify',fields={},questions=['这个偏好适用于什么场景？'])
        with self.assertRaises(HubError):self.accept(d,s)

    def test_memory_purge_cascades_and_old_backup_cannot_resurrect(self):
        d=self.grant(self.create());s=self.suggest(d);a=self.accept(d,s)
        backup=self.db.admin_backup_export('before-purge')['backup_db_path']
        self.db.admin_purge(a['memory_id'],f"PURGE {a['memory_id']} r1",1,lambda _:True)
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'])
        self.db.admin_backup_restore(backup,lambda _:True)
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'])
        with closing(self.db.connect()) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM draft_suggestions').fetchone()[0],0)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM draft_versions').fetchone()[0],0)

    def test_standalone_draft_purge_backup_and_integrity(self):
        d=self.create();backup=self.db.admin_backup_export('draft-only')['backup_db_path']
        self.db.admin_draft_action(d['draft_id'],'cancel',request(expected_revision=1))
        self.assertEqual(self.db.admin_draft_purge(d['draft_id'],request(expected_revision=2,confirm_purge=f"PURGE {d['draft_id']} r2"))['status'],'purged')
        self.db.admin_backup_restore(backup,lambda _:False)
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'])
        records=self.db.tombstone_path.read_text().splitlines();item=json.loads(records[-1]);item['draft_ids']=[];records[-1]=json.dumps(item)
        self.db.tombstone_path.write_text('\n'.join(records)+'\n')
        with self.assertRaises(HubError):self.db.verify_and_reconcile_purge_registry()

    def test_explicit_400_migration_preserves_memory(self):
        m=self.memory();con=self.db.connect()
        for table in ('memory_drafts','draft_versions','draft_suggestions','draft_requests'):con.execute('DROP TABLE '+table)
        con.execute('PRAGMA user_version=400');con.close()
        with self.assertRaises(HubError):MemoryDatabase(self.db.db_path,self.db.tombstone_path,self.db.exports_dir,self.db.backups_dir)
        proof=migrate(self.root);self.assertEqual(proof['schema'],SCHEMA_VERSION)
        self.assertEqual(self.db._admin_row(m['memory_id'])['content'],'原正文')

    def test_http_auth_csrf_and_clients(self):
        auth=AuthManager(self.root/'config/client_tokens.json',self.root/'config/admin_auth.json',initial_passcode='Synthetic-drafts-060',allowed_origin='http://testserver')
        tokens=json.loads(auth.client_tokens_path.read_text())
        with TestClient(create_hub_app(self.db,auth,EmbeddingFixture(),VectorFixture(),auto_start_worker=False,lock_path=self.root/'hub.lock')) as client:
            self.assertEqual(client.get('/v1/admin/drafts').status_code,401)
            sess=client.post('/v1/admin/login',json={'passcode':'Synthetic-drafts-060'},headers={'Origin':'http://testserver'}).json()
            headers={'Origin':'http://testserver','X-CSRF-Token':sess['csrf_token']}
            self.assertEqual(client.post('/v1/admin/drafts',json=request(raw_text='合成')).status_code,403)
            d=client.post('/v1/admin/drafts',json=request(raw_text='合成原话'),headers=headers).json()
            self.assertEqual(client.get('/v1/admin/help').status_code,200)
            client.post('/v1/admin/drafts/'+d['draft_id']+'/grant',json=request(expected_revision=1,clients=['codex','antigravity']),headers=headers).raise_for_status()
            client.cookies.clear()
            for name,key in [('codex','CODEX_CLIENT_TOKEN'),('antigravity','AGY_CLIENT_TOKEN')]:
                bearer={'Authorization':'Bearer '+tokens[key]}
                self.assertEqual(client.get('/v1/drafts/'+d['draft_id'],headers=bearer).status_code,200)
                self.assertEqual(client.post('/v1/admin/drafts/'+d['draft_id']+'/accept',json={},headers=bearer).status_code,403)


if __name__=='__main__':unittest.main(verbosity=2)
