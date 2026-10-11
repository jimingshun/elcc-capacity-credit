const F = {
  fixed: [
    ['测算对象与造价', [
      ['machine','参考机型','2x1000MW超超临界'],['province','目标省份','安徽'],['zone','目标分区','全省'],['price_index','造价指数换算系数（倍数，1.00=不调整；不是百分比）',1.00],
    ]],
    ['建设与融资', [
      ['cyears','建设期（年，1—10；改后返回本页重渲染比例行）',2],['equity','资本金比例（%）',20.00],['loan','贷款利率（年利率%）',4.300],['loan_years','还贷年限（年）',15],['form','固定资产形成率（%）',95.00],
    ]],
    ['折旧及固定费用', [
      ['dep','综合折旧年限（年）',15],['salvage','残值率（%）',5.00],['op','经营期（年）',30],['om','固定运维费（元/kW·年）',71.00],['ins','保险费率（占固定资产原值%）',0.250],['amr','摊销比例（占总投资%）',5.00],['amy','摊销年限（年）',8],['vat','增值税率（%，仅等效换算）',13.00],
    ]],
  ],
  gross: [
    ['测算对象与价格口径', [
      ['obj','对象名称（参考机组/项目，必填）',''],['region','地区（必填）',''],['ty','投产/交付年度',2030],['pby','价格基准年',2026],['cap','装机容量（kW，必填且>0）',2000000],
    ]],
    ['建设期投资时序（元，年末流出）', [
      ['y','建设期（年，1—10）',2],['i0','第1年投资（元）',3891900000],['i1','第2年投资（元）',3891900000],
    ]],
    ['税务与现金流', [
      ['n','经营期（年，1—60）',30],['d','折现率（%，税后口径）',7.000],['tax','统一所得税率（%，0—99.9）',25.00],['dep','税务折旧年限（直线，1—60）',15],['form','固定资产形成率（%，税务折旧基）',100.00],['om','固定运维费（元/kW·年）',0],['res','残值（元，期末一次性流入；不适用显式0）',0],['wc','营运资金（元；不适用显式0）',0],
    ]],
  ],
  net: [
    ['测算对象与口径', [
      ['scenario','来源情景/编号（必填）',''],['unit','参考机组（必填）',''],['region','地区/节点/分区（必填）',''],['year','评估年度',2030],['price_year','价格基准年',2026],['cap_kind','容量分母口径','装机容量'],['capacity','容量分母（kW，必填且>0）',2000000],['source','E&AS来源说明（必填）',''],
    ]],
    ['电能量收支（元/年；成本项非负；留空＝未填，提交时拒绝）', [
      ['spot','现货电能量收入（可为负）',''],['fuel','燃料成本（非负）',''],['vom','变动运维成本（非负）',''],['emis','排放成本（非负）',''],['start','启动及空载等其他变动成本（非负）',''],['contract','合约差价净结算（有符号；无合约显式填0）',''],
    ]],
    ['辅助服务收支（元/年；留空＝未填，提交时拒绝）', [
      ['reserve','备用服务收入',''],['freq','调频服务收入',''],['other','其他适用辅助服务收入',''],['anccost','辅助服务额外增量成本（非负）',''],
    ]],
    ['Gross来源（外部输入）', [
      ['gross','外部Gross CONE数值（≥0；不填=不测算Net）',''],['gross_source','Gross来源与方法说明（必填）',''],['g_unit','Gross侧参考机组',''],['g_region','Gross侧地区/节点/分区',''],['g_year','Gross侧评估年度',2030],['g_py','Gross侧价格基准年',2026],
    ]],
  ],
};
/* ============ P-C: 字段控件元数据（选择器/输入约束，前后端同界） ============ */
let CATALOG = null;
jget('/api/cost/catalog').then(c=>{ CATALOG = c; if(PAGE==='cost'&&COST_STEP==='form') renderCostPage(); }).catch(()=>{});
/* 输入边界（与引擎/检查服务同一张约束表；越界值不允许进入字段状态） */
const BOUNDS = {
  fixed: {
    price_index:{min:0.01,step:0.01}, cyears:{min:1,max:10,int:1},
    equity:{min:0,max:100,step:0.01}, loan:{min:0,step:0.001},
    loan_years:{min:1,max:60,int:1}, dep:{min:1,max:60,int:1},
    salvage:{min:0,max:100,step:0.01}, op:{min:1,max:60,int:1},
    om:{min:0,step:0.01}, ins:{min:0,max:100,step:0.001},
    amr:{min:0,max:100,step:0.01}, amy:{min:1,max:60,int:1},
    vat:{min:0,max:100,step:0.01}, form:{min:0,max:100,step:0.01},
    cm_cost:{min:0.01,step:0.01}, cm_cap:{min:1,max:10000,int:1},
  },
  gross: {
    ty:{min:1900,max:2100,int:1}, pby:{min:1900,max:2100,int:1},
    y:{min:1,max:10,int:1}, n:{min:1,max:60,int:1},
    d:{min:0,max:1000,step:0.001}, tax:{min:0,max:99.9,step:0.01},
    dep:{min:1,max:60,int:1}, form:{min:0,max:100,step:0.01},
    om:{min:0,step:0.01}, res:{min:0,step:0.01}, wc:{min:0,step:0.01},
    cap:{min:0.01,step:0.01},
  },
  net: {
    year:{min:1900,max:2100,int:1}, price_year:{min:1900,max:2100,int:1},
    capacity:{min:0.01,step:0.01},
    gross:{min:0,step:0.0001},
    fuel:{min:0,step:0.01}, vom:{min:0,step:0.01}, emis:{min:0,step:0.01},
    start:{min:0,step:0.01}, anccost:{min:0,step:0.01},
  },
};
const yearOpts = (from,to,cur)=>Array.from({length:to-from+1},(_,i)=>from+i)
  .map(y=>`<option value="${y}" ${String(y)===String(cur)?'selected':''}>${y}</option>`).join('');
