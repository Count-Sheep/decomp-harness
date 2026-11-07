// Pure-helper and rendering tests for web/app.js. Run with: node --test dashboard/tests/test_app.cjs
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function element(tag) {
  return {tag, dataset: {}, children: [], listeners: {}, attributes: {}, classList: {add() {}},
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, listener) { this.listeners[name] = listener; }, dispatch(name) { this.listeners[name]?.(); }};
}
/** Load app.js in a sandbox with a minimal DOM; `ids` become querySelector-able roots. */
function load(ids = [], extra = {}) {
  const roots = new Map(ids.map((id) => [id, element('div')]));
  const document = {createElement: element, querySelector: (id) => roots.get(id) || null, addEventListener() {},
    hidden: false, body: {dataset: {}}, ...extra.document};
  const scope = {Intl, Date, URL, URLSearchParams, setTimeout, clearTimeout, setInterval() {}, document, DashboardNoBoot: true, ...extra.scope};
  vm.runInNewContext(SOURCE, scope);
  return {app: scope.DashboardApp, roots, scope, document};
}
const text = (item) => typeof item === 'string' ? item : [item.textContent || '', ...(item.children || []).map(text)].join(' ');
const flat = (item) => text(item).replace(/\s+/g, ' ');

test('loads without a DOM or config and exports its helpers to node', () => {
  const app = require('../web/app.js');
  assert.equal(typeof app.farmHealth, 'function');
  assert.equal(app.matchClass(100), 'match-exact');
});

test('match thresholds use unrounded percentages', () => {
  const {app} = load();
  assert.deepEqual([100, 99.999, 95, 94.999, 0].map(app.matchClass), ['match-exact', 'match-near', 'match-near', 'match-low', 'match-low']);
});

test('deltas distinguish absent values, zero, regressions and tiny gains', () => {
  const {app} = load();
  assert.equal(app.deltaBadge(null, 'pp').textContent, 'No prior update');
  assert.equal(app.deltaBadge(0, 'pp').textContent, '0.00 pp');
  assert.equal(app.deltaBadge(0.001234, 'pp').textContent, '+0.0012 pp');
  assert.match(app.deltaBadge(-2, 'functions').className, /negative/);
});

test('measured values treat missing as unknown, not zero', () => {
  const {app} = load();
  assert.equal(app.measuredNumber(undefined), null);
  assert.equal(app.measuredNumber(''), null);
  assert.equal(app.measuredNumber('12.5'), 12.5);
  assert.equal(app.measuredPercent({matched_code: 25, total_code: 100}, 'matched_code_percent', 'matched_code', 'total_code'), 25);
  assert.equal(app.measuredPercent({matched_code: 25, total_code: 0}, 'x', 'matched_code', 'total_code'), null);
  assert.equal(app.m2cScored({counts: {}}), null);
  assert.equal(app.m2cScored({counts: {non_exact: 4, scorer_exact: 2}}), 6);
  assert.equal(app.precisePercent(0.001), '0.0010%');
});

test('tables keep three rows visible, roll up the rest and remember the rollup', () => {
  const {app} = load();
  const rows = [['a', '1'], ['b', '2'], ['c', '3'], ['d', '4'], ['e', '5']];
  const result = app.m2cTable(['Function', 'Score'], rows, {numericColumns: [1], rollupKey: 'k'});
  const table = result.children[0].children[0];
  assert.equal(table.children[1].children.length, 3);
  assert.equal(table.children[0].children[0].children[1].className, 'num');
  const details = result.children[1];
  assert.equal(details.children[0].textContent, '2 more rows');
  details.open = true; details.dispatch('toggle');
  assert.equal(app.m2cTable(['Function', 'Score'], rows, {rollupKey: 'k'}).children[1].open, true);
});

