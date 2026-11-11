"""按既有已核验浏览器启动器运行1B独立浏览器会话。"""
import os
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('approved_browser',os.environ['SMH_BROWSER_LAUNCHER'])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
if __name__=='__main__':
    command,env,workspace,output=module.prepare('smh-phase1b-4a8400a659',sys.argv[1:],config_path=ROOT/'receipts/v0.3.0-implementation/browser-config.json')
    result=subprocess.run(command,env=env,cwd=workspace,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=55)
    sys.stdout.write(result.stdout);sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode or (1 if '### Error' in result.stdout else 0))
