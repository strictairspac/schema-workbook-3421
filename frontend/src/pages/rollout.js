import {table} from '../components/common/table.js';import {esc} from '../utils/html.js';import {session} from '../hooks/session.js';import {ROLLOUT_STATES} from '../constants/enums.js';
export const rolloutBadge=v=>`<span class="badge ${esc(v)}">${esc(ROLLOUT_STATES[v]||v)}</span>`;
const TABS=[['pending','待盘点'],['awaiting','待补挂'],['enabled','已启用'],['orphans','孤儿所在行']];

function stats(items){
  const count=s=>items.filter(x=>s.includes(x.status)).length;
  return `<div class="rollout-stats">
    <div><b>${count(['pending','scanning'])}</b><span>待盘点</span></div>
    <div><b>${count(['awaiting_relink'])}</b><span>待补挂</span></div>
    <div><b>${count(['enabled'])}</b><span>已启用</span></div>
    <div><b>${items.reduce((n,x)=>n+(x.orphan_count||0),0)}</b><span>孤儿行</span></div>
  </div>`;
}
function rowActions(r,ro){
  const meta=`<strong>${esc(r.parent_table)}</strong><small>父表 · 键 ${esc((r.parent_key_attributes||[]).join(', '))}</small>`;
  const detail=`<strong>${esc(r.detail_table)}</strong><small>明细 · 孤儿 ${r.orphan_count??0} · 待补挂记录 ${r.pending_orphans??0}</small>`;
  const actions=[];
  if(['pending','scanning','failed'].includes(r.status)&&!ro)actions.push(`<button class="small" data-scan="${r.id}">${r.status==='scanning'?'续跑盘点':'开始盘点'}</button>`);
  if(r.status==='awaiting_relink'&&!ro)actions.push(`<button class="small" data-scan="${r.id}">重新盘点</button>`);
  if(r.status!=='enabled'&&!ro)actions.push(`<button class="small" data-enable="${r.id}">启用外键</button>`);
  actions.push(`<a class="small-link" href="#evidence/${r.report_id}">报告</a>`);
  return [meta,detail,`${rolloutBadge(r.status)}<small>${esc(r.constraint_name||'外键未建立')}</small>`,actions.join(' ')];
}
function createForm(report,ro){
  if(ro)return '';
  const r=report?.result;
  if(!r)return '';
  const opts=r.decomposition.map((attrs,i)=>`<option value="${i}">R${i+1} · ${esc(attrs.join(', '))}</option>`).join('');
  const firstKey=(r.normal_form_checks?.[0]?.candidate_keys?.[0]||[]).join(',');
  return `<section class="form-panel rollout-panel"><span class="eyebrow">FOREIGN KEY ROLLOUT</span><h2>按报告选父表与明细表</h2>
    <form id="rollout-form"><input type="hidden" name="report_id" value="${report.id}">
    <label>父表（被引用）<select name="parent_index">${opts}</select></label>
    <label>明细表（引用方）<select name="detail_index">${opts}</select></label>
    <div class="fields"><label>父表名<input name="parent_table" placeholder="可留空自动命名"></label><label>明细表名<input name="detail_table" placeholder="可留空自动命名"></label></div>
    <label>历史父行 JSON（数组）<textarea name="parent_rows" rows="3" placeholder='[{"${esc(firstKey.split(',').join('":"...","'))}":"..."}]'></textarea></label>
    <label>历史明细行 JSON（数组，旧孤儿原样保留）<textarea name="detail_rows" rows="3" placeholder='[{"委托号":"W-1","检测项":"强度","结果":"合格"}]'></textarea></label>
    <button class="primary">载入历史行并开始盘点 ↗</button></form>
    <p class="help">键取自报告的候选键；历史行载入后不删改，孤儿只登记等待补挂。盘点可续跑，重试不会重复装约束。</p></section>`;
}
function mainTable(items,ro){
  if(!items.length)return '<div class="empty">该报告还没有外键盘点任务。先选择父表与明细表并载入历史行。</div>';
  return table(['父表','明细表','状态','操作'],items.map(r=>rowActions(r,ro)));
}
function orphansTable(rows,ro){
  const items=(rows?.items||[]).filter(x=>x.orphan);
  if(!items.length)return '<div class="empty">没有待补挂的孤儿所在行。孤儿清零后即可启用外键。</div>';
  const cols=Object.keys(items[0].values||{});
  return table(['原行','属性值','所在明细行','补挂'],items.map(x=>[
    `<code>#${esc(x.row_ref)}</code>`,
    cols.map(c=>`<span class="attribute">${esc(c)}=${esc(x.values[c]??'∅')}</span>`).join(' '),
    x.orphan?`<strong>孤儿</strong><small>${esc(Object.entries(x.orphan.key_values||{}).map(([k,v])=>k+'='+(v??'∅')).join(' / '))}</small>`:'<span>已匹配</span>',
    x.orphan&&!ro?`<button class="small" data-relink="${x.orphan.id}">补挂</button>`:'',
  ]));
}
export function rolloutPanel(report,rollouts,tab,rows){
  const ro=session.user?.role==='viewer';
  tab=TABS.some(t=>t[0]===tab)?tab:'pending';
  const active=rollouts.filter(r=>{
    if(tab==='pending')return ['pending','scanning'].includes(r.status);
    if(tab==='awaiting')return ['awaiting_relink','ready','failed'].includes(r.status);
    if(tab==='enabled')return r.status==='enabled';
    return true;
  });
  const body=tab==='orphans'
    ? (rollouts.length?`<div class="rollout-tabs">${rollouts.map(r=>`<a class="${rows?.rollout_id===r.id?'active':''}" href="#evidence/${report.id}/${tab}/${r.id}">${esc(r.detail_table)} <small>孤儿 ${r.orphan_count??0}</small></a>`).join('')}</div>${orphansTable(rows,ro)}`:'<div class="empty">还没有盘点任务。</div>')
    : mainTable(active,ro);
  return `<div class="section-top"><h2>外键盘点 · 父表 / 明细表</h2><span>HISTORY → INVENTORY → RELINK → ENABLE</span></div>
    ${stats(rollouts)}
    <div class="rollout-tabs">${TABS.map(([key,label])=>`<a class="${tab===key?'active':''}" href="#evidence/${report.id}/${key}">${label}</a>`).join('')}</div>
    ${body}
    <div class="rollout-grid"><section>${createForm(report,ro)}</section><section class="protocol"><h3>盘点规则</h3><ol><li>从拆解报告选父表与明细表，按报告候选键试装外键</li><li>载入两表历史行；旧孤儿保留原行，仅登记待补挂</li><li>盘点期间照常录入合法明细；新增孤儿与误删被引用父行由库内守卫拦截</li><li>孤儿清零且 information_schema 查到外键后才算启用；切换失败回滚并保留已录入行</li></ol></section></div>`;
}
