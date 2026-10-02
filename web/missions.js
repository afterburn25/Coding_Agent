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
    const [st,ms,appr,notes,sgoals,scheds,trigs,summary,goals,repairs]=await Promise.all([
      api('/api/autonomy/status'),
      api('/api/missions'),
      api('/api/autonomy/approvals?pending=1'),
      api('/api/autonomy/notifications'),
      api('/api/standing-goals'),
      api('/api/schedules'),
      api('/api/triggers'),
      api('/api/autonomy/summary'),
      api('/api/goals'),
      api('/api/self-repair'),
    ]);
    renderStatus(st);
    missions=ms.missions||[];
    renderList();
    renderDetail();
    renderApprovals(appr.approvals||[]);
    renderNotes(notes.notifications||[]);
    renderDailySummary(summary||{});
    renderGoals(sgoals.goals||[]);
    renderEvalGoals(goals.goals||[],goals.metrics||[]);
    renderRepairs(repairs.incidents||[]);
    renderSchedules(scheds.schedules||[]);
    renderTriggers(trigs.triggers||[]);
    if(Array.isArray(trigs.signals)&&trigs.signals.length&&
       $('#newTrigEvent')&&!$('#newTrigEvent').options.length)
      $('#newTrigEvent').innerHTML=trigs.signals.map(s=>
        `<option value="${esc(s)}">${esc(s)}</option>`).join('');
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
    `<div class="detail-section"><h3>Activity</h3><div id="missionActivity"><div class="hist-row">loading…</div></div></div>`+
    (m.blocked_reason?`<div class="detail-section"><h3>Blocked</h3><div class="objective">${esc(m.blocked_reason)}</div></div>`:'')+
    (m.completion?`<div class="detail-section"><h3>Completion</h3><div class="objective">Elapsed: ${m.completion.elapsed_s}s · ${m.completion.state}</div></div>`:'')+
    `<div class="detail-section"><h3>History</h3>${hist||'<div class="hist-row">empty</div>'}</div>`;
  loadMissionActivity(m.id);
}

