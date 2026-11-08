"""复用已安装浏览器技能的 prepare 校验；按用户约束将本任务输出放到项目内。"""
import argparse,importlib.util,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
launcher=Path('C:/Users/ZhuanZ/.codex/skills/playwright-cli/scripts/browser.py')
spec=importlib.util.spec_from_file_location('approved_browser_launcher',launcher)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',nargs=argparse.REMAINDER);args=p.parse_args()
    command,env,workspace,output=module.prepare('smh-phase1a-030',args.command,
        config_path=ROOT/'receipts/v0.3.0-implementation/browser-config.json')
    try:
        result=subprocess.run(command,env=env,cwd=workspace,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=55)
        sys.stdout.write(result.stdout);sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode or (1 if '### Error' in result.stdout else 0))
    except subprocess.TimeoutExpired:raise SystemExit('browser command timeout; inspect owned session before retrying')
