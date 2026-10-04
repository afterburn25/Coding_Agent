/* Nexus Core · System — health, twin, RAG, LSP, skills, artifacts, backups, eval, experiments */
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
const fmtTs=t=>t?new Date(t*1000).toLocaleString():'—';

/* ---------- Health ---------- */
function renderHealth(h){
  const states=h.states||{};
  $('#overallCard').innerHTML=`Overall: <span class="state st-${esc(h.overall)}">${esc(h.overall||'—')}</span>`;
  $('#healthStates').innerHTML=Object.keys(states).length
    ? Object.entries(states).map(([n,s])=>
      `<div class="health-card"><div class="name">${esc(n)}</div><span class="state st-${esc(s)}">${esc(s)}</span></div>`).join('')
    : '<div class="off">No components registered.</div>';
  $('#healthHistory').innerHTML=(h.history||[]).slice().reverse().map(e=>
    `<div class="list-row"><b>${esc(e.component)}</b> ${esc(e.from||'')} → ${esc(e.to||e.state||'')} <span class="meta">${fmtTs(e.ts)}${e.detail?' · '+esc(e.detail):''}</span></div>`
  ).join('')||'<div class="off">No transitions recorded.</div>';
}

/* ---------- Nexus Brain ---------- */
function renderBrain(b,trace){
  const regions=(b&&b.regions)||{};
  $('#brainRegions').innerHTML=Object.keys(regions).length
    ? Object.entries(regions).map(([n,r])=>
      `<div class="health-card"><div class="name">${esc(n)}</div><span class="state st-${esc(r.state)}">${esc(r.state)}</span>
       <div class="meta">${esc(r.handled??0)} events${r.errors?' · '+esc(r.errors)+' errors':''}${r.last_error?' · '+esc(r.last_error):''}</div></div>`).join('')
    : '<div class="off">Nexus Brain not initialized.</div>';
  const specs=(b&&b.specialists)||{};
  $('#brainSpecialists').innerHTML=Object.keys(specs).length
    ? Object.entries(specs).map(([n,s])=>
      `<div><span class="k">${esc(n)}</span>${esc(s.domain||'')} · caps ${esc((s.capabilities||[]).length)}</div>`).join('')
    : '<div class="off">—</div>';
  const events=(trace&&trace.events)||[];
  $('#brainTrace').innerHTML=events.length
    ? events.slice(-40).reverse().map(e=>{
      const c=e.content||{};
      const det=c.summary||c.route||c.action||c.component||c.event||'';
      return `<div class="list-row"><b>${esc(e.source||'?')}</b> <span class="pill">${esc(e.type)}</span>
        ${e.destination?'→ '+esc(e.destination):''}
        <div class="meta">${esc(det)}${e.correlation_id?' · corr '+esc(e.correlation_id):''} · ${fmtTs(e.ts)}</div></div>`;
    }).join('')
    : '<div class="off">No cognitive events traced yet.</div>';
}

/* ---------- Diagnostics ---------- */
function renderDiagnostics(d){
  const models=(d.models||[]).map(m=>{
    const b=m.backend||{};
    const state=b.exit_code!=null?`exited ${b.exit_code}`:(b.pid?`pid ${b.pid}`:'not running');
    const note=(b.crash_reason||b.error||'').trim();
    const tail=(b.log_tail||'').trim();
    return `<div><span class="k">${esc(m.id)}</span>${esc(m.runtime||'')} · ${esc(state)}`+
      (b.restarts?` · ${b.restarts} restart${b.restarts===1?'':'s'}`:'')+
      (note?` · ${esc(note)}`:'')+
      (tail?`<div class="meta">${esc(tail.split('\n').slice(-3).join('\n'))}</div>`:'')+`</div>`;
  }).join('');
  $('#diagModels').innerHTML=models||'<div class="off">No model backends.</div>';
  const failures=(d.recent_failures||[]).slice().reverse().map(f=>
    `<div class="list-row"><b>${esc(f.subsystem||'?')} · ${esc(f.kind||'')}</b>
     <span class="pill">${esc(f.phase||'')}</span>
     <div class="meta">${esc(f.host||'')}${f.port?':'+f.port:''} ${f.model_id?'· '+esc(f.model_id):''}${f.request_id?' · req '+esc(f.request_id):''} · ${fmtTs(f.time)}${f.streaming?' · '+f.chunks_received+' chunks':''}</div>
     <div class="meta">${esc(f.exception||f.detail||'')}</div>
     <div class="meta">recovery: ${esc(f.recovery||'pending')}</div></div>`).join('');
  $('#diagFailures').innerHTML=failures||'<div class="off">No transport failures recorded this session.</div>';
  const hist=(d.crash_history||[]).slice().reverse().map(e=>{
    if(e.recovery_for!==undefined&&e.recovery!==undefined&&!e.kind){
      return `<div class="list-row"><b>recovery</b> <span class="meta">${esc(e.subsystem||'')} ${e.recovery_for?'· req '+esc(e.recovery_for):''} — ${esc(e.recovery)} · ${fmtTs(e.time)}</span></div>`;
    }
    return `<div class="list-row"><b>${esc(e.subsystem||'?')} · ${esc(e.kind||'')}</b>
     <div class="meta">${esc(e.exception||e.detail||'')} · ${fmtTs(e.time)}${e.recovery?' · '+esc(e.recovery):''}</div></div>`;
  }).join('');
  $('#diagHistory').innerHTML=hist||'<div class="off">No persisted crash history.</div>';
}

