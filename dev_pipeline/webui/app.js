'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const ACTIVE = ['queued','running','stopping'];
const states = {PLANNING:'計画中', PHASE_RUNNING:'実装', PHASE_VERIFYING:'検証', PHASE_REVIEWING:'レビュー', AWAITING_HUMAN_CHECK:'承認待ち', PHASE_FIXING:'修正', COMPLETED:'完了', FAILED:'エラー'};
const phases = {pending:'未着手',running:'進行中',awaiting_human_check:'承認待ち',accepted:'承認済み'};
let config, items = [], detail, filter = 'all', tab = 'overview', evidencePhase, refreshBusy = false, refreshAgain = false, submitting = false, lastRender = '', decisionSnapshot;
let selected = decodeURIComponent(location.hash.slice(1));
let toastTimer, lastList = '';
const pendingRequests = new Map();
const requestId = () => crypto.randomUUID();
const pathFor = id => '/api/initiatives/' + encodeURIComponent(id);

async function api(path, data) {
  let key;
  if (data?.request_id) {
    key = path + JSON.stringify({...data,request_id:undefined});
    if (pendingRequests.has(key)) data.request_id=pendingRequests.get(key);
    else pendingRequests.set(key,data.request_id);
  }
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrf_token},body:JSON.stringify(data)};
  const response = await fetch(path, {...options, cache:'no-store'});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作に失敗しました');
  if (key) pendingRequests.delete(key);
  return result;
}
function toast(message, error = false) {
  const node = $('#toast'); node.textContent = message; node.className = 'toast' + (error ? ' error' : ''); node.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.hidden = true, error ? 9000 : 4000);
}
function jobActive(item) { return ACTIVE.includes(item.job?.status); }
function status(item) {
  if (item.job?.status === 'stopping') return ['停止中','running'];
  if (item.job?.status === 'queued') return ['開始待ち','running'];
  if (item.job?.status === 'running') return [item.state.state === 'PLANNING' ? '計画中' : ['AWAITING_HUMAN_CHECK','COMPLETED'].includes(item.state.state) ? '状態を更新中' : (states[item.state.state] || '実行') + '中','running'];
  if (['failed','interrupted'].includes(item.job?.status)) return [item.job.status === 'failed' ? '実行エラー' : '中断','error'];
  if (item.state.state === 'COMPLETED') return ['完了','success'];
  if (item.state.state === 'AWAITING_HUMAN_CHECK') return ['承認待ち','waiting'];
  return [item.job?.status === 'stopped' ? '停止済み' : '実行待ち',''];
}
const badge = (label, kind='') => `<span class="badge ${esc(kind)}">${esc(label)}</span>`;
const timestamp = value => value ? new Date(value).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
function renderList() {
  const signature=JSON.stringify([items,filter,selected]);
  if(signature===lastList)return;
  lastList=signature;
  const waiting = items.filter(item => item.state.state === 'AWAITING_HUMAN_CHECK' && !jobActive(item)).length;
  const active = items.filter(jobActive).length;
  $('#summary').innerHTML = `<div class="metric waiting"><span>あなたの確認待ち</span><strong>${waiting}</strong></div><div class="metric"><span>実行中</span><strong>${active}</strong></div>`;
  const shown = items.filter(item => filter === 'all' || filter === 'waiting' && item.state.state === 'AWAITING_HUMAN_CHECK' && !jobActive(item) || filter === 'active' && jobActive(item) || filter === 'done' && item.state.state === 'COMPLETED');
  shown.sort((a,b) => Number(b.state.state === 'AWAITING_HUMAN_CHECK') - Number(a.state.state === 'AWAITING_HUMAN_CHECK') || (b.job?.created_at || '').localeCompare(a.job?.created_at || ''));
  $('#initiative-list').innerHTML = shown.length ? shown.map(item => {
    const id = item.initiative.id, accepted = item.phases.filter(phase => phase.status === 'accepted').length;
    return `<a class="initiative-card ${id === selected ? 'selected':''}" href="#${encodeURIComponent(id)}"><div class="card-top"><span class="id">${esc(id)}</span>${badge(...status(item))}</div><h3>${esc(item.initiative.goal)}</h3><div class="card-bottom"><span>${accepted} / ${item.phases.length} フェーズ完了</span><span class="phase-dots">${item.phases.map(p => `<i class="${esc(p.status)}"></i>`).join('')}</span></div></a>`;
  }).join('') : `<div class="empty-list"><strong>${items.length ? '該当する案件はありません' : '最初の依頼を始めましょう'}</strong>${items.length ? '別の絞り込みを選んでください。' : '「新規依頼」から目的や要件を入力すると、<br>ローカルPCで開発が始まります。'}</div>`;
}
function phaseOptions() {
  return detail.phases.map(p => `<option value="${esc(p.id)}" ${p.id === evidencePhase ? 'selected':''}>${esc(p.id)} · ${esc(p.goal)}</option>`).join('');
}
function evidenceView() {
  const ev = detail.evidence.find(e => e.phase_id === evidencePhase);
  const head = `<div class="evidence-heading"><h3>${tab === 'tests' ? '機械検証' : '敵対的レビュー'}</h3><select id="evidence-phase" aria-label="結果を見るフェーズ">${phaseOptions()}</select></div>`;
  if (!ev) return head + '<p class="muted">フェーズの計画ができると表示されます。</p>';
  if (tab === 'tests') {
    const result = ev.verification;
    if (!result) return head + '<p class="muted">このフェーズの検証結果はまだありません。</p>';
    return head + `<div class="result-card"><div class="result-heading">${badge(result.overall, result.overall === 'PASS' ? 'success':'error')}<strong>テスト・検証結果</strong><span class="muted">Run ${esc(result.run_id)} · ${esc(timestamp(result.created_at))}</span></div>${(result.checks || []).map(c => `<div class="check-row"><strong>${esc(c.name)}</strong>${badge(c.status,c.status === 'PASS'?'success':'error')}<code>${esc(c.command)}</code></div>`).join('')}</div><p class="hint">保存された最新の検証結果です。修正中は前回の結果が表示されます。</p><details open><summary>検証ログ</summary><pre class="log">${esc(ev.verification_log || 'ログはありません。')}</pre></details>`;
  }
  const result = ev.review;
  if (!result) return head + '<p class="muted">このフェーズのレビュー結果はまだありません。</p>';
  return head + `<div class="result-card"><div class="result-heading">${badge(result.result,result.result === 'PASS'?'success':'error')}<strong>レビュー結果</strong><span class="muted">${result.run_id != null ? 'Run ' + esc(result.run_id) + ' · ' : ''}${esc(timestamp(result.created_at))}</span></div>${result.findings?.length ? result.findings.map(f => `<div class="finding">${badge(f.severity || '指摘', ['high','critical'].includes(f.severity)?'error':'waiting')}<p>${esc(f.description)}</p>${f.recommendation ? `<p class="recommendation">${esc(f.recommendation)}</p>`:''}</div>`).join('') : '<div class="finding"><p>レビューで指摘事項はありませんでした。</p></div>'}</div><p class="hint">保存された最新のレビュー結果です。修正中は前回の結果が表示されます。</p>`;
}
function overviewView() {
  const current = detail.phases.find(p => p.id === detail.state.current_phase);
  return `${current ? `<section class="section"><h3>このフェーズの受け入れ条件</h3><ul>${current.acceptance_criteria.map(c=>`<li>${esc(c)}</li>`).join('')}</ul></section>`:''}
  <section class="section"><h3>フェーズ構成</h3><div class="phase-list">${detail.phases.map((p,i)=>`<div class="phase-item"><span class="phase-index">${String(i+1).padStart(2,'0')}</span><p>${esc(p.goal)}</p>${badge(phases[p.status],p.status==='accepted'?'success':p.status==='awaiting_human_check'?'waiting':'')}</div>`).join('') || '<p class="muted">要求からフェーズを計画しています。</p>'}</div></section>
  ${detail.checkpoint ? `<details><summary>直近Runのチェックポイント</summary><pre class="log">${esc(detail.checkpoint)}</pre></details>`:''}
  <details><summary>依頼の要件・制約</summary><div class="section"><h3>要件</h3><p class="prose">${esc((detail.initiative.requirements || []).join('\n') || detail.config.requirements || '指定なし')}</p></div><div class="section"><h3>制約</h3><p class="prose">${esc((detail.initiative.constraints || []).join('\n') || detail.config.constraints || '指定なし')}</p></div></details>
  <details><summary>実行設定</summary><form id="settings-form" class="settings-form"><label>エージェント<select name="agent" ${jobActive(detail)?'disabled':''}>${config.agents.map(a=>`<option ${a===detail.config.agent?'selected':''}>${esc(a)}</option>`).join('')}</select></label><label>リポジトリのパス<input name="repository" value="${esc(detail.config.repository)}" required ${jobActive(detail)?'disabled':''}></label><p class="hint">CLIで作った案件が外部リポジトリを使っていた場合は、最初に同じパスを指定してください。</p><p class="form-error" role="alert"></p><button class="button" ${jobActive(detail)?'disabled':''}>設定を保存</button></form></details>`;
}
function actionBar() {
  const disabled = submitting ? 'disabled' : '';
  if (jobActive(detail)) return `<div class="action-bar"><p class="action-note">画面を閉じても実行は続きます</p><div><button class="button danger" id="stop-open" ${detail.job.status==='stopping'?'disabled':''}>${detail.job.status==='stopping'?'停止しています…':'実行を停止'}</button></div></div>`;
  if (detail.state.state === 'COMPLETED') return '<div class="action-bar"><p class="action-note">すべてのフェーズが承認されました</p><div>'+badge('完了','success')+'</div></div>';
  if (detail.state.state === 'AWAITING_HUMAN_CHECK') return `<div class="action-bar"><p class="action-note">テスト・レビュー結果を確認して判断</p><div><button class="button" id="feedback-open" ${disabled}>修正を依頼</button><button class="button primary" id="accept" ${disabled}>承認して${detail.phases.at(-1)?.id===detail.state.current_phase?'完了':'次へ'}</button></div></div>`;
  return `<div class="action-bar"><p class="action-note">保存済みの状態から進めます</p><div><button class="button primary" id="run" ${disabled}>${detail.job?.status==='failed'?'再実行':'実行を開始・再開'}</button></div></div>`;
}
function renderDetail(force = false) {
  document.body.classList.toggle('has-selection', !!selected);
  if (!detail || !selected) {
    $('#detail-pane').innerHTML = '<div class="empty-detail"><span class="empty-mark">↗</span><h2>案件を選んで確認</h2><p>進行状況、テスト結果、レビュー結果を<br>ここで確認できます。</p></div>'; lastRender=''; return;
  }
  const signature = JSON.stringify([detail,tab,evidencePhase,submitting]);
  if (!force && signature === lastRender) return;
  // A polling update must not discard an in-progress settings edit.
  if (!force && $('#settings-form')?.contains(document.activeElement)) return;
  lastRender = signature;
  const current = detail.phases.find(p => p.id === detail.state.current_phase);
  const count = detail.phases.filter(p => p.status === 'accepted').length;
  $('#detail-pane').innerHTML = `<button class="back" id="back">← 案件一覧</button><header class="detail-header"><p class="id">${esc(detail.initiative.id)}</p><div class="title-line"><h1>${esc(detail.initiative.goal)}</h1>${badge(...status(detail))}</div><div class="detail-meta"><span>${esc(detail.config.agent)}</span><span>${count} / ${detail.phases.length} フェーズ完了</span>${detail.usage.total_tokens != null?`<span>${Number(detail.usage.total_tokens).toLocaleString()} tokens</span>`:''}</div></header>
  ${detail.job?.error ? `<p class="notice error">${esc(detail.job.error)}</p>`:''}
  ${current ? `<section class="phase-card"><div class="row"><span class="eyebrow">CURRENT PHASE · ${esc(current.id)}</span><span class="muted">Run ${esc(detail.state.current_run)}</span></div><h2>${esc(current.goal)}</h2><div class="phase-track">${detail.phases.map(p=>`<span class="${esc(p.status)}"></span>`).join('')}</div><p class="muted">${jobActive(detail)?'ローカルPCで処理を進めています。':detail.state.state==='AWAITING_HUMAN_CHECK'?'テストとレビューが完了しました。あなたの判断を待っています。':'実行を再開できます。'}</p></section>`:''}
  <nav class="tabs" aria-label="案件の表示内容">${[['overview','概要'],['tests','テスト'],['review','レビュー'],['logs','実行ログ']].map(([id,label])=>`<button data-tab="${id}" class="${tab===id?'selected':''}">${label}</button>`).join('')}</nav>
  <div id="tab-content">${tab==='overview'?overviewView():tab==='logs'?`<h3>直近の実行ログ</h3><p class="hint">エラーとCLIの実行結果を表示します。エージェント内部のログはPC側に保存されます。</p><pre class="log">${esc(detail.job_log || '出力はまだありません。')}</pre>`:evidenceView()}</div>${actionBar()}`;
  $('#back').onclick = () => location.hash = '';
  document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => {tab=button.dataset.tab;renderDetail(true);});
  if ($('#evidence-phase')) $('#evidence-phase').onchange = event => {evidencePhase=event.target.value;renderDetail(true);};
  if ($('#run')) $('#run').onclick = () => action('run');
  if ($('#accept')) $('#accept').onclick = () => action('human-check',{result:'ACCEPT',revision:detail.revision});
  if ($('#feedback-open')) $('#feedback-open').onclick = () => {decisionSnapshot={id:selected,revision:detail.revision};$('#feedback-phase').textContent=detail.state.current_phase+' · '+current.goal;openDialog('feedback-dialog');};
  if ($('#stop-open')) $('#stop-open').onclick = () => {decisionSnapshot={id:selected};openDialog('stop-dialog');};
  if ($('#settings-form')) $('#settings-form').onsubmit = event => submitForm(event, async form => {await api(pathFor(selected)+'/configure',Object.fromEntries(new FormData(form)));toast('設定を保存しました');await refresh();});
}
async function refresh() {
  if (!config) return;
  if (refreshBusy) {refreshAgain=true;return;}
  refreshBusy = true;
  const id = selected;
  try {
    const [list, loaded] = await Promise.all([api('/api/initiatives'), id ? api(pathFor(id)) : Promise.resolve(null)]);
    items = list.initiatives;
    if (id === selected) {
      detail = loaded;
      if (detail && !detail.phases.some(p=>p.id===evidencePhase)) evidencePhase=detail.state.current_phase || detail.phases.at(-1)?.id;
      renderDetail();
    }
    renderList();
    $('#connection').textContent='接続済み';$('#connection').className='connection online';
  } catch(error) {
    $('#connection').textContent='接続エラー';$('#connection').className='connection offline';
    if (!detail) $('#detail-pane').innerHTML=`<button class="back" id="back-error">← 案件一覧</button><p class="notice error">${esc(error.message)}<br>PCの起動状態とTailscale接続を確認してください。</p>`;
    if ($('#back-error')) $('#back-error').onclick=()=>location.hash='';
  } finally {refreshBusy=false;if(refreshAgain){refreshAgain=false;refresh();}}
}
async function action(name, data={}, id=selected) {
  if (submitting) return;
  submitting=true;renderDetail(true);
  try {await api(pathFor(id)+'/'+name,{request_id:requestId(),...data});toast(name==='human-check'?'判断を受け付けました':'実行を受け付けました');await refresh();}
  catch(error){toast(error.message,true);await refresh();}
  finally{submitting=false;renderDetail(true);}
}
function openDialog(id) {const dialog=$('#'+id);const error=dialog.querySelector('.form-error');if(error)error.textContent='';dialog.showModal();}
async function submitForm(event, callback) {
  event.preventDefault(); const form=event.currentTarget; const button=form.querySelector('[type="submit"]') || form.querySelector('button:not([type])');
  if (button.disabled) return;button.disabled=true;form.querySelector('.form-error').textContent='';
  try {await callback(form);} catch(error){form.querySelector('.form-error').textContent=error.message;} finally {button.disabled=false;}
}
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$('#new-open').onclick=()=>openDialog('new-dialog');
$('#new-form').onsubmit=event=>submitForm(event,async form=>{const job=await api('/api/initiatives',{...Object.fromEntries(new FormData(form)),request_id:requestId()});$('#new-dialog').close();form.reset();location.hash=encodeURIComponent(job.initiative_id);toast('実行を開始しました');await refresh();});
$('#feedback-form').onsubmit=event=>submitForm(event,async form=>{await api(pathFor(decisionSnapshot.id)+'/human-check',{request_id:requestId(),result:'FEEDBACK',feedback:form.elements.feedback.value,revision:decisionSnapshot.revision});$('#feedback-dialog').close();form.reset();toast('修正を依頼しました');await refresh();});
$('#stop-form').onsubmit=event=>submitForm(event,async()=>{await api(pathFor(decisionSnapshot.id)+'/stop',{});$('#stop-dialog').close();toast('停止を受け付けました');await refresh();});
$('#filters').onclick=event=>{if(!event.target.dataset.filter)return;filter=event.target.dataset.filter;$('#filters .selected').classList.remove('selected');event.target.classList.add('selected');renderList();};
window.addEventListener('hashchange',()=>{selected=decodeURIComponent(location.hash.slice(1));detail=null;lastRender='';tab='overview';evidencePhase=null;document.body.classList.toggle('has-selection',!!selected);refresh();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},4000);

