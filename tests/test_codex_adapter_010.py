"""Codex 接入确定性回归；仅独立合成实例，不加载真实凭据。"""
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = Path(os.environ['SMH_CODEX_ADAPTER']) if os.environ.get('SMH_CODEX_ADAPTER') else None
# Optional host adapter: never discover or create personal control assets.
if ADAPTER is None or not ADAPTER.is_file():
    raise unittest.SkipTest('未显式配置可用的 SMH_CODEX_ADAPTER；可选宿主适配测试未运行。')
spec = importlib.util.spec_from_file_location('codex_memory_adapter', ADAPTER)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
TEST_ROOT = ROOT / 'receipts' / ('adapter-unit-' + uuid.uuid4().hex[:12])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.root = TEST_ROOT / uuid.uuid4().hex[:8]
        (self.root / 'config').mkdir(parents=True)
        (self.root / 'workspace').mkdir()
        (self.root / 'config/client_tokens.json').write_text(json.dumps({'CODEX_CLIENT_TOKEN': 'synthetic-codex-only', 'AGY_CLIENT_TOKEN': 'synthetic-other-only'}))
        self.calls, self.created = [], {}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_doc({'status':'ok','content':'full synthetic content','revision':2})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path, body, self.headers.get('Authorization')))
                if self.path.endswith('/write'):
                    key = body['request_key']
                    owner.created.setdefault(key, {'status':'committed','memory_id':'mem_synthetic','stored_revision':1})
                    self.send_doc(owner.created[key])
                else:
                    self.send_doc({'status':'ok','bound_project_id':'synthetic','count':8,'items':[
                        {'memory_id':f'mem_{n}','title':'测试','revision':2,'scope':'project','locked':True,'content':'文'*400,
                         'source':{'origin_type':'user_explicit','session_id':'not-for-brief','evidence_ref':'not-for-brief'},'authority_notice':'historical'} for n in range(8)]})
            def send_doc(self, doc):
                data = json.dumps(doc, ensure_ascii=False).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.profile = self.root / 'profile.json'
        self.profile.write_text(json.dumps({'enabled':True,'runtime_root':str(self.root),'base_url':f'http://127.0.0.1:{self.server.server_port}'}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def invoke(self, *args, body=None, session='synthetic-session-a'):
        output = io.StringIO()
        with patch.dict(os.environ, {'CODEX_THREAD_ID':session}), patch('sys.stdin',io.StringIO(json.dumps(body or {}, ensure_ascii=False))), contextlib.redirect_stdout(output):
            code = adapter.main(['--profile',str(self.profile),'--workspace',str(self.root/'workspace'),*args])
        return code, json.loads(output.getvalue())

    def request(self, key='synthetic-key-a'):
        return {'request_key':key,'operation':'create','subject':'project','scope':'project','kind':'preference','title':'合成偏好','content':'这是明确确认的合成测试偏好。','source':{'origin_type':'user_explicit','evidence_ref':'请记住这条合成偏好'}}

    def test_identity_workspace_progressive_and_context_precheck(self):
        code, result = self.invoke('precheck','--query','合成')
        self.assertEqual(code,0); self.assertEqual(len(result['items']),5)
        self.assertTrue(all(len(i['excerpt'])==200 and 'content' not in i and 'evidence_ref' not in i['source'] for i in result['items']))
        self.assertIn('context',self.calls[0][1]['kinds']); self.assertEqual(self.calls[0][2],'Bearer synthetic-codex-only')
        self.assertEqual(Path(self.calls[0][1]['workspace_path']),self.root/'workspace')
        self.assertFalse((self.root/'client-state').exists())

    def test_get_retains_full_text(self):
        code, result = self.invoke('get','--id','mem_1')
        self.assertEqual(code,0); self.assertEqual(result['content'],'full synthetic content')

    def test_missing_session_rejects_before_send_or_journal(self):
        code, result = self.invoke('write',body=self.request(),session='')
        self.assertEqual(code,2); self.assertEqual(result['status'],'SESSION_ID_REQUIRED')
        self.assertEqual(len(self.calls),0); self.assertFalse((self.root/'client-state').exists())

    def test_write_and_same_key_body_protection(self):
        body = self.request()
        code, result = self.invoke('write',body=body)
        self.assertEqual(code,0); self.assertEqual(self.calls[0][1]['source']['session_id'],'synthetic-session-a')
        changed = {**body,'content':'篡改后的正文'}
        code, result = self.invoke('write',body=changed)
        self.assertEqual(code,2); self.assertEqual(result['status'],'REQUEST_BODY_MISMATCH'); self.assertEqual(len(self.calls),1)

    def test_unknown_write_preserved_and_explicit_reconciliation(self):
        original = adapter.load_transport
        def broken_transport():
            transport = original()
            def unknown(*args,**kwargs):
                self.created[kwargs['payload']['request_key']]={'status':'committed','memory_id':'mem_synthetic','stored_revision':1}
                return {'status':'WRITE_RESULT_UNKNOWN','http_status':503,'request_key':kwargs['payload']['request_key']}
            transport.call_hub = unknown
            return transport
        with patch.object(adapter,'load_transport',broken_transport):
            code, result = self.invoke('write',body=self.request())
        self.assertEqual(code,2); self.assertEqual(result['status'],'WRITE_RESULT_UNKNOWN')
        record = json.loads(Path(result['request_record']).read_text(encoding='utf-8'))
        self.assertEqual(record['status'],'WRITE_RESULT_UNKNOWN'); self.assertEqual(record['request']['request_key'],'synthetic-key-a')
        code, result = self.invoke('reconcile','--request-key','synthetic-key-a')
        self.assertEqual(result['status'],'REPLAY_CONFIRMATION_REQUIRED'); self.assertFalse(self.calls)
        code, result = self.invoke('reconcile','--request-key','synthetic-key-a','--confirm-same-request',session='synthetic-session-b')
        self.assertEqual(code,0); self.assertEqual(len(self.created),1)
        self.assertEqual(self.calls[0][1]['source']['session_id'],'synthetic-session-a')

    def test_sensitive_candidate_never_persisted(self):
        body = self.request(); body['content']='sk-'+'syntheticshape'*4
        code, result = self.invoke('write',body=body)
        self.assertEqual(code,2); self.assertEqual(result['status'],'CONTENT_REJECTED')
        self.assertFalse((self.root/'client-state').exists()); self.assertFalse(self.calls)

    def test_malformed_write_reply_remains_unknown(self):
        original = adapter.load_transport
        def malformed():
            transport = original()
            transport.call_hub = lambda *a, **k: {'raw_text':'invalid response','http_status':200}
            return transport
        with patch.object(adapter,'load_transport',malformed):
            code,result=self.invoke('write',body=self.request())
        self.assertEqual(code,2);self.assertEqual(result['status'],'WRITE_RESULT_UNKNOWN')
        self.assertEqual(json.loads(Path(result['request_record']).read_text(encoding='utf-8'))['status'],'WRITE_RESULT_UNKNOWN')

    def test_no_workspace_does_not_guess_project(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=adapter.main(['--profile',str(self.profile),'--no-workspace','search','--query','合成'])
        self.assertEqual(code,0); self.assertIsNone(self.calls[0][1]['workspace_path'])

    def test_external_endpoint_rejected(self):
        self.profile.write_text(json.dumps({'enabled':True,'runtime_root':str(self.root),'base_url':'https://example.com'}))
        code, result=self.invoke('health')
        self.assertEqual(code,2); self.assertEqual(result['status'],'INVALID_LOCAL_ENDPOINT'); self.assertFalse(self.calls)


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(AdapterTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    TEST_ROOT.mkdir(parents=True,exist_ok=True)
    (TEST_ROOT/'receipt.json').write_text(json.dumps({'evidence_level':'simulated','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'adapter_sha256':hashlib.sha256(ADAPTER.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
    print(TEST_ROOT)
    raise SystemExit(not result.wasSuccessful())
