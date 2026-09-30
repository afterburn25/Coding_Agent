
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
let memoryState=null, knowledgeState=null, growthState=null, conversationState=null;

async function getJson(path){
  const r=await fetch(path); const d=await r.json();
  if(!r.ok) throw new Error(d.error||'Request failed'); return d;
}
async function postJson(path,body={}){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(!r.ok) throw new Error(d.error||'Request failed'); return d;
}

function renderStats(){
  const facts=(memoryState?.facts||[]);
  const rules=(memoryState?.behavior_rules||[]);
  const examples=memoryState?.training_examples||[];
  $('#factCount').textContent=facts.filter(x=>x.active!==false).length;
  $('#ruleCount').textContent=rules.filter(x=>x.active!==false).length;
  $('#exampleCount').textContent=examples.length;
  $('#knowledgeCount').textContent=knowledgeState?.records||0;
  $('#pendingCount').textContent=growthState?.candidate_counts?.pending||0;
  $('#approvedCount').textContent=growthState?.candidate_counts?.approved||0;
  const ruleRows=rules.slice().reverse().slice(0,50).map(x =>
    '<div class="trainer-row '+(x.active===false?'disabled-memory':'')+'"><span class="tag">RULE</span><div><strong>'+esc(x.text||'')+'</strong><small>'+esc(x.scope||'global')+' · '+(x.active===false?'disabled':'active')+'</small></div><button data-memory-edit="rule" data-id="'+esc(x.id||'')+'">Edit</button><button data-memory-toggle="rule" data-id="'+esc(x.id||'')+'" data-active="'+(x.active!==false)+'">'+(x.active===false?'Enable':'Disable')+'</button><button data-memory-scope="rule" data-id="'+esc(x.id||'')+'">Scope</button></div>'
  );
  const factRows=facts.slice().reverse().slice(0,50).map(x =>
    '<div class="trainer-row '+(x.active===false?'disabled-memory':'')+'"><span class="tag fact">FACT</span><div><strong>'+esc(x.text||'')+'</strong><small>'+esc(x.scope||'global')+' · '+(x.active===false?'disabled':'active')+'</small></div><button data-memory-edit="fact" data-id="'+esc(x.id||'')+'">Edit</button><button data-memory-toggle="fact" data-id="'+esc(x.id||'')+'" data-active="'+(x.active!==false)+'">'+(x.active===false?'Enable':'Disable')+'</button><button data-memory-scope="fact" data-id="'+esc(x.id||'')+'">Scope</button></div>'
  );
  $('#rulesList').innerHTML=(ruleRows.length||factRows.length)?ruleRows.concat(factRows).join(''):'<span class="muted">No learned rules or facts yet.</span>';
}

function renderPersonality(){
  const p=conversationState?.personality||{};
  const keys=['warmth','humor','verbosity','curiosity','formality','initiative','slang','follow_up_frequency'];
  $('#personalityControls').innerHTML=keys.map(k =>
    '<label class="range-row"><span>'+esc(k.replaceAll('_',' '))+'</span><input type="range" min="0" max="100" value="'+Number(p[k]??50)+'" data-personality="'+k+'"><output>'+Number(p[k]??50)+'</output></label>'
  ).join('');
  $('#personalityControls').querySelectorAll('input[type=range]').forEach(i=>i.addEventListener('input',()=>i.nextElementSibling.textContent=i.value));
}

function renderCandidates(){
  const filter=$('#candidateFilter').value;
  const items=(growthState?.candidates||[]).filter(x=>!filter||x.status===filter);
  $('#candidateList').innerHTML=items.length?items.slice(0,200).map(x=>{
    const meta=x.metadata||{};
    const sources=(meta.sources||[]).slice(0,3).map(s=>s.url?'<a href="'+esc(s.url)+'" target="_blank" rel="noopener">'+esc(s.title||s.url)+'</a>':'').filter(Boolean).join(' · ');
    return '<div class="candidate-row"><div class="candidate-head"><span class="tag '+esc(x.kind)+'">'+esc(x.kind)+'</span><span class="status '+esc(x.status)+'">'+esc(x.status)+'</span></div><strong>'+esc(String(x.instruction||'').slice(0,700))+'</strong><p>'+esc(String(x.response||'').slice(0,1600))+'</p>'+(sources?'<small>'+sources+'</small>':'')+'<div class="candidate-actions"><button data-review="approved" data-id="'+esc(x.id)+'">Approve</button><button data-review="rejected" data-id="'+esc(x.id)+'">Reject</button><button data-review="pending" data-id="'+esc(x.id)+'">Pending</button></div></div>';
  }).join(''):'<span class="muted">No candidates in this queue.</span>';
}