test('promotions show unique units, milestones, evidence and unavailable reasons', () => {
  const {app} = load();
  const root = element('section');
  app.promotions(root, {records: [{unit: 'dup'}, {unit: 'dup'}], units: [{
    unit: 'main/unit_a', state: 'link_verified', ready_to_merge: false, artifact: '/current.json', record_sha256: 'abc',
    evidence_records: ['/compile.json', '/current.json'],
    milestones: {function_exact: true, unit_complete: true, object_verified: true, link_verified: true, proposal_build_verified: false}}]});
  assert.equal(root.children[0].textContent, 'Unit promotion checks · 0 promoted');
  const rows = root.children[1].children[0].children[1].children;
  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0].children.slice(0, 8).map((cell) => cell.textContent),
    ['main/unit_a', 'link verified', 'Verified', 'Verified', 'Verified', 'Verified', 'Not verified', 'Not ready']);
  assert.equal(rows[0].children[8].children[0].children[0].textContent, '2 records');
  app.promotions(root, {available: false, reason: 'Provider offline'});
  assert.equal(root.hidden, false);
  assert.equal(root.children[1].textContent, 'Provider offline');
  app.promotions(root, {units: []});
  assert.equal(root.hidden, true);
  app.promotions(root, {units: [{unit: 'x', state: 'promoted'}, {unit: 'y', ready_to_merge: true}]});
  assert.equal(root.children[0].textContent, 'Unit promotion checks · 1 promoted');
});

test('telemetry renders harness workers, executors, m2c cards and the last four attempts', () => {
  const {app, roots} = load(['#shared-worker', '#shared-responses', '#worker-updated', '#executor-status', '#m2c-project-overview']);
  app.telemetry({
    active: [{project: 'proj', script: 'm2c_pipeline_runner.py', pid: 10, phase: 'validation', symbol: 'fn_c', elapsed_seconds: 125}],
    executors: [{executor: 'http://127.0.0.1:18766', status: 'ok', slots: 4, latency_ms: 3}, {executor: 'http://127.0.0.1:18767', status: 'unreachable', error: 'refused'}],
    m2c_projects: [{project: 'proj', name: 'Project', version: 'TEST01', available: true,
      active: {stage: 'promotion', processed: 25, total: 100, unit_gate: {ready_units: 4, owner_trials_run: 2}},
      latest: {clean: 80, total: 100, compiled: 70, validation_total: 100, scored: 60, scorer_exact: 5, promoted: 2}},
      {project: 'other', name: 'Other', version: 'TEST01', available: false, active: null, latest: null}],
    recent: Array.from({length: 6}, (_, i) => ({project: 'proj', symbol: `fn_${i}`, attempt: i, delta: i, at: `2026-01-01T00:00:0${i}Z`}))});
  const worker = roots.get('#shared-worker').children[0];
  assert.equal(worker.children[0].textContent, 'proj');
  assert.equal(worker.children[1].textContent, 'fn_c');
  assert.equal(worker.children[2].textContent, 'Validation · m2c_pipeline_runner.py · 2m');
  const chips = roots.get('#executor-status').children;
  assert.equal(chips.length, 2);
  assert.match(chips[0].className, /tone-good/);
  assert.match(chips[1].className, /tone-bad/);
  assert.equal(chips[1].title, 'refused');
  const cards = roots.get('#m2c-project-overview').children;
  assert.equal(cards[0].href, '/?project=proj&version=TEST01#m2c');
  assert.equal(cards[0].children[1].textContent, 'Promotion 25/100');
  assert.equal(cards[1].children[1].textContent, 'No m2c report');
  const badges = roots.get('#shared-responses').children.slice(1);
  assert.deepEqual(badges.map((badge) => badge.children[1].textContent), ['fn_5', 'fn_4', 'fn_3', 'fn_2']);
  assert.ok(badges.every((badge) => !badge.className.includes('is-new')));
  app.telemetry({active: [], recent: [{project: 'proj', symbol: 'fn_new', attempt: 0, delta: -1, at: '2026-01-02T00:00:00Z'}]});
  const fresh = roots.get('#shared-responses').children[1];
  assert.match(fresh.className, /attempt-negative.*is-new/);
  assert.equal(fresh.children[0].textContent, 'm2c');
});

