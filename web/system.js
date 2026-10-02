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
     <div class="meta">${esc(s.description||'')}</div>
     <div class="meta">${(s.capabilities||[]).map(c=>`<span class="pill">${esc(c)}</span>`).join('')}</div>
     <div class="actions"><button class="mini-button" data-skill="${esc(s.name)}" data-en="${s.enabled?0:1}">${s.enabled?'Disable':'Enable'}</button></div>
    </div>`).join('')||'<div class="off">No skills installed.</div>';
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
    const [h,t,r,kn,l,sk,co,a,b,ev,ex]=await Promise.all([
      api('/api/health'),api('/api/twin'),api('/api/rag'),api('/api/knowledge'),api('/api/lsp'),
      api('/api/skills'),api('/api/connectors'),
      api('/api/artifacts?kind='+encodeURIComponent($('#artifactKind').value)),
      api('/api/backups'),api('/api/eval/history'),api('/api/experiments')]);
    renderHealth(h);renderTwin(t);renderRagStats(r);renderKnowledge(kn);renderLsp(l);
    renderSkills(sk.skills||[]);renderConnectors(co.connectors||[]);
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
document.addEventListener('click',async e=>{
  const t=e.target;if(!(t instanceof HTMLElement))return;
  if(t.dataset.skill!==undefined){
    const en=t.dataset.en==='1';
    await api('/api/skills/'+(en?'enable':'disable'),'POST',{name:t.dataset.skill});refresh();
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
$('#expCreate').onclick=async()=>{
  const h=$('#expHypothesis').value.trim();if(!h)return;
  await api('/api/experiments/create','POST',{hypothesis:h,arms:[{name:'control'},{name:'candidate'}]});
  $('#expHypothesis').value='';refresh();
};
refresh();
setInterval(refresh,15000);
