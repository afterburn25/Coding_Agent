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

// The 3s poll rebuilt every panel via innerHTML, destroying DOM nodes —
// which flickered and cleared any in-progress text selection. Write only
// when markup actually changed, and defer while the user is selecting.
function selectionWithin(el){
  const s=window.getSelection();
  if(!s||s.isCollapsed||!s.rangeCount)return false;
  const n=s.anchorNode;
  return !!(n&&el.contains(n.nodeType===1?n:n.parentNode));
}
function setHtml(el,html){
  if(!el)return;
  if(el._nxHtml===html)return;          // identical — leave DOM untouched
  if(selectionWithin(el))return;        // user is selecting — try next tick
  el._nxHtml=html;
  el.innerHTML=html;
}

async function refresh(){
  try{
    const [st,ms,appr,notes,sgoals,scheds,trigs,summary,goals,repairs,findings,ops,nstate]=await Promise.all([
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
      api('/api/findings'),
      api('/api/workers'),
      api('/api/nexus/state'),
    ]);
    renderStatus(st);
    renderNexusState(nstate||{});
    missions=ms.missions||[];
    renderList();
    renderDetail();
    renderApprovals(appr.approvals||[]);
    renderNotes(notes.notifications||[]);
    renderDailySummary(summary||{});
    renderGoals(sgoals.goals||[]);
    renderEvalGoals(goals.goals||[],goals.metrics||[]);
    renderRepairs(repairs.incidents||[]);
    renderFindings(findings.findings||[]);
    renderSchedules(scheds.schedules||[]);
    renderTriggers(trigs.triggers||[]);
    renderOps(ops||{});
    if(Array.isArray(trigs.signals)&&trigs.signals.length&&
       $('#newTrigEvent')&&!$('#newTrigEvent').options.length)
      $('#newTrigEvent').innerHTML=trigs.signals.map(s=>
        `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  }catch(e){
    setHtml($('#autonomyStatus'),'<span class="off">API unavailable</span>');
  }
}

function renderStatus(st){
  const on=st.running&&!st.stopped;
  setHtml($('#autonomyStatus'),
    `<div>Supervisor: <b class="${on?'on':'off'}">${st.stopped?'STOPPED':st.running?'ON':'OFF'}</b></div>`+
    `<div>Active missions: ${st.active_missions}</div>`+
    `<div>Pending approvals: ${st.pending_approvals}</div>`+
    `<div>Workers: ${st.workers} · Tick: ${st.tick_ms??'—'}ms</div>`+
    `<div>Resource mode: ${esc(st.resource_mode||'balanced')}</div>`+
    `<div id="nexusState"></div>`);
  $('#missionCount').textContent=`${missions.length} mission${missions.length===1?'':'s'}`;
}

function renderList(){
  const q=($('#missionFilter').value||'').toLowerCase();
  const rows=missions.filter(m=>!q||(m.title+m.objective+m.status).toLowerCase().includes(q));
  setHtml($('#missionList'),rows.map(m=>
    `<div class="mission-card${selected===m.id?' selected':''}" data-mid="${m.id}">`+
    `<div class="title">${esc(m.title)}</div>`+
    `<div class="meta"><span class="mstatus ${m.status}">${m.status}</span>`+
    `<span>${esc(m.scope)}</span><span>${esc(m.priority)}</span></div></div>`
  ).join('')||'<div class="empty" style="color:#4d5f7c;padding:30px;text-align:center">No missions yet</div>');
}

function critDot(c,ev){
  if(!ev)return'unknown';
  const found=(ev.criteria||[]).find(x=>x.kind===c.kind);
  return found?(found.met?'met':'unmet'):'unknown';
}

function renderDetail(){
  const el=$('#missionDetail');
  const m=missions.find(x=>x.id===selected);
  if(!m){setHtml(el,'<div class="empty">Select a mission</div>');return;}
  const evals=m.evaluator_history||[];
  const lastEval=evals.length?null:null;
  const critHtml=(m.success_criteria||[]).map(c=>{
    const hist=(m.evaluator_history||[]);
    return`<div class="criterion"><span class="dot ${'unknown'}"></span><span>${esc(c.description||c.kind)}</span></div>`;
  }).join('')||'<div class="criterion"><span class="dot unknown"></span><span>no explicit criteria — completion = all tasks finished</span></div>';
  const nodes=(m.graph&&m.graph.nodes)||[];
  // Coding-pipeline missions carry pipeline_stage metadata — render an
  // ordered stage strip so spec→implement→verify→review→serve→commit
  // progress reads at a glance instead of digging through the DAG.
  const stages=nodes.filter(n=>(n.metadata||{}).pipeline_stage)
    .map((n,i)=>`<div class="stage-chip" data-state="${esc(n.state||'')}"`+
      ` title="${esc(n.title||'')}${n.result&&n.result.ok===false?' — failed':''}">`+
      `<span class="snum">${i+1}</span><span class="sname">`+
      `${esc(n.metadata.pipeline_stage)}</span></div>`).join('');
  const dag=nodes.map(n=>
    `<div class="dag-node" data-state="${n.state}"><span class="nstate">${n.state}</span>`+
    `<b>${esc(n.title)}</b> <span style="color:#4d5f7c">· ${esc(n.kind)}</span>`+
    (n.stale_requirement?` <span class="mstatus blocked" title="Planned on a superseded requirement: ${esc(((n.metadata||{}).superseded_requirements||[]).join(', ')||'requirement changed')} — will re-plan on the new value">stale</span>`:'')+
    (n.result&&n.result.output?`<div class="nresult">${esc(String(n.result.output).slice(0,300))}</div>`:'')+
    `</div>`).join('')||'<div class="criterion"><span class="dot unknown"></span><span>No plan yet</span></div>';
  const hist=(m.history||[]).slice(-25).reverse().map(h=>
    `<div class="hist-row"><b>${esc(h.event)}</b> ${esc(h.detail||'')} <span style="float:right">${new Date((h.ts||0)*1000).toLocaleTimeString()}</span></div>`).join('');
  const actionable=['draft','ready','active','executing','paused','blocked','waiting_approval','waiting_dependency','replanning'].includes(m.status);
  setHtml(el,
    `<div class="detail-head"><div><h2>${esc(m.title)}</h2>`+
    `<span class="mstatus ${m.status}">${m.status}</span> <span style="color:#7f91ad;font-size:11px">${esc(m.phase||'')}</span></div>`+
    `<div class="detail-actions">`+
    (actionable?`<button class="mini-button" data-act="pause">Pause</button>`:'')+
    (m.status==='paused'||m.status==='blocked'?`<button class="mini-button" data-act="resume">Resume</button>`:'')+
    (actionable?`<button class="mini-button" data-act="replan">Replan</button>`:'')+
    (actionable?`<button class="mini-button danger" data-act="cancel">Cancel</button>`:'')+
    `</div></div>`+
    `<div class="detail-section"><h3>Objective</h3><div class="objective">${esc(m.objective)}</div></div>`+
    `<div class="detail-section"><h3>Requirements</h3><div id="missionReqs"><div class="hist-row">loading…</div></div></div>`+
    `<div class="detail-section"><h3>Success criteria</h3>${critHtml}</div>`+
    `<div class="detail-section"><h3>Workstreams</h3><div id="missionWs"><div class="hist-row">loading…</div></div></div>`+
    `<div class="detail-section"><h3>Mission context</h3><div id="missionCapsule"><div class="hist-row">loading…</div></div></div>`+
    (stages?`<div class="detail-section"><h3>Pipeline</h3><div class="stage-strip">${stages}</div></div>`:'')+
    `<div class="detail-section"><h3>Task graph (${nodes.length})</h3>${dag}</div>`+
    `<div class="detail-section"><h3>Activity</h3><div id="missionActivity"><div class="hist-row">loading…</div></div></div>`+
    `<div class="detail-section"><h3>Evidence</h3><div id="missionEvidence"><div class="hist-row">loading…</div></div></div>`+
    (m.blocked_reason?`<div class="detail-section"><h3>Blocked</h3><div class="objective">${esc(m.blocked_reason)}</div></div>`:'')+
    (m.completion?`<div class="detail-section"><h3>Completion</h3><div class="objective">Elapsed: ${m.completion.elapsed_s}s · ${m.completion.state}</div></div>`:'')+
    `<div class="detail-section"><h3>History</h3>${hist||'<div class="hist-row">empty</div>'}</div>`);
  loadMissionActivity(m.id);
  loadMissionRequirements(m.id);
  loadMissionEvidence(m.id);
  loadMissionWorkstreams(m.id);
}

const WS_MARKS={integrated:'✓',active:'●',ready:'◐',awaiting_review:'◐',
  integration_ready:'◐',planned:'○',paused:'‖',blocked:'⊘',
  failed:'✕',abandoned:'–'};
async function loadMissionWorkstreams(mid){
  // §34-35 — the durable workstream view: per-lane progress, controls
  // (pause/resume/drop/prioritize), the compact context capsule, and
  // mission metrics. Replaces digging through the raw DAG.
  try{
    const r=await api('/api/missions/'+encodeURIComponent(mid)+'/workstreams');
    const wsel=$('#missionWs');
    if(wsel&&selected===mid){
      const rows=(r.workstreams||[]);
      setHtml(wsel,rows.map(w=>{
        const live=['ready','active','planned'].includes(w.status);
        const paus=['paused','blocked'].includes(w.status);
        const drop=!['integrated','abandoned','failed'].includes(w.status);
        return `<div class="mission-card"><div class="title">`+
          `${WS_MARKS[w.status]||'○'} ${esc(w.title)} `+
          `<span class="mstatus ${w.status==='failed'?'failed':w.status==='integrated'?'done':live?'executing':''}">${esc(w.status)}</span> `+
          `<span class="pill">${esc(w.priority||'p2')}</span></div>`+
          `<div class="meta">${w.tasks_done||0}/${w.tasks||0} tasks · `+
          `${Math.round((w.progress||0)*100)}%`+
          (w.blocker?` — ${esc(w.blocker)}`:'')+
          (w.role?` · ${esc(w.role)}`:'')+`</div>`+
          ((w.acceptance||[]).length?`<div class="meta" style="color:#4d5f7c">${w.acceptance.map(a=>'· '+esc(a)).join('<br>')}</div>`:'')+
          `<div class="side-actions">`+
          (live?`<button class="mini-button" data-wsact="${w.id}:pause">Pause</button>`:'')+
          (paus?`<button class="mini-button" data-wsact="${w.id}:resume">Resume</button>`:'')+
          (live?`<button class="mini-button" data-wsact="${w.id}:reprioritize">P0</button>`:'')+
          (drop?`<button class="mini-button danger" data-wsact="${w.id}:drop">Drop</button>`:'')+
          `</div></div>`;
      }).join('')||'<div class="hist-row">no workstreams — single-lane mission</div>');
    }
    const cel=$('#missionCapsule');
    if(cel&&selected===mid){
      const cap=r.capsule||{};
      const rows=[];
      if((r.decisions||[]).length)
        rows.push(`<div class="hist-row"><b>decisions</b> `+
          r.decisions.slice(0,6).map(d=>esc(d.decision)).join(' · ')+`</div>`);
      if((cap.known_failures||[]).length)
        rows.push(`<div class="hist-row"><b>known failures</b> `+
          cap.known_failures.slice(0,4).map(f=>esc(f)).join(' · ')+`</div>`);
      if((cap.blockers||[]).length)
        rows.push(`<div class="hist-row"><b>blockers</b> `+
          cap.blockers.slice(0,4).map(b=>esc(b)).join(' · ')+`</div>`);
      const met=r.metrics||{};
      const mbits=['compactions','repair_cycles','escalations',
        'ownership_conflicts','checkpoints'].filter(k=>met[k])
        .map(k=>`${k.replace('_',' ')} ${met[k]}`);
      if(mbits.length)
        rows.push(`<div class="hist-row"><b>metrics</b> ${esc(mbits.join(' · '))}</div>`);
      setHtml(cel,rows.join('')||'<div class="hist-row">no capsule yet</div>');
    }
  }catch(e){
    setHtml($('#missionWs'),'<div class="hist-row">workstreams unavailable</div>');
    setHtml($('#missionCapsule'),'');
  }
}

const REQ_MARKS={verified:'✓',implemented:'◐',in_progress:'~',planned:'~',
  not_started:'·',failed:'✕',blocked:'⊘',deferred:'→',rejected:'–'};
async function loadMissionRequirements(mid){
  // Requirements are first-class entities — derived rows are marked
  // 'inferred' so users can tell Nexus-derived criteria from explicit asks.
  try{
    const r=await api('/api/requirements?scope_type=mission&scope_id='+encodeURIComponent(mid));
    const el=$('#missionReqs');
    if(!el||selected!==mid)return;
    const rows=(r.requirements||[]);
    setHtml(el,rows.map(q=>
      `<div class="criterion" title="${esc(q.source)}${q.inferred?' (inferred)':''}">`+
      `<span class="dot ${q.status==='verified'?'met':['failed','blocked'].includes(q.status)?'unmet':'unknown'}"></span>`+
      `<span>${esc(REQ_MARKS[q.status]||'·')} ${esc(q.description)}`+
      (q.inferred?` <span style="color:#4d5f7c;font-size:10px">· inferred</span>`:'')+
      `</span></div>`
    ).join('')||'<div class="criterion"><span class="dot unknown"></span><span>no requirements derived</span></div>');
  }catch(e){
    const el=$('#missionReqs');
    setHtml(el,'<div class="hist-row">requirements unavailable</div>');
  }
}

async function loadMissionActivity(mid){
  // Devin-style live timeline rows recorded against this mission —
  // lets the user inspect what each node actually did, not just state.
  try{
    const r=await api('/api/activity?mission_id='+encodeURIComponent(mid));
    const el=$('#missionActivity');
    if(!el||selected!==mid)return;  // selection changed mid-fetch
    const rows=(r.activities||[]);
    setHtml(el,rows.slice(-40).reverse().map(a=>{
      const timing=a.elapsed!=null?` · ${Number(a.elapsed).toFixed(1)}s`:'';
      const prog=a.progress!=null&&a.state==='running'?` ${Math.round(a.progress*100)}%`:'';
      return `<div class="hist-row"><b>${esc(a.category)}</b> ${esc(a.title)}`+
        ` <span class="mstatus ${a.state==='failed'?'failed':a.state==='running'?'executing':'done'}">${esc(a.state)}${prog}</span>`+
        `<span style="float:right">${esc((a.summary||'').slice(0,80))}${timing}</span></div>`;
    }).join('')||'<div class="hist-row">no activity recorded</div>');
  }catch(e){
    const el=$('#missionActivity');
    if(el&&selected===mid)setHtml(el,'<div class="hist-row">activity unavailable</div>');
  }
}

async function loadMissionEvidence(mid){
  // Action Evidence Ledger rollup — what the mission verifiably did
  // (status counts + last verified/failed actions), not narration.
  try{
    const r=await api('/api/missions/'+encodeURIComponent(mid)+'/evidence');
    const el=$('#missionEvidence');
    if(!el||selected!==mid)return;
    const counts=Object.entries(r.statuses||{})
      .map(([s,c])=>`<span class="mstatus ${s==='verified'?'done':s==='failed'?'failed':s==='awaiting_approval'?'waiting_approval':'executing'}">${esc(s)} ${c}</span>`)
      .join(' ');
    const vf=(r.recent_verified||[]).map(a=>`<div class="hist-row"><b>verified</b> ${esc(a)}</div>`).join('');
    const ff=(r.recent_failures||[]).map(a=>`<div class="hist-row"><b>failed</b> ${esc(a)}</div>`).join('');
    setHtml(el,(r.actions?
      `<div class="hist-row">${counts}</div>${vf}${ff}`:
      '<div class="hist-row">no recorded actions</div>'));
  }catch(e){
    const el=$('#missionEvidence');
    if(el&&selected===mid)setHtml(el,'<div class="hist-row">evidence unavailable</div>');
  }
}

function renderApprovals(rows){
  setHtml($('#approvals'),rows.map(a=>
    `<div class="mission-card"><div class="title">${esc(a.action)}</div>`+
    `<div class="meta">${esc(a.detail||'')}</div>`+
    `<div class="side-actions"><button class="mini-button" data-appr="${a.id}:approve">Approve</button>`+
    `<button class="mini-button danger" data-appr="${a.id}:deny">Deny</button></div></div>`
  ).join('')||'<div class="hist-row">none pending</div>');
}
function renderNexusState(ns){
  const el=$('#nexusState');
  if(!el||!ns||!ns.state)return;
  // Operational-state line under the supervisor card — what Nexus is
  // doing right now, in one glance. Lives in its own placeholder inside
  // #autonomyStatus so it no longer re-appends a node every tick.
  setHtml(el,`<div class="hist-row">State: <b>${esc(ns.state)}</b>`+
    (ns.focus?` — ${esc(ns.focus)}`:'')+
    (ns.next_action?`<br>Next: ${esc(ns.next_action)}`:'')+
    ` <span class="pill">${esc(ns.resource_pressure||'')}</span></div>`);
}
function renderOps(ops){
  const cap=ops.capacity||{},hw=ops.hardware||{},sch=ops.schedulable||{};
  setHtml($('#opsCapacity'),
    `<div class="hist-row">Workers: <b>${cap.active||0} active</b> / ${cap.ceiling||'—'} currently safe</div>`+
    `<div class="hist-row">CPU ${hw.cpu_util!=null?Math.round(hw.cpu_util*100)+'%':'—'} · `+
    `RAM ${((hw.ram_free_mb||0)/1024).toFixed(1)}/${((hw.ram_total_mb||0)/1024).toFixed(0)} GB free · `+
    `VRAM ${((hw.vram_free_mb||0)/1024).toFixed(1)}/${((hw.vram_total_mb||0)/1024).toFixed(1)} GB free</div>`+
    `<div class="hist-row">schedulable: ${(sch.cpu_cores||0).toFixed(1)} cores · `+
    `${((sch.ram_mb||0)/1024).toFixed(1)} GB RAM · ${((sch.vram_mb||0)/1024).toFixed(1)} GB VRAM</div>`);
  setHtml($('#opsWorkers'),(ops.workers||[]).map(w=>
    `<div class="mission-card"><div class="title">${esc(w.name?`${w.name} · ${w.role||'worker'}`:(w.role||'worker'))} · ${esc(w.model_tier||'tool')}</div>`+
    `<div class="meta">${esc(w.title||'')} — ${esc(w.status)}`+
    (w.elapsed_s?` · ${Math.round(w.elapsed_s)}s`:'')+
    (w.branch?` · <code>${esc(w.branch)}</code>`:'')+`</div></div>`
  ).join('')||'<div class="hist-row">no active workers</div>');
  setHtml($('#opsQueue'),(ops.queue||[]).map(q=>
    `<div class="mission-card"><div class="title">#${q.position} ${esc(q.title||'')}</div>`+
    `<div class="meta">${esc(q.message||q.reason||'queued')}`+
    (q.reason_detail?` — ${esc(q.reason_detail)}`:'')+`</div>`+
    `<div class="side-actions"><button class="mini-button danger" data-wcancel="${esc(q.id)}">Cancel</button></div></div>`
  ).join('')||'<div class="hist-row">queue empty</div>');
}