test('idle, loading and unobservable telemetry are distinguished', () => {
  const {app, roots} = load(['#shared-worker', '#shared-responses']);
  app.telemetry({active: [], recent: [], _cache: {status: 'loading'}});
  assert.equal(roots.get('#shared-worker').children[0].textContent, 'Loading activity');
  app.telemetry({active: [], recent: [], available: false});
  assert.equal(roots.get('#shared-worker').children[0].textContent, 'Telemetry unavailable');
  app.telemetry({active: [], recent: [], processes_available: false});
  assert.equal(roots.get('#shared-worker').children[0].textContent, 'Processes not observable on this host');
  app.telemetry({active: [], recent: []});
  assert.equal(roots.get('#shared-worker').children[0].textContent, 'Idle');
});

test('promotion-loop progress, stalls and visible-units text', () => {
  const {app} = load();
  assert.equal(app.gateProgress(null), null);
  const gate = {ready_units: 105, owner_trials_run: 77, promoted: 2, candidate_banked: 9, candidate_linked: 7, gates_run: 3};
  const card = app.gateProgress(gate, true);
  assert.equal(card.children[0].textContent, 'Owner trials 77/105');
  assert.equal(card.children[1].value, 77);
  assert.equal(app.gateProgress(gate).children[0].textContent, '77 / 105 owner trials · 2 units promoted');
  const now = Date.parse('2026-01-01T02:40:00Z');
  assert.equal(app.stallNotice({updated_at: '2026-01-01T02:35:00Z'}, true, now), null);
  assert.equal(app.stallNotice({updated_at: '2026-01-01T01:36:28Z'}, true, now).textContent, 'No checkpoint for 1h 3m');
  assert.equal(app.stallNotice({updated_at: '2026-01-01T01:36:28Z', heartbeat_at: '2026-01-01T02:39:00Z'}, true, now), null);
  assert.equal(app.visibleUnitsText({state: 'promoted', units: 2, functions: 1174, commit: 'e5d74789c9b6'}),
    'Visible units: 1,174 functions in 2 NonMatching units · commit e5d7478');
  assert.equal(app.visibleUnitsText({state: 'rejected', error: 'hash mismatch'}), 'Visible units rejected: hash mismatch');
  const live = app.visibleProgress({units_considered: 20, processed_units: 12, verified_units: 9, verified_functions: 40, phase: 'full_build'});
  assert.equal(live.children[0].textContent, '12 / 20 units checked · 9 verified · 40 functions');
  assert.equal(live.children[2].textContent, 'Step: full build');
});

test('refinement progress shows inner variant scores and completed functions', () => {
  const {app} = load();
  const panel = app.refinementProgress({refinement_progress: {
    current: {symbol: 'fn_cur', baseline_pct: 99.5, best_pct: 99.75, delta_pp: 0.25, variants_tested: 12, variant_budget: 48,
      compiled_attempts: 13, improved_attempts: 2, search_depth: 2, max_depth: 3, latest_variant: {detail: 'swap operands', targeted: true},
      mismatch_counts: {register: 2}, source_lines: [17]},
    recent_completed: [{symbol: 'fn_done', best_pct: 100, delta_pp: 0.5, object_exact: true}]}});
  assert.deepEqual(panel.children[0].children.map((item) => item.children[1].textContent),
    ['99.50000%', '99.75000%', '+0.25000 pp', '12 / 48', '13', '2']);
  assert.match(panel.children[2].textContent, /target source line.*depth 2\/3/);
  assert.match(panel.children[3].textContent, /2 register.*C lines 17/);
  assert.match(panel.children[4].children[1].textContent, /fn_done.*100\.00000%.*exact/);
});

