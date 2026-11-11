"""Synthetic only: related drafting, approvals, atomicity, rollback and erasure."""
import json
import sqlite3
import uuid
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import test_drafts_060 as draft_fixture
from test_drafts_060 import request
from hub.db import HubError, MemoryDatabase, SCHEMA_VERSION
from scripts.migrate_schema import migrate

ROOT=Path(__file__).resolve().parents[1]

class RelatedDrafts(draft_fixture.Drafts):
    def setUp(self):
        self.root=ROOT/'receipts/related-070-synthetic'/uuid.uuid4().hex
        self.db=MemoryDatabase(self.root/'data/sqlite/memory_hub.db',self.root/'data/tombstones/purge_registry.jsonl',self.root/'exports',self.root/'backups')
        self.ws=self.root/'workspace';self.ws.mkdir();self.db.register_project_binding('fixture',self.ws)

    def memory(self,**kw):
        return self.db.write_memory('user_admin','user','web_ui',request(**{'operation':'create','title':'原始合成记忆','content':'原正文','subject':'user','scope':'user_global','source':{'origin_type':'admin_manual'},**kw}))

    def group(self,count=3,**extra):
        memories=[self.memory(title='合成工具'+uuid.uuid4().hex[:8]+str(i),content='合成 Codex 旧入口。独有资料'+str(i),**extra) for i in range(count)]
        members=[{'memory_id':m['memory_id'],'base_revision':m['stored_revision']} for m in memories]
        d=self.db.admin_draft_create(request(raw_text='合成整理：Codex桌面为主；保留各条独有资料。',members=members))
        return memories,self.grant(d)

    def changes(self,d,**extra):
        return [{'memory_id':m['memory_id'],'base_revision':m['base_revision'],'action':'update',
                 'fields':{'content':'合成 Codex 桌面为主。独有资料'+str(i)},'reason':'纠正旧分工，保留其他事实。',
                 'confirmation':'confirmed','confirm_locked_edit':True,**extra}
                for i,m in enumerate(self.db.draft_detail(d['draft_id'])['members'])]

    def suggestion(self,d,changes=None):
        changes=changes or self.changes(d)
        return self.db.draft_suggest(d['draft_id'],'codex','codex',request(base_draft_revision=d['revision'],session_id='synthetic-codex-070',kind='ready',reason='合成关联整理',changes=changes))

    def apply(self,d,s,changes=None,**extra):
        return self.db.admin_draft_action(d['draft_id'],'accept',request(expected_revision=d['revision'],suggestion_id=s['suggestion_id'],**({'changes':changes} if changes else {}),**extra))

    def test_group_one_application_preserves_distinct_content_and_other_suggestions(self):
        memories,d=self.group();s=self.suggestion(d);self.suggestion(d)
        r=self.apply(d,s)
        self.assertEqual(len(r['results']),3)
        for m in memories:
            old=self.db._admin_row(m['memory_id']);self.assertEqual(old['revision'],2);self.assertTrue(old['locked']);self.assertIn('独有资料',old['content'])
        self.assertEqual({s['status'] for s in self.db.draft_detail(d['draft_id'])['suggestions']},{'accepted','superseded'})

    def test_group_failure_rolls_back_all_members_and_result(self):
        memories,d=self.group();s=self.suggestion(d);original=self.db._admin_commit;calls=[]
        def fail(*a,**kw):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('synthetic midway failure')
            return original(*a,**kw)
        with patch.object(self.db,'_admin_commit',side_effect=fail):
            with self.assertRaises(RuntimeError):self.apply(d,s)
        self.assertTrue(all(self.db._admin_row(m['memory_id'])['revision']==1 for m in memories))
        self.assertEqual(self.db.draft_detail(d['draft_id'])['status'],'awaiting_review')
        self.assertEqual(len(self.apply(d,s)['results']),3)

    def test_group_checks_every_revision_including_kept_members(self):
        memories,d=self.group();changes=self.changes(d);changes[1]={'memory_id':changes[1]['memory_id'],'base_revision':1,'action':'keep'};s=self.suggestion(d,changes)
        self.db.admin_set_lock(changes[1]['memory_id'],False,1)
        with self.assertRaises(HubError) as e:self.apply(d,s,changes)
        self.assertEqual(e.exception.error_code,'RevisionConflict')
        self.assertEqual(self.db._admin_row(changes[0]['memory_id'])['revision'],1)

    def test_group_locked_confirmation_and_human_confirmation_required(self):
        memories,d=self.group();changes=self.changes(d);changes[1]['confirm_locked_edit']=False;s=self.suggestion(d,changes)
        with self.assertRaises(HubError) as e:self.apply(d,s,changes)
        self.assertEqual(e.exception.error_code,'LockedConflict')
        changes[1]['confirm_locked_edit']=True;changes[0].pop('confirmation')
        with self.assertRaises(HubError):self.apply(d,s,changes)
        self.assertTrue(all(self.db._admin_row(m['memory_id'])['revision']==1 for m in memories))

    def test_group_scope_confirmation_cannot_be_omitted(self):
        _,d=self.group();changes=self.changes(d);changes[1]['fields']['readers']=['user','codex'];s=self.suggestion(d,changes)
        with self.assertRaises(HubError) as e:self.apply(d,s,changes)
        self.assertEqual(e.exception.error_code,'scope_confirmation_required')
        changes[1]['confirm_scope_change']=True;self.apply(d,s,changes)

    def test_all_group_members_enforce_agent_access(self):
        a=self.memory(title='合成公开',content='A');b=self.memory(title='合成限读',content='B',readers=['user','antigravity'])
        d=self.db.admin_draft_create(request(raw_text='合成组',members=[{'memory_id':m['memory_id'],'base_revision':1} for m in (a,b)]));self.grant(d)
        with self.assertRaises(HubError) as e:self.db.draft_detail(d['draft_id'],'codex','codex')
        self.assertEqual(e.exception.status_code,403)

    def test_group_idempotent_and_concurrent_apply(self):
        _,d=self.group();s=self.suggestion(d);p=request(expected_revision=d['revision'],suggestion_id=s['suggestion_id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=list(pool.map(lambda _:self.db.admin_draft_action(d['draft_id'],'accept',p),range(2)))
        self.assertEqual(a['results'],b['results']);self.assertTrue(b['idempotent_replay'])
        self.assertTrue(all(x['stored_revision']==2 for x in a['results']))

    def test_pending_scope_is_rebased_without_approval(self):
        m=self.db.write_memory('agent_client','codex','codex',request(operation='create',title='合成待批',content='合成旧入口',subject='user',scope='user_global',source={'origin_type':'user_explicit','session_id':'synthetic-codex'}))
        d=self.db.admin_draft_create(request(raw_text='合成修正',members=[{'memory_id':m['memory_id'],'base_revision':1}]))
        d=self.grant(d);s=self.suggestion(d);self.apply(d,s)
        old=self.db._admin_row(m['memory_id']);self.assertEqual(old['state'],'paused');self.assertTrue(old['has_open_conflict'])
        with closing(self.db.connect()) as c:
            pending=c.execute("SELECT * FROM conflict_proposals WHERE memory_id=? AND status='pending'",(m['memory_id'],)).fetchall()
            self.assertEqual(len(pending),1);self.assertEqual(pending[0]['base_revision'],2);self.assertIn('桌面',pending[0]['proposed_content'])
        self.assertEqual(self.db._admin_row(m['memory_id'])['revision'],2)

    def test_explicit_group_scope_approval_and_review_are_separate(self):
        m=self.db.write_memory('agent_client','codex','codex',request(operation='create',title='合成待批2',content='合成待批事实',subject='user',scope='user_global',source={'origin_type':'user_explicit','session_id':'synthetic-codex'}))
        self.db.admin_confirm_review(m['memory_id'],request(expected_revision=1,confirm_accuracy=True,complete_review=True,next_review_due_at=None))
        old=self.db._admin_row(m['memory_id']);self.assertEqual(old['state'],'paused');self.assertTrue(old['has_open_conflict'])

    def test_merge_pauses_and_links_but_never_deletes(self):
        memories,d=self.group();changes=self.changes(d);changes[1].update(action='merge',replacement_id=changes[0]['memory_id']);s=self.suggestion(d,changes)
        self.apply(d,s,changes);old=self.db._admin_row(changes[1]['memory_id']);self.assertEqual(old['state'],'paused')
        self.assertEqual(self.db.memory_relations(changes[1]['memory_id'])[0]['relation'],'superseded_by')
        self.assertEqual(self.db.workbench_list()['total'],3)

    def test_merge_accepts_same_access_members_in_different_order(self):
        _,d=self.group(2,readers=['user','codex','antigravity'],host_constraints=['codex','antigravity'])
        changes=self.changes(d)
        changes[1].update(action='merge',replacement_id=changes[0]['memory_id'])
        changes[1]['fields'].update(readers=['antigravity','user','codex'],host_constraints=['antigravity','codex'])
        s=self.suggestion(d,changes)
        result=self.apply(d,s,changes)
        self.assertEqual(len(result['results']),2)
        self.assertEqual(self.db._admin_row(changes[1]['memory_id'])['state'],'paused')
        self.assertEqual(self.db.memory_relations(changes[1]['memory_id'])[0]['target_id'],changes[0]['memory_id'])

    def test_merge_different_host_members_rejected_atomically(self):
        _,d=self.group(2,host_constraints=['codex','antigravity'])
        changes=self.changes(d)
        changes[1].update(action='merge',replacement_id=changes[0]['memory_id'],confirm_scope_change=True)
        changes[1]['fields']['host_constraints']=['codex']
        s=self.suggestion(d,changes)
        with self.assertRaises(HubError) as e:self.apply(d,s,changes)
        self.assertEqual(e.exception.error_code,'incompatible_merge')
        self.assertTrue(all(self.db._admin_row(c['memory_id'])['revision']==1 for c in changes))

    def test_merge_incompatible_scope_rejected_atomically(self):
        _,d=self.group();changes=self.changes(d);changes[1].update(action='merge',replacement_id=changes[0]['memory_id']);changes[1]['fields']['readers']=['user','codex'];changes[1]['confirm_scope_change']=True;s=self.suggestion(d,changes)
        with self.assertRaises(HubError) as e:self.apply(d,s,changes)
        self.assertEqual(e.exception.error_code,'incompatible_merge')

    def test_group_undo_new_revisions_and_later_edit_conflict(self):
        memories,d=self.group();s=self.suggestion(d);self.apply(d,s)
        r=self.db.admin_draft_action(d['draft_id'],'undo-apply',request(expected_revision=3));self.assertEqual(r['status'],'group_undone')
        for m in memories:
            row=self.db._admin_row(m['memory_id']);self.assertEqual(row['revision'],3);self.assertIn('旧入口',row['content'])
        _,e=self.group();s=self.suggestion(e);self.apply(e,s);mid=self.db.draft_detail(e['draft_id'])['members'][1]['memory_id'];self.db.admin_set_lock(mid,False,2)
        with self.assertRaises(HubError):self.db.admin_draft_action(e['draft_id'],'undo-apply',request(expected_revision=3))

    def test_candidate_includes_paused_and_suppresses_only_current_pair(self):
        memories,d=self.group(2);a,b=memories;self.db.admin_set_state(b['memory_id'],'paused',1)
        result=self.db.related_candidates({'memory_id':a['memory_id']});self.assertEqual(result['items'][0]['memory_id'],b['memory_id'])
        self.db.review_duplicate_candidates({'request_key':uuid.uuid4().hex,'memory_ids':[a['memory_id'],b['memory_id']],'expected_revisions':{a['memory_id']:1,b['memory_id']:2},'decision':'keep_both'})
        self.assertEqual(self.db.related_candidates({'memory_id':a['memory_id']})['items'],[])
        self.db.admin_set_lock(b['memory_id'],False,2)
        self.assertEqual(len(self.db.related_candidates({'memory_id':a['memory_id']})['items']),1)

    def test_review_records_time_preserves_lock_and_paused_state(self):
        m=self.memory(confidence='hypothesis',review_due_at='2020-01-01T00:00:00Z');self.db.admin_set_state(m['memory_id'],'paused',1)
        p=request(expected_revision=2,confirm_accuracy=True,confirm_locked_edit=True,complete_review=True)
        r=self.db.admin_confirm_review(m['memory_id'],p);r2=self.db.admin_confirm_review(m['memory_id'],p)
        row=self.db._admin_row(m['memory_id']);self.assertEqual(row['confidence'],'confirmed');self.assertEqual(row['state'],'paused');self.assertTrue(row['locked']);self.assertIsNotNone(row['last_reviewed_at']);self.assertIsNone(row['review_due_at']);self.assertEqual(r['stored_revision'],r2['stored_revision'])

    def test_no_change_does_not_increment_revision(self):
        m=self.memory();r=self.db.admin_set_lock(m['memory_id'],True,1);self.assertEqual(r['status'],'unchanged');self.assertEqual(r['stored_revision'],1)

    def test_purging_nonprimary_member_erases_group_copies_only(self):
        memories,d=self.group();s=self.suggestion(d);self.apply(d,s);detail=self.db.draft_detail(d['draft_id']);mid=next(m['memory_id'] for m in detail['members'] if m['memory_id']!=detail['target_memory_id'])
        self.db.admin_purge(mid,expected_revision=2,confirm_purge=f'PURGE {mid} r2')
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'])
        with closing(self.db.connect()) as c:
            for table in ('draft_members','draft_applications','draft_versions','draft_suggestions'):self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table+' WHERE draft_id=?',(d['draft_id'],)).fetchone()[0],0)
        self.assertEqual(len(self.db.workbench_list()['items']),2)

    def test_explicit_500_migration_adds_tables_without_changing_memory(self):
        m=self.memory();before=self.db._admin_row(m['memory_id'])
        with closing(self.db.connect()) as c:
            c.execute('ALTER TABLE current_memory DROP COLUMN last_reviewed_at');c.execute('PRAGMA user_version=500')
        result=migrate(self.root);self.assertEqual(result['from_schema'],500);self.assertEqual(result['schema'],SCHEMA_VERSION)
        after=self.db._admin_row(m['memory_id']);self.assertTrue(all(after[k]==v for k,v in before.items() if k!='last_reviewed_at'))


    def test_confirmation_rebases_pending_proposal_without_approving(self):
        m=self.db.write_memory('agent_client','codex','codex',request(operation='create',title='合成待核实范围',content='合成事实',subject='user',scope='user_global',source={'origin_type':'user_explicit','session_id':'synthetic'}))
        self.db.admin_confirm_review(m['memory_id'],request(expected_revision=1,confirm_accuracy=True,complete_review=True))
        with closing(self.db.connect()) as c:
            prop=c.execute("SELECT * FROM conflict_proposals WHERE memory_id=? AND status='pending'",(m['memory_id'],)).fetchone()
            self.assertEqual(prop['base_revision'],self.db._admin_row(m['memory_id'])['revision'])
            self.assertEqual(self.db._admin_row(m['memory_id'])['state'],'paused')
            pid=prop['proposal_id']
        self.db.admin_resolve_conflict(m['memory_id'],pid,'accept',2)
        self.assertEqual(self.db._admin_row(m['memory_id'])['state'],'active')

    def test_group_no_change_keeps_revision_and_pending_baseline(self):
        m=self.db.write_memory('agent_client','codex','codex',request(operation='create',title='合成无变化',content='合成原文',subject='user',scope='user_global',source={'origin_type':'user_explicit','session_id':'synthetic'}))
        d=self.grant(self.db.admin_draft_create(request(raw_text='合成无变化检查',members=[{'memory_id':m['memory_id'],'base_revision':1}])))
        changes=[{'memory_id':m['memory_id'],'base_revision':1,'action':'update','fields':{},'confirmation':'hypothesis'}]
        result=self.apply(d,self.suggestion(d,changes),changes)
        self.assertEqual(result['results'][0]['outcome'],'unchanged')
        with closing(self.db.connect()) as c:self.assertEqual(c.execute("SELECT base_revision FROM conflict_proposals WHERE status='pending'").fetchone()[0],1)

    def test_relations_preserved_by_edit_and_removed_by_history_restore(self):
        _,d=self.group(2);changes=self.changes(d);changes[0]['related_ids']=[changes[1]['memory_id']]
        self.apply(d,self.suggestion(d,changes),changes);mid=changes[0]['memory_id']
        self.db.admin_set_lock(mid,False,2)
        self.assertEqual(len(self.db.memory_relations(mid)),1)
        self.db.admin_restore(mid,1,3)
        self.assertEqual(self.db.memory_relations(mid),[])

    def test_merge_target_must_explicitly_accept_contribution(self):
        _,d=self.group(2);changes=self.changes(d);changes[0]={'memory_id':changes[0]['memory_id'],'base_revision':1,'action':'keep'}
        changes[1].update(action='merge',replacement_id=changes[0]['memory_id'])
        with self.assertRaises(HubError):self.apply(d,self.suggestion(d,changes),changes)

    def test_old_backup_cannot_revive_group_after_nonprimary_purge(self):
        memories,d=self.group(2);self.apply(d,self.suggestion(d));backup=self.db.admin_backup_export()
        mid=next(m['memory_id'] for m in memories if m['memory_id']!=self.db.draft_detail(d['draft_id'])['target_memory_id'])
        self.db.admin_purge(mid,expected_revision=2,confirm_purge=f'PURGE {mid} r2')
        self.db.admin_backup_restore(backup['backup_db_path'])
        with self.assertRaises(HubError):self.db.draft_detail(d['draft_id'])
        self.assertEqual(self.db.workbench_list()['total'],1)

    def test_duplicate_review_replays_original_result_after_revision_changes_and_restart(self):
        memories,d=self.group(2);a,b=memories
        body=request(memory_ids=[a['memory_id'],b['memory_id']],expected_revisions={m['memory_id']:1 for m in memories},decision='defer')
        first=self.db.review_duplicate_candidates(body)
        self.db.admin_set_lock(b['memory_id'],False,1)
        again=self.db.review_duplicate_candidates(body)
        self.assertTrue(again['idempotent_replay']);self.assertEqual(again['pair_key'],first['pair_key'])
        with self.assertRaises(HubError):self.db.review_duplicate_candidates({**body,'decision':'keep_both'})
        self.db.admin_purge(b['memory_id'],expected_revision=2,confirm_purge=f"PURGE {b['memory_id']} r2")
        with self.assertRaises(HubError) as error:self.db.review_duplicate_candidates(body)
        self.assertEqual(error.exception.status_code,410)

    def test_editor_saves_all_metadata_rebases_proposals_and_ignores_noop(self):
        m=self.memory();mid=m['memory_id']
        result=self.db.write_memory('user_admin','user','web_ui',request(operation='update',memory_id=mid,expected_revision=1,title='新标题',content='新正文',readers=['user','codex'],scope='user_global',subject='user',kind='experience'))
        row=self.db._admin_row(mid);self.assertEqual(row['revision'],2);self.assertEqual(row['kind'],'experience');self.assertEqual(json.loads(row['readers_json']),['user','codex'])
        same=self.db.write_memory('user_admin','user','web_ui',request(operation='update',memory_id=mid,expected_revision=2,title='新标题',content='新正文'))
        self.assertEqual(same['stored_revision'],2);self.assertEqual(same['status'],'unchanged')

    def test_write_path_revision_keeps_relation_snapshot(self):
        _,d=self.group(2);changes=self.changes(d);changes[0]['related_ids']=[changes[1]['memory_id']]
        self.apply(d,self.suggestion(d,changes),changes);mid=changes[0]['memory_id']
        self.db.write_memory('user_admin','user','web_ui',request(operation='update',memory_id=mid,expected_revision=2,content='单条更新关系仍保留'))
        self.db.admin_set_lock(mid,False,3)
        self.db.admin_restore(mid,3,4)
        self.assertEqual(len(self.db.memory_relations(mid)),1)

    def test_help_and_served_bundle_match_service_and_schema(self):
        auth=draft_fixture.AuthManager(self.root/'config/client_tokens.json',self.root/'config/admin_auth.json',initial_passcode='Synthetic-release-070',allowed_origin='http://testserver')
        app=draft_fixture.create_hub_app(self.db,auth,draft_fixture.EmbeddingFixture(),draft_fixture.VectorFixture(),auto_start_worker=False,lock_path=self.root/'hub.lock')
        with draft_fixture.TestClient(app) as client:
            login=client.post('/v1/admin/login',json={'passcode':'Synthetic-release-070'},headers={'Origin':'http://testserver'})
            self.assertEqual(login.status_code,200)
            help_doc=client.get('/v1/admin/help').json()
            health=client.get('/v1/health').json()
            self.assertEqual(help_doc['service_version'],health['version'])
            self.assertEqual(help_doc['schema_version'],health['schema_version'])
            self.assertIn(help_doc['workbench_version'],client.get('/ui').text)
            self.assertEqual(client.get('/workbench.js').content,(ROOT/'hub/workbench-v0.7.0/workbench.js').read_bytes())

    def test_explicit_relation_reports_direction_and_does_not_invent_text_overlap(self):
        a=self.memory(title='Alpha',content='unicorn')
        b=self.memory(title='Zeta',content='bison')
        d=self.grant(self.create(members=[{'memory_id':m['memory_id'],'base_revision':1} for m in (a,b)]))
        # draft_detail sorts members by stable ID, not creation order.
        update=next(c for c in self.changes(d) if c['memory_id']==a['memory_id'])
        update['fields']={'title':'Alpha','content':'unicorn'}
        update['related_ids']=[b['memory_id']]
        changes=[update,{'memory_id':b['memory_id'],'base_revision':1,'action':'keep'}]
        self.apply(d,self.suggestion(d,changes),changes)
        result=self.db.related_candidates({'memory_id':a['memory_id']})
        self.assertEqual(result['items'][0]['match_reasons'],['已有明确关联'])
        self.assertEqual(self.db.memory_relations(a['memory_id'])[0]['direction'],'outgoing')
        self.assertEqual(self.db.memory_relations(b['memory_id'])[0]['direction'],'incoming')

if __name__=='__main__':unittest.main()
