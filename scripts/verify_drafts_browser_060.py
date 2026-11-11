"""仅经已登记浏览器启动器操作合成实例；不使用CDP、JS求值或认证导入。"""
import os
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
    evidence=checked_path(args.evidence);transcript=evidence/'browser-actions.log';results=[]
    launcher=Path(os.environ['SMH_BROWSER_LAUNCHER'])
    def run(*args):
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(launcher),'--session','smh-drafts-060',*args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=45)
        with transcript.open('a',encoding='utf-8') as log:log.write('\nCOMMAND '+json.dumps(args,ensure_ascii=False)+'\n'+proc.stdout+'\n'+proc.stderr)
        if proc.returncode or '### Error' in proc.stdout:raise RuntimeError(proc.stdout+proc.stderr)
        return proc.stdout
    def snapshot():return run('snapshot')
    def find(role,label,prefix=False):
        text=snapshot()
        for line in text.splitlines():
            match=re.search(r'- '+role+r' "(.*)".*\[ref=(e\d+)\]',line)
            if match and (match[1].startswith(label) if prefix else match[1]==label):return match[2]
        raise RuntimeError('missing '+role+' '+label)
    def click(label,prefix=False):run('click',find('button',label,prefix))
    def fill(label,value):run('fill',find('textbox',label),value)
    def assert_text(text):
        if text not in snapshot():raise AssertionError(text)
    def check(label):run('check',find('checkbox',label))
    def proof(name):
        output=run('screenshot');results.append({'scenario':name,'screenshot_output':output});print('verified '+name,flush=True)

    # A new browser process loads the final shipped JavaScript, not an old page's copy.
    fill('管理员口令','Synthetic-memory-drafts-060');click('进入工作台')
    run('resize','1440','900');click('草稿与协作')
    click('合成纠正：',True);click('查看差异并确认')
    check('原记忆已锁定；我确认由本人采纳本次修改，保存后继续锁定。')
    assert_text('确认采纳为记忆');click('确认采纳为记忆');assert_text('已采纳为记忆 r2')
    proof('locked-correction-confirmation-keeps-preview-visible')

    click('先写下来');fill('你的原话','合成浏览器取消恢复清除，仅供验收。');click('保存草稿')
    click('取消这份草稿');click('确认');assert_text('已取消')
    click('恢复草稿');assert_text('待整理')
    click('取消这份草稿');click('确认')
    page=snapshot();confirmation=re.search(r'textbox "请输入 (PURGE draft_[a-f0-9]+ r\d+)"',page)
    if not confirmation:raise AssertionError('purge confirmation missing')
    fill('请输入 '+confirmation[1],confirmation[1]);click('确认永久清除草稿');assert_text('草稿已永久清除')
    proof('cancel-restore-permanent-purge')

    click('使用与系统说明');assert_text('SQLite是正文');assert_text('数据库 500');proof('help-desktop-1440x900')
    run('resize','390','844');click('先写下来');fill('你的原话','合成窄屏输入，仅用于验证核心动作可达。');click('保存草稿');assert_text('原话已保存为草稿');proof('capture-narrow-390x844')
    click('使用与系统说明');proof('help-narrow-390x844')
    console=run('console')
    if 'Errors: 0, Warnings: 0' not in console:raise AssertionError(console)
    results.append({'scenario':'console','result':console})
    (evidence/'browser-results.json').write_text(json.dumps({'status':'verified','scenarios':results},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
