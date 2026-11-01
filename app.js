'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let mode = params.get('mode') === 'demo' ? 'demo' : 'live';
let symbol = ['BTC-USD', 'ETH-USD'].includes(params.get('symbol')) ? params.get('symbol') : 'BTC-USD';
let current = null, request = null, generation = 0, busy = false, toastTimer, curve = [], v4state = null,
  v5state = null, forwardState = null, forwardBusy = false, forwardLoaded = false, forwardClockAnchor = null;
let chartBox = {width:900,left:58,right:886};
const usd = (n, decimals = 2) => n == null ? '—' : new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', minimumFractionDigits:decimals, maximumFractionDigits:decimals}).format(n);
const pct = (n, sign = false) => n == null ? '—' : `${sign && n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`;
const date = (ts, short = false) => ts == null ? '—' : new Intl.DateTimeFormat('en-GB', {timeZone:'UTC', day:'2-digit', month:'short', ...(short ? {} : {hour:'2-digit', minute:'2-digit', hour12:false})}).format(new Date(ts * 1000));
const text = (id, value) => { $(id).textContent = value; };
function signed(id, value, percent = true) { text(id, percent ? pct(value, true) : usd(value)); $(id).classList.toggle('positive', value > 0); $(id).classList.toggle('negative', value < 0); }
function toast(message, error = false) { text('toast', message); $('toast').classList.toggle('error', error); $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 7500); }
function cell(row, value, cls) { const el = document.createElement('td'); el.textContent = value; if (cls) el.className = cls; row.append(el); return el; }
function emptyRow(body, count, message) { const row = document.createElement('tr'); const td = cell(row, message, 'empty-cell'); td.colSpan = count; body.replaceChildren(row); }
function selectContext() {
  $('mode-live').setAttribute('aria-pressed', String(mode === 'live'));
  $('mode-demo').setAttribute('aria-pressed', String(mode === 'demo'));
  $('symbol').value = symbol;
  const url = new URL(location.href); url.searchParams.set('mode', mode); url.searchParams.set('symbol', symbol); history.replaceState(null, '', url);
  $('provenance').classList.toggle('synthetic', mode === 'demo');
  text('source-tag', mode === 'demo' ? 'SYNTHETIC DEMO' : 'READ-ONLY LEGACY SNAPSHOT');
  text('source-note', mode === 'demo' ? 'Seeded synthetic prices · fixed January 2025 replay · never market evidence.' : 'Cached CoinGecko history is read-only. New public ML training and refresh are disabled under current API terms.');
  text('chart-mode', mode === 'demo' ? 'SYNTHETIC / SIMULATED' : 'ARCHIVED / SIMULATED');
  text('account-tag', mode === 'demo' ? 'SYNTHETIC REPLAY / PAPER FUNDS' : 'LEGACY ACCOUNT · READ ONLY');
  text('ledger-mode', mode === 'demo' ? 'SYNTHETIC DEMO · SIMULATED FILLS' : 'ARCHIVED PUBLIC DATA · SIMULATED FILLS');
  $('refresh').hidden = true;
  $('export').href = `/api/export.csv?mode=${mode}&symbol=${symbol}`;
}
function loading() {
  $('research').setAttribute('aria-busy', 'true');
  $('chart-empty').hidden = false; text('chart-empty-title', 'Loading research'); text('chart-empty-message', 'Reading the local experiment…');
  $('chart').replaceChildren(); curve = []; text('market-price', '—');
  for (const id of ['metric-return','metric-drawdown','metric-exposure','metric-costs','probability']) text(id, '—');
  emptyRow($('ledger-body'), 7, 'Loading the selected simulated account…');
}
async function load(showLoading = false) {
  const stamp = ++generation;
  request?.abort(); request = new AbortController();
  if (showLoading) loading();
  try {
    const response = await fetch(`/api/state?mode=${mode}&symbol=${symbol}`, {signal:request.signal});
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Local data could not be loaded.');
    if (stamp !== generation) return;
    current = state; render(state);
  } catch (error) {
    if (error.name === 'AbortError') return;
    text('notice', `Local server unavailable: ${error.message}. Restart server.py, then reload this page.`); $('notice').hidden = false;
    $('research').setAttribute('aria-busy', 'false'); text('chart-empty-title', 'Local server unavailable'); text('chart-empty-message', 'Restart the local server to reconnect.');
  }
}
function render(state) {
  const demo = state.mode === 'demo', result = state.research, a = state.account, feed = state.feed;
  $('research').setAttribute('aria-busy', String(feed.state === 'loading'));
  $('research-title').replaceChildren(document.createTextNode(symbol.split('-')[0] + ' '));
  const suffix = document.createElement('span'); suffix.textContent = '/ USD'; $('research-title').append(suffix);
  text('market-price', usd(state.market.price));
  text('market-time', demo ? 'Synthetic fixture · Jan 2025' : feed.quote ? `Provider ${date(feed.quote.market_at)} UTC` : `Latest sample ${date(feed.latest_sample)} UTC`);
  text('feed-short', demo ? 'Synthetic fixture · fixed seed' : 'Legacy public feed retired · cached snapshot only');
  $('feed-dot').style.backgroundColor = demo ? 'var(--amber)' : feed.fresh ? 'var(--green)' : feed.state === 'error' ? 'var(--red)' : 'var(--amber)';
  $('refresh').disabled = true;
  $('try-demo').hidden = demo || (feed.state !== 'error' && state.market.samples > 0);
  const unavailable = !demo && !feed.fresh;
  $('notice').hidden = !unavailable;
  if (unavailable) text('notice', `${result.ready ? 'Saved historical result available. ' : ''}Legacy CoinGecko history is read-only under current API terms; public model training, refresh and new ML paper entries are disabled. Use the seeded demo or V5 prospective price-rule cohort.`);
  text('chart-title', unavailable && result.ready ? 'Cached held-out equity' : 'Held-out equity');
  $('chart-empty').hidden = !!result.ready;
  if (result.ready) {
    const model = result.metrics.model;
    signed('metric-return', model.return); text('metric-drawdown', pct(model.max_drawdown));
    text('metric-exposure', pct(model.exposure)); text('metric-trades', `${model.trades} completed model trades${model.halted ? ' · risk halted' : ''}`);
    text('metric-costs', usd(model.costs));
    text('chart-subtitle', `$10,000 initial cash · ${date(result.splits.holdout.start, true)} – ${date(result.splits.holdout.end, true)} UTC`);
    text('chart-foot', `${demo ? 'SYNTHETIC · ' : ''}Actual next-sample entry → following-sample exit. Equal 50% entry budgets; identical held-out dates.`);
    const rows = [];
    for (const [key, name, cls] of [['model','Logistic model','side-buy'], ['momentum','6-sample momentum','side-sell'], ['buy_hold','Buy & hold','']]) {
      const values = result.metrics[key], row = document.createElement('tr');
      cell(row, name + (values.halted ? ' · halted' : ''), cls); cell(row, pct(values.return, true), values.return > 0 ? 'positive' : values.return < 0 ? 'negative' : '');
      cell(row, pct(values.max_drawdown)); cell(row, pct(values.exposure)); cell(row, usd(values.costs)); rows.push(row);
    }
    $('comparison-body').replaceChildren(...rows);
    text('threshold', `Validation entry threshold ≥ ${(state.latest_threshold * 100).toFixed(0)}%`);
    text('probability-source', a.experiment_id && mode === 'live' ? `Paper model pinned to ${a.experiment_id.slice(0,8)}. See account audit.` : 'Latest completed sample · current research snapshot.');
    const splits = [];
    for (const [key, label] of [['train','Train'],['validation','Validate'],['holdout','Held out']]) {
      const entry = result.splits[key], div = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd'), small = document.createElement('small');
      dt.textContent = label; dd.textContent = `${entry.count} observations`; small.textContent = `${date(entry.start,true)} – ${date(entry.end,true)}`; dd.append(small); div.append(dt,dd); splits.push(div);
    }
    $('splits').replaceChildren(...splits);
    text('split-note', `${result.purged} boundary targets purged. Holdout begins after validation targets end. Gaps invalidate affected windows.`);
    text('model-score', `Held-out accuracy ${pct(result.accuracy)} · train-majority baseline ${pct(result.majority_accuracy)} · Brier ${result.brier.toFixed(3)}`);
    text('experiment', `EXPERIMENT ${result.id.slice(0,12)} / FROZEN SNAPSHOT`);
    text('coefficient-note', `Standardized coefficients (not causal): ${result.coefficients.map(([name,w]) => `${name} ${w >= 0 ? '+' : ''}${w.toFixed(3)}`).join(' · ')}. Fixed 180 training passes, L2 = 0.02.`);
    drawChart(result.curve);
  } else {
    for (const id of ['metric-return','metric-drawdown','metric-exposure','metric-costs']) text(id, '—');
    text('metric-trades', 'No comparison loaded'); text('chart-subtitle', '$10,000 initial cash · equal 50% entry budgets');
    text('chart-empty-title', feed.state === 'loading' || state.job.running ? 'Loading public research' : 'No public research yet');
    text('chart-empty-message', demo ? (state.market.samples ? result.message : 'Deliberately load or replay the seeded synthetic demo.') : (result.message || 'Legacy CoinGecko research is read-only under current API terms; public refresh and model training are disabled.'));
    $('chart').replaceChildren(); curve = [];
    emptyRow($('comparison-body'), 5, 'No held-out comparison is available. Synthetic demo is a separate mode.');
    text('threshold', 'Threshold unavailable until a model is fitted'); text('probability-source', 'No fitted model.');
    text('experiment', 'EXPERIMENT —'); text('model-score', 'Insufficient public observations for evaluation.');
    text('split-note', 'Boundary targets are purged before evaluation.');
    const ds = $('splits').querySelectorAll('dd'); ds.forEach(d => d.textContent = '—');
  }
  text('probability', state.latest_probability == null ? '—' : pct(state.latest_probability));
  $('probability-bar').value = state.latest_probability ?? 0;
  text('account-equity', usd(a.equity)); signed('account-return', a.return); $('account-return').textContent += ' since reset';
  text('account-cash', usd(a.cash)); text('account-qty', `${a.qty.toFixed(6)} ${symbol.split('-')[0]}`);
  text('account-exposure', `${pct(a.exposure)} of current equity`); text('account-status', a.status.toUpperCase());
  text('account-fill-count', `${a.fills} simulated fills`); text('account-reason', a.reason || 'Waiting for a new observed signal.');
  text('account-costs', `${usd(a.fees + a.slippage)} modeled costs`);
  $('start').hidden = !demo;
  $('pause').hidden = demo || !a.qty;
  text('start', demo ? a.status === 'complete' ? 'Replay complete' : 'Run seeded replay' : a.experiment_id ? 'Resume paper account' : 'Start paper account');
  $('start').disabled = busy || a.status === 'complete';
  $('reset').hidden = !demo;
  if (!demo && a.qty) text('pause','Exit at fresh quote');
  $('pause').disabled = busy; $('reset').disabled = busy;
  renderLedger(state);
}
function renderLedger(state) {
  if (!state.ledger.length) emptyRow($('ledger-body'), 7, state.mode === 'demo' ? state.account.status === 'complete' ? 'Replay complete. The model stayed in cash; no synthetic fills were created.' : 'No synthetic fills. Run the seeded replay to audit simulated execution.' : 'No simulated fills. Starting skips historical signals and waits for the next new hourly signal.');
  else {
    const rows = state.ledger.map(fill => { const row = document.createElement('tr');
      cell(row, date(fill.fill_at) + ' UTC'); cell(row, fill.side, fill.side === 'BUY' ? 'side-buy' : 'side-sell'); cell(row, fill.qty.toFixed(6));
      cell(row, usd(fill.price)); cell(row, usd(fill.fee)); cell(row, fill.side === 'SELL' ? usd(fill.pnl) : '—', fill.pnl > 0 ? 'positive' : fill.pnl < 0 ? 'negative' : ''); cell(row, fill.reason); return row;
    }); $('ledger-body').replaceChildren(...rows);
  }
  text('decision-count', `${state.decisions.length} recent decisions`);
  if (!state.decisions.length) text('decisions-body', 'No new hourly signal has been processed.');
  else $('decisions-body').replaceChildren(...state.decisions.map(d => { const div = document.createElement('div'); div.className = 'decision-item'; const tag = document.createElement('span'); tag.textContent = d.status.toUpperCase(); div.append(tag, document.createTextNode(`${date(d.signal_ts)} UTC · ${d.reason} · entry due ${date(d.due_at)} UTC`)); return div; }));
}
const v4pct = value => value == null || !Number.isFinite(value) ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
const v4int = value => value == null ? '—' : new Intl.NumberFormat('en-US').format(value);
const v4money = value => value == null || !Number.isFinite(value) ? '—' : `${new Intl.NumberFormat('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}).format(value)} USDT`;
const v4hash = value => value ? `${value.slice(0,12)}…${value.slice(-8)}` : '—';
function v4empty(body, cols, message) { const row=document.createElement('tr'), td=cell(row,message,'empty-cell'); td.colSpan=cols; body.replaceChildren(row); }
async function loadV4() {
  try {
    const response = await fetch('/api/research/v4'); const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Historical research status is unavailable.');
    v4state = state; renderV4(state);
  } catch (error) {
    text('v4-source-status', `Local archive status unavailable: ${error.message}`);
    text('v4-job-state', 'Could not read local V4 status.');
  }
}
function renderV4(state) {
  const data=state.data||{}, job=state.job||{}, result=state.result, progress=job.progress||{};
  const running=!!job.running, completed=Number(data.completed||0), total=Number(data.total||168);
  const fitProgress=job.action==='evaluate'&&progress.progress_unit==='fits';
  const completedJob=Number(fitProgress?progress.completed_fits:(progress.completed??completed));
  const progressTotal=Number(fitProgress?progress.total_fits:(progress.total??total));
  const rawState=data.state||'empty';
  const sourceText=rawState==='verified' ? `Verified ${v4int(completed)} / ${v4int(total)} monthly archives · official SHA256 · ${Object.values(data.datasets||{}).reduce((n,x)=>n+(x.rows||0),0).toLocaleString()} normalized bars` :
    rawState==='empty' ? (result ? 'Saved result available · local archive cache empty; fetch to rebuild data before evaluating.' : 'No archives downloaded. Use the explicit download control to start.') :
    rawState==='error' ? data.error||'The archive manifest needs attention.' :
    `${rawState.toUpperCase()} · ${v4int(completed)} / ${v4int(total)} monthly archives verified`;
  text('v4-source-status',sourceText);
  $('v4-source-status').classList.toggle('positive',rawState==='verified');
  $('v4-source-status').classList.toggle('negative',rawState==='error');
  const fraction=progressTotal>0?Math.min(progressTotal,completedJob):0;
  $('v4-progress').max=Math.max(1,progressTotal); $('v4-progress').value=fraction;
  $('v4-progress').setAttribute('aria-label',fitProgress?'Completed walk-forward fit progress':'Verified archive progress');
  text('v4-job-count',fitProgress?`${v4int(completedJob)} / ${v4int(progressTotal)} fits complete`:`${v4int(completedJob)} / ${v4int(progressTotal)} archives`);
  let jobLine=running ? (job.action==='fetch' ? 'Downloading and verifying the official archive set…' :
    fitProgress ? `Fit ${Math.min(completedJob+1,progressTotal)} of ${progressTotal} · ${progress.symbol} · ${progress.fold}` : 'Starting walk-forward fits…') :
    job.state==='complete' ? (job.action==='fetch' ? 'Archive verification completed.' : 'Study completed and saved.') :
    job.state==='paused' ? 'Archive fetch paused and can resume.' :
    job.state==='failed' ? 'Job stopped with an error.' : result && rawState==='empty' ? 'Saved historical result loaded · local archive cache is empty.' :
    result ? 'No research job running · showing saved results.' : rawState==='empty' ? 'No research job running · local archive cache is empty.' : 'No research job running.';
  if (!running && job.action) jobLine += ` Last job: ${job.state}.`;
  text('v4-job-state',jobLine);
  const failures=(data.failed_archives||[]).slice(0,2).map(x=>`${x.archive}: ${x.error}`).join(' · ');
  const detail=running && fitProgress ? 'Progress counts completed folds; the currently shown fold is still running.' :
    running && progress.current ? `${progress.phase}: ${progress.current}${progress.error?` · ${progress.error}`:''}` :
    failures || data.error || job.error || (data.terms?.current_source_checked ? `Terms checked over TLS ${data.terms.verified_at_utc||''}.` : result ? 'This saved historical result loaded from disk; page load never starts a download or evaluation.' : 'Page load only reads local status; it never starts a download or evaluation.');
  text('v4-job-detail',detail);
  $('v4-fetch').disabled=running;
  $('v4-evaluate').disabled=running||rawState!=='verified'||state.protocol_status==='historical_test_consumed';
  $('v4-empty').hidden=!!result;
  if (!result) {
    const consumed=state.protocol_status==='historical_test_consumed';
    text('v4-empty-kicker',consumed?'FINAL TEST CONSUMED':'NO RESULTS YET');
    text('v4-empty-title',consumed?'Saved result is missing or invalid; evaluation is blocked.':'Build the archive set, then run the frozen study.');
    text('v4-empty-detail',consumed?'A selection receipt or trial ledger records that the reserved historical test was already opened. Restore a checksum-valid saved result to inspect it.':'Results are saved with source hashes and a trial ledger. Raw exchange archives stay local and are not included in the source bundle.');
  }
  $('v4-results').hidden=!result;
  if (result) renderV4Result(result,data);
}
function renderV4Result(result,dataState={}) {
  const symbol=$('v4-symbol').value, selected=result.symbols?.[symbol];
  $('v4-results').hidden=false; $('v4-empty').hidden=true;
  if (!selected) {
    $('v4-asset-content').hidden=true; $('v4-result-unavailable').hidden=false;
    text('v4-result-title',`${symbol} · unavailable in saved study`);
    text('v4-result-provenance','ASSET NOT PRESENT');
    return;
  }
  $('v4-asset-content').hidden=false; $('v4-result-unavailable').hidden=true;
  const folds=selected.folds||[], final=folds[folds.length-1], test=final?.test||{}, base=test.cost_scenarios?.base||{};
  const model=base.model||{}, hold=base.buy_hold||{}, uncertainty=test.uncertainty_base_cost||{}, diagnostics=test.classification||{};
  text('v4-result-title',`${symbol} · ${selected.dataset.rows?.toLocaleString()||'—'} four-hour bars`);
  text('v4-result-provenance',result.result_provenance==='newly_evaluated'?'JUST EVALUATED · TEST CONSUMED':
    result.dataset_match_status==='matches_current_verified_datasets'?'SAVED RESULT · DATA MATCHED':'SAVED SNAPSHOT · CACHE DIFFERS');
  text('v4-result-note',`${test.label||'Retrospective historical test.'} Model ${v4pct(model.net_return_pct)} vs half-budget buy-and-hold ${v4pct(hold.net_return_pct)} in the final window. First evaluated ${test.final_test_first_consumed_at_utc||result.final_test_first_consumed_at_utc||'—'}. This consumed historical test cannot show future performance.`);
  text('v4-model-return',v4pct(model.net_return_pct));
  text('v4-model-detail',`Max DD ${v4pct(model.max_drawdown_pct)} · ${model.fills||0} fills / ${model.roundtrips||0} round trips · ${v4pct(model.time_in_market_pct)} active · ${model.drawdown_halts||0} risk halt(s)`);
  text('v4-hold-return',v4pct(hold.net_return_pct));
  text('v4-hold-detail',`Max DD ${v4pct(hold.max_drawdown_pct)} · ${hold.roundtrips||0} completed round trips`);
  text('v4-cash-return',v4pct(base.cash?.net_return_pct));
  text('v4-cash-detail',base.cash?.fills===0?'No fills; cash earns no interest and shows no predictive edge.':`${base.cash?.fills??0} fills · cash baseline`);
  text('v4-brier',diagnostics.brier_score==null?'Unavailable':diagnostics.brier_score.toFixed(3));
  text('v4-classification-detail',`${diagnostics.eligible_rows||0} labeled rows · log loss ${diagnostics.log_loss==null?'—':diagnostics.log_loss.toFixed(3)} · accuracy ${diagnostics.accuracy_at_0_5==null?'—':(diagnostics.accuracy_at_0_5*100).toFixed(1)+'%'}`);
  text('v4-chart-caption',`10,000 USDT reset account · fixed final exit ${test.planned_final_exit_utc||'—'} · selected threshold ${final?.selection?.threshold??'—'} from validation only.`);
  drawV4Chart(test.base_cost_curve||{});
  const foldRows=folds.map(fold=>{
    const row=document.createElement('tr'), m=fold.test?.cost_scenarios?.base?.model||{}, mom=fold.test?.cost_scenarios?.base?.momentum||{}, bh=fold.test?.cost_scenarios?.base?.buy_hold||{}, ci=fold.test?.uncertainty_base_cost?.paired_excess_bps_ci95;
    cell(row,fold.name);cell(row,`${fold.dates_utc.test[0]} – ${fold.dates_utc.test[1]}`);
    cell(row,`${fold.counts.test_rows} / ${fold.counts.test_feature_decisions}`);cell(row,Number(fold.selection.threshold).toFixed(2));
    cell(row,v4pct(m.net_return_pct));cell(row,v4pct(mom.net_return_pct));cell(row,v4pct(bh.net_return_pct));
    cell(row,ci?`${ci[0].toFixed(0)} to ${ci[1].toFixed(0)} bps`:'Unavailable');return row;
  }); $('v4-fold-body').replaceChildren(...foldRows);
  const costRows=[];
  for (const [scenario,label] of [['base','Base · 10 + 5 bps/side'],['higher','Higher · 25 + 10'],['stress','Stress · 50 + 25']]) {
    for (const [key,name] of [['model','Logistic model'],['momentum','7-day momentum'],['buy_hold','Half-budget buy & hold'],['cash','Cash']]) {
      const item=test.cost_scenarios?.[scenario]?.[key]||{}, row=document.createElement('tr');
      cell(row,label);cell(row,name);cell(row,v4pct(item.net_return_pct));cell(row,v4pct(item.max_drawdown_pct));
      cell(row,`${item.fills??0} / ${item.roundtrips??0}`);cell(row,v4money((item.fees_usdt||0)+(item.slippage_usdt||0)));
      cell(row,v4pct(item.time_in_market_pct));cell(row,item.drawdown_halts??0);costRows.push(row);
    }
  }
  $('v4-cost-body').replaceChildren(...costRows);
  const facts=[['Source',result.source],['Result data identity',result.dataset_match_status==='matches_current_verified_datasets'?'Matches current verified datasets':'Saved snapshot; current cache differs or is unavailable'],['Current archive cache',`${dataState.completed??'—'} / ${dataState.total??'—'} verified`],['Quote units',result.quote_asset],['Bar interval',result.bar_interval],
    ['Observed bars',`${selected.dataset.rows} / ${selected.dataset.expected_rows??'—'}`],['Missing four-hour slots',selected.dataset_gaps?.missing_4h_slots],
    ['Rejected partial bars',selected.dataset_gaps?.rejected_partial_rows],
    ['Zero-volume bars',selected.dataset_gaps?.zero_volume_bars],['First / last data',`${selected.dataset.first_open_utc||'—'} → ${selected.dataset.last_close_exclusive_utc||'—'}`],
    ['Dataset SHA256',selected.dataset.canonical_sha256],['Protocol SHA256',result.protocol_sha256],['Study ID',result.study_id],
    ['Terms SHA256',result.license?.terms_sha256],['Terms checked',result.license?.verified_at_utc],['License',result.license?.license]];
  const factNodes=facts.map(([name,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=value==null?'—':String(value);div.append(dt,dd);return div;});
  $('v4-facts').replaceChildren(...factNodes);
  const ci=uncertainty.paired_excess_bps_ci95;
  text('v4-uncertainty',uncertainty.state==='unavailable' ? `Uncertainty unavailable: ${uncertainty.reason||'no aligned returns'}.` :
    `${uncertainty.observations} paired observed opens; 7-day circular moving blocks, ${uncertainty.replicates} seeded resamples. Mean model minus buy-and-hold: ${uncertainty.mean_paired_excess_bps.toFixed(1)} bps; descriptive 95% interval ${ci[0].toFixed(1)} to ${ci[1].toFixed(1)} bps. ${uncertainty.inactive_zero_volatility_no_edge_evidence?'The model was inactive with zero return variation; this gives no edge evidence. ':''}${uncertainty.interpretation}`);
  renderV4Diagnostics(selected,final);
  text('v4-license',`${result.license?.attribution||'Binance Vision official spot datasets'} · ${result.license?.license||'CC BY-NC-SA 4.0'}. Personal non-production historical research only. Derived charts, models and results retain these terms. This venue model never arms the aggregate-USD paper account.`);
}
function drawV4Chart(curves) {
  const keys=['model','momentum','buy_hold'], colors={model:'#356e68',momentum:'#765b35',buy_hold:'#697a7c'}, dashes={model:'',momentum:'8 4',buy_hold:'2 4'};
  const maps=Object.fromEntries(keys.map(key=>[key,new Map((curves[key]||[]).map(p=>[p.ts,p.equity]))]));
  const times=[...new Set(keys.flatMap(key=>[...maps[key].keys()]))].sort((a,b)=>a-b);
  const points=times.map(ts=>({ts,...Object.fromEntries(keys.map(key=>[key,maps[key].get(ts)??10000]))}));
  const svg=$('v4-chart'); if(!points.length){svg.replaceChildren();return;}
  const width=900,height=260,left=55,right=890,top=14,bottom=215,values=points.flatMap(p=>keys.map(key=>p[key]));
  let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.14,25);lo-=pad;hi+=pad;
  const x=i=>left+i/Math.max(1,points.length-1)*(right-left),y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top),ns='http://www.w3.org/2000/svg';
  const node=(name,attrs,label)=>{const e=document.createElementNS(ns,name);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,String(v)));if(label)e.textContent=label;return e;};
  const parts=[node('title',{},'Retrospective historical test equity'),node('desc',{},'Model, seven-day momentum and half-budget buy-and-hold. All begin with ten thousand USDT and share the fixed final exit date.')];
  for(let i=0;i<5;i++){const yy=top+i/4*(bottom-top),value=hi-i/4*(hi-lo);parts.push(node('line',{x1:left,x2:right,y1:yy,y2:yy,class:'v4-grid'}),node('text',{x:left-8,y:yy+3,'text-anchor':'end',class:'v4-axis'},`${(value/1000).toFixed(1)}k`));}
  parts.push(node('line',{x1:left,x2:right,y1:y(10000),y2:y(10000),stroke:'#59636c','stroke-dasharray':'3 5','stroke-width':.8}));
  for(const key of keys){const d=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(p[key]).toFixed(2)}`).join(' ');parts.push(node('path',{d,stroke:colors[key],class:'v4-line','stroke-dasharray':dashes[key]}));}
  for(const i of [0,Math.floor(points.length/2),points.length-1])parts.push(node('text',{x:x(i),y:242,'text-anchor':i===0?'start':i===points.length-1?'end':'middle',class:'v4-axis'},date(points[i].ts,true)));
  svg.replaceChildren(...parts);
}
function renderV4Diagnostics(study,fold) {
  const body=$('v4-diagnostics-body'),diag=fold.test?.classification||{}, bins=diag.reliability_bins||[];
  const wrapper=document.createElement('div'), summary=document.createElement('p');
  const baseline=diag.train_majority_baseline;
  summary.textContent=`${diag.eligible_rows||0} future-label-eligible test rows; ${diag.positives||0} positive. Model Brier ${diag.brier_score==null?'—':diag.brier_score.toFixed(4)} / log loss ${diag.log_loss==null?'—':diag.log_loss.toFixed(4)} / accuracy at 0.5 ${diag.accuracy_at_0_5==null?'—':(diag.accuracy_at_0_5*100).toFixed(1)+'%'}. Train-majority class ${baseline?.label??'—'} accuracy ${baseline?.accuracy==null?'—':(baseline.accuracy*100).toFixed(1)+'%'}; constant train positive-rate ${baseline?.constant_train_positive_rate==null?'—':(baseline.constant_train_positive_rate*100).toFixed(1)+'%'} baseline Brier ${baseline?.brier_score==null?'—':baseline.brier_score.toFixed(4)} / log loss ${baseline?.log_loss==null?'—':baseline.log_loss.toFixed(4)}.`;
  wrapper.append(summary);
  const table=document.createElement('table'),head=document.createElement('thead'),hrow=document.createElement('tr');
  for(const label of ['Fixed probability bin','Rows','Mean probability','Observed positive share']){const th=document.createElement('th');th.scope='col';th.textContent=label;hrow.append(th);}head.append(hrow);table.append(head);
  const tBody=document.createElement('tbody');for(const bin of bins){const row=document.createElement('tr');cell(row,bin.bin);cell(row,bin.count);cell(row,bin.mean_probability==null?'—':(bin.mean_probability*100).toFixed(1)+'%');cell(row,bin.observed_positive_share==null?'—':(bin.observed_positive_share*100).toFixed(1)+'%');tBody.append(row);}table.append(tBody);wrapper.append(table);
  const gaps=study.dataset_gaps?.partial_bar_rejections||[];
  const gapText=gaps.slice(0,12).map(item=>`${new Date(item.ts*1000).toISOString().slice(0,16).replace('T',' ')} UTC (${item.duration_seconds}s)`).join('; ');
  const missingText=(study.dataset_gaps?.missing_slots_utc||[]).map(item=>String(item).replace('T',' ').slice(0,16)+' UTC').join('; ');
  const quality=document.createElement('p');quality.textContent=`Test quality: ${JSON.stringify(fold.counts.test_quality)}. Train quality: ${JSON.stringify(fold.counts.train_quality)}. Validation quality: ${JSON.stringify(fold.counts.validation_quality)}. Dataset gaps: ${study.dataset_gaps?.missing_4h_slots??'—'} missing slots${missingText?` (${missingText})`:''}; ${study.dataset_gaps?.rejected_partial_rows??0} partial rows rejected${gapText?` (${gapText})`:''}. Missing future labels never remove earlier strategy decisions.`;wrapper.append(quality);
  body.replaceChildren(wrapper);
}
async function startV4(actionName) {
  $('v4-fetch').disabled=true;$('v4-evaluate').disabled=true;
  try {
    const response=await fetch('/api/research/v4',{method:'POST',headers:{'Content-Type':'application/json','X-Crypto-Lab':'1'},body:JSON.stringify({action:actionName})});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'V4 research job could not start.');
    toast(actionName==='fetch'?'Official archive fetch started.':'Frozen evaluation started.');await loadV4();
  } catch(error) {toast(error.message,true);await loadV4();}
}
$('v4-fetch').addEventListener('click',()=>startV4('fetch'));
$('v4-evaluate').addEventListener('click',()=>startV4('evaluate'));
$('v4-symbol').addEventListener('change',()=>{if(v4state?.result)renderV4Result(v4state.result,v4state.data);});

const v5CandidateLabels = {
  cash:'Cash', passive:'Risk-managed passive', momentum_7d:'Positive 7-day momentum',
  frozen_v4_logistic:'Frozen V4 logistic', sma20_100:'SMA20 / SMA100 trend',
  close_breakout_20_10:'20 / 10 close breakout', trend_gated_v4:'Trend-gated frozen V4'
};
const v5pct = value => value == null || !Number.isFinite(value) ? '—' : (value >= 0 ? '+' : '') + value.toFixed(2) + '%';
const v5num = (value, digits=2) => value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits);
const v5money = value => value == null || !Number.isFinite(value) ? '—' : value.toFixed(2) + ' USDT';
async function loadV5() {
  try {
    const response=await fetch('/api/research/v5',{cache:'no-store'});
    const state=await response.json();
    if(!response.ok)throw new Error(state.message||'Saved V5 summary is unavailable.');
    v5state=state;renderV5(state);
  } catch(error) {
    text('v5-study-status','Saved V5 summary unavailable: ' + error.message);
    emptyRow($('v5-cell-body'),9,'No checksum-valid saved summary is available.');
  }
}
function selectedV5Cells() {
  if(!v5state?.available)return [];
  const symbol=$('v5-symbol').value, fold=$('v5-fold').value, cost=$('v5-cost').value;
  return v5state.cells.filter(x=>x.symbol===symbol&&x.fold===fold&&x.cost_scenario===cost)
    .sort((a,b)=>v5state.candidate_order.indexOf(a.candidate)-v5state.candidate_order.indexOf(b.candidate));
}
function renderV5(state) {
  const mobile=$('v5-mobile-results');
  if(!state.available) {
    text('v5-study-status',state.message||'No verified V5 result is available.');
    emptyRow($('v5-cell-body'),9,'No checksum-valid saved summary is available.');
    mobile.replaceChildren();
    return;
  }
  text('v5-study-status','Saved exploratory comparison verified · ' + state.cell_count_complete + '/' + state.cell_count_expected + ' cells · no candidate selected · Time active excludes residual dust; original scores retained. · ' + state.created_at_utc);
  const selected=selectedV5Cells(), rows=[], mobileRows=[];
  text('v5-cell-caption',$('v5-symbol').value + ' · ' + $('v5-fold').selectedOptions[0].textContent + ' · ' + $('v5-cost').selectedOptions[0].textContent + ' · seven fixed candidates');
  for(const item of selected) {
    const m=item.metrics,row=document.createElement('tr');
    row.className='v5-comparison-row';
    const strategy=v5CandidateLabels[item.candidate]||item.candidate;
    const returnValue=v5pct(m.net_return_pct),drawdown=v5pct(m.max_drawdown_pct),exposure=v5pct(m.mean_capital_exposure_pct)+' mean · '+v5pct(m.max_capital_exposure_pct)+' max';
    const cells=[['Candidate',strategy],['Net return',returnValue],['Max drawdown',drawdown],['Mean / max exposure',exposure],
      ['Time active',v5pct(m.time_in_market_pct)],['Turnover × capital',v5num(m.turnover_x_initial_capital,3)+'×'],
      ['Fills / round trips',m.fills+' / '+m.roundtrips],['Fees + slippage',v5money((m.fees_usdt||0)+(m.slippage_usdt||0))],
      ['Halts / active signals after halt',m.drawdown_halts+' / '+m.active_signals_after_halt]];
    cells.forEach(([label,value],index)=>{const el=cell(row,value,index===1?(m.net_return_pct>0?'positive':m.net_return_pct<0?'negative':''):undefined);el.dataset.label=label;el.classList.add('v5-col-'+index);});
    rows.push(row);

    const card=document.createElement('article');card.className='v5-mobile-row';card.setAttribute('role','listitem');
    const title=document.createElement('h3');title.textContent=strategy;card.append(title);
    const primary=document.createElement('dl');primary.className='v5-mobile-primary';
    for(const [label,value,className] of [['Net return',returnValue,m.net_return_pct>0?'positive':m.net_return_pct<0?'negative':''],['Max drawdown',drawdown,''],['Mean exposure',v5pct(m.mean_capital_exposure_pct),'']]){
      const itemNode=document.createElement('div'),dtNode=document.createElement('dt'),ddNode=document.createElement('dd');dtNode.textContent=label;ddNode.textContent=value;if(className)ddNode.className=className;itemNode.append(dtNode,ddNode);primary.append(itemNode);
    }
    card.append(primary);
    const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Time active, max exposure, turnover, costs and halts';details.append(summary);
    const extra=document.createElement('dl');extra.className='v5-mobile-extra';
    for(const [label,value] of cells.slice(4)){const itemNode=document.createElement('div'),dtNode=document.createElement('dt'),ddNode=document.createElement('dd');dtNode.textContent=label;ddNode.textContent=value;itemNode.append(dtNode,ddNode);extra.append(itemNode);}
    details.append(extra);card.append(details);mobileRows.push(card);
  }
  if(rows.length)$('v5-cell-body').replaceChildren(...rows);else emptyRow($('v5-cell-body'),9,'No result cell matches these filters.');
  if(mobileRows.length)mobile.replaceChildren(...mobileRows);else {const empty=document.createElement('p');empty.className='v5-mobile-empty';empty.textContent='No result cell matches these filters.';mobile.replaceChildren(empty);}
  text('v5-pair-count',state.paired_comparisons.length + ' total · 12 in selected view');
  renderV5Provenance(state);
  if($('v5-pair-details').open)renderV5Pairs(state);
}
function renderV5Provenance(state) {
  const info=[
    ['Study ID',state.study_id],['Status','Complete · exploratory reuse of inspected history'],
    ['Result SHA-256',state.result_sha256],['Result created UTC',state.created_at_utc],
    ['First score started UTC',state.first_score_started_at_utc],['Protocol ID',state.protocol_id],
    ['Protocol SHA-256',state.protocol_sha256],['Historical test fresh','No · every period was already examined in V4'],
    ['BTCUSDT canonical data SHA-256',state.dataset_sha256?.BTCUSDT],
    ['ETHUSDT canonical data SHA-256',state.dataset_sha256?.ETHUSDT],
    ['Frozen V4 source study',state.v4_reference?.study_id],
    ['Frozen V4 result SHA-256',state.v4_reference?.result_sha256],
    ['Score resources',state.resources?state.resources.process_cpu_seconds+'s CPU · '+state.resources.peak_rss_mb+' MiB peak · '+state.resources.wall_seconds+'s wall':null],
    ['Result source and license',state.source_attribution],
    ['Time-active audit',state.time_in_market_audit?.method],
    ['Time-active cells corrected',state.time_in_market_audit?`${state.time_in_market_audit.affected_cells}/${state.time_in_market_audit.audited_cells} · raw result SHA retained`:null],
    ['Audit SHA-256',state.time_in_market_audit?.audit_sha256]
  ];
  const nodes=info.map(([name,value])=>{const dtNode=document.createElement('dt'),ddNode=document.createElement('dd');dtNode.textContent=name;ddNode.textContent=value==null?'—':String(value);return [dtNode,ddNode];}).flat();
  $('v5-provenance-list').replaceChildren(...nodes);
}
function renderV5Pairs(state) {
  const symbol=$('v5-symbol').value,fold=$('v5-fold').value,cost=$('v5-cost').value;
  const matches=state.paired_comparisons.filter(x=>x.symbol===symbol&&x.fold===fold&&x.cost_scenario===cost),rows=[];
  for(const item of matches) {
    const row=document.createElement('tr');
    cell(row,v5CandidateLabels[item.candidate]||item.candidate);
    cell(row,v5CandidateLabels[item.baseline]||item.baseline);
    cell(row,v5pct(item.return_difference_percentage_points),item.return_difference_percentage_points>0?'positive':item.return_difference_percentage_points<0?'negative':'');
    cell(row,v5pct(item.drawdown_difference_percentage_points));
    cell(row,v5pct(item.exposure_difference_percentage_points));
    rows.push(row);
  }
  if(rows.length)$('v5-pair-body').replaceChildren(...rows);else emptyRow($('v5-pair-body'),5,'No paired comparison is available for this view.');
}
$('v5-refresh').addEventListener('click',loadV5);
for(const id of ['v5-symbol','v5-fold','v5-cost'])$(id).addEventListener('change',()=>{if(v5state)renderV5(v5state);});
$('v5-pair-details').addEventListener('toggle',()=>{if($('v5-pair-details').open&&v5state)renderV5Pairs(v5state);});
function fmtUtc(value) {
  if(!value)return '—';
  return new Date(value).toISOString().replace('T',' ').replace('.000Z',' UTC').replace('Z',' UTC');
}
function serverEpochNow() {
  if(!forwardClockAnchor)return null;
  return forwardClockAnchor.epoch+(performance.now()-forwardClockAnchor.performanceAt)/1000;
}
function timerDuration(seconds) {
  const whole=Math.max(0,Math.floor(seconds)),days=Math.floor(whole/86400),hours=Math.floor((whole%86400)/3600),minutes=Math.floor((whole%3600)/60),secs=whole%60;
  const clock=[hours,minutes,secs].map(n=>String(n).padStart(2,'0')).join(':');
  return days?`${days}d ${clock}`:clock;
}
function nextDailyFillEpoch(epoch) {
  return (Math.floor(epoch/86400)+1)*86400+4*3600;
}
function selectedSessionMinutes() {
  const selection=$('forward-duration').value;
  if(selection!=='custom')return Number(selection);
  const raw=$('forward-custom-minutes').value,minutes=Number(raw);
  if(!/^\d+$/.test(raw)||!Number.isInteger(minutes)||minutes<1||minutes>259200)throw new Error('Custom duration must be a whole number from 1 to 259200 minutes.');
  return minutes;
}
async function loadForward() {
  try {
    const response=await fetch('/api/forward/v5',{cache:'no-store'}),state=await response.json();
    if(!response.ok)throw new Error(state.error||'Forward cohort status unavailable.');
    forwardLoaded=true;forwardState=state;forwardClockAnchor={epoch:Number(state.server_now_epoch),performanceAt:performance.now()};$('forward-retry').hidden=true;renderForward(state);
  } catch(error) {
    forwardLoaded=false;forwardState=null;forwardClockAnchor=null;
    text('forward-state','STATUS UNAVAILABLE');
    text('forward-message','The isolated forward cohort status could not be read: ' + error.message);
    text('forward-timing','No cohort action is available until a current local status can be read.');
    for(const id of ['forward-session-options','forward-start','forward-pause','forward-stop','forward-exit','forward-resume','forward-reset','forward-export'])$(id).hidden=true;
    for(const id of ['forward-start','forward-pause','forward-stop','forward-exit','forward-resume','forward-reset'])$(id).disabled=true;
    $('forward-retry').hidden=false;
  }
}
function renderForward(state) {
  const status=state.state||'unavailable',session=state.session_status||'legacy_untimed';
  const labels={not_started:'NOT STARTED · 0 FORWARD DAYS',active:status==='starting'?'SESSION ACTIVE · BASELINE LOADING':'SESSION ACTIVE',paused:'SESSION PAUSED · TIMER CONTINUES',expired_pending_close:'EXPIRED · PENDING CLOSE',stopped_pending_close:'STOPPED · PENDING CLOSE',expired_exit_only:'EXPIRED · EXIT-ONLY',stopped_exit_only:'STOPPED · EXIT-ONLY',finished:'SESSION FINISHED',legacy_untimed:'UNTIMED V5 COHORT · ENTRIES DISABLED',clock_suspect:'CLOCK CHECK FAILED · ENTRIES DISABLED'};
  text('forward-state',labels[session]||session.toUpperCase().replace(/_/g,' '));
  const defaultReason=state.reason||'No research job is running.';
  text('forward-message',state.error||defaultReason);
  text('forward-timing','Baseline '+(state.baseline_utc?.['BTC-USD']||'not set')+' · last paired point '+(state.last_daily_observation_utc?.['BTC-USD']||'not set')+' · next observation '+(state.next_eligible_observation_utc||'after confirmed start')+' · scheduled fill '+(state.next_due_utc||'—')+' · daily entry expiry '+(state.next_expiry_utc||'—'));
  const serverNow=serverEpochNow()??Number(state.server_now_epoch||0),deadline=Number(state.session_deadline_epoch||0),terminal=['expired_pending_close','stopped_pending_close','expired_exit_only','stopped_exit_only','finished','legacy_untimed','clock_suspect'].includes(session);
  text('forward-accepted',state.session_started_at_utc?fmtUtc(state.session_started_at_utc):'—');
  if(deadline){text('forward-deadline',state.session_deadline_utc||'—');const remaining=deadline-(serverNow||0);text('forward-countdown',terminal?(session.includes('pending_close')?'Awaiting fresh close':session.includes('exit_only')?'Exit-only · no new entries':session==='finished'?'Finished':'Entries disabled'):remaining<=0?'Deadline reached · checking server':timerDuration(remaining));}
  else {text('forward-deadline','—');text('forward-countdown',session==='legacy_untimed'?'No approved timer · entries disabled':'Not started');}
  let nextWindow=state.next_due_utc?Date.parse(state.next_due_utc)/1000:nextDailyFillEpoch(serverNow);
  if(!Number.isFinite(nextWindow))nextWindow=nextDailyFillEpoch(serverNow);
  text('forward-next-window',fmtUtc(new Date(nextWindow*1000).toISOString()));
  const actualDays=Number(state.forward_days||0),performance=!!state.is_performance_observation;
  text('forward-disclaimer',performance
    ? actualDays + ' post-start paired daily observation day(s) · ' + (state.totals?.post_start_marks||0) + ' sampled marks · ' + (state.totals?.all_forward_fills||0) + ' simulated fills. USD aggregate sampled-price results only; not venue execution evidence.'
    : 'Not started or no subsequent performance observation · ' + actualDays + ' forward days · starting cash is not a result. Warmup history is input-only.');
  const starting=status==='starting',running=status==='running',paused=status==='paused';
  const canStart=session==='not_started'&&status==='not_started'&&!state.entry_disabled;
  $('forward-session-options').hidden=!canStart;
  $('forward-start').hidden=!canStart;$('forward-start').disabled=forwardBusy||starting;
  $('forward-pause').hidden=!(running&&['active','expired_exit_only','stopped_exit_only'].includes(session));$('forward-pause').disabled=forwardBusy;
  $('forward-stop').hidden=!['active','paused'].includes(session);$('forward-stop').disabled=forwardBusy;
  const policy=state.expiry_policy||'CLOSE';
  $('forward-stop').textContent=policy==='CLOSE'?'Stop · close at fresh quote':'Stop · continue exit-only';
  $('forward-stop').title=policy==='CLOSE'?'Closes the entry window and requests fresh-quote liquidation.':'Closes the entry window; existing holdings may continue beyond the timer under exit-only management.';
  const hasHoldings=(state.accounts||[]).some(a=>Number(a.qty)>1e-12||a.exit_requested);
  $('forward-exit').hidden=!hasHoldings||['not_started','finished'].includes(session);$('forward-exit').disabled=forwardBusy;
  $('forward-exit').textContent=['expired_pending_close','stopped_pending_close','clock_suspect'].includes(session)?'Retry fresh-quote close':'Close positions now';
  $('forward-resume').hidden=!(paused&&['paused','expired_exit_only','stopped_exit_only'].includes(session));$('forward-resume').disabled=forwardBusy;
  const resetBlocked=hasHoldings||starting||running||status==='exiting';
  $('forward-reset').hidden=!state.cohort_id;$('forward-reset').disabled=forwardBusy||resetBlocked;
  $('forward-reset').title=hasHoldings?'Close paper positions at fresh quotes before archiving this cohort.':'';
  $('forward-export').hidden=!state.cohort_id;
  const accountRows=[];
  const mobileRows=[];
  if(!state.accounts?.length)emptyRow($('forward-account-body'),8,'Not started. No accounts exist and no performance is reported.');
  else for(const a of state.accounts) {
    const row=document.createElement('tr'),accountStatus=a.halted?'Risk halt':a.exit_requested?'Exit pending':session==='active'?'Active':session==='paused'?'Paused':labels[session]||status;
    const fields=[['Asset',a.symbol],['Strategy',v5CandidateLabels[a.strategy]||a.strategy],['Account state',accountStatus],['Net liquidation',performance?usd(a.net_liquidation):'Not observed'],['Return',performance?v5pct(a.return_pct):'—'],['Drawdown',performance?v5pct(a.max_drawdown*100):'—'],['Exposure',performance?v5pct(a.asset_weight_pct):'—'],['Fees + slippage',performance?usd((a.fees||0)+(a.slippage||0)):'—']];
    fields.forEach(([label,value],index)=>{const td=cell(row,value);td.dataset.label=label;td.classList.add('forward-col-'+index);});
    accountRows.push(row);
    const card=document.createElement('article');card.className='forward-mobile-row';card.setAttribute('role','listitem');
    const heading=document.createElement('h3');heading.textContent=a.symbol+' · '+(v5CandidateLabels[a.strategy]||a.strategy);card.append(heading);
    const stateLine=document.createElement('p');stateLine.className='forward-account-state';stateLine.textContent=accountStatus;card.append(stateLine);
    const metrics=document.createElement('dl');metrics.className='forward-mobile-primary';
    for(const [label,value,className] of [['Return',fields[4][1],a.return_pct>0?'positive':a.return_pct<0?'negative':''],['Drawdown',fields[5][1],''],['Exposure',fields[6][1],'']]){const div=document.createElement('div'),dtNode=document.createElement('dt'),ddNode=document.createElement('dd');dtNode.textContent=label;ddNode.textContent=value;if(className)ddNode.className=className;div.append(dtNode,ddNode);metrics.append(div);}
    card.append(metrics);
    const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Balance and modeled costs';details.append(summary);
    const extra=document.createElement('dl');extra.className='forward-mobile-extra';
    for(const [label,value] of [fields[3],fields[7]]){const div=document.createElement('div'),dtNode=document.createElement('dt'),ddNode=document.createElement('dd');dtNode.textContent=label;ddNode.textContent=value;div.append(dtNode,ddNode);extra.append(div);}
    details.append(extra);card.append(details);mobileRows.push(card);
  }
  if(accountRows.length)$('forward-account-body').replaceChildren(...accountRows);
  if(mobileRows.length)$('forward-mobile-accounts').replaceChildren(...mobileRows);
  else {const empty=document.createElement('p');empty.className='forward-mobile-empty';empty.textContent='Not started. No accounts exist and no performance is reported.';$('forward-mobile-accounts').replaceChildren(empty);}
  const decisions=state.decisions||[],fills=state.fills||[];
  text('forward-ledger-count',(decisions.length+fills.length) + ' recent records · full archived cohort export');
  const records=[
    ...decisions.map(x=>({kind:'Decision',symbol:x.symbol,strategy:x.strategy,utc:x.created_at,status:x.status,target:x.target_weight,qty:null,reason:x.reason})),
    ...fills.map(x=>({kind:'Fill',symbol:x.symbol,strategy:x.strategy,utc:x.receipt_at,status:x.side,target:null,qty:x.qty,reason:x.reason}))
  ].sort((a,b)=>Date.parse(b.utc||'')-Date.parse(a.utc||'')).slice(0,60);
  const ledgerRows=records.map(x=>{const row=document.createElement('tr');cell(row,x.kind);cell(row,x.symbol+' · '+(v5CandidateLabels[x.strategy]||x.strategy));cell(row,fmtUtc(x.utc));cell(row,x.status);cell(row,x.target==null?(x.qty==null?'—':x.qty.toFixed(6)):(x.target*100).toFixed(2)+'%');cell(row,x.reason);return row;});
  if(ledgerRows.length)$('forward-ledger-body').replaceChildren(...ledgerRows);else emptyRow($('forward-ledger-body'),6,'No forward decisions or fills have been recorded.');
  $('forward-state').classList.toggle('negative',!!state.error);
}
async function forwardAction(actionName,confirmValue,extra={}) {
  if(forwardBusy)return;
  forwardBusy=true;
  try {
    const response=await fetch('/api/forward/v5',{method:'POST',headers:{'Content-Type':'application/json','X-Crypto-Lab':'1'},body:JSON.stringify({action:actionName,confirm:confirmValue,...extra})});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'Forward cohort action failed.');
    const messages={start:'Session started. The timer includes warmup; daily signals keep their existing schedule.',resume:'Session resumed with the same deadline and a new observation baseline.',pause:'Paused. Positions remain held and the original deadline continues.',stop:'The entry window is closed; the selected expiry policy now applies.',exit:'Close requested. Entries are disabled; the cohort finishes after all open positions close from fresh quotes.',reset:'Prior cohort archived; a new session can be reviewed.'};
    toast(messages[actionName]||'Forward cohort updated.');await loadForward();
  } catch(error) {toast(error.message,true);await loadForward();}
  finally {forwardBusy=false;if(forwardState)renderForward(forwardState);}
}
function reviewForwardStart() {
  try {
    if(!forwardLoaded||!forwardState)throw new Error('A current local cohort status is required before reviewing a session. Retry the status check first.');
    const minutes=selectedSessionMinutes(),policy=$('forward-expiry-policy').value,now=serverEpochNow()??Date.now()/1000,deadline=now+minutes*60,next=nextDailyFillEpoch(now);
    const windowText=fmtUtc(new Date(next*1000).toISOString());
    const minuteLabel=n=>`${n} minute${n===1?'':'s'}`;
    text('forward-review-duration',minutes<60?minuteLabel(minutes):minutes%1440===0?`${minutes/1440} day${minutes/1440===1?'':'s'}`:minutes%60===0?`${minutes/60} hour${minutes/60===1?'':'s'}`:minuteLabel(minutes));
    text('forward-review-policy',policy==='CLOSE'?'Close holdings from a fresh quote after expiry.':'Exit-only after expiry; hold until signals or risk rules reduce positions.');
    text('forward-review-start',fmtUtc(new Date(now*1000).toISOString()));
    text('forward-review-deadline',fmtUtc(new Date(deadline*1000).toISOString()));
    text('forward-review-window',windowText);
    const warn=deadline<=next?'This session ends at or before the next scheduled 04:00 UTC fill window. Zero signals and zero trades are expected; the timer will not change signal cadence.':'A signal is still conditional on a later completed daily observation arriving before 04:00 UTC; this session may finish with zero trades.';
    text('forward-zero-signal-warning',warn);
    $('forward-start-dialog').dataset.durationMinutes=String(minutes);$('forward-start-dialog').dataset.expiryPolicy=policy;
    $('forward-start-dialog').showModal();
  } catch(error) {toast(error.message,true);if($('forward-duration').value==='custom')$('forward-custom-minutes').focus();}
}
$('forward-start').addEventListener('click',reviewForwardStart);
$('forward-retry').addEventListener('click',loadForward);
$('forward-confirm-start').addEventListener('click',event=>{
  event.preventDefault();const dialog=$('forward-start-dialog');const duration=Number(dialog.dataset.durationMinutes),policy=dialog.dataset.expiryPolicy;
  dialog.close('confirmed');forwardAction('start','START',{duration_minutes:duration,expiry_policy:policy});
});
$('forward-duration').addEventListener('change',()=>{$('forward-custom-label').hidden=$('forward-duration').value!=='custom';});
$('forward-pause').addEventListener('click',()=>forwardAction('pause'));
$('forward-exit').addEventListener('click',()=>forwardAction('exit'));
$('forward-stop').addEventListener('click',()=>forwardAction('stop'));
$('forward-resume').addEventListener('click',()=>forwardAction('resume'));
$('forward-reset').addEventListener('click',()=>$('forward-reset-dialog').showModal());
$('forward-reset-dialog').addEventListener('close',()=>{if($('forward-reset-dialog').returnValue==='reset')forwardAction('reset','RESET');});
function tickForwardTimer() {
  if(!forwardState||!forwardClockAnchor)return;
  const deadline=Number(forwardState.session_deadline_epoch||0),session=forwardState.session_status;
  if(!deadline)return;
  const terminal=['expired_pending_close','stopped_pending_close','expired_exit_only','stopped_exit_only','finished','legacy_untimed','clock_suspect'].includes(session);
  const remaining=deadline-serverEpochNow();
  const value=terminal?(session.includes('pending_close')?'Awaiting fresh close':session.includes('exit_only')?'Exit-only · no new entries':session==='finished'?'Finished':'Entries disabled'):
    remaining<=0?'Deadline reached · server is applying expiry':timerDuration(remaining);
  text('forward-countdown',value);
}
function drawChart(points) {
  curve = points;
  if (!points.length) return;
  const narrow = innerWidth <= 650;
  const svg = $('chart'), left = narrow ? 48 : 58, right = narrow ? 411 : 886, top = 14, bottom = narrow ? 220 : 242, keys = ['model','momentum','buy_hold'];
  chartBox = {width:narrow ? 420 : 900,left,right};
  svg.setAttribute('viewBox',narrow ? '0 0 420 250' : '0 0 900 280');
  const values = points.flatMap(p => keys.map(k => p[k])); let low = Math.min(...values), high = Math.max(...values);
  const padding = Math.max((high - low) * .15, 35); low -= padding; high += padding;
  const x = i => left + i / Math.max(1, points.length - 1) * (right - left), y = n => bottom - (n - low) / (high - low) * (bottom - top);
  const ns = 'http://www.w3.org/2000/svg';
  const node = (name, attrs, value) => { const e = document.createElementNS(ns,name); Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,String(v))); if (value) e.textContent = value; return e; };
  const content = [node('title',{}, `${mode === 'demo' ? 'Synthetic' : 'Public data'} held-out equity comparison`), node('desc',{}, 'All strategies start with 10,000 simulated USD on the same held-out dates. See the comparison table for return, drawdown, exposure and costs.')];
  for (let i=0;i<5;i++) { const yy = top + i / 4 * (bottom-top), amount = high - i / 4 * (high-low); content.push(node('line',{x1:left,x2:right,y1:yy,y2:yy,class:'grid-line'}),node('text',{x:left-10,y:yy+3,'text-anchor':'end',class:'axis-text'},`${(amount/1000).toFixed(2)}k`)); }
  content.push(node('line',{x1:left,x2:right,y1:y(10000),y2:y(10000),stroke:'#59636c','stroke-dasharray':'3 5','stroke-width':.8}));
  for (const [key,color,dash] of [['buy_hold','#697a7c','2 4'],['momentum','#765b35','8 4'],['model','#356e68','']]) content.push(node('path',{d:points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(p[key]).toFixed(2)}`).join(' '),stroke:color,class:'chart-path','stroke-dasharray':dash}));
  for (const i of [0,Math.floor(points.length/2),points.length-1]) content.push(node('text',{x:x(i),y:narrow ? 243 : 268,'text-anchor':i===0?'start':i===points.length-1?'end':'middle',class:'axis-text'},date(points[i].ts,true)));
  svg.replaceChildren(...content);
}
$('chart-wrap').addEventListener('pointermove', event => {
  if (!curve.length) return;
  const rect = $('chart').getBoundingClientRect(), virtual = (event.clientX-rect.left)/rect.width*chartBox.width;
  const index = Math.max(0,Math.min(curve.length-1,Math.round((virtual-chartBox.left)/(chartBox.right-chartBox.left)*(curve.length-1)))); const p = curve[index];
  text('chart-tooltip', `${date(p.ts)} UTC · Model ${usd(p.model)} · Momentum ${usd(p.momentum)} · Hold ${usd(p.buy_hold)}`); $('chart-tooltip').hidden = false;
});
$('chart-wrap').addEventListener('pointerleave', () => $('chart-tooltip').hidden = true);
async function action(name, confirm) {
  if (busy) return;
  busy = true; if (current) render(current);
  try {
    const response = await fetch('/api/control', {method:'POST',headers:{'Content-Type':'application/json','X-Crypto-Lab':'1'},body:JSON.stringify({mode,symbol,action:name,confirm})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Action failed.');
    toast(result.message); await load();
  } catch (error) { toast(error.message,true); }
  finally { busy = false; if (current) render(current); }
}
for (const nextMode of ['live','demo']) $(`mode-${nextMode}`).addEventListener('click', () => { if(mode===nextMode)return; mode=nextMode; current=null; selectContext(); load(true); });
$('symbol').addEventListener('change', () => {symbol=$('symbol').value; current=null; selectContext(); load(true);});
$('try-demo').addEventListener('click', () => {mode='demo';current=null;selectContext();load(true);});
$('refresh').addEventListener('click', () => action('refresh'));
$('start').addEventListener('click', () => action(mode==='demo'?'replay':current?.account.experiment_id?'resume':'start'));
$('pause').addEventListener('click', () => action('pause'));
$('reset').addEventListener('click', () => {text('reset-title', `Reset ${symbol.split('-')[0]} ${mode === 'demo' ? 'synthetic replay' : 'paper account'}?`);$('reset-dialog').showModal();});
$('reset-dialog').addEventListener('close', () => {if($('reset-dialog').returnValue==='reset') action('reset','RESET');});
selectContext(); load(true); loadV5(); loadForward(); loadV4();
addEventListener('resize',()=>{if(curve.length)drawChart(curve);});
setInterval(() => { if (!document.hidden && !busy && !$('reset-dialog').open) load(); },10000);
setInterval(() => { if (!document.hidden) loadV4(); },5000);
setInterval(() => { if (!document.hidden && forwardState && forwardState.session_status!=='not_started' && forwardState.session_status!=='finished') loadForward(); },15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadForward();});
setInterval(tickForwardTimer,1000);
function clock(){text('session-clock',new Date().toISOString().slice(11,19));} clock();setInterval(clock,1000);