test('permuter farm health, verdicts and session metrics', () => {
  const {app} = load();
  const now = Date.parse('2026-01-01T05:00:00Z');
  const healthy = {reachable: true, polled_at: '2026-01-01T04:58:00Z', farm: {alive: true, ts: '2026-01-01T04:57:00Z'}};
  assert.equal(app.farmHealth(healthy, now).ok, true);
  assert.match(app.farmHealth({...healthy, farm: {alive: true, ts: '2026-01-01T04:40:00Z'}}, now).warnings[0], /heartbeat is 18m behind/);
  assert.match(app.farmHealth({...healthy, reachable: false, error: 'timeout'}, now).warnings[0], /unreachable: timeout/);
  assert.match(app.farmHealth({...healthy, polled_at: '2026-01-01T04:30:00Z', farm: {alive: true, ts: '2026-01-01T04:30:00Z'}}, now).warnings[0], /poller last ran 30m ago/);
  assert.equal(app.farmHealth({...healthy, farm: {alive: false, ts: healthy.farm.ts}}, now).ok, false);
  assert.equal(app.farmVerdict(undefined), 'Not recorded');
  assert.equal(app.farmVerdict('exact: 584 bytes, 12 relocations'), 'Exact (584 bytes, 12 relocations)');
  assert.equal(app.farmVerdict('exact_code_pool_names: @1 vs lbl_2'), 'Exact code; pool name differs (@1 vs lbl_2)');
  assert.deepEqual(Array.from(app.farmSearchMetrics({})), ['—', '—', '—']);
  const cells = Array.from(app.farmSearchMetrics({search_metrics: {scope: 'worker_session', unique_sources: 70, unique_sources_exact: true,
    memory_cache_hits: 20, disk_cache_hits: 10, cache_misses: 70, session_started: 100, updated: 220, last_improvement: null}}));
  assert.deepEqual(cells.slice(0, 2), ['70', '30 (30.0%)']);
  assert.match(cells[2], /^None in 2m/);
  const saturated = Array.from(app.farmSearchMetrics({search_metrics: {scope: 'worker_session', unique_sources: 250000,
    unique_sources_exact: false, session_started: 100, updated: 280, last_improvement: 220, stale: true}}));
  assert.equal(saturated[0], '≥250,000');
  assert.match(saturated[2], /^1m.*\(stale\)$/);
});

test('farm card and detail report the current run with lifetime figures labelled', () => {
  const {app, roots} = load(['#permuter-farm-card', '#permuter-farm'], {document: {body: {dataset: {project: 'proj'}}}});
  const iso = new Date().toISOString();
  const win = (fn, ts, inRun) => ({fn, ts, in_run: inRun, verify: 'exact: 4 bytes'});
  app.renderFarm({project: 'proj', reachable: true, polled_at: iso, farm: {alive: true, ts: iso, workers: 2, target_workers: 2, wins: 3},
    counts: {active: 1, queued: 1}, improved_units: 5, active_iterations: 10,
    run: {pid: 7, started_at: '2026-01-01T17:24:59Z', improved: 1, units_searched: 1, iterations: 1234, nearwins: [], rejected: [], matched_upstream: ['fn_old']},
    units: [{fn: 'fn_a', state: 'active', base_score: 9, best_score: 1, project_pct: 99.5, run: {sessions: 1, start_score: 50, best_score: 40, iterations: 1234, reseeded_in_run: true}},
      {fn: 'fn_b', state: 'queued', base_score: 30, best_score: 20, pct: 97, run: {sessions: 0, start_score: null, best_score: null, iterations: 0}}],
    wins: [win('fn_old', '2026-01-01T09:00:00Z', false), win('fn_new', '2026-01-01T18:00:00Z', true)], nearwins: ['fn_x']});
  const card = flat(roots.get('#permuter-farm-card')), detail = flat(roots.get('#permuter-farm'));
  assert.equal(roots.get('#permuter-farm-card').href, '#permuter-farm');
  assert.match(card, /\b1 win\b/);
  assert.match(card, /1,234|1\.2K/i);
  assert.match(detail, /Wins this run/);
  assert.match(detail, /fn_old \(earlier run\)/);
  assert.match(detail, /fn_a \(reseeded\).*50 → 40 \(-10\).*99\.50%/);
  assert.match(detail, /fn_b queued not run yet 97\.00%/);
  assert.match(detail, /Near wins this run: none · 1 all-time/);
  assert.match(detail, /Already 100% in the project, still in the farm manifest: fn_old/);
  app.renderFarm({project: 'elsewhere', farm: {}});
  assert.equal(roots.get('#permuter-farm').hidden, true);
  assert.equal(roots.get('#permuter-farm-card').href, '/?project=elsewhere#permuter-farm');
});

