const DRAFT_KEY='elcc-tool-draft-v1';
function saveDraft(){
  try{
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      page:PAGE, cost:{fn:COST_FUNCTION, step:COST_STEP, project:COST_PROJECT, form:COST_FORM},
      rel:{method:RM.method, step:RM.step, done:RM.done, project:RM.project, projectName:RM.projectName, sel:RM.sel, runRef:RM.runRef, calibAck:RM.calibAck, otab:RM.otab},
      results:{project:RESULTS_PROJECT, run:RESULTS_RUN},
    }));
  }catch(e){ /* 存储不可用时静默降级：草稿只在内存 */ }
}
function restoreDraft(){
  try{
    const raw=localStorage.getItem(DRAFT_KEY); if(!raw) return;
    const d=JSON.parse(raw);
    if(d.cost){ COST_FUNCTION=d.cost.fn||null; COST_STEP=d.cost.step||'fn'; COST_PROJECT=d.cost.project||null; COST_FORM=d.cost.form||{}; }
    if(d.rel){ RM.method=d.rel.method||'elcc'; RM.step=d.rel.step||0; RM.done=d.rel.done||{}; RM.project=d.rel.project||null;
      RM.projectName=d.rel.projectName||''; RM.sel=d.rel.sel||{}; RM.runRef=d.rel.runRef||null; RM.calibAck=!!d.rel.calibAck; RM.otab=d.rel.otab||0; }
    if(d.results){ RESULTS_PROJECT=d.results.project||null; RESULTS_RUN=d.results.run||null; }
    if(d.page&&(d.cost.project||d.rel.project||d.page==='home')) PAGE=d.page;
  }catch(e){ /* 损坏草稿忽略：从首页开始 */ }
}
function draftDirty(){
  return !!(COST_PROJECT||RM.project||(COST_FORM&&Object.keys(COST_FORM).length));
}
window.addEventListener('beforeunload', e=>{
  saveDraft();
  if(draftDirty()){ e.preventDefault(); e.returnValue=''; }
});

/* ============ 初始化 ============ */
/* Step1（2026-09-28 设计稿）：模式切换带落地页——切市场模式落市场仿真总览
 * （MKTTAB 归零），切补偿模式落首页（两层分区）；参数底座两层共用不锁。
 * 首页市场卡「切换并进入」也走 switchMode（同模式时仅落地不重复切换）。 */
function switchMode(m){
  S.mode = m;
  document.querySelectorAll('#modeSeg button').forEach(x=>x.classList.toggle('on', x.dataset.m===m));
  if(m==='mkt'){ MKTTAB=0; PAGE='market'; flash('已切换至市场模式：落在市场仿真总览（就绪度＋①—⑦页内标签）'); }
  else { PAGE='home'; flash('已切换至补偿模式：落在首页（两层分区）'); }
  rerender();
}
document.querySelectorAll('#modeSeg button').forEach(b=>{
  b.onclick = ()=>{
    if(S.mode===b.dataset.m) return;
    switchMode(b.dataset.m);
  };
});
document.querySelector('#modeSeg button[data-m="comp"]').classList.add('on');
const urlMode = new URLSearchParams(location.search).get('mode');
if(urlMode==='mkt'||urlMode==='comp'){
  S.mode = urlMode;
  document.querySelectorAll('#modeSeg button').forEach(x=>x.classList.toggle('on', x.dataset.m===S.mode));
}
// ?tab=<n>：市场落地页直达页内标签（README/演示链接；仅初始加载消费一次）
const urlTab = new URLSearchParams(location.search).get('tab');
if(urlTab!==null){
  const n = parseInt(urlTab, 10);
  if(Number.isFinite(n) && n>=0 && n<=7){ S.mode = 'mkt'; MKTTAB = n; PAGE = 'market'; }
}
restoreDraft();
rerender();
loadHome();
