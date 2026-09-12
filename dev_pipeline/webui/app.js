'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const ACTIVE = ['queued','running','stopping'];
const states = {PLANNING:'計画中', PHASE_RUNNING:'実装', PHASE_VERIFYING:'検証', PHASE_REVIEWING:'レビュー', AWAITING_HUMAN_CHECK:'承認待ち', PHASE_FIXING:'修正', COMPLETED:'完了', RESOLVED:'完了', FAILED:'エラー'};
const phases = {pending:'未着手',running:'進行中',awaiting_human_check:'承認待ち',accepted:'承認済み'};
let config, projects = [], currentProjectId = '', items = [], detail, filter = 'all', tab = 'overview', evidencePhase, refreshBusy = false, refreshAgain = false, submitting = false, lastRender = '', decisionSnapshot;
let selected = decodeURIComponent(location.hash.slice(1));
let toastTimer, lastList = '', lastProjectSelect = '';
const pendingRequests = new Map();
const requestId = () => crypto.randomUUID();

function parseHash() {
  const hash = decodeURIComponent(location.hash.slice(1)).replace(/^\/+/, '');
  if (!hash) return { projectId: currentProjectId, issueId: '' };
  if (hash.includes('/')) {
    const [p, i] = hash.split('/', 2);
    return { projectId: p, issueId: i };
  }
  // Single identifier: could be project or issue
  if (projects.some(proj => proj.id === hash)) {
    return { projectId: hash, issueId: '' };
  }
  return { projectId: currentProjectId, issueId: hash };
}

function updateHash(projectId, issueId) {
  if (!projectId) {
    location.hash = '';
    return;
  }
  location.hash = issueId ? `${encodeURIComponent(projectId)}/${encodeURIComponent(issueId)}` : encodeURIComponent(projectId);
}

const issuePathFor = (projId, issId) => projId ? `/api/projects/${encodeURIComponent(projId)}/issues/${encodeURIComponent(issId)}` : `/api/initiatives/${encodeURIComponent(issId)}`;

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
  if (item.job?.status === 'running') return [item.state.state === 'PLANNING' ? '計画中' : !item.actions.run ? '状態を更新中' : (states[item.state.state] || '実行') + '中','running'];
  if (['failed','interrupted'].includes(item.job?.status)) return [item.job.status === 'failed' ? '実行エラー' : '中断','error'];
  if (item.actions.completed) return ['完了','success'];
  if (item.actions.human_check) return ['承認待ち','waiting'];
  return [item.job?.status === 'stopped' ? '停止済み' : '実行待ち',''];
}
const badge = (label, kind='') => `<span class="badge ${esc(kind)}">${esc(label)}</span>`;
const timestamp = value => value ? new Date(value).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';

function renderProjectSelect() {
  const sig = JSON.stringify([projects, currentProjectId]);
  if (sig === lastProjectSelect) return;
  lastProjectSelect = sig;
  const sel = $('#project-select');
  if (!sel) return;
  if (!projects.length) {
    sel.innerHTML = '<option value="">プロジェクトなし</option>';
    return;
  }
  sel.innerHTML = projects.map(p => `<option value="${esc(p.id)}" ${p.id === currentProjectId ? 'selected':''}>${esc(p.id)} (${esc(p.issue_count)} Issues)</option>`).join('');
}

