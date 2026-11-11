"""Browser regression for cross-section unsaved input, using only the synthetic service."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',required=True,type=Path)
    args=parser.parse_args()
    import sys
    sys.path.insert(0,str(ROOT))
    from hub.paths import checked_path
    evidence=checked_path(args.evidence)
    spec=json.loads((evidence/'live-spec.json').read_text(encoding='utf-8'))
    assert spec['data_kind']=='synthetic-only' and spec['prefix'].startswith('smh_drafts_060_')
    def run(*args):
        p=subprocess.run(['D:/python/python.exe','-X','utf8','-B',
            'C:/Users/ZhuanZ/.codex/skills/playwright-cli/scripts/browser.py','--session','smh-drafts-060',*args],
            cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=45)
        with (evidence/'browser-unsaved-actions.log').open('a',encoding='utf-8') as stream:
            stream.write('\nCOMMAND '+json.dumps(args,ensure_ascii=False)+'\n'+p.stdout+'\n'+p.stderr)
        if p.returncode or '### Error' in p.stdout:raise RuntimeError(p.stdout+p.stderr)
        return p.stdout
    def find(role,label):
        page=run('snapshot')
        for line in page.splitlines():
            match=re.search(r'- '+role+r' "(.*)".*\[ref=(e\d+)\]',line)
            if match and match[1]==label:return match[2]
        raise AssertionError('missing '+role+' '+label)
    def click(label):return run('click',find('button',label))
    def fill(label,text):return run('fill',find('textbox',label),text)
    screenshots=[]
    try:
        run('open',spec['url']+'/ui')
        fill('管理员口令','Synthetic-memory-drafts-060');click('进入工作台');run('resize','1440','900')
        click('先写下来');fill('你的原话','合成未保存保护：原始文字。');click('保存草稿')
        fill('原话与补充','合成未保存保护：这一句尚未保存，不能静默丢失。')
        run('click',find('generic','草稿适用信息'))
        click('保存适用信息')
        page=run('snapshot');assert '其他区域还有未保存的修改' in page
        click('取消')
        page=run('snapshot');assert '这一句尚未保存，不能静默丢失' in page and '草稿 v1' in page
        screenshots.append(run('screenshot'))
        click('保存原话与补充');page=run('snapshot')
        assert '草稿 v2' in page and '这一句尚未保存，不能静默丢失' in page
        screenshots.append(run('screenshot'))
        click('使用与系统说明');page=run('snapshot');assert '发布检查通过 scripts/check_release_contract.py' in page
        screenshots.append(run('screenshot'));run('resize','390','844');screenshots.append(run('screenshot'))
        console=run('console');assert 'Errors: 0, Warnings: 0' in console
        result={'status':'verified','scenarios':['cross-section save asks before discarding','cancel keeps original input and revision','save own section persists v2','final same-source help desktop and narrow'],'screenshots':screenshots,'console':console}
        (evidence/'browser-unsaved-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':'verified','scenarios':result['scenarios']},ensure_ascii=False))
    finally:run('close')


if __name__=='__main__':main()
