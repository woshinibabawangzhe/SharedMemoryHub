"""Memory-only drafts. Client suggestions are data; only an administrator can apply them."""
from __future__ import annotations

import hashlib
import json
import uuid

from .db import HubError, SECRET_PATTERNS, utc_now_iso

DRAFT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_drafts (
 draft_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
 raw_text TEXT NOT NULL, fields_json TEXT NOT NULL, target_memory_id TEXT,
 base_memory_revision INTEGER, grants_json TEXT NOT NULL DEFAULT '[]',
 result_memory_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_versions (
 draft_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(draft_id,revision)
);
CREATE TABLE IF NOT EXISTS draft_suggestions (
 suggestion_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, base_draft_revision INTEGER NOT NULL,
 base_memory_revision INTEGER, client_name TEXT NOT NULL, session_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_suggestions ON draft_suggestions(draft_id,created_at);
CREATE TABLE IF NOT EXISTS draft_requests (
 request_key TEXT PRIMARY KEY, actor_id TEXT NOT NULL, body_hash TEXT NOT NULL,
 draft_id TEXT NOT NULL, result_json TEXT, is_purged INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
"""

EDITABLE = {'title','content','subject','scope','project_id','kind','topic_key','confidence',
            'domain_id','tags','readers','host_constraints','expires_at','review_due_at'}
BOUNDARY = ('subject','scope','project_id','readers','host_constraints')
DEFAULT_FIELDS = {'subject':'user','scope':'user_global','project_id':None,'kind':'context',
                  'topic_key':'general','confidence':'hypothesis','domain_id':None,'tags':[],
                  'readers':['user','codex','antigravity'],'host_constraints':['any'],
                  'expires_at':None,'review_due_at':None}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))


def check_text(value, maximum=8000):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise HubError(422,'invalid_draft_text',f'请填写非空文字，最多 {maximum} 字。')
    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise HubError(422,'sensitive_credential_rejected','疑似凭据禁止保存到草稿或建议。')
    return value.strip()


class DraftsMixin:
    def _draft_row(self, con, draft_id):
        row=con.execute('SELECT * FROM memory_drafts WHERE draft_id=?',(draft_id,)).fetchone()
        if not row:
            purged=con.execute('SELECT 1 FROM purge_tombstones WHERE memory_id=?',(draft_id,)).fetchone()
            raise HubError(410 if purged else 404,'draft_purged' if purged else 'draft_not_found','草稿已清除。' if purged else '草稿不存在。')
        return dict(row)

    def _draft_revision(self, row, payload):
        if payload.get('expected_revision')!=row['revision']:
            raise HubError(409,'DraftRevisionConflict','草稿版本已变化，请重新读取；未覆盖原话。')

    def _draft_authorize(self, con, row, client_name, host_id, workspace_path):
        if client_name not in json.loads(row['grants_json']):
            raise HubError(403,'draft_access_denied','此草稿未授权给当前客户端。')
        project,_=self.resolve_bound_project(workspace_header=workspace_path)
        fields=json.loads(row['fields_json'])
        target=con.execute('SELECT * FROM current_memory WHERE memory_id=?',(row['target_memory_id'],)).fetchone() if row['target_memory_id'] else None
        if row['target_memory_id'] and (not target or target['state']=='deleted' or not self._check_row_authorized(target,'agent_client',client_name,host_id,project,True)):
            raise HubError(403,'draft_target_denied','目标记忆当前不可供该客户端协作。')
        access={**fields,'state':'active','confidence':'confirmed','expires_at':None,
                'created_by_actor_id':target['created_by_actor_id'] if target else 'user','readers_json':json.dumps(fields['readers']),
                'host_constraints_json':json.dumps(fields['host_constraints'])}
        if not self._check_row_authorized(access,'agent_client',client_name,host_id,project,True):
            raise HubError(403,'draft_scope_denied','当前客户端或工作区不符合草稿适用范围。')

    def _draft_snapshot(self, con, row):
        con.execute('INSERT INTO draft_versions VALUES(?,?,?,?)',
                    (row['draft_id'],row['revision'],canonical(row),utc_now_iso()))

    def _draft_transaction(self, action, draft_id, actor_id, payload, operation):
        key=payload.get('request_key')
        if not isinstance(key,str) or not key.strip() or len(key)>200:
            raise HubError(422,'missing_idempotency_key','必须提供 request_key。')
        body_hash=hashlib.sha256(canonical({'action':action,'id':draft_id,'body':payload}).encode()).hexdigest()
        with self._write_lock:
            con=self.connect()
            try:
                con.execute('BEGIN IMMEDIATE')
                previous=con.execute('SELECT * FROM draft_requests WHERE request_key=?',(key,)).fetchone()
                if previous:
                    if previous['actor_id']!=actor_id:raise HubError(403,'idempotency_owner_mismatch','请求键属于其他调用者。')
                    if previous['is_purged']:raise HubError(410,'draft_purged','关联内容已清除，禁止重放。')
                    if previous['body_hash']!=body_hash:raise HubError(422,'IdempotencyKeyPayloadMismatch','同一请求键不能携带不同内容。')
                    if actor_id!='user':self._draft_authorize(con,self._draft_row(con,previous['draft_id']),actor_id,actor_id,payload.get('workspace_path'))
                    result=json.loads(previous['result_json']);result['idempotent_replay']=True
                    return result
                result=operation(con)
                con.execute('INSERT INTO draft_requests VALUES(?,?,?,?,?,0,?)',
                            (key,actor_id,body_hash,result['draft_id'],canonical(result),utc_now_iso()))
                con.commit()
                return result
            finally:
                if con.in_transaction:con.rollback()
                con.close()

    def admin_draft_create(self,payload):
        raw=check_text(payload.get('raw_text'))
        def create(con):
            fields=dict(DEFAULT_FIELDS);target_id=payload.get('target_memory_id');base=None
            if target_id:
                old=self._admin_row(target_id)
                if old['state']=='deleted':raise HubError(409,'target_deleted','先恢复记忆，再创建修改草稿。')
                if payload.get('expected_memory_revision')!=old['revision']:
                    raise HubError(409,'RevisionConflict','目标记忆已变化，请重新打开。')
                fields.update({k:v for k,v in self.format_memory_item(old).items() if k in EDITABLE})
                base=old['revision']
            requested=payload.get('fields',{})
            if not isinstance(requested,dict) or set(requested)-EDITABLE:raise HubError(422,'invalid_fields','草稿字段无效。')
            # A correction starts from the actual target boundary, not a client-supplied substitute.
            if target_id and any(k in requested and requested[k]!=fields[k] for k in BOUNDARY):
                raise HubError(422,'target_scope_mismatch','修改草稿必须从原记忆范围开始。')
            fields.update(requested);self._validate_draft_fields(fields)
            if not target_id and fields['scope']=='agent_private':raise HubError(422,'private_owner_required','新增单端私有记忆请使用原有客户端入口，不能伪造所属客户端。')
            now=utc_now_iso();did='draft_'+uuid.uuid4().hex[:24]
            con.execute('INSERT INTO memory_drafts VALUES(?,1,?,?,?,?,?,?,NULL,?,?)',
                        (did,'draft',raw,canonical(fields),target_id,base,'[]',now,now))
            row=self._draft_row(con,did);self._draft_snapshot(con,row)
            return {'status':'draft_saved','draft_id':did,'revision':1}
        return self._draft_transaction('create',None,'user',payload,create)

    def _validate_draft_fields(self,fields):
        for key in ('title','content','subject','scope','kind','topic_key','confidence'):
            if key in fields and not isinstance(fields[key],str):raise HubError(422,'invalid_fields','文本字段必须为字符串。')
        if fields.get('confidence') not in ('confirmed','hypothesis'):raise HubError(422,'invalid_confidence','请明确已确认或待核实。')
        if fields.get('kind') not in ('preference','decision','fact','context','experience','handoff','rule_candidate'):raise HubError(422,'invalid_kind','记忆类型无效。')
        if fields.get('subject') not in self.get_meta('allowed_subjects',[]):raise HubError(422,'invalid_subject','主体未登记。')
        if fields.get('scope') not in ('global','user_global','project','agent_private'):raise HubError(422,'invalid_scope','适用范围无效。')
        if fields['scope']=='project' and fields.get('project_id') not in self.get_meta('project_registry',{}).values():raise HubError(422,'invalid_project','项目必须先登记工作区。')
        for key in ('readers','host_constraints'):
            if not isinstance(fields.get(key),list) or not fields[key] or not all(isinstance(v,str) and v for v in fields[key]):raise HubError(422,'invalid_access_metadata','读者和宿主约束须为非空列表。')
        self.validate_organization(fields)
        check_text(canonical(fields),16000)

    def draft_detail(self,draft_id,client_name=None,host_id=None,workspace_path=None):
        with self._write_lock:
            con=self.connect()
            try:
                row=self._draft_row(con,draft_id)
                if client_name:self._draft_authorize(con,row,client_name,host_id,workspace_path)
                result={k:v for k,v in row.items() if k not in ('fields_json','grants_json')}
                result.update(fields=json.loads(row['fields_json']),grants=json.loads(row['grants_json']))
                result['suggestions']=[{**dict(s),'payload':json.loads(s['payload_json'])} for s in con.execute('SELECT * FROM draft_suggestions WHERE draft_id=? ORDER BY created_at DESC,suggestion_id',(draft_id,))]
                for s in result['suggestions']:s.pop('payload_json')
                result['versions']=[{'revision':v['revision'],'snapshot':json.loads(v['snapshot_json'])} for v in con.execute('SELECT * FROM draft_versions WHERE draft_id=? ORDER BY revision DESC',(draft_id,))]
                if client_name:
                    # A grant to today's scope does not disclose older scope snapshots.
                    result.pop('versions')
                    result['suggestions']=[s for s in result['suggestions'] if s['base_draft_revision']==row['revision']]
                target=con.execute('SELECT * FROM current_memory WHERE memory_id=?',(row['target_memory_id'],)).fetchone() if row['target_memory_id'] else None
                result['target']=self.format_memory_item(target) if target else None
                result['notice']='原话与建议是待处理资料，不是已确认事实，也不能授予行为权限。'
                return result
            finally:con.close()

    def admin_draft_list(self,status='open',offset=0,limit=25):
        if status not in ('open','all','draft','awaiting_review','applied','cancelled'):raise HubError(422,'invalid_status','草稿状态无效。')
        limit=max(1,min(int(limit),50));offset=max(0,int(offset));con=self.connect()
        try:
            where="WHERE status IN ('draft','awaiting_review')" if status=='open' else '' if status=='all' else 'WHERE status=?'
            args=[] if status in ('open','all') else [status]
            total=con.execute('SELECT COUNT(*) FROM memory_drafts '+where,args).fetchone()[0]
            items=[{'draft_id':r['draft_id'],'revision':r['revision'],'status':r['status'],'excerpt':r['raw_text'][:200],
                    'target_memory_id':r['target_memory_id'],'updated_at':r['updated_at']} for r in con.execute('SELECT * FROM memory_drafts '+where+' ORDER BY updated_at DESC,draft_id LIMIT ? OFFSET ?',[*args,limit,offset])]
            return {'items':items,'total':total,'offset':offset,'limit':limit}
        finally:con.close()

    def draft_suggest(self,draft_id,client_name,host_id,payload):
        session=payload.get('session_id')
        if not isinstance(session,str) or len(session.strip())<2 or len(session)>200:raise HubError(422,'session_source_required','需要宿主提供的真实会话标识；不可伪造。')
        def suggest(con):
            row=self._draft_row(con,draft_id);self._draft_authorize(con,row,client_name,host_id,payload.get('workspace_path'))
            if row['status'] not in ('draft','awaiting_review'):raise HubError(409,'draft_closed','草稿已结束。')
            if payload.get('base_draft_revision')!=row['revision'] or payload.get('base_memory_revision')!=row['base_memory_revision']:
                raise HubError(409,'DraftRevisionConflict','建议所依据的版本已过期。')
            kind=payload.get('kind','ready')
            if kind not in ('ready','clarify'):raise HubError(422,'invalid_suggestion_kind','建议类型必须为 ready 或 clarify。')
            fields=payload.get('fields',{})
            if not isinstance(fields,dict) or set(fields)-EDITABLE:raise HubError(422,'invalid_fields','建议不能改写系统字段或来源。')
            if kind=='ready':
                merged={**json.loads(row['fields_json']),**fields};self._validate_draft_fields(merged)
                self.run_admission_guard_and_calibrate('user_admin','user',{**merged,'source':{'origin_type':'admin_manual'}},None)
            body={'kind':kind,'fields':fields,'reason':check_text(payload.get('reason'),2000),'questions':payload.get('questions',[])}
            if not isinstance(body['questions'],list) or len(body['questions'])>10 or any(not isinstance(q,str) or len(q)>500 for q in body['questions']):raise HubError(422,'invalid_questions','最多10个澄清问题，每个最多500字。')
            if kind=='clarify' and not body['questions']:raise HubError(422,'questions_required','请提供需要澄清的问题。')
            check_text(canonical(body),20000)
            sid='sug_'+uuid.uuid4().hex[:24];now=utc_now_iso()
            con.execute('INSERT INTO draft_suggestions VALUES(?,?,?,?,?,?,?,?,?)',(sid,draft_id,row['revision'],row['base_memory_revision'],client_name,session,canonical(body),'pending',now))
            con.execute("UPDATE memory_drafts SET status='awaiting_review',updated_at=? WHERE draft_id=?",(now,draft_id))
            return {'status':'suggestion_saved','draft_id':draft_id,'suggestion_id':sid,'revision':row['revision']}
        # Replays must still satisfy current access, including revoked grants.
        self.draft_detail(draft_id,client_name,host_id,payload.get('workspace_path'))
        return self._draft_transaction('suggest',draft_id,client_name,payload,suggest)

    def admin_draft_action(self,draft_id,action,payload):
        if action not in ('edit','grant','accept','reject','cancel','restore'):raise HubError(404,'invalid_draft_action','未知草稿操作。')
        def mutate(con):
            row=self._draft_row(con,draft_id);self._draft_revision(row,payload)
            fields=json.loads(row['fields_json']);now=utc_now_iso()
            if row['status']=='applied':raise HubError(409,'draft_already_applied','草稿已采纳，请在记忆详情继续管理。')
            if row['status']=='cancelled' and action!='restore':raise HubError(409,'draft_cancelled','先恢复草稿再操作。')
            if action=='accept':return self._draft_accept(con,row,payload)
            if action=='edit':
                updates=payload.get('fields',{})
                if not isinstance(updates,dict) or set(updates)-EDITABLE:raise HubError(422,'invalid_fields','草稿字段无效。')
                if any(k in updates and updates[k]!=fields[k] for k in BOUNDARY) and not payload.get('confirm_scope_change'):raise HubError(409,'scope_confirmation_required','适用边界变化须单独确认。')
                fields.update(updates);self._validate_draft_fields(fields)
                row.update(raw_text=check_text(payload.get('raw_text',row['raw_text'])),fields_json=canonical(fields),status='draft')
                if payload.get('rebase'):
                    if row['target_memory_id']:
                        old=self._admin_row(row['target_memory_id'])
                        if payload.get('expected_memory_revision')!=old['revision']:raise HubError(409,'RevisionConflict','目标修订不匹配。')
                        row['base_memory_revision']=old['revision']
                        fields={k:v for k,v in self.format_memory_item(old).items() if k in EDITABLE}
                        fields.update(updates);self._validate_draft_fields(fields);row['fields_json']=canonical(fields)
            elif action=='grant':
                clients=payload.get('clients')
                if not isinstance(clients,list) or any(c not in ('codex','antigravity') for c in clients):raise HubError(422,'invalid_clients','请选择支持的客户端。')
                if any(c not in fields['readers'] and 'any' not in fields['readers'] for c in clients):raise HubError(403,'draft_scope_denied','授权不能扩大现有读者范围。')
                row['grants_json']=canonical(sorted(set(clients)))
            elif action=='reject':
                applied=con.execute("UPDATE draft_suggestions SET status='rejected' WHERE draft_id=? AND suggestion_id=? AND status='pending'",(draft_id,payload.get('suggestion_id')))
                if applied.rowcount!=1:raise HubError(409,'suggestion_not_pending','建议不存在或已处理。')
                # Rejection preserves other suggestions' content baseline.
                return {'status':'suggestion_rejected','draft_id':draft_id,'revision':row['revision']}
            else:row['status']='cancelled' if action=='cancel' else 'draft'
            row.update(revision=row['revision']+1,updated_at=now)
            con.execute('UPDATE memory_drafts SET revision=?,status=?,raw_text=?,fields_json=?,base_memory_revision=?,grants_json=?,updated_at=? WHERE draft_id=?',
                        (row['revision'],row['status'],row['raw_text'],row['fields_json'],row['base_memory_revision'],row['grants_json'],now,draft_id))
            self._draft_snapshot(con,row)
            return {'status':row['status'],'draft_id':draft_id,'revision':row['revision']}
        return self._draft_transaction(action,draft_id,'user',payload,mutate)

    def _draft_accept(self,con,row,payload):
        self._ensure_not_reindexing()
        s=con.execute("SELECT * FROM draft_suggestions WHERE draft_id=? AND suggestion_id=? AND status='pending'",(row['draft_id'],payload.get('suggestion_id'))).fetchone()
        if not s:raise HubError(409,'suggestion_not_pending','请先取得一份待确认建议。')
        suggested=json.loads(s['payload_json'])
        if suggested['kind']!='ready':raise HubError(409,'clarification_required','需要先完成澄清，不能把问题采纳为记忆。')
        if s['base_draft_revision']!=row['revision'] or s['base_memory_revision']!=row['base_memory_revision']:
            raise HubError(409,'stale_suggestion','草稿已变化，请让 AI 基于最新原话重新整理。')
        overrides=payload.get('final_fields',{})
        if not isinstance(overrides,dict) or set(overrides)-EDITABLE:raise HubError(422,'invalid_fields','不可改写系统字段或来源。')
        original=json.loads(row['fields_json']);fields={**original,**suggested['fields'],**overrides}
        self._validate_draft_fields(fields)
        if fields['scope']=='agent_private' and (not row['target_memory_id'] or original['scope']!='agent_private'):
            raise HubError(422,'private_owner_required','不能通过草稿伪造单端私有记忆归属。')
        if any(fields[k]!=original[k] for k in BOUNDARY) and not payload.get('confirm_scope_change'):
            raise HubError(409,'scope_confirmation_required','范围或可用客户端发生变化，需要单独确认。')
        source={'origin_type':'admin_manual','draft_id':row['draft_id'],'draft_revision':row['revision'],
                'suggestion_id':s['suggestion_id'],'collaborator':s['client_name'],'session_id':s['session_id']}
        fields['source']=source
        if row['target_memory_id']:
            old=self._admin_row(row['target_memory_id'])
            if old['revision']!=row['base_memory_revision'] or payload.get('expected_memory_revision')!=old['revision']:
                raise HubError(409,'RevisionConflict','原记忆已有新修订，不能覆盖；请重新基于最新版本整理。')
            if old['state']=='deleted':raise HubError(409,'target_deleted','原记忆已删除，先人工恢复。')
            if old['locked'] and not payload.get('confirm_locked_edit'):raise HubError(409,'LockedConflict','目标记忆已锁定，须由用户明确确认本次人工修改。')
            fields['locked']=old['locked']
            result=self._admin_commit(old,fields,old['revision'],'update',_con=con)
        else:
            # The same validated creation/deduplication path, inside the draft transaction.
            result=self.write_memory('user_admin','user','web_ui',
                  {**fields,'operation':'create','expected_revision':0,'request_key':'draft_apply_'+payload['request_key']},_con=con)
        self._draft_finish(con,row,s,result)
        return {**result,'draft_id':row['draft_id'],'draft_revision':row['revision']+1}

    def _draft_finish(self,con,row,suggestion,result):
        con.execute("UPDATE draft_suggestions SET status='accepted' WHERE suggestion_id=?",(suggestion['suggestion_id'],))
        con.execute("UPDATE memory_drafts SET status='applied',revision=revision+1,result_memory_id=?,updated_at=? WHERE draft_id=?",
                    (result['memory_id'],utc_now_iso(),row['draft_id']))
        self._draft_snapshot(con,self._draft_row(con,row['draft_id']))

    def draft_request_result(self,key,actor_id,host_id=None,workspace_path=None):
        con=self.connect()
        try:
            row=con.execute('SELECT * FROM draft_requests WHERE request_key=?',(key,)).fetchone()
            if not row:raise HubError(404,'request_not_found','未找到已提交结果；只能使用原键与原请求重试。')
            if row['actor_id']!=actor_id:raise HubError(403,'request_access_denied','无权读取其他调用者的请求结果。')
            if row['is_purged']:raise HubError(410,'draft_purged','关联内容已清除。')
            if actor_id!='user':self._draft_authorize(con,self._draft_row(con,row['draft_id']),actor_id,host_id,workspace_path)
            return json.loads(row['result_json'])
        finally:con.close()

    def admin_draft_purge(self,draft_id,payload):
        # A draft uses its own object ID in the existing durable tombstone chain.
        with self._index_lock,self._write_lock:
            key=payload.get('request_key')
            if not isinstance(key,str) or not key:raise HubError(422,'missing_idempotency_key','永久清除必须保留 request_key。')
            con=self.connect()
            try:
                prior=con.execute('SELECT * FROM draft_requests WHERE request_key=?',(key,)).fetchone()
                if prior and prior['is_purged']:
                    if prior['draft_id']!=draft_id or prior['actor_id']!='user':raise HubError(403,'request_access_denied','请求不匹配。')
                    body_hash=hashlib.sha256(canonical({'action':'purge','id':draft_id,'body':payload}).encode()).hexdigest()
                    if prior['body_hash']!=body_hash:raise HubError(422,'IdempotencyKeyPayloadMismatch','清除重试不能改变原请求。')
                    return {**self._complete_purge(draft_id),'draft_id':draft_id}
                row=self._draft_row(con,draft_id);self._draft_revision(row,payload)
                if row['status']!='cancelled':raise HubError(409,'cancel_first','请先取消草稿，再永久清除。已采纳的来源通过关联记忆清除。')
                if payload.get('confirm_purge')!=f"PURGE {draft_id} r{row['revision']}":raise HubError(422,'confirm_purge_required','确认字符串不匹配。')
                digest=hashlib.sha256(row['raw_text'].encode()).hexdigest()
            finally:con.close()
            self._draft_transaction('purge',draft_id,'user',payload,lambda con:{'status':'purge_pending','draft_id':draft_id})
            item=self._append_or_reuse_tombstone_entry(draft_id,digest,[],'user',utc_now_iso())
            self._apply_tombstone(item)
            result=self._complete_purge(draft_id)
            return {**result,'draft_id':draft_id}