function renderList() {
  const signature=JSON.stringify([items,filter,selected,currentProjectId]);
  if(signature===lastList)return;
  lastList=signature;
  const waiting = items.filter(item => item.actions.human_check && !jobActive(item)).length;
  const active = items.filter(jobActive).length;
  $('#summary').innerHTML = `<div class="metric waiting"><span>あなたの確認待ち</span><strong>${waiting}</strong></div><div class="metric"><span>実行中</span><strong>${active}</strong></div>`;
  const shown = items.filter(item => filter === 'all' || filter === 'waiting' && item.actions.human_check && !jobActive(item) || filter === 'active' && jobActive(item) || filter === 'done' && item.actions.completed);
  shown.sort((a,b) => Number(b.actions.human_check) - Number(a.actions.human_check) || (b.job?.created_at || '').localeCompare(a.job?.created_at || ''));
  const listEl = $('#issue-list') || $('#initiative-list');
  if (listEl) {
    listEl.innerHTML = shown.length ? shown.map(item => {
      const id = item.issue?.id, accepted = item.phases.filter(phase => phase.status === 'accepted').length;
      const goal = item.issue?.description;
      const href = currentProjectId ? `#${encodeURIComponent(currentProjectId)}/${encodeURIComponent(id)}` : `#${encodeURIComponent(id)}`;
      return `<a class="issue-card ${id === selected ? 'selected':''}" href="${href}"><div class="card-top"><span class="id">${esc(id)}</span>${badge(...status(item))}</div><h3>${esc(goal)}</h3><div class="card-bottom"><span>${accepted} / ${item.phases.length} フェーズ完了</span><span class="phase-dots">${item.phases.map(p => `<i class="${esc(p.status)}"></i>`).join('')}</span></div></a>`;
    }).join('') : `<div class="empty-list"><strong>${items.length ? '該当するIssueはありません' : '最初のIssueを始めましょう'}</strong>${items.length ? '別の絞り込みを選んでください。' : '「新規Issue」から目的や要件を入力すると、<br>ローカルPCで開発が始まります。'}</div>`;
  }
}
function phaseOptions() {
  return detail.phases.map(p => `<option value="${esc(p.id)}" ${p.id === evidencePhase ? 'selected':''}>${esc(p.id)} · ${esc(p.goal)}</option>`).join('');
}
function technicalFindings(result, open = false) {
  const findings = result.findings || [];
  return `<details id="technical-review-details" ${open?'open':''}><summary>技術的な詳細</summary><div class="result-card"><div class="result-heading">${badge(result.result,result.result === 'PASS'?'success':'error')}<strong>元のレビュー結果</strong><span class="muted">${result.run_id != null ? 'Run ' + esc(result.run_id) + ' · ' : ''}${esc(timestamp(result.created_at))}</span></div>${findings.length ? findings.map((f,index) => `<div class="finding" id="review-finding-${index + 1}" tabindex="-1"><div class="finding-label"><strong>指摘 #${index + 1}</strong>${badge(f.severity || '指摘', ['high','critical'].includes(f.severity)?'error':'waiting')}</div><p>${esc(f.description)}</p>${f.recommendation ? `<p class="recommendation">${esc(f.recommendation)}</p>`:''}</div>`).join('') : '<div class="finding"><p>レビューで指摘事項はありませんでした。</p></div>'}</div></details>`;
}
function businessReviewSummary(summary) {
  if (summary?.status === 'PENDING') return '<p class="notice">業務向け要約を生成しています。元のレビュー結果は技術的な詳細で確認できます。</p>';
  if (!summary || summary.status !== 'SUCCESS') return '<p class="notice error">要約を生成できませんでした。技術的な詳細で元のレビュー結果を確認してください。</p>';
  const approve = summary.recommendation === 'APPROVE';
  const items = summary.items || [];
  return `<div class="business-summary"><div class="result-heading">${badge(approve?'承認してよい':'修正を依頼',approve?'success':'error')}<strong>業務向け要約</strong></div><div class="summary-overview"><h4>判断の理由</h4><p>${esc(summary.decision_reason)}</p><h4>影響する範囲</h4><p>${esc(summary.affected_scope)}</p><h4>判断・指示すること</h4><p>${esc(summary.required_action)}</p></div>${items.map(item => `<div class="business-finding"><div class="finding-refs">${(item.finding_ids || []).map(id => `<button type="button" data-finding-id="${esc(id)}">技術的な指摘 #${esc(id)}</button>`).join('')}</div><h4>何が問題か</h4><p>${esc(item.problem)}</p><h4>業務上の不都合</h4><p>${esc(item.business_impact)}</p><h4>影響する範囲</h4><p>${esc(item.affected_scope)}</p><h4>判断・指示すること</h4><p>${esc(item.required_action)}</p></div>`).join('')}</div>`;
}
function evidenceView() {
  const ev = detail.evidence.find(e => e.phase_id === evidencePhase);
  const head = `<div class="evidence-heading"><h3>${tab === 'tests' ? '機械検証' : 'レビュー結果'}</h3><select id="evidence-phase" aria-label="結果を見るフェーズ">${phaseOptions()}</select></div>`;
  if (!ev) return head + '<p class="muted">フェーズの計画ができると表示されます。</p>';
  if (tab === 'tests') {
    const result = ev.verification;
    if (!result) return head + '<p class="muted">このフェーズの検証結果はまだありません。</p>';
    return head + `<div class="result-card"><div class="result-heading">${badge(result.overall, result.overall === 'PASS' ? 'success':'error')}<strong>テスト・検証結果</strong><span class="muted">Run ${esc(result.run_id)} · ${esc(timestamp(result.created_at))}</span></div>${(result.checks || []).map(c => `<div class="check-row"><strong>${esc(c.name)}</strong>${badge(c.status,c.status === 'PASS'?'success':'error')}<code>${esc(c.command)}</code></div>`).join('')}</div><p class="hint">保存された最新の検証結果です。修正中は前回の結果が表示されます。</p><details open><summary>検証ログ</summary><pre class="log">${esc(ev.verification_log || 'ログはありません。')}</pre></details>`;
  }
  const result = ev.review;
  if (!result) return head + '<p class="muted">このフェーズのレビュー結果はまだありません。</p>';
  const summary = ev.review_summary;
  return head + businessReviewSummary(summary) + technicalFindings(result,!summary || summary.status !== 'SUCCESS') + '<p class="hint">保存された最新のレビュー結果です。修正中は前回の結果が表示されます。</p>';
}
function overviewView() {
  const current = detail.phases.find(p => p.id === detail.state.current_phase);
  const iss = detail.issue;
  return `${current ? `<section class="section"><h3>このフェーズの受け入れ条件</h3><ul>${current.acceptance_criteria.map(c=>`<li>${esc(c)}</li>`).join('')}</ul></section>`:''}
  <section class="section"><h3>フェーズ構成</h3><div class="phase-list">${detail.phases.map((p,i)=>`<div class="phase-item"><span class="phase-index">${String(i+1).padStart(2,'0')}</span><p>${esc(p.goal)}</p>${badge(phases[p.status],p.status==='accepted'?'success':p.status==='awaiting_human_check'?'waiting':'')}</div>`).join('') || '<p class="muted">要求からフェーズを計画しています。</p>'}</div></section>
  ${detail.checkpoint ? `<details><summary>直近Runのチェックポイント</summary><pre class="log">${esc(detail.checkpoint)}</pre></details>`:''}
  <details><summary>依頼の詳細</summary><div class="section"><p class="prose">${esc(iss.description || detail.config.request || '指定なし')}</p></div></details>
  <details><summary>実行設定</summary><form id="settings-form" class="settings-form"><label>エージェント<select name="agent" ${jobActive(detail)?'disabled':''}>${config.agents.map(a=>`<option ${a===detail.config.agent?'selected':''}>${esc(a)}</option>`).join('')}</select></label><label>リポジトリのパス<input name="repository" value="${esc(detail.config.repository)}" required ${jobActive(detail)?'disabled':''}></label><p class="hint">外部リポジトリを使う場合は指定してください。</p><p class="form-error" role="alert"></p><button class="button" ${jobActive(detail)?'disabled':''}>設定を保存</button></form></details>`;
}
function actionBar() {
  const disabled = submitting ? 'disabled' : '';
  if (jobActive(detail)) return `<div class="action-bar"><p class="action-note">画面を閉じても実行は続きます</p><div><button class="button danger" id="stop-open" ${detail.job.status==='stopping'?'disabled':''}>${detail.job.status==='stopping'?'停止しています…':'実行を停止'}</button></div></div>`;
  if (detail.actions.completed) return '<div class="action-bar"><p class="action-note">すべてのフェーズが承認されました</p><div>'+badge('完了','success')+'</div></div>';
  if (detail.actions.human_check) return `<div class="action-bar"><p class="action-note">テスト・レビュー結果を確認して判断</p><div><button class="button" id="feedback-open" ${disabled}>修正を依頼</button><button class="button primary" id="accept" ${disabled}>承認して${detail.phases.at(-1)?.id===detail.state.current_phase?'完了':'次へ'}</button></div></div>`;
  return `<div class="action-bar"><p class="action-note">保存済みの状態から進めます</p><div><button class="button primary" id="run" ${disabled}>${detail.job?.status==='failed'?'再実行':'実行を開始・再開'}</button></div></div>`;
}
function renderDetail(force = false) {
  document.body.classList.toggle('has-selection', !!selected);
  if (!detail || !selected) {
    $('#detail-pane').innerHTML = '<div class="empty-detail"><span class="empty-mark">↗</span><h2>Issueを選んで確認</h2><p>進行状況、テスト結果、レビュー結果を<br>ここで確認できます。</p></div>'; lastRender=''; return;
  }
  const signature = JSON.stringify([detail,tab,evidencePhase,submitting]);
  if (!force && signature === lastRender) return;
  if (!force && $('#settings-form')?.contains(document.activeElement)) return;
  lastRender = signature;
  const current = detail.phases.find(p => p.id === detail.state.current_phase);
  const count = detail.phases.filter(p => p.status === 'accepted').length;
  const allocations = Object.values(detail.allocation_usage || {}).flatMap(a => (a.buckets || []).map(b => `<span>${esc(b.label)}: ${Number(b.used_percent).toLocaleString()}% 使用</span>`)).join('');
  const iss = detail.issue;
  $('#detail-pane').innerHTML = `<button class="back" id="back">← Issue一覧</button><header class="detail-header"><p class="id">${esc(iss.id)}</p><div class="title-line"><h1>${esc(iss.description)}</h1>${badge(...status(detail))}</div><div class="detail-meta"><span>${esc(detail.config.agent)}</span><span>${count} / ${detail.phases.length} フェーズ完了</span>${detail.usage.total_tokens != null?`<span>${Number(detail.usage.total_tokens).toLocaleString()} tokens</span>`:''}${allocations}</div></header>
  ${detail.job?.error ? `<p class="notice error">${esc(detail.job.error)}</p>`:''}
  ${current ? `<section class="phase-card"><div class="row"><span class="eyebrow">CURRENT PHASE · ${esc(current.id)}</span><span class="muted">Run ${esc(detail.state.current_run)}</span></div><h2>${esc(current.goal)}</h2><div class="phase-track">${detail.phases.map(p=>`<span class="${esc(p.status)}"></span>`).join('')}</div><p class="muted">${jobActive(detail)?'ローカルPCで処理を進めています。':detail.actions.human_check?'テストとレビューが完了しました。あなたの判断を待っています。':'実行を再開できます。'}</p></section>`:''}
  <nav class="tabs" aria-label="Issueの表示内容">${[['overview','概要'],['tests','テスト'],['review','レビュー'],['logs','実行ログ']].map(([id,label])=>`<button data-tab="${id}" class="${tab===id?'selected':''}">${label}</button>`).join('')}</nav>
  <div id="tab-content">${tab==='overview'?overviewView():tab==='logs'?`<h3>直近の実行ログ</h3><p class="hint">エラーとCLIの実行結果を表示します。エージェント内部のログはPC側に保存されます。</p><pre class="log">${esc(detail.job_log || '出力はまだありません。')}</pre>`:evidenceView()}</div>${actionBar()}`;
  $('#back').onclick = () => updateHash(currentProjectId, '');
  document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => {tab=button.dataset.tab;renderDetail(true);});
  if ($('#evidence-phase')) $('#evidence-phase').onchange = event => {evidencePhase=event.target.value;renderDetail(true);};
  document.querySelectorAll('[data-finding-id]').forEach(button => button.onclick = () => {const details=$('#technical-review-details');if(details)details.open=true;const finding=$('#review-finding-'+button.dataset.findingId);if(finding){finding.scrollIntoView({behavior:'smooth',block:'center'});finding.focus({preventScroll:true});}});
  if ($('#run')) $('#run').onclick = () => action('run');
  if ($('#accept')) $('#accept').onclick = () => action('human-check',{result:'ACCEPT',revision:detail.revision});
  if ($('#feedback-open')) $('#feedback-open').onclick = () => {decisionSnapshot={id:selected,revision:detail.revision};$('#feedback-phase').textContent=detail.state.current_phase+' · '+current.goal;openDialog('feedback-dialog');};
  if ($('#stop-open')) $('#stop-open').onclick = () => {decisionSnapshot={id:selected};openDialog('stop-dialog');};
  if ($('#settings-form')) $('#settings-form').onsubmit = event => submitForm(event, async form => {await api(issuePathFor(currentProjectId, selected)+'/configure',Object.fromEntries(new FormData(form)));toast('設定を保存しました');await refresh();});
}

