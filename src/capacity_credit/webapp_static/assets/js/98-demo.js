const SCN_DEMO = {
  base:{label:'基准情景（2026）',coeff:0.92,lole:'0.083',std:185,price:163},
  highre:{label:'高新能源渗透（2028E）',coeff:0.88,lole:'0.096',std:185,price:171},
  tight:{label:'供需偏紧（极端年）',coeff:1.00,lole:'0.310',std:185,price:185},
};
const demoBanner = `<div class="hintbar warn">以下内容为交互演示（示例口径），不是本工具的真实计算结果；真实能力以各模块状态标签为准。</div>`;
function viewDemoParams(){
  const locked = S.mode==='comp';
  const tables = [
    `<table class="grid"><tr><th>参数</th><th class="r">当前值</th><th>口径说明</th></tr>
     <tr><td><b>火电核定方法</b></td><td class="r">EFORd</td><td>强迫停运率法等效可用容量</td></tr>
     <tr><td><b>储能时长折算基准</b></td><td class="r">8 h</td><td>额定容量×放电时长÷基准时长</td></tr>
     <tr><td><b>可靠性主指标</b></td><td class="r">LOLE ≤ 0.1 天/年</td><td>校核指标：LOLH、EUE／NEUE</td></tr></table>`,
    `<table class="grid"><tr><th>参数</th><th class="r">当前值</th><th>口径说明</th></tr>
     <tr><td><b>参考机组</b></td><td class="r">660MW超超临界</td><td>高海拔修正系数 1.12</td></tr>
     <tr><td><b>单位造价</b></td><td class="r">3,550 元/kW</td><td>价格基准年 2026，不含增值税</td></tr>
     <tr><td><b>E&AS抵扣来源</b></td><td class="r">年度汇总录入</td><td>缺失＝未测算；负值保留</td></tr></table>`,
    `<table class="grid"><tr><th>参数</th><th class="r">当前值</th><th>口径说明</th></tr>
     <tr><td><b>需求曲线形式</b></td><td class="r">VRR 三点折线</td><td>锚点 A/B/C：98.9%／101.6%／105.8%</td></tr>
     <tr><td><b>拍卖机制</b></td><td class="r">密封报价</td><td>备选：降价时钟；提前期 T-3</td></tr></table>`,
  ];
  return `<div class="ptitle">参数配置中心</div><div class="psub">统一参数入口与版本化：<b>本页为演示口径</b>；参数集Schema与版本服务未建成。</div>${demoBanner}
  <div class="card"><div style="display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn pri" onclick="flash('演示页签')">一档 · 通用参数</button>
    <button class="btn" onclick="flash('演示页签')">二档 · 同源异用参数</button>
    <button class="btn" onclick="${locked?"flash('第三档市场规则参数在补偿模式下为只读预览，切换市场模式后解锁')":"flash('演示页签（市场模式解锁为交互示意）')"}">${locked?'三档 · 市场规则参数（锁定）':'三档 · 市场规则参数'}</button>
  </div><div style="margin-top:12px">${tables[0]}</div>
  ${locked?'<div class="hintbar warn" style="margin-top:12px">当前为补偿模式：第三档页签只读预览。</div>':''}</div>`;
}
/* ============ Step1 T5：年度容量补偿结算总览页（05，现 06 演示内容收作标签页） ============ */
let SETTLETAB = 0; // 0=总览与就绪度，1=月度结算演示，2=正式工作台（Step3 S1）
function viewSettle(){
  const head = `
  <div class="ptitle">年度容量补偿结算</div>
  <div class="psub">一个结算项目对应一个年度：月度填报 → 检查 → 试算 → 锁定；12/12 全锁定后出年度汇总正式结果。</div>
  <div class="hintbar warn">本模块为<b>规划中</b>能力（当前仅演示口径）：正式计算前置＝补偿价表来源（Q-S1）确认；<b>供需系数口径（Q-D3）已裁决，已转正式口径（公式 v1，04 页测算）</b>，本页演示数字仍为示例口径；开放问题未闭环前不产生正式金额。</div>
  <div class="otabs">
    <button class="${SETTLETAB===0?'on':''}" onclick="SETTLETAB=0;rerender()">总览与就绪度</button>
    <button class="${SETTLETAB===1?'on':''}" onclick="SETTLETAB=1;rerender()">月度结算演示（演示口径）</button>
    <button class="${SETTLETAB===2?'on':''}" onclick="SETTLETAB=2;rerender()">正式工作台</button>
  </div>`;
  if(SETTLETAB===0){
    return head + `
    <div class="card"><h3>依赖就绪度</h3><div id="settle-ready" class="loading">就绪度读取中…</div>
      <div class="mono-note">就绪度为动态只读（从项目注册表与结果索引派生）；缺项给跳转，不代做、不伪造就绪。</div></div>
    <div class="card"><h3>结算项目</h3>
      <div class="rowitem"><div class="tt">（尚无结算项目）<small>创建入口在「正式工作台」tab（Step 3 S1 已接线）</small></div><button class="btn" onclick="SETTLETAB=2;rerender()">去工作台</button></div>
    </div>`;
  }
  if(SETTLETAB===2){
    return head + viewSettleBench();
  }
  return head + `
  ${viewDemoSettleBody()}`;
}
/* ============ Step3 S1-M1：结算正式工作台（骨架：项目/价表/参考，零金额计算） ============ */
const SETTLEBENCH = { project:null, config:null, priceTable:null, ncRefs:null, form:{} };
function viewSettleBench(){
  return `
  <div class="card"><h3>正式工作台 <span class="tag lock">骨架（Step 3 S1）</span></h3>
    <div class="psub">本页只做项目创建、价表录入与引用准备——<b>不产生任何金额</b>；月度填报与试算在后续阶段接线。</div>
    <div id="settlebench-body"></div></div>`;
}
async function loadSettleBench(){
  const el = document.getElementById('settlebench-body');
  if(!el) return;
  el.innerHTML = '<div class="loading">读取中…</div>';
  try{
    const d = await jget('/api/projects');
    const rows = (d.projects||[]).filter(p=>p.measurement_type==='settlement_annual' && !p.archived);
    if(rows.length===0){
      el.innerHTML = viewSettleCreate();
      return;
    }
    SETTLEBENCH.project = rows[0];
    const cfg = await jget('/api/settlement/config?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    SETTLEBENCH.config = cfg;
    const pt = await jget('/api/settlement/price-table?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    SETTLEBENCH.priceTable = pt;
    const nc = await jget('/api/settlement/net-cone-references');
    SETTLEBENCH.ncRefs = nc.rows||[];
    const sd = await jget('/api/settlement/subjects?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    SETTLEBENCH.subjectsData = sd;
    el.innerHTML = viewSettleBenchProject();
    settleBenchSubjects(); settleBenchReferences(); settleBenchMonths(); settleBenchAnnual();
  }catch(e){ el.innerHTML = `<div class="errbar">读取失败：${esc(e.message)}</div>`; }
}
function viewSettleCreate(){
  const f = SETTLEBENCH.form;
  return `
  <div class="card"><h3>新建年度结算项目</h3>
    <div class="hintbar">一个年度只允许一个正式结算项目（Q-S6 裁决）；基础信息七字段<b>创建即锁定</b>，改口径请复制新项目。</div>
    <div class="fgrid">
      <div class="fld"><label><b>项目名称</b>（必填）</label><input id="sb-name" value="${esc(f.name||'')}"></div>
      <div class="fld"><label><b>结算年度</b>（≥2020）</label><input class="num" id="sb-year" value="${esc(f.year||'2027')}"></div>
      <div class="fld"><label><b>省份或区域</b>（必填）</label><input id="sb-region" value="${esc(f.region||'')}"></div>
      <div class="fld"><label><b>补偿机制版本</b>（必填，如「2027版规则」）</label><input id="sb-mech" value="${esc(f.mech||'')}"></div>
      <div class="fld"><label>结算币种</label><input id="sb-currency" value="CNY" disabled></div>
      <div class="fld"><label><b>数据截止日期</b>（必填，如 2027-12-31）</label><input id="sb-cutoff" value="${esc(f.cutoff||'')}"></div>
      <div class="fld"><label><b>价格基准</b>（必填，如「含税，2027价格基准年」）</label><input id="sb-pbasis" value="${esc(f.pbasis||'')}"></div>
    </div>
    <div class="fld"><label><b>规则依据说明</b>（必填；规则文件来源与条文索引，人工维护）</label><input id="sb-notes" value="${esc(f.notes||'')}" style="width:100%"></div>
    <button class="btn pri" onclick="settleBenchCreate()">创建年度结算项目</button>
  </div>`;
}
async function settleBenchCreate(){
  const val=(id)=>{const el=document.getElementById(id);return el?el.value.trim():'';};
  const f = SETTLEBENCH.form = {
    name:val('sb-name'), year:val('sb-year'), region:val('sb-region'),
    mech:val('sb-mech'), cutoff:val('sb-cutoff'), pbasis:val('sb-pbasis'), notes:val('sb-notes'),
  };
  if(!f.name){ flash('请填写项目名称'); return; }
  try{
    const r = await jpost('/api/projects', {
      name:f.name, description:'', project_kind:'settlement', measurement_type:'settlement_annual',
      client_request_id:'sb-'+Date.now(),
      settlement_config:{ settlement_year:Number(f.year), region:f.region, mechanism_version:f.mech,
        currency:'CNY', data_cutoff:f.cutoff, price_basis:f.pbasis, rule_notes:f.notes },
    });
    flash('结算项目已创建：'+r.name+'（年度 '+r.settlement_year+'，基础信息已锁定）');
    loadSettleBench();
  }catch(e){ flash('创建被拒绝：'+e.message); }
}
function viewSettleBenchProject(){
  const c = SETTLEBENCH.config||{};
  const pt = SETTLEBENCH.priceTable||{current:null,history:[]};
  const nc = SETTLEBENCH.ncRefs||[];
  return `
  <div class="card"><h3>基础信息（创建即锁定，只读）</h3>
    <table class="grid"><tr><th>字段</th><th>值</th></tr>
      <tr><td>项目</td><td>${esc(SETTLEBENCH.project.name)}</td></tr>
      <tr><td>结算年度</td><td class="num">${esc(c.settlement_year)}</td></tr>
      <tr><td>省份或区域</td><td>${esc(c.region)}</td></tr>
      <tr><td>机制版本</td><td>${esc(c.mechanism_version)}</td></tr>
      <tr><td>数据截止</td><td>${esc(c.data_cutoff)}</td></tr>
      <tr><td>价格基准</td><td>${esc(c.price_basis)}</td></tr>
      <tr><td>规则依据</td><td>${esc(c.rule_notes)}</td></tr></table></div>
  <div class="card"><h3>年度状态（已锁定进度 N/12 动态读取）</h3>
    <div id="sb-months" class="loading">读取中…</div></div>
  <div class="card"><h3>补偿价表（Q-S1：录入制＋政策来源必填）</h3>
    ${pt.current?`<table class="grid"><tr><th>版本</th><th class="r">标准（元/kW·年）</th><th>分类标准（元/kW·年）</th><th>政策来源</th><th>规则版本</th><th>录入时间</th></tr>
      ${pt.history.map(h=>`<tr><td class="num">v${h.version}${h.version===(pt.current&&pt.current.version)?' ←当前':''}</td><td class="r num">${fmt(h.compensation_standard,2)}</td><td>${h.by_resource_type?esc(Object.entries(h.by_resource_type).map(([k,v])=>k+' '+v).join('；')):'—'}</td><td>${esc(h.policy_source)}</td><td>${esc(h.rule_version)}</td><td>${esc(String(h.recorded_at||'').slice(0,10))}</td></tr>`).join('')}</table>`
    :'<div class="hintbar warn">尚未录入补偿价表——数值与政策来源（文号/条文）均必填，无来源不得进入正式链路。</div>'}
    <div class="fgrid" style="margin-top:10px">
      <div class="fld"><label><b>补偿标准</b>（元/kW·年，>0）</label><input class="num" id="sb-pt-std"></div>
      <div class="fld"><label><b>政策来源</b>（文号/条文，必填）</label><input id="sb-pt-src" placeholder="如：皖发改〔2027〕12号 第三条"></div>
      <div class="fld"><label><b>规则版本</b>（必填）</label><input id="sb-pt-ver" value="${esc(c.mechanism_version||'')}"></div>
    </div>
    <div style="margin-top:8px">
      <label style="font-size:12px"><input type="checkbox" id="sb-pt-by-on" onchange="settleBenchByToggle()"> <b>分类补偿</b>（按资源类型分别设价；取价口径＝主体台账的资源类型）</label>
      <div id="sb-pt-by" style="display:none;margin-top:6px">
        <div class="mono-note">留空的类型按基础标准结算（回退在试算/年度的取价来源中如实披露）；填写值须 &gt;0。</div>
        <div class="fgrid">
          ${['coal','gas','hydro','storage','solar','wind','other'].map(t=>`<div class="fld"><label>${t}</label><input class="num" id="sb-pt-by-${t}" placeholder="元/kW·年"></div>`).join('')}
        </div>
      </div>
    </div>
    <button class="btn pri" onclick="settleBenchPriceTable()">录入价表新版本</button></div>
  <div class="card"><h3>Net CONE 定价参考（只读参考——工具测算值，不代替政策定价）</h3>
    ${nc.length?`<table class="grid"><tr><th>项目</th><th class="r">Net CONE</th><th>区域</th><th>评估年</th></tr>
      ${nc.map(r=>`<tr><td>${esc(r.project_name)}</td><td class="r num">${fmt(r.net_cone_raw,2)}</td><td>${esc(r.region||'')}</td><td class="num">${esc(r.assessment_year||'')}</td></tr>`).join('')}</table>`
    :'<span class="loading">本工作区暂无 Net CONE 正式结果——无参考值。</span>'}</div>
  <div class="card"><h3>主体台账 <span class="tag lock">版本化</span></h3>
    <div class="psub">登记参与结算的主体：标识唯一、资源类型、申报容量基线；每次修改存新版本，历史保留不覆盖。</div>
    <div id="settleBenchSubjects" class="loading">读取中…</div>
    <details class="disc"><summary>录入/更新台账（形成新版本）</summary><div class="body">
      <div class="mono-note" style="margin-bottom:6px">每行一个主体：标识, 资源类型(coal/gas/hydro/storage/solar/wind/other), 申报容量基线MW, 备注(可省)</div>
      <textarea id="sb-subjects-input" style="width:100%;height:90px;font-family:var(--mono);font-size:12px" placeholder="燃煤电厂A, coal, 1200,&#10;储能电站D, storage, 200, 试运行"></textarea>
      <div style="margin-top:8px"><button class="btn pri" onclick="settleBenchSaveSubjects()">保存台账新版本</button></div>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn" onclick="settleCsvTemplate('subjects')">下载台账 CSV 模板</button>
        <input type="file" accept=".csv,text/csv" id="sb-subjects-csv" style="max-width:200px">
        <button class="btn" onclick="settleImportSubjectsCsv()">导入台账 CSV</button>
        <span class="mono-note">12 月×多主体批量时用 CSV；导入走同一服务端校验（版本化留痕）。</span>
      </div>
    </div></details></div>
  <div class="card"><h3>引用管理 <span class="tag lock">同口径门</span></h3>
    <div class="psub">把上游正式结果挂进本结算项目（冻结系数／Net CONE／ELCC/EFC 认可容量／目标可靠容量）。五项口径逐项核对：不一致＝阻断；来源缺字段＝警告需确认留痕。</div>
    <div id="settleBenchReferences" class="loading">读取中…</div></div>
  <div class="card"><h3>年度汇总 <span class="tag lock">12/12 后可发布</span></h3>
    <div class="psub">12 个月全部锁定后，发布年度汇总正式结果（一次性不可变，绑定项目正式结果）。发布后月度动作全部关闭，纠错请复制为新年度项目。</div>
    <div id="annualSummary" class="loading">读取中…</div>
    <div id="annualSummaryPublish"><button class="btn pri" onclick="settleAnnualPublish()">发布年度汇总正式结果</button></div></div>`;
}
async function settleBenchSubjects(){
  const el = document.getElementById('settleBenchSubjects');
  if(!el || !SETTLEBENCH.project) return;
  try{
    const d = await jget('/api/settlement/subjects?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    if(!d.current){ el.innerHTML = '<span class="loading">尚未登记主体（展开下方录入）。</span>'; return; }
    el.innerHTML = `<table class="grid"><tr><th>主体</th><th>资源类型</th><th class="r">申报容量基线（MW）</th><th>备注</th></tr>
      ${d.current.subjects.map(s=>`<tr><td>${esc(s.subject_id)}</td><td>${esc(s.resource_type)}</td><td class="r num">${fmt(s.declared_capacity_mw,1)}</td><td>${esc(s.note||'')}</td></tr>`).join('')}</table>
      <div class="mono-note" style="margin-top:6px">当前 v${d.current.version}｜历史 ${d.history.length} 版（追加式，不覆盖）</div>`;
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function settleBenchSaveSubjects(){
  const ta = document.getElementById('sb-subjects-input');
  const lines = (ta?ta.value:'').split('\n').map(l=>l.trim()).filter(Boolean);
  const subjects = lines.map(l=>{
    const [sid, rtype, cap, note] = l.split(',').map(x=>x.trim());
    return {subject_id:sid, resource_type:rtype, declared_capacity_mw:Number(cap), note:note||''};
  });
  try{
    const r = await jpost('/api/settlement/subjects', {project_ref:SETTLEBENCH.project.project_dir, subjects});
    flash('台账 v'+r.version+' 已保存（'+r.subjects.length+' 个主体）');
    settleBenchSubjects();
  }catch(e){ flash('台账被拒绝：'+e.message); }
}
async function settleBenchReferences(){
  const el = document.getElementById('settleBenchReferences');
  if(!el || !SETTLEBENCH.project) return;
  try{
    const [refs, opts] = await Promise.all([
      jget('/api/settlement/references?project='+encodeURIComponent(SETTLEBENCH.project.project_dir)),
      jget('/api/settlement/reference-options'),
    ]);
    const coefs = (opts.coefficients||[]).filter(c=>c.target_year===(SETTLEBENCH.config||{}).settlement_year);
    el.innerHTML = `
      ${refs.rows.length?`<table class="grid"><tr><th>类型</th><th>来源</th><th>值</th><th>口径校验</th></tr>
        ${refs.rows.map(r=>`<tr><td>${esc(r.kind)}</td><td class="mono-note">${esc(r.project_ref||'')}／${esc(String(r.freeze_ref||r.run_ref||'').slice(0,8))}</td><td class="num">${r.kind==='coefficient'?fmt(r.target.coefficient,4):fmt(r.target.point_value_mw??r.target.net_cone_raw,2)}</td><td>${(r.warnings||[]).length?`<span class="tag plan">警告已确认留痕</span>`:'<span class="tag ok">口径核验通过</span>'}</td></tr>`).join('')}</table>`
      :'<span class="loading">尚未挂引用。</span>'}
      <details class="disc" style="margin-top:10px"><summary>挂引用：本年度已冻结系数（${coefs.length}）</summary><div class="body">
        ${coefs.length?coefs.map(c=>`<div class="rowitem"><div class="tt"><b class="num">${fmt(c.coefficient,4)}</b>｜${esc(c.project_name)}<small>formula_version=${esc(c.formula_version)}｜确认人 ${esc(c.confirmer||'')}</small></div>
          <button class="btn" onclick="settleBenchAddCoefRef('${esc(c.project_ref)}','${esc(c.freeze_ref)}')">挂此引用</button></div>`).join('')
        :'<span class="loading">本年度暂无已冻结系数——请先在 04 页测算并冻结。</span>'}
        <div class="mono-note" style="margin-top:6px">地区等来源缺字段的引用＝警告：提交后按提示确认（留痕）；年份不一致＝阻断。</div>
      </div></details>`;
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function settleBenchAddCoefRef(projectRef, freezeRef){
  try{
    await jpost('/api/settlement/references', {project_ref:SETTLEBENCH.project.project_dir, kind:'coefficient', target:{project_ref:projectRef, freeze_ref:freezeRef}});
    flash('引用已挂上');
  }catch(e){
    if(String(e.message).includes('无法核验')){
      if(confirm('口径警告（逐项列出）：\n'+e.message+'\n\n确认接受并留痕？')){
        await jpost('/api/settlement/references', {project_ref:SETTLEBENCH.project.project_dir, kind:'coefficient', confirm_warnings:true, confirmer:'页面确认', target:{project_ref:projectRef, freeze_ref:freezeRef}});
        flash('引用已挂上（警告已确认留痕）');
      } else { flash('已取消'); return; }
    } else { flash('引用被拒绝：'+e.message); return; }
  }
  settleBenchReferences();
}
function settleBenchByToggle(){
  const box = document.getElementById('sb-pt-by');
  const on = document.getElementById('sb-pt-by-on');
  if(box) box.style.display = (on && on.checked) ? '' : 'none';
}
async function settleBenchPriceTable(){
  const val=(id)=>{const el=document.getElementById(id);return el?el.value.trim():'';};
  const byOn = !!((document.getElementById('sb-pt-by-on')||{}).checked);
  const byResourceType = {};
  if(byOn){
    ['coal','gas','hydro','storage','solar','wind','other'].forEach(t=>{
      const raw = val('sb-pt-by-'+t);
      if(raw!=='') byResourceType[t] = Number(raw);
    });
  }
  try{
    const payload = {
      project_ref:SETTLEBENCH.project.project_dir,
      compensation_standard:Number(val('sb-pt-std')),
      policy_source:val('sb-pt-src'),
      rule_version:val('sb-pt-ver'),
    };
    // 开了分类补偿就如实提交分类表（空表由服务端拒绝并说明，不静默降级）
    if(byOn) payload.by_resource_type = byResourceType;
    const r = await jpost('/api/settlement/price-table', payload);
    flash('价表 v'+r.version+' 已录入（标准 '+r.price_table.compensation_standard+' 元/kW·年）'
      +(r.price_table.by_resource_type?'，分类补偿 '+Object.keys(r.price_table.by_resource_type).length+' 类':''));
    loadSettleBench();
  }catch(e){ flash('录入被拒绝：'+e.message); }
}
/* ============ Step3 S2-M1：月度状态机（网格真实状态＋月度页填报/检查/试算） ============ */
const MSTATE = { 未开始:'plan', 待检查:'lock', 检查未通过:'lock', 待确认:'ok', 已锁定:'ok', '已锁定·有调整':'ok' };
async function settleBenchMonths(){
  const el = document.getElementById('sb-months');
  if(!el || !SETTLEBENCH.project) return;
  try{
    const d = await jget('/api/settlement/months?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    const months = d.months||[];
    const locked = months.filter(m=>m.state==='已锁定'||m.state==='已锁定·有调整').length;
    el.innerHTML = `
      <div class="bigstat"><div class="bstat"><div class="k">月度锁定进度</div><div class="v">${locked}<span class="u">/12</span></div><div class="u">点击月份进入月度工作页（填报→检查→试算）</div></div></div>
      <div>${months.map(m=>`<button class="btn" style="margin:3px" onclick="settleMonthOpen(${m.month})">${m.month}月 <span class="tag ${MSTATE[m.state]||'plan'}">${esc(m.state)}</span></button>`).join('')}</div>`;
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function settleBenchAnnual(){
  const el = document.getElementById('annualSummary');
  const btn = document.getElementById('annualSummaryPublish');
  if(!el || !SETTLEBENCH.project) return;
  try{
    const d = await jget('/api/settlement/summary?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    if(d.published){
      const a = d.annual;
      if(btn) btn.innerHTML = '<span class="tag ok">已发布（不可变）</span>';
      el.innerHTML = `
        <div class="bigstat">
          <div class="bstat hero"><div class="k">年度总实付（元）</div><div class="v">${fmt(a.total_paid,2)}</div><div class="u">${esc(a.settlement_year)}年｜公式 ${esc(a.formula_version)}｜发布 ${esc(String(a.published_at||'').slice(0,10))}</div></div>
          <div class="bstat"><div class="k">年度扣减合计（元）</div><div class="v">${fmt(a.total_deduction,2)}</div><div class="u">12 月汇总</div></div>
          <div class="bstat"><div class="k">供需系数</div><div class="v">${fmt(a.coefficient,4)}</div><div class="u">formula_version=${esc(a.coefficient_formula_version||'')}</div></div>
        </div>
        <table class="grid"><tr><th>月份</th><th class="r">实付（元）</th><th class="r">扣减（元）</th></tr>
          ${a.per_month.map(m=>`<tr><td>${m.month}月</td><td class="r num">${fmt(m.paid,2)}</td><td class="r num">${fmt(m.deduction,2)}</td></tr>`).join('')}</table>
        <div class="mono-note" style="margin-top:8px">分主体：${(a.per_subject||[]).map(s=>esc(s.subject_id)+' '+fmt(s.total_paid,2)).join('；')}｜输入哈希 ${(a.input_hashes||[]).length} 条留痕｜引用清单 ${(a.references||[]).length} 条</div>
        <div style="margin-top:10px;display:flex;gap:8px">
          <button class="btn pri" onclick="settleAnnualReport('xlsx')">导出年度报告（Excel）</button>
          <button class="btn" onclick="settleAnnualReport('html')">导出年度报告（HTML）</button>
          <span class="mono-note" style="align-self:center">报告＝已发布正式结果的业务投影（同身份重放，不重算金额）。</span>
        </div>`;
    }else{
      if(btn) btn.innerHTML = `<button class="btn pri" onclick="settleAnnualPublish()">发布年度汇总正式结果</button>`;
      el.innerHTML = `<div class="hintbar warn"><b>年度汇总尚未发布。</b>已锁定 ${d.locked_count}/12${d.unlocked_months&&d.unlocked_months.length?`，未完成：${esc(d.unlocked_months.join('、'))}`:''}。12/12 全部锁定后发布。</div>`;
    }
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function settleAnnualPublish(){
  try{
    const r = await jpost('/api/settlement/summary/publish', {project_ref:SETTLEBENCH.project.project_dir, published_by:'页面确认'});
    flash('年度汇总已发布（一次性不可变）：'+r.annual.total_paid+' 元');
    settleBenchAnnual();
  }catch(e){ flash('发布被拒绝：'+e.message); }
}
function settleAnnualReport(fmt){
  // 年度汇总报告下载（收尾①）：与成本域同款——服务端导出并流式返回文件
  window.location.href = '/api/settlement/report?project='
    + encodeURIComponent(SETTLEBENCH.project.project_dir) + '&format=' + (fmt==='html'?'html':'xlsx');
}
/* ============ 收尾欠账④：台账/考核 CSV 模板与导入 ============ */
function settleCsvTemplate(kind){
  window.location.href = '/api/settlement/template?kind=' + encodeURIComponent(kind==='month'?'month':'subjects');
}
async function settleImportSubjectsCsv(){
  const file = ((document.getElementById('sb-subjects-csv')||{}).files||[])[0];
  if(!file){ flash('请先选择 CSV 文件（可用上方模板）'); return; }
  const csv = await file.text();
  try{
    const r = await jpost('/api/settlement/import',
      {project_ref:SETTLEBENCH.project.project_dir, kind:'subjects', csv});
    flash('台账 v'+r.version+' 已导入（'+r.subjects.length+' 个主体）');
    settleBenchSubjects();
  }catch(e){ flash('CSV 导入被拒绝：'+e.message); }
}
async function settleImportMonthCsv(){
  const file = ((document.getElementById('sm-csv')||{}).files||[])[0];
  if(!file){ flash('请先选择 CSV 文件（可用上方模板）'); return; }
  const csv = await file.text();
  try{
    const r = await jpost('/api/settlement/import',
      {project_ref:SETTLEBENCH.project.project_dir, kind:'month', month:SETTLEBENCH.month, csv});
    flash('填报已导入（'+r.entries+' 条，检查结论已失效，需重新检查）');
    viewSettleMonth();
  }catch(e){ flash('CSV 导入被拒绝：'+e.message); }
}
function settleMonthOpen(month){ SETTLEBENCH.month = month; viewSettleMonth(); }
async function viewSettleMonth(){
  const el = document.getElementById('settlebench-body');
  if(!el || !SETTLEBENCH.project) return;
  const month = SETTLEBENCH.month;
  const subjects = ((SETTLEBENCH.subjectsData||{}).current||{}).subjects||[];
  // 月度状态只读拉取：待确认→显示锁定区；已锁定→全页只读（S2-M2）
  let mstate = '未开始';
  try{
    const ms = await jget('/api/settlement/months?project='+encodeURIComponent(SETTLEBENCH.project.project_dir));
    const row = (ms.months||[]).find(m=>m.month===month);
    if(row) mstate = row.state;
  }catch(e){ /* 状态读失败按未开始呈现 */ }
  const locked = (mstate==='已锁定' || mstate==='已锁定·有调整');
  const dis = locked ? 'disabled' : '';
  el.innerHTML = `
  <div class="card"><h3>${month} 月工作页 <span class="tag ${locked?'ok':'lock'}">${esc(mstate)}</span></h3>
    <div style="margin-bottom:10px"><button class="btn" onclick="loadSettleBench()">← 返回工作台</button></div>
    ${locked?`<div class="lockbar"><b>本月已锁定。</b>填报与试算均已关闭；纠错走下方调整（追加式留痕，当月有效值＝锁定值＋调整差额链）。</div>
    <div class="card"><h3>锁定后调整 <span class="tag lock">追加式</span></h3>
      <div class="fgrid">
        <div class="fld"><label><b>主体标识</b></label><input id="sm-adj-sid" value="${esc(subjects[0]?subjects[0].subject_id:'')}"></div>
        <div class="fld"><label>容量差额（MW，有符号）</label><input class="num" id="sm-adj-dmw" value="0"></div>
        <div class="fld"><label><b>金额差额（元，有符号）</b></label><input class="num" id="sm-adj-damt" value="0"></div>
        <div class="fld"><label><b>调整原因</b>（必填，留痕）</label><input id="sm-adj-reason"></div>
        <div class="fld"><label><b>调整人</b>（必填，留痕）</label><input id="sm-adj-adjuster"></div>
      </div>
      <button class="btn pri" id="settleMonthAdjust" onclick="settleMonthAdjust()">提交调整（追加留痕）</button></div>
    <div class="card"><h3>调整追溯</h3><div id="sm-adj-trace" class="loading">读取中…</div></div>`
    :'<div class="psub">逐主体填报本月数据 → 服务端检查（R-ST-01..08）→ 试算（技术预览，不产生正式金额）→ 确认锁定。</div>'}
    <table class="grid"><tr><th>主体</th><th class="r">申报容量MW</th><th class="r">可靠容量系数</th><th class="r">可获得率</th><th class="r">应出力MW</th><th class="r">实际出力MW</th><th class="r">未履约小时</th><th class="r">扣减单价 元/MWh</th><th class="r">人工调整(元)</th><th>调整依据</th></tr>
      ${subjects.map(s=>`<tr><td>${esc(s.subject_id)}</td>
        <td class="r"><input class="num" data-mf="declared_capacity_mw" data-sid="${esc(s.subject_id)}" value="${s.declared_capacity_mw}" style="width:80px" ${dis}></td>
        <td class="r"><input class="num" data-mf="reliability_ratio" data-sid="${esc(s.subject_id)}" value="1.0" style="width:70px" ${dis}></td>
        <td class="r"><input class="num" data-mf="availability_factor" data-sid="${esc(s.subject_id)}" value="1.0" style="width:70px" ${dis}></td>
        <td class="r"><input class="num" data-mf="expected_output_mw" data-sid="${esc(s.subject_id)}" value="${s.declared_capacity_mw}" style="width:80px" ${dis}></td>
        <td class="r"><input class="num" data-mf="actual_output_mw" data-sid="${esc(s.subject_id)}" value="${s.declared_capacity_mw}" style="width:80px" ${dis}></td>
        <td class="r"><input class="num" data-mf="unmet_hours" data-sid="${esc(s.subject_id)}" value="0" style="width:70px" ${dis}></td>
        <td class="r"><input class="num" data-mf="deduction_price_per_mwh" data-sid="${esc(s.subject_id)}" value="0" style="width:80px" ${dis}></td>
        <td class="r"><input class="num" data-mf="manual_adjustment" data-sid="${esc(s.subject_id)}" value="0" style="width:80px" ${dis}></td>
        <td><input data-mf="adjustment_reason" data-sid="${esc(s.subject_id)}" style="width:120px" ${dis}></td></tr>`).join('')}</table>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn pri" onclick="settleMonthSave()" ${dis}>保存填报</button>
      <button class="btn" onclick="settleMonthCheck()" ${dis}>服务端检查</button>
      <button class="btn" onclick="settleMonthTrial()" ${dis}>试算（技术预览）</button>
    </div>
    ${!locked?`<div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--line2);padding-top:10px">
      <button class="btn" onclick="settleCsvTemplate('month')">下载填报 CSV 模板</button>
      <input type="file" accept=".csv,text/csv" id="sm-csv" style="max-width:200px">
      <button class="btn" onclick="settleImportMonthCsv()">导入填报 CSV</button>
      <span class="mono-note">多主体批量填报用 CSV（缺失≠0：空单元格＝未填，检查会如实报缺）。</span>
    </div>`:''}
    ${!locked?`<div style="margin-top:12px;border-top:1px solid var(--line2);padding-top:12px;display:flex;gap:8px;align-items:center">
      <label style="font-size:12px;color:var(--sub)"><b>锁定人</b>（必填，留痕）</label>
      <input id="sm-locker" style="width:140px" placeholder="姓名/工号">
      <button class="btn pri" onclick="settleMonthLock()">确认锁定（检查通过后）</button>
    </div>`:''}
    <div id="sm-result" style="margin-top:12px"></div></div>`;
  if(locked) settleMonthTrace();
}
async function settleMonthTrace(){
  const el = document.getElementById('sm-adj-trace');
  if(!el) return;
  try{
    const d = await jget('/api/settlement/adjustments?project='+encodeURIComponent(SETTLEBENCH.project.project_dir)+'&month='+SETTLEBENCH.month);
    el.innerHTML = (d.rows||[]).length
      ? `<table class="grid"><tr><th>#</th><th>时间</th><th>主体</th><th class="r">容量差额</th><th class="r">金额差额（元）</th><th>原因</th><th>调整人</th></tr>
          ${d.rows.map(r=>`<tr><td class="num">${r.sequence}</td><td>${esc(String(r.adjusted_at||'').slice(0,16))}</td><td>${esc(r.subject_id)}</td><td class="r num">${fmt(r.delta_mw,2)}</td><td class="r num">${fmt(r.delta_amount,2)}</td><td>${esc(r.reason)}</td><td>${esc(r.adjuster)}</td></tr>`).join('')}</table>
        <div class="mono-note" style="margin-top:6px">追加式时间线：历史永不覆盖；当月有效值＝锁定值＋全部差额之和。</div>`
      : '<span class="loading">本月暂无调整记录。</span>';
  }catch(e){ el.innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function settleMonthAdjust(){
  const val=(id)=>{const el=document.getElementById(id);return el?el.value.trim():'';};
  try{
    await jpost('/api/settlement/month/adjust', {
      project_ref:SETTLEBENCH.project.project_dir, month:SETTLEBENCH.month,
      subject_id:val('sm-adj-sid'),
      delta_mw:Number(val('sm-adj-dmw')), delta_amount:Number(val('sm-adj-damt')),
      reason:val('sm-adj-reason'), adjuster:val('sm-adj-adjuster'),
    });
    flash('调整已追加（留痕）');
    settleMonthTrace();
  }catch(e){ flash('调整被拒绝：'+e.message); }
}
async function settleMonthLock(){
  const box = document.getElementById('sm-result');
  const locker = (document.getElementById('sm-locker')||{}).value?.trim() || '';
  if(!locker){ flash('锁定是显式确认动作：请填写锁定人'); return; }
  try{
    const r = await jpost('/api/settlement/month/lock', {project_ref:SETTLEBENCH.project.project_dir, month:SETTLEBENCH.month, locked_by:locker});
    flash(`${SETTLEBENCH.month} 月已锁定（锁人 ${r.locked_by}）`);
    viewSettleMonth();
  }catch(e){ box.innerHTML = `<div class="errbar">锁定被拒绝：${esc(e.message)}</div>`; }
}
function settleMonthCollect(){
  const entries = {};
  document.querySelectorAll('[data-mf]').forEach(inp=>{
    const sid = inp.dataset.sid, key = inp.dataset.mf;
    entries[sid] = entries[sid] || {subject_id:sid};
    entries[sid][key] = key==='adjustment_reason' ? inp.value : Number(inp.value);
  });
  return Object.values(entries);
}
async function settleMonthSave(){
  try{
    await jpost('/api/settlement/month/input', {project_ref:SETTLEBENCH.project.project_dir, month:SETTLEBENCH.month, entries:settleMonthCollect()});
    flash('填报已保存（检查结论已失效，需重新检查）');
  }catch(e){ flash('保存被拒绝：'+e.message); }
}
async function settleMonthCheck(){
  const box = document.getElementById('sm-result');
  try{
    const r = await jpost('/api/settlement/month/check', {project_ref:SETTLEBENCH.project.project_dir, month:SETTLEBENCH.month});
    box.innerHTML = r.blockers.length===0
      ? `<div class="lockbar"><b>检查通过（待确认）。</b>可以试算。</div>`
      : `<div class="errbar">检查未通过（逐项）：<br>${r.blockers.map(b=>`[${esc(b.rule)}] ${esc(b.message)}`).join('<br>')}</div>`;
  }catch(e){ box.innerHTML = `<div class="errbar">检查请求被拒绝：${esc(e.message)}</div>`; }
}
async function settleMonthTrial(){
  const box = document.getElementById('sm-result');
  try{
    const r = await jpost('/api/settlement/month/trial', {project_ref:SETTLEBENCH.project.project_dir, month:SETTLEBENCH.month});
    box.innerHTML = `
      <div class="hintbar warn"><b>试算（技术预览），非正式结果。</b>正式金额须确认锁定（下一阶段）后进入年度汇总。</div>
      <table class="grid"><tr><th>主体</th><th class="r">应补偿（元）</th><th class="r">考核扣减（元）</th><th class="r">人工调整（元）</th><th class="r">实付（元）</th></tr>
        ${r.subjects.map(s=>`<tr><td>${esc(s.subject_id)}</td><td class="r num">${fmt(s.due_amount,2)}</td><td class="r num">${fmt(s.deduction,2)}</td><td class="r num">${fmt(s.manual_adjustment,2)}</td><td class="r num"><b>${fmt(s.paid,2)}</b></td></tr>`).join('')}
        <tr><td><b>合计</b></td><td></td><td></td><td></td><td class="r num"><b>${fmt(r.total_paid,2)}</b></td></tr></table>
      <div class="mono-note" style="margin-top:6px">公式：应补偿=申报容量×1000×可靠容量系数×标准 ${fmt(r.compensation_standard,2)} 元/kW·年 ÷12×系数 ${fmt(r.coefficient,4)}×可获得率；实付=应补偿−扣减+调整。试算编号 ${esc(String(r.trial_ref).slice(0,8))}</div>`;
  }catch(e){ box.innerHTML = `<div class="errbar">试算被拒绝：${esc(e.message)}</div>`; }
}
async function loadSettle(){
  const el = document.getElementById('settle-ready');
  if(SETTLETAB===2){ loadSettleBench(); return; }
  if(!el) return;
  try{
    const R = await computeReadiness();
    el.innerHTML = readyListHTML(settleReadyRows(R));
  }catch(e){ el.innerHTML = '<div class="errbar">读取失败</div>'; }
}
function viewDemoSettleBody(){
  const c = SCN_DEMO[S.scn]; const eff = (185*Math.min(c.coeff,1)).toFixed(1);
  const subj=[['燃煤电厂A',1200,0.924,1840.0],['燃气电厂B',600,0.941,0],['水电站C',800,0.612,940.0],['储能电站D',200,0.500,632.0],['光伏电站E',1500,0.041,0]];
  return `${demoBanner}
  ${S.scn==='tight'?'<div class="hintbar warn">供需系数 1.00 触发封顶规则：结算按 min(系数,1) 执行。</div>':''}
  <div class="card" style="font-family:var(--mono);font-size:13px">月度补偿 ＝ 申报容量 × 可靠容量系数 × <b>${c.std}</b> 元/kW·年 ÷ 12 × <b>${c.coeff.toFixed(2)}</b> − 考核扣减<span style="color:var(--faint);font-size:11px;margin-left:10px">补偿标准有效值 ${eff} 元/kW·年</span></div>
  <div class="kpis">${[['年度补偿总盘子','7.83','亿元','t1'],['考核扣减合计','3,412','万元','t1'],['单位电量分摊','8.9','厘/kWh','t2'],['峰荷责任法对照','9.4','厘/kWh','t2']].map(([k,v,u,t])=>`<div class="kpi"><div class="k">${dot(t)}${k}</div><div class="v">${v}<small>${u}</small></div></div>`).join('')}</div>
  <div class="card"><h3>${dot('t1')}分主体月度结算表（${SCN_DEMO[S.scn].label}，示例口径）</h3>
  <table class="grid"><tr><th>主体</th><th class="r">申报容量(万kW)</th><th class="r">可靠容量系数</th><th class="r">可靠容量(万kW)</th><th class="r">标准有效值</th><th class="r">考核扣减(万元)</th><th class="r">当月补偿(万元)</th></tr>
  ${subj.map(([nm,cap,k,dz])=>{const rk=cap*k;const m=rk*parseFloat(eff)/12;return `<tr><td><b>${nm}</b></td><td class="r">${cap.toLocaleString('en-US')}</td><td class="r">${k.toFixed(3)}</td><td class="r">${rk.toFixed(1)}</td><td class="r">${eff}</td><td class="r">${dz.toFixed(1)}</td><td class="r"><b>${(m-dz).toFixed(1)}</b></td></tr>`;}).join('')}</table>
  <div class="fh" style="font-size:11px;color:var(--faint);margin-top:8px">示例数据：申报容量上限取自供给域核定结果（演示口径）；扣减明细对应履约考核记录（示例）。</div></div>`;
}
function viewDemoAuction(){
  const c = SCN_DEMO[S.scn];
  return `<div class="ptitle">报价与出清仿真</div><div class="psub">市场仿真核心（演示口径）：需求曲线 × 报价堆栈 → 统一出清价。</div>${demoBanner}
  <div class="kpis">${[['统一出清价',c.price,'元/kW·年','t3'],['中标容量','2,792','万kW',null],['采购完成度','97.6','%',null],['市场力审查触发','2','例（ACR个别审查）','t3']].map(([k,v,u,t])=>`<div class="kpi"><div class="k">${dot(t)}${k}</div><div class="v">${v}<small>${u||''}</small></div></div>`).join('')}</div>
  <div class="card"><h3>需求曲线 × 报价堆栈（示例图）</h3>
  <svg viewBox="0 0 640 260" style="width:100%">
    <rect x="140" y="30" width="460" height="170" fill="#FAEEE9" opacity=".55"/>
    ${[60,95,140,185,240,320,380].map((p,i)=>{const x0=140+i*57;const y=18+(1-p/400)*200;return `<rect x="${x0}" y="${y}" width="57" height="${218-y}" fill="#D8D4CB" stroke="#fff"/>`;}).join('')}
    <path d="M140,44 L${140+(98.9-90)/20*460},${44} L${140+(101.6-90)/20*460},${18+(1-139/400)*200} L${140+(105.8-90)/20*460},216" fill="none" stroke="var(--accent)" stroke-width="2.2"/>
    <circle cx="${140+(101.9-90)/20*460}" cy="${18+(1-163/400)*200}" r="5" fill="var(--t3)"/>
    <text x="${140+(101.9-90)/20*460+6}" y="${18+(1-163/400)*200+14}" font-size="10.5" fill="var(--t3)" font-weight="600">出清点 ≈ ${c.price}</text>
  </svg></div>
  <div class="hintbar">出清点位于 VRR 曲线陡降段：价格对容量偏差高度敏感（示例口径）。</div>`;
}
function viewDemoPerform(){
  const rows=[['考核时段界定','年度前 3% 顶峰时段（约 263 h）'],['预警方式','日前＋日内 4 h 提前量'],['考核费率锚','补偿标准（185 元/kW·年）'],['缺口计量','申报出力 − 实际出力'],['年度上限','扣减 ≤ 年度补偿总额'],['豁免条款','不可抗力＋检修备案']];
  return `<div class="ptitle">履约考核仿真</div><div class="psub">测算引擎⑥（演示口径）：青海式／PJM式两档预设对照。规则来源未核实前为设计表达。</div>${demoBanner}
  <div class="card"><h3>青海式（时段考核）规则要素（示例）</h3>
  <table class="grid">${rows.map(r=>`<tr><td style="width:150px"><b>${r[0]}</b></td><td>${r[1]}</td></tr>`).join('')}</table></div>
  <div class="card"><h3>激励相容性检验（示例）</h3>
  <table class="grid"><tr><th>情景</th><th class="r">申报容量</th><th class="r">预期考核扣减</th><th class="r">净补偿</th><th>结论</th></tr>
  <tr><td>场景B：满额申报</td><td class="r">200 万kW</td><td class="r">−1,840 万元</td><td class="r">2,930 万元</td><td>基准</td></tr>
  <tr><td>场景C：如实降额申报</td><td class="r">160 万kW</td><td class="r">−410 万元</td><td class="r">2,412 万元</td><td style="color:var(--warn)"><b>激励不相容 → 需收紧预警期外的偏差认定</b></td></tr></table></div>`;
}
function viewDemoAssess(){
  const c = SCN_DEMO[S.scn]; const eff=(185*Math.min(c.coeff,1)).toFixed(1);
  const rows=[['年度总盘子','7.83 亿元','8.14 亿元','市场口径含中标溢价'],['单位容量价格',eff+' 元/kW·年',c.price+' 元/kW·年','出清点位于陡降段'],['火电成本回收率','38.2%','41.5%','E&AS抵扣后缺口由容量收入覆盖'],['储能成本回收率','52.7%','58.9%','市场模式下时长价值显性化'],['可靠性达标','LOLE '+c.lole+' 天/年','同左','两模式共用同一需求底座'],['市场力指标','—','HHI 1,240｜触发 2 例','仅市场模式适用']];
  return `<div class="ptitle">情景评估与过渡对照</div><div class="psub">结果评估层（演示口径）：补偿与市场并排对照，随情景重算。</div>${demoBanner}
  <div class="card"><table class="grid"><tr><th>指标</th><th class="r">补偿模式</th><th class="r">市场模式</th><th>差异说明</th></tr>
  ${rows.map(r=>`<tr><td><b>${r[0]}</b></td><td class="r">${r[1]}</td><td class="r">${r[2]}</td><td style="font-size:12px;color:var(--sub)">${r[3]}</td></tr>`).join('')}</table></div>
  <div class="card"><h3>过渡路径（设计表达）</h3><table class="grid">
  <tr><td><b>① 延续补偿</b></td><td>完善核定与考核参数，年度校准 Net CONE</td></tr>
  <tr><td><b>② 放开需求曲线</b></td><td>行政定价 → VRR 曲线，价格锚取自 Net CONE</td></tr>
  <tr><td><b>③ 全市场</b></td><td>报价出清＋履约考核，补偿结算退出</td></tr></table></div>`;
}

/* ============ P-EFC: 草稿持久化与离开保护 ============ */
