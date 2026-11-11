"""共用草稿命令入口。客户端、实例明确选择；不修改任何 Harness。"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hub.paths import checked_path
from clients.memory_cli import call_hub


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client',required=True,choices=['codex','antigravity'])
    instance=parser.add_mutually_exclusive_group(required=True)
    instance.add_argument('--profile',choices=['personal'])
    instance.add_argument('--runtime',type=Path)
    parser.add_argument('--base-url')
    parser.add_argument('--auth-dir',type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    get=sub.add_parser('get');get.add_argument('--id',required=True);get.add_argument('--workspace')
    put=sub.add_parser('suggest');put.add_argument('--id',required=True);put.add_argument('--workspace');put.add_argument('--session-id')
    source=put.add_mutually_exclusive_group(required=True);source.add_argument('--stdin',action='store_true');source.add_argument('--request-file',type=Path)
    result=sub.add_parser('result');result.add_argument('--request-key',required=True);result.add_argument('--workspace')
    args=parser.parse_args(argv)
    runtime=checked_path(ROOT/'runtime/personal-0.4.0' if args.profile else args.runtime)
    auth=checked_path(args.auth_dir or (ROOT/'runtime/codex/config' if args.profile else runtime/'config'))
    if not args.profile and not args.base_url:parser.error('独立实例必须明确 --base-url，禁止误连正式服务。')
    base=args.base_url or 'http://127.0.0.1:8765'
    address=urlsplit(base)
    if address.scheme!='http' or address.hostname!='127.0.0.1' or address.username or address.password or address.path not in ('','/') or address.query or address.fragment:
        parser.error('仅允许明确的 http://127.0.0.1:<port> 本机服务地址。')
    common={'client_name':args.client,'base_url':base,'tokens_path':auth/'client_tokens.json'}
    params={'workspace_path':args.workspace} if args.workspace else None
    if args.command=='get':response=call_hub('GET','/v1/drafts/'+quote(args.id,safe=''),params=params,**common)
    elif args.command=='result':
        response=call_hub('GET','/v1/draft-requests/'+quote(args.request_key,safe=''),params=params,**common)
        if response.get('http_status') in (200,410):
            pending=checked_path(runtime/'client_requests'/args.client/(hashlib.sha256(args.request_key.encode()).hexdigest()+'.json'))
            if pending.exists():pending.unlink()
    else:
        payload=json.load(sys.stdin) if args.stdin else json.loads(checked_path(args.request_file).read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict) or not payload.get('request_key'):parser.error('请求正文必须包含固定 request_key。')
        session=os.environ.get('CODEX_THREAD_ID') if args.client=='codex' else args.session_id
        if not session:parser.error('缺少真实宿主会话标识，停止写入；不可生成占位标识。')
        if payload.get('session_id') and payload['session_id']!=session:parser.error('请求会话与当前宿主不一致。')
        payload['session_id']=session
        if args.workspace:payload['workspace_path']=args.workspace
        path='/v1/drafts/'+quote(args.id,safe='')+'/suggestions'
        record={'base_url':base,'path':path,'client':args.client,'payload':payload}
        pending=checked_path(runtime/'client_requests'/args.client/(hashlib.sha256(str(payload['request_key']).encode()).hexdigest()+'.json'))
        pending.parent.mkdir(parents=True,exist_ok=True)
        if pending.exists() and json.loads(pending.read_text(encoding='utf-8'))!=record:parser.error('此请求键已有待核对原请求，禁止改写后重发。')
        if not pending.exists():
            with pending.open('x',encoding='utf-8') as stream:json.dump(record,stream,ensure_ascii=False,indent=2)
        response=call_hub('POST',path,payload=payload,**common)
        if response.get('status')=='WRITE_RESULT_UNKNOWN':response['pending_request_file']=str(pending)
        else:pending.unlink()  # Only this command's own completed request journal, never user input.
    print(json.dumps(response,ensure_ascii=False,indent=2))
    return 2 if response.get('http_status',200)>=400 or response.get('status') in ('MEMORY_HUB_UNAVAILABLE','WRITE_RESULT_UNKNOWN','error') else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'error','message':str(exc)},ensure_ascii=False));raise SystemExit(2)
