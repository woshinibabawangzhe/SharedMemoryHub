"""新正式运行目录初始化；仅用户终端stdin口令，不处理旧库。"""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from hub.auth import AuthManager
from hub.db import MemoryDatabase
from hub.worker import SingleInstanceFileLock


if __name__=='__main__':
    runtime=checked_path(ROOT/'runtime/codex')
    with SingleInstanceFileLock(runtime/'data/hub.lock'):
        if (runtime/'config/admin_auth.json').exists() or (runtime/'data/sqlite/memory_hub.db').exists():
            raise SystemExit('已有正式认证或数据库已保留；禁止自动重置或迁移。')
        profile_path=checked_path(runtime/'profile.json')
        profile=json.loads(profile_path.read_text(encoding='utf-8-sig')) if profile_path.is_file() else {
            'version':'0.1.0','enabled':False,'mode':'setup','runtime_root':str(runtime),
            'base_url':'http://127.0.0.1:8765','registered_workspaces':[str(ROOT)],'setup_status':'awaiting_user_setup'}
        if Path(profile['runtime_root']).absolute()!=runtime or profile['base_url']!='http://127.0.0.1:8765':
            raise SystemExit('正式运行配置不匹配；未初始化。')
        passcode=sys.stdin.readline().rstrip('\r\n')
        AuthManager(runtime/'config/client_tokens.json',runtime/'config/admin_auth.json',initial_passcode=passcode)
        passcode=None
        db=MemoryDatabase(db_path=runtime/'data/sqlite/memory_hub.db',tombstone_path=runtime/'data/tombstones/purge_registry.jsonl',exports_dir=runtime/'exports',backups_dir=runtime/'backups')
        db.register_project_binding('proj_shared_memory_hub',ROOT)
        profile.update(enabled=True,mode='daily',setup_status='initialized')
        profile_path.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'initialized','runtime':str(runtime),'plaintext_saved':False,'existing_data_migrated':False},ensure_ascii=False))
