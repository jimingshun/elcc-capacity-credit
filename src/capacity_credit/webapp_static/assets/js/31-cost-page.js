const MT_CARDS = [
  {id:'cost_fixed', domain:'cost', title:'固定成本测算', use:'核算参考机组折旧、利息、运维、保险与摊销的年度固定成本', inputs:'参考机型、省份、造价指数、建设/融资/折旧参数', outputs:'首年与经营期平均固定成本（元/kW·年）', status:'正式可用', needs:null},
  {id:'cost_gross_cone', domain:'cost', title:'Gross CONE测算', use:'以年度自由现金流求净现值为零的等额年度固定收入', inputs:'投资时序、折现率、税率、容量分母', outputs:'Gross CONE（元/kW·年）、NPV残差与求解状态', status:'正式可用', needs:null},
  {id:'cost_net_cone', domain:'cost', title:'Net CONE测算', use:'同口径Gross CONE扣减电能量及辅助服务净收入', inputs:'同口径Gross CONE、E&AS各项收支', outputs:'Net CONE（元/kW·年）；缺失不补零、负值保留', status:'正式可用', needs:'同口径Gross CONE结果或外部Gross'},
  {id:'elcc', domain:'reliability', title:'ELCC测算', use:'等风险意义下候选资源可承载的容量', inputs:'冻结可靠性数据、样本集、候选资源、搜索参数', outputs:'有效承载容量（MW）、搜索区间与收敛状态', status:'正式可用', needs:'冻结数据＋样本集；含储能基准拒绝'},
  {id:'efc', domain:'reliability', title:'EFC测算', use:'与候选资源相同系统风险的100%可靠完美容量', inputs:'冻结可靠性数据、样本集、候选资源、上界来源', outputs:'等效完美容量（MW）、搜索区间与收敛状态', status:'正式可用', needs:'冻结数据＋样本集'},
];
function supplyCardHTML(c){
  return `
    <div class="entry">
      <h3>${esc(c.title)} <span class="tag ok">${esc(c.status)}</span></h3>
      <p><b>业务用途：</b>${esc(c.use)}<br><b>主要输入：</b>${esc(c.inputs)}<br><b>主要输出：</b>${esc(c.outputs)}${c.needs?`<br><b>前置：</b>${esc(c.needs)}`:''}</p>
      <div class="foot"><span class="tag">${c.domain==='cost'?'成本域':'可靠性域'}</span><button class="btn pri" onclick="chooseType('${c.id}')">选择此类型</button></div>
    </div>`;
}
function viewSelectType(){
  /* Step1（2026-09-28 设计稿）：原 03/05 合并为本入口；5 卡按域分两组（成本域三件套｜可靠性域），
     卡片内容与 chooseType 分派/openProject/REL_STACK 逻辑逐字节不动 */
  return `
  <div class="ptitle">容量供给类参数</div>
  <div class="psub">一个测算项目对应一个测算类型，创建后锁定。选择类型后进入该类型的项目页（新建或继续已有项目）。</div>
  <div class="hintbar">任何类型都需先选择，不会默认进入；返回上一步保留已选内容。供给类共 5 项＝成本域三件套＋可靠性域两项（两层模型的参数底座）。</div>
  <div class="domgrp">成本域 · 三件套</div>
  <div class="entries t3">${MT_CARDS.filter(c=>c.domain==='cost').map(supplyCardHTML).join('')}
  </div>
  <div class="domgrp">可靠性域</div>
  <div class="entries t2">${MT_CARDS.filter(c=>c.domain==='reliability').map(supplyCardHTML).join('')}
  </div>
  <div style="margin-top:8px"><button class="btn" onclick="PAGE='home';rerender()">← 返回首页</button></div>`;
}
function chooseType(id){
  const card = MT_CARDS.find(c=>c.id===id);
  if(!card) return;
  if(card.domain==='cost'){ COST_FUNCTION=id; COST_STEP='project'; COST_PROJECT=null; COST_FORM={}; PAGE='cost'; }
  else { RM.method=id; RM.step=0; RM.done={}; RM.project=null; RM.options=null;
         RM.sel={}; RM.runRef=null; RM.view=null; RM.calibAck=false; DATA_PROJECT2=null; REL_STACK='relmod'; PAGE='relmod'; }
  rerender();
}

