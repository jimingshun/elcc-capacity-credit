function rmStep3(body){
  const o=RM.options||{};
  const cands=o.elcc_candidates||[];
  const frozen=(o.data_refs||[]).filter(d=>d.frozen&&d.approved);
  const banks=o.scenario_banks||o.banks||[];
  const calibRef=RM.calib&&RM.calib.converged?RM.calib.converged:null;
  body.innerHTML=`
  <div class="lockbar"><b>基准已冻结</b>：数据批次 ${esc(String((frozen[0]||{}).import_batch_id||'').slice(0,8))}｜样本集 ${esc(String((banks[0]||{}).scenario_bank_id||'').slice(0,8))}｜根种子 ${esc(String((banks[0]||{}).root_seed??'见样本集'))}。本步骤只能选择待测资源与情景设定，基准输入不可修改——保证可靠性改善可干净归因于待测资源。</div>
  ${calibRef?`<div class="hintbar"><b>标定参照（CAL-09，解释性，不回写基线）</b>：基准负荷水平 <b>${esc(Number(calibRef.scale).toFixed(2))}%</b>（判据 ${esc(String((calibRef.ref||{}).criterion_metric||''))} 目标 ${esc(String((calibRef.ref||{}).criterion_target_value))} ±${esc(String((calibRef.ref||{}).tolerance_percent))}%）。解读结果时可将该上浮视为系统偏紧程度的注脚；本次测算仍以未缩放的冻结数据为基准。</div>`:''}
  <div class="grid2">
    <div class="card"><h3>待测资源参数 <span class="mono-note">来自冻结数据的候选资源组（引擎口径）</span></h3>
      <div class="hintbar warn">与原型差异（诚实说明）：本工具的待测资源参数<b>不在界面录入</b>，而是取自冻结数据中已解析的候选资源组；因此没有“光伏/风电/储能/火电/水电/需求响应”分页表单。其中<b>需求响应（DR）引擎不支持</b>，火电新建按常规机组口径处理。</div>
      <div class="fgrid">
        <div class="fld"><label><b>待评估资源（候选组）</b></label><select id="rm-group">${cands.map(c=>`<option value="${esc(c.resource_group_id)}">${esc(c.resource_group_id)}</option>`).join('')}</select></div>
        <div class="fld"><label><b>冻结数据批次</b></label><select id="rm-data">${frozen.map(d=>`<option value="${esc(d.import_batch_id)}">批次 ${esc(String(d.import_batch_id).slice(0,8))}（已确认）</option>`).join('')}</select></div>
        <div class="fld"><label><b>模拟样本集</b></label><select id="rm-bank">${banks.map(b=>`<option value="${esc(b.scenario_bank_id)}">样本集 ${esc(String(b.scenario_bank_id).slice(0,8))}（${esc(String(b.sample_count))} 样本）</option>`).join('')}</select></div>
        <div class="fld"><label><b>各年占比方案</b></label><select id="rm-weight">${(o.weight_set_ids||[]).map(w=>`<option value="${esc(w)}">${esc(w)}</option>`).join('')}</select></div>
        ${isEFC()
          ? `<div class="fld"><label><b>搜索上界来源（EFC 专属）</b></label><select id="rm-uppersrc">${(o.efc_upper_bound_sources||['nameplate_capacity','declared_mw']).map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select></div>
             <div class="fld" id="rm-decl-fld" style="display:none"><label><b>声明上界（MW）</b></label><input class="num" id="rm-decl" data-min="0.01" value="2000"></div>`
          : `<div class="fld"><label><b>负荷增长方式（ELCC 专属）</b></label><select id="rm-growth">${(o.elcc_growth_modes||['fixed_mw']).map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('')}</select></div>`}
      </div>
      ${isEFC()?'':`<div class="hintbar" style="margin-top:8px">ELCC 评估链不消费基准储能：若冻结数据含储能，提交会被引擎拒绝（诚实守卫），请改用 EFC 或充裕度。</div>`}
    </div>
    <div>
      <div class="card"><h3>情景设定 <span class="mono-note">四项实验设计变量 · 随结果携带</span></h3>
        <table class="grid">
          <tr><th>变量</th><th>当前工具的承接方式</th></tr>
          <tr><td>测算口径</td><td>边际（等风险搜索）＝已接入；类别平均＝未接入（需多资源聚合服务）</td></tr>
          <tr><td>增量规模</td><td>由步骤 04 的搜索上界/容差承接（MW）</td></tr>
          <tr><td>落点</td><td>由候选资源组承接（全网聚合/分区取决于冻结数据定义）</td></tr>
          <tr><td>组合方式</td><td>单独接入＝已接入；风光储组合＝组合服务未接线（延期项）</td></tr>
        </table>
        <div class="fh" style="margin-top:6px">四项取值不同则结果不同；实际生效值以运行配置哈希与结果投影为准（不另造“出生证”字段）。</div></div>
      <div class="card"><h3>进入增量搜索</h3>
        <div class="fh">下一步将展示搜索设置（上界/容差/迭代/置信水平），确认后发起真实运行；运行期间按钮置灰防重复提交。</div>
        <div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="btn pri" onclick="rmToStep4()">接入资源，进入等效负荷增量搜索 →</button></div></div>
    </div>
  </div>`;
  const us=document.getElementById('rm-uppersrc');
  if(us){ const tg=()=>{document.getElementById('rm-decl-fld').style.display=us.value==='declared_mw'?'':'none';}; us.onchange=tg; tg(); }
}
function rmToStep4(){
  const g=id=>document.getElementById(id);
  RM.sel={ group_id:g('rm-group')?.value||'', data_ref:g('rm-data')?.value||'', bank_id:g('rm-bank')?.value||'',
    metric:(RM.options&&RM.options.metric)||g('rm-metric')?.value||'lole_days_per_year',
    weight_set_id:g('rm-weight')?.value||'', scenario_id:null };
  const metricEl=document.getElementById('rm-metric');
  if(metricEl) RM.sel.metric=metricEl.value;
  if(isEFC()){ RM.sel.upper_source=g('rm-uppersrc')?.value||'nameplate_capacity'; RM.sel.declared_mw=parseFloat(g('rm-decl')?.value)||null; }
  else { RM.sel.growth_mode=g('rm-growth')?.value||'fixed_mw'; }
  if(!RM.sel.group_id){ flash('请选择待评估资源（候选组）'); return; }
  RM.done[3]=true; RM.step=4; renderRelMod();
}
/* ---- W3 成本提示（三切片计划 §4.4）：年数＝覆盖投影 declared_years；
   样本上下限＝引擎收敛默认值（engine/convergence.py default_config：
   min_samples=2000、max_samples=100000，2026-09-22 实读）；
   折算系数＝计划实测记录（1 年 × 2000 次约 25–30 秒）。估算值，如实标注来源 ---- */
const COST_EST={secPerYearAt2000:[25,30],typicalSamples:2000,maxSamples:100000};
function rmCostHintHTML(years){
  const n=Number(years)||0;
  if(n<2) return '';  // 单年不显示（§4.4）
  const lo=n*COST_EST.secPerYearAt2000[0], hi=n*COST_EST.secPerYearAt2000[1];
  const fmtMin=v=>{ const m=Math.round(v/60*10)/10; return (m%1===0?String(m):m.toFixed(1))+' 分钟'; };
  const span=hi<90?`${lo}–${hi} 秒`:`${fmtMin(lo)}–${fmtMin(hi)}`;
  return `<div class="fh" style="margin-top:8px">本次测算按 <b>${n} 年天气</b> × 每档模拟 ${COST_EST.typicalSamples} 次起步、一直不收敛时最多 ${COST_EST.maxSamples} 次（引擎默认收敛口径；若本批数据另有声明口径，以运行配置为准）：通常每轮约 <b>${span}</b>（按实测 1 年 2000 次约 25–30 秒折算，估算值）。天气年份越多越慢。</div>`;
}
async function rmFillCostHint(elId, batchId){
  const el=document.getElementById(elId); if(!el||!batchId) return;
  try{
    const cov=await jget('/api/data/coverage?project='+encodeURIComponent(RM.project)+'&batch='+encodeURIComponent(batchId));
    el.innerHTML=rmCostHintHTML((cov.declared_years||[]).length);
  }catch(e){ /* 成本提示是辅助信息：覆盖不可读时留空，不挡主流程 */ }
}
/* ---- 步骤 04：等效负荷增量搜索（真实运行） ---- */
function rmStep4(body){
  body.innerHTML=`
  <div class="grid2">
    <div class="card"><h3>搜索设置 <span class="mono-note">等风险搜索（引擎既有能力）</span></h3>
      <div class="fgrid">
        <div class="fld"><label><b>搜索上界（MW）</b></label><input class="num" id="rm-upper" data-min="0.01" value="2000"></div>
        <div class="fld"><label>搜索容差（MW）</label><input class="num" id="rm-tol" data-min="0.001" step="0.1" value="1.0"></div>
        <div class="fld"><label>最大迭代</label><input class="num" id="rm-iter" data-min="1" data-max="200" data-int="1" value="20"></div>
        <div class="fld"><label>置信水平（0—1）</label><input class="num" id="rm-conf" data-min="0.5" data-max="0.999" step="0.01" value="0.95"></div>
        <div class="fld"><label>允许上界扩张</label><select id="rm-exp"><option value="0">否</option><option value="1">是</option></select></div>
        <div class="fld"><label>扩张后上界（MW）</label><input class="num" id="rm-expu" data-min="0.01" value="3000"></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:10px">
        <button class="btn" id="rm-preview" onclick="rmPreview()">生成配置预览</button>
        <button class="btn pri" id="rm-start" onclick="rmStart()">确认无误，发起${isEFC()?'EFC':'ELCC'}测算</button></div>
      <div id="rm-cost-hint"></div>
      <div id="rm-prev" style="margin-top:10px"></div>
      <div class="prog" id="rm-prog" style="display:none"><i></i></div>
      <div class="rlog" id="rm-log" style="display:none"></div></div>
    <div class="card"><h3>收敛结果区间 <span class="mono-note">真实结果；未收敛只显示区间不显示点值</span></h3>
      <div id="rm-bracket" class="fh">运行完成后显示搜索括区、有效容量与置信区间。</div>
      <canvas id="rmChart" height="260" style="width:100%;display:none"></canvas>
      <div class="chartcap" id="rmChartCap" style="font-size:11px;color:var(--faint);text-align:center;margin-top:6px;display:none"></div></div>
    <div class="card"><h3>二分迭代轨迹 <span class="mono-note">真实搜索轨迹制品（非演示）</span></h3>
      <div id="rm-trace"><div class="fh">运行完成后显示每轮试算点、风险值与区间收缩；轨迹同时保存在导出报告的「搜索与收敛」工作表。</div></div></div>
  </div>
  <div class="hintbar">原型中的“LOLE–负荷水平双曲线”需要多轮负荷缩放的成对评估结果；当前引擎按等风险容量搜索输出括区与收敛状态，页面据此如实绘制区间图，<b>不构造假曲线</b>。逐轮二分轨迹保存在运行制品的搜索轨迹中，可在导出报告的「搜索与收敛」工作表查看。</div>`;
  document.querySelectorAll('#rmBody input[data-min],#rmBody input[data-int]').forEach(inp=>{
    inp.addEventListener('input',()=>{sanitizeNumericInput(inp);clampNumericInput(inp);}); });
  rmFillCostHint('rm-cost-hint', RM.sel&&RM.sel.data_ref);
  if(RM.rv&&RM.rv.search_trace){ const el=document.getElementById('rm-trace'); if(el) el.innerHTML=traceTableHTML(RM.rv.search_trace); }
  else if(RM.rvError){ const el=document.getElementById('rm-trace'); if(el) el.innerHTML=`<div class="errbar">轨迹不可读（如实披露）：${esc(RM.rvError)}</div>`; }
}
function rmForm(){
  const g=id=>document.getElementById(id);
  const s=Object.assign({},RM.sel);
  s.upper_mw=parseFloat(g('rm-upper')?.value)||2000;
  s.tolerance_mw=parseFloat(g('rm-tol')?.value)||1;
  s.max_iterations=parseInt(g('rm-iter')?.value)||20;
  s.confidence=parseFloat(g('rm-conf')?.value)||0.95;
  s.allow_expansion=g('rm-exp')?.value==='1';
  s.expanded_upper_mw=parseFloat(g('rm-expu')?.value)||null;
  return s;
}
async function rmPreview(){
  const el=document.getElementById('rm-prev');
  el.innerHTML='<div class="loading">生成配置预览…</div>';
  try{
    const pv=await jpost('/api/runs/preview',{project_ref:RM.project,method:RM.method,data_ref:RM.sel.data_ref,form:rmForm()});
    el.innerHTML=`<div class="hintbar">将提交：${esc(pv.summary)}</div>`;
  }catch(e){ el.innerHTML=`<div class="errbar">预览被拒绝：${esc(e.message)}</div>`; }
}
async function rmStart(){
  if(RM.running) return;
  const btn=document.getElementById('rm-start'), pv=document.getElementById('rm-preview');
  const log=document.getElementById('rm-log'), prog=document.getElementById('rm-prog'), bar=prog.querySelector('i');
  btn.disabled=true; pv.disabled=true; RM.running=true;
  prog.style.display='block'; log.style.display='block'; log.innerHTML='';
  const say=(m,cls)=>{ log.innerHTML+=`<div class="${cls||''}">${esc(m)}</div>`; log.scrollTop=log.scrollHeight; };
  try{
    const form=rmForm();
    say('[0] 生成运行配置（服务端预览）…');
    const preview=await jpost('/api/runs/preview',{project_ref:RM.project,method:RM.method,data_ref:form.data_ref,form});
    say('[1] 配置就绪：'+preview.summary,'hl');
    bar.style.width='15%';
    say('[2] 提交真实运行（幂等意图，串行准入）…');
    const st=await jpost('/api/runs/start',{project_ref:RM.project,data_ref:form.data_ref,method:RM.method,config:preview.config});
    bar.style.width='30%';
    let snap=null;
    for(let i=0;i<240;i++){
      await new Promise(r=>setTimeout(r,1000));
      snap=await jget('/api/runs/status?ref='+encodeURIComponent(st.request_ref)+'&project='+encodeURIComponent(RM.project));
      bar.style.width=Math.min(90,30+i*2)+'%';
      if(i%5===0) say(`[3] 运行中… 状态 ${snap.status}${snap.phase?'｜阶段 '+snap.phase:''}`);
      if(snap.status==='execution_finished'||snap.status==='execution_failed') break;
    }
    RM.running=false;
    if(snap&&snap.status==='execution_finished'){
      bar.style.width='100%';
      say('[4] 运行结束，读取正式结果投影…','ok');
      RM.runRef=st.run_ref; RM.done[4]=true;
      await rmLoadResult();
      await rmLoadReportView();
      say('[✓] 结果已读取：'+(RM.view?(RM.view.result_status_label||''):''),'ok');
      RM.step=5; renderRelMod();
    }else{
      say('[✗] 运行未成功结束：'+((snap&&snap.status)||'未知')+((snap&&snap.execution_error)?'｜'+snap.execution_error:''));
      say('    失败/未收敛属于技术执行尝试，不算业务测算；可修正参数后重试。');
      btn.disabled=false; pv.disabled=false;
    }
  }catch(e){
    RM.running=false; say('[✗] 被拒绝：'+e.message);
    btn.disabled=false; pv.disabled=false;
  }
}
async function rmLoadResult(){
  const d=await jget('/api/runs/projection?project='+encodeURIComponent(RM.project)+'&run='+encodeURIComponent(RM.runRef));
  RM.proj=d.projection||{}; RM.view=d.view||{};
}
async function rmLoadReportView(){
  /* the READ-ONLY report projection (search trace + risk events) via the
     same identity loop as the export - never a second artifact channel */
  try{
    RM.rv=await jget('/api/runs/report-view?project='+encodeURIComponent(RM.project)+'&run='+encodeURIComponent(RM.runRef));
    RM.rvError=null;
  }catch(e){ RM.rv=null; RM.rvError=e.message; }
}
function traceTableHTML(tv){
  if(!tv) return '';
  const rows=tv.rows||[];
  if(!rows.length) return `<div class="fh">搜索轨迹无行（${esc(tv.note||'不适用')}）。</div>`;
  const cols=Object.keys(rows[0]);
  return `<div class="fh" style="margin-bottom:6px">来源 ${esc(tv.source)}｜显示 ${tv.displayed_rows} / 共 ${tv.total_rows} 行${tv.note?'｜'+esc(tv.note):''}</div>
    <div style="max-height:260px;overflow:auto"><table class="grid"><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>
    ${rows.map(r=>`<tr>${cols.map(c=>`<td class="r num">${esc(String(r[c]??''))}</td>`).join('')}</tr>`).join('')}</table></div>`;
}
function drawHeat(canvasId, rows){
  /* the gap picture: month x hour aggregation of REAL risk events only */
  const cv=document.getElementById(canvasId); if(!cv||!rows||!rows.length) return;
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=220;
  cv.width=w*dpr; cv.height=h*dpr;
  const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  const m={l:44,r:12,t:8,b:28};
  const cw=(w-m.l-m.r)/24, ch=(h-m.t-m.b)/12;
  const grid=Array.from({length:12},()=>new Array(24).fill(0));
  let parsed=0;
  rows.forEach(r=>{
    const raw=String(r.event_time??r.timestamp??'');
    const mo=/(\d{4})-(\d{2})/.exec(raw), hr=/(?:T|\s)(\d{2}):/.exec(raw);
    if(mo&&hr){ grid[+mo[2]-1][+hr[1]] += Math.abs(Number(r.deficiency_power_mw??0))||1; parsed++; }
  });
  if(!parsed) return;
  const max=Math.max(...grid.flat())||1;
  for(let mo=0;mo<12;mo++) for(let hr=0;hr<24;hr++){
    ctx.fillStyle=`rgba(181,70,47,${Math.min(grid[mo][hr]/max,1)})`;
    ctx.fillRect(m.l+hr*cw, m.t+(11-mo)*ch, cw-1, ch-1);
  }
  ctx.fillStyle='#6B675F'; ctx.font='10px sans-serif'; ctx.textAlign='center';
  for(let hr=0;hr<24;hr+=4) ctx.fillText(hr+'时', m.l+hr*cw+cw/2, h-m.b+14);
  ctx.textAlign='right';
  for(let mo=0;mo<12;mo+=2) ctx.fillText((mo+1)+'月', m.l-6, m.t+(11-mo)*ch+ch/2+3);
}
/* ---- 步骤 05：输出结果与报告（三层输出 + 真实导出） ---- */
function rmStep5(body){
  if(!RM.rv&&!RM.rvError&&RM.runRef) rmLoadReportView().then(()=>{ if(RM.step===5) renderRelMod(); });
  const v=RM.view||{}, p=RM.proj||{};
  const capRows=v.capacity_rows||[], commonRows=v.common_rows||[], convRows=v.convergence_criteria_rows||[];
  const metricRows=v.metric_rows||[], socRows=v.soc_rows||[], boundRows=v.boundary_rows||[];
  const capObj=v.capacity_view||{};
  const acc=capObj.accredited_capacity_mw;
  const frac=capObj.capacity_credit_fraction;
  const heroCls=isEFC()?'bstat heroefc':'bstat hero';
  const mainLabel=isEFC()?'EFC 等效完美容量':'ELCC 有效承载容量';
  const unresolved=(acc===null||acc===undefined);
  body.innerHTML=`
  <div class="bigstat">
    <div class="${heroCls}"><div class="k">${esc(mainLabel)}</div>
      <div class="v num">${unresolved?'未解析':fmt(acc,2)}</div><div class="u">${unresolved?'搜索未收敛：只显示区间，不给点值':'MW'}</div></div>
    <div class="bstat"><div class="k">容量信用率</div><div class="v num">${frac==null?'—':(frac*100).toFixed(2)+'%'}</div><div class="u">相对参考容量（冻结解析）</div></div>
    <div class="bstat"><div class="k">判据 / 口径</div><div class="v num" style="font-size:16px">${esc((metricRows[0]||['—'])[0])}</div><div class="u">候选资源 ${esc(String(RM.sel.group_id||''))}｜${esc(isEFC()?'EFC':'ELCC')}</div></div>
    <div class="bstat"><div class="k">过程可复核</div><div class="v num" style="font-size:16px">${esc(String(v.run_ref||RM.runRef||'').slice(0,8))}</div><div class="u">样本集 ${esc(String(v.scenario_bank_id||'').slice(0,8))}｜结果状态 ${esc(v.result_status_label||'')}</div></div>
  </div>
  ${v.evidence_error?`<div class="errbar">证据缺失（如实披露）：${esc(v.evidence_error)}</div>`:''}
  <div class="card"><div class="otabs" id="rmTabs">
    ${['最终输出数据清单','情景矩阵数据','过程与审计数据'].map((t,i)=>`<button class="${RM.otab===i?'on':''}" onclick="RM.otab=${i};renderRelMod()">${t}</button>`).join('')}
  </div><div id="rmTabBody"></div></div>
  <div class="card" style="display:flex;align-items:center;gap:14px">
    <div style="flex:1"><h3 style="margin-bottom:4px">报告导出</h3>
      <div class="fh">经既有报告链路生成 Excel + HTML（与本页数据同源）。<b>PDF / Word 未接入</b>：报告层当前只输出 Excel 与 HTML。</div></div>
    <a class="btn" href="/api/runs/report?project=${encodeURIComponent(RM.project)}&run=${encodeURIComponent(RM.runRef||'')}&format=xlsx">导出 Excel</a>
    <a class="btn pri" href="/api/runs/report?project=${encodeURIComponent(RM.project)}&run=${encodeURIComponent(RM.runRef||'')}&format=html">导出 HTML</a>
    <button class="btn" onclick="window.print()" title="使用浏览器打印为PDF（非受控制品，无哈希追溯）">打印为 PDF <span class="mono-note" style="font-size:10px">(D2 非受控)</span></button>
  </div>
  <div class="card"><h3>结果状态与后续动作</h3>
    <div class="fh">运行编号 ${esc(String(RM.runRef||''))}｜结果状态 ${esc(v.result_status_label||'未知')}（${esc(v.result_status||'')}）。</div>
    <div style="display:flex;gap:10px;margin-top:8px">
      <button class="btn" onclick="RESULTS_PROJECT=RM.project;RESULTS_RUN=RM.runRef;PAGE='results';rerender()">在「结果与报告」中打开</button>
      <button class="btn" onclick="copyProject(RM.project)">复制为新项目（改参数重算）</button></div></div>`;
  const tb=document.getElementById('rmTabBody');
  const kv=rows=>`<table class="grid">${(rows||[]).map(r=>`<tr><td style="width:300px" class="mono-note">${esc(Array.isArray(r)?r[0]:'')}</td><td class="r">${esc(Array.isArray(r)?String(r[1]):'')}</td></tr>`).join('')}</table>`;
  if(RM.otab===0){
    tb.innerHTML=`<div class="fh" style="margin-bottom:6px">本项目本次运行的单资源结果（真实）：</div>
      ${kv(capRows)}${kv(metricRows)}
      <div class="fh" style="margin:14px 0 6px"><b>${isEFC()?'分技术类别降额系数表':'多资源主结果表'}</b>（跨项目只读聚合，仅汇总各项目的正式结果）：</div>
      <div id="rm-agg" class="loading">读取聚合…</div>`;
    rmLoadAggregate();
  }else if(RM.otab===1){
    tb.innerHTML=`<div id="rm-matrix" class="loading">读取本项目研究批次…</div>
      <table class="grid" style="margin-top:10px"><tr><th>情景矩阵</th><th>状态</th><th>说明</th></tr>
      <tr><td>多年气象波动</td><td><span class="tag lock">未接入</span></td><td>需多种子稳健性批量测算（seed_replication 批次类型已留槽位，worker 未接线）</td></tr>
      <tr><td>组合交互系数</td><td><span class="tag lock">未接入</span></td><td>组合服务未接线</td></tr></table>`;
    rmLoadMatrix();
  }else{
    const rv=RM.rv||{};
    const auditRows=[['运行编号 run_id',rv.run_id||RM.runRef||''],['结果编号 result_id',rv.result_id||v.result_id||''],
      ['随机种子',rv.random_seed==null?'（配置未声明）':rv.random_seed],['风险收敛',rv.risk_convergence||'—'],['搜索收敛',rv.search_convergence||'—'],
      ['数据等级',rv.effective_data_grade||'—'],['主风险指标',rv.primary_risk_metric||'—'],['是否诊断态',rv.is_diagnostic?'是':'否'],
      ['使用边界',rv.use_boundary_code||'—']];
    const ev=rv.risk_events;
    tb.innerHTML=`<table class="grid">${auditRows.map(([k,val])=>`<tr><td style="width:300px" class="mono-note">${esc(k)}</td><td class="r">${esc(String(val))}</td></tr>`).join('')}</table>
      ${rv.use_boundary_warning?`<div class="hintbar warn" style="margin-top:8px">${esc(rv.use_boundary_warning)}</div>`:''}
      ${kv(commonRows)}${convRows.length?'<div class="fh" style="margin:8px 0 4px">收敛准则：</div>'+kv(convRows):''}
      ${socRows.length?'<div class="fh" style="margin:8px 0 4px">储能 SOC 口径（experimental/provisional）：</div>'+kv(socRows):''}
      ${boundRows.length?'<div class="fh" style="margin:8px 0 4px">边界与限制：</div>'+kv(boundRows):''}
      <div class="fh" style="margin:10px 0 4px">风险事件（缺口画像）：</div>
      ${RM.rvError?`<div class="errbar">报告视图不可读（如实披露）：${esc(RM.rvError)}</div>`
        :ev?`<div class="fh">来源 ${esc(ev.source)}｜显示 ${ev.displayed_rows} / 共 ${ev.total_rows} 行${ev.note?'｜'+esc(ev.note):''}</div>
           <canvas id="rmHeat" style="width:100%;display:block;margin:8px 0"></canvas>
           <div style="max-height:220px;overflow:auto"><table class="grid"><tr>${Object.keys((ev.rows||[])[0]||{}).map(c=>`<th>${esc(c)}</th>`).join('')}</tr>
           ${(ev.rows||[]).map(r=>`<tr>${Object.keys(r).map(c=>`<td class="r num">${esc(String(r[c]??''))}</td>`).join('')}</tr>`).join('')}</table></div>`
        :'<div class="fh">本次运行无风险事件明细（或该方法不产生事件表）。</div>'}
      ${traceTableHTML(rv.search_trace)?'<div class="fh" style="margin:10px 0 4px">搜索轨迹：</div>'+traceTableHTML(rv.search_trace):''}
      <details class="disc"><summary>完整结果投影（JSON，如实披露）</summary><div class="body" style="font-family:var(--mono);font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(p,null,1))}</div></details>`;
    if(ev&&(ev.rows||[]).length) requestAnimationFrame(()=>drawHeat('rmHeat',ev.rows));
  }
  rmDrawBracket(capObj,unresolved);
}
async function rmLoadAggregate(){
  /* P-AGG: the per-resource table is a READ projection across projects
     (one project = one formal result), never a batch run */
  const el=document.getElementById('rm-agg'); if(!el) return;
  try{
    const a=await jget('/api/results/aggregate?method='+encodeURIComponent(RM.method));
    if(!a.count){
      el.innerHTML=`<div class="hintbar">尚无可聚合的正式结果。规则：一个项目对应一次业务测算，因此<b>每类资源各建一个${esc(isEFC()?'EFC':'ELCC')}测算项目</b>并完成测算后，本表会自动汇总（含项目、运行编号与结果哈希血缘）。</div>`;
      return;
    }
    const head=isEFC()
      ? ['资源/类别','降额系数','等效可靠容量（MW）','参考容量（MW）','搜索括区（MW）','数据等级','结果状态','项目','血缘']
      : ['资源','ELCC 有效容量（MW）','容量信用率','参考容量（MW）','搜索括区（MW）','数据等级','结果状态','项目','血缘'];
    let html=a.cross_caliber?`<div class="hintbar warn">下列行跨 ${a.groups.length} 个口径组（样本集/数据等级不同），<b>不可直接横向比较</b>；已按口径分组显示。</div>`:'';
    html+=a.groups.map(g=>`
      <div class="fh" style="margin:8px 0 4px">口径组：样本集 ${esc(String(g.scenario_bank_id||'').slice(0,8)||'—')}｜数据等级 ${esc(String(g.effective_data_grade||'—'))}｜已解析 ${g.resolved_count} 行、未解析 ${g.unresolved_count} 行</div>
      <div style="overflow:auto"><table class="grid"><tr>${head.map(h=>`<th>${esc(h)}</th>`).join('')}</tr>
      ${g.rows.map(r=>{
        const frac=r.capacity_credit_fraction;
        const derating=frac==null?'—':(frac*100).toFixed(2)+'%';
        const acc=r.unresolved?'未解析':fmt(r.accredited_capacity_mw,2);
        const br=(r.search_bracket_low_mw==null&&r.search_bracket_high_mw==null)?'—'
          :`${fmt(r.search_bracket_low_mw,1)}—${fmt(r.search_bracket_high_mw,1)}`;
        return `<tr>
          <td>${esc(String(r.resource||'—'))}</td>
          <td class="r num">${isEFC()?derating:acc}</td>
          <td class="r num">${isEFC()?acc:derating}</td>
          <td class="r num">${r.reference_capacity_mw==null?'—':fmt(r.reference_capacity_mw,2)}</td>
          <td class="r num">${br}</td>
          <td>${esc(String(r.effective_data_grade||'—'))}</td>
          <td>${esc(String(r.result_status||'—'))}${r.unresolved?'（未收敛：不给点值）':''}</td>
          <td>${esc(r.project_name)}</td>
          <td class="mono-note">${esc(String(r.run_ref||'').slice(0,8))}${r.result_sha256?'｜'+esc(String(r.result_sha256).slice(0,8)):''}</td></tr>`;
      }).join('')}</table></div>`).join('');
    if((a.unreadable||[]).length){
      html+=`<div class="errbar" style="margin-top:8px">以下正式结果不可读（如实披露，未计入表格）：${a.unreadable.map(u=>`${esc(u.project_name)}／${esc(String(u.run_ref).slice(0,8))}：${esc(u.reason)}`).join('；')}</div>`;
    }
    html+=`<div class="fh" style="margin-top:8px">聚合口径说明：${isEFC()?'降额系数＝等效可靠容量 ÷ 参考容量（即容量信用率，按 EFC 口径命名）':'ELCC 有效容量来自各项目正式结果'}；本表只做只读汇总，不重新计算、不改写任何结果。</div>`;
    el.innerHTML=html;
  }catch(e){ el.innerHTML=`<div class="errbar">聚合读取失败：${esc(e.message)}</div>`; }
}

async function rmLoadMatrix(){
  const el=document.getElementById('rm-matrix'); if(!el) return;
  try{
    const d=await jget('/api/batches');
    const mine=(d.batches||[]).filter(b=>!b.invalid&&b.project_ref===RM.project
      &&(b.kind==='duration_curve'||b.kind==='penetration_curve'));
    if(!mine.length){
      el.innerHTML=`<div class="hintbar">本项目尚无持续时长/渗透率研究批次：储能 ELCC–持续时长曲线、渗透率曲线经<b>研究批次页</b>创建并逐点真实测算后，此处自动绘出真实曲线（失败点位如实留缺口）。</div>
        <div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="btn" onclick="PAGE='batches';rerender()">去研究批次页创建 →</button></div>`;
      return;
    }
    let html='';
    const details={};
    for(const b of mine){
      const det=await jget('/api/batches/detail?ref='+encodeURIComponent(b.batch_ref));
      details[b.batch_ref]=det;
      const pts=(det.points||[]).map(p=>{
        const params=p.params||{};
        const x=params.hours!=null?Number(params.hours):(params.factor!=null?Number(params.factor):null);
        const y=p.metric_value!=null?Number(p.metric_value):null;
        return {x, y, label:p.label||p.point_id, status:p.status, diagnosis:p.diagnosis, runRef:p.run_ref};
      }).filter(p=>p.x!=null);
      pts.sort((a,b)=>a.x-b.x);
      const xName=b.kind==='duration_curve'?'持续时长（h）':'装机倍数';
      const canvasId='rm-curve-'+String(b.batch_ref).replace(/[^a-zA-Z0-9]/g,'');
      html+=`<div class="card"><h3>${b.kind==='duration_curve'?'ELCC–持续时长':'ELCC–渗透率'} <span class="mono-note">批次 ${esc(String(b.batch_ref).slice(0,8))}｜${esc(b.method)}｜状态 ${esc(b.status||'')}</span></h3>
        <canvas id="${canvasId}" height="220" style="width:100%;display:block"></canvas>
        <div style="max-height:180px;overflow:auto;margin-top:8px"><table class="grid"><tr><th>点位</th><th class="r">${esc(xName)}</th><th class="r">有效容量（MW）</th><th>状态</th><th>诊断</th></tr>
        ${pts.map(p=>`<tr><td class="mono-note">${esc(p.label)}</td><td class="r num">${esc(String(p.x))}</td>
          <td class="r num">${p.y==null?'缺口（不给值）':fmt(p.y,2)}</td>
          <td><span class="tag ${p.status==='完成'?'ok':p.status==='失败'?'lock':'plan'}">${esc(p.status)}</span></td>
          <td>${p.diagnosis?`<span style="color:var(--warn)">${esc(p.diagnosis)}</span>`:'—'}</td></tr>`).join('')}</table></div></div>`;
    }
    el.innerHTML=html;
    requestAnimationFrame(()=>{ for(const b of mine){
      const canvasId='rm-curve-'+String(b.batch_ref).replace(/[^a-zA-Z0-9]/g,'');
      const det=details[b.batch_ref];
      if(det) rmDrawCurve(canvasId, det, b.kind==='duration_curve'?'h':'×');
    }});
  }catch(e){ el.innerHTML=`<div class="errbar">矩阵读取失败：${esc(e.message)}</div>`; }
}
function rmDrawCurve(canvasId, det, xUnit){
  const cv=document.getElementById(canvasId); if(!cv) return;
  const pts=(det.points||[]).map(p=>{
    const params=p.params||{};
    const x=params.hours!=null?Number(params.hours):(params.factor!=null?Number(params.factor):null);
    const y=p.metric_value!=null?Number(p.metric_value):null;
    return {x, y};
  }).filter(p=>p.x!=null).sort((a,b)=>a.x-b.x);
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=220;
  cv.width=w*dpr; cv.height=h*dpr;
  const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  const m={l:56,r:16,t:14,b:30};
  const xs=pts.map(p=>p.x), ys=pts.filter(p=>p.y!=null).map(p=>p.y);
  const xmin=Math.min(...xs,0), xmax=Math.max(...xs,1);
  const ymax=Math.max(...ys,1)*1.1;
  const X=v=>m.l+(v-xmin)/((xmax-xmin)||1)*(w-m.l-m.r);
  const Y=v=>h-m.b-(v/ymax)*(h-m.t-m.b);
  ctx.strokeStyle='#E4E2DC'; ctx.fillStyle='#6B675F'; ctx.font='10.5px sans-serif'; ctx.lineWidth=1;
  for(let g=0;g<=4;g++){ const v=ymax*g/4;
    ctx.beginPath(); ctx.moveTo(m.l,Y(v)); ctx.lineTo(w-m.r,Y(v)); ctx.stroke();
    ctx.textAlign='right'; ctx.fillText(v.toFixed(0),m.l-6,Y(v)+3); }
  for(const p of pts){ ctx.textAlign='center'; ctx.fillText(String(p.x)+xUnit, X(p.x), h-m.b+16); }
  let prev=null;
  for(const p of pts){
    if(p.y!=null){
      if(prev&&prev.y!=null){
        ctx.strokeStyle=isEFC()?'#7A4FA3':'#2052BB'; ctx.lineWidth=2;
        ctx.beginPath(); ctx.moveTo(X(prev.x),Y(prev.y)); ctx.lineTo(X(p.x),Y(p.y)); ctx.stroke();
      }
      ctx.fillStyle=isEFC()?'#7A4FA3':'#2052BB';
      ctx.beginPath(); ctx.arc(X(p.x),Y(p.y),4,0,Math.PI*2); ctx.fill();
    }else{
      ctx.strokeStyle='#B5462F'; ctx.lineWidth=2;
      ctx.beginPath(); ctx.arc(X(p.x),h-m.t-14,5,0,Math.PI*2); ctx.stroke();
      ctx.fillStyle='#B5462F'; ctx.textAlign='center';
      ctx.fillText('缺口',X(p.x),h-m.t-24);
    }
    prev=p;
  }
  if(!ys.length){
    ctx.fillStyle='#6B675F'; ctx.textAlign='center';
    ctx.fillText('尚无已完成点位（失败/未收敛点位如实留缺口，不插值）',w/2,h/2);
  }
}

function rmDrawBracket(cap,unresolved){
  const cv=document.getElementById('rmChart'); if(!cv) return;
  const lo=cap.search_bracket_low_mw, hi=cap.search_bracket_high_mw;
  const capTitle=document.getElementById('rmBracket');
  const note=document.getElementById('rm-bracket');
  if(note) note.innerHTML = `搜索括区：${lo==null?'—':fmt(lo,2)} — ${hi==null?'—':fmt(hi,2)} MW｜有效容量：${unresolved?'未解析（不给点值）':fmt(cap.accredited_capacity_mw,2)+' MW'}｜插值方式：${esc(String(cap.interpolation_method||'—'))}｜数据等级：${esc(String(cap.effective_data_grade||'—'))}`;
  if(lo==null&&hi==null) return;
  cv.style.display='block';
  const cap2=document.getElementById('rmChartCap'); if(cap2){cap2.style.display='block';
    cap2.textContent=unresolved?'搜索未解析：仅显示区间（红色虚线为判据口径，由所选风险指标承担）':'区间与点值均来自真实运行结果';}
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=260;
  cv.width=w*dpr; cv.height=h*dpr;
  const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  const m={l:56,r:16,t:16,b:34};
  const span=(hi==null?1:hi)-(lo==null?0:lo)||1;
  const xmin=(lo==null?0:lo)-span*0.4, xmax=(hi==null?1:hi)+span*0.4;
  const X=v=>m.l+(v-xmin)/(xmax-xmin)*(w-m.l-m.r);
  ctx.strokeStyle='#E4E2DC'; ctx.fillStyle='#6B675F'; ctx.font='10.5px sans-serif'; ctx.lineWidth=1;
  [xmin,(xmin+xmax)/2,xmax].forEach(v=>{ ctx.beginPath(); ctx.moveTo(X(v),m.t); ctx.lineTo(X(v),h-m.b); ctx.stroke();
    ctx.textAlign='center'; ctx.fillText(v.toFixed(0)+' MW',X(v),h-m.b+14); });
  ctx.beginPath(); ctx.moveTo(m.l,h/2); ctx.lineTo(w-m.r,h/2); ctx.stroke();
  ctx.textAlign='right'; ctx.fillText('风险判据水平（示意轴）',w-m.r,h/2-6);
  if(lo!=null&&hi!=null){
    ctx.fillStyle='rgba(46,117,182,.18)'; ctx.fillRect(X(lo),m.t,X(hi)-X(lo),h-m.t-m.b);
    ctx.strokeStyle=isEFC()?'#7A4FA3':'#2052BB'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(X(lo),h/2); ctx.lineTo(X(hi),h/2); ctx.stroke();
    [[lo,-1],[hi,1]].forEach(([v,d])=>{ ctx.beginPath(); ctx.moveTo(X(v),h/2); ctx.lineTo(X(v)+7*d,h/2-4); ctx.lineTo(X(v)+7*d,h/2+4); ctx.closePath(); ctx.fillStyle=ctx.strokeStyle; ctx.fill(); });
    ctx.font='bold 11px ui-monospace,monospace'; ctx.textAlign='center'; ctx.fillStyle='#33312C';
    ctx.fillText('括区 '+lo.toFixed(1)+'—'+hi.toFixed(1)+' MW',(X(lo)+X(hi))/2,h/2-12);
  }
  if(!unresolved&&cap.accredited_capacity_mw!=null){
    const v=cap.accredited_capacity_mw;
    ctx.strokeStyle='#B5462F'; ctx.setLineDash([6,4]); ctx.beginPath(); ctx.moveTo(X(v),m.t); ctx.lineTo(X(v),h-m.b); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle='#B5462F'; ctx.font='bold 11px ui-monospace,monospace'; ctx.textAlign='left';
    ctx.fillText((isEFC()?'EFC ':'ELCC ')+v.toFixed(1)+' MW',X(v)+6,m.t+12);
  }
}
/* ============ N2c 研究批次（B1：矩阵与标定的批次流） ============ */