async function refresh() {
  if (!config) return;
  if (refreshBusy) {refreshAgain=true;return;}
  refreshBusy = true;
  const parsed = parseHash();
  let projId = parsed.projectId;
  let issId = parsed.issueId;

  try {
    const projData = await api('/api/projects');
    projects = projData.projects || [];
    if (!projId && projects.length) {
      projId = projects[0].id;
    }
    currentProjectId = projId;
    renderProjectSelect();

    let list = { issues: [] };
    let loaded = null;

    if (projId) {
      list = await api(`/api/projects/${encodeURIComponent(projId)}/issues`);
      items = list.issues || [];
      if (issId) {
        try {
          loaded = await api(issuePathFor(projId, issId));
        } catch (_) {
          loaded = null;
        }
      }
    } else {
      list = await api('/api/initiatives');
      items = (list.initiatives || []).map(item => ({...item, issue: item.issue || item.initiative}));
      if (issId) {
        try {
          loaded = await api(`/api/initiatives/${encodeURIComponent(issId)}`);
        } catch (_) {
          loaded = null;
        }
      }
    }

    selected = issId;
    if (issId === selected) {
      detail = loaded;
      if (detail && !detail.phases.some(p=>p.id===evidencePhase)) evidencePhase=detail.state.current_phase || detail.phases.at(-1)?.id;
      renderDetail();
    }
    renderList();
    $('#connection').textContent='接続済み';$('#connection').className='connection online';
  } catch(error) {
    $('#connection').textContent='接続エラー';$('#connection').className='connection offline';
    if (!detail) $('#detail-pane').innerHTML=`<button class="back" id="back-error">← Issue一覧</button><p class="notice error">${esc(error.message)}<br>PCの起動状態とTailscale接続を確認してください。</p>`;
    if ($('#back-error')) $('#back-error').onclick=()=>updateHash(currentProjectId, '');
  } finally {refreshBusy=false;if(refreshAgain){refreshAgain=false;refresh();}}
}

