let SEL_BATCH = null, AUTH_BY = localStorage.getItem('auth-by') || '';
function viewBatches(){
  return `
  <div class="ptitle">研究批次 <span class="tag plan">技术视图</span></div>
  <div class="psub">面向高级操作的<b>技术视图</b>（工程口径原样保留）：日常业务测算请走首页六步流程；本页用于矩阵/标定批次的台账级操作。一个批次＝一份多点位研究交付（储能时长矩阵／渗透率矩阵／基准标定）。派生数据按批次级授权自动审批冻结（每份记录规则、参数、父版本哈希与授权人）；失败点位保留诊断并显示为缺口。</div>
  <div class="card"><h3>${SEL_BATCH?'批次详情':'创建研究批次'}</h3><div id="bx-main" class="loading">读取中…</div></div>
  <div class="card"><h3>全部批次</h3><div id="bx-list" class="loading">读取中…</div></div>`;
}
function loadBatches(){
  jget('/api/batches').then(d=>{
    const list=document.getElementById('bx-list');
    const rows=(d.batches||[]).filter(b=>!b.invalid);
    list.innerHTML = rows.length ? rows.map(b=>`
      <div class="rowitem"><div class="tt">${esc(b.label||b.kind)}<small>${esc(b.kind)}｜${esc(b.method)}｜<span class="tag ${b.status==='完成'?'ok':'plan'}">${esc(b.status||'')}</span>｜基线 ${esc(String(b.baseline_batch_id||'').slice(0,8))}｜${(b.points||[]).length} 点位</small></div>
      <button class="btn ${SEL_BATCH===b.batch_ref?'pri':''}" onclick="SEL_BATCH='${esc(b.batch_ref)}';loadBatches()">${SEL_BATCH===b.batch_ref?'当前查看':'查看详情'}</button></div>`).join('')
      : '<span class="loading">尚无研究批次：在上方创建第一个。</span>';
    if(SEL_BATCH) rmLoadDetail(); else rmCreateForm();
  }).catch(e=>{ document.getElementById('bx-list').innerHTML=`<div class="errbar">${esc(e.message)}</div>`; });
}
function rmCreateForm(){
  const relProjects = ()=>'（读取中）';
  document.getElementById('bx-main').innerHTML=`
  <div class="fgrid">
    <div class="fld"><label><b>批次类型</b></label><select id="bx-kind">
      <option value="duration_curve">储能时长曲线（2/4/6/8h，DR-03）</option>
      <option value="penetration_curve">渗透率曲线（DR-04/05）</option>
      <option value="calibration">基准标定（CAL，负荷二分）</option></select></div>
    <div class="fld"><label><b>测算方法</b></label><select id="bx-method"><option value="elcc">ELCC</option><option value="efc">EFC</option><option value="calibration" disabled>adequacy（标定用）</option></select>
      <div class="fh">标定批次的点位运行方法为充裕度；方法选择仅对曲线批次生效。</div></div>
    <div class="fld"><label><b>基线项目</b>（可靠性项目）</label><select id="bx-proj"><option value="">（读取中…）</option></select>
      <div class="fh">必须已有已冻结的数据批次。</div></div>
    <div class="fld"><label><b>基线冻结批次 ID</b></label><input id="bx-base" placeholder="已冻结批次的 import_batch_id（见基础数据管理）"></div>
    <div class="fld"><label><b>批次授权人</b>（DR-01a/b 必填，记录在案）</label><input id="bx-auth" value="${esc(AUTH_BY)}" placeholder="您的署名"></div>
    <div class="fld"><label>批次名称（可选）</label><input id="bx-label" placeholder="如：储能时长矩阵（教学基线）"></div>
  </div>
  <div class="fld"><label><b>点位参数</b>（每行一个点位：数值）</label>
    <div class="fh" id="bx-pts-hint">时长矩阵：2 / 4 / 6 / 8；渗透率矩阵：倍数如 1.0 / 1.5 / 2.0；标定：每行一个初始探测点（系统按二分自动补轮，≤8 轮）。</div>
    <input id="bx-pts" value="${document.getElementById('bx-kind')&&document.getElementById('bx-kind').value==='calibration'?'-2.0\\n2.5':'2\\n4\\n6\\n8'}" style="width:100%"></div>
  <div id="bx-crit"></div>
  <button class="btn pri" onclick="rmCreateBatch()">创建批次</button>`;
  jget('/api/projects').then(d=>{
    const sel=document.getElementById('bx-proj');
    const rel=(d.projects||[]).filter(p=>p.kind==='reliability'&&!p.invalid);
    sel.innerHTML=rel.length?rel.map(p=>`<option value="${esc(p.project_dir)}">${esc(p.name)}</option>`).join(''):'<option value="">（尚无可靠性项目）</option>';
  });
  const kindSel=document.getElementById('bx-kind');
  kindSel.onchange=()=>{
    const k=kindSel.value;
    document.getElementById('bx-pts').value = k==='calibration'?'-2.0\n2.5':(k==='penetration_curve'?'1.0\n1.5\n2.0':'2\n4\n6\n8');
  };
  const crit=document.getElementById('bx-crit');
  const renderCrit=()=>{
    if(kindSel.value!=='calibration'){ crit.innerHTML=''; return; }
    crit.innerHTML=`<div class="card"><h3>标定判据（CAL-01/02/03）</h3>
      <div class="fgrid">
        <div class="fld"><label><b>判据指标</b></label><select id="bx-metric">
          ${[['lole_days_per_year','LOLE（天/年）'],['lolh_hours_per_year','LOLH（小时/年）'],['eue_mwh_per_year','EUE（MWh/年）'],['neue_ppm','NEUE（ppm）']].map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('')}</select></div>
        <div class="fld"><label><b>目标值</b>（必填，无默认）</label><input class="num" id="bx-target" placeholder="如 0.1 或 3"></div>
        <div class="fld"><label>容差带（%）</label><input class="num" id="bx-tol" data-min="0.5" data-max="49" step="0.5" value="5"></div>
      </div>
      <div class="hintbar warn">目标值必须由您显式给定（原型的 0.1 天/年只是示例，不是本工具默认）；搜索区间 [-5%, +10%]、最多 8 轮、未收敛只给区间不给点值。</div></div>`;
  };
  renderCrit(); kindSel.addEventListener('change', renderCrit);
}
async function rmCreateBatch(){
  const kind=document.getElementById('bx-kind').value;
  const auth=document.getElementById('bx-auth').value.trim();
  if(!auth){ flash('DR-01a/b：请填写批次授权人'); return; }
  AUTH_BY=auth; localStorage.setItem('auth-by', auth);
  const ptsRaw=document.getElementById('bx-pts').value.split(/\n+/).map(x=>x.trim()).filter(Boolean);
  if(!ptsRaw.length){ flash('请至少填写一个点位'); return; }
  const rule= kind==='duration_curve'?'storage_duration': kind==='penetration_curve'?'renewable_scale':'load_scale';
  const key= kind==='duration_curve'?'hours': kind==='penetration_curve'?'factor':'scale_percent';
  const points=ptsRaw.map((v,i)=>({point_id:`p${i+1}`, label:String(v),
    params: {rule_id: rule, [key]: Number(v)}}));
  const body={ request_id:'web-batch-'+(crypto.randomUUID?crypto.randomUUID():Date.now()),
    kind, method: kind==='calibration'?'adequacy':document.getElementById('bx-method').value,
    project_ref: document.getElementById('bx-proj').value,
    baseline_batch: document.getElementById('bx-base').value.trim(),
    points, axis:{name: kind==='duration_curve'?'储能时长':kind==='penetration_curve'?'新能源装机倍数':'负荷缩放'},
    label: document.getElementById('bx-label').value.trim() };
  if(kind==='calibration'){
    body.criterion={ metric: document.getElementById('bx-metric').value,
      target: document.getElementById('bx-target').value,
      tolerance_percent: Number(document.getElementById('bx-tol').value)||5 };
  }
  try{
    const m=await jpost('/api/batches', body);
    flash('批次已创建：'+m.batch_ref.slice(0,8)+'（'+m.status+'）');
    SEL_BATCH=m.batch_ref; loadBatches();
  }catch(e){ flash('创建被拒绝：'+e.message); }
}
function rmLoadDetail(){
  const main=document.getElementById('bx-main');
  jget('/api/batches/detail?ref='+encodeURIComponent(SEL_BATCH)).then(m=>{
    const rows=(m.points||[]).map(p=>{
      const d=p.derivation||{};
      const actions=[];
      if(p.status==='待派生'||p.status==='失败') actions.push(`<button class="btn pri" onclick="rmDerivePoint('${esc(p.point_id)}')">派生并自动冻结（DR-01a/b）</button>`);
      if(p.status==='待运行') actions.push(`<button class="btn pri" onclick="rmRunPoint('${esc(p.point_id)}')">发起${esc(m.method)}运行</button>`);
      if(p.status==='待运行'&&(p.request_ref||p.run_ref)) actions.push(`<button class="btn" onclick="rmFinalize('${esc(p.point_id)}','${esc(p.request_ref||p.run_ref)}')">收尾读取结果</button>`);
      if(p.run_ref) actions.push(`<button class="btn" onclick="openResult('${esc(m.project_ref)}','reliability','${esc(p.run_ref)}')">查看运行</button>`);
      return `<tr><td><b>${esc(p.point_id)}</b> ${esc(p.label||'')}</td>
        <td>${esc(JSON.stringify(p.params))}</td>
        <td><span class="tag ${p.status==='完成'?'ok':p.status==='失败'?'lock':'plan'}">${esc(p.status)}</span></td>
        <td class="mono-note">${esc(String(p.import_batch_id||'').slice(0,8))}</td>
        <td class="mono-note">${esc(String(p.run_ref||'').slice(0,8))}${p.metric_value!=null?'｜'+esc(String(p.metric_value)):''}</td>
        <td>${p.diagnosis?`<span style="color:var(--warn)">${esc(p.diagnosis)}</span>`:'—'}</td>
        <td>${actions.join(' ')||'—'}</td></tr>`;
    }).join('');
    main.innerHTML=`
      <div class="lockbar"><b>批次 ${esc(String(m.batch_ref).slice(0,8))}</b>｜${esc(m.kind)}｜${esc(m.method)}｜状态 <span class="tag ${m.status==='完成'?'ok':'plan'}">${esc(m.status)}</span>｜基线批次 ${esc(String(m.baseline_batch_id).slice(0,8))}${m.label?'｜'+esc(m.label):''}</div>
      ${m.criterion&&m.criterion.metric?`<div class="hintbar">标定判据：${esc(String(m.criterion.metric))} 目标 ${esc(String(m.criterion.target))} ±${esc(String(m.criterion.tolerance_percent))}%（CAL-02：目标值为您显式给定，无默认）</div>`:''}
      <table class="grid"><tr><th>点位</th><th>参数</th><th>状态</th><th>派生批次</th><th>运行</th><th>诊断</th><th>动作</th></tr>${rows}</table>
      ${m.kind==='calibration'?`<div style="margin-top:10px"><button class="btn" onclick="rmCalibNext()">读取标定决策（下一探测点／收敛判定）</button></div><div id="bx-calib"></div>`:''}
      <div class="fh" style="margin-top:10px">授权人：${esc(AUTH_BY||'（未设置，派生前请在创建表单填写）')}。派生数据按 DR-01a/b 自动审批冻结，逐份记录规则、参数与父版本哈希。</div>`;
  }).catch(e=>{ main.innerHTML=`<div class="errbar">${esc(e.message)}</div>`; });
}
async function rmDerivePoint(pointId){
  if(!AUTH_BY){ AUTH_BY=prompt('DR-01a/b 需要记录批次授权人，请输入您的署名：')||''; if(!AUTH_BY) return; localStorage.setItem('auth-by',AUTH_BY); }
  flash('派生中：生成数据→自动审批冻结→生成样本集（复用基线根种子）…');
  try{
    const r=await jpost('/api/batches/derive-point',{batch_ref:SEL_BATCH, point_id:pointId,
      authorized_by:AUTH_BY, declared_grade:'G0_teaching'});
    flash('点位已派生并冻结，样本集 '+String(r.bank_samples||'')+' 样本（根种子 '+String(r.bank_root_seed??'—')+'）');
    rmLoadDetail();
  }catch(e){ flash('派生被拒绝：'+e.message); rmLoadDetail(); }
}
async function rmRunPoint(pointId){
  try{
    const r=await jpost('/api/batches/point-run',{batch_ref:SEL_BATCH, point_id:pointId});
    flash('运行已提交：'+String(r.request_ref).slice(0,8)+'（后台执行，完成后点“收尾读取结果”）');
    rmLoadDetail();
  }catch(e){ flash('发起被拒绝：'+e.message); }
}
async function rmFinalize(pointId, runRef){
  try{
    const r=await jpost('/api/batches/point-finalize',{batch_ref:SEL_BATCH, point_id:pointId, run_ref:runRef});
    if(r.finalized===false){ flash('运行尚未结束（'+r.status+'）：稍后再收尾'); return; }
    flash(r.usable===false?'该点位未收敛/失败：按 DR-09 保留为缺口':'点位完成：'+String(r.metric_value??''));
    rmLoadDetail();
  }catch(e){ flash('收尾失败：'+e.message); }
}
async function rmCalibNext(){
  const el=document.getElementById('bx-calib'); if(!el) return;
  try{
    const r=await jget('/api/batches/calibration-next?ref='+encodeURIComponent(SEL_BATCH));
    const d=r.decision||{};
    let html=`<div class="hintbar"><b>决策：${
      d.kind==='probe'?'下一探测点 负荷缩放 '+d.scale_percent+'%':
      d.kind==='converged'?'已收敛：基准负荷水平 '+Number(d.scale_percent).toFixed(2)+'%':
      d.kind==='not_converged'?'未收敛：区间 ['+d.interval_percent.map(x=>Number(x).toFixed(2)).join(', ')+']%（不给点值）':
      '区间耗尽'}</b><br><span class="mono-note">已用轮次 ${d.rounds_used??0}｜可用 ${d.usable_rounds??0}${d.note?'｜'+esc(d.note):''}</span></div>`;
    html+=`<table class="grid"><tr><th>点位</th><th class="r">负荷缩放</th><th class="r">判据值</th><th>判定</th><th>诊断</th></tr>
      ${(r.history||[]).map(h=>`<tr><td class="mono-note">${esc(String(h.scale_percent??''))}%</td><td class="r num">${h.metric_value==null?'—':fmt(h.metric_value,4)}</td>
      <td>${esc(h.state==='usable'?(h.verdict==='within_band'?'命中容差带':h.verdict==='too_loose'?'偏松（可加负荷）':'偏紧（需减负荷）'):'不可用')}</td>
      <td>${esc(h.diagnosis||'—')}</td></tr>`).join('')}</table>
      <div class="fh" style="margin-top:6px">${esc(r.gap_note||'')}</div>`;
    el.innerHTML=html;
  }catch(e){ el.innerHTML=`<div class="errbar">${esc(e.message)}</div>`; }
}

/* ============ 演示口径页（保持V0.5原型；不接真实数据） ============ */