function viewCost(){
  const steps = {'project':0,'form':1,'check':2,'confirm':3};
  const names = ['选择测算项目','填写测算参数','系统检查','确认测算'];
  const cur = steps[COST_STEP] ?? 0;
  const fnLabel = COST_FUNCTION ? (COST_FUNCS[COST_FUNCTION]||{}).title : '未选择类型';
  return `
  <div class="ptitle">${esc(fnLabel)}</div>
  <div class="psub">测算类型已锁定为「${esc(fnLabel)}」（项目创建后不可更改）。流程：选择项目 → 填写参数 → 系统检查 → 确认测算 → 结果。比例类字段按百分数填写（内部单次转换）。</div>
  <div class="steps">${names.map((n,i)=>`<div class="st ${i<cur?'done':i===cur?'cur':''}">${'①②③④'[i]} ${n}</div>`).join('')}</div>
  <div id="cost-body"></div>`;
}
function renderCostPage(){
  const body = document.getElementById('cost-body');
  if(!COST_FUNCTION){
    body.innerHTML = `<div class="hintbar warn">尚未选择测算类型。<div style="margin-top:8px"><button class="btn" onclick="PAGE='select';rerender()">← 返回选择测算类型</button></div></div>`;
    return;
  }
  if(COST_STEP==='project'){
    const fnTitle = mlabel(COST_FUNCTION);
    body.innerHTML = `<div class="card"><h3>新建${esc(fnTitle)}项目</h3>
      <div class="psub">项目创建后即锁定为「${esc(fnTitle)}」类型；一个项目对应一次业务测算，完成后如需修改参数请“复制为新项目”。</div>
      <div class="fgrid">
        <div class="fld" id="f-tpname"><label><b>项目名称</b>（必填）</label><input id="np-typed-name" placeholder="如：青海2027固定成本测算"></div>
      </div>
      <button class="btn pri" onclick="createTypedProject()">新建${esc(fnTitle)}项目</button>
      <div style="margin-top:12px"><button class="btn" onclick="PAGE='select';rerender()">← 返回重新选择测算类型</button></div></div>
      <div class="card"><h3>该类型未完成项目（继续测算）</h3><div id="cost-projects" class="loading">读取中…</div></div>
      <div class="card"><h3>该类型已完成项目（只读：查看结果／复制为新项目）</h3><div id="cost-done" class="loading">读取中…</div></div>`;
    jget('/api/projects').then(d=>{
      const mine = d.projects.filter(p=>p.kind==='cost' && !p.invalid && (!p.measurement_type || p.measurement_type===COST_FUNCTION));
      const open = mine.filter(p=>!p.formal_result);
      const done = mine.filter(p=>p.formal_result);
      document.getElementById('cost-projects').innerHTML = open.length ? open.map(p=>
        `<div class="rowitem"><div class="tt">${esc(p.name)}<small>${p.measurement_type?`类型已锁定：${esc(mlabel(p.measurement_type))}`:'遗留成本项目（未锁定类型）'}｜进行中</small></div>
         <button class="btn pri" onclick="pickProject('${esc(p.project_dir)}','${esc(p.name)}')">在此项目继续</button>
         <button class="btn" onclick="costDeleteProject('${esc(p.project_dir)}','${esc(p.name)}')">删除</button></div>`).join('')
        : '<span class="loading">该类型暂无未完成项目：请在上方新建，或复制一个已完成项目。</span>';
      document.getElementById('cost-done').innerHTML = done.length ? done.map(p=>
        `<div class="rowitem"><div class="tt">${esc(p.name)}<small>${esc(mlabel(p.measurement_type)||'成本项目')}｜<span class="tag ok">已完成</span>｜正式结果 ${esc(String((p.formal_result||{}).run_ref||'').slice(0,8))}</small></div>
         <button class="btn" onclick="openResult('${esc(p.project_dir)}','cost','${esc((p.formal_result||{}).run_ref||'')}')">查看结果</button>
         <button class="btn" onclick="copyProject('${esc(p.project_dir)}')">复制为新项目</button></div>`).join('')
        : '<span class="loading">该类型暂无已完成项目。</span>';
    }).catch(e=>{ document.getElementById('cost-projects').innerHTML = `<div class="errbar">${esc(e.message)}</div>`; });
    return;
  }
  if(COST_STEP==='form'){
    const groups = F[COST_FUNCTION==='cost_fixed'?'fixed':COST_FUNCTION==='cost_gross_cone'?'gross':'net'];
    const cyRaw = (()=>{ const v = parseFloat(COST_FORM.cyears); return isNaN(v)?2:Math.max(1,Math.min(10,Math.round(v))); })();
    const bounds = BOUNDS[COST_FUNCTION==='cost_fixed'?'fixed':COST_FUNCTION==='cost_gross_cone'?'gross':'net']||{};
    const tweaks = LABEL_TWEAK[COST_FUNCTION==='cost_fixed'?'fixed':COST_FUNCTION==='cost_gross_cone'?'gross':'net']||{};
    const renderControl = (name,def)=>{
      const b = bounds[name];
      if(b){
        const attrs = `${b.min!==undefined?`data-min="${b.min}"`:''} ${b.max!==undefined?`data-max="${b.max}"`:''} ${b.int?'data-int="1"':''} ${b.step?`step="${b.step}"`:''}`;
        return `<input class="num" data-f="${esc(name)}" ${attrs} value="${esc(COST_FORM[name]!==undefined?COST_FORM[name]:def)}">`;
      }
      if(RENDER[name]) return RENDER[name]();
      return `<input class="num" data-f="${esc(name)}" value="${esc(COST_FORM[name]!==undefined?COST_FORM[name]:def)}">`;
    };
    const groupsHtml = groups.map((g,i)=>{
      let fields = g[1].map(([name,label,def])=>{
        const shown = tweaks[name]||label;
        const preset = (COST_FUNCTION==='cost_fixed'&&(name==='vat'||name==='loan_years'))||(COST_FUNCTION==='cost_gross_cone'&&name==='tax') ? presetSelect(name) : '';
        return `${preset}<div class="fld"><label>${shown.includes('必填')?`<b>${esc(shown)}</b>`:esc(shown)}</label>${renderControl(name,def)}</div>`;
      }).join('');
      if(COST_FUNCTION==='cost_fixed' && g[0]==='建设与融资'){
        const years = Array.from({length: cyRaw}, (_,idx)=>idx);
        fields += `</div><div class="fh" style="margin:-8px 0 10px">逐年投资比例（%，单项0—100，合计必须为100%；越界输入会被立即限制）：${years.map(idx=>`<input class="num" data-f="w${idx}" data-min="0" data-max="100" value="${esc(COST_FORM['w'+idx]!==undefined?COST_FORM['w'+idx]:(idx===0?60:idx===1?40:0))}" style="width:90px;margin-right:6px">`).join('')}</div><div class="fgrid">`;
      }
      return `<div class="fgroup" id="cg-${i}" ${i===0?'style="border-top:0;padding-top:0;margin-top:0"':''}>${esc(g[0])}</div><div class="fgrid">${fields}</div>`;
    }).join('');
    body.innerHTML = `
      <div class="wiz"><div class="anchors" id="wizAnchors">${groups.map((g,i)=>`<a data-t="#cg-${i}" class="${i===0?'on':''}">${esc(g[0])}</a>`).join('')}</div>
      <div style="flex:1;min-width:0"><div class="card">
        ${groupsHtml}
        ${COST_FUNCTION==='cost_net_cone'?`
        <div class="fgroup">Gross来源</div>
        <div class="fgrid">
          <div class="fld"><label><b>Gross来源模式</b></label>
            <select data-f="g_mode" id="net-gmode">
              <option value="外部输入">外部Gross CONE数值</option>
              <option value="引用本工具Gross运行">选择本工具已有Gross结果</option>
            </select></div>
          <div class="fld" id="net-gexternal"><label>外部Gross数值（≥0）</label><input class="num" data-f="gross" value=""></div>
          <div class="fld" id="net-ginternal" style="display:none"><label><b>选择已有Gross结果（跨项目，同口径校验）</b></label>
            <select data-f="g_run_ref" id="net-gref"><option value="">（加载中…）</option></select>
            <div class="fh" id="net-gref-note">显示项目、对象、年度、价格年、容量口径与结果SHA；服务端仍执行同口径校验。</div></div>
          <div class="fld"><label>外部Gross来源说明（外部输入时必填）</label><input data-f="gross_source" value=""></div>
          <div class="fld"><label>同口径确认</label>
            <select data-f="gross_conf" id="net-gconf"><option value="0">未确认</option><option value="1">已核对参考机组、地区、年度、价格年、税务、容量分母与年化方式一致</option></select></div>
          <div class="fld"><label>等额序列E&AS重复假设确认（引用模式）</label>
            <select data-f="g_rep" id="net-grep"><option value="0">未确认</option><option value="1">确认“每年重复该年度收支”</option></select></div>
        </div>
        <div class="hintbar warn">提交前系统将完整校验：任何留空金额＝未测算（拒绝，不按0处理）；引用模式由服务端核对两侧机组、年度与计价口径。</div>`:''}
      </div>
      <div class="wizbar">
        <button class="btn" onclick="COST_STEP='${COST_STEP==='form'?'project':'fn'}';rerender()">← 上一步</button>
        <button class="btn pri" onclick="toCheck()">下一步：系统检查 →</button>
      </div></div></div>`;
    wireFormControls();
    document.querySelectorAll('#wizAnchors a').forEach(a=>{
      a.onclick = ()=>{
        document.querySelectorAll('#wizAnchors a').forEach(x=>x.classList.remove('on'));
        a.classList.add('on');
        const el = document.querySelector(a.dataset.t); if(el) el.scrollIntoView({behavior:'smooth'});
      };
    });
    if(COST_FUNCTION==='cost_net_cone'){
      const gm = document.getElementById('net-gmode');
      if(gm){
        gm.onchange = ()=>{
          const internal = gm.value==='引用本工具Gross运行';
          document.getElementById('net-gexternal').style.display = internal?'none':'';
          document.getElementById('net-ginternal').style.display = internal?'':'none';
        };
        jget('/api/cost/gross-references').then(d=>{
          const sel = document.getElementById('net-gref');
          const runs = d.runs||[];
          sel.innerHTML = runs.length
            ? runs.map(r=>`<option value="${esc(r.run_ref)}">${esc(r.project_name)}｜${esc(r.object_name)||'—'}｜${esc(r.region)||'—'}｜${esc(r.target_year)}｜${esc(r.price_basis_year)}｜<span class="num">${esc(r.capacity_kw)}</span>kW｜SHA ${esc(r.result_sha256)}</option>`).join('')
            : '<option value="">（本工作区暂无可引用的完成Gross结果）</option>';
        }).catch(()=>{ const sel=document.getElementById('net-gref'); if(sel) sel.innerHTML='<option value="">（加载失败）</option>'; });
      }
    }
    return;
  }
  if(COST_STEP==='check'){
    // P-B: the graded check is SERVER-side (same authority as the
    // submit gate); the page renders its aggregate and never decides
    // pass/fail itself
    body.innerHTML = `
    <div class="card"><h3>系统检查：${esc(COST_FUNCS[COST_FUNCTION].title)}</h3>
      <div class="psub">项目：${esc(COST_PROJECT.name)}｜类型：${esc(COST_FUNCS[COST_FUNCTION].title)}｜检查与服务端提交门同源；参数修改后需重新检查。</div>
      <div id="server-check" class="loading">服务端检查中…</div>
      <div class="wizbar">
        <button class="btn" onclick="COST_STEP='form';rerender()">← 上一步，返回填写</button>
        <button class="btn pri" id="cost-toconfirm" disabled>检查通过后可确认</button>
      </div></div>`;
    renderServerCheck();
    return;
  }
  if(COST_STEP==='confirm'){
    // P-B: the SEPARATE confirmation page (check and confirm never
    // merge) - readonly summary + the single submit action
    const payload = Object.assign({}, COST_FORM);
    const label = v => v===undefined||v==='' ? '<i style="color:var(--faint)">（未填）</i>' : fmt(v);
    body.innerHTML = `
    <div class="card"><h3>确认测算：${esc(COST_FUNCS[COST_FUNCTION].title)}</h3>
      <div class="psub">项目：${esc(COST_PROJECT.name)}｜类型：${esc(COST_FUNCS[COST_FUNCTION].title)}（创建后已锁定）。</div>
      <div class="hintbar">确认即提交测算：成功后正式结果绑定本项目（一项目一正式结果，完成后只读）；失败或未收敛属于技术执行尝试，不算业务测算，可修正后重试。完成后如需修改参数，请使用「复制为新项目」。</div>
      <details class="disc" open><summary>参数摘要（只读，供最终核对）</summary><div class="body"><table class="grid">${Object.entries(payload).filter(([,v])=>v!==null&&v!==''&&!(Array.isArray(v)&&v.length===0)).map(([k,v])=>
        `<tr><td style="width:280px" class="mono-note">${esc(k)}</td><td class="r">${typeof v==='object'?esc(JSON.stringify(v)):label(v)}</td></tr>`).join('')}</table></div></details>
      <div class="wizbar">
        <button class="btn" onclick="COST_STEP='check';renderCostPage()">← 上一步，返回检查</button>
        <button class="btn pri" id="cost-confirm" onclick="submitCost()">确认无误，开始${esc(COST_FUNCS[COST_FUNCTION].title)}</button>
      </div></div>`;
    return;
  }
}
let LAST_CHECK = null;
async function renderServerCheck(){
  const el = document.getElementById('server-check');
  try{
    const r = await jpost('/api/check/' + COST_FUNCTION, {project_ref: COST_PROJECT.project_dir, form: COST_FORM});
    LAST_CHECK = r;
    const blockers = r.blockers||[], warnings = r.warnings||[];
    let html = blockers.length
      ? `<div class="errbar"><b>系统检查未通过（${blockers.length} 项阻断）。</b>请修正后再确认。</div>`
      : `<div class="hintbar"><b>系统检查已全部通过，可以进入确认测算。</b><br><span class="mono-note">检查规则 ${r.rules_checked} 项｜阻断 0｜警告 ${warnings.length} 项（已检查规则数不等于通过字段数；警告不阻止测算）</span></div>`;
    if(blockers.length){
      html += `<div class="card" style="border-color:var(--warn)"><h3 style="color:var(--warn)">阻断错误（${blockers.length}）</h3>
      <table class="grid"><tr><th>分组</th><th>参数</th><th>当前值</th><th>不通过原因</th><th>合法范围</th><th>修正建议</th><th></th></tr>
      ${blockers.map(b=>`<tr><td>${esc(b.group)}</td><td class="mono-note">${esc(b.label)}</td><td class="num">${esc(b.current)}</td><td>${esc(b.reason)}</td><td>${esc(b.range)}</td><td>${esc(b.suggestion)}</td>
        <td><button class="btn" onclick="goFix('${esc(b.field)}')">前往修改</button></td></tr>`).join('')}</table></div>`;
    }
    if(warnings.length){
      html += `<details class="disc" ${blockers.length?'':'open'}><summary>警告（${warnings.length}，不阻止测算）</summary><div class="body"><ul>${warnings.map(w=>`<li>${esc(w.reason||'')}</li>`).join('')}</ul></div></details>`;
    }
    el.innerHTML = html;
    const btn = document.getElementById('cost-toconfirm');
    if(!blockers.length && btn){
      btn.disabled = false; btn.textContent = '下一步：确认测算 →';
      btn.onclick = ()=>{ COST_STEP='confirm'; renderCostPage(); };
    }
  }catch(e){ if(el) el.innerHTML = `<div class="errbar">检查失败：${esc(e.message)}</div>`; }
}
function goFix(field){
  // keep the current value across the re-render (renderCostPage reads
  // COST_FORM), then locate the field - schedule rows render inside the
  // 建设与融资 group, so fall back to the group note when the exact
  // input is not present
  const before = document.querySelector(`#cost-body [data-f="${field}"]`);
  const val = before ? before.value : undefined;
  if(field==='cyears' && val!==undefined){ COST_FORM.cyears = val; }
  if(/^w\d+$/.test(field||'') && val!==undefined){ COST_FORM[field]=val; }
  COST_STEP='form'; renderCostPage();
  setTimeout(()=>{
    let el = document.querySelector(`#cost-body [data-f="${field}"]`);
    if(!el && /^w\d+$/.test(field||'')){
      el = [...document.querySelectorAll('#cost-body .fh')].find(n=>n.textContent.includes('逐年投资比例')) || document.querySelector('#cost-body .fh');
    }
    if(el){ el.scrollIntoView({behavior:'smooth',block:'center'}); if(el.focus) el.focus(); el.style.outline='2px solid var(--warn)'; setTimeout(()=>{el.style.outline='';},1600); }
  }, 150);
}
function pickProject(ref,name){ COST_PROJECT={project_dir:ref,name}; COST_STEP='form'; COST_FORM={}; rerender(); }
async function costDeleteProject(ref,name){
  if(!confirm('删除确认：项目「'+name+'」将以归档方式从测算列表移除（不销毁项目目录与已有运行记录，可在「01 基础数据管理」的已归档列表恢复）。确认删除？')) return;
  try{
    await jpost('/api/projects/archive',{project_ref:ref});
    if(COST_PROJECT && COST_PROJECT.project_dir===ref){ COST_PROJECT=null; COST_STEP='project'; COST_FORM={}; }
    flash('已删除（归档）：'+name+'——可在「01 基础数据管理」已归档列表恢复');
    rerender();
  }catch(e){ flash('删除被拒绝：'+e.message); }
}
async function createTypedProject(){
  /* P-A: creation FROM the type flow locks measurement_type at birth */
  const input = document.getElementById('np-typed-name');
  const fld = document.getElementById('f-tpname');
  const name = (input?input.value:'').trim();
  if(!name){ fld.classList.add('err'); flash('项目名称不能为空'); return; }
  fld.classList.remove('err');
  try{
    const r = await jpost('/api/projects', {name: name, description:'', project_kind:'cost', measurement_type: COST_FUNCTION});
    flash('项目创建成功（类型已锁定为「' + mlabel(COST_FUNCTION) + '」）：' + r.name);
    COST_PROJECT={project_dir:r.project_ref, name:r.name};
    COST_STEP='form'; COST_FORM={}; rerender();
  }catch(e){
    fld.classList.add('err');
    flash('创建被拒绝：' + e.message);
  }
}
function collectForm(){
  const out = {};
  document.querySelectorAll('#cost-body input[data-f]').forEach(inp=>{
    const v = inp.value.trim();
    out[inp.dataset.f] = v==='' ? '' : (Number.isFinite(Number(v)) ? Number(v) : v);
  });
  document.querySelectorAll('#cost-body select[data-f]').forEach(sel=>{ out[sel.dataset.f] = sel.value; });
  return out;
}
function toCheck(){
  // R6: 必填非空先就地标红，但不再提前 return——带留空必填项时仍进入
  // 系统检查分级页，由分级页把缺失项列为阻断错误（含“前往修改”）。
  const need = F[COST_FUNCTION==='cost_fixed'?'fixed':COST_FUNCTION==='cost_gross_cone'?'gross':'net']
    .flatMap(g=>g[1]).filter(f=>f[1].includes('必填'));
  for(const [name,label] of need){
    const inp = document.querySelector(`#cost-body input[data-f="${name}"]`);
    if(inp && inp.value.trim()===''){
      const fld = inp.closest('.fld');
      fld.classList.add('err');
      let fh = fld.querySelector('.fh');
      if(!fh){ fh = document.createElement('div'); fh.className='fh'; inp.after(fh); }
      fh.textContent = `✕ 本字段必填：${label.replace('（必填）','')}不能为空，留空将被提交检查拒绝。`;
    }
  }
  // capture the schedule from the LIVE form BEFORE any step change (the
  // check branch re-renders #cost-body and would otherwise find nothing)
  if(COST_FUNCTION==='cost_fixed'){
    document.querySelectorAll('#cost-body [data-f^="w"]').forEach(el=>{ COST_FORM[el.dataset.f]=el.value; });
  }
  COST_FORM = Object.assign({}, COST_FORM, collectForm());
  COST_STEP = 'check';
  renderCostPage();
}
async function submitCost(){
  const btn = document.getElementById('cost-confirm');
  btn.disabled = true; btn.textContent = '测算中…';
  try{
    const r = await jpost('/api/cost/submit', {project_ref: COST_PROJECT.project_dir, kind: COST_FUNCTION, form: COST_FORM});
    flash('测算完成，结果已登记为新的运行记录（只读，可在「结果与报告」找回）');
    RESULTS_PROJECT = COST_PROJECT.project_dir; RESULTS_RUN = r.run_ref;
    PAGE = 'results'; rerender();
  }catch(e){
    flash('提交被拒绝：' + e.message);
    btn.disabled = false; btn.textContent = '确认无误，开始测算';
  }
}

/* ============ 结果与报告（真实数据） ============ */