/* ---------- Digital Twin ---------- */
function renderTwin(t){
  const hw=t.hardware||{};
  const gpus=(hw.gpus||[]).map(g=>`${esc(g.name||'GPU')} (${esc(g.vram_total_gb??'?')} GB VRAM)`).join(', ')||'—';
  $('#twinCard').innerHTML=[
    `<div><span class="k">Hardware fingerprint</span>${esc((t.fingerprint||'').slice(0,16))||'—'}…</div>`,
    `<div><span class="k">GPUs</span>${gpus}</div>`,
    `<div><span class="k">RAM</span>${esc(hw.ram_total_gb??'?')} GB total · ${esc(hw.ram_free_gb??'?')} GB free</div>`,
    `<div><span class="k">Hardware samples</span>${esc(t.samples)}</div>`,
    `<div><span class="k">Model measures</span>${esc(t.model_measures)}</div>`,
  ].join('');
}

/* ---------- RAG ---------- */
function renderRagStats(r){
  const s=r.stats||{};
  $('#ragStats').innerHTML=
    `<div><span class="k">Indexed files</span>${esc(s.files??0)}</div>`+
    `<div><span class="k">Symbols</span>${esc(s.symbols??0)}</div>`+
    `<div><span class="k">Chunks</span>${esc(s.chunks??0)}</div>`;
  if(r.results)$('#ragResults').innerHTML=r.results.map(h=>
    `<div class="list-row"><b>${esc(h.name||h.path||'')}</b> <span class="pill">${esc(h.type||h.kind||'')}</span>
     <div class="meta">${esc(h.file||'')}${h.line?' : '+h.line:''}</div></div>`).join('')
    ||'<div class="off">No matches.</div>';
}

/* ---------- Knowledge graph ---------- */
function renderKnowledge(k){
  if(k&&k.available===false){
    $('#kgStats').innerHTML='<div class="off">Knowledge graph unavailable.</div>';
    return;
  }
  const s=(k&&k.stats)||{};
  $('#kgStats').innerHTML=
    `<div><span class="k">Entities</span>${esc(s.entities??0)}</div>`+
    `<div><span class="k">Edges</span>${esc(s.edges??0)}</div>`;
  if(k&&k.entities)$('#kgResults').innerHTML=k.entities.map(e=>
    `<div class="list-row"><b>${esc(e.name)}</b> <span class="pill">${esc(e.kind)}</span>
     <div class="meta">${esc(e.id)}</div></div>`).join('')
    ||'<div class="off">No entities.</div>';
  if(k&&k.context!==undefined)$('#kgContext').innerHTML=k.context
    ?`<pre class="ctx">${esc(k.context)}</pre>`:'';
}

/* ---------- LSP ---------- */
function renderLsp(l){
  const servers=l.servers||l||{};
  $('#lspStatus').innerHTML=typeof servers==='object'
    ? Object.entries(servers).map(([n,v])=>
        `<div><span class="k">${esc(n)}</span>${esc(typeof v==='object'?JSON.stringify(v):v)}</div>`).join('')
      ||'<div class="off">No servers.</div>'
    : `<div>${esc(servers)}</div>`;
}

