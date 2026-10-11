function viewDemand(){
  return `
  <div class="ptitle">容量需求类参数</div>
  <div class="psub">回答「系统需要多少容量、供需系数取多少」。系统不会默认选择任何项目或功能。</div>
  <div class="entries" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
    <div class="entry"><h3>系统可靠性评估 <span class="tag lock">待验收</span></h3>
      <p>LOLE/LOLH/EUE/NEUE 四指标＋收敛状态＋风险事件。<br><b>入口已接线，完整运行与结果闭环待验收。</b></p>
      <div class="foot"><span class="mono-note">真实引擎</span><button class="btn pri" onclick="REL_FUNCTION='adequacy';REL_STEP='project';REL_STACK='accred';PAGE='accred';rerender()">进入测算流程</button></div></div>
    <div class="entry"><h3>目标可靠容量测算 <span class="tag lock">待验收</span></h3>
      <p>达到目标可靠性标准（目标 LOLE 或 EUE）系统至少需要多少可靠容量。站在已完成的充裕度评估之上做绝对目标搜索（Step 2 已接线）。</p>
      <details class="disc"><summary>查看字段设计（需求类设计§3）</summary><div class="body">输入：来源充裕度项目引用（已完成、同地区/同年份，血缘 project_ref+run_ref）、目标指标（LOLE/EUE 二选一）与目标值、目标年份、求解口径单选（调整完美容量＝MW 轴／调整负荷＝λ 轴，两口径并列不可互比）、搜索上下界（随口径切 MW 或 λ）、容差与最大迭代；输出：调整完美容量＝达标所需追加容量；调整负荷＝目标负荷水平（λ×峰荷）＋λ。未收敛只报区间＋诊断，不给点值。</div></details>
      <div class="foot"><span class="mono-note">需求类设计§3｜Step 2 已接线</span><button class="btn pri" onclick="TCAP.step='src';PAGE='tcap';rerender()">进入测算流程</button></div></div>
    <div class="entry"><h3>容量缺口与备用裕度 <span class="tag lock">待验收</span></h3>
      <p>相对目标还要补多少容量？可靠容量与铭牌容量两口径备用裕度分别显示（不混算互替）；分资源构成分解已开放（占比展示，非缺供责任分摊）。</p>
      <details class="disc"><summary>查看字段设计（需求类设计§4）</summary><div class="body">输入：目标可靠容量（引用已完成目标容量结果或显式输入）＋核定可靠容量＋预计退役＋已确定新增＋峰值负荷＋铭牌合计（可选）；输出：缺口/盈余、两口径备用裕度、退役/新增/目标−峰荷构成分解（测算事实并列，不作归因判断）。试算即时输出，不产生正式运行记录。</div></details>
      <div class="foot"><span class="mono-note">需求类设计§4｜Step 2 已接线（试算）</span><button class="btn pri" onclick="PAGE='gap';rerender()">进入试算</button></div></div>
    <div class="entry"><h3>容量供需系数 <span class="tag lock">待验收</span></h3>
      <p>公式 v1（Q-D3 六项裁决已收）：系数 = 目标可靠容量 ÷ 核定可靠容量，封顶 1.0、下限 0.0，年度口径。正式口径计算器：试算即出数，带公式版本与输入血缘。</p>
      <details class="disc"><summary>查看字段设计（需求类设计§5）</summary><div class="body">分子＝目标可靠容量（引用已完成目标容量结果或显式输入，MW ≥0）；分母＝核定可靠容量（引用 ELCC/EFC 认可容量结果或人工核定值，MW 必须 >0——分母为零诚实拒绝）；封顶 1.0 与下限 0.0 内置于公式 v1 并输出触发标记；年度口径（月度折算留 Q-S 系列）。本轮不持久化（R-C1）：算过的系数不落盘，刷新后凭引用一键复算；冻结为结算可引用正式结果留待结算模块实施。</div></details>
      <div class="foot"><span class="mono-note">需求类设计§5｜Step 2 已接线（正式口径 v1）</span><button class="btn pri" onclick="PAGE='coef';rerender()">进入试算</button></div></div>
  </div>`;
}
function loadDemand(){}

/* ============ Step2 A4：目标可靠容量向导（PAGE='tcap'，四步制镜像成本向导） ============
 * 来源选择（已完成已发布充裕度运行，服务端 /api/demand/target-capacity/sources）
 * → 新建/选择目标容量项目 → 参数（口径单选：调整完美容量／调整负荷——
 *   Q-D4 已裁决 2026-10-10，两口径并列不可互比）
 * → 服务端检查（preview 类型化校验）→ 确认发起 → 轮询 → 结果投影。
 * 就绪度只读原则不变：向导不代做充裕度测算，来源为空时如实提示。 */
