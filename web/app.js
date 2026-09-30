const $=s=>document.querySelector(s);
const chat=$('#chat'),form=$('#composer'),input=$('#input'),send=$('#send'),mode=$('#mode'),activity=$('#activity');
let lastTask=null;
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const welcomeHtml=()=>`<div class="welcome"><img src="/assets/chat-nexus-emblem.png" alt="Chat Nexus" class="welcome-logo" /><h1>Chat Nexus</h1><p>Your local AI coding partner.</p><span>Write. Refactor. Debug. Build. All on your machine.</span><div class="quick-actions"><button type="button" data-prompt="Explain the current code and architecture."><b>&lt;/&gt;</b>Explain this code</button><button type="button" data-prompt="Refactor the current code to be cleaner and easier to maintain."><b>✦</b>Refactor to be cleaner</button><button type="button" data-prompt="Add useful tests for the current code and run them."><b>▤</b>Add tests for this file</button><button type="button" data-prompt="Help me build a new feature in this project. Inspect the repository first and make a plan."><b>↗</b>Help me build a feature</button></div></div>`;
function setUtilityPanel(name){document.querySelectorAll('.utility-tab').forEach(x=>x.classList.toggle('active',x.dataset.panel===name));document.querySelectorAll('[data-utility-panel]').forEach(x=>x.classList.toggle('active',x.dataset.utilityPanel===name));}
function feedbackControls(messageId=''){return '<div class="message-feedback"><button type="button" data-feedback="up" data-message-id="'+esc(messageId)+'" title="Helpful">👍</button><button type="button" data-feedback="down" data-message-id="'+esc(messageId)+'" title="Needs improvement">👎</button></div>';}
function addMessage(role,text,messageId=''){const welcome=chat.querySelector('.welcome');if(welcome)welcome.remove();const el=document.createElement('div');el.className=`message ${role}`;if(messageId)el.dataset.messageId=messageId;el.innerHTML=`<div class="role">${esc(role)}</div><div class="bubble">${esc(text)}</div>${role==='assistant'?feedbackControls(messageId):''}`;chat.appendChild(el);chat.scrollTop=chat.scrollHeight;}
function renderConversationHistory(history=[]){chat.innerHTML='';if(!history.length){chat.innerHTML=welcomeHtml();return;}for(const m of history){if(['user','assistant'].includes(m.role))addMessage(m.role,m.content||'',m.id||'');}}
function renderConversationList(rows=[]){const box=$('#conversationList');if(!box)return;box.innerHTML=rows.length?rows.map(row=>`<button class="conversation-row ${row.active?'active':''}" data-conversation="${esc(row.id)}" type="button"><strong>${esc(row.title||'New chat')}</strong><small>${esc(row.message_count||0)} messages${row.summary?' · '+esc(String(row.summary).slice(0,60)):''}</small></button>`).join(''):'<span class="muted">No conversations yet.</span>';}
async function loadConversations(query=''){try{const url='/api/conversations'+(query?'?q='+encodeURIComponent(query):'');const res=await fetch(url);const data=await res.json();if(!res.ok)throw new Error(data.error||'Conversation lookup failed');if(query){renderConversationList((data.results||[]).map(x=>({...x,message_count:'',active:false})));return data;}renderConversationList(data.conversations||[]);return data;}catch(e){const box=$('#conversationList');if(box)box.textContent='Conversation history unavailable';return null;}}
async function selectConversation(id){const res=await fetch('/api/conversations/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:id})});const data=await res.json();if(!res.ok)throw new Error(data.error||'Could not open conversation');renderConversationHistory(data.history||[]);await loadConversations();input.focus();}
async function newConversation(){const res=await fetch('/api/chat/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const data=await res.json();if(!res.ok)throw new Error(data.error||'Could not start new conversation');chat.innerHTML=welcomeHtml();activity.innerHTML='<span class="muted">Tool calls, model switches, and command output will appear here.</span>';renderTask(null);await loadConversations();input.focus();}
function addRoute(route,events=[]){if(!route)return;const el=document.createElement('div');el.className='route';const switches=(events||[]).filter(x=>x.type==='switch').map(x=>`${x.from} → ${x.to}`).join(' · ');el.textContent=`${route.role} · ${route.model_id} · complexity ${route.complexity}${switches?' · '+switches:''}`;chat.appendChild(el);}
function renderHardware(h){if(!h)return 'Unknown';const gpu=(h.gpus||[]).map(g=>`${g.name}: ${g.free_vram_gb.toFixed(1)}/${g.total_vram_gb.toFixed(1)} GB VRAM free`).join('<br>');return `${esc(h.available_ram_gb?.toFixed?.(1)??'?')}/${esc(h.total_ram_gb?.toFixed?.(1)??'?')} GB RAM free${gpu?'<br>'+esc(gpu).replace(/&lt;br&gt;/g,'<br>'):''}`;}
function phaseIndex(phase){return ({planning:0,working:1,researching_failure:1,waiting_approval:1,interrupted:1,verifying:2,reviewing:3,done:4})[phase]??0;}
function renderDiff(task){const panel=$('#diffPanel');if(!panel)return;if(!task?.files_changed?.length){panel.className='empty-utility';panel.innerHTML='<div class="empty-icon">▤</div><strong>No changes yet</strong><p>When Chat Nexus changes code, affected files will appear here for review.</p>';return;}panel.className='task-card';panel.innerHTML=`<div class="task-head"><strong>Changed files</strong><span class="task-status">${esc(task.files_changed.length)} files</span></div><div class="files">${task.files_changed.map(f=>`<div>• ${esc(f)}</div>`).join('')}</div>${task.review?`<div class="review-box"><strong>Reviewer notes</strong>\n${esc(task.review)}</div>`:''}`;}
function renderTask(task){lastTask=task||null;renderDiff(task);if(!task){$('#taskPanel').innerHTML='<span class="muted">No task yet.</span>';return;}const p=phaseIndex(task.phase);const progress=Array.from({length:5},(_,i)=>`<span class="${i<=p?'active':''}"></span>`).join('');const pending=task.pending_approval;const approval=pending?`<div class="approval"><strong>Approval required</strong><div>${esc(pending.name)} · ${esc(pending.permission)}</div>${pending.detail?`<code>${esc(pending.detail)}</code>`:''}<div class="button-row"><button class="approve" data-approve="1">Approve</button><button class="deny" data-approve="0">Deny</button></div></div>`:'';const verify=(task.verification||[]).length?`<div class="verification-box"><strong>Verification</strong>\n${esc(task.verification.map(v=>`${v.name}: ${String(v.result).includes('EXIT_CODE=0')?'passed':'attention needed'}`).join('\n'))}</div>`:'';const review=task.review?`<div class="review-box"><strong>Reviewer</strong>\n${esc(task.review)}</div>`:'';const rp=task.research?.plan;const research=rp?`<div class="verification-box"><strong>Research</strong>\n${esc(rp.mode)} · ${rp.needed?'evidence needed':'local evidence sufficient'}${rp.reasons?.length?'\n'+esc(rp.reasons.join('\n')):''}</div>`:'';const recover=task.status==='interrupted'?`<button class="resume-button" data-recover="${esc(task.id)}">Resume interrupted task</button>`:'';const undo=['completed','completed_with_warnings','step_limit'].includes(task.status)&&task.files_changed?.length&&!task.reverted?`<button class="undo-button" data-undo="${esc(task.id)}">Undo this task</button>`:'';$('#taskPanel').innerHTML=`<div class="task-card"><div class="task-head"><span class="task-id">${esc(task.id)}</span><span class="task-status ${esc(task.status)}">${esc(task.status)}</span></div><div class="task-prompt">${esc(task.prompt)}</div><div class="progress">${progress}</div><div class="meta"><span>Phase</span><strong>${esc(task.phase)}</strong><span>Model</span><strong>${esc(task.model_role||'—')}</strong><span>Steps</span><strong>${esc(task.steps)}</strong><span>Files</span><strong>${esc(task.files_changed?.length||0)}</strong></div>${task.files_changed?.length?`<div class="files">${esc(task.files_changed.join(', '))}</div>`:''}${approval}${research}${verify}${review}${recover}${undo}</div>`;if(task.pending_approval||task.status==='interrupted')setUtilityPanel('tasks');}
function renderRecent(tasks){$('#recentTasks').innerHTML=tasks?.length?tasks.map(t=>`<div class="recent-task"><div class="row"><span>${esc((t.prompt||'').slice(0,45))}</span><span class="task-status ${esc(t.status)}">${esc(t.status)}</span></div><small>${esc(t.model_role||'')} · ${esc(t.files_changed?.length||0)} files</small></div>`).join(''):'<span class="muted">No task history.</span>';}
function renderStatus(s){$('#status').textContent=`v${s.version} · GPU ready`;$('#workspace').textContent=s.workspace;if($('#policyMode'))$('#policyMode').value=s.policy_mode||'permissive';if($('#ethicalTemperature')){$('#ethicalTemperature').value=Number(s.ethical_temperature??1).toFixed(2);$('#ethicalTemperatureValue').textContent=Number(s.ethical_temperature??1).toFixed(2);}$('#hardware').innerHTML=renderHardware(s.runtime?.hardware);const statuses=Object.fromEntries((s.runtime?.statuses||[]).map(x=>[x.model_id,x]));$('#models').innerHTML=(s.models||[]).map(m=>{const r=statuses[m.id]||{};const cls=r.healthy?'healthy':r.state==='loading'?'loading':String(r.state||'').includes('error')?'error':'';const action=m.runtime==='llama_cpp'?`<button class="mini-button runtime-action" data-action="${r.healthy?'stop':'start'}" data-model="${esc(m.id)}">${r.healthy?'Stop':'Start'}</button>`:'';return `<div class="model"><div class="model-head"><strong>${esc(m.id)}</strong><span class="state ${cls}">${esc(r.state||m.runtime)}</span></div><small>${esc(m.roles.join(', '))}</small><small>${esc(m.runtime)}${r.pid?' · PID '+esc(r.pid):''}</small>${action}</div>`;}).join('');const inventory=s.runtime?.inventory||[];$('#inventory').innerHTML=inventory.length?inventory.map(x=>`<div>${esc(x.name)} <small>${esc(x.size_gb)} GB</small></div>`).join(''):'<span class="muted">No .gguf files found</span>';const idx=s.repository_index||{};$('#repoIndex').textContent=`${idx.file_count||0} files indexed${idx.generated_at?' · '+new Date(idx.generated_at*1000).toLocaleTimeString():''}`;renderTask(s.tasks?.current);renderRecent(s.tasks?.recent||[]);}
function formatBytes(value){const n=Number(value||0);if(n>=1024**3)return `${(n/1024**3).toFixed(1)} GB`;if(n>=1024**2)return `${(n/1024**2).toFixed(0)} MB`;if(n>=1024)return `${(n/1024).toFixed(0)} KB`;return `${n} B`;}
let lastReadiness=null;
let activeModelPlan=null;
function renderReadiness(r){
  lastReadiness=r;
  const panel=$('#readinessPanel');if(!panel)return;
  const ready=!!r.ready_to_code,selfHost=!!r.self_hosting_ready;
  const state=selfHost?'Self-host ready':ready?'Ready to code':'Setup required';
  const cls=selfHost?'selfhost':ready?'ready':'setup';
  const rows=(r.models||[]).map(m=>{const ok=m.healthy||m.runnable;const detail=m.issues?.length?m.issues.join(' · '):(m.healthy?'running':'configured');return `<div class="readiness-model"><span class="readiness-dot ${ok?'ok':'bad'}"></span><div><strong>${esc(m.id)}</strong><small>${esc(m.runtime)} · ${esc(detail)}</small></div></div>`;}).join('');
  const rec=(r.recommendations||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const suggestions=(r.suggested_models||[]);
  const setup=suggestions.length&&!ready?`<div class="setup-suggestion"><strong>Local models found</strong><small>${suggestions.map(x=>`${esc(x.id)} → ${esc((x.roles||[]).join(', '))}`).join('<br>')}</small><button id="applyModelSetup" class="mini-button" type="button">Use discovered models</button></div>`:'';
  const runtime=r.runtime_install||{};const runtimeSetup=!runtime.installed?`<div class="runtime-setup"><strong>llama.cpp runtime needed</strong><small>${esc(runtime.platform||'')} · Chat Nexus will not execute installers automatically.</small>${(runtime.commands||[]).map(c=>`<button class="mini-button copy-runtime-command" data-command="${esc(c.command)}" type="button">${esc(c.label)} · Copy install command</button>`).join('')}</div>`:`<div class="runtime-found">✓ llama.cpp · ${esc(runtime.executable||'available')}</div>`;
  const activeJobs=(r.install_jobs||[]).filter(j=>!['finished','failed','cancelled'].includes(j.state));
  const jobsByModel=Object.fromEntries(activeJobs.map(j=>[j.catalog_id,j]));
  const catalogById=Object.fromEntries((r.catalog||[]).map(m=>[m.id,m]));
  const starter=catalogById['qwen3-14b-q4-k-m'];
  const deep=catalogById['qwen3-coder-30b-a3b-q4-k-m'];
  const missingStarter=starter&&!starter.verified;
  const missingDeep=deep&&!deep.verified;
  const setupBusy=activeJobs.length>0||!!activeModelPlan;
  const planDisabled=setupBusy?' disabled aria-busy="true"':'';
  const storage=r.model_storage||{};
  const installPath=storage.path||r.models_dir||'';
  const freeBytes=Number(storage.free_bytes||0);
  const storageLine=installPath?'<small class="setup-storage">Install location: <code>'+esc(installPath)+'</code>'+(freeBytes?' · '+formatBytes(freeBytes)+' free':'')+'</small>':'';
  const currentJob=activeJobs[0]||null;
  const currentAsset=currentJob?catalogById[currentJob.catalog_id]:null;
  let installProgress='';
  if(currentJob){
    const pct=Math.round(Number(currentJob.progress||0)*100);
    installProgress='<div class="first-run-progress"><div class="first-run-progress-head"><strong>'+esc(currentAsset?.title||currentJob.catalog_id||'Coding model')+'</strong><span>'+pct+'%</span></div><div class="catalog-progress"><span style="width:'+pct+'%"></span></div><small>'+esc(currentJob.state)+' · '+formatBytes(currentJob.bytes_done||0)+' / '+formatBytes(currentJob.bytes_total||0)+'</small></div>';
  }else if(activeModelPlan){
    const total=Number(activeModelPlan.totalBytes||0);
    const done=(activeModelPlan.ids||[]).reduce((sum,id)=>sum+(catalogById[id]?.verified?Number(catalogById[id]?.size_bytes||0):0),0);
    const pct=total?Math.min(100,Math.round((done/total)*100)):0;
    installProgress='<div class="first-run-progress"><div class="first-run-progress-head"><strong>'+esc(activeModelPlan.label||'Coding model setup')+'</strong><span>'+pct+'%</span></div><div class="catalog-progress"><span style="width:'+pct+'%"></span></div><small>Preparing next verified model download…</small></div>';
  }
  let quickSetup='';
  if(missingStarter){
    quickSetup='<div class="first-run-setup"><strong>Finish coding setup</strong><small>Install verified local model weights. Chat Nexus already includes the llama.cpp runtime.</small>'+storageLine+'<div class="first-run-actions"><button class="setup-primary" data-model-plan="starter" type="button"'+planDisabled+'>Install recommended 14B <span>~'+esc(starter.size_gb)+' GB</span></button>'+(missingDeep?'<button class="setup-secondary" data-model-plan="full" type="button"'+planDisabled+'>Install full 14B + 30B stack <span>~'+esc(((starter?.size_gb||0)+(deep?.size_gb||0)).toFixed(1))+' GB</span></button>':'')+'</div>'+installProgress+'<small class="setup-note">14B handles everyday coding. 30B is reserved for deep reasoning and review. Downloads are checksum-verified before use.</small></div>';
  }else if(missingDeep){
    quickSetup='<div class="first-run-setup optional-deep"><strong>'+(ready?'Everyday coding is ready':'Complete the coding stack')+'</strong><small>The 14B coder is installed. Add the 30B coder for difficult debugging, architecture work, and review.</small>'+storageLine+'<div class="first-run-actions"><button class="setup-secondary" data-model-plan="deep" type="button"'+planDisabled+'>Add 30B deep coder <span>~'+esc(deep.size_gb)+' GB</span></button></div>'+installProgress+'</div>';
  }
  const modelStatus=quickSetup?`<details class="setup-details"><summary>Technical model status</summary>${rows||'<div class="muted">No model profiles configured.</div>'}</details>`:(rows||'<div class="muted">No model profiles configured.</div>');
  const catalog=(r.catalog||[]).map(m=>{const job=jobsByModel[m.id];const pct=job?Math.round(Number(job.progress||0)*100):0;const action=m.verified?'<span class="catalog-installed">✓ Installed</span>':m.installed?`<button class="mini-button catalog-repair" data-catalog="${esc(m.id)}">Repair / verify</button>`:`<button class="mini-button catalog-install" data-catalog="${esc(m.id)}">Install</button>`;const progress=job?`<div class="catalog-progress"><span style="width:${pct}%"></span></div><small>${esc(job.state)} · ${pct}% · ${formatBytes(job.bytes_done||0)} / ${formatBytes(job.bytes_total||0)}</small><button class="mini-button catalog-cancel" data-job="${esc(job.id)}">Cancel</button>`:'';return `<div class="catalog-card"><div class="catalog-head"><strong>${esc(m.title)}</strong><span>${m.verified?'verified':esc(m.source_type)}</span></div><small>${esc(m.size_gb)} GB · ${esc(m.license)} · ${esc((m.roles||[]).join(', '))}</small><p>${esc(m.description)}</p><p class="hardware-note">${esc(m.hardware_note)}</p>${progress||`<div class="catalog-action">${action}</div>`}</div>`;}).join('');
  const catalogPanel=(r.catalog||[]).length?`<details class="catalog-panel"><summary>Advanced model downloads <span>${r.catalog.length}</span></summary>${catalog}</details>`:'';
  panel.innerHTML=`<div class="readiness-head ${cls}"><span>${esc(state)}</span><small>${esc((r.covered_roles||[]).join(', ')||'no active coding roles')}</small></div>${quickSetup}${modelStatus}${runtimeSetup}${setup}${catalogPanel}${rec?`<ul class="readiness-recs">${rec}</ul>`:''}${r.self_hosting_tree?`<div class="selfhost-line">${selfHost?'✓':'○'} Chat Nexus self-hosting workspace</div><button id="startSelfDevelopment" class="mini-button selfhost-start" type="button" ${ready?'':'disabled'}>${ready?'Start self-development task':'Install/configure a coding model first'}</button>`:''}`;
  const v=String($('#status').textContent||'').split(' · ')[0]||'Chat Nexus';
  $('#status').textContent=`${v} · ${selfHost?'self-host ready':ready?'ready':'setup required'}`;
}
async function loadReadiness(){try{const [readyRes,catalogRes]=await Promise.all([fetch('/api/readiness'),fetch('/api/models/catalog')]);const data=await readyRes.json();if(!readyRes.ok)throw new Error(data.error||'Readiness check failed');if(catalogRes.ok){const catalog=await catalogRes.json();data.catalog=catalog.models||[];data.install_jobs=catalog.jobs||[];}renderReadiness(data);return data;}catch(e){const p=$('#readinessPanel');if(p)p.innerHTML=`<span class="state error">${esc(e.message)}</span>`;return null;}}
async function copyText(value){
  try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(value);}else{const t=document.createElement('textarea');t.value=value;t.style.position='fixed';t.style.opacity='0';document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();}addMessage('assistant',`Copied install command: ${value}`);}catch(e){addMessage('assistant',`Could not copy command: ${e.message}`);}
}
async function startCatalogInstall(catalogId,repair=false){
  const source=document.querySelector(`[data-catalog="${CSS.escape(catalogId)}"]`);const card=source?.closest('.catalog-card');const title=card?.querySelector('strong')?.textContent||catalogId;
  if(!confirm(`${repair?'Repair/verify':'Download and install'} ${title}? Large model downloads can use significant disk space and bandwidth.`))return;
  try{const job=await beginCatalogInstall(catalogId,repair);await loadReadiness();if(job?.id&&!String(job.id).startsWith('reuse-'))pollCatalogInstall(job.id);}catch(e){addMessage('assistant',`Model install error: ${e.message}`);}
}
async function beginCatalogInstall(catalogId,repair=false){
  const res=await fetch('/api/models/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catalog_id:catalogId,repair})});
  const data=await res.json();if(!res.ok)throw new Error(data.error||'Model install failed');return data.job;
}
async function getCatalogJob(jobId){
  const res=await fetch(`/api/models/install/${encodeURIComponent(jobId)}`);const data=await res.json();if(!res.ok)throw new Error(data.error||'Install status failed');return data.job;
}
async function waitForCatalogJob(jobId){
  while(true){const job=await getCatalogJob(jobId);await loadReadiness();if(['finished','failed','cancelled'].includes(job.state))return job;await new Promise(r=>setTimeout(r,1000));}
}
async function configureDownloadedModels({announce=true}={}){
  const res=await fetch('/api/readiness/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({apply:true})});const data=await res.json();if(!res.ok)throw new Error(data.error||'Could not save model setup');if(announce)addMessage('assistant',data.message||'Coding models configured and activated.');await loadStatus(false);await loadReadiness();return data;
}
async function installModelPlan(kind){
  const ids=kind==='full'?['qwen3-14b-q4-k-m','qwen3-coder-30b-a3b-q4-k-m']:kind==='deep'?['qwen3-coder-30b-a3b-q4-k-m']:['qwen3-14b-q4-k-m'];
  const catalog=Object.fromEntries((lastReadiness?.catalog||[]).map(m=>[m.id,m]));
  const pending=ids.filter(id=>!catalog[id]?.verified);
  const label=kind==='full'?'the full 14B + 30B coding stack':kind==='deep'?'the 30B deep-reasoning coder':'the recommended 14B coding model';
  const bytes=pending.reduce((sum,id)=>sum+Number(catalog[id]?.size_bytes||0),0);
  if(!pending.length){try{await configureDownloadedModels();}catch(e){addMessage('assistant',`Model setup error: ${e.message}`);}return;}
  const freeBytes=Number(lastReadiness?.model_storage?.free_bytes||0);
  const reserve=1024**3;
  if(freeBytes&&freeBytes<bytes+reserve){
    const path=lastReadiness?.model_storage?.path||lastReadiness?.models_dir||'the model directory';
    addMessage('assistant','Not enough free disk space for '+label+'. Need about '+formatBytes(bytes+reserve)+' including working space, but only '+formatBytes(freeBytes)+' is free at '+path+'.');
    return;
  }
  if(!confirm(`Install ${label}? This will download approximately ${formatBytes(bytes)} and verify each model before configuration.`))return;
  activeModelPlan={kind,ids:[...pending],label,totalBytes:bytes};
  document.querySelectorAll('[data-model-plan]').forEach(b=>b.disabled=true);
  try{
    for(const id of pending){
      const job=await beginCatalogInstall(id,false);
      await loadReadiness();
      if(job?.id&&!String(job.id).startsWith('reuse-')){
        const finished=await waitForCatalogJob(job.id);
        if(finished.state!=='finished')throw new Error(finished.error||finished.message||`Install did not finish: ${id}`);
      }
    }
    const configured=await configureDownloadedModels({announce:false});
    const installedLabel=kind==='full'?'14B and 30B models':kind==='deep'?'30B deep coder':'14B model';
    const startError=configured?.applied?.start_error||'';
    if(startError)addMessage('assistant',`${installedLabel} installed, checksum verified, and routing configured. The starter model did not finish loading yet: ${startError}`);
    else addMessage('assistant',`${installedLabel} installed, checksum verified, routing configured, and activated. Chat Nexus is ready to use without restarting.`);
  }catch(e){addMessage('assistant',`First-run model setup error: ${e.message}`);}finally{activeModelPlan=null;await loadReadiness();document.querySelectorAll('[data-model-plan]').forEach(b=>b.disabled=false);}
}
async function pollCatalogInstall(jobId){
  try{const job=await getCatalogJob(jobId);await loadReadiness();if(!['finished','failed','cancelled'].includes(job.state)){setTimeout(()=>pollCatalogInstall(jobId),1000);}else if(job.state==='finished'){addMessage('assistant','Coding model installed and checksum verified. Use “Use discovered models” to assign it to agent roles.');}else if(job.state==='failed'){addMessage('assistant',`Model install failed: ${job.error||job.message}`);}}catch(e){addMessage('assistant',`Model install status error: ${e.message}`);}
}
async function cancelCatalogInstall(jobId){try{const res=await fetch('/api/models/install/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId})});const data=await res.json();if(!res.ok)throw new Error(data.error||'Cancel failed');await loadReadiness();}catch(e){addMessage('assistant',`Cancel error: ${e.message}`);}}
function prepareSelfDevelopmentTask(){
  input.value='Continue developing Chat Nexus itself. Read README.md, PROJECT_STATUS.md, ARCHITECTURE.md, and SESSION_HANDOFF.md first. Inspect the current implementation before changing anything. Make the requested improvement without breaking working features, run the required verification including the isolated self-update test, and report the exact files/tests changed. Do not commit or push unless I explicitly ask.';
  input.focus();input.setSelectionRange(input.value.length,input.value.length);
}
async function applySuggestedModelSetup(){
  if(!confirm('Write the discovered GGUF role assignments to your Chat Nexus config? Existing model profiles will be replaced; other settings are preserved.'))return;
  const button=$('#applyModelSetup');if(button)button.disabled=true;
  try{await configureDownloadedModels();}catch(e){addMessage('assistant',`Model setup error: ${e.message}`);}finally{const b=$('#applyModelSetup');if(b)b.disabled=false;}
}
function renderConversationMemory(m){
  const facts=(m.facts||[]).filter(x=>x.active!==false);
  const rules=(m.behavior_rules||[]).filter(x=>x.active!==false);
  const examples=m.training_examples||[];
  const recentRules=rules.slice(-3).map(x=>'• '+esc(x.text||'')).join('<br>');
  $('#memoryStatus').innerHTML=`<div><strong>${facts.length}</strong> remembered facts/preferences · <strong>${rules.length}</strong> operating rules · <strong>${examples.length}</strong> training examples</div>${recentRules?`<small>${recentRules}</small>`:''}<small>Stored locally · corrections become reviewable examples before any offline weight training.</small>`;
}
async function loadConversationMemory(){try{const res=await fetch('/api/conversation-memory');const data=await res.json();if(!res.ok)throw new Error(data.error||'Memory lookup failed');renderConversationMemory(data);}catch(e){if($('#memoryStatus'))$('#memoryStatus').textContent='Memory unavailable · '+e.message;}}
async function recordBuiltinExchange(userText,assistantText){try{await fetch('/api/conversation-memory/exchange',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:userText,assistant:assistantText})});}catch{}}
function policyModeNote(mode,temp=Number($('#ethicalTemperature')?.value??1)){
  const t=Number(temp).toFixed(2);
  if(mode==='strict')return `Strict · ethical temperature ${t}. Hard tool/action safety remains enforced.`;
  if(mode==='balanced')return `Balanced · ethical temperature ${t}. Hard tool/action safety remains enforced.`;
  return `Permissive · ethical temperature ${t}. 1.00 = maximum conversational permissiveness within hard tool/action safety.`;
}
async function setPolicyMode(mode,ethicalTemperature=null){
  const payload={mode};
  if(ethicalTemperature!==null)payload.ethical_temperature=Number(ethicalTemperature);
  const res=await fetch('/api/policy/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data=await res.json();if(!res.ok)throw new Error(data.error||'Policy update failed');
  if($('#policyMode'))$('#policyMode').value=data.mode;
  if($('#ethicalTemperature'))$('#ethicalTemperature').value=Number(data.ethical_temperature??1).toFixed(2);
  if($('#ethicalTemperatureValue'))$('#ethicalTemperatureValue').textContent=Number(data.ethical_temperature??1).toFixed(2);
  if($('#policyNote'))$('#policyNote').textContent=policyModeNote(data.mode,data.ethical_temperature);
  return data;
}
async function loadStatus(probe=false){try{const url=probe?'/api/runtime':'/api/status';const data=await fetch(url).then(r=>r.json());if(probe){const s=await fetch('/api/status').then(r=>r.json());s.runtime=data;renderStatus(s);}else renderStatus(data);}catch(e){$('#status').textContent='Backend unavailable';}}
async function runtimeAction(action,modelId){const res=await fetch(`/api/runtime/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_id:modelId})});const data=await res.json();if(!res.ok)throw new Error(data.error||'Runtime action failed');await loadStatus(false);}
const imageJobEls=new Map();
function imageJobActions(job){const first=(job.outputs||[])[0]||'';if(!first)return `<a class="image-action" href="/image.html">Open Image Workspace</a>`;return `<button class="image-action" data-image-action="edit" data-path="${esc(first)}">Edit</button><button class="image-action" data-image-action="variation" data-path="${esc(first)}">Variation</button><button class="image-action" data-image-action="upscale" data-path="${esc(first)}">Upscale</button><a class="image-action" href="${esc((job.output_urls||[])[0]||'#')}" download>Save</a><a class="image-action" href="/image.html">Image Workspace</a>`;}
function paintImageJob(el,job){const pct=Math.max(0,Math.min(100,Math.round(Number(job.progress||0)*100)));const imgs=(job.output_urls||[]).map(u=>`<img src="${esc(u)}" alt="Generated image">`).join('');const message=job.error_message||job.error||'';const err=message?`<div class="image-job-error">${esc(message)}</div>`:'';const tech=job.technical_details?`<details class="technical-details"><summary>Technical details</summary><pre>${esc(job.technical_details)}</pre></details>`:'';const active=!['finished','failed','cancelled'].includes(String(job.state||''));const placeholder=active&&!imgs?`<div class="image-generation-placeholder"><div class="image-generation-shimmer"></div><div class="image-generation-copy"><strong>Generating image…</strong><span>${esc(job.stage||'queued')}</span></div></div>`:'';const progressClass=active&&pct<5?' image-job-progress-indeterminate':'';el.innerHTML=`<div class="image-job-head"><strong>${esc(job.operation||'image')}</strong><span>${esc(job.model_id||'')} · ${esc(job.state||'queued')}</span></div>${placeholder}<div class="image-job-progress${progressClass}"><span style="width:${Math.max(pct,active?6:0)}%"></span></div><small>${esc(job.stage||'queued')} · ${pct}%</small>${err}${tech}${imgs?`<div class="inline-image-gallery">${imgs}</div>`:''}<div class="inline-image-actions">${imageJobActions(job)}</div>`;}
async function pollImageJob(id){const el=imageJobEls.get(id);if(!el)return;try{const res=await fetch(`/api/image/job/${encodeURIComponent(id)}`);const data=await res.json();if(!res.ok)throw new Error(data.error||'Image job lookup failed');paintImageJob(el,data.job);if(!['finished','failed','cancelled'].includes(data.job.state))setTimeout(()=>pollImageJob(id),1000);}catch(e){el.querySelector('.image-job-error')?.remove();const d=document.createElement('div');d.className='image-job-error';d.textContent=e.message;el.appendChild(d);}}
function renderImageJobs(jobs=[]){for(const job of jobs){let el=imageJobEls.get(job.id);if(!el){const wrap=document.createElement('div');wrap.className='message assistant image-result';wrap.innerHTML='<div class="role">IMAGE</div><div class="bubble image-job-card"></div>';chat.appendChild(wrap);el=wrap.querySelector('.image-job-card');imageJobEls.set(job.id,el);}paintImageJob(el,job);if(!['finished','failed','cancelled'].includes(job.state))setTimeout(()=>pollImageJob(job.id),500);}chat.scrollTop=chat.scrollHeight;}
function renderAgentResult(data,{addAssistant=true}={}){if(addAssistant)addMessage('assistant',data.content);addRoute(data.routing,data.model_events);renderImageJobs(data.image_jobs||[]);const logs=[];if(data.model_events?.length)logs.push('MODEL EVENTS\n'+data.model_events.map((x,i)=>`${i+1}. ${JSON.stringify(x)}`).join('\n'));if(data.tool_events?.length)logs.push('TOOL EVENTS\n'+data.tool_events.map((x,i)=>`${i+1}. ${x.name} ${JSON.stringify(x.arguments)}\n${x.result}`).join('\n\n'));if(logs.length){activity.textContent=logs.join('\n\n');setUtilityPanel('terminal');}renderTask(data.task);}
async function resumeTask(approved){if(!lastTask)return;send.disabled=true;try{const res=await fetch('/api/tasks/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:lastTask.id,approved})});const data=await res.json();if(!res.ok)throw new Error(data.error||'Could not resume task');renderAgentResult(data);await loadStatus(false);}catch(err){addMessage('assistant',`Resume error: ${err.message}`);}finally{send.disabled=false;}}
async function recoverTask(taskId){send.disabled=true;try{addMessage('assistant','Recovering the interrupted task from its saved workspace/checkpoint state…');const res=await fetch('/api/tasks/recover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId})});const data=await res.json();if(!res.ok)throw new Error(data.error||'Could not recover task');renderAgentResult(data);await loadStatus(false);}catch(err){addMessage('assistant',`Recovery error: ${err.message}`);}finally{send.disabled=false;}}
const nexusPhaseCopy={
  planning:'Plotting response course',
  working:'Processing request',
  researching_failure:'Investigating anomaly',
  waiting_approval:'Awaiting command authorization',
  verifying:'Running diagnostics',
  reviewing:'Cross-checking output',
  interrupted:'Recovering task state',
  done:'Sequence complete',
};
function nexusThinkingMarkup(){
  return '<div class="nexus-thinking-hud"><div class="nexus-core-orbit"><span></span><i></i></div><div class="nexus-thinking-main"><div class="nexus-thinking-title">NEXUS CORE // ACTIVE</div><div class="nexus-thinking-summary">Establishing context link…</div><div class="nexus-thinking-list"></div></div><div class="nexus-thinking-telemetry">00s</div></div><div class="nexus-response-text"></div>';
}
function nexusThinkingStep(state,label,detail='',key=''){
  if(!state?.hud)return;
  const id=key||label;
  const existing=state.steps.find(x=>x.id===id);
  if(existing){existing.label=label;existing.detail=detail;existing.time=Math.max(0,Math.round(Date.now()/1000-state.startedAt));}
  else{state.steps.push({id,label,detail,time:Math.max(0,Math.round(Date.now()/1000-state.startedAt))});if(state.steps.length>6)state.steps.shift();}
  state.list.innerHTML=state.steps.map((x,i)=>'<div class="nexus-thinking-step '+(i===state.steps.length-1?'active':'done')+'"><span class="nexus-step-dot"></span><div><strong>'+esc(x.label)+'</strong>'+(x.detail?'<small>'+esc(x.detail)+'</small>':'')+'</div><em>'+x.time+'s</em></div>').join('');
  state.summary.textContent=detail||label;
}
function nexusThinkingPhase(state,phase,model='',elapsed=0){
  const normalized=String(phase||'working').replaceAll('_',' ');
  const label=nexusPhaseCopy[phase]||('Processing · '+normalized);
  const detail=model?model+' online':normalized;
  if(state.lastPhase!==phase){state.lastPhase=phase;nexusThinkingStep(state,label,detail,'phase:'+phase);}
  else if(state.summary&&!state.receivedToken)state.summary.textContent=label+(model?' · '+model:'');
  if(state.telemetry)state.telemetry.textContent=String(Math.max(0,Number(elapsed||0))).padStart(2,'0')+'s';
}
function beginAssistantStream(){
  const welcome=chat.querySelector('.welcome');if(welcome)welcome.remove();
  const wrap=document.createElement('div');wrap.className='message assistant streaming';
  wrap.innerHTML='<div class="role">assistant</div><div class="bubble">'+nexusThinkingMarkup()+'</div>';
  chat.appendChild(wrap);chat.scrollTop=chat.scrollHeight;
  const bubble=wrap.querySelector('.bubble');
  const state={wrap,bubble,hud:bubble.querySelector('.nexus-thinking-hud'),summary:bubble.querySelector('.nexus-thinking-summary'),list:bubble.querySelector('.nexus-thinking-list'),telemetry:bubble.querySelector('.nexus-thinking-telemetry'),text:bubble.querySelector('.nexus-response-text'),steps:[],lastPhase:'',receivedToken:false,result:null,error:null,lastTask:null,startedAt:Date.now()/1000,requestMessage:''};
  nexusThinkingStep(state,'Context link established','Reading conversation state','context');
  return state;
}
function appendLiveActivity(text){
  const existing=activity.textContent.trim();
  if(!existing||activity.querySelector('.muted'))activity.textContent='';
  activity.textContent+=(activity.textContent?'\n':'')+text;
  if(activity.textContent.length>30000)activity.textContent=activity.textContent.slice(-30000);
  setUtilityPanel('terminal');
}
function handleAgentStreamEvent(name,data,state){
  if(name==='ready'){nexusThinkingStep(state,'Command channel open','Agent stream synchronized','ready');return;}
  if(name==='token'){
    if(!state.receivedToken){state.receivedToken=true;state.hud?.classList.add('compact');nexusThinkingStep(state,'Synthesis stream online','Composing response','synthesis');}
    state.text.textContent+=String(data.text||'');chat.scrollTop=chat.scrollHeight;return;
  }
  if(name==='heartbeat'){if(!state.error)nexusThinkingPhase(state,String(data.phase||'working'),String(data.model_id||''),Number(data.elapsed_seconds||0));chat.scrollTop=chat.scrollHeight;return;}
  if(name==='task'&&data.task){state.lastTask=data.task;renderTask(data.task);nexusThinkingPhase(state,String(data.task.phase||'working'),String(data.task.model_id||''),Math.round(Date.now()/1000-state.startedAt));return;}
  if(name==='approval'){if(data.task)renderTask(data.task);nexusThinkingStep(state,'Authorization hold','Waiting for your approval','approval');setUtilityPanel('tasks');return;}
  if(name==='model'){
    const e=data.event||{};
    if(e.type==='generic_refusal_retry'){state.receivedToken=false;if(state.text)state.text.textContent='';state.hud?.classList.remove('compact');nexusThinkingStep(state,'Policy re-alignment','Discarding canned refusal and retrying','policy-retry');}
    else if(e.type==='switch'||e.type==='activation_fallback')nexusThinkingStep(state,'Routing matrix updated',(e.from||'model')+' → '+(e.to||e.model_id||''),'model-switch');
    else nexusThinkingStep(state,'Model route locked',(e.model_id||e.to||'local model')+(e.role?' · '+e.role:''),'model');
    appendLiveActivity(`MODEL · ${e.type||'event'} · ${e.model_id||e.to||''} ${e.role||''}`.trim());return;
  }
  if(name==='research'){const p=data.research?.plan||data.research||{};nexusThinkingStep(state,'Sensor sweep',p.mode||'Researching external evidence','research');appendLiveActivity(`RESEARCH · ${p.mode||'preflight'}${p.needed===true?' · evidence needed':''}`);return;}
  if(name==='tool'){const t=data.tool||{};nexusThinkingStep(state,'Engineering operation',String(t.name||'tool').replaceAll('_',' '),'tool:'+String(t.name||'unknown'));appendLiveActivity(`TOOL · ${t.name||'unknown'} ${JSON.stringify(t.arguments||{})}\n${String(t.result||'').slice(-6000)}`);return;}
  if(name==='image_job'&&data.job){renderImageJobs([data.job]);nexusThinkingStep(state,'Image synthesis',String(data.job.stage||data.job.state||'generation'),'image');if(!state.receivedToken&&state.summary)state.summary.textContent='Image synthesis in progress';chat.scrollTop=chat.scrollHeight;return;}
  if(name==='result'){state.result=data;state.bubble.textContent=String(data.content||'');state.wrap.classList.remove('streaming');if(!state.wrap.querySelector('.message-feedback'))state.wrap.insertAdjacentHTML('beforeend',feedbackControls());chat.scrollTop=chat.scrollHeight;return;}
  if(name==='error'){state.error=String(data.error||'Agent stream failed');state.bubble.textContent=state.error;state.wrap.classList.remove('streaming');chat.scrollTop=chat.scrollHeight;return;}
}
function parseSseBlock(block,state){
  const lines=block.split(/\r?\n/);let name='message';const data=[];
  for(const line of lines){if(line.startsWith('event:'))name=line.slice(6).trim();else if(line.startsWith('data:'))data.push(line.slice(5).trimStart());}
  if(!data.length)return;let payload;const raw=data.join('\n');try{payload=JSON.parse(raw);}catch{payload={text:raw};}
  handleAgentStreamEvent(name,payload,state);
}
async function streamAgent(message){
  const state=beginAssistantStream();state.requestMessage=message;
  const res=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json','Accept':'text/event-stream'},body:JSON.stringify({message,mode:mode.value})});
  if(!res.ok){let detail='Request failed',code='';try{const d=await res.json();detail=d.error||detail;code=d.code||'';}catch{}state.bubble.textContent=detail;state.wrap.classList.remove('streaming');if(code==='coding_model_setup_required'){document.querySelector('#systemBlock')?.setAttribute('open','');loadReadiness();}const err=new Error(detail);err.displayed=true;throw err;}
  if(!res.body)throw new Error('Streaming response body is unavailable in this browser.');
  const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';
  while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});let split;while((split=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,split);buffer=buffer.slice(split+2);if(block.trim())parseSseBlock(block,state);}if(done)break;}
  if(buffer.trim())parseSseBlock(buffer,state);
  if(state.error){
    const err=new Error(state.error);err.displayed=true;throw err;
  }
  if(!state.result){
    let detail='Chat Nexus connection closed before the task returned a final result.';
    try{
      const statusRes=await fetch('/api/tasks');
      if(statusRes.ok){
        const statusData=await statusRes.json();
        const current=statusData.current;
        const currentIsThisRequest=current&&(
          String(current.prompt||'')===state.requestMessage ||
          Number(current.created_at||0)>=state.startedAt-1
        );
        const lastUpdated=Number(state.lastTask?.updated_at||0);
        const currentUpdated=Number(current?.updated_at||0);
        const task=currentIsThisRequest&&(!state.lastTask||currentUpdated>=lastUpdated)?current:state.lastTask;
        const terminal=['completed','completed_with_warnings','step_limit'];
        if(task&&terminal.includes(task.status)&&String(task.final_content||task.summary||'').trim()){
          const recovered={
            content:String(task.final_content||task.summary||''),
            routing:{
              role:String(task.model_role||'primary_coder'),
              model_id:String(task.model_id||'recovered'),
              complexity:0,
              reasons:['final result recovered from durable task ledger after stream closed'],
            },
            tool_events:[],
            model_events:[{type:'stream_result_recovered',model_id:String(task.model_id||''),role:String(task.model_role||'')}],
            steps:Number(task.steps||0),
            task,
            pending_approval:task.pending_approval||null,
            verification:task.verification||[],
            review:String(task.review||''),
            research:task.research||{},
            image_jobs:[],
          };
          state.result=recovered;
          state.bubble.textContent=recovered.content;
          state.wrap.classList.remove('streaming');
          if(!state.wrap.querySelector('.message-feedback'))state.wrap.insertAdjacentHTML('beforeend',feedbackControls());
          chat.scrollTop=chat.scrollHeight;
          return recovered;
        }
        if(task?.error)detail=task.error;
        else if(task?.status==='interrupted')detail='Chat Nexus restarted while this task was running. Use Resume interrupted task to continue from the saved checkpoint.';
        else if(task?.status==='waiting_approval')detail='The task is waiting for approval. Open the Tasks panel to continue.';
        else if(task?.status)detail+=' Current task status: '+task.status+'.';
      }
    }catch{}
    state.bubble.textContent=detail;state.wrap.classList.remove('streaming');
    const err=new Error(detail);err.displayed=true;throw err;
  }
  return state.result;
}
async function undoTask(taskId){if(!confirm('Restore files to their state before this task?'))return;const res=await fetch('/api/tasks/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId})});const data=await res.json();if(!res.ok){addMessage('assistant',`Undo error: ${data.error||'failed'}`);return;}addMessage('assistant',`Restored ${data.restored.length} file(s) from the task checkpoint.`);await loadStatus(false);}
$('#taskPanel').addEventListener('click',e=>{const a=e.target.closest('[data-approve]');if(a){resumeTask(a.dataset.approve==='1');return;}const r=e.target.closest('[data-recover]');if(r){recoverTask(r.dataset.recover);return;}const u=e.target.closest('[data-undo]');if(u)undoTask(u.dataset.undo);});
$('#models').addEventListener('click',async e=>{const btn=e.target.closest('.runtime-action');if(!btn)return;btn.disabled=true;try{await runtimeAction(btn.dataset.action,btn.dataset.model);}catch(err){addMessage('assistant',`Runtime error: ${err.message}`);}finally{btn.disabled=false;}});
$('#readinessPanel').addEventListener('click',e=>{const plan=e.target.closest('[data-model-plan]');if(plan){installModelPlan(plan.dataset.modelPlan);return;}if(e.target.closest('#startSelfDevelopment')){prepareSelfDevelopmentTask();return;}if(e.target.closest('#applyModelSetup')){applySuggestedModelSetup();return;}const copy=e.target.closest('.copy-runtime-command'),install=e.target.closest('.catalog-install'),repair=e.target.closest('.catalog-repair'),cancel=e.target.closest('.catalog-cancel');if(copy)copyText(copy.dataset.command);else if(install)startCatalogInstall(install.dataset.catalog,false);else if(repair)startCatalogInstall(repair.dataset.catalog,true);else if(cancel)cancelCatalogInstall(cancel.dataset.job);});
$('#refreshRuntime').addEventListener('click',async()=>{await loadStatus(true);await loadReadiness();});
$('#refreshMemory').addEventListener('click',loadConversationMemory);
$('#policyMode').addEventListener('change',async e=>{const select=e.target;select.disabled=true;try{await setPolicyMode(select.value,$('#ethicalTemperature').value);}catch(err){addMessage('assistant',`Policy update error: ${err.message}`);await loadStatus(false);}finally{select.disabled=false;}});
$('#ethicalTemperature').addEventListener('input',e=>{$('#ethicalTemperatureValue').textContent=Number(e.target.value).toFixed(2);});
$('#ethicalTemperature').addEventListener('change',async e=>{const slider=e.target;slider.disabled=true;try{await setPolicyMode($('#policyMode').value,slider.value);}catch(err){addMessage('assistant',`Ethical temperature update error: ${err.message}`);await loadStatus(false);}finally{slider.disabled=false;}});
$('#rebuildIndex').addEventListener('click',async()=>{const b=$('#rebuildIndex');b.disabled=true;try{const res=await fetch('/api/index/rebuild',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const data=await res.json();if(!res.ok)throw new Error(data.error||'Index rebuild failed');await loadStatus(false);}catch(e){addMessage('assistant',`Index error: ${e.message}`);}finally{b.disabled=false;}});
function builtinClientReply(message){
  const normalized=message.trim().toLowerCase().replace(/[!?.,]+$/,'').trim();
  if(['hi','hello','hey','hey there','good morning','good afternoon','good evening'].includes(normalized)){
    return 'Hi! Chat Nexus is ready. What would you like to work on?';
  }
  if(['what can you do','what all can you do','what are your capabilities','what do you do','how can you help'].some(x=>normalized.includes(x))){
    return 'I can inspect and edit code, build features, debug errors, run tests and commands with permission gates, research technical issues, work with Git/GitHub when authorized, manage local coding models, and use configured local image tools.';
  }
  if(['how old are you','do you have an age','what is your age',"what's your age"].includes(normalized)){
    return "I don't have a human age. I'm Chat Nexus, software, so I don't age like a person.";
  }
  if(['who are you','what are you','what is your name',"what's your name",'are you human'].includes(normalized)){
    return "I'm Chat Nexus, a local-first AI coding workstation. I'm software, not a person.";
  }
  return '';
}
form.addEventListener('submit',async e=>{e.preventDefault();const message=input.value.trim();if(!message)return;addMessage('user',message);input.value='';const builtin=builtinClientReply(message);if(builtin){addMessage('assistant',builtin);recordBuiltinExchange(message,builtin).then(()=>Promise.all([loadConversationMemory(),loadConversations()]));input.focus();return;}send.disabled=true;send.textContent='…';try{const data=await streamAgent(message);renderAgentResult(data,{addAssistant:false});await loadStatus(false);await Promise.all([loadConversationMemory(),loadConversations()]);}catch(err){if(!err.displayed)addMessage('assistant',`Error: ${err.message}`);}finally{send.disabled=false;send.textContent='↗';input.focus();}});
chat.addEventListener('click',async e=>{const feedback=e.target.closest('[data-feedback]');if(feedback){feedback.disabled=true;try{await fetch('/api/conversations/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rating:feedback.dataset.feedback,message_id:feedback.dataset.messageId||''})});feedback.textContent=feedback.dataset.feedback==='up'?'✓':'✕';}catch{}return;}const prompt=e.target.closest('[data-prompt]');if(prompt){input.value=prompt.dataset.prompt||'';input.focus();return;}const b=e.target.closest('[data-image-action]');if(!b)return;const p=b.dataset.path||'';const verb={edit:'Edit this image',variation:'Create a variation of this image',upscale:'Upscale this image'}[b.dataset.imageAction]||'Edit this image';input.value=`${verb}: ${p}\n`;input.focus();});
$('#newChat').addEventListener('click',()=>newConversation().catch(e=>addMessage('assistant',`New chat error: ${e.message}`)));
$('#newChatSmall').addEventListener('click',()=>newConversation().catch(e=>addMessage('assistant',`New chat error: ${e.message}`)));
$('#conversationList').addEventListener('click',e=>{const row=e.target.closest('[data-conversation]');if(row)selectConversation(row.dataset.conversation).catch(err=>addMessage('assistant',`Conversation error: ${err.message}`));});
let conversationSearchTimer=null;
$('#conversationSearch').addEventListener('input',e=>{clearTimeout(conversationSearchTimer);const q=e.target.value.trim();conversationSearchTimer=setTimeout(()=>loadConversations(q),180);});
document.querySelectorAll('.utility-tab').forEach(btn=>btn.addEventListener('click',()=>setUtilityPanel(btn.dataset.panel)));
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();form.requestSubmit();}});
loadStatus().then(async()=>{const [_,__,convos]=await Promise.all([loadReadiness(),loadConversationMemory(),loadConversations()]);const active=convos?.active;if(active?.messages?.length)renderConversationHistory(active.messages);});input.focus();
