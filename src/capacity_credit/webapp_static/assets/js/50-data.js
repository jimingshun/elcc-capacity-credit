let DATA_PROJECT = null, DATA_BATCH = null;
const GRADES = [
  ['G0_teaching','示例数据（G0，用于了解操作）'],
  ['G1_preliminary','G1 初步'],
  ['G2_research','G2 研究'],
  ['G3_rule_support','G3 规则支撑'],
];
function viewData(){
  return `
  <div class="ptitle">基础数据管理</div>
  <div class="psub">可靠性项目数据链：下载模板 → 填写CSV → 上传检查 → 核对摘要 → 确认冻结 → 生成样本集。上传与冻结走真实链路；成本项目不使用数据批次。</div>
  <div class="card"><h3>选择可靠性项目</h3><div id="dt-projects" class="loading">读取中…</div></div>
  <div id="dt-body"></div>`;
}
async function archiveProject(ref,name){
  if(!confirm('归档前确认：项目「'+name+'」将不再出现在默认列表（可在已归档列表恢复）。确认归档？')) return;
  await jpost('/api/projects/archive', {project_ref:ref});
  flash('已归档：'+name);
  initDataPage();
}
async function restoreProject(ref){
  await jpost('/api/projects/restore', {project_ref:ref});
  flash('已恢复');
  initDataPage();
}
async function initDataPage(){
  try{
    const projects = (await jget('/api/projects?archived=true')).projects;
    const rel = projects.filter(p=>p.kind==='reliability' && !p.invalid);
    const sel = document.getElementById('dt-projects');
    if(!rel.length){
      sel.innerHTML = '<span class="loading">尚无可靠性项目：请先在「我的项目」创建（项目类型选可靠性项目）。</span>';
      document.getElementById('dt-body').innerHTML = '';
      return;
    }
    sel.innerHTML = rel.filter(p=>!p.archived).map(p=>`<div class="rowitem"><div class="tt">${esc(p.name)}<small>冻结状态：${p.frozen===true?'已冻结':p.frozen===false?'未冻结':'未知'}｜最近数据操作：${esc(p.latest_batch_state||'未知')}</small></div>
      <button class="btn ${DATA_PROJECT===p.project_dir?'pri':''}" onclick="DATA_PROJECT='${esc(p.project_dir)}';DATA_BATCH=null;initDataPage()">管理</button>
      <button class="btn" onclick="archiveProject('${esc(p.project_dir)}','${esc(p.name)}')">归档</button></div>`).join('')
      + (rel.some(p=>p.archived) ? '<div class="fh" style="margin-top:6px">已归档项目：'+rel.filter(p=>p.archived).map(p=>`${esc(p.name)} <button class="btn" style="padding:2px 8px" onclick="restoreProject('${esc(p.project_dir)}')">恢复</button>`).join('｜')+'</div>' : '');
    if(!DATA_PROJECT || !rel.some(p=>p.project_dir===DATA_PROJECT)){
      // P-D: never auto-pick the first project - ask the user instead
      sel.innerHTML += '<div class="fh" style="margin-top:6px">请在上方选择一个项目进行管理（系统不默认选择）。</div>';
      document.getElementById('dt-body').innerHTML = '';
      return;
    }
    await renderDataBody();
  }catch(e){ document.getElementById('dt-projects').innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function renderDataBody(){
  const body = document.getElementById('dt-body');
  body.innerHTML = '<div class="loading">读取模板清单…</div>';
  const t = await jget('/api/data/templates?project=' + encodeURIComponent(DATA_PROJECT));
  body.innerHTML = `
  <div class="card"><h3>数据清单（需要哪些表）</h3>
    <div class="psub">每张表是可填写的固定CSV（utf-8-sig）；示例数值不会作为您的正式数据。来源说明不等于数据质量认证。</div>
    ${t.slots.map(s=>`<div class="rowitem"><div class="tt">${esc(s.title)}<small>${s.required?'公共必需表':'资源可选表'}${s.column_notes&&s.column_notes.length?'｜字段说明：'+s.column_notes.map(n=>esc(n[0])).join('、'):''}</small></div>
      <a class="btn" href="/api/data/template?project=${encodeURIComponent(DATA_PROJECT)}&slot=${encodeURIComponent(s.table_key)}">下载 ${esc(s.table_key)}</a></div>`).join('')}
    ${(t.notes||[]).map(n=>`<div class="mono-note">${esc(n)}</div>`).join('')}
  </div>
  <div class="card"><h3>上传检查</h3>
    <div class="fgrid">
      <div class="fld"><label><b>声明数据等级</b>（不得超过数据可支持的最高等级）</label>
        <select data-f="grade">${GRADES.map(([v,l])=>`<option value="${v}">${esc(l)}</option>`).join('')}</select></div>
    </div>
    <div id="dt-uploads">${t.slots.map(s=>`<div class="rowitem"><div class="tt">${esc(s.title)} <span class="mono-note">${esc(s.table_key)}</span></div>
      <input type="file" accept=".csv" data-slot="${esc(s.table_key)}"></div>`).join('')}</div>
    <div style="margin-top:10px"><button class="btn pri" onclick="uploadData()">检查数据</button></div>
    <div id="dt-import-result"></div>
  </div>
  <div class="card"><h3>核对摘要并确认冻结</h3>
    <div id="dt-summary" class="loading">上传检查通过后，此处显示批次摘要与哈希。</div>
    <div class="fgrid" style="margin-top:10px">
      <div class="fld"><label><b>审批人</b>（必填）</label><input id="fr-by" placeholder="如：数据管理岗"></div>
      <div class="fld"><label><b>审批说明</b>（必填）</label><input id="fr-comment" placeholder="如：已核对批次内容与哈希"></div>
    </div>
    <button class="btn pri" onclick="freezeData()">确认冻结</button>
  </div>
  <div class="card"><h3>生成模拟样本集</h3>
    <div class="fgrid">
      <div class="fld"><label><b>根种子</b>（整数，由您显式选择）</label><input class="num" id="bk-seed" value="7"></div>
      <div class="fld"><label>样本数</label><input class="num" id="bk-samples" value="30"></div>
      <div class="fld"><label>各年占比方案</label><input class="num" id="bk-weight" value="w1"></div>
    </div>
    <button class="btn pri" onclick="makeBank()">生成样本集</button>
    <div id="dt-bank"></div>
  </div>`;
}
async function uploadData(){
  const slotInputs = [...document.querySelectorAll('#dt-uploads input[type=file]')].filter(i=>i.files.length);
  if(!slotInputs.length){ flash('请至少为一个数据表选择CSV文件'); return; }
  const grade = document.querySelector('#dt-body select[data-f="grade"]').value;
  const files = [];
  for(const inp of slotInputs){
    const f = inp.files[0];
    const b64 = await new Promise(res=>{ const r = new FileReader(); r.onload = ()=>res(String(r.result).split(',')[1]||''); r.readAsDataURL(f); });
    files.push({slot: inp.dataset.slot, name: f.name, b64});
  }
  try{
    const r = await jpost('/api/data/import', {project_ref: DATA_PROJECT, declared_grade: grade, files});
    const view = r.view || {};
    document.getElementById('dt-import-result').innerHTML =
      `<div class="${view.status_tone==='success'?'hintbar':'errbar'}">${esc(view.status_label||'已提交')}</div>
       <details class="disc"><summary>提交投影（完整明细）</summary><div class="body">${kvTable(r.result)}</div></details>`;
    const res = r.result || {};
    DATA_BATCH = res.batch_id || res.import_batch_id || res.batch || null;
    if(DATA_BATCH) await loadSummary();
    else document.getElementById('dt-summary').innerHTML = '<div class="loading">提交结果未包含批次号：如被拒绝，请按明细修正后重试。</div>';
  }catch(e){
    document.getElementById('dt-import-result').innerHTML = `<div class="errbar">${esc(e.kind||'')}：${esc(e.message)}</div>`;
  }
}
async function loadSummary(){
  if(!DATA_BATCH) return;
  try{
    const s = await jget('/api/data/summary?project=' + encodeURIComponent(DATA_PROJECT) + '&batch=' + encodeURIComponent(DATA_BATCH));
    document.getElementById('dt-summary').innerHTML =
      `<div class="hintbar">批次 <span class="num">${esc(DATA_BATCH)}</span>｜摘要哈希：<span class="num">${esc(s.summary_hash||'不可用')}</span>${s.evidence_error?'｜'+esc(s.evidence_error):''}</div>
       <details class="disc"><summary>批次文件清单（含哈希）</summary><div class="body">${kvTable(s)}</div></details>`;
  }catch(e){ document.getElementById('dt-summary').innerHTML = `<div class="errbar">${esc(e.message)}</div>`; }
}
async function freezeData(){
  if(!DATA_BATCH){ flash('尚无批次：请先完成上传检查并取得批次号'); return; }
  try{
    const s = await jget('/api/data/summary?project=' + encodeURIComponent(DATA_PROJECT) + '&batch=' + encodeURIComponent(DATA_BATCH));
    const r = await jpost('/api/data/freeze', {
      project_ref: DATA_PROJECT, batch: DATA_BATCH,
      reviewed_by: document.getElementById('fr-by').value,
      review_comment: document.getElementById('fr-comment').value,
      summary_hash: s.summary_hash || '',
    });
    flash('确认与冻结完成（以服务返回为准）');
    document.getElementById('dt-summary').innerHTML += `<details class="disc" open><summary>冻结结果</summary><div class="body">${kvTable(r)}</div></details>`;
  }catch(e){ flash('冻结被拒绝：' + e.message); }
}
async function makeBank(){
  try{
    const r = await jpost('/api/data/bank', {
      project_ref: DATA_PROJECT, batch: DATA_BATCH,
      root_seed: Number(document.getElementById('bk-seed').value),
      samples: Number(document.getElementById('bk-samples').value),
      weight_set_id: document.getElementById('bk-weight').value,
    });
    document.getElementById('dt-bank').innerHTML = `<div class="hintbar">样本集已生成：银行 <span class="num">${esc(r.scenario_bank_id)}</span>｜天气年 <span class="num">${esc(r.weather_years)}</span>｜根种子 <span class="num">${esc(r.root_seed)}</span></div>`;
  }catch(e){ document.getElementById('dt-bank').innerHTML = `<div class="errbar">${esc(e.kind||'')}：${esc(e.message)}</div>`; }
}

/* ============ P-D 可靠性测算向导（类型隔离；绝不默认选项目） ============ */