/* ---------- Skills ---------- */
function renderSkills(list){
  $('#skillsList').innerHTML=list.map(s=>
    `<div class="list-row"><b>${esc(s.name)}</b> <span class="pill">v${esc(s.version)}</span>
     ${s.bundled?'<span class="pill">bundled</span>':''}
     <span class="pill">${s.enabled?'enabled':'disabled'}</span>
     <div class="meta">${esc(s.description||'')}${s.author?' · '+esc(s.author):''}</div>
     <div class="meta">${(s.capabilities||[]).map(c=>`<span class="pill">${esc(c)}</span>`).join('')}
     ${(s.permissions||[]).map(p=>`<span class="pill">perm ${esc(p)}</span>`).join('')}</div>
     ${(s.dependencies||[]).length?`<div class="meta">deps: ${(s.dependencies||[]).map(d=>esc(d)).join(', ')}</div>`:''}
     <div class="actions">
       <button class="mini-button" data-skill-toggle="${esc(s.name)}" data-en="${s.enabled?0:1}">${s.enabled?'Disable':'Enable'}</button>
       <button class="mini-button" data-skill-verify="${esc(s.name)}">Verify</button>
       ${s.rollback_count?`<button class="mini-button" data-skill-rollback="${esc(s.name)}">Rollback</button>`:''}
       ${s.bundled?'':`<button class="mini-button danger" data-skill-remove="${esc(s.name)}">Remove</button>`}
     </div>
    </div>`).join('')||'<div class="off">No skills installed.</div>';
}

function renderSkillResult(r){
  $('#skillResult').innerHTML=r?`<div><b>${esc(r.target||r.name||r.skill||'skill')}</b>
    <span class="pill">${r.needs_approval?'approval needed':(r.ok===false?'failed':'ok')}</span>
    ${(r.errors||[]).map(x=>`<div class="meta">${esc(x)}</div>`).join('')}
    ${(r.warnings||[]).map(x=>`<div class="meta">warning: ${esc(x)}</div>`).join('')}
    ${(r.requested_permissions||[]).length?`<div class="meta">permissions: ${r.requested_permissions.map(x=>esc(x)).join(', ')}</div>`:''}
    </div>`:'';
}

const SKILL_API={
  verify:'/api/skills/verify',install:'/api/skills/install',
  update:'/api/skills/update',enable:'/api/skills/enable',
  disable:'/api/skills/disable',rollback:'/api/skills/rollback',
  remove:'/api/skills/remove',health:'/api/skills/health'
};
async function skillApi(action,body){
  const endpoint=SKILL_API[action]||('/api/skills/'+action);
  let r=await api(endpoint,'POST',body);
  if(r&&r.needs_approval){
    const label=`${action} ${r.skill||body.name||body.path||''}`;
    const perms=(r.requested_permissions||[]).join(', ')||'none declared';
    if(!confirm(`${label} requires approval.\nRequested permissions: ${perms}\nContinue?`))return r;
    r=await api(endpoint,'POST',{...body,approve:true});
  }
  renderSkillResult(r);
  return r;
}

/* ---------- Connectors ---------- */
function renderConnectors(list){
  $('#connectorsList').innerHTML=list.map(c=>{
    const h=c.last_health||{};
    const ok=h.ok===true?'healthy':(h.ok===false?'unhealthy':'unchecked');
    return `<div class="list-row"><b>${esc(c.name)}</b> <span class="pill">${esc(ok)}</span>
     ${c.enabled?'<span class="pill">enabled</span>':'<span class="pill">disabled</span>'}
     ${c.authed?'<span class="pill">authed</span>':''}
     ${c.errors?`<span class="pill">${esc(c.errors)} errors</span>`:''}
     <div class="meta">${(c.capabilities||[]).map(x=>`<span class="pill">${esc(x)}</span>`).join('')}
     ${c.permission?' · perm '+esc(c.permission):''}</div>
    </div>`}).join('')||'<div class="off">No connectors registered.</div>';
}

/* ---------- Jobs ---------- */
function renderJobs(list){
  $('#jobsList').innerHTML=list.map(j=>
    `<div class="list-row"><b>${esc(j.title||j.id)}</b> <span class="pill">${esc(j.kind||'')}</span>
     <span class="state st-${esc(j.state)}">${esc(j.state||'')}</span>
     ${j.progress?` <span class="pill">${Math.round(j.progress*100)}%</span>`:''}
     <div class="meta">${esc(j.detail||'')}${j.source?' · '+esc(j.source):''}${j.error?' · <span class="state st-failed">'+esc(j.error)+'</span>':''}</div>
     <div class="meta">${fmtTs(j.created_at)}${j.finished_at?' → '+fmtTs(j.finished_at):''}</div>
    </div>`).join('')||'<div class="off">No jobs recorded.</div>';
}

/* ---------- Artifacts ---------- */
function renderArtifacts(list){
  $('#artifactsList').innerHTML=list.map(a=>
    `<div class="list-row"><b>${esc(a.name||a.id)}</b> <span class="pill">${esc(a.kind||'')}</span>
     <div class="meta">${esc(a.path||'')}</div>
     <div class="meta">${fmtTs(a.created_at)}${a.task_id?' · task '+esc(a.task_id):''}${a.mission_id?' · mission '+esc(a.mission_id):''}${a.version?' · v'+esc(a.version):''}</div>
    </div>`).join('')||'<div class="off">No artifacts recorded.</div>';
}

