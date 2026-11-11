"""Persistent management queries and metadata; no synthetic adapter in the live service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import re
import uuid

from .db import HubError, parse_iso, utc_now_iso


class WorkbenchMixin:
    def validate_organization(self, fields):
        domain = fields.get('domain_id')
        tags = fields.get('tags', [])
        if domain is not None:
            con = self.connect()
            try:
                if not con.execute('SELECT 1 FROM domains WHERE domain_id=?', (domain,)).fetchone():
                    raise HubError(422, 'unknown_domain', '请选择已登记领域，或保留未分类。')
            finally:
                con.close()
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or not t.strip() or len(t) > 40 for t in tags):
            raise HubError(422, 'invalid_tags', '标签最多20个，每个为1至40字。')
        due = fields.get('review_due_at')
        if due:
            try:
                parse_iso(due)
            except (ValueError, TypeError, AttributeError):
                raise HubError(422, 'invalid_review_date', '复核时间须为ISO日期。')
        return {'domain_id': domain, 'tags': sorted(set(t.strip() for t in tags)), 'review_due_at': due or None}

    def run_admission_guard_and_calibrate(self, actor_role, actor_id, payload, bound_workspace_path):
        self.validate_organization(payload)
        if payload.get('confidence', 'confirmed') not in ('confirmed', 'hypothesis'):
            raise HubError(422, 'invalid_confidence', '确认状态无效。')
        for key in ('readers', 'host_constraints'):
            if key in payload and (not isinstance(payload[key], list) or not payload[key] or any(not isinstance(x, str) or not x for x in payload[key])):
                raise HubError(422, 'invalid_access_metadata', '可用AI与宿主约束不能是空列表。')
        return super().run_admission_guard_and_calibrate(actor_role, actor_id, payload, bound_workspace_path)

    def _link_source(self, con, memory_id, source):
        if not source.get('source_id'):
            return
        row = con.execute('SELECT 1 FROM source_versions WHERE source_id=? AND revision=?',
                          (source['source_id'], source.get('source_revision'))).fetchone()
        if not row:
            raise HubError(422, 'source_version_missing', '引用的来源版本不存在。')
        con.execute('INSERT OR IGNORE INTO source_links VALUES(?,?,?)',
                    (memory_id, source['source_id'], source['source_revision']))

    @staticmethod
    def _duplicate_fields(item):
        fields = ('subject', 'scope', 'project_id', 'kind', 'topic_key', 'confidence', 'expires_at', 'domain_id', 'review_due_at')
        result = {k: item.get(k) for k in fields}
        for key in ('title', 'content'):
            result[key] = (item.get(key) or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        for key in ('tags', 'readers', 'host_constraints'):
            result[key] = sorted(set(item.get(key) or []))
        return result

    def write_memory(self, actor_role, actor_id, host_id, payload, **kwargs):
        result = self._workbench_write(actor_role, actor_id, host_id, payload, **kwargs)
        key = (payload.get('request_key') or payload.get('idempotency_key') or '').strip()
        con = kwargs.get('_con') if kwargs.get('_con') is not None else self.connect()
        try:
            row = con.execute('SELECT result_json FROM requests WHERE request_key=?', (key,)).fetchone()
            if result.get('idempotent_replay') and row and row[0]:
                original = json.loads(row[0])
                original.update(current_state=result['current_state'], idempotent_replay=True)
                return original
            if row and not row[0]:
                con.execute('UPDATE requests SET result_json=? WHERE request_key=? AND result_json IS NULL AND is_purged=0',
                            (json.dumps(result, ensure_ascii=False),key))
            return result
        finally:
            if kwargs.get('_con') is None:con.close()

    def _workbench_write(self, actor_role, actor_id, host_id, payload, **kwargs):
        # The existing single-instance lock + write lock serializes check and creation.
        with self._write_lock:
            if (payload.get('operation') or 'create') != 'create':
                return super().write_memory(actor_role, actor_id, host_id, payload, **kwargs)
            self.check_not_reindexing()
            request_key=(payload.get('request_key') or payload.get('idempotency_key') or '').strip()
            if not request_key:raise HubError(422,'missing_idempotency_key','必须提供请求键。')
            con = kwargs.get('_con') if kwargs.get('_con') is not None else self.connect()
            try:
                if con.execute('SELECT 1 FROM requests WHERE request_key=?', (request_key,)).fetchone():
                    return super().write_memory(actor_role, actor_id, host_id, payload, **kwargs)
                project, workspace = self.resolve_bound_project(workspace_header=payload.get('workspace_path'))
                source_type, confidence, source, expiry = self.run_admission_guard_and_calibrate(actor_role, actor_id, payload, workspace)
                scope = payload.get('scope', 'project')
                if actor_role != 'user_admin' and scope in ('project','agent_private') and payload.get('project_id') and payload['project_id'] != project:
                    raise HubError(403, 'ProjectScopeMismatch', '请求项目与可信工作区绑定不一致。')
                if payload.get('expected_revision') not in (None,0):
                    raise HubError(409,'RevisionConflict','新增的修订基线必须为0。')
                if scope not in ('global','user_global','project','agent_private') or payload.get('subject','project') not in self.get_meta('allowed_subjects',[]):
                    raise HubError(422,'invalid_scope_or_subject','主体或范围无效。')
                if actor_role != 'user_admin' and scope == 'project' and not project:
                    raise HubError(403, 'ProjectScopeMismatch', '项目写入要求已登记工作区。')
                proposed = {**payload, 'subject': payload.get('subject', 'project'), 'scope': scope,
                            'project_id': payload.get('project_id') if actor_role == 'user_admin' else project if scope in ('project','agent_private') else None,
                            'kind': payload.get('kind', 'fact'), 'topic_key': payload.get('topic_key', 'general'),
                            'confidence': confidence, 'expires_at': expiry,
                            'readers': payload.get('readers', ['user','antigravity','codex']),
                            'host_constraints': payload.get('host_constraints', ['any'])}
                for row in con.execute("SELECT * FROM current_memory WHERE state IN ('active','paused')"):
                    if not self._check_row_authorized(row, actor_role, actor_id, host_id, project, include_non_active=True):
                        continue
                    item = self.format_memory_item(row, con=con)
                    if self._duplicate_fields(item) != self._duplicate_fields(proposed):
                        continue
                    if scope == 'agent_private' and row['created_by_actor_id'] != actor_id:
                        continue
                    if expiry and expiry <= utc_now_iso():
                        continue
                    prop = con.execute("SELECT proposal_id FROM conflict_proposals WHERE memory_id=? AND status='pending'", (row['memory_id'],)).fetchone()
                    pending = row['state'] == 'paused' and prop and row['created_by_actor_id'] == actor_id
                    effective = row['state'] == 'active' and row['confidence'] == 'confirmed' and not row['has_open_conflict']
                    if not (pending or effective):
                        continue
                    body = {k:v for k,v in payload.items() if k not in ('request_key','idempotency_key')}
                    body_hash = 'sha256:' + hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    if not request_key:
                        raise HubError(422, 'missing_idempotency_key', '必须提供请求键。')
                    con.execute('INSERT INTO requests(request_key,actor_id,body_hash,memory_id,applied_revision,status_code,created_at) VALUES(?,?,?,?,?,200,?)',
                                (request_key,actor_id,body_hash,row['memory_id'],row['revision'],utc_now_iso()))
                    return {'status':'paused_pending_approval' if pending else 'duplicate', 'memory_id':row['memory_id'],
                            'stored_revision':row['revision'], 'current_state':row['state'], 'proposal_id':prop[0] if pending else None,
                            'duplicate':True, 'reason':'已有相同正文与适用字段；未覆盖、未续期。'}
            finally:
                if kwargs.get('_con') is None:con.close()
            return super().write_memory(actor_role, actor_id, host_id, payload, **kwargs)

    def workbench_catalog(self):
        con = self.connect()
        try:
            domains = [dict(r) for r in con.execute('SELECT * FROM domains ORDER BY parent_id,name')]
            views = [{**dict(r),'filters':json.loads(r['filters_json'])} for r in con.execute('SELECT * FROM saved_views ORDER BY name')]
            tags = sorted({t for r in con.execute("SELECT tags_json FROM current_memory WHERE state!='deleted'") for t in json.loads(r[0])})
            # Navigation is a read-only projection of the same SQLite records.
            # A parent includes its children; pending proposals count once per memory.
            counts = {key:{'total':0,'domains':{},'projects':{}} for key in ('library','pending','trash','review_due')}
            parents = {d['domain_id']:d['parent_id'] for d in domains}
            rows = con.execute('''SELECT m.memory_id,m.state,m.domain_id,m.project_id,m.review_due_at,
                EXISTS(SELECT 1 FROM conflict_proposals p WHERE p.memory_id=m.memory_id AND p.status='pending') AS pending
                FROM current_memory m''')
            for row in rows:
                groups = ['trash'] if row['state']=='deleted' else ['library']
                if row['state']!='deleted' and row['pending']:groups.append('pending')
                if row['state']!='deleted' and row['review_due_at'] and row['review_due_at']<=utc_now_iso():groups.append('review_due')
                domain = row['domain_id'] or 'unclassified'
                for key in groups:
                    target = counts[key];target['total']+=1
                    for domain_id in {domain,parents.get(domain)}-{None}:
                        target['domains'][domain_id]=target['domains'].get(domain_id,0)+1
                    if row['project_id']:
                        target['projects'][row['project_id']]=target['projects'].get(row['project_id'],0)+1
            return {'domains':domains,'tags':tags,'views':views,
                    'navigation_counts':counts,
                    'projects':[dict(r) for r in con.execute('SELECT project_id,workspace_path FROM project_bindings ORDER BY project_id')]}
        finally:
            con.close()

    def workbench_list(self, filters=None, cursor=None, limit=50, vector_search_fn=None):
        filters = {k:v for k,v in (filters or {}).items() if v not in (None, '')}
        allowed = {'query','state','subject','scope','project_id','kind','domain_id','tag','client','pending','review_due','mode'}
        if set(filters) - allowed:
            raise HubError(422,'unknown_filter','存在不支持的筛选条件。')
        limit = max(1,min(int(limit),100))
        signature = hashlib.sha256(json.dumps(filters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if not hasattr(self,'_cursor_secret'):
            self._cursor_secret = secrets.token_bytes(32)
        offset = 0
        if cursor:
            try:
                raw, mac = cursor.split('.')
                if not hmac.compare_digest(hmac.new(self._cursor_secret,raw.encode(),hashlib.sha256).hexdigest(),mac):raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
                if data['query'] != signature or data['limit'] != limit:raise ValueError()
                offset=int(data['offset'])
                if offset<0:raise ValueError()
            except (ValueError,KeyError,TypeError):
                raise HubError(422,'invalid_cursor','筛选或服务已变化，请从第一页重新查询。')
        con = self.connect()
        try:
            terms=(filters.get('query') or '').strip().casefold().split()
            rows=con.execute('SELECT * FROM current_memory ORDER BY updated_at DESC,memory_id').fetchall()
            items=[]
            for row in rows:
                item=self.format_memory_item(row,con=con)
                if any(item[k] != filters[k] for k in ('subject','scope','project_id','kind') if k in filters):continue
                if filters.get('state') and item['state']!=filters['state']:continue
                if not filters.get('state') and item['state']=='deleted':continue
                if filters.get('domain_id')=='unclassified' and item['domain_id']:continue
                if filters.get('domain_id') not in (None,'unclassified'):
                    descendants=[d[0] for d in con.execute('SELECT domain_id FROM domains WHERE parent_id=?',(filters['domain_id'],))]
                    if item['domain_id'] not in [filters['domain_id'],*descendants]:continue
                if filters.get('tag') and filters['tag'] not in item['tags']:continue
                if filters.get('client') and item['created_by_actor_id']!=filters['client']:continue
                if filters.get('pending') and not item['has_open_conflict']:continue
                if filters.get('review_due') and not (item['review_due_at'] and item['review_due_at']<=utc_now_iso()):continue
                hay='\n'.join(str(item[k]) for k in ('memory_id','title','content','topic_key')).casefold()
                if terms and not all(t in hay for t in terms) and filters.get('mode')!='semantic':continue
                item['matched_by']='literal' if terms else 'browse'
                items.append(item)
            degraded=None
            if filters.get('mode')=='semantic' and terms:
                hits,offline=vector_search_fn(filters['query'],self.get_meta('active_index_generation'),50) if vector_search_fn else ([],True)
                ranks={h['memory_id']:i for i,h in enumerate(hits) if h.get('memory_id')}
                exact=[m for m in items if all(t in (m['title']+'\n'+m['content']).casefold() for t in terms)]
                generation=self.get_meta('active_index_generation')
                eligible=[m for m in items if m['state']=='active' and m['confidence']=='confirmed' and (not m['expires_at'] or parse_iso(m['expires_at'])>parse_iso(utc_now_iso())) and m['memory_id'] in ranks and any(h.get('memory_id')==m['memory_id'] and h.get('revision')==m['revision'] and h.get('generation')==generation for h in hits)]
                eligible.sort(key=lambda m:ranks[m['memory_id']])
                items=exact+[m for m in eligible if m['memory_id'] not in {x['memory_id'] for x in exact}]
                for m in items:m['matched_by']='literal' if m in exact else 'vector'
                degraded='keyword_only' if offline else None
            page=items[offset:offset+limit]
            next_cursor=None
            if offset+limit<len(items):
                raw=base64.urlsafe_b64encode(json.dumps({'query':signature,'limit':limit,'offset':offset+limit}).encode()).decode().rstrip('=')
                next_cursor=raw+'.'+hmac.new(self._cursor_secret,raw.encode(),hashlib.sha256).hexdigest()
            return {'items':page,'count':len(page),'total':len(items),'has_more':bool(next_cursor),'next_cursor':next_cursor,'filters':filters,'degraded_mode':degraded,
                    'pagination_notice':'当前视图分页；并发修改后可能改变后续页，不是备份快照。'}
        finally:
            con.close()

    def workbench_domain(self, payload):
        with self._write_lock:
            con=self.connect()
            try:
                name=(payload.get('name') or '').strip()
                if not name or len(name)>40:raise HubError(422,'invalid_name','领域名称为1至40字。')
                domain_id=payload.get('domain_id') or 'domain_'+uuid.uuid4().hex[:12]
                row=con.execute('SELECT * FROM domains WHERE domain_id=?',(domain_id,)).fetchone()
                if row:
                    if payload.get('expected_revision')!=row['revision']:raise HubError(409,'RevisionConflict','领域已改变，请刷新。')
                    con.execute('UPDATE domains SET name=?,revision=revision+1 WHERE domain_id=?',(name,domain_id))
                else:
                    parent=payload.get('parent_id')
                    if parent:
                        p=con.execute('SELECT * FROM domains WHERE domain_id=?',(parent,)).fetchone()
                        if not p or p['parent_id']:raise HubError(422,'domain_depth','领域最多两级。')
                    con.execute('INSERT INTO domains(domain_id,name,parent_id) VALUES(?,?,?)',(domain_id,name,parent))
                return {'status':'committed','domain_id':domain_id}
            finally:con.close()

    def workbench_save_view(self, payload):
        name=(payload.get('name') or '').strip()
        if not name or len(name)>60:raise HubError(422,'invalid_name','视图名称为1至60字。')
        filters=payload.get('filters') or {}
        self.workbench_list(filters,limit=1)
        vid='view_'+uuid.uuid4().hex[:12]
        con=self.connect()
        try:con.execute('INSERT INTO saved_views(view_id,name,filters_json) VALUES(?,?,?)',(vid,name,json.dumps(filters,ensure_ascii=False)))
        finally:con.close()
        return {'status':'committed','view_id':vid}

    def register_source_snapshot(self, source_id, title, content, original_path=None, original_sha256=None, metadata=None):
        from .db import SECRET_PATTERNS
        if not isinstance(source_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,120}',source_id) or not isinstance(title,str) or not title.strip() or len(title)>200 or not isinstance(content,str) or not content.strip():
            raise HubError(422,'invalid_source','来源标识、标题或正文无效。')
        if not isinstance(metadata or {},dict):raise HubError(422,'invalid_source','来源元数据必须为对象。')
        if any(p.search(json.dumps([title,content,original_path,metadata],ensure_ascii=False)) for p in SECRET_PATTERNS):raise HubError(422,'sensitive_credential_rejected','来源含凭据形状，拒绝保存。')
        if len(content)>100000:raise HubError(422,'source_too_large','只支持小型文本快照。')
        digest=hashlib.sha256(content.encode()).hexdigest()
        with self._write_lock:
            con=self.connect()
            try:
                if con.execute('SELECT 1 FROM source_redactions WHERE source_id=?',(source_id,)).fetchone():
                    raise HubError(410,'source_purged','此来源已永久清除，不能重新登记。')
                old=con.execute('SELECT * FROM source_versions WHERE source_id=? ORDER BY revision DESC LIMIT 1',(source_id,)).fetchone()
                if old and old['sha256']==digest:return {'source_id':source_id,'source_revision':old['revision'],'sha256':digest}
                rev=old['revision']+1 if old else 1
                con.execute('INSERT INTO source_versions VALUES(?,?,?,?,?,?,?,?,?)',(source_id,rev,title,content,digest,original_sha256,original_path,utc_now_iso(),json.dumps(metadata or {},ensure_ascii=False)))
                return {'source_id':source_id,'source_revision':rev,'sha256':digest}
            finally:con.close()

    def workbench_sources(self, source_id=None, revision=None):
        con=self.connect()
        try:
            if source_id:
                row=con.execute('SELECT * FROM source_versions WHERE source_id=? AND revision=?',(source_id,revision)).fetchone()
                if not row:raise HubError(404,'source_missing','来源不存在或已清除。')
                return {'item':{**dict(row),'metadata':json.loads(row['metadata_json'])},'notice':'不可变登记快照；不读取磁盘当前原文。'}
            return {'items':[dict(r) for r in con.execute('SELECT source_id,revision,title,sha256,original_sha256,registered_at FROM source_versions ORDER BY title,revision DESC')]}
        finally:con.close()
