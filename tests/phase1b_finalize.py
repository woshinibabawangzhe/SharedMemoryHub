"""按可观察原始证据生成1B Codex单端回执；不读取凭据内容。"""
import argparse,calendar,hashlib,json,os,shutil,socket,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];HOME=Path('C:/Users/ZhuanZ/.codex');sys.path.insert(0,str(ROOT))
from hub.paths import checked_path

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def write(p,doc):p.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def stamp_ns(value):
    value=value.rstrip('Z');head,_,fraction=value.partition('.')
    return calendar.timegm(datetime.fromisoformat(head).timetuple())*10**9+int((fraction+'000000000')[:9])

def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--register-daily',action='store_true');a=p.parse_args();e=checked_path(a.evidence)
    evidence={name:read(e/('native-'+name)/'summary.json') for name in ['discovery','discovery-design','write-latest','offline']}
    for name in ['discovery-design','write-latest','offline']:
        assert evidence[name]['returncode']==0 and evidence[name]['native_fresh_session'] and len(evidence[name]['thread_ids'])==1,name
    discovery=evidence['discovery-design'];assert any('cli.py' in c and 'search' in c for c in discovery['commands']) and any('cli.py' in c and ' get ' in c for c in discovery['commands'])
    assert '山茶初版' in discovery['final_answers'][-1]
    native=evidence['write-latest'];assert '银杏新版' in native['final_answers'][-1]
    preference=read(e/'codex-preference-verified.json')['item'];assert preference['created_by_actor_id']=='codex' and preference['project_id']=='proj_smh1b_synthetic'
    assert preference['created_in_session_id']==native['thread_ids'][0] and preference['source']['session_id']==native['thread_ids'][0]
    assert preference['state']=='active' and preference['revision']==1
    lock=read(e/'codex-locked-update-result.json');assert lock['http_status']==409 and lock['error_code']=='LockedConflict'
    latest=read(e/'codex-report-current.json')['item'];assert latest['revision']==2 and latest['locked'] and '银杏新版' in latest['content']
    offline=evidence['offline'];events=[json.loads(line) for line in (e/'native-offline/events.jsonl').read_text(encoding='utf-8').splitlines()]
    outputs='\n'.join(x.get('item',{}).get('aggregated_output','') for x in events)
    assert 'MEMORY_HUB_UNAVAILABLE' in outputs and any('cli.py' in c and ' search ' in c for c in offline['commands'])
    assert not any('runtime_control.py' in c or 'codex_daily_control.py' in c or 'docker start' in c or 'docker run' in c for c in offline['commands'])
    assert all(word not in offline['final_answers'][-1] for word in ['山茶初版','银杏新版'])
    boundaries=read(e/'boundaries.json');assert boundaries['all_passed'] and all(x['passed'] for x in boundaries['checks'])
    units=read(ROOT/'receipts/adapter-unit-cbae42350dd0/receipt.json');assert units['tests']==9 and not units['failures'] and not units['errors'] and units['adapter_sha256']==sha(HOME/'tools/shared-memory-hub/cli.py')
    degraded=read(e/'degrade-stop.json');assert degraded['keyword_search']['degraded_mode']=='keyword_only' and degraded['gateway_stop']['port_released']
    daily=read(e/'daily-smoke.json');assert daily['daily_left_running'] and daily['manual_controller_stop']['ports_released'] and daily['restart']['health']['embedding_model_loaded']
    daily_profile=read(ROOT/'runtime/codex/profile.json');assert daily_profile['enabled'] and daily_profile['mode']=='daily'
    protected=read(e/'baseline/protected-metadata.json');changed=[]
    for record in protected:
        file=Path(record['path']);stat=file.stat() if file.is_file() else None
        if stat is None or stat.st_size!=record['length'] or stat.st_mtime_ns!=stamp_ns(record['modified_utc']):changed.append(str(file))
    assert not changed,('protected_metadata_changed',changed)
    old=read(ROOT/'receipts/v0.3.0-followup-20261108-160010-70d397dd67/phase1a-final-receipt.json')
    unmatched=[rel for rel,digest in old['source_sha256'].items() if sha(ROOT/rel)!=digest]
    assert not unmatched,('phase1a_source_drift',unmatched)
    browser_source=ROOT/'cache/browser-030/outputs/af8136613d83acf3/smh-phase1b-4a8400a659';browser=e/'browser';browser.mkdir(exist_ok=True)
    for name in ['page-2026-11-08T12-09-14-857Z.png','page-2026-11-08T12-08-42-484Z.yml','console-2026-11-08T12-02-49-638Z.log']:
        src=browser_source/name;dest=browser/name
        if dest.exists():assert sha(dest)==sha(src)
        else:shutil.copyfile(src,dest)
    checks=[
        {'id':'C1','name':'非平凡任务自动发现及渐进召回','status':'已验证','evidence':'native-discovery-design/events.jsonl'},
        {'id':'C2','name':'真实Codex提炼写入与项目/会话归属','status':'已验证','evidence':'codex-preference-verified.json'},
        {'id':'C3','name':'看板自动编辑锁定与新会话最新修订/409拒绝','status':'已验证','evidence':'native-write-latest/events.jsonl','human_clicked':False},
        {'id':'C4','name':'离线新会话继续主任务且不自启','status':'已验证','evidence':'native-offline/events.jsonl'},
        {'id':'C5','name':'工作区、待批、TTL、过期、旧修订和丢响应对账','status':'已验证','evidence':'boundaries.json'},
        {'id':'C6','name':'真实CUDA/Docker与关键词降级、停止释放端口','status':'已验证','evidence':'degrade-stop.json'},
        {'id':'C7','name':'新正式空库初始化与手动启停/预热','status':'已验证','evidence':'daily-smoke.json'}]
    gate={'version':'0.1.0','created_at':datetime.now(timezone.utc).isoformat(),'status':'已验证','scope':'Codex native CLI controlled trial only','evidence_level':'controlled-trial','codex_single_client_qualified':True,'phase1b_overall_qualified':False,'checks':checks}
    write(e/'codex-controlled-gate.json',gate)
    if a.register_daily:
        h=read(HOME/'manifests/harness.json');h['shared_memory_hub']['daily_activation']='enabled-new-formal-root';h['shared_memory_hub']['admin_setup']='user-local-terminal-completed';write(HOME/'manifests/harness.json',h)
        q=read(HOME/'manifests/external-deployments.json');item=next(x for x in q['deployments'] if x['id']=='shared-memory-hub-codex');item['activation']='native CLI controlled trial passed; new formal empty root enabled after local user setup; desktop new chat and Antigravity pending';write(HOME/'manifests/external-deployments.json',q)
        write(e/'status.json',{'phase':'1B-Codex','status':'VERIFYING','unit':'final-control-integrity','daily_enabled':True,'codex_gate_passed':True,'antigravity_modified':False,'continuity_protection':'NOT_ARMED'})
        print(json.dumps({'codex_gate':'passed','daily_enabled':True,'register_only':True},ensure_ascii=False));return
    # 实际运行现有校验器并保存原始结果；当前源码指纹必须已经刷新。
    validators={}
    for name in ['verify_bundle','verify_protocol_adaptation']:
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(HOME/'scripts'/f'{name}.py')],capture_output=True,text=True,encoding='utf-8',timeout=60,cwd=HOME)
        doc=json.loads(proc.stdout);write(e/(name+'.json'),{'exit_code':proc.returncode,'result':doc});assert proc.returncode==0 and doc['status']=='verified',name;validators[name]=doc
    code_files=[*sorted((HOME/'tools/shared-memory-hub').glob('*')),HOME/'AGENTS.md',HOME/'关联规则索引.md',HOME/'scripts/verify_protocol_adaptation.py',HOME/'manifests/harness.json',HOME/'manifests/external-deployments.json',HOME/'manifests/environment-dependencies.json',HOME/'README.md',HOME/'docs/恢复与验收.md',HOME/'checksums.json',ROOT/'docs/客户端调用规范.md',ROOT/'design/2026-11-07/共同实施规格书-1B接入补充-v0.3.1.md',ROOT/'使用与启停-1B-Codex-v0.1.0.md',ROOT/'runtime/.gitignore',ROOT/'receipts/.gitignore']
    code_files += [ROOT/'scripts'/name for name in ['initialize_codex.py','initialize_codex.ps1','codex_daily_control.py']]
    code_files += [ROOT/name for name in ['初始化Codex记忆中心.cmd','启动Codex记忆中心.cmd','停止Codex记忆中心.cmd']]
    code_files += list(sorted((ROOT/'tests').glob('*1b*.py')))+[ROOT/'tests/test_codex_adapter_010.py']
    hashes={str(file):sha(file) for file in code_files if file.is_file()}
    trace_hashes={str(file.relative_to(e)):sha(file) for folder in ['native-discovery','native-discovery-design','native-write-latest','native-offline','browser'] for file in sorted((e/folder).glob('*')) if file.is_file()}
    for name in ['boundaries.json','boundaries-attempt2.json','degrade-stop.json','daily-smoke.json','codex-controlled-gate.json','verify_bundle.json','verify_protocol_adaptation.json','验收报告-1B-Codex-v0.1.0.md']:trace_hashes[name]=sha(e/name)
    for port in (18789,16379):
        with socket.socket() as sock:
            sock.settimeout(1);assert sock.connect_ex(('127.0.0.1',port))!=0
    git=subprocess.run(['git','-C',str(HOME),'status','--short'],capture_output=True,text=True,encoding='utf-8',timeout=20)
    (e/'codex-git-status.txt').write_text(git.stdout,encoding='utf-8')
    receipt={**gate,'version':'0.1.0+phase1b.codex','status':'部分验证','codex_single_client_status':'已验证','phase1b_overall_status':'部分验证','daily_enabled':True,'daily_origin':'http://127.0.0.1:8765','formal_memory_count_at_enable':0,'user_password_entered_locally':True,'new_admin_credentials_inspected':False,'antigravity_modified':False,'old_data_migrated':False,'git_commit_push_performed':False,'old_protected_files_metadata_unchanged':len(protected),'phase1a_hashes_still_matching':len(old['source_sha256']),'phase1a_reexecuted':False,'source_sha256':hashes,'evidence_sha256':trace_hashes,'phase1a_receipt_sha256':sha(ROOT/'receipts/v0.3.0-followup-20261108-160010-70d397dd67/phase1a-final-receipt.json'),'dependency_lock_sha256':sha(ROOT/'requirements.lock'),'backend_identity':daily['health_after_real_inference'],'qdrant_image':old.get('qdrant_image',read(e/'live-instance.json')['image']),'deterministic_tests':units,'native_session_ids':{k:v['thread_ids'] for k,v in evidence.items()},'adapter_connect_timeout_seconds':1.5,'adapter_read_timeout_seconds':5,'timeout_fault_total_seconds':next(x['detail']['elapsed'] for x in boundaries['checks'] if x['name']=='committed_response_lost_unknown'),'not_verified':['Codex desktop App fresh chat','Human personally editing via dashboard','Antigravity integration and full B1-B3','Cross-machine restore and GitHub publication'],'limitations':['First simple discovery scenario did not call memory; retained as not-triggered evidence, nontrivial design scenario passed.','Boundary harness first read the wrong item envelope, then used mismatched source causing a proposal; fixtures corrected without server changes.','Native fresh CLI sessions inherit real home instructions but do not prove desktop App triggers on every task.','Source edition remains existing 1.2.2; new shared-memory component version is 0.1.0.','No independent reviewer was invoked; this receipt is executor verification.','Continuity capsule NOT_ARMED; this receipt is not a continuity capsule.'],'recovery':'baseline/codex holds prior control assets; compare current/final hashes before reverting this integration only. Preserve runtime and memories; do not overwrite later changes.'}
    receipt['model']={**old['model'],'identity_basis':'unchanged service paths and 1A pinned model identity; protected model file metadata checked this round','model_hashes_recomputed_this_round':False,'current_cuda_inference_verified':True,'cpu_probe_repeated_this_round':False}
    receipt['dependencies']={key:old['dependencies'][key] for key in ['python','packages','lock_sha256']}
    receipt['spec_sha256']={'baseline_v030':old['spec_sha256'],'supplement_v031':sha(ROOT/'design/2026-11-07/共同实施规格书-1B接入补充-v0.3.1.md'),'client_contract_v010':sha(ROOT/'docs/客户端调用规范.md')}
    write(e/'phase1b-codex-receipt.json',receipt)
    write(e/'status.json',{'phase':'1B-Codex','status':'FROZEN','codex_status':'已验证','overall_1b_status':'部分验证','daily_enabled':True,'codex_single_client_qualified':True,'phase1b_overall_qualified':False,'antigravity_modified':False,'continuity_protection':'NOT_ARMED','receipt':'phase1b-codex-receipt.json'})
    print(json.dumps({'codex_single_client':'已验证','whole_phase1b':'部分验证','daily_enabled':True,'source_files_hashed':len(hashes),'protected_metadata_unchanged':len(protected),'phase1a_hashes_match':len(old['source_sha256'])},ensure_ascii=False))

if __name__=='__main__':main()