function renderNotes(rows){
  setHtml($('#notifications'),rows.slice(0,15).map(n=>
    `<div class="hist-row"><b>${esc(n.level)}</b> ${esc(n.title||n.message).slice(0,120)}</div>`
  ).join('')||'<div class="hist-row">none</div>');
}
function renderDailySummary(s){
  const el=$('#dailySummary');
  if(!el)return;
  setHtml(el,
    `<div class="hist-row">completed <b>${s.missions_completed||0}</b> · failed <b>${s.missions_failed||0}</b> · tasks <b>${s.tasks_completed||0}</b></div>`+
    ((s.missions_blocked||[]).map(t=>`<div class="hist-row"><b>blocked</b> ${esc(t).slice(0,80)}</div>`).join(''))+
    ((s.pending_approvals||[]).map(t=>`<div class="hist-row"><b>approval</b> ${esc(t).slice(0,80)}</div>`).join(''))+
    `<div class="hist-row">${s.notifications||0} unread · next run ${s.next_scheduled?new Date(s.next_scheduled*1000).toLocaleTimeString():'—'}</div>`);
}
const HEALTH_CLASS={healthy:'ok',satisfied:'ok',degrading:'warn',violated:'bad',blocked:'bad',unknown:'unknown'};
function renderEvalGoals(rows,metricSpecs){
  setHtml($('#evalGoals'),rows.map(g=>{
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
  }).join('')||'<div class="hist-row">none</div>');
  // Populate the metric picker once with measurable keys.
  const sel=$('#newEvalGoalMetric');
  if(sel&&!sel.options.length)
    sel.innerHTML='<option value="">metric…</option>'+metricSpecs.map(m=>
      `<option value="${esc(m.key)}">${esc(m.key)}${m.unit?` (${esc(m.unit)})`:''}</option>`).join('');
}
const REPAIR_STATE_CLASS={resolved:'ok',detected:'unknown',collecting:'unknown',localizing:'warn',diagnosing:'warn',planning:'warn',patching:'warn',testing:'warn',reviewing:'warn',canary:'warn',promoting:'warn',rolled_back:'warn',needs_human:'bad',abandoned:'unknown'};
function renderRepairs(rows){
  setHtml($('#repairPanel'),rows.slice(0,12).map(r=>{
    const top=(r.hypotheses||[])[0];
    return `<div class="mission-card"><div class="title">${esc(r.error_class)} · ${esc(r.subsystem)}</div>`+
    `<div class="goal-evidence">${esc(top?top.detail:'')}${r.needs_human_reason?' — '+esc(r.needs_human_reason):''}</div>`+
    `<div class="meta"><span class="gstatus ${REPAIR_STATE_CLASS[r.state]||'unknown'}">${esc(r.state)}</span>`+
    `<span>${esc(r.severity)}</span><span>conf ${Math.round((r.confidence||0)*100)}%</span>`+
    `${r.occurrences>1?`<span>×${r.occurrences}</span>`:''}</div>`+
    `<div class="meta"><span style="flex:1">${new Date((r.last_seen||r.created_at)*1000).toLocaleTimeString()}</span>`+
    (r.state==='needs_human'?`<button class="mini-button" data-rep="${r.id}:retry">Retry</button>`+
    `<button class="mini-button" data-rep="${r.id}:abandon">Dismiss</button>`:'')+
    `</div></div>`;
  }).join('')||'<div class="hist-row">no incidents</div>');
}
const FINDING_SEV_CLASS={critical:'bad',high:'warn',normal:'unknown',low:'unknown'};
function renderFindings(rows){
  setHtml($('#findingsPanel'),rows.slice(0,10).map(f=>{
    const ev=Object.entries(f.evidence||{}).filter(([k,v])=>typeof v!=='object')
      .map(([k,v])=>`${esc(k)}=${esc(String(v))}`).join(' ');
    return `<div class="mission-card"><div class="title">${esc(f.title)}</div>`+
    `<div class="goal-evidence">${esc(ev)}${f.routed_to?` → ${esc(f.routed_to)}`:''}</div>`+
    `<div class="meta"><span class="gstatus ${FINDING_SEV_CLASS[f.severity]||'unknown'}">${esc(f.severity)}</span>`+
    `<span>${esc(f.route)}</span><span>conf ${Math.round((f.confidence||0)*100)}%</span>`+
    `${f.sightings>1?`<span>×${f.sightings}</span>`:''}`+
    `<button class="mini-button" data-find="${f.id}:dismiss">Dismiss</button></div></div>`;
  }).join('')||'<div class="hist-row">no signals</div>');
}
function renderGoals(rows){
  setHtml($('#standingGoals'),rows.map(g=>
    `<div class="mission-card"><div class="title">${esc(g.objective).slice(0,80)}</div>`+
    `<div class="meta"><span class="mstatus ${g.enabled?'executing':'paused'}">${g.enabled?'enabled':'disabled'}</span>`+
    `<button class="mini-button" data-goalrun="${g.id}">Run now</button>`+
    `<button class="mini-button" data-goaltoggle="${g.id}:${g.enabled?'disable':'enable'}">${g.enabled?'Disable':'Enable'}</button></div></div>`
  ).join('')||'<div class="hist-row">none</div>');
}
function renderSchedules(rows){
  setHtml($('#schedules'),rows.map(s=>
    `<div class="hist-row"><b>${esc(s.name)}</b> ${esc(s.kind)}${s.next_run?' · next '+new Date(s.next_run*1000).toLocaleString():''}`+
    ` <button class="mini-button" data-sched="${s.id}:${s.enabled===false?'enable':'disable'}">${s.enabled===false?'Enable':'Disable'}</button>`+
    ` <button class="mini-button danger" data-scheddel="${s.id}">×</button></div>`
  ).join('')||'<div class="hist-row">none</div>');
}
function renderTriggers(rows){
  setHtml($('#triggers'),rows.map(t=>
    `<div class="hist-row"><b>${esc(t.name)}</b> ${esc(t.event)} · fired ${t.fire_count||0}×${t.enabled===false?' · off':''}`+
    ` <button class="mini-button" data-trig="${t.id}:${t.enabled===false?'enable':'disable'}">${t.enabled===false?'Enable':'Disable'}</button>`+
    ` <button class="mini-button danger" data-trigdel="${t.id}">×</button></div>`
  ).join('')||'<div class="hist-row">none</div>');
}

