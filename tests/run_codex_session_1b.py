"""启动真实、无历史继承的Codex CLI会话；仅保存工具轨迹和最终答复。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
CODEX=Path('C:/Users/ZhuanZ/AppData/Roaming/npm/codex.cmd')


def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--name',required=True);p.add_argument('--prompt-file',type=Path,required=True);a=p.parse_args()
    evidence=a.evidence.absolute()
    if not evidence.is_relative_to(ROOT/'receipts'):raise RuntimeError('invalid_evidence_root')
    meta=json.loads((evidence/'live-instance.json').read_text(encoding='utf-8'))
    target=evidence/('native-'+a.name)
    target.mkdir()
    runtime=target/'runtime'
    for name in ('tmp','logs','state','cache'):(runtime/name).mkdir(parents=True,exist_ok=True)
    env=os.environ.copy()
    for name in ('CODEX_THREAD_ID','CODEX_SESSION_ID'):env.pop(name,None)
    env.update(TEMP=str(runtime/'tmp'),TMP=str(runtime/'tmp'),PYTHONDONTWRITEBYTECODE='1',XDG_CACHE_HOME=str(runtime/'cache'))
    command=[str(CODEX),'exec','--ephemeral','--json','--skip-git-repo-check','--color','never','--sandbox','workspace-write',
        '-c','approval_policy="never"','-c','features.multi_agent=false','-c','sandbox_workspace_write.network_access=true',
        '-c','log_dir='+json.dumps(str(runtime/'logs')),'-c','sqlite_home='+json.dumps(str(runtime/'state')),
        '-C',meta['workspace'],'--add-dir',str(evidence),'-']
    prompt=a.prompt_file.read_text(encoding='utf-8-sig')
    (target/'prompt.txt').write_text(prompt,encoding='utf-8')
    started=time.monotonic()
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,cwd=meta['workspace'],text=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
    stderr=[]
    def drain():
        for line in proc.stderr:
            # 不保存认证、端点、启动日志；仅记录错误类别计数。
            stderr.append('error' if 'error' in line.lower() else 'diagnostic')
    threading.Thread(target=drain,daemon=True).start()
    proc.stdin.write(prompt);proc.stdin.close()
    timer=threading.Timer(300,proc.kill);timer.start()
    events=[]
    try:
        for line in proc.stdout:
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('item',{}).get('type')=='reasoning':continue
            events.append(event)
            with (target/'events.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(event,ensure_ascii=False)+'\n')
        code=proc.wait(timeout=15)
    finally:timer.cancel()
    summary={'returncode':code,'elapsed_seconds':round(time.monotonic()-started,3),'native_fresh_session':True,'ephemeral':True,
        'thread_ids':[e.get('thread_id') for e in events if e.get('type')=='thread.started'],
        'commands':[e['item'].get('command','') for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='command_execution'],
        'final_answers':[e['item'].get('text','') for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='agent_message'],
        'errors':[e for e in events if e.get('type') in ('error','turn.failed')],'diagnostic_lines':len(stderr),'evidence_level':'controlled-trial'}
    (target/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
