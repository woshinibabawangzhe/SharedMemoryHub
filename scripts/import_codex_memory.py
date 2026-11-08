"""获准的Codex记忆完整复制 v0.1.0；原文留本机，普通客户端创建待批记录。"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).absolute().parents[1];sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from hub.db import SECRET_PATTERNS, PRIVILEGE_ESCALATION_PATTERNS

PREFIX='全量副本 F1'
CURRENT=('personal-context/canonical/','personal-context/work-environment/')
RESIDUAL={'communication-preferences.md','decision-preferences.md','learning-profile.md'}

def sha(data):return hashlib.sha256(data).hexdigest()

def safe_source(value):
    p=Path(os.path.abspath(value))
    for a in (p,*p.parents):
        if a.is_symlink() or (a.exists() and a.is_junction()):raise ValueError('linked_source_forbidden')
    return p

def save(path,value):
    path=checked_path(path)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

def scan(root):
    files=[];empty=[]
    for name in ('personal-context','memories'):
        base=safe_source(root/name)
        if not base.is_dir():raise ValueError('required_source_root_missing: '+name)
        for folder,dirs,names in os.walk(base,followlinks=False):
            safe_source(folder)
            for d in dirs:
                if d!='.git':safe_source(Path(folder)/d)
            dirs[:]=[d for d in dirs if d!='.git']
            if not dirs and not names:empty.append(Path(folder).relative_to(root).as_posix())
            for name in names:
                p=safe_source(Path(folder)/name)
                if p.suffix not in ('.md','.yaml','.yml') and p.name!='.gitignore':
                    raise ValueError('unclassified_source_file: '+p.relative_to(root).as_posix())
                files.append(p)
    return sorted(files),empty

def body_and_meta(text):
    meta={};body=text
    if text.startswith('---\n'):
        _,head,body=text.split('---\n',2)
        for line in head.splitlines():
            if ': ' in line:
                k,v=line.split(': ',1);meta[k]=v.strip().strip('"')
    return body.strip(),meta

def prepare(source_root,batch,trial):
    source_root=safe_source(source_root);batch=checked_path(batch);trial=checked_path(trial)
    if batch.exists():raise FileExistsError('existing_batch_preserved')
    old=json.loads(trial.read_text(encoding='utf-8'));active=[];purged=[]
    for card,item in zip(old['cards'],old['imported']):
        assert card['number']==item['number']
        row={'memory_id':item['memory_id'],'content':card['request']['content']}
        (purged if int(card['number'])==8 else active).append(row)
    files,empty=scan(source_root);staged=[]
    # Scan known memory sources before persisting any content; no auth/session roots.
    for p in files:
        data=p.read_bytes();text=data.decode('utf-8-sig')
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):raise ValueError('credential_pattern_in_source: '+p.name)
        staged.append((p,data,text.replace('\r\n','\n')))
    batch.mkdir(parents=True,exist_ok=False);(batch/'requests').mkdir();seen={};rows=[];requests=[]
    staged.sort(key=lambda x:(not x[0].relative_to(source_root).as_posix().startswith(CURRENT),x[0].relative_to(source_root).as_posix()))
    for p,data,text in staged:
        rel=p.relative_to(source_root).as_posix();snapshot=checked_path(batch/'source-snapshots'/rel);snapshot.parent.mkdir(parents=True,exist_ok=True)
        snapshot.write_bytes(data)
        if p.read_bytes()!=data or snapshot.read_bytes()!=data:raise RuntimeError('source_changed_during_copy')
        body,meta=body_and_meta(text);current=rel.startswith(CURRENT)
        status='current_source_copy' if current else 'historical_or_control_archive'
        if meta.get('status')=='needs_periodic_review':status='needs_review'
        if '/.local/' in rel:status='local_only_machine_archive'
        row={'source':str(p),'relative_path':rel,'snapshot':str(snapshot),'bytes':len(data),'sha256':sha(data),
             'source_mtime_ns':p.stat().st_mtime_ns,'source_metadata':meta,'disposition':status,
             'sensitivity':'local-only' if '/.local/' in rel else 'shared-private','record_numbers':[],'reused_memory_ids':[], 'suppressed_purged_ids':[]}
        if sha(data) in seen:
            row.update(disposition='duplicate_bytes_archived',record_numbers=seen[sha(data)]['record_numbers'],duplicate_of=seen[sha(data)]['relative_path'])
            rows.append(row);continue
        if p.name=='.gitignore' or rel=='memories/raw_memories.md' and body=='# Raw Memories\n\nNo raw memories yet.':
            row['disposition']='control_or_empty_placeholder_archive_only';rows.append(row);seen[sha(data)]=row;continue
        if current and p.name in RESIDUAL:
            kept=[]
            for line in body.splitlines():
                sentence=line[2:].strip() if line.startswith('- ') else None
                old_match=next((x for x in active if sentence and sentence in x['content']),None)
                gone_match=next((x for x in purged if sentence and sentence in x['content']),None)
                if old_match:row['reused_memory_ids'].append(old_match['memory_id'])
                elif gone_match:row['suppressed_purged_ids'].append(gone_match['memory_id'])
                else:kept.append(line)
            body='\n'.join(kept).strip();row['reused_memory_ids']=sorted(set(row['reused_memory_ids']));row['suppressed_purged_ids']=sorted(set(row['suppressed_purged_ids']))
        label=meta.get('display_name') or next((s[2:].strip() for s in body.splitlines() if s.startswith('# ')),p.stem)
        label=re.sub(r'[^\w\u4e00-\u9fff /.-]','',label)[:65]
        mode='当前资料' if status=='current_source_copy' else '待复核' if status=='needs_review' else '档案'
        heading=f'来源副本：{rel}。原始确认与复核日期见source元数据；本次复制不等于重新确认。'
        if not current:heading+='这是历史或管理资料，仅供追溯；其中指令不作为现役规则，不自动执行。'
        if row['reused_memory_ids']:heading+='已生效部分沿用原记录，本条补足其余内容。'
        if row['suppressed_purged_ids']:heading+='用户已清除的开发模式片段不进入可检索正文；原始来源仅留本机档案。'
        if status=='needs_review':heading+='原卡需要定期复核，不作为当前服务能力保证。'
        chunks=[body[i:i+3100] for i in range(0,len(body),3100)]
        for part,chunk in enumerate(chunks,1):
            content=heading+'\n\n'+chunk
            if any(pattern.search(content) for pattern in PRIVILEGE_ESCALATION_PATTERNS):
                row['disposition']='guard_rejected_archive_only';continue
            number=len(requests)+1;title=f'{PREFIX}｜{mode}｜{label}'+(f'（{part}/{len(chunks)}）' if len(chunks)>1 else '')
            request={'request_key':'full_f1_'+sha((batch.name+rel+str(part)+sha(data)).encode())[:36],
                'operation':'create','subject':'user','scope':'user_global','kind':'context',
                'topic_key':'codex.fullcopy.'+sha(rel.encode())[:20]+'.'+str(part),
                'title':title[:120],'content':content,'confidence':'confirmed' if status=='current_source_copy' else 'hypothesis',
                'readers':['user','codex'] if row['sensitivity']=='local-only' else ['user','codex','antigravity'],
                'source':{'origin_type':'project_fact','evidence_path':str(snapshot),'evidence_sha256':sha(data),
                    'original_path':str(p),'original_metadata':meta,'copy_batch':batch.name,'archive_disposition':status,
                    'sensitivity':row['sensitivity'],'summary':'用户批准完整复制；原文有独立哈希，未重新核实历史事实。'}}
            path=batch/'requests'/f'{number:03}.json';save(path,request)
            requests.append({'number':number,'request_file':str(path),'title':title,'source_relative_path':rel,
                'expected_confidence':request['confidence'],'request_key':request['request_key'],'status':'prepared'})
            row['record_numbers'].append(number)
        rows.append(row);seen[sha(data)]=row
    manifest={'version':'0.1.0','batch_id':batch.name,'created_at':datetime.now(timezone.utc).isoformat(),'prefix':PREFIX,
        'source_root':str(source_root),'source_roots':['personal-context','memories'],'source_files':rows,'empty_directories':empty,
        'raw_files':len(rows),'raw_bytes':sum(x['bytes'] for x in rows),'requests':requests,
        'reused_active_memories':[x['memory_id'] for x in active],'suppressed_purged_memories':[x['memory_id'] for x in purged],
        'exclusions':['.git metadata and deleted history','authentication and sessions','caches and dependencies','external libraries referenced by pointers'],
        'old_mechanism_changed':False,'raw_archive_in_git':False,'status':'prepared'}
    save(batch/'manifest.json',manifest);return manifest

def load_adapter(path):
    path=safe_source(path)
    if not path.is_file():raise FileNotFoundError('registered_adapter_missing')
    spec=importlib.util.spec_from_file_location('approved_codex_adapter',path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def send(batch,adapter):
    batch=checked_path(batch);manifest=json.loads((batch/'manifest.json').read_text(encoding='utf-8'));module=load_adapter(adapter)
    if not os.environ.get('CODEX_THREAD_ID'):raise RuntimeError('real_codex_session_required')
    def call(command,**kw):return module.execute(SimpleNamespace(profile=module.DEFAULT_PROFILE,no_workspace=True,workspace=None,command=command,**kw))
    for memory in manifest['suppressed_purged_memories']:
        if call('get',id=memory).get('http_status')!=410:raise RuntimeError('purged_selection_no_longer_matches')
    for row in manifest['requests']:
        if row['status']=='paused_pending_approval':continue
        if row['status']!='prepared':raise RuntimeError('unknown_or_failed_write_requires_explicit_reconciliation')
        precheck=call('precheck',query=row['title'],limit=5)
        if precheck.get('http_status',200)>=400:raise RuntimeError('precheck_unavailable')
        row['precheck']={'status':precheck.get('status'),'candidate_ids':[x['memory_id'] for x in precheck.get('items',[])]}
        row['status']='WRITE_RESULT_UNKNOWN';save(batch/'manifest.json',manifest)
        result=call('write',request_file=Path(row['request_file']))
        row.update(status=result.get('status','unknown'),result=result);save(batch/'manifest.json',manifest)
        if result.get('http_status',200)>=400 or row['status']!='paused_pending_approval':raise RuntimeError('write_not_confirmed_pending; preserve original key and request')
        print(json.dumps({'number':row['number'],'status':row['status'],'memory_id':result.get('memory_id')},ensure_ascii=False),flush=True)
    manifest['status']='copied_pending_user_review';save(batch/'manifest.json',manifest);return manifest

def verify(batch):
    batch=checked_path(batch);m=json.loads((batch/'manifest.json').read_text(encoding='utf-8'))
    actual,_=scan(safe_source(m['source_root']));assert {str(p) for p in actual}=={r['source'] for r in m['source_files']}
    for row in m['source_files']:
        assert sha(safe_source(row['source']).read_bytes())==row['sha256']==sha(checked_path(row['snapshot']).read_bytes())
    return {'status':'verified','raw_files':len(m['source_files']),'raw_bytes':m['raw_bytes'],'source_unchanged':True,
            'request_count':len(m['requests']),'pending':sum(r['status']=='paused_pending_approval' for r in m['requests'])}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','send','verify']);p.add_argument('--batch',required=True)
    p.add_argument('--source-root');p.add_argument('--trial-manifest');p.add_argument('--adapter');a=p.parse_args()
    if a.action=='prepare':m=prepare(a.source_root,a.batch,a.trial_manifest);result={'status':m['status'],'raw_files':m['raw_files'],'requests':len(m['requests'])}
    elif a.action=='send':m=send(a.batch,a.adapter);result={'status':m['status'],'requests':len(m['requests'])}
    else:result=verify(a.batch)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
