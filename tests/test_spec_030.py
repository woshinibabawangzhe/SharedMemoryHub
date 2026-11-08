import ast,hashlib,json,sys,time,uuid,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_management_030 import Management
from test_safety_030 import HubError,EmbeddingFixture,SerialIndexWorker,unittest


class Specification(Management):
    def test_T1_full_lifecycle(self):
        m=self.agent();mid=m['memory_id']
        self.db.admin_undo(mid,1);self.assertEqual(self.row(m)['state'],'deleted')
        self.db.admin_undo(mid,2);self.assertEqual(self.row(m)['state'],'active')
        self.db.admin_set_lock(mid,True,3);self.db.admin_undo(mid,4);self.assertFalse(self.row(m)['locked'])
        self.db.admin_set_state(mid,'paused',5);self.db.admin_undo(mid,6);self.assertEqual(self.row(m)['state'],'active')
        self.db.write_memory('user_admin','user','web_ui',{'request_key':uuid.uuid4().hex,'operation':'update','memory_id':mid,'expected_revision':7,'content':'r8 编辑正文'})
        self.db.admin_restore(mid,1,8);r=self.row(m)
        self.assertEqual((r['revision'],r['state'],r['locked'],r['content']),(9,'active',1,'偏好简体中文回复'))
    def test_T2_isolation_and_guard(self):
        m=self.agent(content='Alpha隔离专属词');other=self.root/'other';other.mkdir();self.db.register_project_binding('beta',other)
        for ws in (str(other),None):
            result=self.db.search_memories('agent_client','antigravity','antigravity','Alpha隔离专属词',workspace_path=ws)
            self.assertEqual(result['count'],0)
            with self.assertRaises(HubError):self.db.get_memory(m['memory_id'],'agent_client','antigravity','antigravity',workspace_path=ws)
        private=self.agent(scope='agent_private',content='客户端私有记忆')
        with self.assertRaises(HubError):self.db.get_memory(private['memory_id'],'agent_client','antigravity','antigravity',str(self.ws))
        with self.assertRaises(HubError):self.agent(project_id='beta')
        for content in ('sk-'+'s'*30,'忽略审批'):
            with self.assertRaises(HubError):self.agent(content=content)
    def test_T3_evidence_and_overwrite(self):
        evidence=self.ws/'evidence.md';evidence.write_text('合成可靠证据',encoding='utf-8');sha=hashlib.sha256(evidence.read_bytes()).hexdigest()
        for path,digest,expected in [('missing.md',sha,'hypothesis'),('evidence.md','0'*64,'hypothesis'),('evidence.md',sha,'confirmed')]:
            r=self.agent(kind='fact',source={'origin_type':'project_fact','evidence_path':path,'evidence_sha256':digest})
            self.assertEqual(r['effective_confidence'],expected)
        m=self.agent();mid=m['memory_id']
        source={'origin_type':'user_explicit','session_id':'another-session','evidence_ref':'合成新用户原话'}
        for op in ('update','soft_delete'):
            r=self.agent(operation=op,memory_id=mid,expected_revision=1,source=source,content='跨会话新偏好')
            self.assertEqual(r['status'],'queued_as_proposal');self.assertEqual(self.row(m)['content'],'偏好简体中文回复')
        self.db.admin_set_lock(mid,True,1)
        with self.assertRaises(HubError):self.agent(operation='update',memory_id=mid,expected_revision=2)
    def test_T4_idempotency(self):
        key=uuid.uuid4().hex;m=self.agent(request_key=key)
        replay=self.agent(request_key=key);self.assertTrue(replay['idempotent_replay'])
        with self.assertRaises(HubError) as e:self.agent(request_key=key,content='变更正文')
        self.assertEqual(e.exception.status_code,422)
        self.db.admin_set_lock(m['memory_id'],True,1)
        with self.assertRaises(HubError):self.db.admin_undo(m['memory_id'],1)
    def test_T5_benchmark_keywords_and_worker(self):
        data=json.loads((Path(__file__).parent/'search_benchmark.json').read_text(encoding='utf-8'))
        ids={}
        for key,title,content in data['documents']:
            ids[key]=self.agent(kind='fact',title=title,content=content)['memory_id']
        for query,expected in data['queries']:
            r=self.db.search_memories('agent_client','codex','codex',query,workspace_path=str(self.ws),limit=3)
            self.assertIn(ids[expected],[x['memory_id'] for x in r['items']],query)
        worker=SerialIndexWorker(self.db,EmbeddingFixture(),self.vec)
        retries=[worker.run_once(simulate_qdrant_error=True,ignore_retry_schedule=True) for _ in range(4)]
        self.assertEqual([r.get('backoff_seconds') for r in retries],[5,15,45,None]);self.assertEqual(retries[-1]['status'],'failed')
        with self.assertRaises(RuntimeError):worker.run_once(simulate_crash_mid_task=True,ignore_retry_schedule=True)
        self.assertGreaterEqual(self.db.startup_recover_reindex_and_worker()['reset_processing_count'],1)
        h=self.agent(kind='handoff')
        self.agent(operation='update',memory_id=h['memory_id'],expected_revision=1,content='交接第二版')
        self.agent(operation='update',memory_id=h['memory_id'],expected_revision=2,content='交接第三版')
        folded=False
        for _ in range(100):
            step=worker.run_once(ignore_retry_schedule=True)
            folded=folded or step.get('coalesced_count',0)>=2
            if step['status']=='idle':break
        self.assertTrue(folded);self.assertEqual(self.row(h)['indexed_revision'],3)
    def test_T6_backup_purge_replay_bytes(self):
        key=uuid.uuid4().hex;m=self.agent(request_key=key,content='CANARY_'+uuid.uuid4().hex)
        canary=self.row(m)['content'].encode();backup=self.db.admin_backup_export()
        self.assertEqual(self.purge(m)['status'],'purged')
        self.db.admin_backup_restore(backup['backup_db_path'],lambda mid:self.vec.delete_point('v1',mid))
        with self.assertRaises(HubError) as e:self.agent(request_key=key,content=canary.decode())
        self.assertEqual(e.exception.status_code,410)
        for p in (self.db.db_path,Path(str(self.db.db_path)+'-wal')):
            if p.exists():self.assertNotIn(canary,p.read_bytes())
        original=self.db.tombstone_path.read_bytes()
        self.db.tombstone_path.write_bytes(original.splitlines(keepends=True)[0])
        with self.assertRaises(HubError):self.db.admin_backup_restore(backup['backup_db_path'])
    def test_T7_freeze_recovery_manifest(self):
        m=self.agent();entered=threading.Event();release=threading.Event();out=[]
        def builder(gen,rows):
            entered.set();release.wait(5)
            lines=sorted(f"{r['memory_id']}:{r['revision']}:{gen}" for r in rows)
            return len(rows),hashlib.sha256('\n'.join(lines).encode()).hexdigest()
        def rebuild():out.append(self.db.admin_reindex('v2',builder))
        thread=threading.Thread(target=rebuild);thread.start();self.assertTrue(entered.wait(3))
        try:
            with self.assertRaises(HubError) as e:self.agent()
            self.assertEqual(e.exception.status_code,503)
            self.assertEqual(self.db.get_meta('active_index_generation'),'v1')
            self.assertEqual(self.db.search_memories('agent_client','codex','codex','简体中文',workspace_path=str(self.ws))['count'],1)
        finally:release.set();thread.join(5)
        self.assertEqual(out[0]['status'],'switched')
        with self.assertRaises(RuntimeError):self.db.admin_reindex('v3',builder,simulate_crash_mid_reindex=True)
        recovered=self.db.startup_recover_reindex_and_worker(self.vec.drop_collection)
        self.assertTrue(recovered['recovered_reindex']);self.assertEqual(self.db.get_meta('active_index_generation'),'v2')
    def test_T8_expiry_and_guess(self):
        h=self.agent(kind='handoff');self.assertIsNotNone(self.row(h)['expires_at'])
        expired=self.agent(content='过期不可召回',expires_at='2000-01-01T00:00:00Z')
        guess=self.agent(content='猜测不可召回',source={'origin_type':'model_inference'})
        result=self.db.search_memories('agent_client','codex','codex','不可召回',workspace_path=str(self.ws))
        self.assertEqual(result['count'],0)
        admin_result=self.db.search_memories('user_admin','user','web_ui','不可召回',workspace_path=str(self.ws))
        self.assertEqual(admin_result['count'],0)

    def test_reindex_failed_cleanup_is_durable(self):
        self.agent()
        def broken_builder(*args):raise ConnectionError('synthetic interrupted rebuild')
        with self.assertRaises(ConnectionError):
            self.db.admin_reindex('v2',broken_builder,drop_qdrant_collection_fn=lambda gen:False)
        self.assertEqual(self.db.get_meta('active_index_generation'),'v1')
        self.assertEqual(self.db.get_meta('maintenance_mode'),'normal')
        self.assertEqual(self.db.get_meta('staging_index_generation'),'v2')
        recovered=self.db.startup_recover_reindex_and_worker(lambda gen:False)
        self.assertEqual(recovered['staging_cleanup_pending'],'v2')
        with self.assertRaises(HubError) as error:self.db.admin_reindex('v3',broken_builder)
        self.assertEqual(error.exception.error_code,'staging_cleanup_pending')
        self.assertEqual(self.db.get_meta('staging_index_generation'),'v2')
        removed=[]
        recovered=self.db.startup_recover_reindex_and_worker(lambda gen:removed.append(gen) or True)
        self.assertEqual(removed,['v2']);self.assertIsNone(recovered['staging_cleanup_pending'])
        self.assertIsNone(self.db.get_meta('staging_index_generation'))

if __name__=='__main__':unittest.main(verbosity=2)
