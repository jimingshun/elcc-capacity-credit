/* ============ 全局状态（演示口径的模式仅影响演示页；情景选择器已按用户2026-09-17决定移除，scn固定为base供06演示页取数） ============ */
const S = { mode:'comp', scn:'base' };
const TIER = { t1:'var(--t1)', t2:'var(--t2)', t3:'var(--t3)' };
let COST_PROJECT = null;   // {project_dir,name}
let COST_FUNCTION = null;  // cost_fixed | cost_gross_cone | cost_net_cone
let COST_STEP = 'fn';      // fn | project | form | check
let COST_FORM = {};        // draft (in-memory; 刷新即失，帮助页如实说明)
let RESULTS_PROJECT = null, RESULTS_RUN = null;

const NAV = [
  { grp:'入口' },
  { id:'home', t:'首页' },
  { id:'projects', t:'我的项目' },
  { id:'results', t:'结果与报告' },
  { id:'batches', t:'研究批次' },
  { id:'help', t:'使用帮助' },
  { grp:'① 先算参数 · 参数底座（两层共用）' },
  { id:'data', t:'基础数据管理', idx:'01', tier:'t2' },
  { id:'params', t:'参数配置中心', idx:'02', tier:'t2', demo:true },
  { id:'supply', t:'容量供给类参数', idx:'03', tier:'t1', sel:'select' },
  { id:'demand', t:'容量需求类参数', idx:'04', tier:'t1' },
  { grp:'② 再用参数做业务 · 应用层' },
  { id:'settle', t:'年度容量补偿结算', idx:'05', mode:'comp', tier:'t1' },
  { id:'market', t:'容量市场仿真', idx:'06', mode:'mkt', tier:'t1' },
];
const CRUMB = {
  home:['首页'], projects:['我的项目'], results:['结果与报告'], help:['使用帮助'],
  cost:['当前项目','测算引擎','成本测算'],
  data:['当前项目','参数底座','基础数据管理'],
  params:['当前项目','参数配置中心（演示）'],
  accred:['当前项目','系统可靠性评估（充裕度）'],
  select:['首页','选择测算类型'],
  supply:['① 先算参数','容量供给类参数'],
  demand:['① 先算参数','容量需求类参数'],
  settle:['② 再用参数做业务','年度容量补偿结算'],
  market:['② 再用参数做业务','容量市场仿真'],
  tcap:['① 先算参数','目标可靠容量测算'],
  gap:['① 先算参数','容量缺口与备用裕度'],
  coef:['① 先算参数','容量供需系数'],
  batches:['研究批次','矩阵与标定（B1）'],
  relmod:['当前项目','测算工作区','ELCC · EFC 测算（六步）'],
};
let PAGE = 'home';

/* ============ API ============ */
async function api(method, path, body) {
  const opt = { method, headers: {'Content-Type':'application/json'} };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const resp = await fetch(path, opt);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!resp.ok) {
    const err = new Error((data && data.message) || ('HTTP ' + resp.status));
    err.kind = data && data.kind; err.payload = data; throw err;
  }
  return data;
}
const jget = (p) => api('GET', p);
const jpost = (p, b) => api('POST', p, b);
let toastTimer = null;
function flash(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 2600);
}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = (v, d=4) => (typeof v === 'number' ? v.toLocaleString('en-US', {maximumFractionDigits:d}) : esc(v));
const dot = (tier) => tier ? `<span class="dot" style="background:${TIER[tier]}"></span>` : '';

/* ============ Step1 两层交互：就绪度只读派生（共享助手，home/settle/market 三处复用） ============
 * 数据源全部为既有只读端点：/api/projects（项目清单）＋逐项目 /api/results?project=
 * （统一结果索引行）＋已发布行的 /api/runs/projection（点值在场判定）。设计稿提到的
 * /api/results/recent 仅取最近数条，不能作全工作区就绪判定。
 * Step 2 v2（2026-09-29 R1 授权，设计稿 §6）：①成本域按结果索引行 method 判（旧无类型
 * 成本项目不再漏报）；②ELCC/EFC 计就绪须已发布且取到点值（capacity_view.
 * accredited_capacity_mw；未收敛终态不给点值、不标就绪）；③无扫描上限，旧无类型可靠性
 * 项目一并纳入。就绪度只导航不代做；派生失败＝该块「读取失败」。 */
