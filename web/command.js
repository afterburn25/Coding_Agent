/* Command Center — unified ops: status, workers, missions, queues,
   update/recovery (LKG), dev servers, capability attention list. */
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=(p,body)=>fetch(p,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
const ago=ts=>{const s=Math.max(0,Date.now()/1000-Number(ts||0));if(s<60)return Math.round(s)+'s ago';if(s<3600)return Math.round(s/60)+'m ago';if(s<86400)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago';};

async function refresh(){
  const [status,workers,missions,queue,lkg,update,servers,caps,safemode]=await Promise.all([
    api('/api/status').catch(()=>({})),api('/api/workers').catch(()=>({})),
    api('/api/missions').catch(()=>({})),api('/api/queue').catch(()=>({})),
    api('/api/lkg').catch(()=>({})),api('/api/update/status').catch(()=>({})),
    api('/api/devservers').catch(()=>({})),api('/api/capability-states').catch(()=>({})),
    api('/api/safemode').catch(()=>({}))]);
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
}

$('#ccRefresh').addEventListener('click',refresh);
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

refresh();
setInterval(refresh,15000);