async function notificationStatus() {
  config=await api('/api/config');
  const supported='serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const registration=supported?await navigator.serviceWorker.getRegistration():null;
  const subscription=registration?registration.pushManager.getSubscription():Promise.resolve(null);
  const current=await subscription;
  $('#notification-toggle').textContent=current?'通知を無効にする':'通知を有効にする';
  $('#notification-toggle').disabled=!supported || !registration || !config.notifications.available;
  $('#notification-test').disabled=!current;
  $('#notification-status').textContent=!supported || !registration?'このブラウザーでは通知を利用できません。iPhoneではホーム画面から開いてください。':config.notifications.reason || config.notifications.last_error || (current?'この端末の通知は有効です。':'この端末の通知はまだ有効になっていません。');
}
$('#notifications-open').onclick=async()=>{openDialog('notification-dialog');try{await notificationStatus();}catch(e){$('#notification-status').textContent=e.message;}};
$('#notification-toggle').onclick=async()=>{
  try {
    // Permission must be requested in direct response to the user's tap on iOS.
    const permission=await Notification.requestPermission();
    if(permission!=='granted')throw new Error('通知が許可されていません。端末の通知設定を確認してください。');
    const registration=await navigator.serviceWorker.ready;
    const existing=await registration.pushManager.getSubscription();
    if(existing){await api('/api/notifications/unsubscribe',{endpoint:existing.endpoint});await existing.unsubscribe();}
    else {
      const base64=config.notifications.public_key.replace(/-/g,'+').replace(/_/g,'/');
      const key=Uint8Array.from(atob(base64+'='.repeat((4-base64.length%4)%4)),c=>c.charCodeAt(0));
      const subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});
      try{await api('/api/notifications/subscribe',{subscription:subscription.toJSON()});}catch(e){await subscription.unsubscribe();throw e;}
    }
    await notificationStatus();
  }catch(error){$('#notification-status').textContent=error.message;}
};
$('#notification-test').onclick=async()=>{try{await api('/api/notifications/test',{});toast('テスト通知を配信キューに追加しました');}catch(e){toast(e.message,true);}};
async function boot(){
  try{
    config=await api('/api/config');
    document.querySelectorAll('.agent-options').forEach(select=>select.innerHTML=config.agents.map(a=>`<option ${a===config.default_agent?'selected':''}>${esc(a)}</option>`).join(''));
    if('serviceWorker' in navigator){try{await navigator.serviceWorker.register('/sw.js');}catch(_){toast('通知の準備に失敗しました。通常の操作は利用できます。',true);}}
    await refresh();
  }catch(error){$('#connection').textContent='接続エラー';$('#connection').className='connection offline';toast(error.message,true);}
}
boot();
