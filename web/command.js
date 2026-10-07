/* Command Center — unified ops: status, workers, missions, queues,
   update/recovery (LKG), dev servers, capability attention list. */
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=(p,body)=>fetch(p,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
const ago=ts=>{const s=Math.max(0,Date.now()/1000-Number(ts||0));if(s<60)return Math.round(s)+'s ago';if(s<3600)return Math.round(s/60)+'m ago';if(s<86400)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago';};

async function refresh(){
  const [status,workers,missions,queue,lkg,update,servers,caps,activity,safemode,prov,ledger]=await Promise.all([
    api('/api/status').catch(()=>({})),api('/api/workers').catch(()=>({})),
    api('/api/missions').catch(()=>({})),api('/api/queue').catch(()=>({})),
    api('/api/lkg').catch(()=>({})),api('/api/update/status').catch(()=>({})),
    api('/api/devservers').catch(()=>({})),api('/api/capability-states').catch(()=>({})),
    api('/api/activity?recent=30').catch(()=>({})),
    api('/api/safemode').catch(()=>({})),
    api('/api/provisioning').catch(()=>({})),
    api('/api/action-ledger?recent=25').catch(()=>({}))]);
  const sm=safemode||{};
  const voice=(window.NexusVoice?.status)||{};
  $('#ccHeader').innerHTML=
    `<div>v${esc(status.version||'?')} <span class="cc-ver">·</span> ${esc(status.workspace||'')}</div>`+
    `<div style="margin-top:6px">`+
    `${sm.active?'<span class="cc-badge cc-bad">SAFE MODE</span>':''}`+
    `${(workers.capacity||{}).active!=null?`<span class="cc-badge cc-ok">${workers.capacity.active} worker${workers.capacity.active===1?'':'s'} active</span>`:''}`+
    `${(queue.size||0)?`<span class="cc-badge cc-warn">${queue.size} chat${queue.size===1?'':'s'} queued</span>`:''}`+
    `${(missions.missions||[]).filter(m=>['running','active'].includes(String(m.status||''))).length?`<span class="cc-badge cc-warn">${(missions.missions||[]).filter(m=>['running','active'].includes(String(m.status||''))).length} mission${''} running</span>`:''}`+
    `${voice.muted?'<span class="cc-badge cc-warn">voice muted</span>':(voice.enabled?'<span class="cc-badge cc-ok">voice on</span>':'')}`+
    `</div>`;

  // workers
  const wl=workers.workers||[];
  $('#ccWorkers').innerHTML=wl.map(w=>
    `<div class="mission-card cc-worker"><div class="title">${esc(w.name||w.id)} <small>· ${esc(w.role||'worker')}</small></div>`+
    `<div class="meta">${esc(w.title||'')} — ${esc(w.status)}${w.elapsed_s?` · ${Math.round(w.elapsed_s)}s`:''}${w.phase?` · ${esc(w.phase)}`:''}</div></div>`).join('')
    ||'<div class="hist-row">no active workers</div>';
  const cap=workers.capacity||{},hw=workers.hardware||{};
  $('#ccWorkers').innerHTML+=`<div class="hist-row">capacity ${cap.active||0}/${cap.ceiling||'—'} · CPU ${hw.cpu_util!=null?Math.round(hw.cpu_util*100)+'%':'—'} · RAM free ${((hw.ram_free_mb||0)/1024).toFixed(1)} GB</div>`;
  $('#ccWorkerQueue').innerHTML=(workers.queue||[]).map(qi=>
    `<div class="hist-row">#${qi.position} ${esc(qi.title||'')} <small>— ${esc(qi.message||qi.reason||'')}</small></div>`).join('')
    ||'<div class="hist-row">empty</div>';

  // missions
  const ms=(missions.missions||[]).filter(m=>String(m.status||'')!=='archived').slice(0,12);
  $('#ccMissions').innerHTML=ms.map(m=>
    `<div class="mission-card"><div class="title">${esc(m.title||m.id)}</div>`+
    `<div class="meta">${esc(m.status||'')} · ${(m.graph?.nodes||[]).length} nodes${m.updated_at?` · ${ago(m.updated_at)}`:''}</div></div>`).join('')
    ||'<div class="hist-row">no missions</div>';
  $('#ccChatQueue').innerHTML=(queue.items||[]).map((i,n)=>
    `<div class="hist-row">${n+1}. ${esc(String(i.prompt||'').slice(0,80))}${i.mission_id?' <small>· mission</small>':''}</div>`).join('')
    ||'<div class="hist-row">empty</div>';

  // update & recovery
  const plan=update.plan||{};
  const pend=(lkg||{}).pending_update||(lkg||{}).pending_rollback;
  $('#ccUpdate').innerHTML=
    `<div class="hist-row">installed <b class="cc-ver">${esc(plan.installed_version||'?')}</b> · source <b class="cc-ver">${esc(plan.source_version||'?')}</b></div>`+
    `<div class="hist-row">${plan.ok?`checkout ${esc(plan.head||'')} · ${plan.commits_behind} commits behind upstream${plan.dirty?' · <b>dirty</b>':''}`:esc(plan.reason||'no source checkout')}</div>`+
    (pend?`<div class="hist-row"><span class="cc-badge cc-warn">${pend.staged_dir?'update staged':'rollback pending'}</span> applies on next start</div>`:'')+
    `<div class="hist-row" id="ccUpdateMsg"></div>`;
  $('#ccSnaps').innerHTML=(lkg.snapshots||[]).map(s=>
    `<div class="hist-row">${esc(s.name)} · v${esc(s.version||'?')} · ${ago(s.created_at)} · ${(s.bytes/1048576).toFixed(0)} MB`+
    ` <button class="mini-button danger" data-rb="${esc(s.name)}">rollback</button></div>`).join('')
    ||'<div class="hist-row">no snapshots yet</div>';

  // dev servers + capabilities
  $('#ccServers').innerHTML=(servers.servers||[]).map(sv=>
    `<div class="hist-row"><b>${esc(sv.name||sv.id)}</b> ${esc(sv.url||'')} <small>· ${esc(sv.status||'')}${sv.healthy===true?' · healthy':sv.healthy===false?' · down':''}</small></div>`).join('')
    ||'<div class="hist-row">none running</div>';
  const capList=Object.entries(caps.capabilities||caps.states||{}).filter(([k,v])=>!['verified','available','ready'].includes(String((v&&v.state)||v||'')));
  $('#ccCaps').innerHTML=capList.map(([k,v])=>{
    const st=(v&&typeof v==='object')?(v.state||v.status||'?'):String(v);
    const cls=['broken','unavailable'].includes(st)?'cc-bad':['degraded','setup_required','unauthorized'].includes(st)?'cc-warn':'cc-ok';
    return `<div class="hist-row"><span class="cc-badge ${cls}">${esc(st)}</span> ${esc(k)}</div>`;
  }).join('')||'<div class="hist-row">all clear</div>';

  // background provisioning — post-install workstation setup progress
  const pv=prov||{},items=pv.items||[];
  if(!pv.enabled){
    $('#ccSetup').innerHTML='<div class="hist-row">disabled</div>';
    $('#ccSetupItems').innerHTML='';
  }else{
    const gb=b=>(b/1073741824).toFixed(1);
    const rem=pv.remaining_download_bytes?` · ~${gb(pv.remaining_download_bytes)} GB left`:'';
    const head=pv.complete?`Setup complete · ${pv.completed}/${pv.total}`
      :`Finishing setup · ${pv.completed} of ${pv.total} components ready${rem}${pv.paused?' · <b>paused</b>':''}`;
    $('#ccSetup').innerHTML=`<div class="hist-row">${head}</div>`+
      (pv.failed?`<div class="hist-row"><span class="cc-badge cc-bad">${pv.failed} failed</span></div>`:'')+
      `<div class="hist-row"><small>free disk ${gb(pv.free_disk_bytes||0)} GB</small></div>`;
    $('#ccSetupItems').innerHTML=items.map(it=>{
      const p=it.progress||{};
      const frac=(p.bytes_total&&p.bytes_done)?p.bytes_done/p.bytes_total:null;
      const speed=p.bytes_per_sec?` · ${(p.bytes_per_sec/1048576).toFixed(1)} MB/s`:'';
      const eta=p.eta_seconds!=null?` · ~${Math.round(p.eta_seconds/60)}m`:'';
      const badge=it.state==='completed'?'cc-ok':it.state==='failed'?'cc-bad'
        :['running','verifying','queued'].includes(it.state)?'cc-warn':'cc-ver';
      const acts=(it.state==='failed'||it.state==='skipped'||it.state==='cancelled')
        ?` <button class="mini-button" data-pv-retry="${esc(it.id)}">Retry</button>`:'';
      const ap=(it.state==='waiting_approval')
        ?` <button class="mini-button" data-pv-approve="${esc(it.id)}">Approve</button>`:'';
      const lic=(it.state==='waiting_approval'&&it.payload&&it.payload.license)
        ?` · license ${esc(it.payload.license)}`:'';
      const cx=(it.state==='waiting'||it.state==='queued'||it.state==='running')
        ?` <button class="mini-button" data-pv-cancel="${esc(it.id)}">Cancel</button>`:'';
      const bar=(it.state==='running'&&frac!=null)
        ?`<div class="bar"><span style="width:${Math.round(frac*100)}%"></span></div>`:'';
      return `<div class="hist-row"><span class="cc-badge ${badge}">${esc(it.blocked_reason?'blocked':it.state)}</span> `+
        `${esc(it.label)}`+
        `<small>${it.detail?' · '+esc(it.detail):''}${lic}${speed}${eta}${it.error_code?' · '+esc(it.error_code):''}${it.blocked_reason?' · '+esc(it.blocked_reason):''}</small>${ap}${acts}${cx}${bar}</div>`;
    }).join('')||'<div class="hist-row">nothing to set up</div>';
  }

  // recent activity feed — one row per tracked step, newest first
  const acts=(activity.activities||[]);
  $('#ccActivity').innerHTML=acts.map(a=>
    `<div class="hist-row"><span class="cc-badge ${a.state==='failed'?'cc-bad':a.state==='running'?'cc-warn':'cc-ok'}">${esc(a.state||'')}</span> `+
    `${esc(a.title||a.category||'')}`+
    `<small> · ${esc(a.category||'')}${a.mission_id?' · mission':''} · ${ago(a.started_at)}</small></div>`).join('')
    ||'<div class="hist-row">no activity yet</div>';

  // action evidence — the durable record behind every action claim;
  // a reply is only truthful when a 'verified' row exists here
  const badge=st=>st==='verified'?'cc-ok':(st==='awaiting_approval'||st==='recorded')?'cc-warn':'cc-bad';
  $('#ccEvidence').innerHTML=(ledger.entries||[]).map(e=>
    `<div class="hist-row"><span class="cc-badge ${badge(e.status)}">${esc(e.status||'')}</span> `+
    `${esc(e.action||e.kind||'')}`+
    `<small> · ${esc(e.kind||'')}${e.tool?' · '+esc(e.tool):''}`+
    `${e.artifact?' → '+esc(e.artifact):''}`+
    `${e.failure?' · '+esc(e.failure):''}`+
    `${e.mission_id?' · mission':''} · ${ago(e.finished_at||e.created_at)}</small></div>`).join('')
    ||'<div class="hist-row">no recorded actions yet</div>';
}

$('#ccRefresh').addEventListener('click',refresh);
$('#ccSetupPause').addEventListener('click',async()=>{await api('/api/provisioning/pause',{});refresh();});
$('#ccSetupResume').addEventListener('click',async()=>{await api('/api/provisioning/resume',{});refresh();});
$('#ccSetupItems').addEventListener('click',async e=>{
  const r=e.target.closest('[data-pv-retry]'),c=e.target.closest('[data-pv-cancel]'),
    a=e.target.closest('[data-pv-approve]');
  if(r){await api('/api/provisioning/retry',{id:r.dataset.pvRetry});refresh();}
  if(c){await api('/api/provisioning/cancel',{id:c.dataset.pvCancel});refresh();}
  if(a){await api('/api/provisioning/approve',{id:a.dataset.pvApprove});refresh();}
});
$('#ccPlan').addEventListener('click',async()=>{
  const m=$('#ccUpdateMsg');if(m)m.textContent='Checking source…';
  const r=await api('/api/update/plan',{}).catch(()=>({}));
  if(m)m.textContent=r.ok?`Source: ${r.head} · ${r.commits_behind} commits behind · v${r.source_version}`:(r.reason||'no plan');
});
$('#ccApply').addEventListener('click',async()=>{
  const m=$('#ccUpdateMsg');
  if(!confirm('Stage an update? The source checkout is pulled, tested, built, snapshotted for rollback, and applied on next start.'))return;
  if(m)m.textContent='Staging update (pull → test → build → snapshot → stage)…';
  const r=await api('/api/update/apply',{confirm:true}).catch(()=>({}));
  if(m)m.textContent=r.ok?`Staged v${r.version} — applies on next start.`:`Failed: ${(r.stages||[]).filter(s=>!s.ok).map(s=>s.name+': '+s.detail).join('; ')||r.error||'unknown'}`;
  refresh();
});
$('#ccSnap').addEventListener('click',async()=>{
  const m=$('#ccUpdateMsg');
  const r=await api('/api/lkg/snapshot',{label:'manual'});
  if(m)m.textContent=r.ok?`Snapshot ${r.name} created.`:'Snapshot failed.';
  refresh();
});
$('#ccSnaps').addEventListener('click',async e=>{
  const b=e.target.closest('[data-rb]');if(!b)return;
  if(!confirm(`Roll back to ${b.dataset.rb}? Applies the next time Nexus Core starts.`))return;
  await api('/api/lkg/rollback',{name:b.dataset.rb,reason:'manual command-center rollback'});
  refresh();
});
$('#ccDeps').addEventListener('click',async()=>{
  const out=$('#ccAuditOut');
  out.innerHTML='<span class="muted">Listing dependencies…</span>';
  const d=await api('/api/audit/deps').catch(()=>({}));
  const mans=(d.manifests||[]);
  out.innerHTML=mans.length
    ?mans.map(m=>`<div class="hist-row"><code>${esc(m.manifest)}</code> — ${m.count} deps</div>`+
      (m.dependencies||[]).slice(0,40).map(x=>
        `<div class="hist-row" style="padding-left:14px">${esc(x.name)} <small>${esc(x.spec||'')}${x.dev?' · dev':''}</small></div>`).join('')
    ).join('')
    :`<div class="hist-row">${esc(d.error||'no manifests found')}</div>`;
});
$('#ccAudit').addEventListener('click',async()=>{
  const out=$('#ccAuditOut');
  out.innerHTML='<span class="muted">Running security audit (may take a minute)…</span>';
  const d=await api('/api/audit/run',{}).catch(()=>({}));
  const audits=d.audits||[];
  const summarize=(r)=>{
    if(r==null)return'';
    if(typeof r!=='object')return String(r).slice(0,200);
    const v=r.metadata&&r.metadata.vulnerabilities;        // npm audit
    if(v)return` vulns:${JSON.stringify(v)}`;
    const cv=r.vulnerabilities&&r.vulnerabilities.count;   // cargo audit
    if(cv!=null)return` vulns:${cv}`;
    if(Array.isArray(r))return` ${r.reduce((n,p)=>n+((p.vulns||[]).length),0)} vulns (pip-audit)`;
    return'';
  };
  out.innerHTML=audits.length?audits.map(a=>{
    const hdr=`<strong>${esc(a.ecosystem)}</strong>`;
    if(a.status==='auditor_unavailable')
      return`<div class="hist-row">${hdr}: auditor not installed${a.install?` — <code>${esc(a.install)}</code>`:''}</div>`;
    if(a.status==='timeout')
      return`<div class="hist-row">${hdr}: auditor timed out</div>`;
    return`<div class="hist-row">${hdr}: ran, exit ${a.exit_code}${esc(summarize(a.result))}</div>`;
  }).join(''):`<div class="hist-row">${esc(d.error||d.note||'no auditable manifests found')}</div>`;
});

refresh();
setInterval(refresh,15000);
