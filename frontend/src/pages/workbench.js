import {datasets,jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
export async function workbench(){
 workspace.datasets=await datasets.list();const m=workspace.meta;
 const snap=await jobs.dispatch().catch(()=>null);
 const mine=snap?snap.rotation.find(r=>r.email===session.user.email):null;
 const waitingHere=snap?snap.waiting.filter(w=>w.submitter_email===session.user.email):[];
 const dispatchNote=snap?'<section class="panel dispatch-note"><div class="section-title"><h2>我的派工位置</h2><span>全站名额 '+snap.capacity+' · 在途 '+snap.running+'</span></div>'+(mine?'<p>你在第 '+(snap.cycle)+' 轮的第 '+mine.position+' 位，状态：<strong>'+esc(mine.state)+'</strong>；你还有 '+mine.waiting+' 件在等待，其中预计下一件排在第 '+(waitingHere[0]?.order??'—')+' 位。</p>':'<p>你尚未进入轮转。提交第一件分析后，会排在当前循环末尾。</p>')+'<p class="hint">派工按发起人轮转：每人每轮领取一项，个人任务按受理顺序处理，无任务者跳过。等待次序与派工依据可在<a href="#/dispatch">派工调度台 ↗</a>核对。</p></section>':'';
 return heading('03 / ANALYSIS LAB',m.workbench_title,'选择业务批次和分析口径。参数、输入指纹与计算结论随任务保存，便于复核和复现。')+dispatchNote+'<div class="workbench-grid"><section class="panel"><form id="job-form"><label>分析任务名称<input name="name" required minlength="2" value="'+esc(m.name)+' · 复核试算"></label><label>数据批次<select name="dataset_id" required>'+workspace.datasets.map(d=>'<option value="'+d.id+'">'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label><div class="parameter-grid">'+m.parameters.map(p=>'<label>'+esc(p.label)+(p.type==='select'?'<select name="param-'+esc(p.key)+'">'+p.options.map(o=>'<option value="'+esc(o.value)+'" '+(o.value==p.default?'selected':'')+'>'+esc(o.label)+'</option>').join('')+'</select>':'<input type="number" step="any" name="param-'+esc(p.key)+'" value="'+esc(p.default)+'" min="'+esc(p.min??'')+'" max="'+esc(p.max??'')+'" required>')+'</label>').join('')+'</div><button type="submit" class="primary" '+(session.user.role==='viewer'||!workspace.datasets.length?'disabled':'')+'>'+esc(m.action_label||'执行分析')+' ↗</button><p class="hint">提交后由后台任务计算。观察员可查看参数和结果，无法提交。</p></form></section><section class="method-sheet"><p class="eyebrow">METHOD / BUSINESS RULE</p><h2>'+esc(m.method)+'</h2><p>'+esc(m.tagline)+'</p><h3>业务判读边界</h3><ul>'+m.limitations.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul><div class="method-trace"><span>输入</span><strong>'+esc(m.dataset_name)+'</strong><span>输出</span><strong>可复核结果 / 参数版本 / 分发归档</strong></div></section></div>';
}
export function bindWorkbench(){submit(document.querySelector('#job-form'),async f=>{const params={};for(const p of workspace.meta.parameters){const value=f.get('param-'+p.key);params[p.key]=p.type==='number'?Number(value):value;}const j=await jobs.create({name:f.get('name'),dataset_id:Number(f.get('dataset_id')),params});workspace.selectedJob=j.id;notify('分析任务已提交，按发起人轮转排队');location.hash='#/results/'+j.id;});}
