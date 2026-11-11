"""Existing authentication and Origin/CSRF protect the draft API too."""
import hashlib
import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse


def register_draft_routes(app,get_db,require_admin,resolve_caller,get_context):
    @app.get('/v1/admin/help')
    def help_page(request:Request):
        require_admin(request)
        root=Path(__file__).resolve().parents[1]
        source=root/'docs/使用与系统说明.md'
        text=source.read_text(encoding='utf-8')
        manifest=json.loads((root/'deployment-manifest.json').read_text(encoding='utf-8'))
        if manifest.get('candidate_manifest'):
            manifest=json.loads((root/manifest['candidate_manifest']).read_text(encoding='utf-8'))
        return {'content':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),
                'service_version':app.version,'workbench_version':manifest['workbench_version'],
                'schema_version':manifest['schema_version'],'harness_center_routing':manifest['harness_center_routing'],
                'collaboration':get_context()}

    @app.post('/v1/admin/memories/related')
    def related_candidates(request:Request,body:dict):
        require_admin(request);return get_db().related_candidates(body)

    @app.post('/v1/admin/memories/duplicate-review')
    def duplicate_review(request:Request,body:dict):
        require_admin(request);return get_db().review_duplicate_candidates(body)

    @app.post('/v1/admin/memory/{memory_id}/confirm-review')
    def confirm_review(memory_id:str,request:Request,body:dict):
        require_admin(request);return get_db().admin_confirm_review(memory_id,body)

    @app.get('/v1/admin/drafts')
    def draft_list(request:Request,status:str='open',offset:int=0,limit:int=25):
        require_admin(request);return get_db().admin_draft_list(status,offset,limit)

    @app.post('/v1/admin/drafts')
    def create_draft(request:Request,body:dict):
        require_admin(request);return get_db().admin_draft_create(body)

    @app.get('/v1/admin/drafts/{draft_id}')
    def draft_detail(draft_id:str,request:Request):
        require_admin(request);return get_db().draft_detail(draft_id)

    @app.post('/v1/admin/drafts/{draft_id}/purge')
    def purge_draft(draft_id:str,request:Request,body:dict):
        require_admin(request);result=get_db().admin_draft_purge(draft_id,body)
        return JSONResponse(result,status_code=202 if result['status']=='purge_pending' else 200)

    @app.post('/v1/admin/drafts/{draft_id}/{action}')
    def draft_action(draft_id:str,action:str,request:Request,body:dict):
        require_admin(request);return get_db().admin_draft_action(draft_id,action,body)

    @app.get('/v1/drafts/{draft_id}')
    def client_draft(draft_id:str,request:Request,workspace_path:str|None=None):
        identity=resolve_caller(request)
        if identity.actor_role=='user_admin':return get_db().draft_detail(draft_id)
        return get_db().draft_detail(draft_id,identity.actor_id,identity.host_id,workspace_path)

    @app.post('/v1/drafts/{draft_id}/suggestions')
    def suggest(draft_id:str,request:Request,body:dict):
        identity=resolve_caller(request)
        from .db import HubError
        if identity.actor_role!='agent_client':raise HubError(403,'agent_client_required','建议必须记录真实客户端身份。')
        return get_db().draft_suggest(draft_id,identity.actor_id,identity.host_id,body)

    @app.get('/v1/draft-requests/{request_key}')
    def request_result(request_key:str,request:Request,workspace_path:str|None=None):
        identity=resolve_caller(request)
        return get_db().draft_request_result(request_key,identity.actor_id,identity.host_id,workspace_path)
