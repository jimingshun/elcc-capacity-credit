const RM = { method:'elcc', step:0, done:{}, project:null, projectName:'', options:null,
             slots:null, sel:{}, runRef:null, view:null, proj:null, otab:0, running:false, calibAck:false,
             calib:null };
const RM_STEPS = [
  {no:'00', nm:'选择 / 新建项目', st:'类型锁定，一项目一结果'},
  {no:'01', nm:'基准系统输入数据', st:'数据卡 + 完备性校验'},
  {no:'02', nm:'基准系统可靠性标定', st:'先测现状，再逐档试到达标'},
  {no:'03', nm:'接入待测资源', st:'候选资源 + 情景设定'},
  {no:'04', nm:'等效负荷增量搜索', st:'等风险搜索（真实运行）'},
  {no:'05', nm:'输出结果与报告', st:'三层输出 + 报告导出'},
];
const RM_METHOD_NOTE = {
  elcc:'<b>ELCC</b>：等风险意义下候选资源可承载的容量。判据＝所选风险指标（LOLE/LOLH/EUE/NEUE），输出有效承载容量（MW）与容量信用率。',
  efc:'<b>EFC</b>：与候选资源同系统风险的 100% 可靠完美容量（MW）。流程与 ELCC 同源，差异见下方提示条。',
};
const RM_DIFF = ['判据：原型为冬季晚峰失负荷期望（交付年口径）——当前引擎按所选风险指标，交付年口径属方法扩展，未接入',
  '输出：原型为分技术类别降额系数表——本工具按“一类资源一个项目”分别测算，步骤05的跨项目聚合表已可汇总各项目正式结果并给出降额系数/容量信用率；同项目内多类别一键测算未接线',
  '负荷数据：冬季高峰月份侧重口径未接入（引擎按冻结数据全年时序）',
  '拍卖衔接：T-4 / T-1 交付年协议关联属市场模块（规划中）',
  '结果用途：容量市场参拍资格与协议容量核定（规划中）'];