const TCAP = { step:'src', project:null, source:null, form:{caliber:'adjust_perfect_capacity', metric:'lole_days_per_year', value:'', year:'2027', upper:'100', lower:'0', tol:'25', lowerL:'0.8', upperL:'1.1', tolL:'0.01', maxiter:'12'}, runRef:null, pollTimer:null };
function tcapConfig(){
  const f = TCAP.form, s = TCAP.source;
  const search = f.caliber==='adjust_load'
    ? { lower_bound_load_scale:Number(f.lowerL), upper_bound_load_scale:Number(f.upperL),
        load_scale_tolerance:Number(f.tolL), max_iterations:Number(f.maxiter) }
    : { lower_bound_mw:Number(f.lower), upper_bound_mw:Number(f.upper),
        capacity_tolerance_mw:Number(f.tol), max_iterations:Number(f.maxiter) };
  return {
    run_config: {
      run_config_schema_version:'1.0', run_mode:'teaching', run_type:'demand_target_capacity',
      method_profile:'target_capacity_v1', primary_risk_metric:f.metric,
      weather_year_weight_set_id:'w1', random_seed:(s.random_seed!=null?s.random_seed:7), scenario_bank_id:s.scenario_bank_id,
    },
    source: { project_ref:s.project_ref, run_ref:s.run_ref },
    target: { metric:f.metric, value:Number(f.value), target_year:Number(f.year) },
    search,
    caliber:f.caliber,
  };
}
function viewTcapWizard(){
  const steps = ['选择来源与项目','填写测算参数','服务端检查','确认测算'];
  const names = { src:0, form:1, check:2, confirm:3 };
  const cur = names[TCAP.step] ?? 0;
  return `
  <div class="ptitle">目标可靠容量测算</div>
  <div class="psub">站在已完成的充裕度评估之上：选定来源运行→设定目标风险（LOLE/EUE）→搜索达标所需容量或负荷水平。一个项目对应一次测算，类型创建后锁定。</div>
  <div class="hintbar">两种求解口径并列（结果语义不同，<b>不可互相比较</b>）：「调整完美容量」＝追加零停运容量补缺，结果＝达标所需追加容量（MW）；「调整负荷」＝供给体系全部冻结、原生负荷曲线按同一系数缩放，结果＝恰好满足目标时的负荷水平（MW）与缩放系数 λ。</div>
  <div class="steps">${steps.map((n,i)=>`<div class="st ${i<cur?'done':i===cur?'cur':''}">${'①②③④'[i]} ${n}</div>`).join('')}</div>
  <div id="tcap-body"></div>`;
}
function renderTcapPage(){
  const body = document.getElementById('tcap-body');
  if(!body) return;
  if(TCAP.step==='src'){
    body.innerHTML = `
    <div class="card"><h3>选择来源充裕度运行</h3>
      <div class="psub">仅列出已完成且已发布正式结果的充裕度评估运行（只读派生）。</div>
      <div id="tcap-sources" class="loading">读取中…</div></div>
    <div class="card"><h3>新建目标容量测算项目</h3>
      <div class="fgrid"><div class="fld" id="f-tcapname"><label><b>项目名称</b>（必填）</label><input id="tcap-name" placeholder="如：2027年目标可靠容量测算"></div></div>
      <button class="btn pri" onclick="tcapCreateProject()">新建项目（类型锁定为目标可靠容量）</button></div>`;
    tcapLoadSources();
    return;
  }
  if(TCAP.step==='form'){
    const f = TCAP.form;
    const isLoad = f.caliber==='adjust_load';
    body.innerHTML = `
    <div class="card"><h3>填写测算参数</h3>
      <div class="psub">来源：${esc(TCAP.source.project_name)}｜运行 ${esc(String(TCAP.source.run_ref).slice(0,8))}（${esc(TCAP.source.project_ref)}）</div>
      <div class="fgrid">
        <div class="fld"><label><b>目标指标</b>（二选一）</label>
          <select id="tcap-metric" onchange="TCAP.form.metric=this.value">
            <option value="lole_days_per_year" ${f.metric==='lole_days_per_year'?'selected':''}>目标 LOLE（天/年）</option>
            <option value="eue_mwh_per_year" ${f.metric==='eue_mwh_per_year'?'selected':''}>目标 EUE（MWh/年）</option>
          </select></div>
        <div class="fld"><label><b>目标值</b>（必填，>0）</label><input class="num" id="tcap-value" value="${esc(f.value)}"></div>
        <div class="fld"><label><b>目标年份</b></label><input class="num" id="tcap-year" value="${esc(f.year)}"></div>
        <div class="fld"><label><b>求解口径</b>（强制显式，无默认）</label>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <label style="display:flex;gap:4px;align-items:center"><input type="radio" name="tcap-caliber" ${!isLoad?'checked':''} onchange="TCAP.form.caliber='adjust_perfect_capacity';rerender()"> 调整完美容量</label>
            <label style="display:flex;gap:4px;align-items:center"><input type="radio" name="tcap-caliber" ${isLoad?'checked':''} onchange="TCAP.form.caliber='adjust_load';rerender()"> 调整负荷</label>
            <span class="tag lock">两口径并列不可互比</span>
          </div></div>
        ${isLoad
          ? `<div class="fld"><label>搜索下界（<b>负荷缩放系数</b> λ，>0）</label><input class="num" id="tcap-low-lambda" value="${esc(f.lowerL)}"></div>
             <div class="fld"><label>搜索上界（λ）</label><input class="num" id="tcap-up-lambda" value="${esc(f.upperL)}"></div>
             <div class="fld"><label>容差（λ，>0）</label><input class="num" id="tcap-tol-lambda" value="${esc(f.tolL)}"></div>`
          : `<div class="fld"><label>搜索下界（MW，≥0）</label><input class="num" id="tcap-lower" value="${esc(f.lower)}"></div>
             <div class="fld"><label>搜索上界（MW）</label><input class="num" id="tcap-upper" value="${esc(f.upper)}"></div>
             <div class="fld"><label>容差（MW，>0）</label><input class="num" id="tcap-tol" value="${esc(f.tol)}"></div>`}
        <div class="fld"><label>最大迭代（≥1）</label><input class="num" id="tcap-maxiter" value="${esc(f.maxiter)}"></div>
      </div>
      ${isLoad?'<div class="mono-note" style="margin-top:6px">调整负荷口径：供给体系（电源/储能/联络线）全部冻结，全部小时负荷按同一 λ 保形缩放；结果＝目标负荷水平（λ×来源峰荷）＋λ。联络线按比例增长等属灵敏度分析，不在本口径。</div>':''}
      <div class="wizbar">
        <button class="btn" onclick="TCAP.step='src';rerender()">← 上一步</button>
        <button class="btn pri" onclick="tcapToCheck()">下一步：服务端检查 →</button>
      </div></div>`;
    return;
  }
  if(TCAP.step==='check'){
    body.innerHTML = `<div class="card"><h3>服务端检查：目标可靠容量</h3>
      <div id="tcap-check" class="loading">服务端检查中…</div>
      <div class="wizbar">
        <button class="btn" onclick="TCAP.step='form';rerender()">← 上一步，返回填写</button>
        <button class="btn pri" id="tcap-toconfirm" disabled>检查通过后可确认</button>
      </div></div>`;
    tcapRunCheck();
    return;
  }
  if(TCAP.step==='confirm'){
    const f = TCAP.form;
    const isLoad = f.caliber==='adjust_load';
    const boundsSummary = isLoad
      ? `搜索 [${esc(f.lowerL)}, ${esc(f.upperL)}] λ（负荷缩放系数）｜容差 ${esc(f.tolL)} λ`
      : `搜索 [${esc(f.lower)}, ${esc(f.upper)}] MW｜容差 ${esc(f.tol)} MW`;
    body.innerHTML = `<div class="card"><h3>确认测算：目标可靠容量</h3>
      <div class="psub">项目：${esc(TCAP.project.name)}｜来源：${esc(TCAP.source.project_name)} / ${esc(String(TCAP.source.run_ref).slice(0,8))}｜口径：${isLoad?'调整负荷（结果＝目标负荷水平＋λ）':'调整完美容量（结果＝达标所需追加容量）'}</div>
      <div class="hintbar">确认即提交真实搜索（教学数据数秒—分钟级）。未收敛属诚实终态：只报区间与诊断，不给点值；完成后项目绑定该次正式结果。</div>
      <details class="disc" open><summary>参数摘要（只读，供最终核对）</summary><div class="body">
        目标 ${esc(f.metric==='lole_days_per_year'?'LOLE':'EUE')}=${esc(f.value)}｜目标年份 ${esc(f.year)}｜${boundsSummary}｜最大迭代 ${esc(f.maxiter)}</div></details>
      <div class="wizbar">
        <button class="btn" onclick="TCAP.step='check';rerender()">← 上一步，返回检查</button>
        <button class="btn pri" id="tcap-confirm" onclick="tcapStart()">确认无误，开始目标可靠容量测算</button>
      </div></div>
    <div id="tcap-result"></div>`;
    return;
  }
}
async function tcapLoadSources(){
  const el = document.getElementById('tcap-sources');
  try{
    const d = await jget('/api/demand/target-capacity/sources');
    const rows = d.sources||[];
    el.innerHTML = rows.length ? rows.map(s=>`
      <div class="rowitem"><div class="tt">${esc(s.project_name)}｜充裕度运行 ${esc(String(s.run_ref).slice(0,8))}<small>${esc(s.project_ref)}｜${s.import_batch_id?'批次可引用':'批次不可读（该来源暂不可发起）'}</small></div>
        <button class="btn ${s.import_batch_id?'pri':''}" ${s.import_batch_id?'':'disabled style="opacity:.45;cursor:not-allowed"'} onclick='tcapPickSource(${JSON.stringify(s).replace(/'/g,"&#39;")})'>选此来源</button></div>`).join('')
      : '<span class="loading">暂无已完成且已发布的充裕度运行。请先在「系统可靠性评估」完成一次测算，这里会如实列出。</span>';
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
function tcapPickSource(s){ TCAP.source=s; TCAP.step = TCAP.project?'form':'form'; rerender(); }
async function tcapCreateProject(){
  const input = document.getElementById('tcap-name');
  const fld = document.getElementById('f-tcapname');
  const name = (input?input.value:'').trim();
  if(!name){ fld.classList.add('err'); flash('项目名称不能为空'); return; }
  fld.classList.remove('err');
  try{
    const r = await jpost('/api/projects', {name, description:'', project_kind:'reliability', measurement_type:'demand_target_capacity'});
    flash('项目创建成功（类型已锁定）：'+r.name);
    TCAP.project = {project_dir:r.project_ref, name:r.name};
    if(TCAP.source) TCAP.step='form';
    rerender();
  }catch(e){ fld.classList.add('err'); flash('创建被拒绝：'+e.message); }
}
function tcapCollectForm(){
  const f = TCAP.form;
  const fields = f.caliber==='adjust_load'
    ? [['value','#tcap-value'],['year','#tcap-year'],['lowerL','#tcap-low-lambda'],['upperL','#tcap-up-lambda'],['tolL','#tcap-tol-lambda'],['maxiter','#tcap-maxiter']]
    : [['value','#tcap-value'],['year','#tcap-year'],['lower','#tcap-lower'],['upper','#tcap-upper'],['tol','#tcap-tol'],['maxiter','#tcap-maxiter']];
  fields.forEach(([k,sel])=>{
    const el = document.querySelector(sel);
    if(el) f[k] = el.value;
  });
  const m = document.getElementById('tcap-metric');
  if(m) f.metric = m.value;
}
function tcapToCheck(){
  /* 表单→检查步进：先收表单再进检查步（检查步渲染时自动跑服务端校验） */
  tcapCollectForm();
  TCAP.step = 'check';
  rerender();
}
async function tcapRunCheck(){
  const el = document.getElementById('tcap-check');
  tcapCollectForm();
  try{
    const r = await jpost('/api/runs/preview', {method:'demand_target_capacity', project_ref:TCAP.project.project_dir, form:tcapConfig()});
    TCAP._checked = r;
    const s = r.summary||{};
    const metricName = s.target_metric==='lole_days_per_year'?'LOLE（天/年）':'EUE（MWh/年）';
    const caliberLabel = s.caliber==='adjust_load'?'调整负荷':'调整完美容量';
    const boundsLabel = s.caliber==='adjust_load'
      ? `搜索 [${s.search_bounds_load_scale&&s.search_bounds_load_scale[0]}, ${s.search_bounds_load_scale&&s.search_bounds_load_scale[1]}] λ（负荷缩放系数）`
      : `搜索 ${s.search_bounds_mw} MW`;
    el.innerHTML = `<div class="hintbar"><b>配置校验通过，可以进入确认。</b><br>
      <span class="mono-note">目标 ${metricName}=${s.target_value}｜目标年份 ${s.target_year}｜口径 ${caliberLabel}｜${boundsLabel}</span></div>`;
    const btn = document.getElementById('tcap-toconfirm');
    btn.disabled = false; btn.textContent='下一步：确认测算 →';
    btn.onclick = ()=>{ TCAP.step='confirm'; rerender(); };
  }catch(e){ el.innerHTML = `<div class="errbar">检查未通过：${esc(e.message)}</div>`; }
}
async function tcapStart(){
  const btn = document.getElementById('tcap-confirm');
  btn.disabled = true; btn.textContent = '搜索中…';
  try{
    const r = await jpost('/api/runs/start', {project_ref:TCAP.project.project_dir, data_ref:TCAP.source.import_batch_id, method:'demand_target_capacity', config:tcapConfig()});
    tcapPoll(r.request_ref);
  }catch(e){
    flash('发起被拒绝：'+e.message);
    btn.disabled = false; btn.textContent = '确认无误，开始目标可靠容量测算';
  }
}
function tcapPoll(ref){
  const box = document.getElementById('tcap-result');
  if(!box) return;
  fetch('/api/runs/status?ref='+encodeURIComponent(ref)+'&project='+encodeURIComponent(TCAP.project.project_dir))
    .then(r=>r.json())
    .then(async s=>{
      if(s.status==='execution_finished'){
        TCAP.runRef = s.run_ref;
        await tcapRenderResult();
      }else if(s.status==='execution_failed'){
        box.innerHTML = `<div class="errbar">运行失败：${esc(s.execution_error||'详见运行记录')}</div>`;
      }else{
        box.innerHTML = `<div class="loading">搜索中（${esc(s.phase||s.status)}）…</div>`;
        TCAP.pollTimer = setTimeout(()=>tcapPoll(ref), 1500);
      }
    })
    .catch(()=>{ TCAP.pollTimer = setTimeout(()=>tcapPoll(ref), 2500); });
}
async function tcapRenderResult(){
  const box = document.getElementById('tcap-result');
  try{
    const d = await jget('/api/runs/projection?project='+encodeURIComponent(TCAP.project.project_dir)+'&run='+encodeURIComponent(TCAP.runRef));
    const p = d.projection||{}, cv = p.capacity_view||{}, cm = p.common_view||{};
    const metricName = cv && cm.primary_risk_metric==='lole_days_per_year'?'LOLE':'EUE';
    const isLoad = cv.solver_caliber==='adjust_load';
    const point = cv.target_capacity_mw;
    const heroCard = isLoad
      ? `<div class="bstat hero"><div class="k">目标负荷水平（恰好满足目标）</div><div class="v">${fmt(point,2)}<span class="u">MW</span></div><div class="u">口径：调整负荷｜负荷缩放系数 λ=${fmt(cv.load_scale_factor,4)}（＝该水平÷来源峰荷）</div></div>`
      : `<div class="bstat hero"><div class="k">目标可靠容量（达标所需追加）</div><div class="v">${fmt(point,2)}<span class="u">MW</span></div><div class="u">口径：调整完美容量｜保守取区间上端</div></div>`;
    const axisCol = isLoad ? '负荷水平（MW）' : '容量（MW）';
    const rowLabels = isLoad ? ['下端（已达标）','上端（未达标）'] : ['下端（未达标）','上端（已达标）'];
    const html = point!=null
      ? `<div class="bigstat">${heroCard}
         <div class="bstat"><div class="k">现状风险（${metricName}）</div><div class="v">${fmt(cv.current_risk,4)}</div></div>
         <div class="bstat"><div class="k">目标风险</div><div class="v">${fmt(cv.target_risk_value,4)}</div></div></div>
         <div class="card"><h3>搜索区间</h3><table class="grid"><tr><th></th><th class="r">${axisCol}</th><th class="r">${metricName} 风险</th></tr>
           <tr><td>${rowLabels[0]}</td><td class="r">${fmt(cv.bracket_low_mw,2)}</td><td class="r">${fmt(cv.bracket_low_risk,4)}</td></tr>
           <tr><td><b>${rowLabels[1]}</b></td><td class="r"><b>${fmt(cv.bracket_high_mw,2)}</b></td><td class="r">${fmt(cv.bracket_high_risk,4)}</td></tr></table>
         <div class="mono-note">来源：${esc(cv.source_project_ref)} / ${esc(String(cv.source_run_ref||'').slice(0,8))}｜搜索收敛：${esc(cm.search_convergence||'')}${isLoad?'｜该口径结果是负荷水平，不可与「调整完美容量」口径结果互相比较，也不可作为缺口/供需系数的目标容量引用':''}</div></div>`
      : `<div class="hintbar warn"><b>搜索未收敛（诚实终态）</b>：不给点值。区间 [${fmt(cv.bracket_low_mw,2)}, ${fmt(cv.bracket_high_mw,2)}] ${isLoad?'MW（负荷水平）':'MW'}；${(cm.diagnostics||[]).map(d=>esc(d.code)).join('｜')}</div>
         <div class="mono-note">请调整搜索界或放宽容差后新建项目重试。</div>`;
    box.innerHTML = `<div class="lockbar"><b>测算完成。</b>运行 ${esc(String(TCAP.runRef).slice(0,8))}｜结果 ${esc(String(p.result_id||'').slice(0,8))}｜状态 ${esc(p.result_status||'')}</div>` + html;
    flash('目标可靠容量测算完成，结果可在「结果与报告」找回');
  }catch(e){ box.innerHTML = `<div class="errbar">结果读取失败：${esc(e.message)}</div>`; }
}

/* ============ Step2 B：容量缺口与备用裕度（PAGE='gap'，确定性算术试算） ============ */
const GAP = { targetMode:'manual', refProject:'', refRun:'', target:'', accredited:'', retire:'0', additions:'0', peak:'', nameplate:'', year:'2027', accreditedNote:'', rcCoal:'', rcGas:'', rcHydro:'', rcStorage:'', rcSolar:'', rcWind:'', rcIntertie:'', rcOther:'', result:null };
function viewGapCalc(){
  return `
  <div class="ptitle">容量缺口与备用裕度</div>
  <div class="psub">确定性算术试算（需求类设计 §4）：不产生正式运行记录；两口径备用裕度分别显示，不混算互替；分资源构成＝构成分解（Q-D2 裁决 2026-10-10：占比展示，非缺供责任分摊）。</div>
  <div class="hintbar">目标可靠容量可显式输入，或引用一条已完成（已收敛）的目标可靠容量结果——引用经服务端身份环核验，未收敛结果不可引用。分资源构成取各资源 ELCC/EFC 正式结果点值，未核定类型留空不填报。</div>
  <div class="card"><h3>输入</h3>
    <div class="fgrid">
      <div class="fld"><label><b>目标可靠容量来源</b></label>
        <select onchange="GAP.targetMode=this.value;rerender()">
          <option value="manual" ${GAP.targetMode==='manual'?'selected':''}>显式输入（MW）</option>
          <option value="ref" ${GAP.targetMode==='ref'?'selected':''}>引用目标容量结果（项目＋运行）</option>
        </select></div>
      ${GAP.targetMode==='manual'
        ? `<div class="fld"><label><b>目标可靠容量</b>（MW，≥0）</label><input class="num" id="gap-target" value="${esc(GAP.target)}"></div>`
        : `<div class="fld"><label><b>项目目录名</b>（project_ref）</label><input id="gap-refproj" value="${esc(GAP.refProject)}" placeholder="如 8f21c0…"></div>
           <div class="fld"><label><b>运行标识</b>（run_ref）</label><input id="gap-refrun" value="${esc(GAP.refRun)}"></div>`}
      <div class="fld"><label><b>现有核定可靠容量</b>（MW，≥0）</label><input class="num" id="gap-accredited" value="${esc(GAP.accredited)}"><div class="fh">来源说明：<input id="gap-accrednote" value="${esc(GAP.accreditedNote)}" style="width:100%"></div></div>
      <div class="fld"><label>预计退役（MW，≥0）</label><input class="num" id="gap-retire" value="${esc(GAP.retire)}"></div>
      <div class="fld"><label>已确定新增（MW，≥0）</label><input class="num" id="gap-additions" value="${esc(GAP.additions)}"></div>
      <div class="fld"><label><b>峰值负荷</b>（MW，>0）</label><input class="num" id="gap-peak" value="${esc(GAP.peak)}"></div>
      <div class="fld"><label>铭牌容量合计（MW，可选——不填则不出该口径）</label><input class="num" id="gap-nameplate" value="${esc(GAP.nameplate)}"></div>
      <div class="fld"><label>目标年度</label><input class="num" id="gap-year" value="${esc(GAP.year)}"></div>
    </div>
    <details class="disc" style="margin-top:8px"><summary>分资源构成（可选——填报后展示构成分解，非缺供责任分摊）</summary><div class="body">
      <div class="mono-note" style="margin-bottom:6px">各类型填「该资源 ELCC/EFC 正式结果的核定可靠容量 MW」；未核定类型留空（不拿铭牌凑数）。构成合计与上面「现有核定可靠容量」不一致时会如实披露差额。</div>
      <div class="fgrid">
        <div class="fld"><label>coal 火电（MW）</label><input class="num" id="gap-rc-coal" value="${esc(GAP.rcCoal)}"></div>
        <div class="fld"><label>gas 燃气（MW）</label><input class="num" id="gap-rc-gas" value="${esc(GAP.rcGas)}"></div>
        <div class="fld"><label>hydro 水电（MW）</label><input class="num" id="gap-rc-hydro" value="${esc(GAP.rcHydro)}"></div>
        <div class="fld"><label>storage 储能（MW）</label><input class="num" id="gap-rc-storage" value="${esc(GAP.rcStorage)}"></div>
        <div class="fld"><label>solar 光伏（MW）</label><input class="num" id="gap-rc-solar" value="${esc(GAP.rcSolar)}"></div>
        <div class="fld"><label>wind 风电（MW）</label><input class="num" id="gap-rc-wind" value="${esc(GAP.rcWind)}"></div>
        <div class="fld"><label>intertie 联络线（MW）</label><input class="num" id="gap-rc-intertie" value="${esc(GAP.rcIntertie)}"></div>
        <div class="fld"><label>other 其他（MW）</label><input class="num" id="gap-rc-other" value="${esc(GAP.rcOther)}"></div>
      </div>
    </div></details>
    <button class="btn pri" onclick="gapCompute()">试算（即时输出，不产生运行记录）</button>
    <div style="margin-top:10px"><button class="btn" onclick="PAGE='demand';rerender()">← 返回需求类参数</button></div>
  </div>
  <div id="gap-result"></div>`;
}
async function gapCompute(){
  const box = document.getElementById('gap-result');
  const val = (id)=>{ const el=document.getElementById(id); return el?el.value:''; };
  GAP.target=val('gap-target'); GAP.accredited=val('gap-accredited'); GAP.retire=val('gap-retire');
  GAP.additions=val('gap-additions'); GAP.peak=val('gap-peak'); GAP.nameplate=val('gap-nameplate');
  GAP.year=val('gap-year'); GAP.accreditedNote=val('gap-accrednote');
  GAP.refProject=val('gap-refproj'); GAP.refRun=val('gap-refrun');
  ['coal','gas','hydro','storage','solar','wind','intertie','other'].forEach(t=>{
    const key = 'rc'+t[0].toUpperCase()+t.slice(1);
    GAP[key]=val('gap-rc-'+t);
  });
  const payload = {
    accredited_capacity_mw:Number(GAP.accredited), retirement_mw:Number(GAP.retire),
    additions_mw:Number(GAP.additions), peak_load_mw:Number(GAP.peak),
    target_year:Number(GAP.year), accredited_source_note:GAP.accreditedNote,
  };
  if(GAP.targetMode==='manual') payload.target_capacity_mw = Number(GAP.target);
  else payload.target_reference = {project_ref:GAP.refProject, run_ref:GAP.refRun};
  if(GAP.nameplate!=='') payload.nameplate_capacity_mw = Number(GAP.nameplate);
  const rc = ['coal','gas','hydro','storage','solar','wind','intertie','other']
    .map(t=>({t, raw:val('gap-rc-'+t)}))
    .filter(x=>x.raw.trim()!=='')
    .map(x=>({resource_type:x.t, accredited_mw:Number(x.raw)}));
  if(rc.length) payload.resource_composition = rc;
  try{
    const r = await jpost('/api/demand/capacity-gap/preview', payload);
    GAP.result = r;
    const c = r.composition||{};
    const rcRes = r.resource_contribution;
    box.innerHTML = `
    <div class="bigstat">
      <div class="bstat ${r.gap_direction==='缺口'?'':(r.gap_direction==='盈余'?'hero':'')}"><div class="k">${r.gap_direction==='持平'?'缺口/盈余':'容量'+r.gap_direction}</div><div class="v">${fmt(Math.abs(r.gap_mw),2)}<span class="u">MW</span></div><div class="u">目标 −（核定 − 退役 ＋ 新增）</div></div>
      <div class="bstat"><div class="k">有效可靠容量</div><div class="v">${fmt(r.effective_reliable_capacity_mw,2)}<span class="u">MW</span></div><div class="u">核定 − 退役 ＋ 新增</div></div>
      <div class="bstat"><div class="k">可靠容量口径备用裕度</div><div class="v">${(r.reliable_capacity_margin*100).toFixed(2)}<span class="u">%</span></div><div class="u">（有效可靠容量 − 峰荷）÷ 峰荷</div></div>
      ${r.nameplate_margin!=null?`<div class="bstat"><div class="k">铭牌口径备用裕度</div><div class="v">${(r.nameplate_margin*100).toFixed(2)}<span class="u">%</span></div><div class="u">（铭牌 − 峰荷）÷ 峰荷（两口径并列，不互替）</div></div>`:`<div class="bstat"><div class="k">铭牌口径备用裕度</div><div class="v" style="font-size:14px;color:var(--faint)">未提供铭牌合计（不补零）</div></div>`}
    </div>
    <div class="card"><h3>构成分解（测算事实并列，不作归因判断）</h3>
      <table class="grid"><tr><th>构成项</th><th class="r">数值（MW）</th></tr>
        <tr><td>预计退役</td><td class="r">${fmt(c.retirement_mw,2)}</td></tr>
        <tr><td>已确定新增</td><td class="r">${fmt(c.additions_mw,2)}</td></tr>
        <tr><td>目标可靠容量 − 峰值负荷</td><td class="r">${fmt(c.target_minus_peak_mw,2)}</td></tr></table>
      <div class="mono-note">试算不产生正式运行记录。</div></div>
    ${rcRes?`<div class="card"><h3>分资源构成分解 <span class="tag lock">非缺供责任分摊</span></h3>
      <table class="grid"><tr><th>资源类型</th><th class="r">核定可靠容量（MW）</th><th class="r">占比</th></tr>
        ${rcRes.rows.map(x=>`<tr><td>${esc(x.resource_type)}</td><td class="r num">${fmt(x.accredited_mw,2)}</td><td class="r num">${x.share==null?'—':(x.share*100).toFixed(2)+'%'}</td></tr>`).join('')}
        <tr><td><b>构成合计</b></td><td class="r num"><b>${fmt(rcRes.total_mw,2)}</b></td><td class="r">—</td></tr></table>
      ${rcRes.mismatch_disclosed?`<div class="errbar">构成合计与「现有核定可靠容量」相差 ${fmt(Math.abs(rcRes.mismatch_mw),2)} MW（如实披露，请核对构成填报——差额不自动摊到任何类型）。</div>`:''}
      <div class="mono-note">构成分解，非缺供责任分摊；数值应取各资源 ELCC/EFC 正式结果点值，未核定类型不填报（不拿铭牌凑数）。</div></div>`:''}`;
  }catch(e){
    box.innerHTML = `<div class="errbar">试算被拒绝：${esc(e.message)}</div>`;
  }
}

/* ============ Step2 C：容量供需系数（PAGE='coef'，正式口径计算器 v1） ============
 * 公式 v1（Q-D3 六项裁决 2026-10-07 冻结）：系数 = clamp(目标可靠容量 ÷ 核定可靠容量, 0.0, 1.0)，年度口径。
 * 分子/分母各自「引用/显式」二选一：引用经服务端身份环核验（未收敛/方法不符诚实拒绝）。
 * Step 3 S0（Q-S6 裁决②）：试算（零副作用）＋冻结为正式结果（显式确认动作）两段并存；
 * 冻结后项目完成、系数可进结算；同年多系数人工选定（系统只读列出，不代选）。 */
const COEF = { numMode:'manual', denMode:'manual', numRefProj:'', numRefRun:'', denRefProj:'', denRefRun:'', target:'', accredited:'', year:'2027', numNote:'', accredNote:'', result:null, freezeName:'', confirmer:'' };
function viewCoefCalc(){
  return `
  <div class="ptitle">容量供需系数</div>
  <div class="psub">正式口径计算器（需求类设计 §5，公式 v1）：系数 = 目标可靠容量 ÷ 核定可靠容量，封顶 1.0、下限 0.0，年度口径。试算即出数不产生运行记录；冻结为正式结果后进入结算引用池（Step 3 S0）。</div>
  <div class="hintbar">分子引用＝已完成（已收敛）的目标可靠容量结果；分母引用＝ELCC/EFC 认可容量结果——引用经服务端身份环核验，未收敛或方法不符诚实拒绝。分母必须 &gt;0（为零诚实拒绝）。</div>
  <div class="card"><h3>输入</h3>
    <div class="fgrid">
      <div class="fld"><label><b>分子：目标可靠容量来源</b></label>
        <select onchange="COEF.numMode=this.value;rerender()">
          <option value="manual" ${COEF.numMode==='manual'?'selected':''}>显式输入（MW，≥0）</option>
          <option value="ref" ${COEF.numMode==='ref'?'selected':''}>引用目标可靠容量结果（项目＋运行）</option>
        </select></div>
      ${COEF.numMode==='manual'
        ? `<div class="fld"><label><b>目标可靠容量</b>（MW）</label><input class="num" id="coef-target" value="${esc(COEF.target)}"><div class="fh">来源说明：<input id="coef-numnote" value="${esc(COEF.numNote)}" style="width:100%"></div></div>`
        : `<div class="fld"><label><b>分子项目目录名</b>（project_ref）</label><input id="coef-numproj" value="${esc(COEF.numRefProj)}"></div>
           <div class="fld"><label><b>分子运行标识</b>（run_ref）</label><input id="coef-numrun" value="${esc(COEF.numRefRun)}"></div>`}
      <div class="fld"><label><b>分母：核定可靠容量来源</b></label>
        <select onchange="COEF.denMode=this.value;rerender()">
          <option value="manual" ${COEF.denMode==='manual'?'selected':''}>显式输入（MW，必须 >0）</option>
          <option value="ref" ${COEF.denMode==='ref'?'selected':''}>引用 ELCC/EFC 认可容量结果（项目＋运行）</option>
        </select></div>
      ${COEF.denMode==='manual'
        ? `<div class="fld"><label><b>核定可靠容量</b>（MW，>0）</label><input class="num" id="coef-accredited" value="${esc(COEF.accredited)}"><div class="fh">来源说明：<input id="coef-accrednote" value="${esc(COEF.accredNote)}" style="width:100%"></div></div>`
        : `<div class="fld"><label><b>分母项目目录名</b>（project_ref）</label><input id="coef-denproj" value="${esc(COEF.denRefProj)}"></div>
           <div class="fld"><label><b>分母运行标识</b>（run_ref）</label><input id="coef-denrun" value="${esc(COEF.denRefRun)}"></div>`}
      <div class="fld"><label>目标年度</label><input class="num" id="coef-year" value="${esc(COEF.year)}"></div>
    </div>
    <button class="btn pri" onclick="coefCompute()">试算（公式 v1，即时输出）</button>
    <div style="margin-top:10px"><button class="btn" onclick="PAGE='demand';rerender()">← 返回需求类参数</button></div>
  </div>
  <div id="coef-result"></div>
  <div class="card"><h3>冻结为正式结果 <span class="tag lock">显式确认动作</span></h3>
    <div class="psub">冻结＝把当前表单输入的计算结果固化为<b>可引用正式结果</b>：不可变、带 formula_version 与输入血缘；结算模块只认冻结系数（Q-S6 裁决②）。一个项目一个正式结果——冻结即新建系数项目并完成，不可再改；重新测算请换新名称再冻结。</div>
    <div class="fgrid">
      <div class="fld"><label><b>系数项目名称</b>（必填；冻结即新建，类型锁定为供需系数）</label><input id="coef-freeze-name" value="${esc(COEF.freezeName)}" placeholder="如：2027年容量供需系数"></div>
      <div class="fld"><label><b>确认人</b>（必填，留痕追溯）</label><input id="coef-confirmer" value="${esc(COEF.confirmer)}" placeholder="姓名/工号"></div>
    </div>
    <button class="btn pri" onclick="coefFreeze()">确认并冻结为正式结果</button>
  </div>
  <div class="card"><h3>同年已有系数（只读比对，不代选）</h3><div id="coefSameYear" class="loading">读取中…</div></div>`;
}
function coefCollectPayload(){
  const val = (id)=>{ const el=document.getElementById(id); return el?el.value:''; };
  COEF.target=val('coef-target'); COEF.accredited=val('coef-accredited'); COEF.year=val('coef-year');
  COEF.numNote=val('coef-numnote'); COEF.accredNote=val('coef-accrednote');
  COEF.numRefProj=val('coef-numproj'); COEF.numRefRun=val('coef-numrun');
  COEF.denRefProj=val('coef-denproj'); COEF.denRefRun=val('coef-denrun');
  const payload = { target_year:Number(COEF.year) };
  if(COEF.numMode==='manual'){ payload.target_capacity_mw = Number(COEF.target); payload.numerator_source_note = COEF.numNote; }
  else payload.numerator_reference = {project_ref:COEF.numRefProj, run_ref:COEF.numRefRun};
  if(COEF.denMode==='manual'){ payload.accredited_capacity_mw = Number(COEF.accredited); payload.accredited_source_note = COEF.accredNote; }
  else payload.denominator_reference = {project_ref:COEF.denRefProj, run_ref:COEF.denRefRun};
  return payload;
}
async function coefFreeze(){
  const nameEl = document.getElementById('coef-freeze-name');
  const confEl = document.getElementById('coef-confirmer');
  COEF.freezeName = nameEl ? nameEl.value.trim() : '';
  COEF.confirmer = confEl ? confEl.value.trim() : '';
  if(!COEF.freezeName){ flash('请填写系数项目名称'); return; }
  if(!COEF.confirmer){ flash('冻结是显式确认动作：请填写确认人'); return; }
  const payload = coefCollectPayload();
  try{
    const created = await jpost('/api/projects', {name:COEF.freezeName, description:'', project_kind:'reliability', measurement_type:'demand_coefficient'});
    const r = await jpost('/api/demand/coefficient/freeze', {...payload, project_ref:created.project_ref, confirmer:COEF.confirmer});
    flash(`已冻结为正式结果：系数 ${r.coefficient}（公式 v1），项目「${COEF.freezeName}」已完成，结算模块可引用`);
    coefLoadSameYear();
  }catch(e){ flash('冻结被拒绝：'+e.message); }
}
async function coefLoadSameYear(){
  const el = document.getElementById('coefSameYear');
  if(!el) return;
  try{
    const d = await jget('/api/demand/coefficient/list?year='+encodeURIComponent(COEF.year||'2027'));
    const rows = (d.rows||[]);
    el.innerHTML = rows.length ? rows.map(r=> r.invalid
      ? `<div class="rowitem"><div class="tt">⚠ 记录损坏<small>${esc(r.invalid_reason||'')}</small></div></div>`
      : `<div class="rowitem"><div class="tt"><b class="num">${fmt(r.coefficient,4)}</b>｜${esc(r.project_name)}<small>冻结 ${esc(String(r.frozen_at||'').slice(0,10))}｜确认人 ${esc(r.confirmer||'')}｜formula_version=${esc(r.formula_version||'')}</small></div></div>`
    ).join('') : '<span class="loading">该年度暂无已冻结系数；冻结后在上方列表与「结果与报告」可见。</span>';
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`;
  }
}
async function coefCompute(){
  const box = document.getElementById('coef-result');
  const payload = coefCollectPayload();
  try{
    const r = await jpost('/api/demand/coefficient/preview', payload);
    COEF.result = r;
    const lin = r.lineage||{};
    box.innerHTML = `
    <div class="bigstat">
      <div class="bstat hero"><div class="k">供需系数（公式 v1·年度口径）</div><div class="v">${fmt(r.coefficient,4)}</div><div class="u">${r.cap_triggered?'封顶 1.0 已触发（原始比值 '+fmt(r.raw_ratio,4)+'）':'clamp(分子÷分母, 0.0, 1.0)'}${r.floor_triggered?'｜下限 0.0 已触发':''}</div></div>
      <div class="bstat"><div class="k">目标可靠容量（分子）</div><div class="v">${fmt(r.inputs.target_capacity_mw,2)}<span class="u">MW</span></div><div class="u">${lin.numerator_reference?'引用 '+esc(String(lin.numerator_reference.run_ref||'').slice(0,8)):esc(r.declared_inputs.numerator_source_note||'显式输入')}</div></div>
      <div class="bstat"><div class="k">核定可靠容量（分母）</div><div class="v">${fmt(r.inputs.accredited_capacity_mw,2)}<span class="u">MW</span></div><div class="u">${lin.denominator_reference?'引用 '+esc(String(lin.denominator_reference.run_ref||'').slice(0,8)):esc(r.declared_inputs.accredited_source_note||'显式输入')}</div></div>
    </div>
    <div class="card"><h3>公式与血缘</h3>
      <div class="mono-note" style="font-size:12px;line-height:1.9">公式 v1：系数 = clamp(目标可靠容量 ÷ 核定可靠容量, 0.0, 1.0)｜口径＝${esc(r.caliber)}｜formula_version=${esc(r.formula_version)}<br>
      原始比值 ${fmt(r.raw_ratio,6)}｜封顶触发 ${r.cap_triggered?'是':'否'}｜下限触发 ${r.floor_triggered?'是':'否'}｜目标年度 ${esc(r.declared_inputs.target_year)}</div>
      <div class="mono-note" style="margin-top:8px">试算不落盘；冻结为正式结果见下方冻结卡（冻结后可在结算引用，血缘随系数追溯）。</div></div>`;
    coefLoadSameYear();
  }catch(e){
    box.innerHTML = `<div class="errbar">试算被拒绝：${esc(e.message)}</div>`;
  }
}

/* ============ Step1 T6：容量市场仿真落地页（06，市场模式切换落点） ============
 * 七步与履约考核从侧栏收进本页页内标签；tab1-7 内容逐字节平移自原 viewMkt1-7
 * （仅摘除各页独立 ptitle，共享本页页头），tab8＝履约考核（viewDemoPerform 原样）。 */
let MKTTAB = 0; // 0=总览，1-7=七步，8=履约考核
const MKTTABS = ['总览','① 市场情景','② 容量需求曲线','③ 供给资源与报价','④ 出清设置','⑤ 出清过程','⑥ 市场力与合规','⑦ 结果与情景比较','履约考核'];
const MKTSTATUS = ['','已接通','已接通','已接通','已接通','已接通','已接通','已接通','演示'];
function mktTabsBar(){
  return `<div class="otabs" style="flex-wrap:wrap">${MKTTABS.map((t,i)=>`<button class="${i===MKTTAB?'on':''}" onclick="MKTTAB=${i};rerender()">${esc(t)}${i>0?` <span class="tag ${MKTSTATUS[i]==='演示'?'lock':'ok'}" style="font-size:9.5px">${MKTSTATUS[i]}</span>`:''}</button>`).join('')}</div>`;
}
function viewMarket(){
  const head = `
  <div class="ptitle">容量市场仿真</div>
  <div class="psub">需求曲线 × 供给报价 → 出清过程的情景仿真与结果比较（Q-M1—M6 裁决 2026-10-11；统一出清价、单区；市场力披露不阻断）。</div>
  <div class="hintbar">正式出清内核已接线：②—⑦ 为真实计算（VRR 曲线引用目标容量与 Net CONE；报价申报不得超过核定）。履约考核标签内仍为演示口径。</div>
  ${mktTabsBar()}`;
  if(MKTTAB===0){
    return head + `
    <div class="card"><h3>依赖就绪度</h3><div id="market-ready" class="loading">就绪度读取中…</div>
      <div class="mono-note">就绪度为动态只读（从项目注册表与结果索引派生）；缺项给跳转，不代做、不伪造就绪。</div></div>
    <div class="card"><h3>市场仿真项目</h3>
      <div id="mkt-projects" class="loading">项目读取中…</div>
      <div class="fgrid" style="margin-top:8px"><div class="fld"><label><b>新建市场仿真项目</b>（类型锁定）</label><input id="mkt-newname" placeholder="如：2027年容量市场仿真"></div></div>
      <button class="btn pri" onclick="mktCreateProject()">新建项目</button>
    </div>
    <div class="card"><h3>流程导览</h3>
      <div class="steps" style="margin-bottom:6px">${['①情景','②需求曲线','③供给报价','④出清设置','⑤出清过程','⑥市场力','⑦结果比较'].map((n,i)=>`<div class="st" style="cursor:pointer" onclick="MKTTAB=${i+1};rerender()">${n}</div>`).join('')}</div>
      <div class="mono-note">当前项目：${MKT.project?esc(MKT.project.name)+'（'+esc(MKT.project.project_dir.slice(0,8))+'）':'<b>未选择</b>——在①选择或新建后，②—⑦在该项目上工作。'}</div></div>`;
  }
  const bodies = { 1:viewMkt1, 2:viewMkt2, 3:viewMkt3, 4:viewMkt4, 5:viewMkt5, 6:viewMkt6, 7:viewMkt7, 8:viewDemoPerform };
  return head + (bodies[MKTTAB]||viewMkt1)();
}
async function loadMarket(){
  const el = document.getElementById('market-ready');
  if(el){
    try{
      const R = await computeReadiness();
      el.innerHTML = readyListHTML(marketReadyRows(R));
    }catch(e){ el.innerHTML = '<div class="errbar">读取失败</div>'; }
  }
  // 出清工作台按标签挂载只读加载器（元素在哪个标签就喂哪个）
  if(MKTTAB===0) mktLoadProjects('mkt-projects');
  if(MKTTAB===1) mktLoadProjects('mkt-projects-1');
  if(MKTTAB===3) mktLoadOffers();
  if(MKTTAB===7) mktLoadFrozen();
}

/* ============ 市场出清正式工作台（Q-M1—M6 裁决 2026-10-11；设计 2026-10-11 §4/§5） ============
 * ①情景（项目选择/新建）→②需求曲线（VRR 预览图）→③供给报价（台账＋CSV）
 * →④出清设置→⑤出清预览（大图）＋冻结→⑥市场力→⑦结果与报告。
 * 预览零副作用可多次；冻结＝显式动作一次性不可变（rule_version＋确认人必填）。 */
const MKT = {
  project: null,
  targetMode: 'manual', targetRefProj: '', targetRefRun: '', target: '1000',
  coneMode: 'manual', coneRefProj: '', coneRefRun: '', cone: '185',
  a: [['0.97','1.75'], ['1.00','1.30'], ['1.05','1.00']],
  cap: '', floor: '', dev: '0',
  curveResult: null,
  offersText: '', ledger: null,
  minproc: '970', mit: '', rule: '', confirmer: '',
  result: null, frozen: null,
};
const mktDemoNote = `<div class="hintbar warn">以下演示区为<b>演示口径</b>（示例数字）：演示区不执行任何出清计算，演示阈值不得当作正式规则——正式出清计算见本页上方工作台。</div>`;
function mktGuardCard(){
  return `<div class="hintbar warn">请先在「① 市场情景」选择或新建一个市场仿真项目——本页全部操作都作用于该项目。</div>`;
}
function mktCurvePayload(){
  const p = { project_ref: MKT.project.project_dir };
  if(MKT.targetMode==='manual') p.target_mw = Number(MKT.target);
  else p.target_reference = { project_ref: MKT.targetRefProj, run_ref: MKT.targetRefRun };
  if(MKT.coneMode==='manual') p.net_cone = Number(MKT.cone);
  else p.net_cone_reference = { project_ref: MKT.coneRefProj, run_ref: MKT.coneRefRun };
  p.anchors = MKT.a.map(x => [Number(x[0]), Number(x[1])]);
  if(MKT.cap!=='') p.cap = Number(MKT.cap);
  if(MKT.floor!=='') p.floor = Number(MKT.floor);
  if(MKT.dev!=='') p.quantity_deviation_percent = Number(MKT.dev);
  return p;
}
function mktCollect(){
  const val = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  if(!MKT.project) return;
  MKT.target = val('mkt-target'); MKT.cone = val('mkt-cone');
  MKT.targetRefProj = val('mkt-trefproj'); MKT.targetRefRun = val('mkt-trefrun');
  MKT.coneRefProj = val('mkt-crefproj'); MKT.coneRefRun = val('mkt-crefrun');
  MKT.a = [[val('mkt-a0q'), val('mkt-a0p')], [val('mkt-a1q'), val('mkt-a1p')], [val('mkt-a2q'), val('mkt-a2p')]];
  MKT.cap = val('mkt-cap'); MKT.floor = val('mkt-floor'); MKT.dev = val('mkt-dev');
  const of = val('mkt-offers'); if(of!=='' || MKT.offersText) MKT.offersText = of;
  MKT.minproc = val('mkt-minproc') || MKT.minproc;
  MKT.mit = val('mkt-mit'); MKT.rule = val('mkt-rule'); MKT.confirmer = val('mkt-confirmer');
}
function viewMkt1(){
  if(!MKT.project) return mktGuardCard() + `<div class="card"><h3>市场仿真项目</h3><div id="mkt-projects-1" class="loading">读取中…</div></div>`;
  return `<div class="card"><h3>市场仿真项目（类型 market_simulation，创建即锁定）</h3>
    <div id="mkt-projects-1" class="loading">读取中…</div>
    <div class="fgrid" style="margin-top:8px"><div class="fld"><label><b>新建</b>（新情景＝新项目）</label><input id="mkt-newname" placeholder="如：2028年高负荷情景"></div></div>
    <button class="btn" onclick="mktCreateProject()">新建市场仿真项目</button></div>
    <div class="card"><h3>当前情景</h3>
      <div class="bigstat"><div class="bstat"><div class="k">当前项目</div><div class="v" style="font-size:18px">${esc(MKT.project.name)}</div><div class="u">${esc(MKT.project.project_dir.slice(0,8))}｜一个项目＝一个市场情景；重跑请复制新项目</div></div></div></div>`;
}
async function mktLoadProjects(elId){
  const el = document.getElementById(elId||'mkt-projects-1');
  if(!el) return;
  try{
    const d = await jget('/api/projects');
    const rows = (d.projects||[]).filter(p=>p.measurement_type==='market_simulation' && !p.archived);
    el.innerHTML = rows.length ? rows.map(p=>`<div class="rowitem"><div class="tt">${esc(p.name)}<small>${p.formal_result?'<span class="tag ok">已有正式出清结果</span>':'进行中'}｜${esc(p.project_dir.slice(0,8))}</small></div>
      <button class="btn ${MKT.project&&MKT.project.project_dir===p.project_dir?'':''}" onclick='mktPickProject(${JSON.stringify({project_dir:p.project_dir,name:p.name}).replace(/'/g,"&#39;")})'>${MKT.project&&MKT.project.project_dir===p.project_dir?'当前项目':'选此项目'}</button></div>`).join('')
      : '<span class="loading">尚无市场仿真项目——新建一个即可开始。</span>';
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
function mktPickProject(p){ MKT.project = p; MKT.result=null; MKT.frozen=null; MKT.curveResult=null; rerender(); }
async function mktCreateProject(){
  const input = document.getElementById('mkt-newname');
  const name = (input?input.value:'').trim();
  if(!name){ flash('项目名称不能为空'); return; }
  try{
    const r = await jpost('/api/projects', {name, description:'', project_kind:'market', measurement_type:'market_simulation'});
    flash('市场仿真项目已创建（类型锁定）：'+r.name);
    MKT.project = {project_dir:r.project_ref, name:r.name};
    rerender();
  }catch(e){ flash('创建被拒绝：'+e.message); }
}
function viewMkt2(){
  if(!MKT.project) return mktGuardCard();
  return `<div class="card"><h3>容量需求曲线（Q-M3：VRR 三点折线）</h3>
    <div class="fgrid">
      <div class="fld"><label><b>目标容量来源</b></label>
        <select onchange="MKT.targetMode=this.value;rerender()">
          <option value="manual" ${MKT.targetMode==='manual'?'selected':''}>显式输入（MW）</option>
          <option value="ref" ${MKT.targetMode==='ref'?'selected':''}>引用目标容量结果（项目＋运行）</option>
        </select></div>
      ${MKT.targetMode==='manual'
        ? `<div class="fld"><label><b>目标容量</b>（MW，>0）</label><input class="num" id="mkt-target" value="${esc(MKT.target)}"></div>`
        : `<div class="fld"><label>项目目录名</label><input id="mkt-trefproj" value="${esc(MKT.targetRefProj)}"></div>
           <div class="fld"><label>运行标识</label><input id="mkt-trefrun" value="${esc(MKT.targetRefRun)}"></div>`}
      <div class="fld"><label><b>Net CONE 来源</b></label>
        <select onchange="MKT.coneMode=this.value;rerender()">
          <option value="manual" ${MKT.coneMode==='manual'?'selected':''}>显式输入（元/kW·年）</option>
          <option value="ref" ${MKT.coneMode==='ref'?'selected':''}>引用 Net CONE 结果</option>
        </select></div>
      ${MKT.coneMode==='manual'
        ? `<div class="fld"><label><b>Net CONE</b>（元/kW·年，>0）</label><input class="num" id="mkt-cone" value="${esc(MKT.cone)}"></div>`
        : `<div class="fld"><label>项目目录名</label><input id="mkt-crefproj" value="${esc(MKT.coneRefProj)}"></div>
           <div class="fld"><label>运行标识</label><input id="mkt-crefrun" value="${esc(MKT.coneRefRun)}"></div>`}
      ${MKT.a.map((x,i)=>`<div class="fld"><label>锚点 ${'ABC'[i]}（容量比, 价格比）</label>
        <div style="display:flex;gap:6px"><input class="num" id="mkt-a${i}q" value="${esc(x[0])}" style="width:80px"><input class="num" id="mkt-a${i}p" value="${esc(x[1])}" style="width:80px"></div></div>`).join('')}
      <div class="fld"><label>价格上限（元/kW·年，可选）</label><input class="num" id="mkt-cap" value="${esc(MKT.cap)}" placeholder="不填＝不启用"></div>
      <div class="fld"><label>价格下限（可选）</label><input class="num" id="mkt-floor" value="${esc(MKT.floor)}" placeholder="不填＝不启用"></div>
      <div class="fld"><label>数量偏差（%，可选）</label><input class="num" id="mkt-dev" value="${esc(MKT.dev)}"></div>
    </div>
    <button class="btn pri" onclick="mktPreviewCurve()">构造并预览需求曲线</button>
    <div id="mkt-curve-out" style="margin-top:12px"></div></div>`;
}
async function mktPreviewCurve(){
  mktCollect();
  try{
    const curve = await jpost('/api/market/curve', mktCurvePayload());
    MKT.curveResult = curve;
    const el = document.getElementById('mkt-curve-out');
    const qs = curve.points.map(p=>fmt(p[0],0)).join(' / ');
    const ps = curve.points.map(p=>fmt(p[1],2)).join(' / ');
    el.innerHTML = `<div class="mono-note">容量点（MW）：${qs}｜价格点（元/kW·年）：${ps}｜领口 ${curve.cap!=null?'上限 '+fmt(curve.cap,2):'无'} / ${curve.floor!=null?'下限 '+fmt(curve.floor,2):'无'}｜需求量在两端饱和（诚实简化）</div>
    <div style="margin-top:8px">${mktCurveSVG(curve)}</div>`;
    flash('需求曲线已构造（预览零副作用）');
  }catch(e){ flash('曲线构造被拒绝：'+e.message); }
}
function mktCurveSVG(curve){
  const W=680,H=300,L=64,R=16,T=14,B=40;
  const pts = curve.points;
  const maxQ = Math.max(...pts.map(p=>p[0]))*1.12, maxP = Math.max(...pts.map(p=>p[1]))*1.15;
  const x = q => L + (q/maxQ)*(W-L-R), y = p => H-B - (p/maxP)*(H-T-B);
  const poly = pts.map(p=>`${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  let extra = '';
  if(curve.cap!=null) extra += `<line x1="${L}" y1="${y(curve.cap)}" x2="${W-R}" y2="${y(curve.cap)}" stroke="#c62828" stroke-dasharray="6 4"/><text x="${W-R-4}" y="${y(curve.cap)-4}" font-size="10" fill="#c62828" text-anchor="end">上限 ${fmt(curve.cap,0)}</text>`;
  if(curve.floor!=null) extra += `<line x1="${L}" y1="${y(curve.floor)}" x2="${W-R}" y2="${y(curve.floor)}" stroke="#2e7d32" stroke-dasharray="6 4"/><text x="${W-R-4}" y="${y(curve.floor)-4}" font-size="10" fill="#2e7d32" text-anchor="end">下限 ${fmt(curve.floor,0)}</text>`;
  const marks = pts.map((p,i)=>`<circle cx="${x(p[0])}" cy="${y(p[1])}" r="4" fill="#1565c0"/><text x="${x(p[0])+6}" y="${y(p[1])-6}" font-size="10" fill="#1565c0">${'ABC'[i]}（${fmt(p[0],0)}MW, ${fmt(p[1],0)}）</text>`).join('');
  const ticks = [0,0.25,0.5,0.75,1].map(t=>`<line x1="${L+(W-L-R)*t}" y1="${H-B}" x2="${L+(W-L-R)*t}" y2="${H-B+4}" stroke="#999"/><text x="${L+(W-L-R)*t}" y="${H-B+14}" font-size="9" text-anchor="middle" fill="#666">${fmt(maxQ*t,0)}</text>`).join('');
  const pticks = [0,0.5,1].map(t=>`<text x="${L-6}" y="${y(maxP*t)+3}" font-size="9" text-anchor="end" fill="#666">${fmt(maxP*t,0)}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:720px"><line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" stroke="#999"/><line x1="${L}" y1="${H-B}" x2="${W-R}" y2="${H-B}" stroke="#999"/>${ticks}${pticks}
    <polyline points="${L},${y(pts[0][1])} ${poly} ${W-R},${y(pts[pts.length-1][1])}" fill="none" stroke="#1565c0" stroke-width="2.2"/>${extra}${marks}
    <text x="${(L+W-R)/2}" y="${H-6}" font-size="10" text-anchor="middle" fill="#666">容量（MW）</text>
    <text x="14" y="${(T+H-B)/2}" font-size="10" fill="#666" transform="rotate(-90 14 ${(T+H-B)/2})">价格（元/kW·年）</text></svg>`;
}
function viewMkt3(){
  if(!MKT.project) return mktGuardCard();
  return `<div class="card"><h3>供给报价台账 <span class="tag lock">版本化</span></h3>
    <div class="psub">每行一个主体：标识, 资源类型, 核定可靠容量MW, 申报容量MW, 报价元/kW·年, 是否合格(true/留空)。核定值取 ELCC/EFC 正式结果点值——<b>申报不得超过核定（Q-M4，超出即阻断）</b>。</div>
    <div id="mkt-ledger" class="loading">台账读取中…</div>
    <details class="disc" style="margin-top:8px"><summary>录入/更新报价（形成新版本）</summary><div class="body">
      <textarea id="mkt-offers" style="width:100%;height:110px;font-family:var(--mono);font-size:12px" placeholder="燃煤A, coal, 300, 300, 60,&#10;光伏B, solar, 500, 500, 110,">${esc(MKT.offersText)}</textarea>
      <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button class="btn pri" onclick="mktSaveOffers()">保存报价新版本</button>
        <button class="btn" onclick="mktCsvTemplate()">下载 CSV 模板</button>
        <input type="file" accept=".csv,text/csv" id="mkt-csv" style="max-width:180px">
        <button class="btn" onclick="mktImportCsv()">导入 CSV</button>
      </div></div></details></div>`;
}
async function mktLoadOffers(){
  const el = document.getElementById('mkt-ledger');
  if(!el || !MKT.project) return;
  try{
    const d = await jget('/api/market/offers?project='+encodeURIComponent(MKT.project.project_dir));
    MKT.ledger = d;
    if(!d.current){ el.innerHTML = '<span class="loading">尚未录入报价。</span>'; return; }
    const totalAcc = d.current.offers.reduce((a,o)=>a+o.accredited_capacity_mw,0);
    const totalOff = d.current.offers.reduce((a,o)=>a+o.offered_capacity_mw,0);
    el.innerHTML = `<table class="grid"><tr><th>主体</th><th>资源类型</th><th class="r">核定（MW）</th><th class="r">申报（MW）</th><th class="r">报价（元/kW·年）</th><th>合格</th></tr>
      ${d.current.offers.map(o=>`<tr><td>${esc(o.subject_id)}</td><td>${esc(o.resource_type||'')}</td><td class="r num">${fmt(o.accredited_capacity_mw,1)}</td><td class="r num">${fmt(o.offered_capacity_mw,1)}</td><td class="r num">${fmt(o.offer_price,2)}</td><td>${o.qualified?'<span class="tag ok">合格</span>':'<span class="tag lock">暂停</span>'}</td></tr>`).join('')}
      <tr><td><b>合计</b></td><td></td><td class="r num"><b>${fmt(totalAcc,1)}</b></td><td class="r num"><b>${fmt(totalOff,1)}</b></td><td colspan="2"></td></tr></table>
      <div class="mono-note" style="margin-top:6px">当前 v${d.current.version}｜历史 ${d.history.length} 版（追加式不覆盖）</div>`;
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function mktSaveOffers(){
  if(!MKT.project) return;
  const ta = document.getElementById('mkt-offers');
  const lines = ((ta?ta.value:'')||MKT.offersText).split('\n').map(l=>l.trim()).filter(Boolean);
  const offers = lines.map(l=>{
    const [sid, rtype, acc, off, price, qual] = l.split(',').map(x=>(x||'').trim());
    return {subject_id:sid, resource_type:rtype||'', accredited_capacity_mw:Number(acc),
            offered_capacity_mw:Number(off), offer_price:Number(price),
            qualified: qual==='' ? true : !(qual==='false'||qual==='否'||qual==='暂停')};
  });
  try{
    const r = await jpost('/api/market/offers', {project_ref:MKT.project.project_dir, offers});
    MKT.offersText = (ta?ta.value:'');
    flash('报价 v'+r.version+' 已保存（'+r.offers.length+' 个主体）');
    mktLoadOffers();
  }catch(e){ flash('报价被拒绝：'+e.message); }
}
function mktCsvTemplate(){ window.location.href = '/api/market/offers/template'; }
async function mktImportCsv(){
  if(!MKT.project) return;
  const file = ((document.getElementById('mkt-csv')||{}).files||[])[0];
  if(!file){ flash('请先选择 CSV 文件（可用上方模板）'); return; }
  const csv = await file.text();
  try{
    const r = await jpost('/api/market/offers/import', {project_ref:MKT.project.project_dir, csv});
    flash('报价 v'+r.version+' 已导入（'+r.offers.length+' 个主体）');
    mktLoadOffers();
  }catch(e){ flash('CSV 导入被拒绝：'+e.message); }
}
function viewMkt4(){
  if(!MKT.project) return mktGuardCard();
  return `<div class="card"><h3>出清设置（Q-M2/Q-M5/Q-M6）</h3>
    <div class="fgrid">
      <div class="fld"><label><b>最低采购要求</b>（MW，>0）</label><input class="num" id="mkt-minproc" value="${esc(MKT.minproc)}"><div class="fh">触顶仍不足此要求 ⇒ 触顶出清＋缺口披露（不静默成功）</div></div>
      <div class="fld"><label>缓解参考成本（元/kW·年，可选）</label><input class="num" id="mkt-mit" value="${esc(MKT.mit)}" placeholder="开启缓解重清对比"><div class="fh">关键供应商报价替换为此值重清一次，两版并列</div></div>
      <div class="fld"><label><b>市场规则版本</b>（冻结必填）</label><input id="mkt-rule" value="${esc(MKT.rule)}" placeholder="如：2027版容量市场规则"></div>
      <div class="fld"><label><b>确认人</b>（冻结必填，留痕）</label><input id="mkt-confirmer" value="${esc(MKT.confirmer)}" placeholder="姓名/工号"></div>
    </div>
    <div class="mono-note">统一出清价（密封一次性、单区）；同价段按申报容量比例分配并披露；市场力指标为披露性质不阻断出清（Q-M1）。</div></div>`;
}
function viewMkt5(){
  if(!MKT.project) return mktGuardCard();
  return `<div class="card"><h3>出清预览与冻结</h3>
    <div class="psub">预览＝②曲线参数＋③当前台账报价＋④设置（零副作用可多次）；冻结＝显式动作，一次性不可变并绑定项目正式结果。</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn pri" onclick="mktPreviewClearing()">出清预览（即时计算）</button>
      <button class="btn" onclick="mktFreeze()">确认冻结正式出清结果</button>
    </div>
    <div id="mkt-clear-out" style="margin-top:12px"></div></div>
    ${MKT.result?mktResultCards(MKT.result):''}
    <div class="card"><h3>示意演示区（演示口径）</h3>${mktDemoNote}${viewDemoAuction()}</div>`;
}
async function mktPreviewClearing(){
  mktCollect();
  try{
    const payload = {...mktCurvePayload(), min_procurement_mw:Number(MKT.minproc)};
    if(MKT.mit!=='') payload.mitigation_reference_cost = Number(MKT.mit);
    const r = await jpost('/api/market/clearing/preview', payload);
    MKT.result = r;
    rerender();
    flash('出清预览完成：'+(r.clearing_price!=null?'出清价 '+fmt(r.clearing_price,2)+' 元/kW·年':r.status));
  }catch(e){ flash('出清被拒绝：'+e.message); }
}
function mktResultCards(r){
  const statusLabel = {cleared:'出清成功', cleared_with_shortfall:'触顶出清（有缺口）', not_cleared:'未出清（无有效供给）'}[r.status]||r.status;
  const winners = r.winners||[], losers = r.losers||[];
  return `<div class="card"><h3>出清结果 <span class="tag ${r.status==='cleared'?'ok':'lock'}">${esc(statusLabel)}</span>${r.tie_allocated?' <span class="tag plan">并列报价按比例分配</span>':''}</h3>
    <div class="bigstat">
      <div class="bstat hero"><div class="k">统一出清价（元/kW·年）</div><div class="v">${r.clearing_price!=null?fmt(r.clearing_price,2):'—'}</div><div class="u">边际主体报价｜报价来源 ${esc(r.offers_source||'')}</div></div>
      <div class="bstat"><div class="k">中标容量</div><div class="v">${fmt(r.procured_mw,1)}<span class="u">MW</span></div><div class="u">目标 ${fmt(r.target_mw,0)} MW｜完成率 ${(r.completion_ratio*100).toFixed(1)}%</div></div>
      <div class="bstat"><div class="k">容量缺口</div><div class="v">${fmt(r.shortfall_mw,1)}<span class="u">MW</span></div><div class="u">最低采购要求 ${fmt(r.min_procurement_mw,0)} MW</div></div>
    </div>
    <div style="margin-top:8px">${mktClearingSVG(r)}</div>
    <details class="disc" open><summary>中标清单（统一出清价结算）</summary><div class="body">
      <table class="grid"><tr><th>主体</th><th class="r">申报（MW）</th><th class="r">中标（MW）</th><th class="r">报价</th></tr>
      ${winners.map(w=>`<tr><td>${esc(w.subject_id)}</td><td class="r num">${fmt(w.offered_capacity_mw,1)}</td><td class="r num"><b>${fmt(w.cleared_mw,1)}</b></td><td class="r num">${fmt(w.offer_price,2)}</td></tr>`).join('')||'<tr><td colspan="4">无</td></tr>'}</table></div></details>
    <details class="disc"><summary>未中标清单（${losers.length} 家）</summary><div class="body">
      <table class="grid"><tr><th>主体</th><th class="r">申报（MW）</th><th class="r">报价</th></tr>
      ${losers.map(w=>`<tr><td>${esc(w.subject_id)}</td><td class="r num">${fmt(w.offered_capacity_mw,1)}</td><td class="r num">${fmt(w.offer_price,2)}</td></tr>`).join('')}</table></div></details>
  </div>`;
}
function mktClearingSVG(r){
  const curve = (MKT.curveResult&&MKT.curveResult.target_mw===r.target_mw) ? MKT.curveResult : {points:[[r.target_mw*0.97,r.clearing_price||1],[r.target_mw,r.clearing_price||1],[r.target_mw*1.05,(r.clearing_price||1)*0.7]]};
  const stack = [...(r.winners||[]), ...(r.losers||[])].sort((a,b)=>a.offer_price-b.offer_price);
  if(!stack.length) return '';
  let cum = 0; const steps = [[0,0]];
  for(const o of stack){ steps.push([cum, o.offer_price]); cum += o.offered_capacity_mw; steps.push([cum, o.offer_price]); }
  const W=680,H=320,L=64,R=16,T=14,B=40;
  const maxQ = Math.max(cum, r.target_mw*1.08, ...curve.points.map(p=>p[0]))*1.05;
  const maxP = Math.max(...stack.map(o=>o.offer_price), ...curve.points.map(p=>p[1]), r.clearing_price||0)*1.15;
  const x = q => L + (q/maxQ)*(W-L-R), y = p => H-B - (p/maxP)*(H-T-B);
  const curvePoly = curve.points.map(p=>`${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  const stackPoly = steps.map(s=>`${x(s[0]).toFixed(1)},${y(s[1]).toFixed(1)}`).join(' ');
  const px = x(r.procured_mw), py = y(r.clearing_price||0);
  const gapRect = r.shortfall_mw>0 && r.clearing_price!=null
    ? `<rect x="${px}" y="${y(r.clearing_price)}" width="${x(r.target_mw)-px}" height="${H-B-y(r.clearing_price)}" fill="#c6282818" stroke="#c62828" stroke-dasharray="4 3"/><text x="${(px+x(r.target_mw))/2}" y="${y(r.clearing_price)-6}" font-size="10" fill="#c62828" text-anchor="middle">缺口 ${fmt(r.shortfall_mw,0)} MW</text>` : '';
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:720px">
    <line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" stroke="#999"/><line x1="${L}" y1="${H-B}" x2="${W-R}" y2="${H-B}" stroke="#999"/>
    <polyline points="${stackPoly}" fill="none" stroke="#ef6c00" stroke-width="2.2"/>
    <polyline points="${curvePoly}" fill="none" stroke="#1565c0" stroke-width="2.2"/>
    ${r.clearing_price!=null?`<line x1="${px}" y1="${py}" x2="${L}" y2="${py}" stroke="#2e7d32" stroke-dasharray="4 3"/><line x1="${px}" y1="${py}" x2="${px}" y2="${H-B}" stroke="#2e7d32" stroke-dasharray="4 3"/>
    <circle cx="${px}" cy="${py}" r="5" fill="#2e7d32"/><text x="${px+8}" y="${py-8}" font-size="11" fill="#2e7d32">出清点 ${fmt(r.clearing_price,1)} × ${fmt(r.procured_mw,0)}MW</text>`:''}
    ${gapRect}
    <text x="${W-R}" y="${y(curve.points[0][1])-4}" font-size="10" fill="#1565c0" text-anchor="end">VRR 需求曲线</text>
    <text x="${x(cum)-4}" y="${y(steps[steps.length-1][1])-4}" font-size="10" fill="#ef6c00" text-anchor="end">供给堆栈</text>
    <text x="${(L+W-R)/2}" y="${H-6}" font-size="10" text-anchor="middle" fill="#666">容量（MW）</text></svg>`;
}
async function mktFreeze(){
  mktCollect();
  if(!MKT.project) return;
  if(!MKT.rule){ flash('冻结需要市场规则版本（④出清设置）'); return; }
  if(!MKT.confirmer){ flash('冻结是显式确认动作：请填写确认人'); return; }
  try{
    const payload = {...mktCurvePayload(), min_procurement_mw:Number(MKT.minproc),
                     rule_version:MKT.rule, confirmer:MKT.confirmer};
    if(MKT.mit!=='') payload.mitigation_reference_cost = Number(MKT.mit);
    const r = await jpost('/api/market/clearing/freeze', payload);
    flash('正式出清结果已冻结（一次性不可变）：出清价 '+(r.clearing_price!=null?fmt(r.clearing_price,2):'—'));
    MKT.frozen = null;
    MKTTAB = 7; rerender();
  }catch(e){ flash('冻结被拒绝：'+e.message); }
}
function viewMkt6(){
  if(!MKT.project) return mktGuardCard();
  const r = MKT.result;
  if(!r) return `<div class="hintbar warn">先在「⑤ 出清过程」跑一次出清预览——市场力画像随出清结果给出（Q-M1：披露不阻断，阈值随规则版本判读）。</div>`;
  const p = r.market_power||{};
  const mit = r.mitigation;
  return `<div class="card"><h3>市场力画像 <span class="tag lock">披露不阻断</span></h3>
    <div class="bigstat">
      <div class="bstat"><div class="k">HHI</div><div class="v">${p.hhi!=null?fmt(p.hhi,0):'—'}</div><div class="u">按申报容量份额（出清前口径）</div></div>
      <div class="bstat"><div class="k">最大主体占比</div><div class="v">${p.max_share!=null?(p.max_share*100).toFixed(1)+'%':'—'}</div></div>
      <div class="bstat"><div class="k">关键供应商</div><div class="v">${(p.pivotal_subjects||[]).length}<span class="u">家</span></div><div class="u">${(p.pivotal_subjects||[]).join('、')||'无'}</div></div>
    </div>
    <div class="mono-note" style="margin-top:8px">${esc(p.note||'')}</div></div>
  ${mit?`<div class="card"><h3>缓解对比（关键供应商报价替换重清一次；正式结果仍为缓解前）</h3>
    <table class="grid"><tr><th></th><th class="r">缓解前</th><th class="r">缓解后</th></tr>
      <tr><td>出清价（元/kW·年）</td><td class="r num">${mit.price_before!=null?fmt(mit.price_before,2):'—'}</td><td class="r num">${mit.price_after!=null?fmt(mit.price_after,2):'—'}</td></tr>
      <tr><td>中标容量（MW）</td><td class="r num">${fmt(mit.procured_before,1)}</td><td class="r num">${fmt(mit.procured_after,1)}</td></tr></table>
    <div class="mono-note">替换主体：${esc((mit.replaced_subjects||[]).join('、')||'无')}｜参考成本 ${fmt(mit.reference_cost,2)}</div></div>`
  :'<div class="hintbar">未开启缓解（④出清设置填"缓解参考成本"后重新预览可见对比）。</div>'}
  <div class="card"><h3>演示区（演示口径，非正式判据）</h3>${mktDemoNote}${viewDemoAssessSmall()}</div>`;
}
function viewDemoAssessSmall(){ return viewDemoAssess(); }
function viewMkt7(){
  if(!MKT.project) return mktGuardCard();
  return `<div class="card"><h3>正式出清结果与报告</h3>
    <div id="mkt-frozen" class="loading">读取中…</div></div>
    <div class="card"><h3>情景比较（演示口径）</h3>${mktDemoNote}${viewDemoAssess()}</div>`;
}
async function mktLoadFrozen(){
  const el = document.getElementById('mkt-frozen');
  if(!el || !MKT.project) return;
  try{
    const d = await jget('/api/market/clearing?project='+encodeURIComponent(MKT.project.project_dir));
    if(!d.frozen){ el.innerHTML = '<div class="hintbar warn">尚未冻结正式出清结果——在「⑤ 出清过程」预览并确认冻结后，这里展示结果与报告下载。</div>'; return; }
    MKT.frozen = d;
    const m = d.manifest, res = d.result;
    const statusLabel = {cleared:'出清成功', cleared_with_shortfall:'触顶出清（有缺口）', not_cleared:'未出清'}[m.result_status]||m.result_status;
    el.innerHTML = `<div class="bigstat">
        <div class="bstat hero"><div class="k">统一出清价（元/kW·年）</div><div class="v">${m.clearing_price!=null?fmt(m.clearing_price,2):'—'}</div><div class="u">${esc(statusLabel)}｜规则 ${esc(m.rule_version)}｜冻结 ${esc(String(m.frozen_at||'').slice(0,10))}（${esc(m.confirmer)}）</div></div>
        <div class="bstat"><div class="k">中标容量</div><div class="v">${fmt(m.procured_mw,1)}<span class="u">MW</span></div></div>
        <div class="bstat"><div class="k">容量缺口</div><div class="v">${fmt(m.shortfall_mw,1)}<span class="u">MW</span></div></div>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn pri" onclick="mktReport('xlsx')">导出 Excel 报告</button>
        <button class="btn" onclick="mktReport('html')">导出 HTML 报告</button>
        <span class="mono-note" style="align-self:center">报告＝已冻结正式结果的业务投影（同身份重放，不重算出清）。</span>
      </div>
      <details class="disc" style="margin-top:10px"><summary>中标清单（${(res.winners||[]).length} 家）</summary><div class="body">
        <table class="grid"><tr><th>主体</th><th class="r">申报（MW）</th><th class="r">中标（MW）</th><th class="r">报价</th></tr>
        ${(res.winners||[]).map(w=>`<tr><td>${esc(w.subject_id)}</td><td class="r num">${fmt(w.offered_capacity_mw,1)}</td><td class="r num">${fmt(w.cleared_mw,1)}</td><td class="r num">${fmt(w.offer_price,2)}</td></tr>`).join('')}</table></div></details>`;
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
function mktReport(fmtType){
  window.location.href = '/api/market/report?project='
    + encodeURIComponent(MKT.project.project_dir) + '&format=' + (fmtType==='html'?'html':'xlsx');
}

/* ============ P-EFC: ELCC/EFC 六步测算模块（重构） ============ */
