function rmStep2(body){
  body.innerHTML=`<div id="rm-calib" class="loading">读取标定状态…</div>`;
  rmCalibLoad();
}
function calDetailUrl(){ return '/api/batches/detail?ref='+encodeURIComponent(RM.calib.batchRef); }
function calNextUrl(){ return '/api/batches/calibration-next?ref='+encodeURIComponent(RM.calib.batchRef); }
function authSeq(pts){ const k=(pts||[]).filter(p=>p.import_batch_id).length; return '授权#'+(k+1); }
function calScaleOf(p){ const v=(p.params||{}).scale_percent; if(v===null||v===undefined||v==='') return null; const n=Number(v); return Number.isFinite(n)?n:null; }
async function rmCalibLoad(){
  const el=document.getElementById('rm-calib'); if(!el) return;
  try{
    const d=await jget('/api/batches');
    const mine=(d.batches||[]).filter(b=>!b.invalid&&b.kind==='calibration'&&b.project_ref===RM.project);
    if(mine.length){ if(!RM.calib||!RM.calib.batchRef) RM.calib={batchRef:mine[0].batch_ref}; return rmCalibPanel(); }
    RM.calib=null; rmCalibCreateForm(el);
  }catch(e){ el.innerHTML=`<div class="errbar">${esc(e.message)}</div>`; }
}
function rmCalibCreateForm(el){
  const frozen=((RM.options||{}).data_refs||[]).filter(d=>d.frozen&&d.approved);
  const base=(frozen[0]||{}).import_batch_id||'';
  el.innerHTML=`
  <div class="hintbar">测算口径：按冻结数据测算<span class="mono-note">（多年天气年份与各年占比口径接入后，此处显示年份与占比；口径不同结论不同，标定前请先在步骤01 确认数据口径）</span></div>
  <div class="card"><h3>① 先定"多可靠才算达标" <span class="tag plan">目标值无默认，由您显式给定</span></h3>
    <div class="fgrid">
      <div class="fld"><label><b>风险指标</b></label><select id="rc-metric">
        ${[['lole_days_per_year','LOLE 期望缺电天数（天/年）'],['lolh_hours_per_year','LOLH 期望缺电小时数（小时/年）'],['eue_mwh_per_year','EUE 期望缺供电量（MWh/年）'],['neue_ppm','NEUE 缺供电量占比（ppm）']].map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('')}</select>
        <div class="fh">指标与目标值应来自您采用的考核/监管口径。</div></div>
      <div class="fld" id="rc-target-fld"><label><b>目标值</b>（必填，本工具没有默认）</label><input class="num" id="rc-target" placeholder="如 0.1（天/年）">
        <div class="fh">0.1 天/年 ≈ 平均十年里允许缺电一天。</div></div>
      <div class="fld"><label>允许波动（容差带，%）</label><input class="num" id="rc-tol" data-min="0.5" data-max="49" step="0.5" value="5">
        <div class="fh">默认 ±5%；口径越严，所需档位越多。</div></div>
    </div>
    <div class="fh" style="margin:4px 0 8px">建立任务后系统<b>先自动测一次现状</b>，再逐档给出建议——每档只需点一下「按建议测这一档」，数据版本与授权留痕（授权#N）自动完成。</div>
    <div class="btnrow" style="display:flex;justify-content:flex-end;margin-top:10px">
      <button class="btn" onclick="RM.step=1;renderRelMod()">← 返回步骤 01</button>
      <button class="btn pri" id="rc-create" onclick="rmCalibCreate()">建立标定任务并先测现状 →</button></div>
    <details class="disc"><summary>技术依据</summary><div class="body">判据指标可选 LOLE/LOLH/EUE/NEUE；目标值必须显式给定、系统无默认；容差带默认 ±5%。批次级授权：每档测算自动留痕——授权#N（本批第 N 次授权动作）＋规则＋参数＋父版本哈希，页面不再询问授权人。标定每档是充裕度内核研究子运行，不改变"一项目一正式结果"。</div></details>
  </div>`;
  const wire=()=>{ const t=document.getElementById('rc-target');
    document.getElementById('rc-create').disabled = !t.value.trim(); };
  const t=document.getElementById('rc-target'); t.addEventListener('input',wire); wire();
  const tol=document.getElementById('rc-tol');
  tol.addEventListener('input',()=>{sanitizeNumericInput(tol);clampNumericInput(tol);});
}
async function rmCalibCreate(){
  const target=document.getElementById('rc-target').value.trim();
  if(!target){ flash('目标值必须由您显式给定（本工具没有默认）'); return; }
  if(!RM.options){ try{ RM.options=await jget('/api/runs/options?project='+encodeURIComponent(RM.project)); }catch(e){ flash('读取数据口径失败：'+e.message); return; } }
  const tol=parseFloat(document.getElementById('rc-tol').value)||5;
  const frozen=((RM.options||{}).data_refs||[]).filter(d=>d.frozen&&d.approved);
  const base=(frozen[0]||{}).import_batch_id||'';
  if(!base){ flash('步骤 01 尚无已冻结批次：标定以冻结数据为基准'); return; }
  const body={ request_id:'web-calib-'+(crypto.randomUUID?crypto.randomUUID():Date.now()),
    kind:'calibration', method:'adequacy', project_ref:RM.project, baseline_batch:base,
    points:[{point_id:'p0', label:'现状（0%）', params:{rule_id:'load_scale', scale_percent:0}}],
    axis:{name:'负荷缩放'}, criterion:{metric:document.getElementById('rc-metric').value, target, tolerance_percent:tol},
    authorized_by:'授权#1',
    label:'基准标定（六步流程步骤02）' };
  try{
    const m=await jpost('/api/batches', body);
    RM.calib={batchRef:m.batch_ref};
    flash('标定任务已建立：先自动测一次现状，然后逐档给建议');
    rmCalibPanel();
  }catch(e){ flash('创建被拒绝：'+e.message); }
}
function calChainRail(step){
  const names=['准备数据版本','发起测算','跟踪进度','读取判定'];
  return `<div class="steps" id="rm-chain-rail">${names.map((nm,i)=>{
    const cls=i<step?'done':(i===step?'cur':'');
    const ic=i<step?'✓':(i===step?'◐':'·');
    return `<div class="st ${cls}">${ic} ${nm}</div>`;}).join('')}</div>`;
}
async function rmCalibPanel(){
  const el=document.getElementById('rm-calib'); if(!el) return;
  const C=RM.calib; const ref=C&&C.batchRef;
  if(!ref){ return rmCalibLoad(); }
  try{
    const [det,nx]=await Promise.all([jget(calDetailUrl()), jget(calNextUrl())]);
    const dec=nx.decision||{}, cri=nx.criterion||{};
    const pts=det.points||[];
    const baseP=pts.find(p=>calScaleOf(p)===0);
    const done=pts.filter(p=>p.status==='完成'&&calScaleOf(p)!==null&&calScaleOf(p)!==0);
    const last=done.length?[...done].sort((a,b)=>(calScaleOf(a)??0)-(calScaleOf(b)??0)).slice(-1)[0]:null;
    const target=Number(cri.target??0.1), tol=Number(cri.tolerance_percent??5);
    const bandLo=target*(1-tol/100), bandHi=target*(1+tol/100);
    const converged=dec.kind==='converged';
    const failed=dec.kind==='not_converged'||dec.kind==='interval_exhausted';
    const pendPts=pts.filter(p=>p.status==='待派生'||p.status==='待确认'||p.status==='待运行');
    const badLegacy=pendPts.some(p=>calScaleOf(p)===null);
    const awaiting=pendPts.filter(p=>p.status==='待确认');
    let html=`<div class="hintbar"><b>标定任务 ${esc(String(det.batch_ref).slice(0,8))}</b>｜口径：按冻结数据测算<span class="mono-note">（多年天气口径接入后显示年份与占比）</span>｜目标 ${esc(String(cri.target))} ±${esc(String(cri.tolerance_percent))}%｜状态 <span class="tag ${converged?'ok':failed?'warn':'plan'}">${converged?'已完成':failed?'未达标':'测算中'}</span><span class="mono-note">（CAL-02：目标值为您显式给定，无默认）</span></div>`;
    /* ② 系统现状卡 */
    if(baseP){
      const bDone=baseP.status==='完成';
      const bv=baseP.metric_value;
      html+=`<div class="card"><h3>② 系统现状 <span class="tag ${bDone?'ok':'plan'}">${bDone?'现状已测':'现状测算中…'}</span></h3>`;
      if(bDone&&bv!=null){
        const loose=bv>bandHi, tight=bv<bandLo;
        html+=`<div class="kpis"><div class="kpi"><div class="k">当前缺电风险（现状）</div><div class="v">${esc(fmt(bv,3))}<small>天/年</small></div></div></div>
        <div style="font-size:12.5px;color:var(--sub);line-height:1.7">您的标准是 ${esc(fmt(target,3))} 天/年：当前系统<b>${loose?'比标准松':tight?'比标准紧':'已在标准带内'}</b>${loose?`，约为标准的 <b class="num">${fmt(bv/target,1)}</b> 倍——适合用<b>负荷缩放</b>法标定（把全年预测负荷整体上浮，找到恰好达标的位置）`:tight?'——负荷缩放解释力弱，完美容量法（追加理想机组）在方法签核后开放':'。'}</div>`;
      }else{
        html+=`<div class="loading">正在用当前系统测一次现状风险（自动进行，约一两分钟）…</div>`;
      }
      html+=`<details class="disc"><summary>技术依据</summary><div class="body">现状首测＝0% 档一次真实充裕度运行，计入 8 轮上限（CAL-04/05）；轴推荐＝R9（偏松→负荷缩放，偏紧→完美容量，后者待方法签核，当前禁用）。</div></details></div>`;
    }
    /* ③ 逐档测算卡 */
    html+=`<div class="card"><h3>④ 逐档测算 <span class="tag plan">已完成 ${done.length}／最多 7 档</span></h3><div id="rm-calib-cost"></div>`;
    if(C.busy){
      html+=calChainRail(C.chainStep||0)+`<div class="fh" style="margin:4px 0 0">正在自动完成这一档，无需操作…</div>`;
    }else if(converged||failed){
      /* 结论在下方卡片 */
    }else if(dec.kind==='probe'&&(!baseP||baseP.status==='完成')&&!badLegacy&&!awaiting.length){
      html+=`<div style="margin:6px 0 4px">
        <button class="btn pri big" onclick="rmCalibChainOne()">按建议测这一档：负荷${Number(dec.scale_percent)>=0?'上浮':'下调'} <span class="num">${esc(fmt(Math.abs(Number(dec.scale_percent)),3))}%</span></button>
        <button class="btn" onclick="rmCalibRefresh()">刷新状态</button>
        <span style="font-size:11.5px;color:var(--faint);margin-left:8px">点一下就行，中间步骤自动完成；数据版本与授权留痕（授权#N）自动生成</span></div>
        <div class="fh" style="margin:2px 0 0">已用 ${dec.rounds_used??0} 轮｜可用 ${dec.usable_rounds??0} 轮（上限 8 轮，含现状首测）</div>`;
      if(nx.gap_note) html+=`<div class="fh" style="margin-top:6px">缺口画像：${esc(nx.gap_note)}</div>`;
    }
    if(last&&!converged&&!failed){
      const lv=last.metric_value;
      const vtext=lv==null?'—':(lv>bandHi?'偏松（风险比标准高，还要加负荷）':lv<bandLo?'偏紧（风险已低于标准，加过头了）':'达标（落进容差带）');
      const cls=lv==null?'':(lv>bandHi?'':lv<bandLo?'tight':'good');
      html+=`<div class="roundline ${cls}"><b>第 ${done.length} 档（负荷${Number((last.params||{}).scale_percent)>=0?'上浮':'下调'} ${esc(fmt(Math.abs(Number((last.params||{}).scale_percent)),3))}%）：</b>缺电风险 ${lv==null?'—':esc(fmt(lv,3))} 天/年 → <b>${vtext}</b>。${dec.kind==='probe'?`下一档建议 <b class="num">${esc(fmt(Math.abs(Number(dec.scale_percent)),3))}%</b>。`:''}</div>`;
    }
    if(C.lastErr) html+=`<div class="roundline" style="border-color:var(--warn)"><b>这一档没有走完：</b>${esc(C.lastErr)}　<button class="btn" onclick="rmCalibRetry()">重试</button></div>`;
    if(awaiting.length) html+=`<div class="hintbar warn" style="margin-top:10px"><b>有 ${awaiting.length} 档停在"准备中"：它的派生数据版本等待确认（DR-01 要求）。</b>这些是旧批次或中断残留的点位——点「继续这一档」补齐样本集并接着测算（沿用原派生记录与授权留痕，不重复派生）。 <button class="btn pri" onclick="rmCalibResumePoint()">继续这一档</button></div>`;
    if(badLegacy) html+=`<div class="hintbar warn" style="margin-top:10px"><b>该任务包含缺少数值档位的旧版点位（旧版流程创建），无法自动测算。</b>建议重开任务：按本页流程重新建立标定任务，旧任务数据保留可查。</div>
      <div style="margin:8px 0"><button class="btn pri" onclick="rmCalibRestart()">重开标定任务</button></div>`;
    html+=`<details class="disc"><summary>试算记录（技术留痕）</summary><div class="body">
      <table class="grid"><tr><th>档位</th><th class="r">缺电风险（天/年）</th><th>判定</th><th>状态</th><th>说明</th></tr>
      ${pts.map(p=>{
        const s=calScaleOf(p);
        const v=p.metric_value;
        const vt=v==null?'—':(v>bandHi?'偏松':v<bandLo?'偏紧':'达标');
        return `<tr><td class="num">${s==null?esc(p.label||'—'):(s===0?'0%（现状）':(s>=0?'+':'−')+fmt(Math.abs(s),2)+'%')}</td>
          <td class="r">${v==null?'—':esc(fmt(v,3))}</td><td>${v==null?'—':vt}</td>
          <td><span class="tag ${p.status==='完成'?'ok':p.status==='失败'?'lock':'plan'}">${p.status==='待派生'?'待准备':p.status==='待确认'?'准备中':p.status==='待运行'?'测算中':p.status==='完成'?'已完成':'未成功'}</span></td>
          <td class="mono-note">${p.run_ref?'run '+esc(String(p.run_ref).slice(0,8)):''}${p.diagnosis?'｜'+esc(p.diagnosis):''}</td></tr>`;}).join('')}</table></div></details>
    <details class="disc"><summary>技术依据</summary><div class="body">「按建议测这一档」在前端串 追加→派生→发起→轮询→收尾（不新增后端编排）；最多 8 轮、各轮同种子同精度、引擎收敛判定不放宽；授权留痕＝授权#N（本批第 N 次授权动作，前端按试算记录计数）。</div></details>
    </div>`;
    /* ④ 夹逼图（收敛/失败后由结论卡取代主视图） */
    if(!converged&&!failed){
      html+=`<div class="card"><h3>⑤ 夹逼过程 <span class="tag plan">每测完一档多一个点</span></h3>
        <canvas id="rm-bisect" style="width:100%;height:260px;display:block"></canvas>
        <div style="display:flex;gap:18px;font-size:11.5px;color:var(--sub);margin-top:8px;flex-wrap:wrap">
        <span>● 已测档位</span><span>◌ 系统建议的下一档</span><span style="color:var(--t3)">▨ 达标带（${esc(fmt(target,3))}±${esc(String(tol))}%）</span><span>横轴：负荷上浮 %；纵轴：缺电风险（天/年）</span></div></div>`;
    }
    /* ⑤ 结论卡 */
    if(converged){
      const ref=(nx.calibrated_baseline_ref||'');
      const doneP=pts.find(p=>p.status==='完成'&&calScaleOf(p)!==null&&Math.abs(calScaleOf(p)-Number(dec.scale_percent))<1e-9);
      RM.calib.converged={scale:dec.scale_percent, ref, runRef:(doneP||{}).run_ref||null};
      html+=`<div class="card"><h3>⑥ 结论 <span class="tag ok">已完成</span></h3>
      <div class="hintbar good"><b>达标了：把全年预测负荷整体上浮 <span class="num">${esc(fmt(Math.abs(Number(dec.scale_percent)),3))}%</span>，加权缺电风险恰好落在 ${esc(fmt(target,3))}±${esc(String(cri.tolerance_percent))}% 内（该档实测 ${esc(String(dec.metric_value))}）。</b><br>
      <span style="font-size:12px">这是<b>解释性中间成果</b>（CAL-09）：只帮您读系统现状，<b>不会改回基础数据</b>；后续待测资源测算仍以冻结数据为基准。已用 ${dec.rounds_used??0} 轮。</span></div>
      <div style="display:flex;gap:10px">
        <button class="btn pri" onclick="rmCalibComplete()">确认结论，进入下一步：接入待测资源 →</button>
        ${(RM.calib.converged.runRef)?`<button class="btn" onclick="openResult('${esc(RM.project)}','reliability','${esc(String(RM.calib.converged.runRef))}')">查看这次测算的明细（含缺口画像）</button>`:''}</div>
      <details class="disc"><summary>技术依据</summary><div class="body">收敛判定＝判据值落入目标±容差带；逐轮留痕见试算记录（DR-01a/b，授权#N）；明细页含缺口画像与分年视图（多年口径接入后）。</div></details></div>`;
    }else if(dec.kind==='not_converged'||dec.kind==='interval_exhausted'){
      const itv=dec.interval_percent?[Number(dec.interval_percent[0]).toFixed(2), Number(dec.interval_percent[1]).toFixed(2)].join('%～')+'%':'—';
      if(dec.kind==='not_converged'){
        html+=`<div class="card"><h3>⑥ 结论 <span class="tag warn">未达标</span></h3>
        <div class="hintbar warn"><b>测满 8 档，风险曲线始终没落进 ${esc(fmt(target,3))}±${esc(String(cri.tolerance_percent))}% 的达标带。</b><br>
        <b>差多少：</b>达标位置被夹在负荷上浮 <span class="num">${esc(itv)}</span> 之间，但每档结果都差一点进不了带。<br>
        <b>怎么办（三选一）：</b></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn pri" onclick="rmCalibWidenTolerance()">放宽容差重测</button>
          <button class="btn" onclick="rmAckCalib()">接受区间结论（+${esc(fmt(Number(dec.interval_percent?dec.interval_percent[0]:0),2))}%～+${esc(fmt(Number(dec.interval_percent?dec.interval_percent[1]:0),2))}%），不做标定基准</button>
          <button class="btn" disabled title="完美容量法待方法签核后开放">换标定方法（准备中）</button></div>
        <div class="fld" style="margin-top:12px"><label><input type="checkbox" id="rm-ack" ${RM.calibAck?'checked':''}> 我已知悉：本次未达标，结论只给区间、不给单点值；后续测算以冻结数据继续，不虚构基准</label></div>
        <details class="disc"><summary>技术依据</summary><div class="body">CAL-08：未收敛只给最终区间与逐轮轨迹，不给点值；CAL-06/07：各轮同种子同精度、引擎收敛判定不放宽。</div></details></div>`;
      }else{
        html+=`<div class="card"><h3>⑥ 结论 <span class="tag warn">未达标</span></h3>
        <div class="hintbar warn"><b>达标区间已缩到最小档位仍跨着标准线，再测没有意义，已自动停止。</b><br>
        <b>差多少：</b>区间 <span class="num">${esc(itv)}</span>，已达档位分辨率下限。<br>
        <b>怎么办：</b>建议重开任务，把初始口径放宽一档；已测数据保留供参考。</div>
        <div style="display:flex;gap:10px"><button class="btn pri" onclick="rmCalibRestart()">重开任务（放宽口径）</button><button class="btn" onclick="rmAckCalib()">接受区间结论并继续</button></div>
        <details class="disc"><summary>技术依据</summary><div class="body">接口契约 §3 情形2（interval_exhausted）：区间收缩至无意义步长；处置＝说明原因＋建议重开任务。</div></details></div>`;
      }
    }
    el.innerHTML=html;
    rmFillCostHint('rm-calib-cost', det.baseline_batch_id);
    if(!converged&&!failed) requestAnimationFrame(()=>rmCalibChartDraw(nx));
    const pend=pts.find(p=>p.status==='待派生'||p.status==='待确认'||p.status==='待运行');
    if(pend&&!C.busy&&!C.lastErr&&calScaleOf(pend)===0){
      setTimeout(()=>rmCalibChainOne(),400);
    }
  }catch(e){ el.innerHTML=`<div class="errbar">${esc(e.message)}</div>`; }
}
function rmCalibChartDraw(nx){
  const cv=document.getElementById('rm-bisect'); if(!cv) return;
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth, H=260;
  if(W<50){ requestAnimationFrame(()=>rmCalibChartDraw(nx)); return; }
  cv.width=W*dpr; cv.height=H*dpr;
  const g=cv.getContext('2d'); g.scale(dpr,dpr); g.clearRect(0,0,W,H);
  const cri=(nx||{}).criterion||{};
  const target=Number(cri.target??0.1), tol=Number(cri.tolerance_percent??5);
  const dec=(nx||{}).decision||{};
  const his=(nx||{}).history||[];
  const pts=his.filter(h=>h.state==='usable'&&h.metric_value!=null);
  const xs=-5, xe=10;
  const vals=pts.map(p=>p.metric_value).concat([target]);
  const vmax=Math.max(1,...vals.map(v=>v*4));
  const padL=52,padR=16,padT=14,padB=30;
  const X=s=>padL+(s-xs)/(xe-xs)*(W-padL-padR);
  const ylo=Math.log10(0.01),yhi=Math.log10(vmax);
  const Y=v=>padT+(1-(Math.log10(Math.max(v,0.01))-ylo)/(yhi-ylo))*(H-padT-padB);
  g.strokeStyle='#EFEDE8';g.fillStyle='#9B968C';g.font='10.5px JetBrains Mono,monospace';
  for(const t of [0.01,0.02,0.05,0.1,0.2,0.5,1,2,5,10]){ if(t>vmax) continue; g.beginPath();g.moveTo(padL,Y(t));g.lineTo(W-padR,Y(t));g.stroke(); g.fillText(String(t),8,Y(t)+3); }
  for(let s=xs;s<=xe;s+=5){ g.fillText((s>0?'+':'')+s+'%',X(s)-8,H-10); }
  g.fillStyle='rgba(83,129,53,.16)';
  g.fillRect(padL,Y(target*(1+tol/100)),W-padL-padR,Y(target*(1-tol/100))-Y(target*(1+tol/100)));
  g.strokeStyle='rgba(83,129,53,.5)';g.setLineDash([4,3]);
  g.beginPath();g.moveTo(padL,Y(target));g.lineTo(W-padR,Y(target));g.stroke();g.setLineDash([]);
  if(dec.kind==='probe'){ g.fillStyle='rgba(32,82,187,.07)';
    g.fillRect(X(Number(dec.interval_percent?dec.interval_percent[0]:xs)),padT,X(Number(dec.interval_percent?dec.interval_percent[1]:xe))-X(Number(dec.interval_percent?dec.interval_percent[0]:xs)),H-padT-padB); }
  if(pts.length>1){ g.strokeStyle='#2052BB';g.lineWidth=1.2;g.beginPath();
    pts.forEach((p,i)=>{ i?g.lineTo(X(Number(p.scale_percent)),Y(p.metric_value)):g.moveTo(X(Number(p.scale_percent)),Y(p.metric_value)); }); g.stroke(); g.lineWidth=1; }
  pts.forEach(p=>{ g.beginPath();g.arc(X(Number(p.scale_percent)),Y(p.metric_value),4.5,0,7);
    g.fillStyle=Number(p.scale_percent)===0?'#9B968C':(p.verdict==='within_band'?'#4E7A3A':'#2052BB');g.fill(); });
  if(dec.kind==='probe'){ const s=Number(dec.scale_percent);
    g.setLineDash([3,4]);g.strokeStyle='#B9772B';g.beginPath();g.moveTo(X(s),padT);g.lineTo(X(s),H-padB);g.stroke();g.setLineDash([]);
    g.beginPath();g.arc(X(s),Y(target),6,0,7);g.strokeStyle='#B9772B';g.stroke();
    g.fillStyle='#B9772B';g.font='10.5px sans-serif';g.fillText('下一档建议 '+(s>=0?'+':'−')+Math.abs(s).toFixed(2)+'%',X(s)+9,Y(target)+4); }
}
async function rmCalibChainOne(){
  const C=RM.calib; if(!C||C.busy) return;
  const upd=()=>{ const r=document.getElementById('rm-chain-rail'); if(r) r.outerHTML=calChainRail(C.chainStep||0);
    const note=document.getElementById('rm-chain-note'); if(note) note.textContent='测算进行中…已等待 '+Math.round((Date.now()-(C.t0||Date.now()))/1000)+' 秒'; };
  let pts=[];
  try{
    let det=await jget(calDetailUrl()); pts=(det.points||[]);
    let target=pts.find(p=>(p.status==='待派生'||p.status==='待确认'||p.status==='待运行')&&calScaleOf(p)!==null);
    if(!target){
      const nx=await jget(calNextUrl());
      if((nx.decision||{}).kind!=='probe'){ rmCalibPanel(); return; }
      await jpost('/api/batches/append-probe',{batch_ref:C.batchRef, authorized_by:authSeq(pts)});
      det=await jget(calDetailUrl()); pts=(det.points||[]);
      target=pts.find(p=>(p.status==='待派生'||p.status==='待确认'||p.status==='待运行')&&calScaleOf(p)!==null);
      if(!target){ rmCalibPanel(); return; }
    }
    C.busy=true; C.chainStep=0; C.t0=Date.now(); rmCalibPanel();
    if(target.status==='待派生'||target.status==='待确认'){
      await jpost('/api/batches/derive-point',{batch_ref:C.batchRef, point_id:target.point_id,
        authorized_by:authSeq(pts), declared_grade:'G0_teaching'});
    }
    C.chainStep=1; upd();
    const r=await jpost('/api/batches/point-run',{batch_ref:C.batchRef, point_id:target.point_id});
    const runRef=r.request_ref||r.run_ref||target.run_ref;
    C.chainStep=2; upd();
    let stat=null;
    for(let i=0;i<400;i++){
      const s=await jget('/api/runs/status?ref='+encodeURIComponent(runRef)+'&project='+encodeURIComponent(RM.project));
      stat=s.status||s.state;
      C.chainTick=i; upd();
      if(stat==='execution_finished'||stat==='execution_failed') break;
      await new Promise(res=>setTimeout(res,1500));
    }
    if(stat!=='execution_finished'&&stat!=='execution_failed'){
      C.busy=false; C.chainStep=0;
      flash('运行超过 10 分钟未结束（如实呈现，不虚报完成）：请稍后点「刷新状态」继续');
      rmCalibPanel(); return;
    }
    C.chainStep=3; upd();
    const f=await jpost('/api/batches/point-finalize',{batch_ref:C.batchRef, point_id:target.point_id, run_ref:runRef});
    if(f.finalized===false){ C.busy=false; C.chainStep=0; flash('运行尚未结束（'+f.status+'）：稍后刷新状态再收尾'); rmCalibPanel(); return; }
    C.busy=false; C.chainStep=0;
    const fp=((f.batch||{}).points||[]).find(p=>p.point_id===target.point_id)||{};
    flash(fp.status==='失败'
      ? ('这一档未成功（如实保留为缺口）：'+String(fp.diagnosis||'').slice(0,120))
      : (f.usable===false?'这一档的测算未收敛/失败：记为不可用，不占夹逼进度（这一档可重测）':'这一档完成：缺电风险 '+String(f.metric_value??'')+' 天/年'));
    rmCalibPanel();
  }catch(e){ C.busy=false; C.chainStep=0; C.lastErr=e.message; flash('这一档没有走完：'+e.message); rmCalibPanel(); }
}
function rmCalibRetry(){ if(RM.calib) RM.calib.lastErr=null; rmCalibChainOne(); }
/* 待确认续推：链条自身已把待确认点位交给
   derive-point（现按既有派生记录续推而非拒绝），按钮＝把链条再点一次 */
function rmCalibResumePoint(){ rmCalibChainOne(); }
function rmCalibRefresh(){ rmCalibPanel(); }
function rmCalibWidenTolerance(){
  RM.calib=null; RM.calibAck=false;
  flash('已重开任务：请在目标卡把允许波动放宽（如 ±5% → ±10%）后重新建立任务');
  rmCalibLoad();
}
function rmCalibRestart(){
  RM.calib=null; RM.calibAck=false;
  flash('已重开任务：请放宽初始口径后重新建立标定任务');
  rmCalibLoad();
}
function rmCalibComplete(){
  RM.done[2]=true; RM.step=3; renderRelMod();
}
function rmAckCalib(){
  if(!RM.calibAck){ const cb=document.getElementById('rm-ack'); RM.calibAck=!!(cb&&cb.checked); }
  if(!RM.calibAck){ flash('请先勾选知悉声明（未收敛的标定不得当作已收敛使用）'); return; }
  RM.done[2]='skip'; RM.step=3; renderRelMod();
}
/* ---- 步骤 03：接入待测资源 + 情景设定（真实选项） ---- */
