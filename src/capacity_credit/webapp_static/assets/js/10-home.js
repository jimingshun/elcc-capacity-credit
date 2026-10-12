const ARCH_STRIP = `
<div class="card">
  <h3>目标架构链路（真实状态）</h3>
  <div style="display:grid;grid-template-columns:1fr;gap:6px;font-size:12.5px">
    <div><b>参数底座</b>：<span class="tag ok">项目管理已接通</span> <span class="tag plan">02 参数配置中心（演示）</span></div>
    <div><b>核心测算引擎</b>：<span class="tag ok">供给5项＋充裕度已接通</span> <span class="tag plan">05 年度容量补偿结算（演示/规划）</span></div>
    <div><b>市场仿真</b>：<span class="tag ok">①—⑦ 出清内核已接通</span> <span class="tag lock">履约考核仍为演示口径</span></div>
  </div>
</div>`;
function viewHome(){
  /* Step1（2026-09-28 设计稿）：首页两分区——上区①先算参数（供给/需求，现状保留）；
     下区②再用参数做业务（结算/市场仿真卡带就绪度清单，动态只读派生）。 */
  return `
  <div class="ptitle">选择测算功能</div>
  <div class="psub">点击入口只打开入口；不会开始计算，也不会改动您的数据。真实计算覆盖供给五项与充裕度评估；其余入口如实标注状态。</div>
  <div class="hintbar">本工具按「先算参数 → 再用参数做业务」两层组织：第一层（参数底座）算出带指纹的正式参数结果；第二层（应用层）组合引用这些参数做结算与市场仿真，不算新参数。</div>

  <div class="part">
    <div class="part-title"><span class="stepno">①</span>先算参数 · 参数底座</div>
    <div class="entries t2">
      <div class="entry">
        <h3>容量供给类参数 <span class="tag ok">已接通 5</span></h3>
        <p>固定成本、Gross CONE、Net CONE、ELCC、EFC 五项均接真实计算（向导制：类型锁定→检查→确认）。</p>
        <div class="foot"><span class="mono-note">已接通 5</span><button class="btn pri" onclick="PAGE='supply';rerender()">选择测算功能</button></div>
      </div>
      <div class="entry">
        <h3>容量需求类参数 <span class="tag lock">待验收</span></h3>
        <p>充裕度评估（四指标＋收敛）、目标可靠容量测算、缺口/备用裕度试算与供需系数（公式 v1）均已接真实计算。</p>
        <div class="foot"><span class="mono-note">已接通 4｜规划中 0</span><button class="btn pri" onclick="PAGE='demand';rerender()">选择测算功能</button></div>
      </div>
    </div>
  </div>

  <div class="part app">
    <div class="part-title"><span class="stepno">②</span>再用参数做业务 · 应用层</div>
    <div class="entries t2">
      <div class="entry">
        <h3>年度容量补偿结算 <span class="tag lock">补偿模式</span> <span class="tag plan">规划中（演示）</span></h3>
        <p>月度填报 → 检查 → 试算 → 锁定（12/12 全锁定后出年度汇总）。正式计算前置＝补偿价表来源与供需系数口径确认。</p>
        <div id="home-ready-settle" class="loading">就绪度读取中…</div>
        <div class="foot"><span class="mono-note" id="home-ready-settle-note">依赖就绪度动态只读</span><button class="btn pri" onclick="PAGE='settle';rerender()">进入结算</button></div>
      </div>
      <div class="entry">
        <h3>容量市场仿真 <span class="tag lock">市场模式</span> <span class="tag ok">出清内核已接通</span></h3>
        <p>需求曲线 × 供给报价 → 出清过程的情景仿真（统一出清价、单区、市场力披露不阻断；履约考核仍为演示口径）。</p>
        <div id="home-ready-market" class="loading">就绪度读取中…</div>
        <div class="foot"><span class="mono-note">已接通 7｜演示 1</span><button class="btn" onclick="switchMode('mkt')">切换并进入</button></div>
      </div>
    </div>
  </div>

  ${ARCH_STRIP}
  <div class="card"><h3>最近项目</h3><div id="home-projects" class="loading">读取中…</div></div>
  <div class="card"><h3>最近结果</h3><div id="home-results" class="loading">读取中…</div></div>`;
}
async function loadHome(){
  const projectsBox=document.getElementById('home-projects');
  const resultsBox=document.getElementById('home-results');
  try{
    const R = await computeReadiness();
    try{
      const rows = settleReadyRows(R);
      document.getElementById('home-ready-settle').innerHTML = readyListHTML(rows);
      const note=document.getElementById('home-ready-settle-note');
      if(note) note.textContent = `缺 ${rows.filter(r=>!r.ok).length} 项依赖`;
    }catch(e){ document.getElementById('home-ready-settle').innerHTML = '<div class="errbar">读取失败</div>'; }
    try{
      document.getElementById('home-ready-market').innerHTML = readyListHTML(marketReadyRows(R));
    }catch(e){ document.getElementById('home-ready-market').innerHTML = '<div class="errbar">读取失败</div>'; }
  }catch(e){
    const a=document.getElementById('home-ready-settle'), b=document.getElementById('home-ready-market');
    if(a) a.innerHTML = '<div class="errbar">读取失败</div>';
    if(b) b.innerHTML = '<div class="errbar">读取失败</div>';
  }
  try{
    const all=(await jget('/api/projects')).projects.slice()
      .sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));
    const projects=all.slice(0,3);
    projectsBox.innerHTML = projects.length ? projects.map(p=>
      `<div class="rowitem"><div class="tt">${esc(p.name)}<small>${esc(KIND_LABELS[p.kind]||'类型未知')}${p.measurement_type?`｜${esc(mlabel(p.measurement_type))}`:''}｜${p.formal_result?'<span class="tag ok">已完成</span>':'<span class="tag plan">进行中</span>'}</small></div>
       <button class="btn" onclick="openProject('${esc(p.project_dir)}')">打开</button></div>`).join('')
      + (all.length>3?`<div class="mono-note" style="margin-top:6px">仅显示最近 3 个项目，全部项目见「我的项目」。</div>`:'')
      : '<span class="loading">未有最近项目。在「我的项目」或测算向导中创建第一个项目后，这里会显示最近 3 个。</span>';
  }catch(e){ projectsBox.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
  try{
    const idx = await jget('/api/results/recent');
    const rows = (idx.rows||[]).slice(0,3);
    resultsBox.innerHTML = rows.length ? rows.map(r=>
      `<div class="rowitem"><div class="tt">${esc(r.project_name)}｜${esc(r.method_label)}<small>${esc(r.status_label)}｜${esc(r.created_at_label)}</small></div>
       <button class="btn" onclick="openResult('${esc(r.route.project_ref)}','${esc(r.route.domain)}','${esc(r.route.run_ref)}')">查看</button></div>`).join('')
      + ((idx.rows||[]).length>3?`<div class="mono-note" style="margin-top:6px">仅显示最近 3 次结果，全部结果见「结果与报告」。</div>`:'')
      : '<span class="loading">未有最近结果。完成一次测算后，这里会显示最近 3 次。</span>';
    if(idx.incomplete) resultsBox.innerHTML += `<div class="mono-note">注意：${esc(idx.incomplete_reason)}</div>`;
  }catch(e){ resultsBox.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
function openProject(ref){
  jget('/api/projects').then(d=>{
    const p = d.projects.find(x=>x.project_dir===ref);
    if(p && p.kind==='cost'){ COST_PROJECT={project_dir:p.project_dir,name:p.name}; COST_FUNCTION=p.measurement_type||null; COST_STEP='project'; PAGE='cost'; rerender(); }
    else if(p && (p.measurement_type==='elcc'||p.measurement_type==='efc')){
      /* D-3 双栈收敛（2026-09-23 用户审核确认）：ELCC/EFC 项目一律载入
         六步模块；旧向导已退居纯充裕度，不再承接 ELCC/EFC 项目 */
      REL_STACK='relmod';
      RM.method=p.measurement_type;
      rmPick(p.project_dir, p.name);  // 无六步 DOM 时其内部渲染静默返回
      PAGE='relmod'; rerender();
    }
    else if(p){ REL_STACK='accred'; RM.project=null; REL_FUNCTION=null; REL_STEP='fn'; DATA_PROJECT2=null; PAGE='accred'; rerender(); }
  });
}
function openResult(pr,domain,run){
  RESULTS_PROJECT=pr; RESULTS_RUN=run; PAGE='results'; rerender();
}

/* ============ 我的项目 ============ */