function isEFC(){ return RM.method==='efc'; }
function viewRelMod(){
  return `
  <div class="ptitle">可靠容量测算：ELCC / EFC</div>
  <div class="psub">按「基准系统输入数据 → 基准标定 → 接入待测资源 → 等效负荷增量搜索 → 输出结果」组织；能力未接入的环节如实标注，不用演示数字冒充。</div>
  <div class="methodbar"><span style="font-size:12.5px;color:var(--sub)">测算方法</span>
    <div class="mseg" id="rmSeg">
      <button data-m="elcc" class="${!isEFC()?'on':''}">ELCC 有效负荷承载能力</button>
      <button data-m="efc" class="efc ${isEFC()?'on efc':''}">EFC 等效可靠容量</button>
    </div>
    <div class="methodnote">${RM_METHOD_NOTE[RM.method]}</div></div>
  ${isEFC()?`<div class="diffbar"><b>EFC 口径差异（相对 ELCC 流程，含未接入项的诚实说明）：</b>${RM_DIFF.map(d=>`<span class="di">${esc(d)}</span>`).join('')}</div>`:''}
  <div class="steprail" id="rmRail"></div>
  <div id="rmBody"></div>`;
}
function rmRailHTML(){
  return RM_STEPS.map((s,i)=>{
    const done=!!RM.done[i], on=RM.step===i;
    const lock=!done&&!on&&i>0&&!RM.done[i-1]&&!(i===2&&RM.done[1]);
    const stx = done?(i===2?'已确认':'已完成'):on?'进行中':lock?'待前序完成':s.st;
    return `<div class="srail ${on?'on':''} ${done?'done':''} ${i===2&&!done&&!on?'ready':''} ${lock?'lock':''}" data-i="${i}">
      <div class="no">${s.no}</div><div class="nm">${s.nm}</div><div class="stx">${lock?'🔒 ':''}${stx}</div></div>`;
  }).join('');
}
function renderRelMod(){
  const rail=document.getElementById('rmRail');
  if(rail){
    rail.innerHTML=rmRailHTML();
    rail.querySelectorAll('.srail').forEach(el=>{ el.onclick=()=>{
      if(el.classList.contains('lock')){ flash('请先完成前序步骤（顺序锁定：基准先于资源接入，资源接入先于增量搜索）'); return; }
      RM.step=+el.dataset.i; renderRelMod(); }; });
  }
  const seg=document.getElementById('rmSeg');
  if(seg) seg.querySelectorAll('button').forEach(b=>{ b.onclick=()=>{
    const m=b.dataset.m; if(m===RM.method) return;
    if(RM.step>0||RM.runRef){ if(!confirm('切换方法将清空当前流程状态（项目与已有结果不受影响），确认切换？')) return; }
    RM.method=m; RM.step=0; RM.done={}; RM.project=null; RM.options=null; RM.sel={}; RM.runRef=null; RM.view=null; RM.calibAck=false; RM.calib=null;
    renderRelMod(); }; });
  const body=document.getElementById('rmBody'); if(!body) return;
  if(RM.step===0) rmStep0(body);
  else if(RM.step===1) rmStep1(body);
  else if(RM.step===2) rmStep2(body);
  else if(RM.step===3) rmStep3(body);
  else if(RM.step===4) rmStep4(body);
  else rmStep5(body);
}
function rmComplete(i){ RM.done[i]=true; RM.step=i+1; renderRelMod(); }
/* ---- 步骤 00：选择 / 新建项目（真实） ---- */
function rmStep0(body){
  const title=isEFC()?'EFC测算':'ELCC测算';
  body.innerHTML=`
  <div class="card"><h3>新建${esc(title)}项目</h3>
    <div class="psub">项目创建后即锁定为「${esc(title)}」类型；一个项目对应一次业务测算，完成后只读（改参数用“复制为新项目”）。</div>
    <div class="fgrid">
      <div class="fld" id="rm-f-name"><label><b>项目名称</b>（必填）</label><input id="rm-name" placeholder="如：青海2028光伏ELCC测算"></div>
    </div>
    <div class="fh" style="margin:4px 0 8px">储能/水电资源构成申报已移至步骤 01（输入数据清单顶部）——申报决定可选模板槽位，创建时无需先行确认。</div>
    <button class="btn pri" onclick="rmCreateProject()">新建${esc(title)}项目并进入步骤 01</button></div>
  <div class="card"><h3>该方法的未完成项目（继续测算）</h3><div id="rm-open" class="loading">读取中…</div></div>
  <div class="card"><h3>该方法的已完成项目（只读）</h3><div id="rm-done" class="loading">读取中…</div></div>`;
  jget('/api/projects').then(d=>{
    const mine=d.projects.filter(p=>p.kind==='reliability'&&!p.invalid&&(!p.measurement_type||p.measurement_type===RM.method));
    const open=mine.filter(p=>!p.formal_result), done=mine.filter(p=>p.formal_result);
    document.getElementById('rm-open').innerHTML = open.length?open.map(p=>
      `<div class="rowitem"><div class="tt">${esc(p.name)}<small>${p.measurement_type?esc(isEFC()?'EFC测算':'ELCC测算'):'遗留可靠性项目（未锁定类型）'}｜进行中</small></div>
       <button class="btn pri" onclick="rmPick('${esc(p.project_dir)}','${esc(p.name)}')">载入并进入步骤 01</button>
       <button class="btn" onclick="rmDeleteProject('${esc(p.project_dir)}','${esc(p.name)}')">删除</button></div>`).join('')
      :'<span class="loading">该方法暂无未完成项目。</span>';
    document.getElementById('rm-done').innerHTML = done.length?done.map(p=>
      `<div class="rowitem"><div class="tt">${esc(p.name)}<small><span class="tag ok">已完成</span>｜正式结果 ${esc(String((p.formal_result||{}).run_ref||'').slice(0,8))}</small></div>
       <button class="btn" onclick="openResult('${esc(p.project_dir)}','reliability','${esc((p.formal_result||{}).run_ref||'')}')">查看结果</button>
       <button class="btn" onclick="copyProject('${esc(p.project_dir)}')">复制为新项目</button></div>`).join('')
      :'<span class="loading">该方法暂无已完成项目。</span>';
  }).catch(e=>{ document.getElementById('rm-open').innerHTML=`<div class="errbar">${esc(e.message)}</div>`; });
}
async function rmCreateProject(){
  const name=document.getElementById('rm-name').value.trim();
  const fld=document.getElementById('rm-f-name');
  if(!name){ fld.classList.add('err'); flash('项目名称不能为空'); return; }
  fld.classList.remove('err');
  try{
    const r=await jpost('/api/projects',{name,description:'',project_kind:'reliability',measurement_type:RM.method});
    flash('项目创建成功（类型已锁定）：'+r.name);
    rmPick(r.project_ref,r.name);
  }catch(e){ fld.classList.add('err'); flash('创建被拒绝：'+e.message); }
}
function rmPick(ref,name){ REL_STACK='relmod'; RM.project=ref; RM.projectName=name; DATA_PROJECT2=ref; RM.step=1; RM.done[0]=true; RM.calib=null; RM.calibAck=false; renderRelMod(); }
async function rmDeleteProject(ref,name){
  if(!confirm('删除确认：项目「'+name+'」将以归档方式从测算列表移除（不销毁项目目录与已有运行记录，可在「01 基础数据管理」的已归档列表恢复）。确认删除？')) return;
  try{
    await jpost('/api/projects/archive',{project_ref:ref});
    if(RM.project===ref){ RM.project=null; RM.projectName=null; RM.step=0; RM.done={}; RM.options=null; RM.sel={}; RM.runRef=null; RM.view=null; RM.calib=null; }
    flash('已删除（归档）：'+name+'——可在「01 基础数据管理」已归档列表恢复');
    renderRelMod();
  }catch(e){ flash('删除被拒绝：'+e.message); }
}
/* ---- 步骤 01：基准系统输入数据（真实模板/冻结状态） ---- */
const RM_CARDS=[
  {t:'负荷数据',keys:['load_hourly'],req:'必备',note:'全年逐时负荷曲线；LDC 由曲线派生'},
  {t:'常规机组',keys:['thermal_units','thermal_reliability_classes','thermal_unit_reliability_map'],req:'必备',note:'铭牌容量、可靠性分类与机组映射（FOR 口径由数据契约定义）'},
  {t:'新能源出力',keys:['renewable_hourly','renewable_capacities'],req:'必备',note:'风/光逐时出力与分年装机，与负荷同一段天气年份'},
  {t:'水电',keys:['hydro'],req:'按系统构成',note:'分月可调容量/来水约束（声明包含水电时必需）'},
  {t:'储能',keys:['storage'],req:'按系统构成',note:'功率/能量/效率/SOC 窗口/年边界（experimental/provisional）'},
  {t:'联络线',keys:['intertie_hourly','interties'],req:'按系统构成',note:'逐时交换计划与通道清单'},
  {t:'各年占比',keys:['weather'+'_year_'+'weight'+'_sets'],req:'必备',note:'决定各年天气在测算中各占多少分量'},
  {t:'运行规则与应急程序',keys:[],req:'—',note:'引擎无此类输入表：失负荷判定与调度口径由方法学固定，不可在界面录入（诚实标注）'},
];
async function rmStep1(body){
  body.innerHTML=`<div class="card"><h3>资源构成申报 <span class="mono-note">决定下方输入模板的可选槽位（储能/水电）；项目完成后只读</span></h3>
    <div class="fgrid" id="rm-comp"><div class="loading">读取当前申报…</div></div></div>
    <div class="card"><h3>情景设定</h3><div id="rm-scen" class="loading">读取运行选项…</div></div>
    <div class="card"><h3>输入数据清单 <span class="mono-note">按数据契约的模板槽位分组；状态来自服务端真实登记</span></h3>
    <div class="grid2" id="rm-cards"><div class="loading">读取模板槽位…</div></div></div>
    <div class="card"><h3>天气数据用了哪几年 <span class="mono-note">结果只反映这些年的天气情况；年份越多，结论越扎实</span></h3>
    <div id="rm-ycard" class="loading">读取天气数据覆盖…</div></div>
    <div class="card"><h3>完备性校验</h3><div id="rm-complete" class="loading">校验中…</div></div>`;
  try{
    const [tpl,opt]=await Promise.all([
      jget('/api/data/templates?project='+encodeURIComponent(RM.project)),
      jget('/api/runs/options?project='+encodeURIComponent(RM.project))]);
    RM.options=opt; RM.slots=(tpl.slots||[]);
    const comp=tpl.composition||{storage:'unconfirmed',hydro:'unconfirmed'};
    const compOpts=(cur,kind)=>[['unconfirmed','尚未确认'],['included','包含'+kind],['excluded','明确不包含'+kind]].map(([v,l])=>`<option value="${v}"${v===cur?' selected':''}>${esc(l)}</option>`).join('');
    document.getElementById('rm-comp').innerHTML=`
      <div class="fld"><label><b>储能资源构成</b></label><select id="rm-comp-storage">${compOpts(comp.storage,'储能')}</select>
        <div class="fh">ELCC 评估链不消费基准储能：声明“包含储能”时 ELCC 提交会被拒绝（引擎诚实守卫），EFC/充裕度可经资源链消费。</div></div>
      <div class="fld"><label><b>水电资源构成</b></label><select id="rm-comp-hydro">${compOpts(comp.hydro,'水电')}</select>
        <div class="fh">声明“包含水电”后出现水电模板槽位（分月可调容量/来水约束）；“明确不包含”则记录合法缺席。</div></div>
      <div class="fld" style="display:flex;align-items:flex-end"><button class="btn pri" onclick="rmSaveComposition()">保存申报并刷新模板清单</button></div>`;
    const slotByKey={}; RM.slots.forEach(s=>{slotByKey[s.table_key]=s;});
    const frozen=(opt.data_refs||[]).filter(d=>d.frozen&&d.approved);
    const banks=opt.scenario_banks||opt.banks||[];
    const weights=opt.weight_set_ids||[];
    const scen=isEFC()
      ? `<div class="fgrid">
          <div class="fld"><label><b>可靠性判据</b>（收敛锚）</label><select id="rm-metric">${[['lole_days_per_year','LOLE 期望缺电天数（天/年）'],['lolh_hours_per_year','LOLH 期望缺电小时数（小时/年）'],['eue_mwh_per_year','EUE 期望缺供电量（MWh/年）'],['neue_ppm','NEUE 缺供电量占比（ppm）']].map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('')}</select>
            <div class="fh">原型的“冬季晚峰失负荷期望（交付年口径）”属方法扩展，<b>未接入</b>；当前判据即所选风险指标。</div></div>
          <div class="fld"><label>模拟方法</label><div class="fh" style="font-size:12.5px;color:var(--ink)">序贯蒙特卡洛（引擎事实，只读）</div></div>
          <div class="fld"><label>目标年/时间轴</label><div class="fh" style="font-size:12.5px;color:var(--ink)">由冻结数据的时间轴决定（只读，不在界面另设）</div></div>
        </div>`
      : `<div class="fgrid">
          <div class="fld"><label><b>可靠性判据</b>（收敛锚）</label><select id="rm-metric">${[['lole_days_per_year','LOLE 期望缺电天数（天/年）'],['lolh_hours_per_year','LOLH 期望缺电小时数（小时/年）'],['eue_mwh_per_year','EUE 期望缺供电量（MWh/年）'],['neue_ppm','NEUE 缺供电量占比（ppm）']].map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('')}</select>
            <div class="fh">原型默认“LOLE = 0.1 天/年（十年一天）”为示例判据；本工具不预设判据数值，由您显式选择指标，收敛与结果状态由引擎判定。</div></div>
          <div class="fld"><label>模拟方法</label><div class="fh" style="font-size:12.5px;color:var(--ink)">序贯蒙特卡洛（引擎事实，只读）</div></div>
          <div class="fld"><label>目标年/时间轴</label><div class="fh" style="font-size:12.5px;color:var(--ink)">由冻结数据的时间轴决定（只读，不在界面另设）</div></div>
        </div>`;
    document.getElementById('rm-scen').innerHTML=scen;
    document.getElementById('rm-cards').innerHTML=RM_CARDS.map(c=>{
      const hit=RM.slots.filter(s=>c.keys.some(k=>s.table_key===k||s.table_key.startsWith(k)));
      const state = c.keys.length===0 ? '<span class="tag plan">无对应输入表</span>'
        : hit.length ? `<span class="tag ok">模板已定义 ${hit.length} 表</span>`
        : '<span class="tag lock">未提供槽位</span>';
      return `<div class="datacard"><h4>${esc(c.t)} ${state} <span class="mono-note">${esc(c.req)}</span></h4>
        <ul>${hit.length?hit.map(s=>`<li>${esc(s.title)}<span class="mono-note" data-tech="1">（${esc(s.table_key)}）</span>${s.required?'公共必需表':'资源可选表'}</li>`).join(''):`<li>${esc(c.note)}</li>`}</ul>
        ${hit.length?`<div class="fnote">${esc(c.note)}</div>`:''}</div>`;
    }).join('');
    const need=[];
    if(!frozen.length) need.push('已审批且已冻结的数据批次 ≥1');
    if(!banks.length) need.push('模拟样本集 ≥1（在「基础数据管理」生成）');
    if(!weights.length) need.push('各年占比方案 ≥1');
    if(!(opt.elcc_candidates||[]).length) need.push('候选资源组 ≥1（ELCC/EFC 必需）');
    (opt.not_runnable_reasons||[]).forEach(r=>need.push(r));
    const ok=!need.length&&opt.runnable;
    document.getElementById('rm-complete').innerHTML = ok
      ? `<div class="hintbar"><b>完备性校验通过</b>：冻结批次 ${frozen.length} 个｜样本集 ${banks.length} 个｜各年占比方案 ${weights.length} 套｜候选资源 ${(opt.elcc_candidates||[]).length} 个。可进入下一步。</div>
         <div class="btnrow" style="display:flex;justify-content:flex-end"><button class="btn" onclick="PAGE='data';rerender()">去基础数据管理</button>
         <button class="btn pri" onclick="rmComplete(1)">完成填写，进入基准标定 →</button></div>`
      : `<div class="errbar"><b>完备性校验未通过（${need.length} 项）</b>，不得带病进入标定：<ul style="margin:6px 0 0 18px">${need.map(n=>`<li>${esc(n)}</li>`).join('')}</ul></div>
         <div style="display:flex;justify-content:flex-end"><button class="btn" onclick="PAGE='data';rerender()">去基础数据管理补链</button></div>`;
    rmLoadYearCard(frozen.length?frozen[0].import_batch_id:null);
  }catch(e){ body.innerHTML=`<div class="errbar">${esc(e.message)}</div>`; }
}
/* ---- W3 年份卡＋占比子卡（三切片计划 §4.1/§4.2）：数据源＝覆盖投影（只读）；
   占比数值与等权行都来自服务端投影，页面不自算 1/N ---- */