document.addEventListener('click',async e=>{
  const card=e.target.closest('.mission-card[data-mid]');
  if(card){selected=card.dataset.mid;renderList();renderDetail();return;}
  const act=e.target.closest('[data-act]');
  if(act&&selected){
    try{await api(`/api/missions/${selected}/${act.dataset.act}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;
  }
  const wsa=e.target.closest('[data-wsact]');
  if(wsa&&selected){
    const[wsid,op]=wsa.dataset.wsact.split(':');
    const body=op==='reprioritize'?{priority:'p0'}:{};
    try{await api(`/api/missions/${selected}/workstreams/${wsid}/${op}`,'POST',body);refresh();}catch(err){alert(err.message);}
    return;
  }
  const appr=e.target.closest('[data-appr]');
  if(appr){
    const[id,verb]=appr.dataset.appr.split(':');
    try{await api(`/api/autonomy/approvals/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
    return;
  }
  const wc=e.target.closest('[data-wcancel]');
  if(wc){
    try{await api('/api/workers/cancel','POST',{id:wc.dataset.wcancel});refresh();}catch(err){alert(err.message);}
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
    try{
      let r=await api(`/api/self-repair/${id}/${verb}`,'POST',{});
      if(r.needs_approval){
        if(!confirm(`${r.permission||'repair.manage'} approval is required to ${verb} repair ${id}. Continue?`))return;
        r=await api(`/api/self-repair/${id}/${verb}`,'POST',{approve:true});
      }
      if(r.ok===false&&r.error)alert(r.error);
      refresh();
    }catch(err){alert(err.message);}
    return;}
  const fd=e.target.closest('[data-find]');
  if(fd){const[id,verb]=fd.dataset.find.split(':');
    try{await api(`/api/findings/${id}/${verb}`,'POST',{});refresh();}catch(err){alert(err.message);}
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
