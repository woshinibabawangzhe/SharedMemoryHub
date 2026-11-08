"""完整复制的保真、去重、永久清除选择和漂移拒绝。"""
import json,sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).absolute().parents[1];sys.path.insert(0,str(ROOT))
from scripts.import_codex_memory import prepare,verify,send

class FullMemoryCopy(unittest.TestCase):
    def setUp(self):
        base=ROOT/'receipts'/'full-copy-synthetic';base.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.tmp.name);self.source=self.root/'source'
        (self.source/'personal-context/canonical').mkdir(parents=True);(self.source/'memories/.git').mkdir(parents=True)
        (self.source/'memories/.git/config').write_text('ignored metadata',encoding='utf-8')
        (self.source/'memories/MEMORY.md').write_text('Historical snapshot',encoding='utf-8')
        self.raw=b'---\r\nid: decision_preferences\r\n---\r\n\r\n# Preferences\r\n- Existing preference\r\n- Deleted preference\r\n- New preference\r\n'
        self.file=self.source/'personal-context/canonical/decision-preferences.md';self.file.write_bytes(self.raw)
        cards=[];imported=[]
        for n in range(1,9):
            cards.append({'number':f'{n:02}','request':{'content':'Deleted preference' if n==8 else 'Existing preference' if n==1 else f'other {n}'}})
            imported.append({'number':f'{n:02}','memory_id':f'mem_synthetic_{n}'})
        self.trial=self.root/'trial.json';self.trial.write_text(json.dumps({'cards':cards,'imported':imported}),encoding='utf-8')
    def tearDown(self):self.tmp.cleanup()
    def test_bytes_reuse_and_purged_suppression(self):
        batch=self.root/'batch';m=prepare(self.source,batch,self.trial)
        row=next(r for r in m['source_files'] if r['relative_path'].endswith('decision-preferences.md'))
        self.assertEqual(Path(row['snapshot']).read_bytes(),self.raw)
        self.assertEqual(row['reused_memory_ids'],['mem_synthetic_1']);self.assertEqual(row['suppressed_purged_ids'],['mem_synthetic_8'])
        req=json.loads(Path(m['requests'][0]['request_file']).read_text(encoding='utf-8'))
        self.assertIn('New preference',req['content']);self.assertNotIn('Deleted preference',req['content']);self.assertNotIn('Existing preference',req['content'])
        self.assertEqual(verify(batch)['raw_files'],2);self.assertTrue(self.file.read_bytes()==self.raw)
        with self.assertRaises(FileExistsError):prepare(self.source,batch,self.trial)
    def test_changed_original_invalidates_coverage(self):
        batch=self.root/'batch';prepare(self.source,batch,self.trial);self.file.write_text('changed',encoding='utf-8')
        with self.assertRaises(AssertionError):verify(batch)
    def test_unknown_source_type_fails_before_copy(self):
        (self.source/'memories/unknown.env').write_text('no credential; unknown file type',encoding='utf-8')
        with self.assertRaises(ValueError):prepare(self.source,self.root/'batch',self.trial)
        self.assertFalse((self.root/'batch').exists())
    def test_pending_response_is_not_resent(self):
        batch=self.root/'batch';prepare(self.source,batch,self.trial);writes=[]
        def execute(args):
            if args.command=='get':return {'http_status':410}
            if args.command=='precheck':return {'status':'ok','items':[]}
            writes.append(args.request_file)
            return {'status':'paused_pending_approval','http_status':200,'memory_id':'mem_synthetic'}
        adapter=SimpleNamespace(DEFAULT_PROFILE=self.root/'profile.json',execute=execute)
        with patch('scripts.import_codex_memory.load_adapter',return_value=adapter),patch.dict('os.environ',{'CODEX_THREAD_ID':'synthetic-test-thread'}):
            send(batch,self.root/'adapter.py');count=len(writes);send(batch,self.root/'adapter.py')
        self.assertEqual(count,2);self.assertEqual(len(writes),2)
    def test_unknown_write_freezes_batch(self):
        batch=self.root/'batch';prepare(self.source,batch,self.trial);writes=[]
        def execute(args):
            if args.command=='get':return {'http_status':410}
            if args.command=='precheck':return {'status':'ok','items':[]}
            writes.append(args.request_file);return {'status':'WRITE_RESULT_UNKNOWN','http_status':503}
        adapter=SimpleNamespace(DEFAULT_PROFILE=self.root/'profile.json',execute=execute)
        with patch('scripts.import_codex_memory.load_adapter',return_value=adapter),patch.dict('os.environ',{'CODEX_THREAD_ID':'synthetic-test-thread'}):
            with self.assertRaises(RuntimeError):send(batch,self.root/'adapter.py')
            with self.assertRaises(RuntimeError):send(batch,self.root/'adapter.py')
        self.assertEqual(len(writes),1)

if __name__=='__main__':unittest.main()