async function action(name, data={}, id=selected) {
  if (submitting) return;
  submitting=true;renderDetail(true);
  try {
    await api(issuePathFor(currentProjectId, id)+'/'+name,{request_id:requestId(),...data});
    toast(name==='human-check'?'判断を受け付けました':'実行を受け付けました');
    await refresh();
  } catch(error){
    toast(error.message,true);
    await refresh();
  } finally {
    submitting=false;renderDetail(true);
  }
}

function openDialog(id) {const dialog=$('#'+id);const error=dialog.querySelector('.form-error');if(error)error.textContent='';dialog.showModal();}
async function submitForm(event, callback) {
  event.preventDefault(); const form=event.currentTarget; const button=form.querySelector('[type="submit"]') || form.querySelector('button:not([type])');
  if (button.disabled) return;button.disabled=true;form.querySelector('.form-error').textContent='';
  try {await callback(form);} catch(error){form.querySelector('.form-error').textContent=error.message;} finally {button.disabled=false;}
}
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>button.closest('dialog').close());

if ($('#project-select')) {
  $('#project-select').onchange = event => {
    const nextProj = event.target.value;
    if (nextProj !== currentProjectId) {
      currentProjectId = nextProj;
      updateHash(nextProj, '');
    }
  };
}

if ($('#new-project-open')) {
  $('#new-project-open').onclick = () => openDialog('new-project-dialog');
}