async function readinessHasPointValue(projectRef, runRef){
  /* 只读投影：已发布的搜索类运行若未收敛，点值保持 null（引擎不造数）——
   * 就绪度同样不把「计算完成但无点值」标成就绪。ELCC/EFC 看
   * accredited_capacity_mw；目标可靠容量看 target_capacity_mw（双键）。 */
  try{
    const d = await jget('/api/runs/projection?project='+encodeURIComponent(projectRef)+'&run='+encodeURIComponent(runRef));
    const cv = d && d.projection && d.projection.capacity_view;
    if(!cv) return false;
    return (cv.accredited_capacity_mw !== null && cv.accredited_capacity_mw !== undefined)
        || (cv.target_capacity_mw !== null && cv.target_capacity_mw !== undefined);
  }catch(e){ return false; }
}
async function computeReadiness(){
  const d = await jget('/api/projects');
  const projects = (d.projects||[]).filter(p=>!p.archived);
  let netCone = false, accredit = false, targetCap = false;
  /* v2：就绪判定一律以统一结果索引的行为准（成本域按 method，可靠性域含旧无类型
   * 项目）；不再只凭登记字段（measurement_type＋formal_result）判，避免旧无类型
   * 成本项目漏报。Step2 A6：targetCap 切真实派生（行 method 判定＋点值在场）。
   * 单项目索引读取失败不拖垮整体派生：其余项目继续核。 */
  for(const p of projects){
    if(netCone && accredit && targetCap) break;
    try{
      const idx = await jget('/api/results?project='+encodeURIComponent(p.project_dir));
      const rows = idx.rows||[];
      if(!netCone && rows.some(r=>r.method==='cost_net_cone' && r.status_label==='计算完成')) netCone = true;
      if(!accredit){
        for(const r of rows){
          if((r.method==='elcc'||r.method==='efc') && r.status_label==='计算完成' && r.run_ref){
            if(await readinessHasPointValue(p.project_dir, r.run_ref)){ accredit = true; break; }
          }
        }
      }
      if(!targetCap){
        for(const r of rows){
          if(r.method==='demand_target_capacity' && r.status_label==='计算完成' && r.run_ref){
            if(await readinessHasPointValue(p.project_dir, r.run_ref)){ targetCap = true; break; }
          }
        }
      }
    }catch(e){ /* 单项目索引读取失败不拖垮整体派生 */ }
  }
  /* Step3 S1-M2：priceTable/coeffFrozen 切真实派生（只读既有端点，不新造数据源）。
   * priceTable＝存在已录价表的结算项目（价表 current 非空）；coeffFrozen＝存在
   已冻结系数（S0 冻结列表非空）。单点读取失败＝该旗如实保持未就绪。 */
  let priceTable = false;
  for(const p of projects){
    if(p.kind!=='settlement') continue;
    try{
      const pt = await jget('/api/settlement/price-table?project='+encodeURIComponent(p.project_dir));
      if(pt.current){ priceTable = true; break; }
    }catch(e){ /* 单项目读取失败不拖垮整体派生 */ }
  }
  let coeffFrozen = false;
  try{
    const cl = await jget('/api/demand/coefficient/list');
    coeffFrozen = (cl.rows||[]).some(r=>!r.invalid);
  }catch(e){ /* 系数列表读取失败＝保持未就绪 */ }
  return {
    netCone,                              // 结算：Net CONE 锚定（供给参数正式结果）
    accredit,                             // 市场：ELCC/EFC 可申报容量（已发布且取到点值）
    coeff: targetCap && accredit,         // 系数可测算＝分子（目标容量点值）与分母（认可容量点值）双就绪（公式 v1 已定案，派生自既有两旗，不新造数据源）
    coeffFrozen,                          // Step3 S0：已有冻结正式系数（结算引用的前置）
    targetCap,                            // 市场：目标可靠容量（Step2 A 真实派生）
    priceTable,                           // Step3 S1：补偿价表已录入（政策来源在册）
  };
}
function readyListHTML(rows){
  return `<ul class="ready">${rows.map(r=>`
    <li><span class="${r.ok?'ryes':'rno'}">${r.ok?'✓':'—'}</span><span>${esc(r.k)} <span class="rnote">（${esc(r.note)}）</span></span>${r.jump?`<a href="javascript:void(0)" onclick="PAGE='${r.jump}';rerender()">${esc(r.jt)} →</a>`:''}</li>`).join('')}</ul>`;
}
function settleReadyRows(R){
  return [
    { ok:R.netCone, k:'Net CONE 锚定（供给参数）', note:R.netCone?'已有正式结果':'未测算', jump:R.netCone?'results':'supply', jt:R.netCone?'查看':'去测算' },
    { ok:R.coeffFrozen, k:'供需系数（需求参数）', note:R.coeffFrozen?'已有正式系数（已冻结）':(R.coeff?'可测算未冻结（04 页冻结后进正式链路）':'未测算（需目标可靠容量与认可容量双点值就绪）'), jump:'coef', jt:R.coeffFrozen?'查看':'去测算' },
    { ok:R.priceTable, k:'补偿价表（政策来源）', note:R.priceTable?'已录入（政策来源在册）':'未录入（结算工作台录入）', jump:'settle', jt:'去录入' },
  ];
}
function marketReadyRows(R){
  return [
    { ok:R.targetCap, k:'目标可靠容量 → 需求曲线', note:R.targetCap?'已有正式结果':'未测算（Step 2 已接线）', jump:R.targetCap?'results':'demand', jt:R.targetCap?'查看':'去测算' },
    { ok:R.accredit, k:'ELCC/EFC 可申报容量（供给参数）', note:R.accredit?'已有正式结果':'未测算', jump:R.accredit?'results':'supply', jt:R.accredit?'查看':'去测算' },
    { ok:false, k:'出清内核', note:'演示口径，无正式内核', jump:null, jt:null },
  ];
}


