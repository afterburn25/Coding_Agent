/* Nexus Core · Mission Control */
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,method='GET',body){
  const o={method,headers:{'Content-Type':'application/json'}};
  if(body!==undefined)o.body=JSON.stringify(body);
  const r=await fetch(path,o);
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error||`HTTP ${r.status}`);
  return d;
}
let missions=[],selected=null;

async function refresh(){
  try{
    const [st,ms,appr,notes,goals,scheds,trigs]=await Promise.all([
      api('/api/autonomy/status'),
      api('/api/missions'),
      api('/api/autonomy/approvals?pending=1'),
      api('/api/autonomy/notifications'),
      api('/api/standing-goals'),
      api('/api/schedules'),
      api('/api/triggers'),
      api('/api/autonomy/summary'),
    ]);
    renderStatus(st);
    missions=ms.missions||[];
    renderList();
    renderDetail();
    renderApprovals(appr.approvals||[]);
    renderNotes(notes.notifications||[]);
    renderGoals(goals.goals||[]);
    renderSchedules(scheds.schedules||[]);
    renderTriggers(trigs.triggers||[]);
  }catch(e){
    $('#autonomyStatus').innerHTML='<span class="off">API unavailable</span>';
  }
}

function renderStatus(st){
  const on=st.running&&!st.stopped;
  $('#autonomyStatus').innerHTML=
    `<div>Supervisor: <b class="${on?'on':'off'}">${st.stopped?'STOPPED':st.running?'ON':'OFF'}</b></div>`+
    `<div>Active missions: ${st.active_missions}</div>`+
    `<div>Pending approvals: ${st.pending_approvals}</div>`+
    `<div>Workers: ${st.workers} · Tick: ${st.tick_ms??'—'}ms</div>`+
    `<div>Resource mode: ${esc(st.resource_mode||'balanced')}</div>`;
  $('#missionCount').textContent=`${missions.length} mission${missions.length===1?'':'s'}`;
}

function renderList(){
  const q=($('#missionFilter').value||'').toLowerCase();
  const rows=missions.filter(m=>!q||(m.title+m.objective+m.status).toLowerCase().includes(q));
  $('#missionList').innerHTML=rows.map(m=>
    `<div class="mission-card${selected===m.id?' selected':''}" data-mid="${m.id}">`+
    `<div class="title">${esc(m.title)}</div>`+
    `<div class="meta"><span class="mstatus ${m.status}">${m.status}</span>`+
    `<span>${esc(m.scope)}</span><span>${esc(m.priority)}</span></div></div>`
  ).join('')||'<div class="empty" style="color:#4d5f7c;padding:30px;text-align:center">No missions yet</div>';
}

function critDot(c,ev){
  if(!ev)return'unknown';
  const found=(ev.criteria||[]).find(x=>x.kind===c.kind);
  return found?(found.met?'met':'unmet'):'unknown';
}

function renderDetail(){
  const el=$('#missionDetail');
  const m=missions.find(x=>x.id===selected);
  if(!m){el.innerHTML='<div class="empty">Select a mission</div>';return;}
  const evals=m.evaluator_history||[];
  const lastEval=evals.length?null:null;
  const critHtml=(m.success_criteria||[]).map(c=>{
    const hist=(m.evaluator_history||[]);
    return`<div class="criterion"><span class="dot ${'unknown'}"></span><span>${esc(c.description||c.kind)}</span></div>`;
  }).join('')||'<div class="criterion"><span class="dot unknown"></span><span>no explicit criteria — completion = all tasks finished</span></div>';
  const nodes=(m.graph&&m.graph.nodes)||[];
  const dag=nodes.map(n=>
    `<div class="dag-node" data-state="${n.state}"><span class="nstate">${n.state}</span>`+
    `<b>${esc(n.title)}</b> <span style="color:#4d5f7c">· ${esc(n.kind)}</span>`+
    (n.result&&n.result.output?`<div class="nresult">${esc(String(n.result.output).slice(0,300))}</div>`:'')+
    `</div>`).join('')||'<div class="criterion"><span class="dot unknown"></span><span>No plan yet</span></div>';
  const hist=(m.history||[]).slice(-25).reverse().map(h=>
    `<div class="hist-row"><b>${esc(h.event)}</b> ${esc(h.detail||'')} <span style="float:right">${new Date((h.ts||0)*1000).toLocaleTimeString()}</span></div>`).join('');
  const actionable=['draft','ready','active','executing','paused','blocked','waiting_approval','waiting_dependency','replanning'].includes(m.status);
  el.innerHTML=
    `<div class="detail-head"><div><h2>${esc(m.title)}</h2>`+
    `<span class="mstatus ${m.status}">${m.status}</span> <span style="color:#7f91ad;font-size:11px">${esc(m.phase||'')}</span></div>`+
    `<div class="detail-actions">`+
    (actionable?`<button class="mini-button" data-act="pause">Pause</button>`:'')+
    (m.status==='paused'||m.status==='blocked'?`<button class="mini-button" data-act="resume">Resume</button>`:'')+
    (actionable?`<button class="mini-button" data-act="replan">Replan</button>`:'')+
    (actionable?`<button class="mini-button danger" data-act="cancel">Cancel</button>`:'')+
    `</div></div>`+
    `<div class="detail-section"><h3>Objective</h3><div class="objective">${esc(m.objective)}</div></div>`+
    `<div class="detail-section"><h3>Success criteria</h3>${critHtml}</div>`+
    `<div class="detail-section"><h3>Task graph (${nodes.length})</h3>${dag}</div>`+
    (m.blocked_reason?`<div class="detail-section"><h3>Blocked</h3><div class="objective">${esc(m.blocked_reason)}</div></div>`:'')+
    (m.completion?`<div class="detail-section"><h3>Completion</h3><div class="objective">Elapsed: ${m.completion.elapsed_s}s · ${m.completion.state}</div></div>`:'')+
    `<div class="detail-section"><h3>History</h3>${hist||'<div class="hist-row">empty</div>'}</div>`;
}

