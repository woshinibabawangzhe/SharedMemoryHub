"""显式离线 Schema 迁移。不会建立缺失墓碑；先备份，再事务增加清除进度。"""
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.paths import checked_path
from hub.worker import SingleInstanceFileLock
from hub.db import HubError


def migrate(runtime):
    root=checked_path(runtime)
    db=root/'data/sqlite/memory_hub.db'
    tomb=root/'data/tombstones/purge_registry.jsonl'
    if not db.is_file() or not tomb.is_file():
        raise HubError(503,'missing_or_corrupted_purge_evidence','数据库或墓碑缺失，拒绝迁移。')
    with SingleInstanceFileLock(root/'data/hub.lock'):
        records=[json.loads(x) for x in tomb.read_text(encoding='utf-8').splitlines()]
        previous=hashlib.sha256(b'shared_memory_hub_purge_genesis_v1').hexdigest()
        if not records or records[0].get('seq')!=0 or records[0].get('entry_hash')!=previous:
            raise ValueError('corrupted_genesis')
        for i,row in enumerate(records[1:],1):
            digest=hashlib.sha256(f"{previous}:{i}:{row['memory_id']}:{row['purged_content_sha256']}".encode()).hexdigest()
            if row['seq']!=i or row['prev_hash']!=previous or row['entry_hash']!=digest:
                raise ValueError('corrupted_tombstone_chain')
            previous=digest
        con=sqlite3.connect(str(db))
        try:
            maximum=json.loads(con.execute("SELECT meta_value_json FROM hub_meta WHERE meta_key='max_purge_seq'").fetchone()[0])
            if len(records)-1 < maximum:
                raise ValueError('truncated_tombstone_chain')
            version=con.execute('PRAGMA user_version').fetchone()[0]
            if version==300:
                return {'status':'already_current','schema':300}
            if version!=0:
                raise ValueError('unsupported_schema')
            backup=db.with_name('memory_hub.pre-v0.3.0.db')
            if backup.exists():
                raise FileExistsError('migration backup exists; inspect previous result')
            with closing(sqlite3.connect(str(backup))) as target:
                con.backup(target)
            columns={r[1] for r in con.execute('PRAGMA table_info(purge_tombstones)')}
            con.execute('BEGIN IMMEDIATE')
            for col in ('sqlite_done','vector_done','exports_done','compact_done'):
                if col not in columns:
                    con.execute(f'ALTER TABLE purge_tombstones ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0')
            con.execute('PRAGMA user_version=300')
            con.commit()
            return {'status':'migrated','schema':300,'backup':str(backup)}
        finally:
            con.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',required=True)
    print(json.dumps(migrate(parser.parse_args().runtime),ensure_ascii=False))
