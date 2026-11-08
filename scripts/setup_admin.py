"""仅初始化缺失认证；口令从标准输入传入，不接受命令行口令。"""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from hub.auth import AuthManager
from hub.worker import SingleInstanceFileLock

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',default=str(ROOT));a=p.parse_args()
    runtime=checked_path(a.runtime)
    with SingleInstanceFileLock(runtime/'data/hub.lock'):
        target=runtime/'config/admin_auth.json'
        if target.exists():raise SystemExit('已有管理员认证已保留；本入口不重置。')
        passcode=sys.stdin.readline().rstrip('\r\n')
        AuthManager(runtime/'config/client_tokens.json',target,initial_passcode=passcode)
        print(json.dumps({'status':'initialized','plaintext_saved':False}))
