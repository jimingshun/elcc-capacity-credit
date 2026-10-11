function viewProjects(){
  return `
  <div class="ptitle">我的项目</div>
  <div class="psub">项目是数据与测算的组织单元：成本项目不依赖可靠性数据批次；可靠性项目的数据须经「校验 → 审批 → 冻结」后方可计算（数据链在切片②接入）。</div>
  <div class="card">
    <h3>新建项目</h3>
    <div class="fgrid">
      <div class="fld" id="f-pname"><label><b>项目名称</b>（必填，支持中文与空格，最长200字符）</label><input id="np-name" placeholder="如：青海2027补偿测算">
        <div class="fh" id="fh-pname">项目名仅作管理标识，不影响任何计算口径。</div></div>
      <div class="fld"><label><b>项目类型</b></label><select id="np-kind"><option value="cost">成本项目</option><option value="reliability">可靠性项目</option></select>
        <div class="fh">可靠性项目的数据批次上传与冻结在切片②接入；本项目类型可先行创建。</div></div>
      <div class="fld"><label>储能资源构成</label><select id="np-storage"><option value="unconfirmed">尚未确认</option><option value="included">包含储能</option><option value="excluded">明确不包含储能</option></select>
        <div class="fh">决定数据模板是否包含储能表（切片②生效）。</div></div>
      <div class="fld"><label>水电资源构成</label><select id="np-hydro"><option value="unconfirmed">尚未确认</option><option value="included">包含水电</option><option value="excluded">明确不包含水电</option></select></div>
      <div class="fld" id="f-obj"><label>测算对象（成本项目）</label><input id="np-object" placeholder="如：660MW超超临界">
        <div class="fh">作为该项目业务标识；参数页仍可按次填写。</div></div>
      <div class="fld" id="f-prov"><label>目标省份或区域（成本项目）</label><input id="np-province" placeholder="如：青海"></div>
      <div class="fld" id="f-by"><label>基准年份（成本项目）</label><input class="num" id="np-baseyear" placeholder="如：2026"></div>
    </div>
    <div class="fld"><label>项目说明／数据来源描述（可选）</label><input id="np-desc" placeholder="来源说明不等于数据质量认证"></div>
    <div style="margin-top:6px"><button class="btn pri" onclick="createProject()">创建项目</button></div>
  </div>
  <div class="card"><h3>已有项目</h3><div id="proj-list" class="loading">读取中…</div></div>`;
}
async function loadProjects(){
  try{
    const projects = (await jget('/api/projects')).projects;
    document.getElementById('proj-list').innerHTML = projects.length ? projects.map(p=>{
      const parts = [KIND_LABELS[p.kind]||'类型未知'];
      if(p.measurement_type) parts.push(mlabel(p.measurement_type));
      if(p.object_name) parts.push('对象 '+p.object_name);
      if(p.province) parts.push(p.province);
      if(p.base_year) parts.push(p.base_year+'年');
      parts.push(p.formal_result?'已完成':'进行中');
      if(p.created_at) parts.push('创建 '+String(p.created_at).slice(0,10));
      return `<div class="rowitem"><div class="tt">${esc(p.name)}<small>${parts.map(esc).join('｜')}</small></div>
       ${COST_PROJECT&&COST_PROJECT.project_dir===p.project_dir?'<span class="tag ok">当前项目</span>':''}
       <button class="btn" onclick="openProject('${esc(p.project_dir)}')">打开项目</button></div>`;
    }).join('')
      : '<span class="loading">尚无项目。</span>';
  }catch(e){ document.getElementById('proj-list').innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function createProject(){
  const name = document.getElementById('np-name').value.trim();
  if(!name){
    document.getElementById('f-pname').classList.add('err');
    document.getElementById('fh-pname').textContent = '✕ 本字段必填：项目名称不能为空，留空不会创建项目。';
    return;
  }
  const kind = document.getElementById('np-kind').value;
  const composition = kind==='reliability' ? {
    storage: document.getElementById('np-storage').value,
    hydro: document.getElementById('np-hydro').value,
  } : null;
  const objectName = kind==='cost' ? document.getElementById('np-object').value.trim() : null;
  const province = kind==='cost' ? document.getElementById('np-province').value.trim() : null;
  const baseYearRaw = kind==='cost' ? document.getElementById('np-baseyear').value.trim() : '';
  const baseYear = baseYearRaw ? parseInt(baseYearRaw,10) : null;
  try{
    const r = await jpost('/api/projects', {name, description: document.getElementById('np-desc').value, project_kind: kind, resource_composition: composition, object_name: objectName, province: province, base_year: baseYear});
    flash('项目创建成功：' + r.name);
    loadProjects();
  }catch(e){
    document.getElementById('f-pname').classList.add('err');
    document.getElementById('fh-pname').textContent = '✕ ' + e.message;
  }
}

/* ============ 成本向导（真实计算） ============ */
const COST_FUNCS = {
  cost_fixed: {title:'新建机组固定成本测算', outputs:'首年与经营期平均固定成本（元/kW·年）及造价链'},
  cost_gross_cone: {title:'Gross CONE测算', outputs:'Gross CONE（元/kW·年）、NPV残差与求解状态'},
  cost_net_cone: {title:'Net CONE测算', outputs:'Net CONE（元/kW·年）、E&AS抵扣构成与来源标识'},
};
/* P-A: the full measurement-type registry (labels for non-cost types) */
const MEASUREMENT_LABELS = {
  cost_fixed:'固定成本测算', cost_gross_cone:'Gross CONE测算', cost_net_cone:'Net CONE测算',
  adequacy:'系统可靠性评估', elcc:'ELCC测算', efc:'EFC测算',
  demand_target_capacity:'目标可靠容量测算', demand_capacity_gap:'容量缺口与备用裕度', demand_coefficient:'容量供需系数',
  settlement_annual:'年度容量补偿结算', market_simulation:'容量市场仿真',
};
const KIND_LABELS = {cost:'成本项目', reliability:'可靠性项目', settlement:'结算项目', market:'市场仿真项目'};
const mlabel = (t) => (COST_FUNCS[t]||{}).title || MEASUREMENT_LABELS[t] || t || '';
/* P-A copy-as-new-project: a NEW same-type project with provenance */
async function copyProject(ref){
  if(!confirm('复制为新项目？将创建一个同类型新项目（记录来源与参数预填），原项目保持不变。')) return null;
  try{
    const rid = 'web-copy-' + (crypto.randomUUID ? crypto.randomUUID() : (Date.now() + '-' + Math.random().toString(36).slice(2)));
    const r = await jpost('/api/projects/copy', {project_ref: ref, client_request_id: rid});
    flash('已复制为新项目：' + r.name + '（来源项目已记录）');
    return r.project_ref;
  }catch(e){ flash('复制被拒绝：' + e.message); return null; }
}