/* ---------- Backups ---------- */
function renderBackups(list){
  $('#backupsList').innerHTML=list.map(b=>
    `<div class="list-row"><b>${esc(b.name)}</b>${b.corrupt?' <span class="pill">CORRUPT</span>':''}
     <div class="meta">${esc(b.label||'')}${b.created_at?' · '+fmtTs(b.created_at):''}${b.files!=null?' · '+b.files+' files':''}</div>
     ${b.corrupt?'':`<div class="actions">
       <button class="mini-button" data-verify="${esc(b.name)}">Verify (dry-run)</button>
       <button class="mini-button danger" data-restore="${esc(b.name)}">Restore</button></div>`}
    </div>`).join('')||'<div class="off">No backups yet.</div>';
}

/* ---------- Eval + Experiments ---------- */
function renderEval(runs){
  $('#evalHistory').innerHTML=(runs||[]).slice().reverse().map(r=>
    `<div class="list-row"><b>${esc(r.suite||'')}</b> <span class="pill">${esc(r.subject||'')}</span>
     <div class="meta">${fmtTs(r.at)} · ${esc(r.passed??'?')}/${esc(r.total??'?')} passed${r.elapsed_s?' · '+r.elapsed_s+'s':''}</div>
    </div>`).join('')||'<div class="off">No eval runs recorded.</div>';
}
function renderExperiments(list){
  $('#experimentsList').innerHTML=list.map(e=>
    `<div class="list-row"><b>${esc(e.hypothesis||e.id)}</b> <span class="pill">${esc(e.status||'open')}</span>
     <div class="meta">${(e.arms||[]).map(a=>`<span class="pill">${esc(a.name||a)}</span>`).join('')}
     ${e.conclusion?' · '+esc(e.conclusion):''}</div>
     ${e.status==='open'?`<div class="actions"><button class="mini-button" data-conclude="${esc(e.id)}">Conclude…</button></div>`:''}
    </div>`).join('')||'<div class="off">No experiments.</div>';
}

/* ---------- Load ---------- */
async function refresh(){
  try{
    const [h,t,r,kn,l,sk,co,jb,a,b,ev,ex,dg,br,tr]=await Promise.all([
      api('/api/health'),api('/api/twin'),api('/api/rag'),api('/api/knowledge'),api('/api/lsp'),
      api('/api/skills'),api('/api/connectors'),api('/api/jobs'),
      api('/api/artifacts?kind='+encodeURIComponent($('#artifactKind').value)),
      api('/api/backups'),api('/api/eval/history'),api('/api/experiments'),
      api('/api/diagnostics'),api('/api/brain/status'),api('/api/brain/trace?limit=60')]);
    renderHealth(h);renderTwin(t);renderRagStats(r);renderKnowledge(kn);renderLsp(l);
    renderDiagnostics(dg);renderBrain(br,tr);
    renderSkills(sk.skills||[]);renderConnectors(co.connectors||[]);
    renderJobs(jb.jobs||[]);
    renderArtifacts(a.artifacts||[]);
    renderBackups(b.backups||[]);renderEval(ev.runs||[]);renderExperiments(ex.experiments||[]);
  }catch(e){
    $('#overallCard').innerHTML='<span class="off">API unavailable — '+esc(e.message)+'</span>';
  }
}

