"use strict";
let csrfToken = "";
let selected = null;
const el = id => document.getElementById(id);
const show = value => { el("receiptBox").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };
const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
function button(parent, label, action, cls="") {
  const b = node("button", label, cls);
  b.addEventListener("click", () => Promise.resolve().then(action).catch(e => show(e.message)));
  parent.append(b); return b;
}
async function api(path, method="GET", body) {
  const headers = {"X-CSRF-Token": csrfToken};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, {method, headers, cache:"no-store", body:body === undefined ? undefined : JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) throw new Error(d.message || d.error_code || `HTTP ${r.status}`);
  return d;
}
async function refreshHealth() {
  try {
    const h = await api("/v1/health");
    el("healthBanner").textContent = `服务：${h.status} · 向量：${h.qdrant} · 索引：${h.active_index_generation} · 待办：${h.pending_index_work}`;
  } catch(e) { el("healthBanner").textContent = "服务连接异常"; }
}
async function loginAdmin() {
  const passcode = el("passcodeInput").value;
  el("passcodeInput").value = "";
  const d = await api("/v1/admin/login", "POST", {passcode});
  csrfToken = d.csrf_token;
  el("authStatus").textContent = "已登录；关闭服务后会话失效";
  show("登录成功"); await loadMemories();
}
async function bindProject() {
  show(await api("/v1/admin/projects", "POST", {project_id:el("projId").value, workspace_path:el("projPath").value}));
}
async function createAdminMemory() {
  const payload = {request_key:crypto.randomUUID(),operation:"create",scope:el("newScope").value,
    subject:el("newSubject").value,kind:el("newKind").value,workspace_path:el("newWorkspace").value || null,
    topic_key:el("newTopic").value || "general",title:el("newTitle").value,content:el("newContent").value,
    source:{origin_type:"admin_manual"}};
  show(await api("/v1/memory/write","POST",payload)); await loadMemories();
}
async function mutate(m, action, body) {
  const result = await api(`/v1/admin/memory/${encodeURIComponent(m.memory_id)}/${action}`,"POST",{expected_revision:m.revision,...body});
  show(result); await loadMemories();
  if (action !== "purge") await viewDetail(m.memory_id);
  else { selected = null; el("detailPanel").replaceChildren(); }
}
async function loadMemories() {
  const d = await api(`/v1/admin/memories?query=${encodeURIComponent(el("filterQuery").value)}`);
  const list = el("memoryList"); list.replaceChildren();
  if (!d.items.length) list.append(node("p","暂无记忆"));
  for (const m of d.items) {
    const card = node("article", undefined, "mem-item"); card.dataset.memoryId = m.memory_id;
    card.append(node("div", `${m.title} · r${m.revision}`, "mem-title"));
    card.append(node("div", `${m.state} · ${m.kind} · ${m.scope} · ${m.confidence}${m.locked ? " · 已锁定" : ""}${m.has_open_conflict ? " · 待裁决" : ""}`, "badge"));
    if (m.source.verification_status === "unverified_excerpt") card.append(node("p","自动记忆来自会话摘录，可撤销。", "badge conflict"));
    card.append(node("div",m.content,"mem-content"));
    button(card,"编辑与历史",()=>viewDetail(m.memory_id));
    button(card,m.locked?"解锁":"锁定",()=>mutate(m,"lock",{locked:!m.locked}));
    button(card,m.state==="active"?"暂停":"启用",()=>mutate(m,"state",{state:m.state==="active"?"paused":"active"}));
    button(card,"软删除",()=>mutate(m,"state",{state:"deleted"}),"warn");
    button(card,"撤销上一动作",()=>mutate(m,"undo",{}));
    button(card,"永久清除",async()=>{
      const expected = `PURGE ${m.memory_id} r${m.revision}`;
      const typed = prompt(`永久清除无法撤销。请输入：${expected}`);
      if (typed === null) return;
      await mutate(m,"purge",{confirm_purge:typed});
    },"danger");
    list.append(card);
  }
  await refreshHealth();
}
function input(parent, id, label, value, textarea=false) {
  const l=node("label",label); l.htmlFor=id;
  const i=node(textarea?"textarea":"input"); i.id=id;
  i.value=value == null ? "" : String(value); if(textarea)i.rows=5;
  parent.append(l,i); return i;
}
async function viewDetail(id) {
  const d = await api(`/v1/admin/memory/${encodeURIComponent(id)}/detail`); selected=d.item;
  const m = d.item, panel = el("detailPanel"); panel.replaceChildren();
  panel.append(node("h2",`编辑：${m.title} · r${m.revision}`));
  input(panel,"editTitle","标题",m.title); input(panel,"editContent","正文（纯文本）",m.content,true);
  for (const [field,label] of [["subject","主体"],["scope","适用范围"],["project_id","项目标识"],["kind","类别"],["topic_key","主题键"],["confidence","确认状态"],["expires_at","过期时间（ISO 格式）"]]) input(panel,`edit_${field}`,label,m[field]);
  input(panel,"edit_readers","可读取客户端（JSON 数组）",JSON.stringify(m.readers));
  input(panel,"edit_hosts","宿主约束（JSON 数组）",JSON.stringify(m.host_constraints));
  button(panel,"保存编辑",async()=>{
    const changes={};
    for (const k of ["subject","scope","project_id","kind","topic_key","confidence","expires_at"]) changes[k]=el(`edit_${k}`).value || null;
    Object.assign(changes,{title:el("editTitle").value,content:el("editContent").value,readers:JSON.parse(el("edit_readers").value),host_constraints:JSON.parse(el("edit_hosts").value)});
    show(await api("/v1/memory/write","POST",{request_key:crypto.randomUUID(),operation:"update",memory_id:m.memory_id,expected_revision:m.revision,...changes}));
    await loadMemories(); await viewDetail(id);
  },"success");
  panel.append(node("h2","版本对比与恢复"));
  for (const r of d.revisions) {
    const details=node("details"), summary=node("summary",`r${r.revision} · ${r.operation} · ${r.state}`);
    details.append(summary,node("pre",`所选版本 r${r.revision}\n${r.title}\n${r.content}\n${r.metadata_snapshot_json}\n\n当前版本 r${m.revision}\n${m.title}\n${m.content}\n范围：${m.scope} / ${m.project_id || ""}`));
    if(r.revision<m.revision)button(details,`恢复 r${r.revision}`,()=>mutate(m,"restore",{target_revision:r.revision}));
    panel.append(details);
  }
  panel.append(node("h2","待裁决提案"));
  for (const p of d.proposals.filter(p=>p.status==="pending")) {
    const box=node("section",undefined,"mem-item");
    box.append(node("p",`${p.proposal_type} · 基线 r${p.base_revision}`),node("pre",`${p.proposed_title}\n${p.proposed_content}\n${p.proposed_metadata_json}`));
    for (const [label,resolution] of [["采纳","accept"],["拒绝","reject"],["使用编辑框手动合并","merge"]]) button(box,label,()=>mutate(m,"resolve",{proposal_id:p.proposal_id,resolution,
      ...(resolution==="merge"?{final_title:el("editTitle").value,final_content:el("editContent").value}:{})}));
    panel.append(box);
  }
}
for (const [id, action] of [["loginButton",loginAdmin],["bindButton",bindProject],["createButton",createAdminMemory],["refreshButton",loadMemories]])
  el(id).addEventListener("click",()=>action().catch(e=>show(e.message)));
refreshHealth();
