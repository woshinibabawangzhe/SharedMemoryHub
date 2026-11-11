// Execute the shipped navigation against an instrumented DOM, without packages.
// The assertions observe node lifetime and deferred HTTP results, not source text.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.listeners={};this.className='';this.parentNode=null;this.replacements=0;this.textContent='';}
  get isConnected(){return this.connectedRoot||Boolean(this.parentNode?.isConnected);}
  append(...nodes){for(const node of nodes){if(node.tagName==='#fragment'){const items=[...node.children];node.children=[];this.append(...items);}else{node.parentNode=this;this.children.push(node);}}}
  replaceChildren(...nodes){this.replacements++;for(const node of this.children)node.parentNode=null;this.children=[];this.append(...nodes);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  querySelectorAll(selector){const matches=[];for(const child of this.children){if(selector.startsWith('.')&&child.className.split(' ').includes(selector.slice(1)))matches.push(child);matches.push(...child.querySelectorAll(selector));}return matches;}
}

function harness(){
  const nodes=new Map();
  const document={createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('#fragment'),getElementById:id=>{if(!nodes.has(id)){const n=new Element('div');n.connectedRoot=true;nodes.set(id,n);}return nodes.get(id);}};
  const context=vm.createContext({document,window:{addEventListener(){}},fetch:async()=>({ok:false,status:401,json:async()=>({})}),AbortController,setTimeout,clearTimeout,console});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../hub/workbench-v0.7.0/workbench.js'),'utf8'),context);
  const run=code=>vm.runInContext(code,context);
  run(`catalog={domains:[{domain_id:'tools',name:'工具环境',parent_id:null},{domain_id:'device',name:'设备',parent_id:'tools'}],views:[],projects:[],tags:[],navigation_counts:{library:{total:2,domains:{tools:2,device:1},projects:{}}}};`);
  return {run,document,context};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('selecting and collapsing preserves nodes, focus targets and loaded branches',async()=>{
  const h=harness();
  h.run(`calls=[];api=async(path,method,body)=>{calls.push(body.filters);return {total:1,items:[{memory_id:body.filters.domain_id,title:'合成条目',domain_id:body.filters.domain_id}]};};expandedDomains.add('tools');expandedDomains.add('device');renderDirectory();`);
  await flush();
  const directory=h.document.getElementById('directory'),all=directory.children[0];
  const leaf=h.run(`directoryBranches.get('device').records.children[0]`);
  const childCategory=h.run(`directoryBranches.get('device').pick`);
  h.run(`filters={domain_id:'device'};selectedId='device';renderDirectory();renderDirectory();renderDirectory();`);
  assert.equal(directory.children[0],all);
  assert.equal(h.run(`directoryBranches.get('device').pick`),childCategory);
  assert.equal(h.run(`directoryBranches.get('device').records.children[0]`),leaf);
  assert.equal(leaf.attributes['aria-current'],'page');
  h.run(`expandedDomains.delete('tools');renderDirectory();expandedDomains.add('tools');renderDirectory();`);
  await flush();
  assert.equal(h.run('calls.length'),2);
  assert.equal(directory.replacements,1);
  assert.equal(h.run(`directoryBranches.get('device').records.children[0]`),leaf);
});

test('repeat renders during a slow request neither clear the branch nor duplicate requests',async()=>{
  const h=harness();
  h.run(`calls=0;finish=null;api=()=>{calls++;return new Promise(resolve=>finish=resolve);};expandedDomains.add('tools');renderDirectory();renderDirectory();renderDirectory();`);
  assert.equal(h.run('calls'),1);
  assert.equal(h.document.getElementById('directory').replacements,1);
  h.run(`finish({total:1,items:[{memory_id:'slow',title:'慢响应合成条目',domain_id:'tools'}]});`);
  await flush();
  h.run('renderDirectory();');
  assert.equal(h.run('calls'),1);
  assert.equal(h.run(`directoryBranches.get('tools').records.children[0].textContent`),'慢响应合成条目');
});

test('refresh invalidates data and ignores a late response from the previous tree',async()=>{
  const h=harness();
  h.run(`resolvers=[];api=()=>new Promise(resolve=>resolvers.push(resolve));expandedDomains.add('tools');renderDirectory();catalog={...catalog};renderDirectory();`);
  h.run(`resolvers[1]({total:1,items:[{memory_id:'new',title:'最新标题',domain_id:'tools'}]});`);
  await flush();
  h.run(`resolvers[0]({total:1,items:[{memory_id:'old',title:'过期标题',domain_id:'tools'}]});`);
  await flush();
  assert.equal(h.run(`directoryBranches.get('tools').records.children[0].textContent`),'最新标题');
  assert.equal(h.document.getElementById('directory').replacements,2);
});

test('failed branches offer explicit retry without rebuilding or automatic retry loops',async()=>{
  const h=harness();
  h.run(`calls=0;api=async()=>{calls++;throw new Error('合成离线');};expandedDomains.add('tools');renderDirectory();`);
  await flush();
  h.run('renderDirectory();renderDirectory();');
  assert.equal(h.run('calls'),1);
  assert.equal(h.run(`directoryBranches.get('tools').records.children[1].textContent`),'重新加载此分类');
  h.run(`api=async()=>{calls++;return {total:0,items:[]};};directoryBranches.get('tools').records.children[1].listeners.click();`);
  await flush();
  assert.equal(h.run('calls'),2);
  assert.equal(h.run(`directoryBranches.get('tools').records.children[0].textContent`),'暂无记忆');
  assert.equal(h.document.getElementById('directory').replacements,1);
});

test('catalog refresh updates review and category counts without replacing navigation',()=>{
  const h=harness();h.run(`catalog.navigation_counts.review_due={total:1};renderDirectory();beforeReview=directoryCounters.get('review_due');beforeAll=directoryAll;catalog=receiveCatalog({...catalog,navigation_counts:{library:{total:3,domains:{tools:3,device:2},projects:{}},review_due:{total:0}}});renderDirectory();`);
  assert.equal(h.run(`directoryCounters.get('review_due')`),h.run(`beforeReview`));
  assert.equal(h.run(`directoryCounters.get('review_due').textContent`),'0');
  assert.equal(h.run(`directoryCounters.get('domain:tools').textContent`),'3');
  assert.equal(h.run(`directoryAll`),h.run(`beforeAll`));
});


test('unsaved edits require a decision and unknown writes block leaving without prompting',()=>{
  const h=harness();
  h.run(`prompts=[];confirm=text=>{prompts.push(text);return false;};dirty=true;unknown=null;`);
  assert.equal(h.run('canLeave()'),false);
  assert.equal(h.run('dirty'),true);
  assert.equal(h.run('prompts.length'),1);
  h.run(`confirm=()=>true;`);
  assert.equal(h.run('canLeave()'),true);
  h.run(`dirty=false;`);
  assert.equal(h.run('canLeave()'),true);
  h.run(`unknown={request_key:'synthetic-unchanged-key'};messages=[];message=(text,error)=>messages.push({text,error});confirm=()=>{throw new Error('must not prompt');};`);
  assert.equal(h.run('canLeave()'),false);
  assert.equal(h.run('unknown.request_key'),'synthetic-unchanged-key');
  assert.equal(h.run('messages[0].error'),true);
});