/* ---------- Events ---------- */
$('#refreshAll').onclick=refresh;
$('#artifactKind').onchange=refresh;
$('#ragSearch').onclick=async()=>{
  const q=$('#ragQuery').value.trim();if(!q)return;
  const r=await api('/api/rag?q='+encodeURIComponent(q));renderRagStats(r);
};
$('#kgSearch').onclick=async()=>{
  const q=$('#kgQuery').value.trim();if(!q)return;
  const k=await api('/api/knowledge?q='+encodeURIComponent(q));renderKnowledge(k);
};
$('#ragUpdate').onclick=async()=>{
  $('#ragResults').innerHTML='<div class="off">Updating…</div>';
  const r=await api('/api/rag/update','POST',{});
  $('#ragResults').innerHTML=`<div class="list-row">+${esc(r.added??0)} added · ${esc(r.updated??0)} updated · ${esc(r.removed??0)} removed</div>`;
  refresh();
};
$('#ragRebuild').onclick=async()=>{
  if(!confirm('Full rebuild of the repository index?'))return;
  const r=await api('/api/rag/update','POST',{force:true});refresh();
};
$('#backupCreate').onclick=async()=>{
  const r=await api('/api/backups/create','POST',{label:$('#backupLabel').value.trim()});
  if(r.ok){$('#backupLabel').value='';refresh();}else alert('Backup failed: '+(r.error||'unknown'));
};
$('#skillVerify').onclick=async()=>{
  const path=$('#skillPath').value.trim();if(!path)return;
  const r=await api('/api/skills/verify','POST',{path});renderSkillResult(r);
};
$('#skillInstall').onclick=async()=>{
  const path=$('#skillPath').value.trim();if(!path)return;
  const r=await skillApi('install',{path});if(r.ok)refresh();
};
$('#skillUpdate').onclick=async()=>{
  const path=$('#skillPath').value.trim();if(!path)return;
  const r=await skillApi('update',{path});if(r.ok)refresh();
};
document.addEventListener('click',async e=>{
  const t=e.target;if(!(t instanceof HTMLElement))return;
  if(t.dataset.skillToggle!==undefined){
    const en=t.dataset.en==='1';
    const r=await skillApi(en?'enable':'disable',{name:t.dataset.skillToggle});
    if(r.ok)refresh();
  }else if(t.dataset.skillVerify!==undefined){
    const r=await api('/api/skills/verify','POST',{name:t.dataset.skillVerify});
    renderSkillResult(r);
  }else if(t.dataset.skillRollback!==undefined){
    if(!confirm(`Roll back skill ${t.dataset.skillRollback}?`))return;
    const r=await skillApi('rollback',{name:t.dataset.skillRollback});if(r.ok)refresh();
  }else if(t.dataset.skillRemove!==undefined){
    if(!confirm(`Remove skill ${t.dataset.skillRemove}? This deletes its managed package and rollback snapshots.`))return;
    const r=await skillApi('remove',{name:t.dataset.skillRemove});if(r.ok)refresh();
  }else if(t.dataset.verify){
    const r=await api('/api/backups/restore','POST',{backup:t.dataset.verify,dry_run:true});
    alert(r.ok?`Verified — would restore ${r.would_restore} files.`:`Verification failed: ${r.error||'hash mismatch'}`);
  }else if(t.dataset.restore){
    if(!confirm(`Restore backup ${t.dataset.restore}? Current files are stashed under data/backups/pre-restore-* first.`))return;
    const r=await api('/api/backups/restore','POST',{backup:t.dataset.restore});
    alert(r.ok?`Restored ${r.restored} files.`:`Restore failed: ${r.error||'unknown'}`);
  }else if(t.dataset.conclude){
    const c=prompt('Conclusion for this experiment:');if(c==null)return;
    await api('/api/experiments/conclude','POST',{id:t.dataset.conclude,conclusion:c});refresh();
  }
});
/* ---------- Simulate ---------- */
$('#simRun').onclick=async()=>{
  let plan;
  try{plan=JSON.parse($('#simPlan').value||'{}');}
  catch(e){$('#simResults').innerHTML='<div class="off">Invalid JSON: '+esc(e.message)+'</div>';return;}
  const r=await api('/api/simulate','POST',{plan});
  $('#simResults').innerHTML=
    `<div class="list-row"><b>${esc(r.step_count??0)} steps</b>
     <span class="pill">~${esc(Math.round(r.estimated_duration_s||0))}s est</span>
     <span class="pill">${esc(r.approvals_expected??0)} approvals</span>
     ${(r.denied||[]).length?`<span class="pill">${esc(r.denied.length)} denied</span>`:''}
     ${r.resource_note?`<div class="meta">${esc(r.resource_note)}</div>`:''}</div>`+
    (r.steps||[]).map(s=>
      `<div class="list-row"><b>${esc(s.title)}</b> <span class="pill">${esc(s.tool||'—')}</span>
       <span class="pill">${esc(s.verdict)}</span> <span class="pill">${esc(s.risk)}</span>
       ${(s.files_affected||[]).length?`<div class="meta">${s.files_affected.map(f=>esc(f)).join(', ')}</div>`:''}
      </div>`).join('')+
    (r.failure_points||[]).map(f=>`<div class="list-row"><b>risk</b><div class="meta">${esc(f)}</div></div>`).join('')
    ||'';
};

$('#expCreate').onclick=async()=>{
  const h=$('#expHypothesis').value.trim();if(!h)return;
  await api('/api/experiments/create','POST',{hypothesis:h,arms:[{name:'control'},{name:'candidate'}]});
  $('#expHypothesis').value='';refresh();
};
refresh();
setInterval(refresh,15000);
