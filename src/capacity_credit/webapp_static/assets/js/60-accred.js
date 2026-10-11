const REL_FUNCS = {
  /* D-3 双栈收敛（2026-09-23）：旧向导退居纯充裕度；ELCC/EFC 走六步模块 */
  adequacy:{title:'系统可靠性评估', status:'待验收', desc:'LOLE（天/年）/LOLH（小时/年）/EUE（MWh/年）/NEUE（ppm）四指标并列＋收敛状态＋风险事件。', note:'入口已接线，完整运行与结果闭环待验收。'},
};
let REL_FUNCTION = null;   // adequacy（D-3 双栈收敛后旧向导仅充裕度）
let REL_STACK = null;      // D-3：最近打开的可靠性流程（'relmod' 六步 | 'accred' 充裕度向导）——导航 03 的分派依据
let REL_STEP = 'fn';       // fn | project | form | check | confirm
function viewAccred(){
  const steps = {'fn':0,'project':1,'form':2,'check':3,'confirm':4};
  const names = ['选择测算功能','选择/新建项目','填写参数','系统检查','确认发起'];
  const cur = steps[REL_STEP] ?? 0;
  return `
  <div class="ptitle">系统可靠性评估（充裕度）</div>
  <div class="psub">测算引擎①。本向导只承接充裕度评估；ELCC/EFC 测算请走「选择测算功能」的六步流程（数据→标定→接入资源→搜索→结果）。流程：选择功能 → 选择/新建项目 → 参数 → 检查 → 确认。系统不会替您默认选择任何项目。</div>
  <div class="steps">${names.map((n,i)=>`<div class="st ${i<cur?'done':i===cur?'cur':''}">${'①②③④⑤'[i]} ${n}</div>`).join('')}</div>
  <div id="rel-body"></div>`;
}
function renderRelPage(){
  const body = document.getElementById('rel-body');
  if(REL_STEP==='fn'){
    body.innerHTML = `<div class="entries" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">${
      Object.entries(REL_FUNCS).map(([id,f])=>`
      <div class="entry"><h3>${esc(f.title)} <span class="tag ${f.status==='待验收'?'lock':'ok'}">${esc(f.status)}</span></h3>
        <p>${esc(f.desc)}${f.note?`<br><b>${esc(f.note)}</b>`:''}</p>
        <div class="foot"><span class="mono-note">可靠性域</span><button class="btn pri" onclick="REL_FUNCTION='${id}';REL_STEP='project';REL_STACK='accred';renderRelPage()">选择此功能</button></div>
      </div>`).join('')}</div>
      <div class="card"><h3>规划能力（未交付，如实说明）</h3><table class="grid">
        <tr><td><b>目标可靠容量求解</b></td><td>规划中（需求类设计§3）；需独立求解服务与口径确认。</td></tr>
        <tr><td><b>容量缺口与备用裕度</b></td><td>规划中（需求类设计§4）；可靠容量与铭牌容量两口径将分别显示。</td></tr>
        <tr><td><b>容量供需系数</b></td><td>规划中（需求类设计§5）；分子/分母/封顶/下限等六项口径确认前不实现。</td></tr>
      </table></div>`;
    return;
  }
  if(REL_STEP==='project'){
    body.innerHTML = `<div class="card"><h3>新建${esc(REL_FUNCS[REL_FUNCTION].title)}项目</h3>
      <div class="psub">项目创建后即锁定为「${esc(REL_FUNCS[REL_FUNCTION].title)}」类型；一项目一正式结果，完成后只读。</div>
      <div class="fgrid">
        <div class="fld" id="f-rname"><label><b>项目名称</b>（必填）</label><input id="nr-name" placeholder="如：青海2027充裕度评估"></div>
        <div class="fld"><label><b>储能资源构成</b></label><select id="nr-storage"><option value="unconfirmed">尚未确认</option><option value="included">包含储能</option><option value="excluded">明确不包含储能</option></select></div>
        <div class="fld"><label><b>水电资源构成</b></label><select id="nr-hydro"><option value="unconfirmed">尚未确认</option><option value="included">包含水电</option><option value="excluded">明确不包含水电</option></select></div>
      </div>
      <button class="btn pri" onclick="createRelProject()">新建${esc(REL_FUNCS[REL_FUNCTION].title)}项目</button>
      <div style="margin-top:10px"><button class="btn" onclick="REL_STEP='fn';renderRelPage()">← 返回选择测算功能</button></div></div>
      <div class="card"><h3>该功能未完成项目（继续测算）</h3><div id="rel-projects" class="loading">读取中…</div></div>
      <div class="card"><h3>该功能已完成项目（只读：查看结果／复制为新项目）</h3><div id="rel-done" class="loading">读取中…</div></div>`;
    loadRelProjects();
    return;
  }
  if(REL_STEP==='form'){
    body.innerHTML = `<div class="card"><h3>参数：${esc(REL_FUNCS[REL_FUNCTION].title)}</h3>
      <div id="rel-form" class="loading">读取运行选项（冻结数据/样本集/候选资源）…</div></div>
      <div class="wizbar"><button class="btn" onclick="REL_STEP='project';renderRelPage()">← 上一步</button></div>`;
    loadRelForm();
    return;
  }
  if(REL_STEP==='check'){
    body.innerHTML = `<div class="card"><h3>系统检查：${esc(REL_FUNCS[REL_FUNCTION].title)}</h3>
      <div class="psub">检查依据：服务端运行选项真实状态（runnable/冻结批次/样本集/候选资源）；发起时引擎门还会再次校验。</div>
      <div id="rel-check" class="loading">检查中…</div>
      <div class="wizbar"><button class="btn" onclick="REL_STEP='form';renderRelPage()">← 上一步，返回填写</button>
      <button class="btn pri" id="rel-toconfirm" disabled>检查通过后可确认</button></div></div>`;
    renderRelCheck();
    return;
  }
  if(REL_STEP==='confirm'){
    body.innerHTML = `<div class="card"><h3>确认发起：${esc(REL_FUNCS[REL_FUNCTION].title)}</h3>
      <div class="hintbar">确认即发起测算：成功后正式结果绑定本项目（一项目一正式结果）；失败/未收敛属于技术执行尝试，不算业务测算，可修正后重试。未收敛不会显示为正式结论。</div>
      <div id="rel-confirm" class="loading">生成服务端预览…</div>
      <div class="wizbar"><button class="btn" onclick="REL_STEP='check';renderRelPage()">← 上一步，返回检查</button>
      <button class="btn pri" id="rel-start" onclick="startRelRun()">确认无误，发起${esc(REL_FUNCS[REL_FUNCTION].title)}</button></div></div>`;
    renderRelConfirm();
    return;
  }
}
async function loadRelProjects(){
  try{
    const projects = (await jget('/api/projects')).projects;
    const mine = projects.filter(p=>p.kind==='reliability' && !p.invalid && (!p.measurement_type || p.measurement_type===REL_FUNCTION));
    const open = mine.filter(p=>!p.formal_result);
    const done = mine.filter(p=>p.formal_result);
    document.getElementById('rel-projects').innerHTML = open.length ? open.map(p=>
      `<div class="rowitem"><div class="tt">${esc(p.name)}<small>${p.measurement_type?esc(REL_FUNCS[p.measurement_type].title):'遗留可靠性项目（未锁定类型）'}｜进行中</small></div>
       <button class="btn pri" onclick="pickRelProject('${esc(p.project_dir)}','${esc(p.name)}')">在此项目继续</button></div>`).join('')
      : '<span class="loading">该功能暂无未完成项目：请在上方新建。</span>';
    document.getElementById('rel-done').innerHTML = done.length ? done.map(p=>
      `<div class="rowitem"><div class="tt">${esc(p.name)}<small><span class="tag ok">已完成</span>｜正式结果 ${esc(String((p.formal_result||{}).run_ref||'').slice(0,8))}</small></div>
       <button class="btn" onclick="openResult('${esc(p.project_dir)}','reliability','${esc((p.formal_result||{}).run_ref||'')}')">查看结果</button>
       <button class="btn" onclick="copyProject('${esc(p.project_dir)}')">复制为新项目</button></div>`).join('')
      : '<span class="loading">该功能暂无已完成项目。</span>';
  }catch(e){ document.getElementById('rel-projects').innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function createRelProject(){
  const name = document.getElementById('nr-name').value.trim();
  const fld = document.getElementById('f-rname');
  if(!name){ fld.classList.add('err'); flash('项目名称不能为空'); return; }
  fld.classList.remove('err');
  try{
    const r = await jpost('/api/projects', {name, description:'', project_kind:'reliability', measurement_type: REL_FUNCTION,
      resource_composition:{storage:document.getElementById('nr-storage').value, hydro:document.getElementById('nr-hydro').value}});
    flash('项目创建成功（类型已锁定为「' + REL_FUNCS[REL_FUNCTION].title + '」）：' + r.name);
    pickRelProject(r.project_ref, r.name);
  }catch(e){ fld.classList.add('err'); flash('创建被拒绝：' + e.message); }
}
function pickRelProject(ref,name){ DATA_PROJECT2=ref; REL_STEP='form'; renderRelPage(); }
function relFormDefs(){
  const o = REL_OPTIONS;
  const frozenData = (o.data_refs||[]).filter(d=>d.frozen&&d.approved);
  const banks = o.scenario_banks||o.banks||[];
  const weightIds = o.weight_set_ids||[];
  const scenarioIds = o.scenario_ids||[];
  const metrics = [['lole_days_per_year','LOLE 期望缺电天数（天/年）'],['lolh_hours_per_year','LOLH 期望缺电小时数（小时/年）'],['eue_mwh_per_year','EUE 期望缺供电量（MWh/年）'],['neue_ppm','NEUE 缺供电量占比（ppm）']];
  const sel = (id,label,list)=>`<div class="fld"><label><b>${esc(label)}</b></label><select id="${id}">${list.map(x=>`<option value="${esc(x[0])}">${esc(x[1])}</option>`).join('')}</select></div>`;
  let h = sel('rl-data_ref','冻结数据（已审批+已冻结）', frozenData.map(d=>[d.import_batch_id,'批次 '+String(d.import_batch_id).slice(0,8)+'（已确认）']))
        + sel('rl-bank_id','模拟样本集', banks.map(b=>[b.scenario_bank_id,'样本集 '+String(b.scenario_bank_id).slice(0,8)+'（'+b.sample_count+'样本）']))
        + sel('rl-metric','主要风险指标（收敛锚）', metrics)
        + sel('rl-weight_set_id','各年占比方案', weightIds.map(w=>[w,w]))
        + `<div class="fld"><label>业务情景（可选）</label><select id="rl-scenario_id"><option value="">（不指定）</option>${scenarioIds.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select></div>`;
  /* D-3：旧向导仅充裕度（REL_FUNCTION 恒为 adequacy）——ELCC/EFC 的
     候选组/搜索参数表单分支随双栈收敛移除（六步模块承接） */
  return {html:h, frozenData, banks, weightIds};
}
async function loadRelForm(){
  const el = document.getElementById('rel-form');
  try{
    REL_OPTIONS = await jget('/api/runs/options?project=' + encodeURIComponent(DATA_PROJECT2));
    const d = relFormDefs();
    if(!(REL_OPTIONS.runnable && d.frozenData.length && d.banks.length && d.weightIds.length)){
      el.innerHTML = `<div class="errbar">当前不可运行：${esc((REL_OPTIONS.not_runnable_reasons||[]).join('；')||'运行条件未齐（冻结数据/样本集/各年占比方案）')}</div>
        <div class="fh">请在「基础数据管理」完成数据链（校验→审批→冻结→样本集）后返回。</div>`;
      return;
    }
    el.innerHTML = `<div class="fgrid">${d.html}</div>
      <div style="margin-top:8px"><button class="btn pri" onclick="REL_STEP='check';renderRelPage()">下一步：系统检查 →</button></div>`;
    document.querySelectorAll('#rel-form input[data-min],#rel-form input[data-max],#rel-form input[data-int]').forEach(inp=>{
      inp.addEventListener('input', ()=>{ sanitizeNumericInput(inp); clampNumericInput(inp); });
    });
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
function relSelections(){
  const g = id=>document.getElementById(id);
  const s = {data_ref:g('rl-data_ref')?.value||'', bank_id:g('rl-bank_id')?.value||'', metric:g('rl-metric')?.value||'', weight_set_id:g('rl-weight_set_id')?.value||'', scenario_id:g('rl-scenario_id')?.value||null};
  return s;
}
function renderRelCheck(){
  const el = document.getElementById('rel-check');
  const o = REL_OPTIONS||{}; const s = relSelections();
  const blockers = [];
  (o.not_runnable_reasons||[]).forEach(r=>blockers.push({label:'项目运行条件', current:'—', reason:r, range:'可运行', suggestion:'按提示到「基础数据管理」补齐数据链'}));
  const d = relFormDefs();
  if(!d.frozenData.length) blockers.push({label:'冻结数据', current:'无', reason:'无已审批且已冻结的数据批次', range:'≥1个冻结批次', suggestion:'完成数据链的审批与冻结'});
  if(!d.banks.length) blockers.push({label:'模拟样本集', current:'无', reason:'无可用模拟样本集', range:'≥1个样本集', suggestion:'生成样本集后返回'});
  if(!d.weightIds.length) blockers.push({label:'各年占比', current:'无', reason:'无可用各年占比方案', range:'≥1套占比方案', suggestion:'检查样本集的占比方案配置'});
  let html = blockers.length
    ? `<div class="errbar"><b>系统检查未通过（${blockers.length} 项阻断）。</b></div>
       <table class="grid"><tr><th>参数</th><th>当前</th><th>原因</th><th>合法要求</th><th>建议</th></tr>
       ${blockers.map(b=>`<tr><td class="mono-note">${esc(b.label)}</td><td>${esc(b.current)}</td><td>${esc(b.reason)}</td><td>${esc(b.range)}</td><td>${esc(b.suggestion)}</td></tr>`).join('')}</table>`
    : `<div class="hintbar"><b>系统检查通过：运行选项齐备（可运行、冻结数据、样本集、各年占比）。</b><br><span class="mono-note">本检查展示服务端运行选项状态；发起时引擎门会再次校验（结果以运行记录为准）。</span></div>`;
  el.innerHTML = html;
  const btn = document.getElementById('rel-toconfirm');
  if(!blockers.length && btn){ btn.disabled=false; btn.textContent='下一步：确认发起 →'; btn.onclick=()=>{ REL_STEP='confirm'; renderRelPage(); }; }
}
async function renderRelConfirm(){
  const el = document.getElementById('rel-confirm');
  const s = relSelections();
  try{
    const pv = await jpost('/api/runs/preview', {project_ref:DATA_PROJECT2, method:REL_FUNCTION, data_ref:s.data_ref, form:s});
    el.innerHTML = `<div class="hintbar">将提交：${esc(pv.summary)}</div>
      <table class="grid">${Object.entries(s).filter(([,v])=>v!==null&&v!=='').map(([k,v])=>`<tr><td style="width:220px" class="mono-note">${esc(k)}</td><td class="r">${esc(String(v))}</td></tr>`).join('')}</table>`;
  }catch(e){
    el.innerHTML = `<div class="errbar">预览被拒绝：${esc(e.message)}</div>`;
    const btn = document.getElementById('rel-start'); if(btn) btn.disabled = true;
  }
}
async function startRelRun(){
  const btn = document.getElementById('rel-start');
  btn.disabled = true; btn.textContent = '测算中…';
  const s = relSelections();
  try{
    const pv = await jpost('/api/runs/preview', {project_ref:DATA_PROJECT2, method:REL_FUNCTION, data_ref:s.data_ref, form:s});
    const st = await jpost('/api/runs/start', {project_ref:DATA_PROJECT2, data_ref:s.data_ref, method:REL_FUNCTION, config:pv.config});
    let snap = null;
    for(let i=0;i<150;i++){ await new Promise(r=>setTimeout(r,800)); snap = await jget('/api/runs/status?ref='+encodeURIComponent(st.request_ref)+'&project='+encodeURIComponent(DATA_PROJECT2)); if(snap.status==='execution_finished'||snap.status==='execution_failed') break; }
    if(st.run_ref){ RESULTS_PROJECT=DATA_PROJECT2; RESULTS_RUN=st.run_ref; PAGE='results'; rerender(); }
    else flash('执行状态：' + (snap?snap.status:'未知'));
  }catch(e){
    flash('发起被拒绝：' + e.message);
    btn.disabled = false; btn.textContent = '确认无误，发起' + REL_FUNCS[REL_FUNCTION].title;
  }
}
let DATA_PROJECT2 = null;

/* ============ 帮助 / 切片说明 ============ */