function renderApprovals(rows){
  $('#approvals').innerHTML=rows.map(a=>
    `<div class="mission-card"><div class="title">${esc(a.action)}</div>`+
    `<div class="meta">${esc(a.detail||'')}</div>`+
    `<div class="side-actions"><button class="mini-button" data-appr="${a.id}:approve">Approve</button>`+
    `<button class="mini-button danger" data-appr="${a.id}:deny">Deny</button></div></div>`
  ).join('')||'<div class="hist-row">none pending</div>';
}
function renderNotes(rows){
  $('#notifications').innerHTML=rows.slice(0,15).map(n=>
    `<div class="hist-row"><b>${esc(n.level)}</b> ${esc(n.title||n.message).slice(0,120)}</div>`
  ).join('')||'<div class="hist-row">none</div>';
}
function renderGoals(rows){
  $('#standingGoals').innerHTML=rows.map(g=>
    `<div class="mission-card"><div class="title">${esc(g.objective).slice(0,80)}</div>`+
    `<div class="meta"><span class="mstatus ${g.enabled?'executing':'paused'}">${g.enabled?'enabled':'disabled'}</span>`+
    `<button class="mini-button" data-goalrun="${g.id}">Run now</button></div></div>`
  ).join('')||'<div class="hist-row">none</div>';
}
function renderSchedules(rows){
  $('#schedules').innerHTML=rows.map(s=>
    `<div class="hist-row"><b>${esc(s.name)}</b> ${esc(s.kind)}${s.next_run?' · next '+new Date(s.next_run*1000).toLocaleString():''}</div>`
  ).join('')||'<div class="hist-row">none</div>';
}
function renderTriggers(rows){
  $('#triggers').innerHTML=rows.map(t=>
    `<div class="hist-row"><b>${esc(t.name)}</b> ${esc(t.event)} · fired ${t.fire_count||0}×</div>`
  ).join('')||'<div class="hist-row">none</div>';
}

document.addEventListener('click',async e=>{
  const card=e.target.closest('.mission-card[data-mid]');
  if(card){selected=card.dataset.mid;renderList();renderDetail();return;}
  const act=e.target.closest('[data-act]');
  if(act&&selected){
    try{await api(`/api/missions/${selected}/${act.dataset.act}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;
  }
  const appr=e.target.closest('[data-appr]');
  if(appr){
    const[id,verb]=appr.dataset.appr.split(':');
    try{await api(`/api/autonomy/approvals/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;
  }
  const gr=e.target.closest('[data-goalrun]');
  if(gr){try{await api(`/api/standing-goals/${gr.dataset.goalrun}/run`,'POST',{});refresh();}catch(err){alert(err.message);}}
});
$('#createMission').addEventListener('click',async()=>{
  const objective=$('#newObjective').value.trim();
  if(!objective)return;
  try{
    const d=await api('/api/missions','POST',{
      objective,scope:$('#newScope').value,
      autonomy_profile:$('#newProfile').value,start:true});
    $('#newObjective').value='';
    selected=d.mission.id;
    refresh();
  }catch(err){alert(err.message);}
});
$('#stopAutonomy').addEventListener('click',async()=>{
  if(!confirm('Stop all autonomous work? Running missions pause cooperatively.'))return;
  await api('/api/autonomy/stop','POST',{});refresh();
});
$('#resumeAutonomy').addEventListener('click',async()=>{
  await api('/api/autonomy/resume','POST',{});refresh();
});
$('#missionFilter').addEventListener('input',renderList);
refresh();
setInterval(refresh,3000);
