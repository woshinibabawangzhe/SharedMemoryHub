import contextlib,http.server,json,os,pathlib,subprocess,sys,threading,time,uuid,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_safety_030 import Safety,AuthManager,EmbeddingFixture,create_hub_app,TestClient
from clients.memory_cli import call_hub
import httpx
from unittest.mock import patch


class Clients(Safety):
    def test_admin_setup_hash_and_preservation(self):
        tokens=self.root/'config/client_tokens.json';admin=self.root/'config/admin_auth.json'
        with self.assertRaises(Exception):AuthManager(tokens,admin)
        self.assertFalse(admin.exists())
        AuthManager(tokens,admin,initial_passcode='Synthetic-fixture-030')
        before=admin.read_bytes()
        AuthManager(tokens,admin,initial_passcode='Another-synthetic-value')
        self.assertEqual(before,admin.read_bytes())
        self.assertNotIn(b'Synthetic-fixture-030',before)
        self.assertEqual(json.loads(before)['algorithm'],'scrypt')
        self.assertFalse(any('ADMIN' in key for key in json.loads(tokens.read_text())))
    def test_inference_timeout_guard(self):
        from hub.embedder import EmbeddingEngine
        engine=EmbeddingEngine(device='cpu');release=threading.Event()
        def blocked(*args):
            with engine._infer_lock:
                release.wait(2)
                return [[1.,0.]]
        engine._raw_encode_locked=blocked
        try:
            with self.assertRaises(TimeoutError):engine.encode(['synthetic'],timeout_sec=.05)
            self.assertTrue(engine.wedged)
            start=time.monotonic()
            with self.assertRaises(TimeoutError):engine.encode(['synthetic'],timeout_sec=.05)
            self.assertLess(time.monotonic()-start,.05)
        finally:release.set();engine._executor.shutdown(wait=True)
    def test_timeout_keeps_key(self):
        tokens=self.root/'tokens.json';tokens.write_text(json.dumps({'CODEX_CLIENT_TOKEN':'synthetic-http-client'}))
        with patch('httpx.Client.request',side_effect=httpx.ReadTimeout('synthetic timeout')):
            result=call_hub('POST','/v1/memory/write','codex',{'request_key':'same-key'},tokens_path=tokens)
        self.assertEqual(result['status'],'WRITE_RESULT_UNKNOWN');self.assertEqual(result['request_key'],'same-key')
    def test_cli_http_error_exit(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(422);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"status":"error"}')
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        tokens=self.root/'tokens.json';tokens.write_text(json.dumps({'CODEX_CLIENT_TOKEN':'synthetic-http-client'}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'clients/memory_cli.py'),'--client','codex','--tokens-path',str(tokens),'--base-url',f'http://127.0.0.1:{server.server_port}',
                'write','--op','create','--request-key','synthetic-key','--kind','context','--scope','user_global','--title','合成','--content','合成正文'],capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,2,result.stderr);self.assertEqual(json.loads(result.stdout)['http_status'],422)
        finally:server.shutdown();server.server_close();thread.join(3)
    def test_http_purge_pending(self):
        auth=AuthManager(self.root/'config/client_tokens.json',self.root/'config/admin_auth.json',initial_passcode='Synthetic-fixture-030',allowed_origin='http://testserver')
        app=create_hub_app(self.db,auth,EmbeddingFixture(),self.vec,lock_path=self.root/'hub.lock',auto_start_worker=False)
        m=self.create();self.vec.offline=True
        with TestClient(app) as c:
            login=c.post('/v1/admin/login',headers={'Origin':'http://testserver'},json={'passcode':'Synthetic-fixture-030'}).json()
            hdr={'Origin':'http://testserver','X-CSRF-Token':login['csrf_token']}
            result=c.post('/v1/admin/memory/'+m['memory_id']+'/purge',headers=hdr,json={'expected_revision':1,'confirm_purge':f"PURGE {m['memory_id']} r1"})
            self.assertEqual(result.status_code,202);self.assertEqual(result.json()['status'],'purge_pending')
    def test_import_zero_runtime_side_effects(self):
        target=self.root/'not-created-by-import'
        env=os.environ.copy();env['SMH_RUNTIME_ROOT']=str(target)
        result=subprocess.run([sys.executable,'-B','-c','import hub.app'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(target.exists())
    def test_background_worker_consumes(self):
        auth=AuthManager(self.root/'config/client_tokens.json',self.root/'config/admin_auth.json',initial_passcode='Synthetic-fixture-030',allowed_origin='http://testserver')
        m=self.create();mid=m['memory_id']
        with TestClient(create_hub_app(self.db,auth,EmbeddingFixture(),self.vec,lock_path=self.root/'hub.lock')):
            for _ in range(30):
                if self.db._admin_row(mid)['indexed_revision']==1:break
                time.sleep(.1)
            self.assertEqual(self.db._admin_row(mid)['indexed_revision'],1)

if __name__=='__main__':unittest.main(verbosity=2)