async function rmLoadYearCard(batchId){
  const el=document.getElementById('rm-ycard'); if(!el) return;
  RM.yBatch=batchId||null;
  if(!batchId){ el.innerHTML=`<div class="fh">尚无已审批冻结的数据批次：完成数据链后，这里告诉您用了哪几年的天气。</div>`; return; }
  try{
    const cov=await jget('/api/data/coverage?project='+encodeURIComponent(RM.project)+'&batch='+encodeURIComponent(batchId));
    el.innerHTML=rmYearCardHTML(cov);
  }catch(e){ el.innerHTML=`<div class="errbar">天气数据覆盖不可读（如实披露）：${esc(e.message)}</div>`; }
}
function rmFmtPct(w){ const v=Number(w)*100; return (Math.abs(v-Math.round(v))<1e-9?String(Math.round(v)):String(Math.round(v*10)/10))+'%'; }
function rmYearCardHTML(cov){
  const years=(cov.declared_years||[]), byYear=cov.years||[];
  if(!years.length) return `<div class="fh">该批次没有可用的天气数据年份（如实显示）。</div>`;
  const sets=cov.weight_sets||[];
  const runnable=((RM.options||{}).weight_set_ids)||[];
  const covering=s=>years.every(y=>(s.years||[]).includes(y));
  const cur=sets.find(s=>s.normalized&&covering(s)&&runnable.includes(s.weight_set_id))
        || sets.find(s=>s.normalized&&covering(s))
        || sets[0]||null;
  const n=years.length;
  const sorted=[...years].sort((a,b)=>a-b);
  const contig=sorted.every((y,i)=>i===0||y===sorted[i-1]+1);
  const range=contig?`${sorted[0]}–${sorted[sorted.length-1]}`:sorted.join('、');
  let shareTxt='；各年占比方案未配齐（见下方「各年占比」）';
  if(cur&&cur.weights){
    const ws=years.map(y=>Number((cur.weights||{})[y]));
    if(ws.every(w=>Number.isFinite(w)&&Math.abs(w-1/n)<1e-9))
      shareTxt=`；目前测算时 ${n} 年平均看待、各占 ${rmFmtPct(1/n)}`;
    else shareTxt='；各年按占比加权（见下方「各年占比」）';
  }
  const top = n===1
    ? `目前只有 <b>1 年</b>的天气数据：结果反映的是这一年的天气情况，换一个年份结果可能不同。建议今后补充更多年份的数据。`
    : `天气数据有 <b>${range} 共 ${n} 年</b>${shareTxt}。`;
  const rows=byYear.map(y=>{
    const loadOK=Number(y.load_hours)===Number(y.expected_hours);
    const renOK=Number(y.renewable_hours)===Number(y.expected_hours);
    if(!y.complete){
      const miss=[!loadOK?'用电负荷':null,!renOK?'新能源出力':null].filter(Boolean).join('、');
      return `<div class="yrow" style="margin-top:4px"><b>${y.year}年</b> 数据不完整（缺${miss}）：请补齐后再把这一年纳入测算。</div>`;
    }
    const role=y.role==='stress_test'?'压力考验年（不参与平均）':(y.role==='mixed'?'参与口径不一，以运行为准':'参与平均');
    return `<div class="yrow" style="margin-top:4px"><b>${y.year}年</b>｜用电负荷 完整一年 ✓｜新能源出力 完整一年 ✓｜${esc(role)}</div>`;
  }).join('');
  return `<div class="fh" style="font-size:13px;line-height:1.8">${top}</div>${rows}${rmWeightSubCardHTML(cov,cur,years)}`;
}
function rmWeightSubCardHTML(cov,cur,years){
  const n=years.length;
  const ws=(cur&&cur.weights)||{};
  const setYears=cur?[...(cur.years||[])].sort((a,b)=>a-b):[];
  let summary='尚未配置各年占比方案';
  if(cur){
    const vals=setYears.map(y=>Number(ws[y]));
    const eq=n>0&&setYears.length===n&&vals.every(w=>Number.isFinite(w)&&Math.abs(w-1/n)<1e-9);
    summary=eq?`方案“${esc(cur.weight_set_id)}”：${n} 年各占 ${rmFmtPct(1/n)}（平均看待）`
      :`方案“${esc(cur.weight_set_id)}”：各年占比见下方明细`;
  }
  const perYear=setYears.map(y=>`<div class="yrow" style="margin-top:2px">${y}年 占 ${rmFmtPct(Number(ws[y])||0)}</div>`).join('');
  const hints=[];
  if(cur){
    const sumW=setYears.reduce((a,y)=>a+(Number(ws[y])||0),0);
    const sumPct=sumW*100;
    if(Math.abs(sumW-1)>1e-9)
      hints.push(`各年占比加起来要等于 <b>100%</b>，目前是 <b>${Math.round(sumPct*10)/10}%</b>，还差 ${Math.round((100-sumPct)*10)/10}%`);
    else hints.push('各年占比加起来正好 100%（校验通过）');
    years.filter(y=>!(y in ws)).forEach(y=>
      hints.push(`占比表里没有 <b>${y}年</b>，但天气数据里有这一年——每一年都要给占比，请补上。`));
    setYears.filter(y=>Number(ws[y])<0).forEach(y=>hints.push(`${y}年的占比不能是负数。`));
  }else{
    hints.push('还没有可用的占比方案：可点下方「改成各年平均看待」生成一份，上传后生效。');
  }
  return `<details style="border:1px solid var(--line);border-radius:8px;margin-top:10px;padding:8px 12px">
    <summary style="cursor:pointer"><b>各年占比</b>：${summary}</summary>
    <div class="fh" style="margin-top:8px">如果某年恰好是大风少水、风险特别高的年份，它在结果里该占多大分量？占比就是回答这件事。不确定就选平均看待。</div>
    <div style="margin-top:6px">${perYear}</div>
    <div class="fh" style="margin-top:6px">${hints.map(h=>`<div>· ${h}</div>`).join('')}</div>
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn" onclick="rmDownloadEqualWeights()">改成各年平均看待</button>
      <span class="fh">下载占比表 → 到「基础数据管理」上传 → 审批冻结后生效（占比只能在数据链里改，页面不直接保存）。</span></div>
    <details class="disc"><summary>技术依据</summary><div class="body">「改成各年平均看待」调用覆盖投影的 preview_equal_weights（服务端 default_equal_weights 生成器：source_id=equal_weight_default、data_status=assumed，血缘留痕），下载为 weather_year_weight_sets.csv（模板目录同款表头 record_id,weight_set_id,weather_year,weight,source_id,data_status,note；record_id 由页面自造 EQW+年份）；上传后由 DQ-21 校验把关（覆盖＝声明年、合计＝100%、非负、source_id 必填）。页面不做占比直接保存（v1 裁决：保存必须走导入链哈希/授权留痕）。</div></details>
  </details>`;
}
async function rmDownloadEqualWeights(){
  if(!RM.yBatch){ flash('请先完成数据链（没有已冻结批次）'); return; }
  try{
    const cov=await jget('/api/data/coverage?project='+encodeURIComponent(RM.project)+'&batch='+encodeURIComponent(RM.yBatch)+'&preview_equal_weights=1');
    const rows=cov.equal_weight_preview||[];
    if(!rows.length){ flash('该批次没有天气数据年份，无法生成占比表'); return; }
    const head='record_id,weight_set_id,weather_year,weight,source_id,data_status,note';
    const lines=rows.map(r=>[`EQW${r.weather_year}`,r.weight_set_id,r.weather_year,r.weight,r.source_id,r.data_status,''].join(','));
    const csv='﻿'+[head,...lines].join('\n')+'\n';
    const blob=new Blob([csv],{type:'text/csv;charset=utf-8'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download='weather'+'_year_'+'weight'+'_sets.csv';
    a.click(); URL.revokeObjectURL(a.href);
    flash('已下载「各年平均看待」占比表：请到「基础数据管理」按既有导入链上传，审批冻结后生效');
  }catch(e){ flash('等权预览获取失败：'+e.message); }
}
async function rmSaveComposition(){
  try{
    await jpost('/api/projects/composition',{project_ref:RM.project,resource_composition:{
      storage:document.getElementById('rm-comp-storage').value,
      hydro:document.getElementById('rm-comp-hydro').value}});
    flash('资源构成申报已保存，模板清单已按新申报刷新');
    renderRelMod();
  }catch(e){ flash('申报被拒绝：'+e.message); }
}
/* ---- 步骤 02：基准标定（五段式业务页：口径条→目标→现状→逐档一键链→图→结论；M2，calib-ui 分支①） ---- */
