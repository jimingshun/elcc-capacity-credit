function viewResults(){
  return `
  <div class="ptitle">结果与报告</div>
  <div class="psub">同一项目下的多次运行为「历史版本」，只读、不重算；每条以 <span class="num">项目＋业务域＋运行标识</span> 唯一确定，下载报告始终绑定同一版本。</div>
  <div class="card">
    <div class="fgrid">
      <div class="fld"><label><b>选择项目</b></label><select id="res-proj" onchange="RESULTS_PROJECT=this.value;RESULTS_RUN=null;loadResultsPage()"></select></div>
      <div class="fld"><label>复制为新项目（已完成项目需改参数重新测算时）</label><button class="btn" onclick="copyProject(RESULTS_PROJECT)">复制当前项目（记录来源）</button>
        <div class="fh">复制会创建同类型新项目并预填参数；原项目与其正式结果保持不变。</div></div>
    </div>
    <div id="res-rows"></div>
  </div>
  <div id="res-detail"></div>`;
}
async function loadResultsPage(){
  try{
    const projects = (await jget('/api/projects')).projects;
    const sel = document.getElementById('res-proj');
    sel.innerHTML = projects.map(p=>`<option value="${esc(p.project_dir)}">${esc(p.name)}</option>`).join('');
    if(RESULTS_PROJECT && projects.some(p=>p.project_dir===RESULTS_PROJECT)) sel.value = RESULTS_PROJECT;
    else if(!RESULTS_PROJECT) RESULTS_PROJECT = sel.value;
    if(!RESULTS_PROJECT){ document.getElementById('res-rows').innerHTML = '<span class="loading">尚无项目。</span>'; return; }
    const listing = await jget('/api/results?project=' + encodeURIComponent(RESULTS_PROJECT));
    const rows = (listing.rows || []).slice().reverse(); // newest first = 版本1为最新
    document.getElementById('res-rows').innerHTML = rows.length ? `<div class="psub">项目「${esc(listing.project_name)}」的历史版本（含失败与未发布，只读，不重算；版本1为最新）：</div>` +
      rows.map((r,idx)=>`<div class="rowitem"><div class="tt">版本 <span class="num">${rows.length-idx}</span>｜${esc(r.method_label)}<small>${esc(r.status_label)}｜${esc(r.created_at_label)}｜追溯 <span class="num">${esc(String(r.run_ref).slice(0,8))}</span></small></div>
        ${r.invalid?'<span class="tag lock">损坏</span>':`<button class="btn" onclick="RESULTS_RUN='${esc(r.run_ref)}';showResult('${esc(r.domain)}','${esc(r.run_ref)}')">查看详情</button>`}</div>`).join('')
      : '<span class="loading">该项目尚无历史版本。</span>';
    document.getElementById('res-detail').innerHTML = '';
    if(RESULTS_RUN){
      const row = rows.find(r=>r.run_ref===RESULTS_RUN);
      if(row) showResult(row.domain, RESULTS_RUN);
    }
  }catch(e){ document.getElementById('res-rows').innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function showResult(domain, runRef){
  const el = document.getElementById('res-detail');
  if(domain === 'settlement'){
    // 结算域详情（收尾②）：数据源＝年度汇总正式结果（只读，不重算）
    el.innerHTML = '<div class="loading">读取结算结果…</div>';
    try{
      const d = await jget('/api/settlement/summary?project=' + encodeURIComponent(RESULTS_PROJECT));
      el.innerHTML = renderSettlementResult(runRef, d);
    }catch(e){ el.innerHTML = `<div class="card"><div class="errbar">结果查询失败：${esc(e.message)}</div></div>`; }
    return;
  }
  if(domain !== 'cost'){
    el.innerHTML = '<div class="loading">读取可靠性结果…</div>';
    try{
      const data = await jget('/api/runs/projection?project=' + encodeURIComponent(RESULTS_PROJECT) + '&run=' + encodeURIComponent(runRef));
      el.innerHTML = renderReliabilityResult(runRef, data);
    }catch(e){ el.innerHTML = `<div class="card"><div class="errbar">结果查询失败：${esc(e.message)}</div></div>`; }
    return;
  }
  el.innerHTML = '<div class="loading">读取结果…</div>';
  try{
    const rec = await jget('/api/cost/projection?project=' + encodeURIComponent(RESULTS_PROJECT) + '&run=' + encodeURIComponent(runRef));
    if(rec.manifest && rec.manifest.status !== 'completed'){
      el.innerHTML = `<div class="card"><div class="errbar">该记录状态：${esc(rec.manifest.status)}——未完成的结果如实显示，不冒充成功。</div></div>`;
      return;
    }
    el.innerHTML = renderCostResult(runRef, rec);
  }catch(e){ el.innerHTML = `<div class="card"><div class="errbar">结果查询失败：${esc(e.message)}</div></div>`; }
}
function renderReliabilityResult(runRef, data){
  // P-D: the typed VIEW (method-specific KPI rows with units) is the
  // authority - raw projection scalars are never promoted to KPIs
  const p = data.projection || {}, v = data.view || {};
  const meta = `<div class="psub">运行编号：<span class="num">${esc(String(runRef).slice(0,8))}</span>（只读，不重算）｜生命周期：${esc(v.lifecycle_status||p.lifecycle_status||'未知')}｜结果状态：${esc(v.result_status_label||p.result_status||'未知')}｜方法：${esc(v.method||p.method||'—')}</div>`;
  const evidence = (p.evidence_error || v.evidence_error) ? `<div class="errbar">证据缺失（如实披露）：${esc(p.evidence_error||v.evidence_error)}</div>` : '';
  const metricCards = (v.metric_rows||[]).slice(0,4).map(([label,value])=>[label,value,'']);
  const kv = rows => `<table class="grid">${(rows||[]).map(r=>`<tr><td style="width:280px" class="mono-note">${esc(Array.isArray(r)?r[0]:'')}</td><td class="r">${esc(Array.isArray(r)?String(r[1]):'')}</td></tr>`).join('')}</table>`;
  const capacityBlock = (v.capacity_rows&&v.capacity_rows.length)
    ? `<details class="disc" open><summary>容量价值（专属卡片，未解析状态如实显示）</summary><div class="body">${kv(v.capacity_rows)}</div></details>` : '';
  const socBlock = (v.soc_rows&&v.soc_rows.length)
    ? `<details class="disc"><summary>储能SOC口径（experimental/provisional）</summary><div class="body">${kv(v.soc_rows)}</div></details>` : '';
  const commonBlock = (v.common_rows&&v.common_rows.length)
    ? `<details class="disc" open><summary>运行与收敛明细</summary><div class="body">${kv(v.common_rows)}${v.convergence_criteria_rows&&v.convergence_criteria_rows.length?'<div class="fh" style="margin-top:6px">收敛准则：</div>'+kv(v.convergence_criteria_rows):''}</div></details>` : '';
  const interpBlock = (v.metric_interpretation_rows&&v.metric_interpretation_rows.length)
    ? `<details class="disc"><summary>指标解读（规则化生成）</summary><div class="body">${kv(v.metric_interpretation_rows)}</div></details>` : '';
  const boundaryBlock = (v.boundary_rows&&v.boundary_rows.length)
    ? `<details class="disc"><summary>边界与限制（限制说明）</summary><div class="body">${kv(v.boundary_rows)}</div></details>` : '';
  setTimeout(()=>{ loadResultRiskEvents(RESULTS_PROJECT, runRef); loadResultWeatherYears(RESULTS_PROJECT, runRef, p); },0);
  return `<div class="card"><h3>${esc(({adequacy:'系统可靠性评估',elcc:'ELCC测算',efc:'EFC测算'})[v.method]||'可靠性测算')}结果 <span class="tag ${v.result_status_tone==='success'?'ok':'lock'}">${esc(v.result_status_label||'状态未知')}</span></h3>${meta}${evidence}
    ${metricCards.length?kpisHtml(metricCards):''}
    ${capacityBlock}
    ${commonBlock}
    ${socBlock}
    ${interpBlock}
    ${boundaryBlock}
    <details open style="border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:8px 12px"><summary style="cursor:pointer"><b>分年看风险：每年天气各带来多少缺电风险</b></summary><div class="body" id="rv-wyears"><span class="loading">读取中…</span></div></details>
    <details class="disc"><summary>风险事件（缺口画像，来自报告层）</summary><div class="body" id="rv-events"><span class="loading">读取中…</span></div></details>
    <details class="disc"><summary>完整投影（JSON，如实披露）</summary><div class="body" style="font-family:var(--mono);font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(p, null, 1))}</div></details>
  </div>`;
}
async function loadResultWeatherYears(projectRef, runRef, proj){
  // W3 分年看风险（三切片计划 §4.3）：数据源＝report-view 逐年表；
  // 渲染时对账 Σ(占比×该年风险) 与页面加权总值（1e-9），不一致如实报错
  const el=document.getElementById('rv-wyears'); if(!el) return;
  try{
    const rv=await jget('/api/runs/report-view?project='+encodeURIComponent(projectRef)+'&run='+encodeURIComponent(runRef));
    const wym=rv.weather_year_metrics;
    if(!wym){ el.innerHTML='<div class="fh">该运行早于逐年数据上线，无分年视图（如实缺省，不装空表）。</div>'; return; }
    const rows=(wym.rows||[]).map(r=>({
      year:r.weather_year, role:String(r.role||''),
      weight:(r.weight===''||r.weight===null||r.weight===undefined)?null:Number(r.weight),
      mean:(r.primary_metric_mean===''||r.primary_metric_mean===null||r.primary_metric_mean===undefined)?null:Number(r.primary_metric_mean)}));
    if(!rows.length){ el.innerHTML='<div class="fh">逐年表为空（如实缺省）。</div>'; return; }
    const metric=String(rv.primary_risk_metric||'lole_days_per_year');
    const colLabel=({lole_days_per_year:'该年缺电风险（天/年）',lolh_hours_per_year:'该年缺电小时（小时/年）',eue_mwh_per_year:'该年缺供电量（MWh/年）',neue_ppm:'该年缺供电量占比（ppm）'})[metric]||'该年风险值';
    let agg=null;
    const m=(proj&&proj.metrics)||null, cap=(proj&&proj.capacity_view)||null;
    if(m&&m[metric]!=null&&Number.isFinite(Number(m[metric]))) agg=Number(m[metric]);
    else if(cap&&cap.base_risk!=null&&Number.isFinite(Number(cap.base_risk))) agg=Number(cap.base_risk);
    const baseRows=rows.filter(r=>r.role==='probability_baseline');
    const sum=baseRows.reduce((a,r)=>a+(r.weight||0)*(r.mean||0),0);
    let recon='';
    if(agg===null) recon=`<div class="fh">页面投影未带加权总值，无法对账（如实说明）；合计行按分年占比加总显示。</div>`;
    else if(Math.abs(sum-agg)>1e-9) recon=`<div class="errbar"><b>分年对不上总账（如实报错，不装正常）：</b>分年加总 ${sum}，页面显示的加权总风险 ${agg}。请以运行记录为准。</div>`;
    const trs=rows.map(r=>{
      const stress=r.role==='stress_test';
      const share=r.weight===null?'—':rmFmtPct(r.weight);
      const contrib=stress?'不参与平均':((r.weight===null||r.mean===null)?'—':esc(fmt(r.weight*r.mean,4)));
      return `<tr><td class="num">${esc(String(r.year))}</td><td class="r">${share}</td><td>${stress?'压力考验年':'参与平均'}</td><td class="r num">${r.mean===null?'—':esc(fmt(r.mean,4))}</td><td class="r num">${contrib}</td></tr>`;
    }).join('');
    el.innerHTML=`${recon}
      <div class="fh" style="margin:6px 0">总风险不是某一年算出来的，而是各年按占比加权平均——这张表告诉你哪一年把风险拉高、哪一年拉低。</div>
      <table class="grid"><tr><th>年份</th><th class="r">占比</th><th>是否压力考验年</th><th class="r">${esc(colLabel)}</th><th class="r">对总风险的贡献</th></tr>
      ${trs}
      <tr><td colspan="3"><b>合计（加权）＝总风险</b></td><td class="r num" colspan="2"><b>${esc(fmt(sum,4))}</b>${agg!==null?`（页面总风险 ${esc(fmt(agg,4))}）`:''}</td></tr></table>
      <div class="fh" style="margin-top:4px">来源 ${esc(String(wym.source))}｜显示 ${esc(String(wym.displayed_rows))} / 共 ${esc(String(wym.total_rows))} 行${wym.note?'｜'+esc(wym.note):''}</div>`;
  }catch(e){ el.innerHTML=`<div class="errbar">逐年视图不可读（如实披露）：${esc(e.message)}</div>`; }
}
async function loadResultRiskEvents(projectRef, runRef){
  const el=document.getElementById('rv-events'); if(!el) return;
  try{
    const rv=await jget('/api/runs/report-view?project='+encodeURIComponent(projectRef)+'&run='+encodeURIComponent(runRef));
    const ev=rv.risk_events;
    if(!ev){ el.innerHTML='<div class="fh">本次运行无风险事件明细（或该方法不产生事件表）。</div>'; return; }
    // Q-D1：缺供持续时间汇总（分年聚合＋跨年加权，
    // 与四指标同一套年份占比；压力年不参与加权）
    const ds=rv.risk_event_duration_summary;
    let durationBlock='';
    if(ds && ds.available){
      const wy=ds.weighted_event_count_per_year!=null;
      durationBlock=`<div class="fh" style="margin:8px 0 4px"><b>缺供持续时间（连续小时算同一场，不跨年拼接）</b></div>
      <table class="grid"><tr><th>年份</th><th class="r">事件数</th><th class="r">总缺供小时</th><th class="r">平均持续（h）</th><th class="r">最长（h）</th></tr>
        ${ds.per_year.map(r=>`<tr><td class="num">${esc(String(r.scenario_id||'').replace('weather_year_',''))}</td><td class="r num">${r.event_count}</td><td class="r num">${r.total_hours}</td><td class="r num">${esc(String(r.avg_duration_hours??'—'))}</td><td class="r num">${r.max_duration_hours}</td></tr>`).join('')}
        <tr><td colspan="2"><b>全样本最长一次</b></td><td class="r num" colspan="3"><b>${ds.max_duration_hours} 小时</b>（共 ${ds.total_event_count} 场事件）</td></tr>
        ${wy?`<tr><td colspan="2"><b>跨年加权（期望/年）</b></td><td class="r num" colspan="3">事件 ${fmt(ds.weighted_event_count_per_year,2)} 场｜缺供 ${fmt(ds.weighted_loss_hours_per_year,2)} 小时｜平均一场 ${fmt(ds.weighted_avg_duration_hours,2)} 小时</td></tr>`:''}
      </table>
      <div class="fh" style="margin-top:4px">${esc(ds.weighted_note||'')}</div>`;
    } else if(ds){
      durationBlock=`<div class="fh">缺供持续时间汇总不可得：${esc(ds.reason||'')}</div>`;
    }
    const cols=Object.keys((ev.rows||[])[0]||{});
    el.innerHTML=`<div class="fh">来源 ${esc(ev.source)}｜显示 ${ev.displayed_rows} / 共 ${ev.total_rows} 行${ev.note?'｜'+esc(ev.note):''}｜随机种子 ${esc(String(rv.random_seed??'未声明'))}</div>
      ${durationBlock}
      <canvas id="rvHeat" style="width:100%;display:block;margin:8px 0"></canvas>
      <div style="max-height:240px;overflow:auto"><table class="grid"><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>
      ${(ev.rows||[]).map(r=>`<tr>${cols.map(c=>`<td class="r num">${esc(String(r[c]??''))}</td>`).join('')}</tr>`).join('')}</table></div>`;
    if((ev.rows||[]).length) requestAnimationFrame(()=>drawHeat('rvHeat',ev.rows));
  }catch(e){ el.innerHTML=`<div class="errbar">不可读（如实披露）：${esc(e.message)}</div>`; }
}
function kvTable(obj){
  return `<table class="grid">${Object.entries(obj||{}).map(([k,v])=>`<tr><td style="width:280px" class="mono-note">${esc(k)}</td><td class="r">${typeof v==='object'&&v!==null?esc(JSON.stringify(v)):fmt(v)}</td></tr>`).join('')}</table>`;
}
function renderSettlementResult(runRef, d){
  // 结算域结果详情：已发布＝正式结果；未发布如实非正式（不装）
  const meta = `<div class="psub">追溯编号：<span class="num">${esc(String(runRef).slice(0,16))}</span>（只读，不重算）｜方法：年度容量补偿结算</div>`;
  const reportBtns = `
    <div class="card"><h3>分析报告</h3>
      <div class="psub">报告与生成时的正式结果绑定（同身份重放，不随界面改版而改写）。</div>
      <a class="btn pri" href="/api/settlement/report?project=${encodeURIComponent(RESULTS_PROJECT)}&format=xlsx">导出 Excel 报告</a>
      <a class="btn" href="/api/settlement/report?project=${encodeURIComponent(RESULTS_PROJECT)}&format=html" style="margin-left:8px">导出 HTML 报告</a>
    </div>`;
  if(!d.published){
    return `<div class="card"><h3>年度容量补偿结算 <span class="tag plan">未发布</span></h3>${meta}
      <div class="hintbar warn">年度汇总尚未发布（已锁定 ${esc(String(d.locked_count||0))}/12）——预览不冒充正式结果，发布后可下载报告。</div></div>`;
  }
  const a = d.annual || {};
  return `<div class="card"><h3>年度容量补偿结算 <span class="tag ok">已发布</span></h3>${meta}
    ${kpisHtml([['年度总实付',a.total_paid,'元'],['年度扣减合计',a.total_deduction,'元'],['供需系数',a.coefficient,'']])}
    <div class="psub">${esc(String(a.settlement_year||''))}年｜公式 ${esc(a.formula_version||'')}｜补偿标准 ${fmt(a.compensation_standard,2)} 元/kW·年｜发布 ${esc(String(a.published_at||'').slice(0,10))}｜发布人 ${esc(a.published_by||'')}</div>
    <table class="grid"><tr><th>月份</th><th class="r">实付（元）</th><th class="r">扣减（元）</th></tr>
      ${(a.per_month||[]).map(m=>`<tr><td class="num">${m.month}月</td><td class="r num">${fmt(m.paid,2)}</td><td class="r num">${fmt(m.deduction,2)}</td></tr>`).join('')}</table>
    <details class="disc" open><summary>分主体汇总</summary><div class="body">
      <table class="grid"><tr><th>主体</th><th class="r">年度实付（元）</th><th class="r">年度扣减（元）</th><th class="r">月份数</th></tr>
      ${(a.per_subject||[]).map(s=>`<tr><td>${esc(s.subject_id)}</td><td class="r num">${fmt(s.total_paid,2)}</td><td class="r num">${fmt(s.total_deduction,2)}</td><td class="r num">${esc(String(s.months||''))}</td></tr>`).join('')}</table></div></details>
    <details class="disc"><summary>引用清单与输入哈希（追溯）</summary><div class="body">
      <div class="mono-note">引用 ${(a.references||[]).length} 条；12 月输入哈希 ${(a.input_hashes||[]).length} 条留痕；政策来源：${esc(a.policy_source||'—')}</div></div></details>
  </div>${reportBtns}`;
}
function renderCostResult(runRef, rec){
  const r = rec.result || {};
  const kind = r.calculation_kind || (rec.manifest||{}).calculation_kind || '';
  const reportBtns = `
    <div class="card"><h3>分析报告</h3>
      <div class="psub">报告与生成时的结果绑定，不随界面改版而改写。</div>
      <a class="btn pri" href="/api/cost/report?project=${encodeURIComponent(RESULTS_PROJECT)}&run=${encodeURIComponent(runRef)}&format=xlsx">导出 Excel 报告</a>
      <a class="btn" href="/api/cost/report?project=${encodeURIComponent(RESULTS_PROJECT)}&run=${encodeURIComponent(runRef)}&format=html" style="margin-left:8px">导出 HTML 报告</a>
    </div>`;
  const meta = `<div class="psub">运行编号：<span class="num">${esc(String(runRef).slice(0,8))}</span>（可在历史记录中随时重开）｜定位：<span class="num">${esc(RESULTS_PROJECT)}</span> ｜ 领域 cost ｜ 记录 <span class="num">${esc(runRef)}</span></div>`;
  if(kind === 'cost_fixed' || kind === 'fixed_cost'){
    const fy = r.first_year || {}, pa = r.period_average || {};
    return `<div class="card"><h3>新建机组固定成本核算结果 <span class="tag ok">计算完成</span></h3>${meta}
      ${kpisHtml([['首年固定成本',fy.total,'元/kW·年'],['经营期平均',pa.total,'元/kW·年'],['含增值税首年',fy.total_with_vat,'元/kW·年']])}
      <table class="grid"><tr><th>年份</th><th class="r">折旧</th><th class="r">利息</th><th class="r">固定运维</th><th class="r">保险</th><th class="r">摊销</th><th class="r">合计</th></tr>
      ${(r.yearly||[]).map(y=>`<tr><td class="num">${y.year}</td><td class="r">${fmt(y.depreciation)}</td><td class="r">${fmt(y.interest)}</td><td class="r">${fmt(y.fixed_om)}</td><td class="r">${fmt(y.insurance)}</td><td class="r">${fmt(y.amortization)}</td><td class="r"><b>${fmt(y.total)}</b></td></tr>`).join('')}
      <tr><td><b>合计</b></td><td class="r"><b>${fmt(fy.depreciation)}</b></td><td class="r"><b>${fmt(fy.interest)}</b></td><td class="r"><b>${fmt(fy.fixed_om)}</b></td><td class="r"><b>${fmt(fy.insurance)}</b></td><td class="r"><b>${fmt(fy.amortization)}</b></td><td class="r"><b>${fmt(fy.total)}</b></td></tr></table>
      <div class="hintbar" style="margin-top:12px">会计费用加总口径（折旧/利息/运维/保险/摊销）：不含股东资本回报，不是Gross CONE。</div></div>
      <details class="disc"><summary>口径与假设（如实披露）</summary><div class="body">${kvTable(r.assumptions)}</div></details>${reportBtns}`;
  }
  if(kind === 'cost_gross_cone'){
    const solve = r.solve || {};
    return `<div class="card"><h3>完整Gross CONE测算结果 <span class="tag ok">计算完成</span></h3>${meta}
      ${kpisHtml([['Gross CONE',r.gross_cone_per_kw,'元/kW·年'],['对象',r.object_name,''],['地区',r.region,''],['投产年度',r.target_year,''],['价格基准年',r.price_basis_year,'']])}
      <details class="disc"><summary>计算检查（求解状态与投资回收计算误差）</summary><div class="body">${kvTable(solve)}</div></details>
      <details class="disc" open><summary>口径与假设（如实披露）</summary><div class="body">${kvTable(r.assumptions)}</div></details></div>${reportBtns}`;
  }
  if(kind === 'cost_net_cone'){
    const gross = r.gross || {};
    return `<div class="card"><h3>年度Net CONE测算结果 <span class="tag ok">计算完成</span></h3>${meta}
      ${kpisHtml([['电能量净收益',r.energy_net_revenue,'元/年'],['辅助服务净收益',r.ancillary_net_revenue,'元/年'],['E&AS年度净收益',r.eas_annual,'元/年'],['E&AS抵扣',r.eas_offset_per_kw,'元/kW·年'],['Net CONE原始值',r.net_cone_raw,'元/kW·年','t2']])}
      <div class="hintbar">${esc(r.net_interpretation || '')}（缺失不补零、负值保留。）</div>
      <details class="disc" open><summary>Gross来源与血缘（如实披露）</summary><div class="body">${kvTable(gross)}</div></details>
      <details class="disc"><summary>口径与假设（如实披露）</summary><div class="body">${kvTable(r.assumptions)}</div></details></div>${reportBtns}`;
  }
  return `<div class="card"><div class="errbar">未知成本类型，拒绝按目录存在冒充结果：${esc(kind)}</div></div>`;
}
function kpisHtml(list){
  return `<div class="kpis">${list.map(([k,v,u,tier])=>`<div class="kpi"><div class="k">${dot(tier)}${esc(k)}</div><div class="v">${fmt(v)}</div></div>`).join('')}</div>`;
}

/* ============ 基础数据管理（切片②，真实数据链） ============ */
