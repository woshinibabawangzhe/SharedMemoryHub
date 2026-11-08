import sys,time,uuid,threading,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_safety_030 import Safety,EmbeddingFixture,VectorFixture,AuthManager,HubError,create_hub_app,TestClient,unittest


class Management(Safety):
    def setUp(self):
        super().setUp()
        self.ws=self.root/'workspace';self.ws.mkdir()
        self.db.register_project_binding('synthetic',self.ws)
    def agent(self,**changes):
        p={'request_key':uuid.uuid4().hex,'scope':'project','workspace_path':str(self.ws),'title':'明确偏好','content':'偏好简体中文回复',
            'kind':'preference','source':{'origin_type':'user_explicit','session_id':'session-030','evidence_ref':'合成用户明确原话'}}
        if changes.get('operation','create') != 'create':
            p={k:v for k,v in p.items() if k in ('request_key','workspace_path','source')}
        p.update(changes)
        return self.db.write_memory('agent_client','codex','codex',p)
    def row(self,m):return self.db._admin_row(m['memory_id'])
    def test_preference_and_global_approval(self):
        m=self.agent();self.assertEqual(m['effective_confidence'],'confirmed')
        result=self.db.search_memories('agent_client','antigravity','antigravity','简体中文',workspace_path=str(self.ws))
        self.assertIn(m['memory_id'],[x['memory_id'] for x in result['items']])
        for changes in ({'scope':'user_global'},{'kind':'rule_candidate'}):
            m=self.agent(**changes);self.assertEqual(m['current_state'],'paused')
            self.db.admin_resolve_conflict(m['memory_id'],m['proposal_id'],'accept',1)
            self.assertEqual(self.row(m)['state'],'active')
    def test_inference_stays_hypothesis(self):
        m=self.agent(source={'origin_type':'model_inference'});self.assertEqual(m['effective_confidence'],'hypothesis')
    def test_missing_session_cannot_inherit_write_authority(self):
        m=self.agent()
        r=self.db.write_memory('agent_client','codex','codex',{'request_key':uuid.uuid4().hex,'operation':'update','memory_id':m['memory_id'],
            'expected_revision':1,'workspace_path':str(self.ws),'content':'没有新会话证据的更改'})
        self.assertEqual(r['status'],'queued_as_proposal')
        self.assertEqual(self.row(m)['content'],'偏好简体中文回复')
    def test_handoff_omission_and_null_ttl(self):
        m=self.agent(kind='handoff');expiry=self.row(m)['expires_at']
        for extra in ({},{'expires_at':None}):
            rev=self.row(m)['revision']
            result=self.agent(operation='update',memory_id=m['memory_id'],expected_revision=rev,content='更新的合成交接',**extra)
            self.assertEqual(result['status'],'committed')
            self.assertEqual(self.row(m)['revision'],rev+1)
            self.assertIsNotNone(self.row(m)['expires_at']);self.assertEqual(self.row(m)['kind'],'handoff')
    def test_metadata_undo_restore_and_stale_revision(self):
        m=self.agent();mid=m['memory_id']
        payload={'request_key':uuid.uuid4().hex,'operation':'update','memory_id':mid,'expected_revision':1,'scope':'user_global','subject':'user',
            'readers':['codex'],'host_constraints':['codex'],'content':'人工编辑正文'}
        self.db.write_memory('user_admin','user','web_ui',payload)
        self.assertEqual(self.row(m)['scope'],'user_global')
        self.db.admin_undo(mid,2);r=self.row(m)
        self.assertEqual((r['scope'],r['subject'],r['content']),('project','project','偏好简体中文回复'))
        self.assertIn('antigravity',json.loads(r['readers_json']))
        self.db.admin_restore(mid,2,3);r=self.row(m)
        self.assertEqual((r['scope'],r['content']),('user_global','人工编辑正文'))
        with self.assertRaises(HubError):self.db.admin_set_lock(mid,False,3)
    def test_lock_delete_undo_and_resolution_undo(self):
        m=self.agent();mid=m['memory_id']
        self.db.admin_set_lock(mid,True,1);self.db.admin_undo(mid,2);self.assertFalse(self.row(m)['locked'])
        self.db.admin_set_state(mid,'deleted',3);self.db.admin_undo(mid,4);self.assertEqual(self.row(m)['state'],'active')
        g=self.agent(scope='user_global');self.db.admin_resolve_conflict(g['memory_id'],g['proposal_id'],'accept',1)
        self.db.admin_undo(g['memory_id'],2);self.assertEqual(self.row(g)['state'],'paused')
        self.assertTrue(self.row(g)['has_open_conflict'])
    def test_manual_merge_validates(self):
        m=self.agent(scope='user_global')
        with self.assertRaises(HubError):self.db.admin_resolve_conflict(m['memory_id'],m['proposal_id'],'merge',1,final_title='x'*121,final_content='合成正文')
        self.db.admin_resolve_conflict(m['memory_id'],m['proposal_id'],'merge',1,final_title='合并',final_content='合并后的合成正文')
        self.assertEqual(self.row(m)['state'],'active')
    def test_event_loop_responsive(self):
        auth=AuthManager(self.root/'config/client_tokens.json',self.root/'config/admin_auth.json',initial_passcode='Synthetic-fixture-030',allowed_origin='http://testserver')
        embed=EmbeddingFixture()
        started=threading.Event()
        def slow(*a,**kw):started.set();time.sleep(.7);return [1.,0.]
        embed.encode_one=slow
        app=create_hub_app(self.db,auth,embed,self.vec,lock_path=self.root/'lock',auto_start_worker=False)
        with TestClient(app) as c:
            headers={'Authorization':'Bearer '+auth.get_client_tokens()['CODEX_CLIENT_TOKEN']}
            thread=threading.Thread(target=lambda:c.post('/v1/memory/search',json={'query':'合成查询','workspace_path':str(self.ws)},headers=headers));thread.start()
            self.assertTrue(started.wait(5));t=time.monotonic()
            self.assertEqual(c.get('/v1/health').status_code,200);elapsed=time.monotonic()-t
            thread.join(5);self.assertLess(elapsed,.4)
    def test_dom_sinks_absent(self):
        root=Path(__file__).resolve().parents[1]
        js=(root/'hub/ui.js').read_text(encoding='utf-8')
        for unsafe in ('innerHTML','outerHTML','insertAdjacentHTML','document.write','eval('):self.assertNotIn(unsafe,js)

if __name__=='__main__':unittest.main(verbosity=2)