const provincesOptions = (cur)=>CATALOG?Object.keys(CATALOG.provinces).map(p=>`<option value="${esc(p)}" ${p===cur?'selected':''}>${esc(p)}</option>`).join(''):'';
const zoneOptions = (prov,cur)=>{
  if(!CATALOG) return '';
  const prefix = prov+'|';
  return Object.keys(CATALOG.zones).filter(k=>k.startsWith(prefix))
    .map(k=>{const z=k.slice(prefix.length);return `<option value="${esc(z)}" ${z===cur?'selected':''}>${esc(z)}（系数 ${CATALOG.zones[k]}）</option>`;}).join('');
};
/* 地区选择控件（用户2026-09-17三次裁决）：全国省份均可选＋当前省
   分区＋自定义输入；服务端对gross地区仅必填校验，不限制目录 */
const regionZones = (prov)=>{
  if(!CATALOG) return [];
  const prefix = (prov||'')+'|';
  return Object.keys(CATALOG.zones).filter(k=>k.startsWith(prefix)).map(k=>k.slice(prefix.length));
};
const regionChoices = (prov,cur)=>{
  const zones = regionZones(prov);
  const provinces = CATALOG?Object.keys(CATALOG.provinces):[];
  const inList = !!cur && (provinces.includes(cur) || zones.includes(cur));
  return `<select id="f-region-sel">
      <option value="">（请选择地区）</option>
      <optgroup label="全国省份">${provinces.map(p=>`<option value="${esc(p)}"${cur===p?' selected':''}>${esc(p)}</option>`).join('')}</optgroup>
      ${zones.length?`<optgroup label="${esc(prov||'当前省')}的分区">${zones.map(z=>`<option value="${esc(z)}"${cur===z?' selected':''}>${esc(z)}</option>`).join('')}</optgroup>`:''}
      <option value="__custom__"${inList?'':' selected'}>（自定义输入…）</option>
    </select>
    <input data-f="region" id="f-region" value="${esc(cur||'')}" placeholder="自定义地区/节点（必填）" ${inList?'style="display:none"':''}>
    <div class="fh">下拉可选全国任一省份或当前省的分区；选「自定义输入」可填任意地区/节点（必填），Net 引用模式下与Gross侧做同口径比对。</div>`;
};
/* 选择器控件（覆盖同名输入字段） */
const RENDER = {
  machine: ()=>{
    if(!CATALOG) return '<span class="loading">机型目录加载中…</span>';
    const cur = COST_FORM.machine!==undefined?COST_FORM.machine:'2x1000MW超超临界';
    const opts = Object.entries(CATALOG.machines).map(([id,m])=>
      `<option value="${esc(id)}" ${id===cur?'selected':''}>${esc(id)}｜${m.capacity_mw}MW｜基准造价 ${m.base_cost_per_kw} 元/kW</option>`).join('');
    const custom = cur==='__custom__';
    const cm = custom?(COST_FORM||{}):{};
    return `<select data-f="machine" id="f-machine"><option value="">（请选择机型）</option>${opts}<option value="__custom__" ${custom?'selected':''}>自定义机型（填写参数）</option></select>
      <div class="fh" id="machine-preview"></div>
      <div class="fh">${esc(CATALOG.disclaimer)}；选择自定义机型后需填写名称、基准造价与建档容量，测算按所填参数执行。</div>
      <div id="cm-fields" style="display:${custom?'':'none'};border-left:3px solid var(--line);padding-left:10px;margin-top:6px">
        <div class="fld"><label><b>自定义机型名称</b>（必填，作为结果中的机型标识）</label><input data-f="cm_name" value="${esc(COST_FORM.cm_name||'')}"></div>
        <div class="fld"><label><b>基准造价</b>（元/kW，必填且&gt;0）</label><input class="num" data-f="cm_cost" data-min="0.01" step="0.01" value="${esc(COST_FORM.cm_cost||'')}"></div>
        <div class="fld"><label><b>建档容量</b>（MW，必填正整数）</label><input class="num" data-f="cm_cap" data-min="1" data-max="10000" data-int="1" value="${esc(COST_FORM.cm_cap||'')}"></div>
      </div>`;
  },
  province: ()=>{
    if(!CATALOG) return '<span class="loading">省份目录加载中…</span>';
    const cur = COST_FORM.province!==undefined?COST_FORM.province:'安徽';
    return `<select data-f="province" id="f-province"><option value="">（请选择省份）</option>${provincesOptions(cur)}</select>
      <div class="fh">选择省份后，分区选项按目录级联（目录外分区引擎拒绝，不回退通用值）。</div>`;
  },
  zone: ()=>{
    const prov = COST_FORM.province!==undefined?COST_FORM.province:'安徽';
    const cur = COST_FORM.zone!==undefined?COST_FORM.zone:'全省';
    return `<select data-f="zone" id="f-zone">${zoneOptions(prov,cur)}</select>
      <div class="fh">分区目录当前仅“全省”种子行；扩展分区需目录授权（选了也会被拒绝）。</div>`;
  },
};
/* 税率/融资期限/折旧方式预设（辅助选择器，不直接作为提交字段） */
const PRESETS = {
  vat:{label:'增值税率预设',opts:[[13,'13%（一般税率）'],[9,'9%'],[6,'6%']],target:'vat'},
  loan_years:{label:'融资期限预设',opts:[[10,'10年'],[15,'15年'],[20,'20年']],target:'loan_years'},
  tax:{label:'所得税率预设',opts:[[25,'25%'],[15,'15%']],target:'tax'},
};
/* 年份/口径/地区等选择器（覆盖同名输入字段） */
const yearField = (name,defYear)=>()=>{
  const cur = COST_FORM[name]!==undefined?COST_FORM[name]:defYear;
  return `<select data-f="${name}">${yearOpts(2020,2050,cur)}</select>`;
};
Object.assign(RENDER, {
  ty: yearField('ty',2030), pby: yearField('pby',2026),
  year: yearField('year',2030), price_year: yearField('price_year',2026),
  region: ()=>{
    const prov = COST_FORM.province!==undefined?COST_FORM.province:'安徽';
    const cur = COST_FORM.region!==undefined?COST_FORM.region:'';
    return `<div id="f-region-fld">${regionChoices(prov,cur)}</div>`;
  },
  cap_kind: ()=>`<select data-f="cap_kind"><option value="装机容量">装机容量（当前引擎唯一口径）</option></select>
    <div class="fh">核定可靠容量口径依赖需求域产出，规划中（开放项Q3）；当前选其他口径会被拒绝。</div>`,
});
const LABEL_TWEAK = {
  fixed:{dep:'综合折旧年限（年，折旧方式=直线法）', op:'经营期（年，正式合同上限待定义，当前1—60）', loan:'贷款利率（年利率%，正式合同上限待定义）'},
  gross:{dep:'税务折旧年限（直线法，年）'},
};
function presetSelect(name){
  const p = PRESETS[name]; if(!p) return '';
  return `<div class="fld"><label>${esc(p.label)}</label><select id="preset-${name}" data-target="${p.target}">
    <option value="">（自定义输入）</option>${p.opts.map(([v,t])=>`<option value="${v}">${esc(t)}</option>`).join('')}</select>
    <div class="fh">选择预设即填入下方数值框，仍可手工修改；最终以数值框为准。</div></div>`;
}
/* ============ 测算类型选择页（R1：供给入口第一步） ============ */
function sanitizeNumericInput(inp){
  const isInt = inp.dataset.int==='1';
  let v = inp.value;
  if(isInt){ v = v.replace(/[^\d]/g,''); }
  else { v = v.replace(/[^\d.\-]/g,''); const head=v.charAt(0); v = head==='-' ? '-'+v.slice(1).replace(/-/g,'') : v.replace(/-/g,''); }
  if(v!==inp.value) inp.value = v;
}
function clampNumericInput(inp){
  if(inp.dataset.min===undefined && inp.dataset.max===undefined) return;
  if(inp.value===''||inp.value==='-') return;
  const v = parseFloat(inp.value);
  if(isNaN(v)) return;
  const min = inp.dataset.min!==undefined?parseFloat(inp.dataset.min):null;
  const max = inp.dataset.max!==undefined?parseFloat(inp.dataset.max):null;
  let nv = inp.dataset.int==='1' ? Math.round(v) : v;
  let clamped = false;
  if(min!==null && nv<min){ nv=min; clamped=true; }
  if(max!==null && nv>max){ nv=max; clamped=true; }
  if(clamped){
    inp.value = String(nv);
    inp.closest('.fld')?.classList.add('err');
    const fh = inp.closest('.fld')?.querySelector('.fh:last-of-type');
    if(fh){ fh.dataset.note = fh.dataset.note||fh.textContent; fh.textContent = '✕ 输入超出合法范围，已自动限制到 '+nv+'（合法区间 '+(min!==null?min:'−∞')+'—'+(max!==null?max:'∞')+'）。'; }
    setTimeout(()=>{ inp.closest('.fld')?.classList.remove('err');
      const fh2 = inp.closest('.fld')?.querySelector('.fh:last-of-type');
      if(fh2 && fh2.dataset.note){ fh2.textContent = fh2.dataset.note; delete fh2.dataset.note; } }, 2400);
  }
}
function wireFormControls(){
  document.querySelectorAll('#cost-body input[data-f]').forEach(inp=>{
    inp.addEventListener('input', ()=>{ sanitizeNumericInput(inp); clampNumericInput(inp); });
    inp.addEventListener('change', ()=>clampNumericInput(inp));
  });
  const machine = document.getElementById('f-machine');
  if(machine){
    const toggle = ()=>{
      const custom = machine.value==='__custom__';
      const cm = document.getElementById('cm-fields'); if(cm) cm.style.display = custom?'':'none';
      const pv = document.getElementById('machine-preview');
      if(pv){ const m = CATALOG && CATALOG.machines[machine.value];
        pv.textContent = m ? `参数预览：建档容量 ${m.capacity_mw} MW｜基准造价 ${m.base_cost_per_kw} 元/kW｜K_machine ${m.k_machine}` : (custom?'自定义机型：按您填写的造价与容量计算。':''); }
    };
    machine.addEventListener('change', ()=>{ COST_FORM[machine.dataset.f]=machine.value; toggle(); });
    toggle();
  }
  const prov = document.getElementById('f-province');
  if(prov){
    prov.addEventListener('change', ()=>{
      COST_FORM.province = prov.value;
      const zsel = document.getElementById('f-zone');
      if(zsel){ zsel.innerHTML = zoneOptions(prov.value,''); COST_FORM.zone = zsel.value||''; }
      const rfld = document.getElementById('f-region-fld');
      if(rfld){
        rfld.innerHTML = regionChoices(prov.value, COST_FORM.region||'');
        wireRegionSelect();
      }
    });
  }
  wireRegionSelect();
  document.querySelectorAll('#preset-vat,#preset-loan_years,#preset-tax').forEach(sel=>{
    sel.addEventListener('change', ()=>{
      if(sel.value==='') return;
      const target = document.querySelector(`#cost-body [data-f="${sel.dataset.target}"]`);
      if(target){ target.value = sel.value; clampNumericInput(target); flash('已按预设填入 '+sel.value+'（可继续手工修改）'); }
    });
  });
}
function wireRegionSelect(){
  const rsel = document.getElementById('f-region-sel');
  if(!rsel) return;
  rsel.addEventListener('change', ()=>{
    const rinp = document.getElementById('f-region');
    if(!rinp) return;
    if(rsel.value==='__custom__'){ rinp.style.display=''; rinp.focus(); }
    else if(rsel.value){ rinp.value = rsel.value; rinp.style.display='none'; flash('地区已选：'+rsel.value); }
    else { rinp.value=''; rinp.style.display='none'; }
  });
}
