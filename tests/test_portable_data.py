"""便携数据的确定性回归；真实Docker/CUDA恢复由独立流程验证。"""
import json,sys,unittest,uuid
from pathlib import Path
ROOT=Path(__file__).absolute().parents[1];sys.path.insert(0,str(ROOT))
from hub.db import MemoryDatabase,HubError
from hub.worker import SingleInstanceFileLock
from scripts.portable_data import backup,restore,verify_bundle

class PortableDataTests(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'receipts'/('portable-unit-'+uuid.uuid4().hex)
        self.runtime=self.root/'source'
        self.db=MemoryDatabase(self.runtime/'data/sqlite/memory_hub.db',self.runtime/'data/tombstones/purge_registry.jsonl',self.runtime/'exports',self.runtime/'backups')
        self.keep=self.create('合成记忆保留正文')
        self.gone=self.create('合成记忆清除正文')
        mid=self.gone['memory_id']
        self.db.admin_purge(mid,f'PURGE {mid} r1',1,lambda _mid:True)
    def create(self,content):
        return self.db.write_memory('user_admin','user','web_ui',{'request_key':uuid.uuid4().hex,'operation':'create','scope':'user_global','title':'合成便携测试','content':content,'source':{}})
    def test_history_purge_and_index_watermark_survive(self):
        result=backup(self.runtime,self.root/'bundle')
        self.assertEqual(result['max_purge_seq'],1)
        recovered=restore(self.root/'bundle',self.root/'restored',minimum_purge_seq=1)
        self.assertEqual(result['counts'],recovered['counts'])
        db=MemoryDatabase(self.root/'restored/data/sqlite/memory_hub.db',self.root/'restored/data/tombstones/purge_registry.jsonl',self.root/'restored/exports',self.root/'restored/backups')
        item=db.get_memory(self.keep['memory_id'],'user_admin','user','web_ui')['item']
        self.assertEqual(item['content'],'合成记忆保留正文')
        self.assertEqual(item['revision'],1)
        with self.assertRaises(HubError) as error:db.get_memory(self.gone['memory_id'],'user_admin','user','web_ui')
        self.assertEqual(error.exception.status_code,410)
        self.assertFalse((self.root/'restored/config').exists())
    def test_live_lock_and_existing_target_refused(self):
        with SingleInstanceFileLock(self.runtime/'data/hub.lock'):
            with self.assertRaises(RuntimeError):backup(self.runtime,self.root/'locked-bundle')
        self.assertFalse((self.root/'locked-bundle').exists())
        backup(self.runtime,self.root/'bundle')
        with self.assertRaises(FileExistsError):backup(self.runtime,self.root/'bundle')
        restore(self.root/'bundle',self.root/'restored',minimum_purge_seq=1)
        with self.assertRaises(FileExistsError):restore(self.root/'bundle',self.root/'restored',minimum_purge_seq=1)
    def test_tamper_and_missing_tombstone_refused(self):
        backup(self.runtime,self.root/'bundle')
        with (self.root/'bundle/purge_registry.jsonl').open('ab') as stream:stream.write(b'bad')
        with self.assertRaises(RuntimeError):verify_bundle(self.root/'bundle')
        self.assertFalse((self.root/'not-created').exists())
        with self.assertRaises(RuntimeError):restore(self.root/'bundle',self.root/'not-created',minimum_purge_seq=1)
        self.assertFalse((self.root/'not-created').exists())
    def test_later_purge_replayed_on_older_backup(self):
        backup(self.runtime,self.root/'older-bundle')
        mid=self.keep['memory_id'];self.db.admin_purge(mid,f'PURGE {mid} r1',1,lambda _mid:True)
        with self.assertRaises(HubError):restore(self.root/'older-bundle',self.root/'rejected-stale',minimum_purge_seq=2)
        restore(self.root/'older-bundle',self.root/'latest-restored',minimum_purge_seq=2,latest_tombstone=self.db.tombstone_path)
        db=MemoryDatabase(self.root/'latest-restored/data/sqlite/memory_hub.db',self.root/'latest-restored/data/tombstones/purge_registry.jsonl',self.root/'latest-restored/exports',self.root/'latest-restored/backups')
        with self.assertRaises(HubError) as error:db.get_memory(mid,'user_admin','user','web_ui')
        self.assertEqual(error.exception.status_code,410)

if __name__=='__main__':unittest.main()
