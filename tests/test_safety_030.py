"""隔离确定性回归；替身向量结果不代表 Docker/CUDA 验收。"""
import os
import sys
import json
import sqlite3
import threading
import time
import unittest
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hub.db import MemoryDatabase,HubError
from hub.auth import AuthManager
from hub.app import create_hub_app
from hub.worker import SerialIndexWorker,SingleInstanceFileLock
from fastapi.testclient import TestClient


class EmbeddingFixture:
    wedged=False;loaded=True
    def encode_one(self,*a,**kw): return [1.,0.]
    def cosine_similarity(self,*a,**kw): return 0.5


class VectorFixture:
    def __init__(self): self.points={};self.offline=False;self.on_upsert=None
    def delete_point(self,g,mid):
        if self.offline:return False
        self.points.pop((g,mid),None);return True
    def upsert_point(self,g,mid,r,v):
        if self.on_upsert:self.on_upsert()
        self.points[g,mid]=r;return True
    def is_healthy(self):return not self.offline
    def ensure_collection(self,*a,**kw):return True
    def drop_collection(self,*a):return True
    def close(self):pass
    def search_points(self,*a,**kw):return []


class Safety(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'receipts/v0.3.0-implementation/synthetic'/uuid.uuid4().hex
        self.db=MemoryDatabase(self.root/'data/sqlite/memory_hub.db',self.root/'data/tombstones/purge_registry.jsonl',self.root/'exports',self.root/'backups')
        self.vec=VectorFixture()
    def create(self):
        return self.db.write_memory('user_admin','user','web_ui',{'request_key':uuid.uuid4().hex,'scope':'user_global','title':'合成清除标题','content':'SYNTHETIC_CANARY_'+uuid.uuid4().hex,'source':{},'operation':'create'})
    def purge(self,m,**kw):
        return self.db.admin_purge(m['memory_id'],f"PURGE {m['memory_id']} r1",1,self.vec.delete_point if False else lambda mid:self.vec.delete_point('v1',mid),**kw)
    def test_pending_recovery_and_no_replay(self):
        m=self.create();mid=m['memory_id'];self.vec.points['v1',mid]=1;self.vec.offline=True
        self.assertEqual(self.purge(m)['status'],'purge_pending')
        with self.assertRaises(HubError) as error:self.db.get_memory(mid,'user_admin','user','web_ui')
        self.assertIn(error.exception.status_code,(404,410))
        self.vec.offline=False
        self.db.verify_and_reconcile_purge_registry(lambda x:self.vec.delete_point('v1',x))
        self.assertNotIn(('v1',mid),self.vec.points)
        con=self.db.connect();r=con.execute('SELECT * FROM purge_tombstones').fetchone();con.close()
        self.assertTrue(all(r[k] for k in ('sqlite_done','vector_done','exports_done','compact_done')))
    def test_confirmation_revision_required(self):
        m=self.create()
        for confirm,rev in ((True,1),('bad',1),(f"PURGE {m['memory_id']} r1",None)):
            with self.assertRaises(HubError):self.db.admin_purge(m['memory_id'],confirm,rev)
    def test_missing_tombstone_after_crash_fails_closed(self):
        m=self.create()
        with self.assertRaises(RuntimeError):self.purge(m,simulate_crash_after_tombstone_file=True)
        self.db.tombstone_path.unlink() # owned synthetic fixture only
        with self.assertRaises(HubError):MemoryDatabase(self.db.db_path,self.db.tombstone_path,self.db.exports_dir,self.db.backups_dir)
    def test_stale_upsert_compensated(self):
        m=self.create();mid=m['memory_id']
        self.vec.on_upsert=lambda:self.purge(m)
        worker=SerialIndexWorker(self.db,EmbeddingFixture(),self.vec)
        res=worker.run_once()
        self.assertEqual(res['status'],'superseded_compensated')
        self.assertNotIn(('v1',mid),self.vec.points)
    def test_compensation_failure_stays_pending(self):
        m=self.create();mid=m['memory_id']
        def interleave():
            self.purge(m)
            self.vec.offline=True
        self.vec.on_upsert=interleave
        worker=SerialIndexWorker(self.db,EmbeddingFixture(),self.vec)
        worker.run_once()
        con=self.db.connect()
        try:self.assertEqual(con.execute('SELECT vector_done FROM purge_tombstones WHERE memory_id=?',(mid,)).fetchone()[0],0)
        finally:con.close()
        self.vec.offline=False
        self.db.retry_pending_purges(lambda x:self.vec.delete_point('v1',x))
        self.assertNotIn(('v1',mid),self.vec.points)
    def test_schema_explicit(self):
        con=self.db.connect();con.execute('PRAGMA user_version=0');con.close()
        with self.assertRaises(HubError) as e:MemoryDatabase(self.db.db_path,self.db.tombstone_path,self.db.exports_dir,self.db.backups_dir)
        self.assertEqual(e.exception.error_code,'migration_required')
        from scripts.migrate_schema import migrate
        self.assertEqual(migrate(self.root)['status'],'migrated')
    def test_auth_origin_and_lifecycle_lock(self):
        auth=AuthManager(self.root/'config/client_tokens.json',self.root/'config/admin_auth.json',initial_passcode='Synthetic-fixture-030',allowed_origin='http://testserver')
        sess=auth.create_admin_session('Synthetic-fixture-030')
        for origin in (None,'http://localhost:8765','http://testserver/'):
            with self.assertRaises(HubError):auth.require_admin_session('POST',session_header=sess['session_id'],csrf_header=sess['csrf_token'],origin_header=origin)
        self.assertEqual(auth.require_admin_session('POST',session_header=sess['session_id'],csrf_header=sess['csrf_token'],origin_header='http://testserver').actor_role,'user_admin')
        lock=self.root/'data/hub.lock'
        app=create_hub_app(self.db,auth,EmbeddingFixture(),self.vec,lock_path=lock,auto_start_worker=False)
        with TestClient(app) as client:
            self.assertEqual(client.get('/v1/health').status_code,200)
            with self.assertRaises(RuntimeError):SingleInstanceFileLock(lock).acquire()
        with SingleInstanceFileLock(lock):pass

if __name__=='__main__':unittest.main(verbosity=2)
