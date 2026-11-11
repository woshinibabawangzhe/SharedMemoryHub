"""显式离线 Schema 迁移。不会建立缺失墓碑；先备份，再事务增加清除进度。"""
import argparse
import hashlib
import json
import sqlite3
import shutil
from datetime import datetime, timezone
import sys
from contextlib import closing
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.paths import checked_path
from hub.worker import SingleInstanceFileLock
from hub.db import HubError, SCHEMA_SQL, SCHEMA_VERSION, tombstone_digest


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
            digest=tombstone_digest(row)
            if row['seq']!=i or row['prev_hash']!=previous or row['entry_hash']!=digest:
                raise ValueError('corrupted_tombstone_chain')
            previous=digest
        con=sqlite3.connect(str(db))
        try:
            maximum=json.loads(con.execute("SELECT meta_value_json FROM hub_meta WHERE meta_key='max_purge_seq'").fetchone()[0])
            if len(records)-1 < maximum:
                raise ValueError('truncated_tombstone_chain')
            version=con.execute('PRAGMA user_version').fetchone()[0]
            if version==SCHEMA_VERSION:
                return {'status':'already_current','schema':SCHEMA_VERSION}
            if version not in (0,300,400,500):
                raise ValueError('unsupported_schema')
            backup_dir=root/'backups'/(f'schema-{SCHEMA_VERSION}-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
            backup_dir.mkdir(parents=True,exist_ok=False)
            backup=backup_dir/'memory_hub.db'
            with closing(sqlite3.connect(str(backup))) as target:
                con.backup(target)
            shutil.copyfile(tomb,backup_dir/'purge_registry.jsonl')
            statements=[]
            request_columns={r[1] for r in con.execute('PRAGMA table_info(requests)')}
            if 'result_json' not in request_columns:
                statements.append('ALTER TABLE requests ADD COLUMN result_json TEXT;')
            columns={r[1] for r in con.execute('PRAGMA table_info(purge_tombstones)')}
            for col in ('sqlite_done','vector_done','exports_done','compact_done'):
                if col not in columns:statements.append(f'ALTER TABLE purge_tombstones ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0;')
            columns={r[1] for r in con.execute('PRAGMA table_info(current_memory)')}
            for col,definition in [('domain_id','TEXT'),('tags_json',"TEXT NOT NULL DEFAULT '[]'"),('review_due_at','TEXT'),('last_reviewed_at','TEXT')]:
                if col not in columns:statements.append(f'ALTER TABLE current_memory ADD COLUMN {col} {definition};')
            try:
                con.executescript('BEGIN IMMEDIATE;\n'+'\n'.join(statements)+'\n'+SCHEMA_SQL+f'\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
                if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('migration_integrity_failed')
            except Exception:
                if con.in_transaction:con.rollback()
                raise
            result={'status':'migrated','from_schema':version,'schema':SCHEMA_VERSION,'backup':str(backup),'tombstone_backup':str(backup_dir/'purge_registry.jsonl')}
            (backup_dir/'migration-receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            return result
        finally:
            con.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',required=True)
    print(json.dumps(migrate(parser.parse_args().runtime),ensure_ascii=False))
