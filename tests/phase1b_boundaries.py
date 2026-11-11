"""真实隔离HTTP边界与丢响应注入；不访问默认库或输出凭据。"""
import argparse,contextlib,hashlib,http.server,importlib.util,io,json,os,socket,subprocess,sys,threading,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from phase1b_live_control import admin_client,readmeta
ADAPTER=Path(os.environ['SMH_CODEX_ADAPTER'])
spec=importlib.util.spec_from_file_location('adapter',ADAPTER);adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);a=parser.parse_args()
    evidence=a.evidence;meta=readmeta(evidence);profile=evidence/'trial-profile.json';results=[]
    assert os.environ.get('CODEX_THREAD_ID'),'real session required'
    def check(name,condition,detail):
        results.append({'name':name,'passed':bool(condition),'detail':detail})
        (evidence/'boundaries-progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        assert condition,(name,detail)
    def invoke(*args,body=None,workspace=True,profile_path=profile):
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()),contextlib.ExitStack() as stack:
            if body is not None:
                from unittest.mock import patch
                stack.enter_context(patch('sys.stdin',io.StringIO(json.dumps(body,ensure_ascii=False))))
            code=adapter.main(['--profile',str(profile_path),*(['--workspace',meta['workspace']] if workspace else ['--no-workspace']),*args])
        return code,json.loads(out.getvalue())
    def request(**extra):
        return {'request_key':'1b-'+uuid.uuid4().hex,'operation':'create','subject':'project','scope':'project','kind':'preference','topic_key':'boundary.'+uuid.uuid4().hex,
            'title':'合成边界测试','content':'合成边界偏好：测试报告含预期结果。','confidence':'confirmed','source':{'origin_type':'user_explicit','evidence_ref':'用户批准的合成验收偏好'},**extra}
    with contextlib.closing(admin_client(meta)) as admin:
        code,r=invoke('write',body=request(),workspace=False);check('no_workspace_project_rejected',code!=0,r)
        unregistered=evidence/'unregistered';unregistered.mkdir(exist_ok=True)
        # 确定是真实目录，仍未被服务端登记。
        out=io.StringIO()
        from unittest.mock import patch
        with contextlib.redirect_stdout(out),patch('sys.stdin',io.StringIO(json.dumps(request(),ensure_ascii=False))):
            code=adapter.main(['--profile',str(profile),'--workspace',str(unregistered),'write'])
        r=json.loads(out.getvalue());check('unregistered_project_rejected',code!=0,r)
        code,g=invoke('write',body=request(scope='user_global',subject='user'),workspace=False)
        check('global_paused_pending',code==0 and g.get('current_state')=='paused' and bool(g.get('proposal_id')),g)
        code,rule=invoke('write',body=request(kind='rule_candidate'))
        check('rule_paused_pending',code==0 and rule.get('current_state')=='paused' and bool(rule.get('proposal_id')),rule)
        body=request(kind='handoff',source={'origin_type':'task_state','summary':'合成阶段交接'});code,h=invoke('write',body=body)
        detail=admin.get('/v1/admin/memory/'+h['memory_id']+'/detail').json()
        (evidence/'handoff-detail.json').write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding='utf-8')
        _,row=invoke('get','--id',h['memory_id']);expiry=row.get('item',row).get('expires_at')
        check('handoff_has_ttl',code==0 and bool(expiry),{'write':h,'expires_at':expiry})
        from datetime import datetime,timezone
        check('handoff_bounded_ttl',0<(datetime.fromisoformat(expiry.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()<=30*86400,expiry)
        update={'request_key':'1b-'+uuid.uuid4().hex,'operation':'update','memory_id':h['memory_id'],'expected_revision':1,'content':'合成交接第二版','expires_at':None,'source':body['source']}
        code,changed=invoke('write',body=update);_,row=invoke('get','--id',h['memory_id'])
        check('null_ttl_preserved',code==0 and changed.get('status')=='committed' and changed.get('stored_revision')==2 and row.get('item',{}).get('kind')=='handoff' and bool(row.get('item',row).get('expires_at')),changed)
        stale={**update,'request_key':'1b-'+uuid.uuid4().hex};code,r=invoke('write',body=stale)
        check('stale_revision_rejected',code!=0 and r.get('http_status')==409,r)
        code,expired=invoke('write',body=request(kind='handoff',expires_at='2000-01-01T00:00:00Z',content='过期合成交接唯一校验'))
        _,found=invoke('search','--query','过期合成交接唯一校验')
        check('expired_handoff_not_recalled',expired.get('memory_id') not in [x['memory_id'] for x in found.get('items',[])],{'write':expired,'search':found})
        # 代理先将请求提交给真实服务，再延迟首次响应，模拟“已提交但客户端不知道”。
        state={'forwarded':0,'delayed':False}
        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                import httpx
                data=self.rfile.read(int(self.headers['Content-Length']))
                with httpx.Client(trust_env=False,timeout=15) as client:
                    response=client.post(meta['origin']+self.path,content=data,headers={'Authorization':self.headers.get('Authorization',''),'Content-Type':'application/json'})
                state['forwarded']+=1
                if not state['delayed']:state['delayed']=True;time.sleep(6)
                self.send_response(response.status_code);self.send_header('Content-Length',str(len(response.content)));self.end_headers()
                try:self.wfile.write(response.content)
                except OSError:pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Proxy);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        pp=evidence/'timeout-proxy-profile.json';p=json.loads(profile.read_text(encoding='utf-8'));p['base_url']=f'http://127.0.0.1:{server.server_port}';pp.write_text(json.dumps(p),encoding='utf-8')
        try:
            body=request(topic_key='timeout.unique',content='丢响应合成记忆唯一校验');started=time.monotonic();code,r=invoke('write',body=body,profile_path=pp)
            check('committed_response_lost_unknown',code!=0 and r.get('status')=='WRITE_RESULT_UNKNOWN',{'result':r,'elapsed':round(time.monotonic()-started,3)})
            record=json.loads(Path(r['request_record']).read_text(encoding='utf-8'))
            check('unknown_original_retained',record['status']=='WRITE_RESULT_UNKNOWN' and record['request']['request_key']==body['request_key'],{'request_sha256':record['request_sha256']})
            code,replay=invoke('reconcile','--request-key',body['request_key'],'--confirm-same-request',profile_path=pp)
            check('same_key_reconciliation',code==0 and replay.get('idempotent_replay') is True,replay)
            listed=admin.get('/v1/admin/memories',params={'query':'丢响应合成记忆唯一校验'}).json()
            matches=[x for x in listed.get('items',[]) if x.get('topic_key')=='timeout.unique']
            check('no_duplicate_after_reconcile',len(matches)==1,{'matching_count':len(matches),'forwarded':state['forwarded']})
        finally:server.shutdown();server.server_close();thread.join(2)
    receipt={'evidence_level':'controlled-trial','backend':'Docker Qdrant + real CUDA model','fault_injection':'local forward-then-delay proxy','checks':results,'all_passed':all(x['passed'] for x in results)}
    (evidence/'boundaries.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