if ($('#new-project-form')) {
  $('#new-project-form').onsubmit = event => submitForm(event, async form => {
    const data = Object.fromEntries(new FormData(form));
    const result = await api('/api/projects', { ...data, request_id: requestId() });
    $('#new-project-dialog').close();
    form.reset();
    currentProjectId = result.project_id;
    updateHash(currentProjectId, '');
    toast('プロジェクトを作成しました');
    await refresh();
  });
}

$('#new-open').onclick=()=>openDialog('new-dialog');
$('#new-form').onsubmit=event=>submitForm(event,async form=>{
  const data = Object.fromEntries(new FormData(form));
  let job;
  if (currentProjectId) {
    job = await api(`/api/projects/${encodeURIComponent(currentProjectId)}/issues`, {...data, request_id: requestId()});
  } else {
    job = await api('/api/initiatives', {...data, request_id: requestId()});
  }
  $('#new-dialog').close();
  form.reset();
  const newId = job.issue_id || job.id;
  updateHash(currentProjectId, newId);
  toast('実行を開始しました');
  await refresh();
});

$('#feedback-form').onsubmit=event=>submitForm(event,async form=>{
  await api(issuePathFor(currentProjectId, decisionSnapshot.id)+'/human-check',{request_id:requestId(),result:'FEEDBACK',feedback:form.elements.feedback.value,revision:decisionSnapshot.revision});
  $('#feedback-dialog').close();
  form.reset();
  toast('修正を依頼しました');
  await refresh();
});

$('#stop-form').onsubmit=event=>submitForm(event,async()=>{
  await api(issuePathFor(currentProjectId, decisionSnapshot.id)+'/stop',{});
  $('#stop-dialog').close();
  toast('停止を受け付けました');
  await refresh();
});

$('#filters').onclick=event=>{if(!event.target.dataset.filter)return;filter=event.target.dataset.filter;$('#filters .selected').classList.remove('selected');event.target.classList.add('selected');renderList();};
window.addEventListener('hashchange',()=>{
  const parsed = parseHash();
  currentProjectId = parsed.projectId;
  selected = parsed.issueId;
  detail=null;lastRender='';tab='overview';evidencePhase=null;
  document.body.classList.toggle('has-selection',!!selected);
  refresh();
});
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