function renderKnowledge(){
  const rows=knowledgeState?.recent||[];
  $('#knowledgeList').innerHTML=rows.length?rows.map(x=>{
    const src=(x.sources||[]).slice(0,4).map(s=>s.url?'<a href="'+esc(s.url)+'" target="_blank" rel="noopener">'+esc(s.title||s.url)+'</a>':'').filter(Boolean).join('<br>');
    const expiry=x.expires_at?new Date(x.expires_at*1000).toLocaleString():'';
    return '<div class="knowledge-row"><strong>'+esc(x.query||'')+'</strong><p>'+esc(String(x.answer||'').slice(0,1200))+'</p><small>'+(x.current_sensitive?'refresh-sensitive · ':'')+'expires '+esc(expiry)+'</small>'+(src?'<div class="source-links">'+src+'</div>':'')+'</div>';
  }).join(''):'<span class="muted">No sourced knowledge yet.</span>';
}

function renderRegistry(){
  const reg=growthState?.registry||{};
  const active=reg.active_candidate_id||'';
  const rows=reg.models||[];
  $('#registryList').innerHTML=rows.length?rows.slice().reverse().map(x => {
    let actions='';
    if(x.status==='candidate')actions='<button data-evaluate="pass" data-id="'+esc(x.id)+'">Eval pass</button><button data-evaluate="fail" data-id="'+esc(x.id)+'">Eval fail</button>';
    else if(x.status==='evaluated'&&x.id!==active)actions='<button data-promote="'+esc(x.id)+'">Promote</button>';
    return '<div class="trainer-row"><span class="tag '+(x.id===active?'active-model':'')+'">'+(x.id===active?'ACTIVE':'MODEL')+'</span><div><strong>'+esc(x.id)+' · '+esc(x.base_model_id)+'</strong><small>'+esc(x.status)+' · '+esc(x.artifact_path||'')+'</small></div>'+actions+'</div>';
  }).join(''):'<span class="muted">No trained candidate models registered yet.</span>';
  const jobs=growthState?.jobs||[];
  if(jobs.length){
    $('#growthJobResult').innerHTML=jobs.slice(0,8).map(j=>'<div class="job-row"><div><strong>'+esc(j.output_name)+'</strong><small>'+esc(j.method)+' · '+esc(j.status)+' · '+esc(j.output_dir)+'</small></div>'+(j.status==='planned'?'<button data-start-job="'+esc(j.id)+'">Run</button>':'')+'</div>').join('');
  }
}

async function refresh(){
  try{
    [memoryState,knowledgeState,growthState,conversationState]=await Promise.all([
      getJson('/api/conversation-memory'),
      getJson('/api/knowledge-memory'),
      getJson('/api/model-growth'),
      getJson('/api/conversations')
    ]);
    renderStats(); renderPersonality(); renderCandidates(); renderKnowledge(); renderRegistry();
  }catch(e){ document.body.dataset.error=e.message; }
}

