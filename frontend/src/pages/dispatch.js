import {jobs} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {status} from '../components/common/status.js';
import {DispatchKind} from '../constants/enums.js';
import {esc,date,notify} from '../utils/dom.js';

const kind=k=>DispatchKind[k]||k;
const reason=d=>d.detail?.reason||'';
const detailBits=d=>{
 const x=d.detail||{};
 const bits=[];
 if(x.slot_seq)bits.push('派工序号 '+x.slot_seq);
 if(x.cycle)bits.push('第 '+x.cycle+' 轮'+(x.position?' · 第 '+x.position+' 位':''));
 if(x.worker)bits.push('执行者 '+x.worker);
 if(x.capacity)bits.push('名额 '+x.capacity);
 if(x.waiting_after!==undefined)bits.push('后续等待 '+x.waiting_after);
 if(x.old_executor)bits.push('原执行者 '+x.old_executor);
 if(x.recovery_count!==undefined)bits.push('恢复次数 '+x.recovery_count);
 return bits.filter(Boolean).join(' · ');
};

function capacityForm(snap){
 const admin=session.user.role==='admin';
 return '<section class="panel"><div class="section-title"><h2>全站运行名额</h2><span>在途 '+snap.running+' / 名额 '+snap.capacity+'</span></div>'+
  '<p class="lead-line">名额控制同时在跑的分析数量。调低名额不打断在途任务，空位出现后才按轮转派下一项。</p>'+
  '<form id="capacity-form" class="inline-form"><label>运行名额<input name="capacity" type="number" min="1" max="8" value="'+snap.capacity+'" '+(admin?'':'disabled')+' required></label>'+
  '<button type="submit" class="primary" '+(admin?'':'disabled')+'>保存名额</button>'+
  (admin?'<button type="button" class="button" id="recover-jobs">恢复失联任务</button>':'')+'</form>'+
  '<p class="hint">当前空位 '+snap.available+'。观察员只读；仅业务负责人可调整名额或触发恢复。</p></section>';
}

function rotationTable(snap){
 return '<section class="panel"><div class="section-title"><h2>发起人轮转次序</h2><span>当前第 '+snap.cycle+' 轮 · 游标第 '+(snap.cursor+1)+' 位</span></div>'+
  '<div class="table-wrap"><table><thead><tr><th>位次</th><th>发起人</th><th>状态</th><th>等待</th><th>占位任务</th></tr></thead><tbody>'+
  snap.rotation.map(r=>'<tr><td>'+r.position+'</td><td>'+esc(r.name)+'<br><small>'+esc(r.email)+'</small></td><td><span class="dispatch-state">'+esc(r.state)+'</span></td><td>'+r.waiting+'</td><td>'+(r.running.length?r.running.map(run=>'#'+run.job_id+' '+esc(run.name)).join('<br>'):'—')+'</td></tr>').join('')+
  '</tbody></table></div>'+(snap.rotation.length?'':'<div class="empty">尚无发起人。提交分析后自动加入轮转末尾。</div>')+'</section>';
}

function waitingTable(snap){
 return '<section class="panel"><div class="section-title"><h2>等待次序</h2><span>'+snap.waiting.length+' 件待派工</span></div>'+
  (snap.waiting.length?'<div class="table-wrap"><table><thead><tr><th>次序</th><th>任务</th><th>发起人</th><th>状态</th><th>轮次 / 位次</th><th>已尝试</th></tr></thead><tbody>'+
   snap.waiting.map(w=>'<tr><td>'+w.order+'</td><td>#'+w.job_id+' '+esc(w.name)+'</td><td>'+esc(w.submitter)+'<br><small>'+esc(w.submitter_email||'')+'</small></td><td>'+status(w.status)+'</td><td>第 '+w.cycle+' 轮 · 第 '+w.position+' 位'+(w.advanced_round?'（下一轮）':'')+'</td><td>'+w.attempts+'</td></tr>').join('')+
   '</tbody></table></div>':'<div class="empty">没有等待任务。提交的分析会在名额空出后按轮转派出。</div>')+'</section>';
}

