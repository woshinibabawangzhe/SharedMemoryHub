"""便携数据备份/恢复 v0.1.0；仅获准离线维护，不是普通记忆客户端入口。"""
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3, sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).absolute().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from hub.db import MemoryDatabase, SCHEMA_VERSION
from hub.worker import SingleInstanceFileLock

def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def write_new(path,doc):
    with path.open('x',encoding='utf-8',newline='\n') as stream:json.dump(doc,stream,ensure_ascii=False,indent=2);stream.write('\n')

def counts(db):
    with closing(sqlite3.connect(f'file:{db.as_posix()}?mode=ro',uri=True)) as con:
        if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('sqlite_integrity_failed')
        if con.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION:raise RuntimeError('migration_required')
        return {name:con.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('current_memory','revisions','conflict_proposals','purge_tombstones','project_bindings')}

def verify_bundle(bundle):
    bundle=checked_path(bundle)
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!='smh-portable-data-v1':raise RuntimeError('unsupported_bundle_format')
    if set(manifest['files'])!={'memory_hub.db','purge_registry.jsonl'}:raise RuntimeError('unexpected_bundle_members')
    for name,expected in manifest['files'].items():
        path=checked_path(bundle/name)
        if digest(path)!=expected:raise RuntimeError('bundle_integrity_failed: '+name)
    if counts(bundle/'memory_hub.db')!=manifest['counts']:raise RuntimeError('bundle_counts_mismatch')
    return manifest

def backup(runtime,output):
    runtime=checked_path(runtime);output=checked_path(output)
    db=checked_path(runtime/'data/sqlite/memory_hub.db');tomb=checked_path(runtime/'data/tombstones/purge_registry.jsonl')
    if not db.is_file() or not tomb.is_file():raise RuntimeError('existing_database_and_tombstone_required')
    if output.exists():raise FileExistsError('backup_target_exists; no overwrite')
    with SingleInstanceFileLock(runtime/'data/hub.lock'):
        original_tomb=digest(tomb)
        output.mkdir(parents=True,exist_ok=False)
        with closing(sqlite3.connect(f'file:{db.as_posix()}?mode=ro',uri=True)) as source,closing(sqlite3.connect(str(output/'memory_hub.db'))) as dest:source.backup(dest)
        shutil.copyfile(tomb,output/'purge_registry.jsonl')
        if digest(tomb)!=original_tomb:raise RuntimeError('source_changed_during_backup')
        # 验证副本的墓碑链；绝不在本次备份过程中改写原库。
        stage=MemoryDatabase(output/'memory_hub.db',output/'purge_registry.jsonl',output/'verify/exports',output/'verify/backups')
        proof=stage.verify_and_reconcile_purge_registry()
        with closing(stage.connect()) as con:con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        manifest={'format':'smh-portable-data-v1','created_at':datetime.now(timezone.utc).isoformat(),
            'schema':SCHEMA_VERSION,'service_version':'0.5.0','portability_revision':'0.1.0','source_runtime':str(runtime),
            'files':{name:digest(output/name) for name in ('memory_hub.db','purge_registry.jsonl')},
            'counts':counts(output/'memory_hub.db'),'max_purge_seq':proof['file_max_seq'],
            'credentials_included':False,'vectors_included':False,'encrypted':False,
            'notice':'私有本机数据包，禁止加入源码仓库；携带最新墓碑，新机需新认证及重建向量。'}
        write_new(output/'manifest.json',manifest)
        verify_bundle(output)
        return {'status':'backed_up','bundle':str(output),'counts':manifest['counts'],'max_purge_seq':manifest['max_purge_seq'],'credentials_included':False,'encrypted':False}

def restore(bundle,target,*,minimum_purge_seq,latest_tombstone=None):
    if minimum_purge_seq<0:raise ValueError('minimum_purge_seq_must_be_nonnegative')
    bundle=checked_path(bundle);target=checked_path(target);manifest=verify_bundle(bundle)
    if target.exists():raise FileExistsError('restore_target_exists; no overwrite')
    target.mkdir(parents=True,exist_ok=False)
    with SingleInstanceFileLock(target/'data/hub.lock'):
        db=checked_path(target/'data/sqlite/memory_hub.db');tomb=checked_path(target/'data/tombstones/purge_registry.jsonl')
        db.parent.mkdir(parents=True,exist_ok=True);tomb.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(bundle/'memory_hub.db',db)
        shutil.copyfile(checked_path(latest_tombstone) if latest_tombstone else bundle/'purge_registry.jsonl',tomb)
        if counts(db)!=manifest['counts']:raise RuntimeError('restored_history_counts_mismatch')
        restored=MemoryDatabase(db,tomb,target/'exports',target/'backups')
        proof=restored.verify_and_reconcile_purge_registry(target_min_seq=max(minimum_purge_seq,manifest['max_purge_seq']))
        # Qdrant是可重建索引；保留正文修订，明确作废旧索引水位。
        with closing(restored.connect()) as con:
            con.execute("UPDATE current_memory SET indexed_revision=0 WHERE state='active'")
        restored.startup_recover_reindex_and_worker()
        result={'status':'restored_auth_required','target':str(target),'counts':counts(db),
            'max_purge_seq':proof['file_max_seq'],'credentials_restored':False,'automatic_service_start':False,
            'requires':['user initializes new credentials','review project workspace mapping','serial worker rebuilds vectors'],
            'bundle_manifest_sha256':digest(bundle/'manifest.json')}
        write_new(target/'restore-receipt.json',result)
        return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('backup');p.add_argument('--runtime',required=True);p.add_argument('--output',required=True);p.add_argument('--confirm-offline-maintenance',action='store_true',required=True)
    p=sub.add_parser('restore');p.add_argument('--bundle',required=True);p.add_argument('--target',required=True);p.add_argument('--minimum-purge-seq',type=int,required=True);p.add_argument('--latest-tombstone');p.add_argument('--confirm-offline-maintenance',action='store_true',required=True)
    p=sub.add_parser('verify');p.add_argument('--bundle',required=True)
    a=parser.parse_args()
    if a.command=='verify':result={'status':'verified','manifest':verify_bundle(a.bundle)}
    elif a.command=='backup':result=backup(a.runtime,a.output)
    else:result=restore(a.bundle,a.target,minimum_purge_seq=a.minimum_purge_seq,latest_tombstone=a.latest_tombstone)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