$('#rulesList').addEventListener('click',async e=>{
  const toggle=e.target.closest('[data-memory-toggle]');
  const edit=e.target.closest('[data-memory-edit]');
  const scope=e.target.closest('[data-memory-scope]');
  const button=toggle||edit||scope;
  if(!button)return;
  const kind=button.dataset.memoryToggle||button.dataset.memoryEdit||button.dataset.memoryScope;
  const itemId=button.dataset.id;
  try{
    if(toggle){
      await postJson('/api/conversation-memory/update',{kind,item_id:itemId,active:toggle.dataset.active!=='true'});
    }else if(edit){
      const current=button.closest('.trainer-row')?.querySelector('strong')?.textContent||'';
      const text=prompt('Edit learned item:',current);
      if(text===null)return;
      await postJson('/api/conversation-memory/update',{kind,item_id:itemId,text});
    }else if(scope){
      const value=prompt('Scope: global, project, or conversation','global');
      if(!value)return;
      await postJson('/api/conversation-memory/update',{kind,item_id:itemId,scope:value.trim().toLowerCase()});
    }
    await refresh();
  }catch(err){alert(err.message);}
});
$('#savePersonality').addEventListener('click',async()=>{
  const personality={}; document.querySelectorAll('[data-personality]').forEach(i=>personality[i.dataset.personality]=Number(i.value));
  try{const out=await postJson('/api/conversations/personality',{personality});conversationState.personality=out.personality;renderPersonality();}catch(e){alert(e.message);}
});
$('#syncGrowth').addEventListener('click',async()=>{try{await postJson('/api/model-growth/sync');await refresh();}catch(e){alert(e.message);}});
$('#candidateFilter').addEventListener('change',renderCandidates);
$('#candidateList').addEventListener('click',async e=>{
  const b=e.target.closest('[data-review]'); if(!b)return; b.disabled=true;
  try{await postJson('/api/model-growth/review',{candidate_id:b.dataset.id,status:b.dataset.review});await refresh();}catch(err){alert(err.message);}finally{b.disabled=false;}
});
$('#exportDataset').addEventListener('click',async()=>{
  try{
    const out=await postJson('/api/model-growth/export',{name:$('#datasetName').value.trim(),include_knowledge:$('#includeKnowledge').checked});
    $('#growthDataset').value=out.dataset.path||'';
    $('#datasetResult').innerHTML='<strong>'+esc(out.dataset.name)+'</strong><small>'+esc(out.dataset.examples)+' examples · '+esc(out.dataset.path)+'</small>';
  }catch(e){$('#datasetResult').textContent=e.message;}
});
$('#createGrowthJob').addEventListener('click',async()=>{
  try{
    const out=await postJson('/api/model-growth/job',{base_model_id:$('#growthBase').value,method:$('#growthMethod').value,dataset_path:$('#growthDataset').value.trim(),output_name:$('#growthName').value.trim(),trainer_command:$('#trainerCommand').value.trim()});
    $('#growthJobResult').innerHTML='<strong>'+esc(out.job.output_name)+'</strong><small>planned · '+esc(out.job.output_dir)+'</small>';
    $('#registerJobId').value=out.job.id||'';
    await refresh();
  }catch(e){$('#growthJobResult').textContent=e.message;}
});
$('#registerCandidate').addEventListener('click',async()=>{
  try{
    const out=await postJson('/api/model-growth/register',{job_id:$('#registerJobId').value.trim(),base_model_id:$('#growthBase').value,artifact_path:$('#registerArtifact').value.trim(),metrics:{}});
    $('#registerArtifact').value='';
    await refresh();
  }catch(e){alert(e.message);}
});
$('#registryList').addEventListener('click',async e=>{
  const evalBtn=e.target.closest('[data-evaluate]');
  if(evalBtn){
    const passed=evalBtn.dataset.evaluate==='pass';
    try{await postJson('/api/model-growth/evaluate',{candidate_id:evalBtn.dataset.id,passed,metrics:{manual_review:true}});await refresh();}catch(err){alert(err.message);}
    return;
  }
  const b=e.target.closest('[data-promote]'); if(!b)return;
  if(!confirm('Promote this evaluated candidate model?'))return;
  try{await postJson('/api/model-growth/promote',{candidate_id:b.dataset.promote});await refresh();}catch(err){alert(err.message);}
});
$('#growthJobResult').addEventListener('click',async e=>{
  const b=e.target.closest('[data-start-job]'); if(!b)return;
  if(!confirm('Start this configured external training job now?'))return;
  b.disabled=true;
  try{await postJson('/api/model-growth/job/start',{job_id:b.dataset.startJob});await refresh();}catch(err){alert(err.message);}finally{b.disabled=false;}
});
$('#rollbackGrowth').addEventListener('click',async()=>{
  if(!confirm('Rollback the active candidate and return to the base/default model configuration?'))return;
  try{await postJson('/api/model-growth/rollback');await refresh();}catch(e){alert(e.message);}
});
refresh();