function runningTable(snap){
 return '<section class="panel"><div class="section-title"><h2>占位中的分析</h2><span>'+snap.running_jobs.length+' 件在途</span></div>'+
  (snap.running_jobs.length?'<div class="table-wrap"><table><thead><tr><th>任务</th><th>发起人</th><th>执行者</th><th>轮次 / 位次</th><th>派工序号</th><th>租约到期</th></tr></thead><tbody>'+
   snap.running_jobs.map(j=>'<tr><td>#'+j.job_id+' '+esc(j.name)+'</td><td>'+esc(j.submitter)+'</td><td>'+esc(j.executor||'—')+'</td><td>第 '+(j.cycle??'—')+' 轮'+(j.position?' · 第 '+j.position+' 位':'')+'</td><td>'+(j.dispatch_seq??'—')+'</td><td>'+(j.lease_expires_at?date(j.lease_expires_at):'—')+'</td></tr>').join('')+
   '</tbody></table></div>':'<div class="empty">当前没有占位任务。</div>')+
  (snap.recovering.length?'<div class="section-title" style="margin-top:26px"><h2>待恢复任务</h2><span>'+snap.recovering.length+' 件</span></div><div class="table-wrap"><table><thead><tr><th>任务</th><th>发起人</th><th>原轮次</th><th>恢复次数</th><th>尝试</th></tr></thead><tbody>'+
   snap.recovering.map(j=>'<tr><td>#'+j.job_id+' '+esc(j.name)+'</td><td>'+esc(j.submitter)+'</td><td>第 '+(j.round_no??'—')+' 轮</td><td>'+j.recovery_count+'</td><td>'+j.attempts+'</td></tr>').join('')+'</tbody></table></div>':'')+'</section>';
}

function eventTable(snap){
 return '<section class="panel"><div class="section-title"><h2>派工依据</h2><span>可追溯记录</span></div>'+
  '<ul class="dispatch-log">'+snap.events.map(e=>'<li><div class="log-head"><span class="log-kind">'+esc(kind(e.kind))+'</span><strong>#'+e.job_id+' '+esc(e.job_name)+'</strong><span>'+date(e.at)+'</span></div>'+
   (reason(e)?'<p>'+esc(reason(e))+'</p>':'')+(detailBits(e)?'<small>'+esc(detailBits(e))+'</small>':'')+'</li>').join('')+'</ul>'+
  (snap.events.length?'':'<div class="empty">尚无派工记录。</div>')+'</section>';
}

export async function dispatchPage(){
 const snap=await jobs.dispatch();
 return heading('05 / DISPATCH','派工调度台','按发起人轮转派工，每人每轮领取一项；任务按受理顺序处理，无任务者跳过。这里可核对等待次序、占位与每一次派工的依据。','<button class="button" id="refresh-dispatch">刷新调度 ↻</button>')+
  '<section class="dispatch-summary"><div><span>全站名额</span><strong>'+snap.capacity+'</strong></div><div><span>在途占位</span><strong>'+snap.running+'</strong></div><div><span>空闲名额</span><strong>'+snap.available+'</strong></div><div><span>待派工</span><strong>'+snap.waiting.length+'</strong></div><div><span>当前轮次</span><strong>'+snap.cycle+'</strong></div></section>'+
  capacityForm(snap)+rotationTable(snap)+runningTable(snap)+waitingTable(snap)+eventTable(snap);
}

export function bindDispatch(refresh){
 document.querySelector('#refresh-dispatch')?.addEventListener('click',refresh);
 document.querySelector('#capacity-form')?.addEventListener('submit',async e=>{
  e.preventDefault();
  const value=Number(new FormData(e.currentTarget).get('capacity'));
  try{await jobs.setCapacity(value);notify('全站运行名额已保存为 '+value);await refresh();}catch(err){notify(err.message,true);}
 });
 document.querySelector('#recover-jobs')?.addEventListener('click',async()=>{
  try{const r=await jobs.recover();notify(r.recovered.length?'已恢复 '+r.recovered.length+' 件中断任务':'没有需要恢复的任务');await refresh();}catch(err){notify(err.message,true);}
 });
}