test('farm heartbeat without a poller shape still renders', () => {
  const {app, roots} = load(['#permuter-farm-card', '#permuter-farm']);
  app.renderFarm({kind: 'permuter_farm', source: 'farm_heartbeat', reachable: true, polled_at: new Date().toISOString(),
    farm: {alive: true, ts: new Date().toISOString(), workers: 1, target_workers: 4, wins: 0}, counts: {active: 1},
    units: [{fn: 'fn_a', state: 'active', base_score: 90, best_score: 40, elapsed_seconds: 5}], wins: []});
  assert.match(flat(roots.get('#permuter-farm-card')), /1\/4 workers/);
  assert.match(flat(roots.get('#permuter-farm')), /fn_a active 90 → 40 \(-50\)/);
});

test('history series keep only measured percentages', () => {
  const {app} = load();
  const series = app.historySeries([{at: '2026-01-01', fuzzy_match_percent: 50, matched_code: 10, total_code: 100}, {at: '2026-01-02', fuzzy_match_percent: 60}]);
  assert.deepEqual([...series.map((item) => item.key)], ['fuzzy', 'code']);
  assert.deepEqual([...series[1].values.map((row) => row.value)], [10, null]);
});

test('poll revalidates with the last ETag, backs off after failures and pauses while hidden', async () => {
  const timers = [], requests = [], listeners = {};
  const responses = [{status: 200, headers: {ETag: '"1"'}, body: {n: 1}}, {status: 304}, new Error('down'), {status: 200, headers: {ETag: '"2"'}, body: {n: 2}}];
  const {app, document} = load([], {document: {addEventListener: (name, fn) => { listeners[name] = fn; }},
    scope: {AbortController, setTimeout: (fn, delay) => { timers.push({fn, delay}); return timers.length; }, clearTimeout() {},
      fetch: async (url, options) => {
        requests.push(options.headers['If-None-Match']);
        const next = responses.shift();
        if (next instanceof Error) throw next;
        return {status: next.status, ok: next.status < 400, headers: {get: (name) => next.headers?.[name] ?? null}, json: async () => next.body};
      }}});
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  const seen = [], unchanged = [], errors = [];
  app.poll('/api/x', {onData: (data) => seen.push(data.n), onUnchanged: () => unchanged.push(1), onError: (error) => errors.push(error.message)}, {interval: 500});
  await settle();
  const next = () => timers.filter((timer) => timer.delay !== 30000).pop();
  next().fn(); await settle();
  assert.equal(next().delay, 500);
  next().fn(); await settle();
  assert.equal(next().delay, 1000);
  next().fn(); await settle();
  assert.deepEqual(seen, [1, 2]);
  assert.equal(unchanged.length, 1);
  assert.deepEqual(errors, ['down']);
  assert.deepEqual(requests, [undefined, '"1"', '"1"', undefined]);
  document.hidden = true;
  const count = requests.length;
  next().fn(); await settle();
  assert.equal(requests.length, count);
  document.hidden = false;
  listeners.visibilitychange(); await settle();
  assert.equal(requests.length, count + 1);
});