/* ============ 壳层 ============ */
function renderNav(){
  document.getElementById('nav').innerHTML = NAV.map(n=>{
    if(n.grp) return `<div class="grp">${n.grp}</div>`;
    const dis = n.mode && n.mode !== S.mode;
    const badge = n.demo ? '<span style="margin-left:auto" class="tag plan">演示</span>'
      : n.plan ? '<span style="margin-left:auto" class="tag plan">规划中</span>'
      : n.slice ? `<span style="margin-left:auto" class="tag lock">切片${n.slice}</span>` : '';
    return `<button data-id="${n.id}" class="${PAGE===n.id?'on':''} ${dis?'dis':''}">${n.idx?`<span class="idx">${n.idx}</span>`:''}${dot(n.tier)}${n.t}${dis?`<span style="margin-left:auto" class="tag lock">${n.mode==='mkt'?'市场模式':'补偿模式'}</span>`:badge}</button>`;
  }).join('');
  document.querySelectorAll('#nav button').forEach(b=>{
    b.onclick = ()=>{
      const item = NAV.find(x=>x.id===b.dataset.id);
      if(item.mode && item.mode !== S.mode){
        flash(`「${item.t}」仅在${item.mode==='mkt'?'市场':'补偿'}模式下可用，请先切换顶栏模式开关`);
        return;
      }
      let target = item.sel || item.id;  /* Step1：03=容量供给类参数恒落类型选择页（旧03跟随REL_STACK的行为随混合入口退役） */
      PAGE = target; rerender();
    };
  });
  document.getElementById('sidefoot').innerHTML = (COST_PROJECT
    ? `当前项目：<span class="pname">${esc(COST_PROJECT.name)}</span>`
    : '当前项目：<span class="pname">未选择</span>')
    + `<span class="mbadge ${S.mode}">${S.mode==='comp'?'补偿模式':'市场模式'}</span><br>成本项目历史随时可在「结果与报告」找回`;
  const c = CRUMB[PAGE] || ['首页'];
  document.getElementById('crumb').innerHTML = c.map((x,i)=> i===c.length-1?`<b>${esc(x)}</b>`:esc(x)).join(' ／ ');
}
function rerender(){
  saveDraft();
  const v = document.getElementById('view');
  if(PAGE==='home') { v.innerHTML = viewHome(); loadHome(); } /* loadHome 随渲染挂载（修复：导航回首页时就绪度/最近卡不再停留「读取中」） */
  else if(PAGE==='projects') { v.innerHTML = viewProjects(); loadProjects(); }
  else if(PAGE==='results') { v.innerHTML = viewResults(); loadResultsPage(); }
  else if(PAGE==='data') { v.innerHTML = viewData(); initDataPage(); }
  else if(PAGE==='wizard') { PAGE='supply'; v.innerHTML = viewSelectType(); } /* Step1：旧05深链重定向到容量供给类参数入口 */
  else if(PAGE==='select'||PAGE==='supply') { v.innerHTML = viewSelectType(); }
  else if(PAGE==='cost') { v.innerHTML = viewCost(); }
  else if(PAGE==='help') v.innerHTML = viewHelp();
  else if(PAGE==='batches') { v.innerHTML = viewBatches(); loadBatches(); }
  else if(PAGE==='params') v.innerHTML = viewDemoParams();
  else if(PAGE==='settle') { v.innerHTML = viewSettle(); loadSettle(); }
  else if(PAGE==='market'||PAGE==='mkt1'||PAGE==='mkt2'||PAGE==='mkt3'||PAGE==='mkt4'||PAGE==='mkt5'||PAGE==='mkt6'||PAGE==='mkt7'||PAGE==='perform'||PAGE==='auction'||PAGE==='assess'){ PAGE='market'; v.innerHTML = viewMarket(); loadMarket(); } /* Step1：市场落地页（七步/履约考核收进页内标签；旧键一律重定向） */
  else if(PAGE==='relmod') { v.innerHTML = viewRelMod(); renderRelMod(); }
  else if(PAGE==='accred') { v.innerHTML = viewAccred(); renderRelPage(); }
  else if(PAGE==='demand') { v.innerHTML = viewDemand(); loadDemand(); }
  else if(PAGE==='tcap') { v.innerHTML = viewTcapWizard(); renderTcapPage(); } /* Step2 A4：目标可靠容量向导 */
  else if(PAGE==='gap') { v.innerHTML = viewGapCalc(); } /* Step2 B：缺口与裕度试算 */
  else if(PAGE==='coef') { v.innerHTML = viewCoefCalc(); coefLoadSameYear(); } /* Step2 C 计算器＋Step3 S0 冻结/同年比对 */
  renderNav();
  if(PAGE==='cost') renderCostPage();
  window.scrollTo(0,0);
}

/* ============ 首页 ============ */