async function loadMissionActivity(mid){
  // Devin-style live timeline rows recorded against this mission —
  // lets the user inspect what each node actually did, not just state.
  try{
    const r=await api('/api/activity?mission_id='+encodeURIComponent(mid));
    const el=$('#missionActivity');
    if(!el||selected!==mid)return;  // selection changed mid-fetch
    const rows=(r.activities||[]);
    el.innerHTML=rows.slice(-40).reverse().map(a=>{
      const timing=a.elapsed!=null?` · ${Number(a.elapsed).toFixed(1)}s`:'';
      const prog=a.progress!=null&&a.state==='running'?` ${Math.round(a.progress*100)}%`:'';
      return `<div class="hist-row"><b>${esc(a.category)}</b> ${esc(a.title)}`+
        ` <span class="mstatus ${a.state==='failed'?'failed':a.state==='running'?'executing':'done'}">${esc(a.state)}${prog}</span>`+
        `<span style="float:right">${esc((a.summary||'').slice(0,80))}${timing}</span></div>`;
    }).join('')||'<div class="hist-row">no activity recorded</div>';
  }catch(e){
    const el=$('#missionActivity');
    if(el&&selected===mid)el.innerHTML='<div class="hist-row">activity unavailable</div>';
  }
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
function renderDailySummary(s){
  const el=$('#dailySummary');
  if(!el)return;
  el.innerHTML=
    `<div class="hist-row">completed <b>${s.missions_completed||0}</b> · failed <b>${s.missions_failed||0}</b> · tasks <b>${s.tasks_completed||0}</b></div>`+
    ((s.missions_blocked||[]).map(t=>`<div class="hist-row"><b>blocked</b> ${esc(t).slice(0,80)}</div>`).join(''))+
    ((s.pending_approvals||[]).map(t=>`<div class="hist-row"><b>approval</b> ${esc(t).slice(0,80)}</div>`).join(''))+
    `<div class="hist-row">${s.notifications||0} unread · next run ${s.next_scheduled?new Date(s.next_scheduled*1000).toLocaleTimeString():'—'}</div>`;
}
const HEALTH_CLASS={healthy:'ok',satisfied:'ok',degrading:'warn',violated:'bad',blocked:'bad',unknown:'unknown'};
function renderEvalGoals(rows,metricSpecs){
  $('#evalGoals').innerHTML=rows.map(g=>{
    const live=(g.linked_missions||[]).filter(l=>!l.resolved&&l.status!=='missing'&&l.status!=='failed'&&l.status!=='completed'&&l.status!=='completed_with_warnings'&&l.status!=='cancelled').length;
    const last=g.last_evaluated_at?new Date(g.last_evaluated_at*1000).toLocaleTimeString():'never';
    const metrics=(g.metrics||[]).map(s=>`${s.key} ${s.op} ${s.target}`).join(' · ');
    return `<div class="mission-card"><div class="title">${esc(g.title).slice(0,90)}</div>`+
    `<div class="goal-evidence">${esc(g.health_detail||'')}</div>`+
    `<div class="meta"><span class="gstatus ${HEALTH_CLASS[g.health]||'unknown'}">${esc(g.health)}</span>`+
    `<span>${esc(g.type)}</span><span>${esc(g.priority)}</span>`+
    `<span>conf ${Math.round((g.confidence||0)*100)}%</span></div>`+
    `<div class="meta"><span style="flex:1">${metrics?esc(metrics.trim()):'no metrics'} · eval ${last}${live?` · ${live} live repair`+(live>1?'s':''):''}</span></div>`+
    `<div class="meta"><button class="mini-button" data-goalact="${g.id}:evaluate">Evaluate</button>`+
    (g.status!=='archived'&&g.status!=='completed'?`<button class="mini-button" data-goalact="${g.id}:${g.status==='active'?'disable':'enable'}">${g.status==='active'?'Pause':'Enable'}</button>`:'')+
    (g.status!=='archived'?`<button class="mini-button danger" data-goalact="${g.id}:archive">Archive</button>`:'')+
    `</div></div>`;
  }).join('')||'<div class="hist-row">none</div>';
  // Populate the metric picker once with measurable keys.
  const sel=$('#newEvalGoalMetric');
  if(sel&&!sel.options.length)
    sel.innerHTML='<option value="">metric…</option>'+metricSpecs.map(m=>
      `<option value="${esc(m.key)}">${esc(m.key)}${m.unit?` (${esc(m.unit)})`:''}</option>`).join('');
}
const REPAIR_STATE_CLASS={resolved:'ok',detected:'unknown',collecting:'unknown',localizing:'warn',diagnosing:'warn',planning:'warn',patching:'warn',testing:'warn',reviewing:'warn',canary:'warn',promoting:'warn',rolled_back:'warn',needs_human:'bad',abandoned:'unknown'};
function renderRepairs(rows){
  $('#repairPanel').innerHTML=rows.slice(0,12).map(r=>{
    const top=(r.hypotheses||[])[0];
    return `<div class="mission-card"><div class="title">${esc(r.error_class)} · ${esc(r.subsystem)}</div>`+
    `<div class="goal-evidence">${esc(top?top.detail:'')}${r.needs_human_reason?' — '+esc(r.needs_human_reason):''}</div>`+
    `<div class="meta"><span class="gstatus ${REPAIR_STATE_CLASS[r.state]||'unknown'}">${esc(r.state)}</span>`+
    `<span>${esc(r.severity)}</span><span>conf ${Math.round((r.confidence||0)*100)}%</span>`+
    `${r.occurrences>1?`<span>×${r.occurrences}</span>`:''}</div>`+
    `<div class="meta"><span style="flex:1">${new Date((r.last_seen||r.created_at)*1000).toLocaleTimeString()}</span>`+
    (r.state==='needs_human'?`<button class="mini-button" data-rep="${r.id}:retry">Retry</button>`:'')+
    `</div></div>`;
  }).join('')||'<div class="hist-row">no incidents</div>';
}
function renderGoals(rows){
  $('#standingGoals').innerHTML=rows.map(g=>
    `<div class="mission-card"><div class="title">${esc(g.objective).slice(0,80)}</div>`+
    `<div class="meta"><span class="mstatus ${g.enabled?'executing':'paused'}">${g.enabled?'enabled':'disabled'}</span>`+
    `<button class="mini-button" data-goalrun="${g.id}">Run now</button>`+
    `<button class="mini-button" data-goaltoggle="${g.id}:${g.enabled?'disable':'enable'}">${g.enabled?'Disable':'Enable'}</button></div></div>`
  ).join('')||'<div class="hist-row">none</div>';
}
function renderSchedules(rows){
  $('#schedules').innerHTML=rows.map(s=>
    `<div class="hist-row"><b>${esc(s.name)}</b> ${esc(s.kind)}${s.next_run?' · next '+new Date(s.next_run*1000).toLocaleString():''}`+
    ` <button class="mini-button" data-sched="${s.id}:${s.enabled===false?'enable':'disable'}">${s.enabled===false?'Enable':'Disable'}</button>`+
    ` <button class="mini-button danger" data-scheddel="${s.id}">×</button></div>`
  ).join('')||'<div class="hist-row">none</div>';
}
function renderTriggers(rows){
  $('#triggers').innerHTML=rows.map(t=>
    `<div class="hist-row"><b>${esc(t.name)}</b> ${esc(t.event)} · fired ${t.fire_count||0}×${t.enabled===false?' · off':''}`+
    ` <button class="mini-button" data-trig="${t.id}:${t.enabled===false?'enable':'disable'}">${t.enabled===false?'Enable':'Disable'}</button>`+
    ` <button class="mini-button danger" data-trigdel="${t.id}">×</button></div>`
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
  if(gr){try{await api(`/api/standing-goals/${gr.dataset.goalrun}/run`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const gt=e.target.closest('[data-goaltoggle]');
  if(gt){const[id,verb]=gt.dataset.goaltoggle.split(':');
    try{await api(`/api/standing-goals/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const ga=e.target.closest('[data-goalact]');
  if(ga){const[id,verb]=ga.dataset.goalact.split(':');
    try{await api(`/api/goals/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const rp=e.target.closest('[data-rep]');
  if(rp){const[id,verb]=rp.dataset.rep.split(':');
    try{await api(`/api/self-repair/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const sc=e.target.closest('[data-sched]');
  if(sc){const[id,verb]=sc.dataset.sched.split(':');
    try{await api(`/api/schedules/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const sd=e.target.closest('[data-scheddel]');
  if(sd){try{await api(`/api/schedules/${sd.dataset.scheddel}/delete`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const tg=e.target.closest('[data-trig]');
  if(tg){const[id,verb]=tg.dataset.trig.split(':');
    try{await api(`/api/triggers/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;}
  const td=e.target.closest('[data-trigdel]');
  if(td){try{await api(`/api/triggers/${td.dataset.trigdel}/delete`,'POST',{});refresh();}catch(err){alert(err.message);}}
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
$('#createGoal').addEventListener('click',async()=>{
  const objective=$('#newGoalObjective').value.trim();
  if(!objective)return;
  try{
    await api('/api/standing-goals','POST',{objective});
    $('#newGoalObjective').value='';refresh();
  }catch(err){alert(err.message);}
});
$('#createEvalGoal').addEventListener('click',async()=>{
  const title=$('#newEvalGoalTitle').value.trim();
  if(!title)return;
  const key=$('#newEvalGoalMetric').value;
  const target=parseFloat($('#newEvalGoalTarget').value);
  const body={title,
    type:'reliability',
    priority:$('#newEvalGoalPriority').value,
    escalation_policy:$('#newEvalGoalEsc').value};
  if(key&&Number.isFinite(target))
    body.metrics=[{key,op:$('#newEvalGoalOp').value,target}];
  try{
    await api('/api/goals','POST',body);
    $('#newEvalGoalTitle').value='';$('#newEvalGoalTarget').value='';
    refresh();
  }catch(err){alert(err.message);}
});
$('#createSchedule').addEventListener('click',async()=>{
  const name=$('#newSchedName').value.trim()||'schedule';
  const objective=$('#newSchedObjective').value.trim();
  if(!objective){alert('Objective required');return;}
  const kind=$('#newSchedKind').value;
  const raw=$('#newSchedValue').value.trim();
  const body={name,kind,action:{kind:'mission',objective}};
  if(kind==='interval'||kind==='once'){
    const mins=parseFloat(raw)||60;
    if(kind==='interval')body.interval_s=mins*60;
    else body.at=Math.floor(Date.now()/1000)+mins*60;
  }else{
    const m=/^(\d{1,2}):(\d{2})/.exec(raw||'03:00');
    body.hour=Math.min(23,parseInt(m?m[1]:'3',10));
    body.minute=Math.min(59,parseInt(m?m[2]:'0',10));
    // Scheduler uses Python tm_wday (Mon=0); JS getDay() is Sun=0.
    if(kind==='weekly')body.weekday=(new Date().getDay()+6)%7;
  }
  try{
    await api('/api/schedules','POST',body);
    $('#newSchedName').value='';$('#newSchedObjective').value='';
    $('#newSchedValue').value='';refresh();
  }catch(err){alert(err.message);}
});
$('#createTrigger').addEventListener('click',async()=>{
  const name=$('#newTrigName').value.trim();
  const objective=$('#newTrigObjective').value.trim();
  if(!objective){alert('Objective required');return;}
  const body={name:name||$('#newTrigEvent').value,
              event:$('#newTrigEvent').value,
              action:{kind:'mission',objective}};
  const watch=$('#newTrigWatch').value.trim();
  if(watch)body.watch=watch;
  try{
    await api('/api/triggers','POST',body);
    $('#newTrigName').value='';$('#newTrigObjective').value='';
    $('#newTrigWatch').value='';refresh();
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
