import {jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {chart} from '../components/common/chart.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {DispatchKind} from '../constants/enums.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
const traceLabel=k=>DispatchKind[k]||k;
function traceBlock(j){
 const rows=j.trace||[];
 return '<section class="panel"><div class="section-title"><h2>任务生命轨迹</h2><span>从排队到占位、完成或待恢复</span></div>'+
  (rows.length?'<ul class="dispatch-log compact-log">'+rows.map(e=>'<li><div class="log-head"><span class="log-kind">'+esc(traceLabel(e.kind))+'</span>'+
   (e.detail?.reason?'<p>'+esc(e.detail.reason)+'</p>':'')+'<span>'+date(e.at)+'</span></div>'+
   traceMeta(e)+'</li>').join('')+'</ul>':'<div class="empty">尚无调度记录（历史结果在启用轮转派工前生成）。</div>')+'</section>';
}
function traceMeta(e){
 const d=e.detail||{},bits=[];
 if(d.slot_seq)bits.push('派工序号 '+d.slot_seq);
 if(d.cycle)bits.push('第 '+d.cycle+' 轮'+(d.position?' · 第 '+d.position+' 位':''));
 if(d.worker)bits.push('执行者 '+d.worker);
 if(d.old_executor)bits.push('原执行者 '+d.old_executor);
 if(d.attempt)bits.push('第 '+d.attempt+' 次尝试');
 return bits.length?'<small>'+esc(bits.join(' · '))+'</small>':'';
}
export async function results(id){
 workspace.jobs=await jobs.list();const j=id?await jobs.get(id):(workspace.jobs.find(j=>j.id===workspace.selectedJob)||workspace.jobs[0]);
 if(j&&!workspace.jobs.some(x=>x.id===j.id))workspace.jobs.push(j);
 if(!j)return heading('04 / REVIEW','结果复核','尚无分析结果。')+'<div class="empty">先到分析工作台，选择批次并提交一次计算。<a href="#/workbench">前往工作台 ↗</a></div>';
 workspace.selectedJob=j.id;const r=j.result||{};
 const queueNote=j.status==='queued'?'等待名额空出后按发起人轮转派工。':j.status==='recovering'?'执行中断，已保留原轮次位置，等待重新派工恢复。':j.status==='running'?'已占位，后台正在计算。':'';
 return heading('04 / REVIEW','结果复核与业务解释',j.name,'<button class="button" id="refresh-results">刷新结果 ↻</button>')+'<div class="result-selector"><label>分析记录<select id="select-job">'+workspace.jobs.map(x=>'<option value="'+x.id+'" '+(x.id===j.id?'selected':'')+'>'+esc(x.name)+' · '+esc(x.status)+'</option>').join('')+'</select></label>'+status(j.status)+'<span>'+date(j.created_at)+'</span><span class="approval">'+(j.approved?'已获业务批准':'待负责人复核')+'</span></div>'+(j.status==='completed'?'<section class="result-lead"><p class="eyebrow">COMPUTED FINDING</p><h2>'+esc(r.summary)+'</h2><div class="metrics">'+(r.metrics||[]).map(m=>'<div><span>'+esc(m.label)+'</span><strong>'+esc(fmt(m.value))+'<small>'+esc(m.unit||'')+'</small></strong></div>').join('')+'</div></section><section class="panel">'+chart(r.chart)+'</section><div class="two-columns"><section class="panel"><div class="section-title"><h2>业务解释</h2><span>由计算结果生成</span></div><ol class="questions">'+(r.findings||[]).map(v=>'<li>'+esc(v)+'</li>').join('')+'</ol>'+(r.warnings||[]).map(v=>'<p class="warning">'+esc(v)+'</p>').join('')+'</section><section class="method-sheet compact"><p class="eyebrow">REPRODUCIBILITY</p><h3>计算口径与参数</h3><p>'+esc(r.method)+'</p><pre>'+esc(JSON.stringify(j.params,null,2))+'</pre>'+(session.user.role==='admin'&&!j.approved?'<button class="primary" id="approve-job">批准此业务结论</button>':'')+'<p class="hint">仅批准结果可进入定时分发档案。</p></section></div><section class="panel"><div class="section-title"><h2>结果明细</h2><span>'+(r.table?.rows?.length||0)+' 行</span></div>'+table(r.table?.columns||[],r.table?.rows||[])+'</section>':'<section class="panel empty">'+(j.status==='failed'?'计算失败：'+esc(j.error)+'。请返回工作台调整参数或数据。':esc(queueNote)||'后台正在执行计算。点击刷新结果查看最新进展。')+(j.dispatched_at?'<p class="hint">第 '+(j.round_no??'—')+' 轮派工 · 尝试 '+(j.attempts||0)+' 次'+(j.recovery_count?' · 恢复 '+(j.recovery_count||0)+' 次':'')+'</p>':'')+'<a href="#/workbench">返回工作台 ↗</a> <a href="#/dispatch">查看派工调度 ↗</a></section>')+traceBlock(j);
}
export function bindResults(refresh){document.querySelector('#refresh-results')?.addEventListener('click',refresh);document.querySelector('#select-job')?.addEventListener('change',e=>{workspace.selectedJob=Number(e.target.value);location.hash='#/results/'+e.target.value;});document.querySelector('#approve-job')?.addEventListener('click',async()=>{try{await jobs.approve(workspace.selectedJob);notify('已批准，可创建分发计划');await refresh();}catch(e){notify(e.message,true);}});}
