"""人工修改共用校验、完整快照和修订号。v0.3.0"""
import json
import uuid
from .db import HubError, DEFAULT_GENERATION, utc_now_iso


class AdminActionsMixin:
    def _ensure_not_reindexing(self):
        self.check_not_reindexing()

    def _admin_row(self, memory_id):
        con=self.connect()
        try:
            row=con.execute('SELECT * FROM current_memory WHERE memory_id=?',(memory_id,)).fetchone()
            if not row:raise HubError(404,'memory_not_found','记忆不存在或已永久清除。')
            return dict(row)
        finally:con.close()

    def _validated_snapshot(self, old, changes):
        snap={**self._row_to_metadata_snapshot(old), 'title':old['title'], 'content':old['content'], 'state':old['state'],'locked':old['locked']}
        snap.update(changes)
        snap.update(self.validate_organization(snap))
        if snap.get('kind')=='rule_candidate' and snap.get('state')=='active':
            snap['state']='paused'
        if snap['subject'] not in self.get_meta('allowed_subjects',[]):raise HubError(422,'invalid_subject','主体未登记。')
        if snap['scope'] not in ('global','user_global','project','agent_private'):raise HubError(422,'invalid_scope','无效的适用范围。')
        if snap['scope']=='project' and snap['project_id'] not in self.get_meta('project_registry',{}).values():raise HubError(422,'invalid_project','项目必须先绑定。')
        if snap['state'] not in ('active','paused','deleted'):raise HubError(422,'invalid_state','无效状态。')
        for key in ('readers','host_constraints'):
            if not isinstance(snap[key],list) or not snap[key] or not all(isinstance(x,str) and x for x in snap[key]):raise HubError(422,'invalid_access_metadata','读者与宿主必须是非空字符串数组。')
        _,confidence,_,expiry=self.run_admission_guard_and_calibrate('user_admin','user',snap,None)
        snap.update(confidence=confidence,expires_at=expiry,title=snap['title'].strip(),content=snap['content'].strip())
        return snap

    def _rebase_pending_proposals(self, con, mid, revision, snap):
        # Keep the historical proposal, and bind its successor to the new full snapshot.
        for row in con.execute("SELECT * FROM conflict_proposals WHERE memory_id=? AND status='pending' AND base_revision<?", (mid, revision)).fetchall():
            prop=dict(row)
            con.execute("UPDATE conflict_proposals SET status='rejected',resolution='superseded_by_revision',resolved_by='user',resolved_by_revision=?,resolved_at=? WHERE proposal_id=?",(revision,utc_now_iso(),prop['proposal_id']))
            prop.update(proposal_id='prop_'+uuid.uuid4().hex[:24],base_revision=revision,status='pending',resolution=None,resolved_by=None,resolved_by_revision=None,resolved_at=None,created_at=utc_now_iso())
            if prop['proposal_type'] in ('scope_expansion','rule_candidate'):
                prop.update(proposed_title=snap['title'],proposed_content=snap['content'],proposed_metadata_json=json.dumps(snap,ensure_ascii=False))
            cols=list(prop)
            con.execute('INSERT INTO conflict_proposals ('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',[prop[k] for k in cols])

    def _admin_commit(self, existing, changes, expected_revision, operation, actor_id='user', restored_from=None, proposal=None, request_info=None, _con=None):
        with self._write_lock:
            self._ensure_not_reindexing()
            old=dict(_con.execute('SELECT * FROM current_memory WHERE memory_id=?',(existing['memory_id'],)).fetchone()) if _con is not None else self._admin_row(existing['memory_id'])
            if expected_revision is None or int(expected_revision)!=old['revision']:raise HubError(409,'RevisionConflict','修订号已变化，请刷新。')
            links_con=_con if _con is not None else self.connect()
            try:
                current_relations=[dict(r) for r in links_con.execute('SELECT * FROM memory_relations WHERE memory_id=? ORDER BY target_id,relation',(old['memory_id'],))]
                current_links=[dict(r) for r in links_con.execute('SELECT * FROM source_links WHERE memory_id=? ORDER BY source_id,source_revision',(old['memory_id'],))]
            finally:
                if _con is None:links_con.close()
            snap=self._validated_snapshot(old,changes)
            snap.setdefault('relations',current_relations)
            snap.setdefault('source_links',current_links)
            if snap['state']=='active' and any(r['relation']=='superseded_by' for r in snap['relations']):
                raise HubError(409,'superseded_memory','此条目已被取代；请通过关联整理撤销取代关系后再启用。')
            pending=bool(_con.execute("SELECT 1 FROM conflict_proposals WHERE memory_id=? AND status='pending'",(old['memory_id'],)).fetchone()) if _con is not None else bool(old['has_open_conflict'])
            if pending and old['state']=='paused' and snap['state']=='active' and not proposal:
                if operation=='restore':
                    snap['state']='paused'
                else:
                    raise HubError(409,'approval_required','请先裁决待批提案；不能通过启用跳过批准。')
            mid=old['memory_id'];rev=old['revision']+1;now=utc_now_iso()
            prior={**self._row_to_metadata_snapshot(old),'title':old['title'],'content':old['content'],'state':old['state'],'locked':old['locked']}
            prior.update(relations=current_relations,source_links=current_links)
            if not proposal and all(snap.get(k)==v for k,v in prior.items()):
                result={'status':'unchanged','memory_id':mid,'stored_revision':old['revision'],'current_state':old['state'],'locked':bool(old['locked']),'has_open_conflict':bool(old['has_open_conflict'])}
                if request_info:
                    own=_con is None; journal=_con if _con is not None else self.connect()
                    try:journal.execute('INSERT INTO requests(request_key,actor_id,body_hash,memory_id,applied_revision,status_code,result_json,created_at) VALUES(?,?,?,?,?,200,?,?)',(request_info[0],actor_id,request_info[1],mid,old['revision'],json.dumps(result),now))
                    finally:
                        if own:journal.close()
                return result
            con=_con if _con is not None else self.connect()
            try:
                if _con is None:con.execute('BEGIN IMMEDIATE')
                if proposal:
                    pid,status,resolution=proposal
                    if status=='pending':
                        con.execute('UPDATE conflict_proposals SET status=?,base_revision=?,resolution=NULL,resolved_by=NULL,resolved_at=NULL,resolved_by_revision=NULL WHERE proposal_id=? AND memory_id=?',(status,rev,pid,mid))
                    else:
                        applied=con.execute("UPDATE conflict_proposals SET status=?,resolution=?,resolved_by=?,resolved_at=?,resolved_by_revision=? WHERE proposal_id=? AND memory_id=? AND status='pending'",(status,resolution,actor_id,now,rev,pid,mid))
                        if applied.rowcount!=1:raise HubError(409,'proposal_not_pending','提案已处理。')
                    snap['has_open_conflict']=bool(con.execute("SELECT 1 FROM conflict_proposals WHERE memory_id=? AND status='pending'",(mid,)).fetchone())
                self._rebase_pending_proposals(con,mid,rev,snap)
                snap['has_open_conflict']=bool(con.execute("SELECT 1 FROM conflict_proposals WHERE memory_id=? AND status='pending'",(mid,)).fetchone())
                con.execute('DELETE FROM memory_relations WHERE memory_id=?',(mid,))
                for rel in snap['relations']:
                    target=con.execute("SELECT * FROM current_memory WHERE memory_id=? AND state!='deleted'",(rel['target_id'],)).fetchone()
                    if target is None:raise HubError(409,'related_memory_unavailable','关联目标不可用，不能恢复此关系。')
                    con.execute('INSERT INTO memory_relations VALUES(?,?,?,?,?)',(mid,rel['target_id'],rel['relation'],rel['draft_id'],rel['created_at']))
                con.execute('DELETE FROM source_links WHERE memory_id=?',(mid,))
                for link in snap['source_links']:
                    con.execute('INSERT OR IGNORE INTO source_links VALUES(?,?,?)',(mid,link['source_id'],link['source_revision']))
                applied=con.execute('''UPDATE current_memory SET revision=?,state=?,locked=?,has_open_conflict=?,subject=?,scope=?,project_id=?,kind=?,topic_key=?,title=?,content=?,confidence=?,readers_json=?,host_constraints_json=?,source_type=?,source_json=?,expires_at=?,updated_at=? WHERE memory_id=? AND revision=?''',
                    (rev,snap['state'],int(bool(snap['locked'])),int(snap['has_open_conflict']),snap['subject'],snap['scope'],snap['project_id'],snap['kind'],snap['topic_key'],snap['title'],snap['content'],snap['confidence'],json.dumps(snap['readers']),json.dumps(snap['host_constraints']),snap.get('source_type','admin_manual'),json.dumps(snap.get('source',{}),ensure_ascii=False),snap['expires_at'],now,mid,old['revision']))
                if applied.rowcount!=1:raise HubError(409,'RevisionConflict','条件更新失败。')
                con.execute('UPDATE current_memory SET domain_id=?,tags_json=?,review_due_at=?,last_reviewed_at=? WHERE memory_id=?',
                    (snap['domain_id'],json.dumps(snap['tags'],ensure_ascii=False),snap['review_due_at'],snap.get('last_reviewed_at'),mid))
                self._link_source(con,mid,snap.get('source',{}))
                con.execute('DELETE FROM memory_fts WHERE memory_id=?',(mid,))
                if snap['state']=='active':con.execute('INSERT INTO memory_fts(memory_id,title,content,topic_key) VALUES(?,?,?,?)',(mid,snap['title'],snap['content'],snap['topic_key']))
                gen=self.get_meta('active_index_generation',DEFAULT_GENERATION)
                con.execute("INSERT INTO index_work(memory_id,target_revision,generation,action,status,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?)",(mid,rev,gen,'upsert' if snap['state']=='active' else 'delete',now,now))
                event_source={'origin_type':'admin_manual'}
                if proposal:event_source['resolved_proposal_id']=proposal[0]
                con.execute('''INSERT INTO revisions(memory_id,revision,parent_revision,operation,state,locked,title,content,metadata_snapshot_json,actor_role,actor_id,source_type,source_json,restored_from,created_at) VALUES(?,?,?,?,?,?,?,?,?,'user_admin',?,'admin_manual',?,?,?)''',
                    (mid,rev,old['revision'],operation,snap['state'],int(bool(snap['locked'])),snap['title'],snap['content'],json.dumps(snap,ensure_ascii=False),actor_id,json.dumps(event_source),restored_from,now))
                if request_info:con.execute('INSERT INTO requests(request_key,actor_id,body_hash,memory_id,applied_revision,status_code,created_at) VALUES(?,?,?,?,?,200,?)',(request_info[0],actor_id,request_info[1],mid,rev,now))
                event=operation if operation in ('undo','restore','soft_delete') else ('lock_toggle' if operation in ('lock','unlock') else 'write')
                con.execute("INSERT INTO audit_events(event_type,memory_id,revision,actor_role,actor_id,scope,project_id,result_status,created_at) VALUES(?,?,?,'user_admin',?,?,?,'committed',?)",(event,mid,rev,actor_id,snap['scope'],snap['project_id'],now))
                if _con is None:con.commit()
            finally:
                if _con is None:con.close()
            return {'status':'committed','memory_id':mid,'stored_revision':rev,'current_state':snap['state'],'locked':bool(snap['locked']),'has_open_conflict':snap['has_open_conflict'],'restored_from':restored_from}

    def _historical_snapshot(self, mid, revision):
        con=self.connect()
        try:
            row=con.execute('SELECT * FROM revisions WHERE memory_id=? AND revision=?',(mid,revision)).fetchone()
            if not row:raise HubError(404,'revision_not_found','历史修订不存在。')
            snap=json.loads(row['metadata_snapshot_json']);snap.update(title=row['title'],content=row['content'],state=row['state'],locked=row['locked'])
            for key,value in [('domain_id',None),('tags',[]),('review_due_at',None),('last_reviewed_at',None)]:snap.setdefault(key,value)
            snap.setdefault('relations',[])
            snap.setdefault('source_links',[])
            return snap,dict(row)
        finally:con.close()

    def admin_undo(self,memory_id,expected_revision,actor_id='user'):
        with self._write_lock:
            old=self._admin_row(memory_id);proposal=None
            if old['revision']==1:changes={'state':'deleted'}
            else:
                changes,_=self._historical_snapshot(memory_id,old['revision']-1)
                _,current=self._historical_snapshot(memory_id,old['revision'])
                if current['operation']=='resolve_conflict':
                    pid=json.loads(current['source_json']).get('resolved_proposal_id')
                    if pid:proposal=(pid,'pending',None)
            return self._admin_commit(old,changes,expected_revision,'undo',actor_id,old['revision']-1,proposal)

    def admin_restore(self,memory_id,target_revision,expected_revision,actor_id='user'):
        with self._write_lock:
            old=self._admin_row(memory_id)
            if not 1<=int(target_revision)<old['revision']:raise HubError(422,'invalid_target_revision','请选择已有的更早版本。')
            changes,_=self._historical_snapshot(memory_id,target_revision)
            changes.update(state='active',locked=1,has_open_conflict=old['has_open_conflict'])
            return self._admin_commit(old,changes,expected_revision,'restore',actor_id,target_revision)

    def admin_set_lock(self,memory_id,locked,expected_revision,actor_id='user'):
        return self._admin_commit(self._admin_row(memory_id),{'locked':locked},expected_revision,'lock' if locked else 'unlock',actor_id)

    def admin_set_state(self,memory_id,new_state,expected_revision,actor_id='user'):
        return self._admin_commit(self._admin_row(memory_id),{'state':new_state},expected_revision,'soft_delete' if new_state=='deleted' else 'state',actor_id)

    def admin_resolve_conflict(self,memory_id,proposal_id,resolution,expected_revision,final_title=None,final_content=None,actor_id='user',final_fields=None,confirm_scope_change=False):
        if resolution not in ('accept','reject','edit','merge'):raise HubError(422,'invalid_resolution','无效裁决。')
        with self._write_lock:
            old=self._admin_row(memory_id);con=self.connect()
            try:prop=con.execute("SELECT * FROM conflict_proposals WHERE proposal_id=? AND memory_id=? AND status='pending'",(proposal_id,memory_id)).fetchone()
            finally:con.close()
            if not prop:raise HubError(404,'proposal_not_pending','提案不存在或已处理。')
            if resolution!='reject' and prop['base_revision']!=old['revision']:raise HubError(409,'stale_proposal','提案基线已变化，请刷新比较；不能覆盖后来版本。旧建议可拒绝后重新提出。')
            changes={'locked':1}
            if resolution!='reject':
                changes.update(json.loads(prop['proposed_metadata_json']))
                changes.update(title=prop['proposed_title'] or old['title'],content=prop['proposed_content'] or old['content'])
                if resolution in ('edit','merge'):
                    if final_title is None or final_content is None:raise HubError(422,'merge_content_required','手动合并必须提供完整正文和标题。')
                    changes.update(title=final_title,content=final_content)
                if prop['proposal_type']=='delete_proposal':changes['state']='deleted'
                elif prop['proposal_type'] in ('scope_expansion','rule_candidate'):changes['state']='active'
                if final_fields is not None:
                    allowed={'title','content','subject','scope','project_id','kind','topic_key','confidence','readers','host_constraints','domain_id','tags','expires_at','review_due_at'}
                    if not isinstance(final_fields,dict) or set(final_fields)-allowed:raise HubError(422,'invalid_fields','不可改写来源、修订或系统字段。')
                    changes.update(final_fields)
                    prior=self._row_to_metadata_snapshot(old)
                    if any((sorted(changes.get(k,prior[k]))!=sorted(prior[k]) if k in ('readers','host_constraints') else changes.get(k,prior[k])!=prior[k]) for k in ('subject','scope','project_id','readers','host_constraints')) and not confirm_scope_change:
                        raise HubError(409,'scope_confirmation_required','适用范围变化必须单独确认。')
                if changes.get('kind',old['kind'])=='rule_candidate':changes['state']='paused'
            changes['locked']=1
            status={'accept':'accepted','reject':'rejected','edit':'edited','merge':'merged'}[resolution]
            return self._admin_commit(old,changes,expected_revision,'resolve_conflict',actor_id,proposal=(proposal_id,status,resolution))
