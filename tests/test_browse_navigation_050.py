"""Navigation counts must describe the real browse set without changing memories."""
import hashlib
import json
import unittest
import uuid
from pathlib import Path
from hub.db import MemoryDatabase

ROOT=Path(__file__).resolve().parents[1]

class NavigationCounts(unittest.TestCase):
    def setUp(self):
        root=ROOT/'receipts/browse-050-20261109/synthetic'/uuid.uuid4().hex
        self.db=MemoryDatabase(root/'data/sqlite/memory_hub.db',root/'data/tombstones/purge_registry.jsonl',root/'exports',root/'backups')

    def create(self,domain=None,**extra):
        p={'operation':'create','request_key':uuid.uuid4().hex,'scope':'user_global','subject':'user','kind':'context','title':uuid.uuid4().hex,'content':'合成正文不应出现在目录统计中','domain_id':domain,'source':{'origin_type':'admin_manual'}}
        p.update(extra)
        return self.db.write_memory('user_admin','user','web_ui',p)

    def test_counts_match_parent_pending_deleted_and_review(self):
        child=self.db.workbench_domain({'name':'合成设备','parent_id':'tools'})['domain_id']
        self.create(child,review_due_at='2026-01-01')
        self.create('tools')
        deleted=self.create('personal');self.db.admin_set_state(deleted['memory_id'],'deleted',1)
        self.create(None)
        p={'operation':'create','request_key':uuid.uuid4().hex,'scope':'user_global','subject':'user','title':'合成待批偏好','content':'只在本次合成实例使用','domain_id':'tools','source':{'origin_type':'user_explicit','session_id':'fixture-050','evidence_ref':'合成原话'}}
        self.db.write_memory('agent_client','codex','codex',p)
        catalog=self.db.workbench_catalog();c=catalog['navigation_counts']
        self.assertEqual(c['library']['total'],4)
        self.assertEqual(c['library']['domains']['tools'],3)
        self.assertEqual(c['library']['domains'][child],1)
        self.assertEqual(c['library']['domains']['unclassified'],1)
        self.assertEqual(c['pending']['total'],1)
        self.assertEqual(c['trash']['domains']['personal'],1)
        self.assertEqual(c['review_due']['total'],1)
        for group,filters in [('library',{}),('pending',{'pending':True}),('trash',{'state':'deleted'}),('review_due',{'review_due':True})]:
            self.assertEqual(c[group]['total'],self.db.workbench_list(filters)['total'])
        self.assertNotIn('合成正文不应出现在目录统计中',json.dumps(catalog,ensure_ascii=False))

    def test_catalog_is_read_only(self):
        self.create('personal')
        def fingerprint():
            con=self.db.connect()
            try:return hashlib.sha256(json.dumps([tuple(r) for r in con.execute('SELECT * FROM current_memory')],ensure_ascii=False).encode()).hexdigest()
            finally:con.close()
        before=fingerprint()
        self.db.workbench_catalog();self.db.workbench_catalog()
        self.assertEqual(before,fingerprint())

if __name__=='__main__':unittest.main(verbosity=2)
