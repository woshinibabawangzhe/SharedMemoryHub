"""Related-memory drafting: explicit members, human review, one atomic application."""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from .db import HubError, utc_now_iso
from .drafts import EDITABLE, BOUNDARY, canonical, check_text

RELATED_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS draft_members (
 draft_id TEXT NOT NULL, memory_id TEXT NOT NULL, base_revision INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL, proposals_json TEXT NOT NULL,
 PRIMARY KEY(draft_id,memory_id));
CREATE TABLE IF NOT EXISTS draft_applications (
 draft_id TEXT PRIMARY KEY, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
 results_json TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS memory_relations (
 memory_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
 draft_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(memory_id,target_id,relation));
CREATE TABLE IF NOT EXISTS duplicate_reviews (
 pair_key TEXT PRIMARY KEY, decision TEXT NOT NULL, members_json TEXT NOT NULL,
 reviewed_at TEXT NOT NULL);
"""

class RelatedMemoryMixin:
    def _members(self, con, draft_id):
        return [dict(r) for r in con.execute('SELECT * FROM draft_members WHERE draft_id=? ORDER BY memory_id',(draft_id,))]

    def _memory_in(self, con, mid):
        row=con.execute('SELECT * FROM current_memory WHERE memory_id=?',(mid,)).fetchone()
        if not row or row['state']=='deleted':
            raise HubError(409,'related_memory_unavailable','关联记忆已删除或清除，请重新整理。')
        return dict(row)

    def _pending(self, con, mid):
        return [dict(r) for r in con.execute("SELECT * FROM conflict_proposals WHERE memory_id=? AND status='pending' ORDER BY proposal_id",(mid,))]

    def _draft_snapshot(self, con, row):
        super()._draft_snapshot(con,row)
        members=self._members(con,row['draft_id'])
        if members:
            snap={**row,'members':members}
            con.execute('UPDATE draft_versions SET snapshot_json=? WHERE draft_id=? AND revision=?',(canonical(snap),row['draft_id'],row['revision']))

    def admin_draft_create(self, payload):
        if 'members' not in payload:return super().admin_draft_create(payload)
        members=payload['members']
        if not isinstance(members,list) or not members or len(members)>50 or any(not isinstance(m,dict) or set(m)!={'memory_id','base_revision'} for m in members):
            raise HubError(422,'invalid_members','请选择1至50条记忆，并提供各条的当前版本。')
        if len({m['memory_id'] for m in members})!=len(members):raise HubError(422,'duplicate_member','同一记忆不能重复加入。')
        raw=check_text(payload.get('raw_text'))
        def create(con):
            captured=[]
            for member in members:
                old=self._memory_in(con,member['memory_id'])
                if old['revision']!=member['base_revision']:raise HubError(409,'RevisionConflict','关联记忆版本已变化，请刷新后重新选择。')
                captured.append((old,self.format_memory_item(old,con=con),self._pending(con,old['memory_id'])))
            first=captured[0][1];fields={k:v for k,v in first.items() if k in EDITABLE}
            self._validate_draft_fields(fields)
            did='draft_'+uuid.uuid4().hex[:24];now=utc_now_iso()
            con.execute('INSERT INTO memory_drafts VALUES(?,1,?,?,?,?,?,?,NULL,?,?)',(did,'draft',raw,canonical(fields),first['memory_id'],first['revision'],'[]',now,now))
            for old,snap,props in captured:
                con.execute('INSERT INTO draft_members VALUES(?,?,?,?,?)',(did,old['memory_id'],old['revision'],canonical(snap),canonical(props)))
            self._draft_snapshot(con,self._draft_row(con,did))
            return {'status':'draft_saved','draft_id':did,'revision':1,'member_count':len(captured)}
        return self._draft_transaction('create',None,'user',payload,create)

    def _draft_authorize(self, con, row, client_name, host_id, workspace_path):
        super()._draft_authorize(con,row,client_name,host_id,workspace_path)
        project,_=self.resolve_bound_project(workspace_header=workspace_path)
        for member in self._members(con,row['draft_id']):
            old=self._memory_in(con,member['memory_id']);base=json.loads(member['snapshot_json'])
            for item in (old,{**old,'subject':base['subject'],'scope':base['scope'],'project_id':base['project_id'],
                              'readers_json':canonical(base['readers']),'host_constraints_json':canonical(base['host_constraints'])}):
                if not self._check_row_authorized(item,'agent_client',client_name,host_id,project,True):
                    raise HubError(403,'draft_member_denied','至少一条关联记忆不允许当前客户端或工作区访问，不能读取整组。')

    def draft_detail(self,draft_id,client_name=None,host_id=None,workspace_path=None):
        result=super().draft_detail(draft_id,client_name,host_id,workspace_path)
        con=self.connect()
        try:
            members=self._members(con,draft_id)
            if members:
                result['members']=[]
                for member in members:
                    base=json.loads(member['snapshot_json']);old=con.execute('SELECT * FROM current_memory WHERE memory_id=?',(member['memory_id'],)).fetchone()
                    result['members'].append({'memory_id':member['memory_id'],'base_revision':member['base_revision'],'baseline':base,
                        'current':self.format_memory_item(old,con=con) if old and old['state']!='deleted' else None,
                        'base_proposals':json.loads(member['proposals_json'])})
                application=con.execute('SELECT * FROM draft_applications WHERE draft_id=?',(draft_id,)).fetchone()
                if application:result['application']={'results':json.loads(application['results_json']),'undone':bool(application['undone'])}
            return result
        finally:con.close()

    def draft_suggest(self,draft_id,client_name,host_id,payload):
        with self._write_lock:
            con=self.connect()
            try:members=self._members(con,draft_id)
            finally:con.close()
            if not members:return super().draft_suggest(draft_id,client_name,host_id,payload)
            self.draft_detail(draft_id,client_name,host_id,payload.get('workspace_path'))
            session=payload.get('session_id')
            if not isinstance(session,str) or not 2<=len(session.strip())<=200:raise HubError(422,'session_source_required','缺少真实会话标识。')
            def suggest(con):
                row=self._draft_row(con,draft_id);self._draft_authorize(con,row,client_name,host_id,payload.get('workspace_path'))
                if row['status'] not in ('draft','awaiting_review'):raise HubError(409,'draft_closed','草稿已结束。')
                if payload.get('base_draft_revision')!=row['revision']:raise HubError(409,'DraftRevisionConflict','草稿已变化，请重新读取。')
                kind=payload.get('kind','ready');changes=payload.get('changes',[]);questions=payload.get('questions',[])
                if kind not in ('ready','clarify'):raise HubError(422,'invalid_suggestion_kind','建议类型无效。')
                if not isinstance(questions,list) or any(not isinstance(q,str) or len(q)>500 for q in questions) or len(questions)>10 or (kind=='clarify' and not questions):raise HubError(422,'invalid_questions','澄清问题无效。')
                if kind=='ready':self._validate_changes(con,members,changes,False)
                body={'kind':kind,'changes':changes,'reason':check_text(payload.get('reason'),2000),'questions':questions}
                check_text(canonical(body),180000)
                sid='sug_'+uuid.uuid4().hex[:24]
                con.execute('INSERT INTO draft_suggestions VALUES(?,?,?,?,?,?,?,?,?)',(sid,draft_id,row['revision'],row['base_memory_revision'],client_name,session,canonical(body),'pending',utc_now_iso()))
                con.execute("UPDATE memory_drafts SET status='awaiting_review',updated_at=? WHERE draft_id=?",(utc_now_iso(),draft_id))
                return {'status':'suggestion_saved','draft_id':draft_id,'suggestion_id':sid,'revision':row['revision']}
            return self._draft_transaction('suggest',draft_id,client_name,payload,suggest)

    def _validate_changes(self,con,members,changes,human):
        ids={m['memory_id'] for m in members}
        if not isinstance(changes,list) or len(changes)!=len(ids) or any(not isinstance(x,dict) for x in changes) or {x.get('memory_id') for x in changes}!=ids:
            raise HubError(422,'invalid_changes','建议必须逐条列出全部成员；无需修改的条目请选择保留。')
        for change in changes:
            if set(change)-{'memory_id','base_revision','action','fields','reason','replacement_id','related_ids','confirmation','proposal_decision','confirm_scope_change','confirm_locked_edit'}:raise HubError(422,'invalid_changes','建议包含不可编辑字段。')
            member=next(m for m in members if m['memory_id']==change['memory_id'])
            if change.get('base_revision')!=member['base_revision']:raise HubError(409,'RevisionConflict','建议所依据的记忆版本不匹配。')
            action=change.get('action','keep')
            if action not in ('keep','update','merge'):raise HubError(422,'invalid_changes','处理方式无效。')
            fields=change.get('fields',{})
            if not isinstance(fields,dict) or set(fields)-EDITABLE:raise HubError(422,'invalid_fields','不可修改来源、版本或系统字段。')
            if action=='keep' and (fields or change.get('replacement_id') or change.get('related_ids')):raise HubError(422,'invalid_keep','保留条目不能夹带修改。')
            snap=json.loads(member['snapshot_json']);merged={**{k:v for k,v in snap.items() if k in EDITABLE},**fields}
            self._validate_draft_fields(merged)
            if human and action!='keep' and change.get('confirmation') not in ('confirmed','hypothesis'):raise HubError(422,'confirmation_required','请明确选择确认内容准确或暂存待核实。')
            if change.get('proposal_decision','keep') not in ('keep','approve','reject'):raise HubError(422,'invalid_proposal_decision','待批提案处理方式无效。')
            if action=='merge' and (change.get('replacement_id') not in ids or change['replacement_id']==change['memory_id']):raise HubError(422,'invalid_replacement','请选择组内另一条记忆作为保留条目。')
            related=change.get('related_ids',[])
            if not isinstance(related,list) or any(mid not in ids or mid==change['memory_id'] for mid in related):raise HubError(422,'invalid_relation','只能关联本组其他条目。')

    def admin_draft_action(self,draft_id,action,payload):
        if action=='undo-apply':return self._undo_group(draft_id,payload)
        if action=='edit' and payload.get('rebase'):
            con=self.connect()
            try:
                if self._members(con,draft_id):raise HubError(409,'group_rebase_required','关联基线已变化时，请保留原话并重新选择成员建立整理草稿。')
            finally:con.close()
        return super().admin_draft_action(draft_id,action,payload)

    def _draft_accept(self,con,row,payload):
        members=self._members(con,row['draft_id'])
        if not members:return super()._draft_accept(con,row,payload)
        self._ensure_not_reindexing()
        s=con.execute("SELECT * FROM draft_suggestions WHERE draft_id=? AND suggestion_id=? AND status='pending'",(row['draft_id'],payload.get('suggestion_id'))).fetchone()
        if not s:raise HubError(409,'suggestion_not_pending','建议已处理或不存在。')
        suggested=json.loads(s['payload_json'])
        if suggested['kind']!='ready':raise HubError(409,'clarification_required','需要先澄清。')
        if s['base_draft_revision']!=row['revision']:raise HubError(409,'stale_suggestion','原话已变化，请重新整理。')
        changes=payload.get('changes',suggested['changes']);self._validate_changes(con,members,changes,True)
        before={};final={};old_relations=[dict(r) for r in con.execute('SELECT * FROM memory_relations') if r['memory_id'] in {m['memory_id'] for m in members}]
        for member in members:
            mid=member['memory_id'];old=self._memory_in(con,mid);props=self._pending(con,mid)
            if old['revision']!=member['base_revision'] or canonical(props)!=member['proposals_json']:raise HubError(409,'RevisionConflict','记忆或待批提案已有变化，整组未保存，请重新预览。')
            before[mid]={'snapshot':{**self._row_to_metadata_snapshot(old),'title':old['title'],'content':old['content'],'state':old['state'],'locked':old['locked']},'proposals':props,'source_links':[dict(r) for r in con.execute('SELECT * FROM source_links WHERE memory_id=?',(mid,))],'relations':[r for r in old_relations if r['memory_id']==mid]}
            change=next(x for x in changes if x['memory_id']==mid);fields=change.get('fields',{})
            if change['action']!='keep':
                if old['locked'] and not change.get('confirm_locked_edit'):raise HubError(409,'LockedConflict','至少一条记忆已锁定，须确认本次人工修改。')
                prior=self._row_to_metadata_snapshot(old)
                if any((sorted(fields.get(k,prior[k]))!=sorted(prior[k]) if k in ('readers','host_constraints') else fields.get(k,prior[k])!=prior[k]) for k in BOUNDARY) and not change.get('confirm_scope_change'):raise HubError(409,'scope_confirmation_required','适用边界变化须单独确认。')
            if change['action']=='keep' and change.get('proposal_decision','keep')!='keep':raise HubError(422,'invalid_keep','要处理提案，请选择修改并预览最终内容。')
            final[mid]=self._validated_snapshot(old,{**fields,**({'confidence':change['confirmation']} if change['action']!='keep' else {})})
        for change in changes:
            if change['action']=='merge':
                mid=change['memory_id'];target=change['replacement_id']
                other=next(c for c in changes if c['memory_id']==target)
                if other['action']!='update':raise HubError(422,'merge_target_review_required','保留条目必须选择修改，明确预览合并后的正文与贡献来源。')
                for field in ('subject','scope','project_id','kind','readers','host_constraints','expires_at'):
                    left,right=final[mid].get(field),final[target].get(field)
                    if field in ('readers','host_constraints'):
                        # Access membership is unordered; saved display order is not a permission change.
                        left,right=set(left),set(right)
                    if left!=right:
                        raise HubError(409,'incompatible_merge','主体、范围、类型、权限或期限不同，不能合并。')
        for change in changes:
            mid=change['memory_id'];snap=final[mid]
            snap['relations']=list(before[mid]['relations'])
            snap['source_links']=list(before[mid]['source_links'])
            if 'related_ids' in change:
                snap['relations']=[r for r in snap['relations'] if r['relation']!='related']
                snap['relations'] += [{'memory_id':mid,'target_id':target,'relation':'related','draft_id':row['draft_id'],'created_at':utc_now_iso()} for target in set(change['related_ids'])]
            if change['action']=='merge':
                snap['state']='paused'
                snap['relations']=[r for r in snap['relations'] if r['relation']!='superseded_by']+[{'memory_id':mid,'target_id':change['replacement_id'],'relation':'superseded_by','draft_id':row['draft_id'],'created_at':utc_now_iso()}]
        for change in changes:
            if change['action']=='merge':
                target=final[change['replacement_id']]
                for link in before[change['memory_id']]['source_links']:
                    if not any(x['source_id']==link['source_id'] and x['source_revision']==link['source_revision'] for x in target['source_links']):target['source_links'].append(link)
        results=[]
        for change in changes:
            mid=change['memory_id'];old=self._memory_in(con,mid);action=change['action'];snap=final[mid]
            if action=='keep':
                results.append({'memory_id':mid,'before_revision':old['revision'],'stored_revision':old['revision'],'outcome':'kept'});continue
            decision=change.get('proposal_decision','keep');props=before[mid]['proposals']
            if decision=='keep' and all(snap.get(k)==v for k,v in before[mid]['snapshot'].items()) and snap['relations']==before[mid]['relations'] and snap['source_links']==before[mid]['source_links']:
                results.append({'memory_id':mid,'before_revision':old['revision'],'stored_revision':old['revision'],'outcome':'unchanged'});continue
            if decision=='approve' and not props:raise HubError(409,'proposal_not_pending','没有可批准的提案。')
            if props:
                for prop in props:
                    status='accepted' if decision=='approve' else 'rejected'
                    resolution='accept' if decision=='approve' else 'reject' if decision=='reject' else 'superseded_by_draft:'+row['draft_id']
                    con.execute('UPDATE conflict_proposals SET status=?,resolution=?,resolved_by=?,resolved_by_revision=?,resolved_at=? WHERE proposal_id=? AND status=\'pending\'',(status,resolution,'user',old['revision']+1,utc_now_iso(),prop['proposal_id']))
                    if decision=='keep':
                        successor=dict(prop);successor.update(proposal_id='prop_'+uuid.uuid4().hex[:24],base_revision=old['revision']+1,status='pending',resolution=None,resolved_by=None,resolved_by_revision=None,resolved_at=None,created_at=utc_now_iso())
                        if prop['proposal_type'] in ('scope_expansion','rule_candidate'):
                            successor.update(proposed_title=snap['title'],proposed_content=snap['content'],proposed_metadata_json=canonical(snap))
                        columns=list(successor);con.execute('INSERT INTO conflict_proposals ('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[successor[k] for k in columns])
                snap['has_open_conflict']=bool(self._pending(con,mid))
                if decision=='approve' and old['state']=='paused' and snap['kind']!='rule_candidate':snap['state']='active'
            if action=='merge':snap['state']='paused'
            prior=before[mid]['snapshot']
            changed=any(snap.get(k)!=v for k,v in prior.items()) or snap['relations']!=before[mid]['relations'] or snap['source_links']!=before[mid]['source_links'] or decision!='keep'
            if changed:
                snap['source']={**snap.get('source',{}),'related_draft_id':row['draft_id'],'collaborator':s['client_name'],'session_id':s['session_id']}
            result=self._admin_commit(old,snap,old['revision'],'related_update',_con=con)
            result.update(before_revision=old['revision'],outcome='merged' if action=='merge' else 'unchanged' if result['status']=='unchanged' else 'updated');results.append(result)
        after={r['memory_id']:r['stored_revision'] for r in results}
        con.execute('INSERT INTO draft_applications VALUES(?,?,?,?,0)',(row['draft_id'],canonical(before),canonical(after),canonical(results)))
        primary=next(r for r in results if r['memory_id']==row['target_memory_id'])
        result={**primary,'status':'group_committed','results':results}
        self._draft_finish(con,row,s,result)
        return {**result,'draft_id':row['draft_id'],'draft_revision':row['revision']+1}

    def _draft_finish(self,con,row,suggestion,result):
        super()._draft_finish(con,row,suggestion,result)
        con.execute("UPDATE draft_suggestions SET status='superseded' WHERE draft_id=? AND status='pending'",(row['draft_id'],))

    def _undo_group(self,draft_id,payload):
        def undo(con):
            row=self._draft_row(con,draft_id);self._draft_revision(row,payload)
            application=con.execute('SELECT * FROM draft_applications WHERE draft_id=?',(draft_id,)).fetchone()
            if not application or application['undone']:raise HubError(409,'application_not_undoable','没有可撤销的整组应用。')
            before=json.loads(application['before_json']);after=json.loads(application['after_json'])
            for mid,revision in after.items():
                if self._memory_in(con,mid)['revision']!=revision:raise HubError(409,'RevisionConflict','至少一条已有后续修改，整组未撤销。')
            results=[]
            for mid,saved in before.items():
                old=self._memory_in(con,mid);original=next(r for r in json.loads(application['results_json']) if r['memory_id']==mid)
                if original['outcome'] in ('kept','unchanged'):continue
                con.execute("UPDATE conflict_proposals SET status='rejected',resolution='superseded_by_group_undo',resolved_by='user',resolved_at=? WHERE memory_id=? AND status='pending'",(utc_now_iso(),mid))
                for prop in saved['proposals']:
                    prop.update(proposal_id='prop_'+uuid.uuid4().hex[:24],base_revision=old['revision']+1);columns=list(prop)
                    con.execute('INSERT INTO conflict_proposals ('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[prop[k] for k in columns])
                snapshot={**saved['snapshot'],'relations':saved['relations'],'source_links':saved['source_links']}
                result=self._admin_commit(old,snapshot,old['revision'],'related_undo',_con=con);results.append(result)
                con.execute('DELETE FROM memory_relations WHERE memory_id=?',(mid,))
                con.execute('DELETE FROM source_links WHERE memory_id=?',(mid,))
                for link in saved['source_links']:con.execute('INSERT OR IGNORE INTO source_links VALUES(?,?,?)',(mid,link['source_id'],link['source_revision']))
                for rel in saved['relations']:con.execute('INSERT INTO memory_relations VALUES(?,?,?,?,?)',tuple(rel[k] for k in ('memory_id','target_id','relation','draft_id','created_at')))
            con.execute('UPDATE draft_applications SET undone=1 WHERE draft_id=?',(draft_id,))
            con.execute('UPDATE memory_drafts SET revision=revision+1,updated_at=? WHERE draft_id=?',(utc_now_iso(),draft_id))
            self._draft_snapshot(con,self._draft_row(con,draft_id))
            return {'status':'group_undone','draft_id':draft_id,'revision':row['revision']+1,'results':results}
        return self._draft_transaction('undo-apply',draft_id,'user',payload,undo)

    @staticmethod
    def _pair_key(items):
        return hashlib.sha256(canonical(sorted((x['memory_id'],x['revision']) for x in items)).encode()).hexdigest()

    def review_duplicate_candidates(self,payload):
        key=payload.get('request_key')
        if not isinstance(key,str) or not key:raise HubError(422,'missing_idempotency_key','必须提供原请求键。')
        digest='sha256:'+hashlib.sha256(canonical({'action':'duplicate-review','body':payload}).encode()).hexdigest()
        with self._write_lock:
            con=self.connect()
            try:
                con.execute('BEGIN IMMEDIATE')
                prior=con.execute('SELECT * FROM requests WHERE request_key=?',(key,)).fetchone()
                if prior:
                    if prior['is_purged']:raise HubError(410,'memory_purged','关联记忆已清除，禁止重放。')
                    if prior['actor_id']!='user' or prior['body_hash']!=digest:raise HubError(422,'IdempotencyKeyPayloadMismatch','同一请求键不能更换操作内容。')
                    return {**json.loads(prior['result_json']),'idempotent_replay':True}
                if payload.get('decision') not in ('keep_both','defer'):raise HubError(422,'invalid_decision','请选择保留两条或暂缓。')
                ids=payload.get('memory_ids')
                if not isinstance(ids,list) or len(ids)!=2 or len(set(ids))!=2:raise HubError(422,'invalid_members','请恰好选择两条不同记忆；更多条目请逐对核对。')
                items=[self._memory_in(con,mid) for mid in ids]
                expected=payload.get('expected_revisions',{})
                if not isinstance(expected,dict) or any(expected.get(x['memory_id'])!=x['revision'] for x in items):raise HubError(409,'RevisionConflict','版本已变化，请重新查看。')
                pair=self._pair_key(items)
                con.execute('INSERT OR REPLACE INTO duplicate_reviews VALUES(?,?,?,?)',(pair,payload['decision'],canonical([{k:x[k] for k in ('memory_id','revision')} for x in items]),utc_now_iso()))
                result={'status':'reviewed','pair_key':pair,'decision':payload['decision'],'memory_ids':ids}
                con.execute('INSERT INTO requests(request_key,actor_id,body_hash,memory_id,applied_revision,status_code,result_json,created_at) VALUES(?,?,?,?,?,200,?,?)',(key,'user',digest,ids[0],items[0]['revision'],canonical(result),utc_now_iso()))
                con.commit();return result
            finally:
                if con.in_transaction:con.rollback()
                con.close()

    def related_candidates(self,payload):
        con=self.connect()
        try:
            base=self._memory_in(con,payload['memory_id']) if payload.get('memory_id') else None
            text=payload.get('query','') or ((base['title']+' '+base['content']) if base else '')
            if not isinstance(text,str) or len(text)>12000:raise HubError(422,'invalid_query','查询文字无效。')
            def terms(s):
                english=set(re.findall(r'[a-z0-9_./:+-]{2,}',s.lower()));chinese=''.join(re.findall('[\u4e00-\u9fff]+',s))
                return english|{chinese[i:i+2] for i in range(len(chinese)-1)}
            query=terms(text);items=[]
            for row in con.execute("SELECT * FROM current_memory WHERE state IN ('active','paused')"):
                item=dict(row)
                if base and item['memory_id']==base['memory_id']:continue
                score=len(query&terms(item['title']+' '+item['content']));reasons=[]
                if score:reasons.append('文字或术语有重叠')
                linked=base and con.execute('SELECT 1 FROM memory_relations WHERE (memory_id=? AND target_id=?) OR (memory_id=? AND target_id=?)',(base['memory_id'],item['memory_id'],item['memory_id'],base['memory_id'])).fetchone()
                if linked:score+=100;reasons.append('已有明确关联')
                if base and base['topic_key']!='general' and item['topic_key']==base['topic_key']:score+=5;reasons.append('相同主题标识')
                if not score:continue
                if base and con.execute('SELECT 1 FROM duplicate_reviews WHERE pair_key=?',(self._pair_key([base,item]),)).fetchone():continue
                value=self.format_memory_item(row,con=con);value['excerpt']=value.pop('content')[:200];value['match_reasons']=reasons;value['candidate_score']=score;items.append(value)
            items.sort(key=lambda x:(-x['candidate_score'],x['memory_id']))
            return {'items':items[:5],'mode':'keyword_and_explicit_relations','notice':'候选供人工检查，不代表等义或冲突；可从目录手动补充。'}
        finally:con.close()

    def memory_relations(self,mid):
        con=self.connect()
        try:
            results=[]
            for rel in con.execute('SELECT * FROM memory_relations WHERE memory_id=? OR target_id=?',(mid,mid)):
                other=rel['target_id'] if rel['memory_id']==mid else rel['memory_id'];row=con.execute('SELECT * FROM current_memory WHERE memory_id=?',(other,)).fetchone()
                if row and row['state']!='deleted':results.append({**dict(rel),'direction':'outgoing' if rel['memory_id']==mid else 'incoming','other_id':other,'other_title':row['title'],'other_revision':row['revision']})
            return results
        finally:con.close()

    def admin_confirm_review(self,mid,payload):
        key=payload.get('request_key')
        if not isinstance(key,str) or not key:raise HubError(422,'missing_idempotency_key','必须提供原请求键。')
        digest='sha256:'+hashlib.sha256(canonical({'memory_id':mid,**{k:v for k,v in payload.items() if k!='request_key'}}).encode()).hexdigest()
        with self._write_lock:
            con=self.connect()
            try:
                con.execute('BEGIN IMMEDIATE')
                prior=con.execute('SELECT * FROM requests WHERE request_key=?',(key,)).fetchone()
                if prior:
                    if prior['actor_id']!='user' or prior['body_hash']!=digest:raise HubError(422,'IdempotencyKeyPayloadMismatch','同一请求键不能更换操作内容。')
                    if prior['is_purged']:raise HubError(410,'memory_purged','记忆已清除。')
                    return {**json.loads(prior['result_json']),'idempotent_replay':True}
                old=self._memory_in(con,mid)
                if payload.get('confirm_accuracy') is not True:raise HubError(422,'confirmation_required','请确认已核对内容准确。')
                if old['locked'] and payload.get('confirm_locked_edit') is not True:raise HubError(409,'LockedConflict','须确认本次人工修改锁定记忆。')
                changes={'confidence':'confirmed'}
                if payload.get('complete_review'):
                    changes['last_reviewed_at']=utc_now_iso();changes['review_due_at']=payload.get('next_review_due_at')
                result=self._admin_commit(old,changes,payload.get('expected_revision'),'review' if payload.get('complete_review') else 'confirm',_con=con)
                result.update(last_reviewed_at=changes.get('last_reviewed_at',old.get('last_reviewed_at')))
                con.execute('INSERT INTO requests(request_key,actor_id,body_hash,memory_id,applied_revision,status_code,result_json,created_at) VALUES(?,?,?,?,?,200,?,?)',(key,'user',digest,mid,result['stored_revision'],canonical(result),utc_now_iso()));con.commit();return result
            finally:con.close()
