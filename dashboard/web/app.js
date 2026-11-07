/* Decomp dashboard: report progress, m2c sweeps, unit promotions, draft attempts, workers and the permuter farm.
 * One file: pure helpers (exported for node tests) plus the page boot, which runs only in a browser that
 * received window.DashboardConfig from the server. All data is rendered with textContent, never as HTML. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.DashboardApp = api;
  if (root.document && root.DashboardConfig && !root.DashboardNoBoot) api.boot();
})(typeof window !== 'undefined' ? window : globalThis, function (root) {
  'use strict';
  const number = new Intl.NumberFormat();
  const doc = () => root.document;
  const $ = (selector) => doc().querySelector(selector);
  const openRollups = new Set();
  let telemetryAttemptKeys = [];

  // ── Small helpers ─────────────────────────────────────────────────────────────────────────
  function node(tag, className, text) {
    const element = doc().createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function date(value) { return value ? new Date(value).toLocaleString() : 'not recorded'; }
  function measuredNumber(value) {
    return (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '')) && Number.isFinite(Number(value)) ? Number(value) : null;
  }
  function measuredPercent(measures, key, count, total) {
    const value = measuredNumber(measures?.[key]);
    if (value !== null) return value;
    const numerator = measuredNumber(measures?.[count]), denominator = measuredNumber(measures?.[total]);
    return numerator !== null && denominator > 0 ? 100 * numerator / denominator : null;
  }
  function precisePercent(value) {
    const percent = Number(value || 0);
    return `${percent === 0 ? '0.00' : percent < 0.01 ? percent.toFixed(4) : percent < 1 ? percent.toFixed(3) : percent.toFixed(2)}%`;
  }
  function matchClass(value) { return Number(value) >= 100 ? 'match-exact' : Number(value) >= 95 ? 'match-near' : 'match-low'; }
  function duration(seconds) {
    const minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
    return hours ? `${hours}h ${minutes % 60}m` : minutes ? `${minutes}m` : `${Math.floor(seconds)}s`;
  }
  function deltaBadge(value, unit = '') {
    const numeric = typeof value === 'number' && Number.isFinite(value);
    const badge = node('small', `metric-delta ${numeric ? value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral' : 'unknown'}`);
    const amount = !numeric ? '' : unit === 'pp' ? (value !== 0 && Math.abs(value) < 0.01 ? Number(value.toPrecision(2)).toString() : value.toFixed(2)) : number.format(value);
    badge.textContent = numeric ? `${value > 0 ? '+' : ''}${amount}${unit ? ` ${unit}` : ''}` : 'No prior update';
    badge.title = 'Change from the previous recorded data update';
    return badge;
  }
  function metric(label, value, detail, delta, unit) {
    const element = node('article', 'metric');
    element.dataset.metric = String(label).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    element.append(node('p', '', label), node('strong', '', value), node('span', '', detail || ''));
    if (arguments.length >= 4) element.append(deltaBadge(delta, unit));
    return element;
  }
  /** A table showing three rows, with the rest in a remembered-open rollup. */
  function m2cTable(headers, rows, options = {}) {
    const numeric = new Set(options.numericColumns || []);
    const makeTable = (tableRows) => {
      const table = node('table', `m2c-table${headers.length > 2 ? ' m2c-wide-table' : ''}${options.className ? ` ${options.className}` : ''}`);
      const head = node('thead'), heading = node('tr'), body = node('tbody');
      headers.forEach((label, index) => heading.append(node('th', numeric.has(index) ? 'num' : '', label)));
      head.append(heading);
      tableRows.forEach((values) => {
        const row = node('tr');
        values.forEach((value, index) => {
          const cell = node('td', numeric.has(index) ? 'num' : '');
          if (value && typeof value === 'object' && (value.nodeType || typeof value.append === 'function')) cell.append(value);
          else cell.textContent = String(value ?? '-');
          row.append(cell);
        });
        body.append(row);
      });
      table.append(head, body);
      return table;
    };
    const wrap = node('div', 'm2c-table-wrap');
    wrap.tabIndex = 0;
    wrap.append(makeTable(rows.slice(0, 3)));
    if (rows.length <= 3 || options.rollup === false) return wrap;
    const details = node('details', 'm2c-rollup');
    const key = options.rollupKey || headers.join('\u001f');
    details.dataset.rollupKey = key;
    details.open = openRollups.has(key);
    details.addEventListener('toggle', () => { if (details.open) openRollups.add(key); else openRollups.delete(key); });
    const more = node('div', 'm2c-table-wrap');
    more.tabIndex = 0;
    more.append(makeTable(rows.slice(3)));
    details.append(node('summary', '', `${number.format(rows.length - 3)} more rows`), more);
    const group = node('div', 'm2c-table-group');
    group.append(wrap, details);
    return group;
  }

  // ── Run progress (live m2c runs and the promotion loop) ───────────────────────────────────
  const STALL_MINUTES = 10;
  /** Seconds since an active run last wrote a checkpoint, or null when it has none. */
  function checkpointAge(run, now = Date.now()) {
    const at = Date.parse(run?.heartbeat_at || run?.updated_at || '');
    return Number.isFinite(at) ? Math.max(0, (now - at) / 1000) : null;
  }
  /** Warning for a run that is alive but has not checkpointed recently, or null. */
  function stallNotice(run, compact = false, now = Date.now()) {
    const age = checkpointAge(run, now);
    if (age === null || age < STALL_MINUTES * 60) return null;
    const text = compact ? `No checkpoint for ${duration(age)}`
      : `No checkpoint for ${duration(age)}. The runner process is alive, but this stage has not recorded progress since ${new Date(Date.parse(run.heartbeat_at || run.updated_at)).toLocaleTimeString()}; the bar above reflects the last checkpoint, not current work.`;
    return node(compact ? 'span' : 'p', compact ? 'worker-card-stall' : 'm2c-stall', text);
  }
  /** One-line outcome of the gate's visible-units step. */
  function visibleUnitsText(visible) {
    if (!visible || typeof visible !== 'object') return null;
    const counts = `${number.format(visible.functions || 0)} functions in ${number.format(visible.units || 0)} NonMatching units`;
    if (visible.state === 'promoted') return `Visible units: ${counts}${visible.commit ? ` · commit ${String(visible.commit).slice(0, 7)}` : ''}`;
    if (visible.state === 'nothing_to_promote') return 'Visible units: nothing to promote';
    if (visible.state === 'rejected') return `Visible units rejected${visible.error ? `: ${visible.error}` : ''}`;
    return `Visible units: ${counts}`;
  }
  function visibleProgress(progress) {
    if (!progress || typeof progress !== 'object') return null;
    const element = node('div', 'gate-progress');
    const total = Number(progress.units_considered) || 0, done = Number(progress.processed_units) || 0;
    element.append(node('p', 'm2c-live-count', `${number.format(done)}${total ? ` / ${number.format(total)}` : ''} units checked · ${number.format(progress.verified_units || 0)} verified · ${number.format(progress.verified_functions || 0)} functions`));
    const meter = node('progress', 'gate-progress-bar');
    meter.setAttribute('aria-label', 'Visible units checked');
    if (total) { meter.max = total; meter.value = Math.min(done, total); }
    element.append(meter);
    if (progress.phase) element.append(node('p', 'm2c-live-meta', `Step: ${String(progress.phase).replaceAll('_', ' ')}`));
    return element;
  }
  /** Promotion-loop totals for an active run: owner-trial bar plus counters. `compact` is the card form. */
  function gateProgress(gate, compact = false) {
    if (!gate || typeof gate !== 'object') return null;
    const count = (key) => Number(gate[key]) || 0, trials = count('owner_trials_run'), ready = count('ready_units');
    const element = node('div', compact ? 'gate-progress gate-compact' : 'gate-progress');
    const meter = node('progress', 'gate-progress-bar');
    meter.setAttribute('aria-label', 'Incremental owner trials');
    if (ready > 0) { meter.max = ready; meter.value = Math.min(trials, ready); }
    if (compact) {
      element.append(node('span', 'gate-progress-label', `Owner trials ${ready ? `${number.format(trials)}/${number.format(ready)}` : number.format(trials)}`), meter);
      return element;
    }
    element.append(node('p', 'm2c-live-count', `${number.format(trials)}${ready ? ` / ${number.format(ready)}` : ''} owner trials · ${number.format(count('promoted'))} units promoted`), meter,
      node('p', 'm2c-live-meta', `${number.format(count('owner_objects_verified'))} owner objects · ${number.format(count('owner_links_verified'))} owner links · ${number.format(count('gates_run'))} complete-unit gates · ${number.format(count('links_verified'))} links verified`),
      node('p', 'm2c-live-meta', `${number.format(count('candidate_banked'))} functions banked · ${number.format(count('candidate_linked'))} linked · ${number.format(count('candidate_visible'))} visible`));
    const visible = visibleUnitsText(gate.visible_units);
    if (visible) element.append(node('p', 'm2c-live-meta', visible));
    return element;
  }
  function refinementProgress(run) {
    const progress = run?.refinement_progress, current = progress?.current;
    if (!current) return null;
    const element = node('section', 'm2c-refinement-live');
    const score = (value) => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(5)}%` : '-';
    const delta = typeof current.delta_pp === 'number' && Number.isFinite(current.delta_pp)
      ? `${current.delta_pp > 0 ? '+' : ''}${current.delta_pp.toFixed(5)} pp` : '-';
    const facts = node('dl', 'm2c-refinement-facts');
    for (const [label, value] of [['Owner baseline', score(current.baseline_pct)], ['Best', score(current.best_pct)], ['Delta', delta],
      ['Variants', `${number.format(current.variants_tested || 0)} / ${number.format(current.variant_budget || 0)}`],
      ['Compiled', number.format(current.compiled_attempts || 0)], ['Improved', number.format(current.improved_attempts || 0)]]) {
      const fact = node('div');
      fact.append(node('dt', '', label), node('dd', label === 'Delta' && Number(current.delta_pp) > 0 ? 'positive' : '', value));
      facts.append(fact);
    }
    element.append(facts);
    const meter = node('progress', 'm2c-refinement-progress');
    meter.setAttribute('aria-label', `${current.symbol || 'Current function'} source variants tested`);
    if (Number(current.variant_budget) > 0) { meter.max = Number(current.variant_budget); meter.value = Math.min(Number(current.variants_tested || 0), meter.max); }
    element.append(meter);
    if (typeof current.validation_pct === 'number' && typeof current.baseline_pct === 'number' && Math.abs(current.validation_pct - current.baseline_pct) > 0.000001)
      element.append(node('p', 'm2c-refinement-detail', `Validation score ${score(current.validation_pct)} · normalized to the full-owner baseline above`));
    const variant = current.latest_variant || current.best_variant;
    if (variant) {
      const location = variant.targeted ? 'target source line' : Number.isFinite(variant.target_distance) ? `${variant.target_distance} lines from mismatch` : 'general rewrite';
      element.append(node('p', 'm2c-refinement-detail', `Latest: ${variant.detail || variant.kind || 'source variant'} · ${location}${current.search_depth ? ` · depth ${current.search_depth}/${current.max_depth || '?'}` : ''}`));
    }
    const mismatch = Object.entries(current.mismatch_counts || {}).sort((a, b) => b[1] - a[1]);
    if (mismatch.length || current.source_lines?.length) {
      const parts = mismatch.map(([kind, count]) => `${number.format(count)} ${kind.replaceAll('_', ' ')}`);
      if (current.source_lines?.length) parts.push(`C lines ${current.source_lines.join(', ')}`);
      element.append(node('p', 'm2c-refinement-detail', `Best residual: ${parts.join(' · ')}`));
    }
    if (progress.recent_completed?.length) {
      const recent = node('div', 'm2c-refinement-recent');
      recent.append(node('span', '', 'Completed'));
      for (const item of progress.recent_completed) {
        const itemDelta = typeof item.delta_pp === 'number' && Number.isFinite(item.delta_pp) ? `${item.delta_pp > 0 ? '+' : ''}${item.delta_pp.toFixed(5)} pp` : 'no measured delta';
        recent.append(node('code', item.object_exact ? 'positive' : '', `${item.symbol || 'unknown'} · ${score(item.best_pct)} · ${itemDelta}${item.object_exact ? ' · exact' : ''}`));
      }
      element.append(recent);
    }
    return element;
  }

  // ── Unit promotions (harness m2c_unit_queue) ──────────────────────────────────────────────
  function promotions(element, report = {}) {
    if (!element) return;
    const opened = new Set([...(element.querySelectorAll?.('details[open]') || [])].map((item) => item.dataset.unit));
    const rows = report?.units || report?.records || [];
    element.replaceChildren();
    element.hidden = !rows.length && report?.available !== false;
    const promoted = Number.isFinite(Number(report?.promoted)) ? Number(report.promoted)
      : rows.filter((row) => row.promoted === true || row.state === 'promoted').length;
    element.append(node('h3', '', `Unit promotion checks · ${number.format(promoted)} promoted`));
    if (report?.available === false) { element.append(node('p', 'muted', report.reason || 'Promotion evidence unavailable')); return; }
    const records = rows.map((row) => {
      const milestones = row.milestones || {};
      const measured = (value) => value === true ? 'Verified' : value === false ? 'Not verified' : 'Unknown';
      const values = [row.unit || 'Unknown', String(row.state || 'unverified').replaceAll('_', ' '),
        measured(milestones.function_exact), measured(milestones.unit_complete),
        measured(milestones.object_verified ?? row.object_verified), measured(milestones.link_verified ?? row.link_verified),
        measured(milestones.proposal_build_verified), row.ready_to_merge === true ? 'Ready' : 'Not ready'];
      const details = node('details', 'promotion-evidence');
      details.dataset.unit = row.unit || '';
      details.open = opened.has(row.unit || '');
      const artifacts = row.evidence_records || (row.artifact ? [row.artifact] : []);
      details.append(node('summary', '', `${artifacts.length} records`));
      if (row.error) details.append(node('p', 'negative', row.error));
      if (row.artifact) details.append(node('p', '', `Current: ${row.artifact}`));
      if (row.record_sha256) details.append(node('p', '', `SHA256: ${row.record_sha256}`));
      const history = node('ul');
      for (const artifact of artifacts) history.append(node('li', '', artifact));
      details.append(history);
      values.push(details);
      return values;
    });
    element.append(m2cTable(['Unit', 'State', 'Function exact', 'Unit complete', 'Object', 'Link', 'Project build', 'Merge', 'Evidence'], records,
      {className: 'unit-promotion-table', rollupKey: 'unit-promotions'}));
  }

  // ── Worker strip (global telemetry) ───────────────────────────────────────────────────────
  const STAGES = {generation: 'Draft generation', validation: 'Validation', refinement: 'Refinement', owner_trial: 'Owner trial',
    asm_backed_trial: 'ASM-backed trial', unit_gate: 'Unit gate', promotion: 'Promotion', candidate_bank: 'Candidate bank',
    visible_units: 'Visible units', finalizing: 'Finalizing reports', pipeline: 'Pipeline'};
  function attemptKey(row) { return [row.project, row.version, row.symbol, row.attempt, row.at || row.updated_at].join(':'); }
  function telemetry(data) {
    const active = data.active || [], recent = data.recent || [];
    const worker = $('#shared-worker');
    worker.replaceChildren();
    if (!active.length) worker.append(node('span', 'worker-idle', data._cache?.status === 'loading' ? 'Loading activity'
      : data.available === false || data._cache?.status === 'error' ? 'Telemetry unavailable'
      : data.processes_available === false ? 'Processes not observable on this host' : 'Idle'));
    for (const item of active) {
      const summary = node('span', 'worker-current');
      summary.title = `${item.script || 'harness'}${item.pid ? ` · pid ${item.pid}` : ''}`;
      summary.append(node('span', 'worker-project', item.project || 'harness'), node('strong', '', item.symbol || item.worker || item.script || 'Worker'),
        node('span', 'worker-detail', [item.attempt != null ? `#${item.attempt}` : '', STAGES[item.phase] || item.phase || 'running',
          item.symbol ? item.script : '', item.elapsed_seconds ? duration(item.elapsed_seconds) : ''].filter(Boolean).join(' · ')));
      worker.append(summary);
    }
    const updated = $('#worker-updated');
    if (updated) updated.textContent = `${data._cache?.error ? 'Stale' : 'Checked'} ${new Date(data._cache?.updated_at || Date.now()).toLocaleTimeString()}`;
    const executors = $('#executor-status');
    if (executors) {
      executors.replaceChildren();
      executors.hidden = !(data.executors || []).length;
      for (const row of data.executors || []) {
        const ok = row.status === 'ok';
        const chip = node('span', `executor-chip ${ok ? 'tone-good' : 'tone-bad'}`);
        chip.title = row.error || [row.worker, row.tool_revision, row.tool_fingerprint].filter(Boolean).join(' · ');
        chip.append(node('b', '', ok ? 'ok' : row.status || 'unknown'), ` m2c ${row.executor}${row.slots ? ` · ${row.slots} slots` : ''}${row.latency_ms != null ? ` · ${row.latency_ms} ms` : ''}`);
        executors.append(chip);
      }
    }
    const overview = $('#m2c-project-overview');
    if (overview) {
      overview.replaceChildren();
      for (const project of data.m2c_projects || []) {
        const card = node('a', 'worker-project-card');
        card.href = `/?project=${encodeURIComponent(project.project)}&version=${encodeURIComponent(project.version || '')}#m2c`;
        const title = node('span', 'worker-card-heading');
        title.append(node('strong', '', project.name || project.project), node('small', '', project.version || ''));
        card.append(title);
        const run = project.active, latest = project.latest;
        if (run) {
          const stage = STAGES[run.stage] || 'Draft generation';
          const processed = Number(run.processed || 0), total = Number(run.total || 0);
          card.append(node('span', 'worker-card-state positive', `${stage}${total ? ` ${number.format(processed)}/${number.format(total)}` : ''}`));
          const meter = node('progress', 'worker-card-progress');
          meter.setAttribute('aria-label', `${project.name || project.project} ${stage} progress`);
          if (total) { meter.max = total; meter.value = Math.min(processed, total); }
          card.append(meter);
          const stall = stallNotice(run, true);
          if (stall) { card.append(stall); meter.classList.add('stalled'); }
          const gate = gateProgress(run.unit_gate, true);
          if (gate) card.append(gate);
          if (run.auto_promote === false) card.append(node('span', 'worker-card-flag', 'Auto-promote off'));
        } else card.append(node('span', 'worker-card-state muted', project.available ? 'No sweep running' : 'No m2c report'));
        const facts = node('dl', 'worker-card-facts');
        const pair = (label, value, total, empty = '—') => {
          const item = node('div');
          item.append(node('dt', '', label), node('dd', '', value == null ? empty : total == null ? number.format(value) : `${number.format(value)}/${number.format(total)}`));
          facts.append(item);
        };
        pair('Clean', latest?.clean, latest?.total);
        pair('Compiled', latest?.compiled, latest?.validation_total);
        pair('Scored', latest?.scored, latest?.validation_total);
        pair('Exact', latest?.scorer_exact, null);
        pair('Promoted', run?.unit_gate ? run.unit_gate.promoted : latest?.promoted, null, 'Not run');
        card.append(facts);
        const visible = visibleUnitsText(latest?.visible);
        if (visible) card.append(node('span', 'worker-card-visible', visible));
        overview.append(card);
      }
      if (!overview.children.length) overview.append(node('span', 'muted', 'm2c progress unavailable'));
    }
    const responses = $('#shared-responses');
    responses.replaceChildren();
    const rows = [...recent].sort((a, b) => String(b.at || b.updated_at || '').localeCompare(String(a.at || a.updated_at || ''))).slice(0, 4);
    responses.append(node('span', 'response-label', 'Last 4'));
    if (!rows.length) responses.append(node('span', 'muted', 'No attempts recorded'));
    for (const row of rows) {
      const delta = Number(row.delta);
      const status = row.delta != null && Number.isFinite(delta) ? delta > 0 ? 'attempt-positive' : delta < 0 ? 'attempt-negative' : 'attempt-neutral' : 'attempt-neutral';
      const isNew = telemetryAttemptKeys.length && !telemetryAttemptKeys.includes(attemptKey(row));
      const badge = node('span', `attempt-badge ${status}${isNew ? ' is-new' : ''}`);
      badge.title = `${row.project || 'worker'}${row.at ? ` · ${new Date(row.at).toLocaleString()}` : ''}`;
      badge.append(node('span', 'attempt-number', row.attempt === 0 ? 'm2c' : `#${row.attempt ?? '-'}`), node('strong', '', row.symbol || 'Unknown function'), deltaBadge(row.delta, 'pp'));
      responses.append(badge);
    }
    telemetryAttemptKeys = rows.map(attemptKey);
  }

  // ── Permuter farm (status file written by the farm or its poller) ─────────────────────────
  // The poller runs every minute, so a poll older than four minutes means it has stopped.
  const FARM_STALL_SECONDS = 600, FARM_POLL_STALE_SECONDS = 240;
  function farmHealth(data, now = Date.now()) {
    const farm = data.farm || {}, warnings = [];
    const polled = Date.parse(data.polled_at || ''), beat = Date.parse(farm.ts || '');
    if (data.reachable === false) warnings.push(`Farm unreachable${data.error ? `: ${data.error}` : ''}; showing the last successful poll`);
    else if (farm.alive === false) warnings.push('Farm reports it is not alive');
    if (Number.isFinite(polled) && Number.isFinite(beat) && polled - beat > FARM_STALL_SECONDS * 1000)
      warnings.push(`Farm heartbeat is ${duration((polled - beat) / 1000)} behind the last poll`);
    if (Number.isFinite(polled) && now - polled > FARM_POLL_STALE_SECONDS * 1000)
      warnings.push(`Farm poller last ran ${duration((now - polled) / 1000)} ago`);
    return {warnings, beatAge: Number.isFinite(beat) ? Math.max(0, (now - beat) / 1000) : null,
      ok: !warnings.length && data.reachable !== false && farm.alive !== false};
  }
  /** The farm's independent byte/relocation verdict for a win. */
  function farmVerdict(verify) {
    if (!verify) return 'Not recorded';
    const text = String(verify);
    if (text.startsWith('exact_code_pool_names')) return `Exact code; pool name differs (${text.replace(/^exact_code_pool_names:?\s*/, '')})`;
    return text.startsWith('exact') ? `Exact${text.replace(/^exact:?/, '') ? ` (${text.replace(/^exact:?\s*/, '')})` : ''}` : text;
  }
  function farmPhase(data, now = Date.now()) {
    const focus = data.focus_campaign || {}, current = focus.current || {};
    const random = focus.random || current.random_refinement;
    if (focus.state === 'random_refinement' || current.search_phase === 'random_refinement') {
      const whole = Array.isArray(random?.whole_slots) ? random.whole_slots.length : 0;
      const targeted = Array.isArray(random?.targeted_slots) ? random.targeted_slots.length : 0;
      const remaining = Number.isFinite(random?.deadline_at) ? Math.max(0, random.deadline_at * 1000 - now) : null;
      const left = remaining === null ? '' : remaining < 60000 ? `${Math.ceil(remaining / 1000)}s` : `${Math.ceil(remaining / 60000)}m`;
      return {label: 'random left', value: left || `${whole}/${targeted}`, detail: `${whole} whole / ${targeted} targeted workers${left ? ` · ${left} left` : ''}`, tone: 'tone-active'};
    }
    const systematic = focus.systematic;
    if (!systematic?.stage_id) return null;
    const workers = `${number.format(systematic.reporting_slots || 0)}/${number.format(systematic.slots || 0)}`;
    return {label: 'stage', value: `${systematic.stage || '—'}/${systematic.stages || '—'} · ${number.format(systematic.completed || 0)}/${number.format(systematic.total || 0)}`,
      detail: `${systematic.stage_id} · ${workers} workers${systematic.next_stage ? `; next ${systematic.next_stage}` : ''}`, tone: 'tone-active'};
  }
  /** Session search counters of a farm unit: unique sources, cache hits and time since the last gain. */
  function farmSearchMetrics(unit) {
    const m = unit.search_metrics;
    if (!m || m.scope !== 'worker_session') return ['—', '—', '—'];
    const hits = (m.memory_cache_hits || 0) + (m.disk_cache_hits || 0), lookups = hits + (m.cache_misses || 0);
    const unique = `${m.unique_sources_exact ? '' : '≥'}${number.format(m.unique_sources || 0)}`;
    const cache = `${number.format(hits)}${lookups ? ` (${(hits / lookups * 100).toFixed(1)}%)` : ''}`;
    const age = Math.max(0, (m.updated || 0) - (m.last_improvement ?? m.session_started ?? 0));
    return [unique, cache, `${m.last_improvement == null ? 'None in ' : ''}${duration(age)}${m.stale ? ' (stale)' : ''}`];
  }
  function farmCard(data, now = Date.now()) {
    const card = $('#permuter-farm-card');
    if (!card) return;
    const farm = data.farm || {}, counts = data.counts || {}, health = farmHealth(data, now);
    const polled = Date.parse(data.polled_at || '');
    card.hidden = false;
    const owner = data.project || root.DashboardConfig?.permuter_farm?.project;
    card.href = owner && owner !== doc().body?.dataset.project ? `/?project=${encodeURIComponent(owner)}#permuter-farm` : '#permuter-farm';
    card.className = `farm-card${health.ok ? '' : ' farm-warn'}`;
    card.title = `Permuter farm${data.host ? ` on ${data.host}` : ''}${data.polled_at ? ` · polled ${new Date(data.polled_at).toLocaleString()}` : ''}`;
    const n = (value) => value == null ? '—' : number.format(value);
    const short = (value) => value == null ? '—' : new Intl.NumberFormat(undefined, {notation: 'compact', maximumFractionDigits: 1}).format(value);
    const status = data.available === false ? 'Status unavailable'
      : `${data.reachable === false ? 'Unreachable' : farm.alive === false ? 'Not alive' : 'Alive'}${health.beatAge === null ? '' : ` · ${duration(health.beatAge)} ago`}`;
    const run = data.run || null, phase = farmPhase(data, now), runWins = run ? (data.wins || []).filter((win) => win.in_run) : (data.wins || []);
    const winCount = run ? runWins.length : farm.wins ?? runWins.length;
    const verified = runWins.filter((win) => String(win.verify || '').startsWith('exact')).length;
    const item = (label, value, className = '') => { const span = node('span', `farm-item ${className}`.trim()); span.append(node('b', '', value), ` ${label}`); return span; };
    const improved = run ? run.improved : data.improved_units;
    card.replaceChildren(
      node('strong', 'farm-name', `Permuter · ${data.host || 'farm'}`),
      node('span', `farm-status ${health.ok ? 'positive' : 'negative'}`, status),
      item('workers', farm.workers == null ? '—' : `${n(farm.workers)}/${n(farm.target_workers)}`,
        farm.workers == null ? 'tone-muted' : farm.workers >= (farm.target_workers ?? farm.workers) ? 'tone-good' : 'tone-warn'),
      item(winCount === 1 ? 'win' : 'wins', n(winCount), winCount ? 'tone-good' : 'tone-muted'),
      item('verified', `${n(verified)}/${n(runWins.length)}`, !runWins.length ? 'tone-muted' : verified === runWins.length ? 'tone-good' : 'tone-warn'),
      item('active', n(counts.active), 'tone-active'), item('queued', n(counts.queued), 'tone-muted'),
      item('improved', n(improved), improved ? 'tone-improved' : 'tone-muted'),
      phase ? item(phase.label, phase.value, phase.tone) : item('iters', short(run ? run.iterations : data.active_iterations), 'tone-muted'),
      run?.started_at ? node('span', 'farm-polled', `run since ${new Date(run.started_at).toLocaleTimeString()}`) : '',
      node('span', 'farm-polled', Number.isFinite(polled) ? `polled ${duration(Math.max(0, (now - polled) / 1000))} ago` : ''),
      ...health.warnings.map((text) => node('span', 'farm-alert', text)));
  }
  function farmDetail(data, now = Date.now()) {
    const element = $('#permuter-farm');
    if (!element) return;
    const project = data.project || root.DashboardConfig?.permuter_farm?.project;
    element.hidden = Boolean(project) && doc().body?.dataset.project !== project;
    if (element.hidden) return;
    const farm = data.farm || {}, counts = data.counts || {}, health = farmHealth(data, now), units = data.units || [];
    const run = data.run || null, phase = farmPhase(data, now), allWins = data.wins || [], runWins = run ? allWins.filter((win) => win.in_run) : allWins;
    const heading = node('div', 'section-heading');
    heading.append(node('h2', '', 'Permuter farm'), node('p', 'muted',
      `${data.host || 'farm'}${data.runtime ? ` · ${data.runtime}` : ''} · polled ${data.polled_at ? new Date(data.polled_at).toLocaleTimeString() : 'never'} · farm heartbeat ${farm.ts ? new Date(farm.ts).toLocaleTimeString() : 'unknown'}`));
    const children = [heading, ...health.warnings.map((text) => node('p', 'm2c-stall', text))];
    const metrics = node('div', 'metrics');
    metrics.append(
      metric('Workers', farm.workers == null ? 'Unknown' : `${number.format(farm.workers)} / ${number.format(farm.target_workers ?? farm.workers)}`, `round ${farm.round ?? '—'} · ${number.format(farm.bad || 0)} bad`),
      metric('Wins this run', number.format(run ? runWins.length : farm.wins ?? runWins.length), `${number.format(Math.max(allWins.length, farm.wins || 0))} all-time · review only, nothing applied`),
      run ? metric('Improved this run', `${number.format(run.improved || 0)} / ${number.format(run.units_searched || 0)}`, 'best below the score at run start (or reseed)')
        : metric('Improved', `${number.format(data.improved_units || 0)} / ${number.format(units.length)}`, 'best score below base score'),
      metric('Units', `${number.format(counts.active || 0)} active`, `${number.format(counts.queued || 0)} queued · ${number.format(counts.won || 0)} won · ${number.format(counts.done || 0)} done${counts.project_exact ? ` · ${number.format(counts.project_exact)} integrated seeds paused` : ''}`),
      phase ? metric('Search phase', phase.value, phase.detail)
        : metric('Iterations this run', number.format(run ? run.iterations || 0 : data.active_iterations || 0), run ? 'all sessions since run start' : 'across active units'));
    children.push(metrics);
    if (run) {
      const loop = run.loop;
      children.push(node('p', 'm2c-live-meta', `Run started ${run.started_at ? new Date(run.started_at).toLocaleString() : 'unknown'} (farm pid ${run.pid ?? '—'})`
        + `${loop ? ` · deterministic loop since ${new Date(loop.started_at).toLocaleTimeString()}: ${number.format(loop.sweeps || 0)} sweeps, ${number.format(loop.improved || 0)} improved, ${number.format(loop.seeds_pending || 0)} seeds pending, ${number.format(loop.published || 0)} published` : ''}`
        + `${(run.rejected || []).length ? ` · ${number.format(run.rejected.length)} rejected by the verify gate` : ''}`));
      if ((run.matched_upstream || []).length) children.push(node('p', 'm2c-stall', `Already 100% in the project, still in the farm manifest: ${run.matched_upstream.join(', ')}`));
    }
    const order = {won: 0, active: 1, done: 2, queued: 3};
    const runScore = (unit) => run ? unit.run?.best_score : unit.best_score, runStart = (unit) => run ? unit.run?.start_score : unit.base_score;
    const sorted = [...units].sort((a, b) => (runScore(a) ?? Infinity) - (runScore(b) ?? Infinity)
      || ((a.best_score - a.base_score) || 0) - ((b.best_score - b.base_score) || 0)
      || (order[a.state] ?? 9) - (order[b.state] ?? 9) || String(a.fn).localeCompare(String(b.fn)));
    const score = (unit) => {
      const best = runScore(unit), start = runStart(unit);
      if (best == null) return run ? 'not run yet' : '—';
      const delta = start == null ? null : best - start;
      return `${number.format(start ?? 0)} → ${number.format(best)}${delta ? ` (${delta > 0 ? '+' : ''}${number.format(delta)})` : ''}`;
    };
    const pct = (unit) => { const value = unit.project_pct ?? unit.pct; return value == null ? '—' : `${Number(value).toFixed(2)}%`; };
    children.push(node('h3', 'project-subhead', run ? 'Units by best score this run (0 = exact)' : 'Units by best score (0 = exact)'),
      sorted.length ? m2cTable(['Function', 'State', run ? 'Score run start → best' : 'Score base → best', 'objdiff % (project)', run ? 'Iterations (run)' : 'Iterations', run ? 'Sessions (run)' : 'Rounds', 'Elapsed', 'Unique C (session)', 'Cache hits (session)', 'Last gain (session)', 'Size', 'Compiler', 'Unit'],
        sorted.map((unit) => [`${unit.fn}${unit.run?.reseeded_in_run ? ' (reseeded)' : ''}`, unit.state === 'project_exact' ? 'Integrated; paused' : unit.state, score(unit), pct(unit),
          number.format((run ? unit.run?.iterations : unit.iterations) || 0), number.format((run ? unit.run?.sessions : unit.rounds) || 0), unit.elapsed_seconds == null ? '—' : duration(unit.elapsed_seconds),
          ...farmSearchMetrics(unit), unit.size == null ? '—' : `${number.format(unit.size)} B`, unit.mw_version || '—', unit.unit || '—']),
        {numericColumns: [2, 3, 4, 5, 6, 7, 8, 9, 10], rollupKey: 'permuter-units'}) : node('p', 'muted', 'No units reported.'));
    const wins = [...allWins].sort((a, b) => (run ? Number(!!b.in_run) - Number(!!a.in_run) : 0) || String(b.ts || '').localeCompare(String(a.ts || '')));
    children.push(node('h3', 'project-subhead', `Wins (review only; nothing is applied to the project)${run ? ` · ${number.format(runWins.length)} this run, ${number.format(allWins.length - runWins.length)} earlier` : ''}`));
    children.push(wins.length ? m2cTable(['Function', 'Won at', 'Verified', 'Address', 'Size', 'objdiff % queued', 'Base score', 'Source', 'Unit'],
      wins.map((win) => [`${win.fn}${run && !win.in_run ? ' (earlier run)' : ''}`, win.ts ? new Date(win.ts).toLocaleString() : '—', farmVerdict(win.verify), win.addr || '—', win.size == null ? '—' : `${number.format(win.size)} B`,
        win.pct == null ? '—' : `${Number(win.pct).toFixed(2)}%`, win.base_score ?? '—', win.local_source || '—', win.unit || '—']),
      {numericColumns: [4, 5, 6], rollupKey: 'permuter-wins'}) : node('p', 'muted', 'No wins yet.'));
    if (run) children.push(node('p', 'm2c-live-meta', `Near wins this run: ${(run.nearwins || []).length ? run.nearwins.join(', ') : 'none'}${(data.nearwins || []).length ? ` · ${number.format(data.nearwins.length)} all-time` : ''}`));
    else if ((data.nearwins || []).length) children.push(node('p', 'm2c-live-meta', `Near wins: ${data.nearwins.join(', ')}`));
    element.replaceChildren(...children);
  }
  function renderFarm(data, now = Date.now()) { farmCard(data, now); farmDetail(data, now); }

  // ── Polling ───────────────────────────────────────────────────────────────────────────────
  /**
   * Poll a JSON endpoint without overlap. Sends If-None-Match so an unchanged snapshot costs a 304 and no
   * re-render, pauses while the tab is hidden (refreshing at once on return) and backs off after failures.
   */
  function poll(url, handlers, options = {}) {
    const interval = options.interval || 1000, maxBackoff = options.maxBackoff || 16000;
    let etag = null, timer = 0, failures = 0, inFlight = false;
    const schedule = () => {
      root.clearTimeout(timer);
      if (doc().hidden) return;
      timer = root.setTimeout(tick, Math.min(interval * 2 ** Math.min(failures, 5), maxBackoff));
    };
    async function tick() {
      if (inFlight || doc().hidden) return;
      inFlight = true;
      const controller = new root.AbortController();
      const timeout = root.setTimeout(() => controller.abort(), 30000);
      try {
        const response = await root.fetch(url, {cache: 'no-store', signal: controller.signal, headers: etag ? {'If-None-Match': etag} : {}});
        if (response.status === 304) { failures = 0; handlers.onUnchanged?.(response); }
        else {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const data = await response.json();
          etag = response.headers.get('ETag');
          failures = 0;
          handlers.onData(data, response);
        }
      } catch (error) {
        failures += 1; etag = null;
        handlers.onError?.(error);
      } finally { root.clearTimeout(timeout); inFlight = false; schedule(); }
    }
    doc().addEventListener('visibilitychange', () => { if (!doc().hidden) { root.clearTimeout(timer); tick(); } });
    tick();
    return {refresh() { root.clearTimeout(timer); tick(); }};
  }
  /** Re-render without moving what the reader is looking at: keep the first visible element with an id in place. */
  function keepViewport(render) {
    const left = root.scrollX || 0, top = root.scrollY || 0;
    let anchor = null;
    try {
      for (const element of doc().querySelectorAll('main [id]')) {
        if (!element.getClientRects().length) continue;
        const rect = element.getBoundingClientRect();
        if (rect.top >= 0 && rect.top < root.innerHeight) { anchor = {id: element.id, document: rect.top + top}; break; }
      }
    } catch (_) { anchor = null; }
    render();
    let target = top;
    const element = anchor && doc().getElementById(anchor.id);
    if (element) target = top + (element.getBoundingClientRect().top + (root.scrollY || 0) - anchor.document);
    target = Math.max(0, Math.round(target));
    if (Math.abs((root.scrollY || 0) - target) >= 1 || (root.scrollX || 0) !== left) root.scrollTo({left, top: target, behavior: 'instant'});
  }

  // ── Charts: theme-aware canvas drawing with hover inspection ──────────────────────────────
  const FONT = 'Inter, ui-sans-serif, system-ui, sans-serif';
  const chartStates = new WeakMap();
  const css = (name, fallback) => (root.getComputedStyle ? root.getComputedStyle(doc().documentElement).getPropertyValue(name) : '').trim() || fallback;
  function chartColors() {
    return {grid: css('--chart-grid', '#e1e6ec'), axis: css('--muted', '#57606a'), goal: css('--series-exact', '#1a7f37'),
      surface: css('--surface', '#fff'), text: css('--text', '#17202a'), guide: css('--faint', '#8c959f'),
      tipBackground: css('--surface-raised', '#fff'), tipBorder: css('--border-strong', '#b9c2cc')};
  }
  /** Column whose x is closest to `x`, or null when none is within `tolerance`. */
  function nearest(columns, x, tolerance = Infinity) {
    let best = null, distance = tolerance;
    for (const column of columns || []) { const gap = Math.abs(column.x - x); if (gap <= distance) { best = column; distance = gap; } }
    return best;
  }
  function paintOverlay(ctx, model, hoverX) {
    if (hoverX === null || !model?.columns?.length) return;
    const {left, top, plotW, plotH} = model.plot;
    if (hoverX < left - 12 || hoverX > left + plotW + 12) return;
    const column = nearest(model.columns, hoverX);
    if (!column) return;
    const c = chartColors();
    ctx.save();
    if (model.band) {
      ctx.globalAlpha = 0.14; ctx.fillStyle = c.guide; ctx.fillRect(column.x - model.band / 2, top, model.band, plotH); ctx.globalAlpha = 1;
    } else {
      ctx.setLineDash([4, 3]); ctx.lineWidth = 1; ctx.strokeStyle = c.guide;
      ctx.beginPath(); ctx.moveTo(column.x, top); ctx.lineTo(column.x, top + plotH); ctx.stroke(); ctx.setLineDash([]);
      column.rows.forEach((row) => {
        if (row.y == null) return;
        ctx.beginPath(); ctx.arc(column.x, row.y, 6, 0, Math.PI * 2); ctx.fillStyle = row.color; ctx.fill();
        ctx.lineWidth = 2.5; ctx.strokeStyle = c.surface; ctx.stroke();
      });
    }
    const pad = 10, rowHeight = 19, detailHeight = 12, swatch = 8;
    const titleFont = `600 12px ${FONT}`, rowFont = `12px ${FONT}`, valueFont = `600 12px ${FONT}`, detailFont = `11px ${FONT}`;
    ctx.font = titleFont;
    let width = ctx.measureText(column.title).width;
    column.rows.forEach((row) => {
      ctx.font = rowFont; const label = ctx.measureText(row.label).width;
      ctx.font = valueFont; const value = ctx.measureText(row.value).width;
      ctx.font = detailFont; const detail = row.detail ? ctx.measureText(row.detail).width - swatch - 16 : 0;
      width = Math.max(width, swatch + 8 + label + 18 + value, detail + swatch + 8);
    });
    width = Math.ceil(width + pad * 2);
    const height = pad * 2 + 18 + column.rows.reduce((sum, row) => sum + rowHeight + (row.detail ? detailHeight : 0), 0);
    let x = column.x + 16;
    if (x + width > model.width - 4) x = column.x - 16 - width;
    x = Math.max(4, x);
    const y = Math.min(Math.max(top + 4, 4), Math.max(4, model.height - height - 4));
    ctx.shadowColor = 'rgba(0, 0, 0, .25)'; ctx.shadowBlur = 12; ctx.shadowOffsetY = 3;
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, y, width, height, 8); else ctx.rect(x, y, width, height);
    ctx.fillStyle = c.tipBackground; ctx.fill();
    ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;
    ctx.lineWidth = 1; ctx.strokeStyle = c.tipBorder; ctx.stroke();
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    let line = y + pad + 9;
    ctx.font = titleFont; ctx.fillStyle = c.text; ctx.fillText(column.title, x + pad, line);
    line += 18;
    column.rows.forEach((row) => {
      ctx.beginPath(); ctx.arc(x + pad + swatch / 2, line, swatch / 2, 0, Math.PI * 2); ctx.fillStyle = row.color; ctx.fill();
      ctx.font = rowFont; ctx.fillStyle = c.text; ctx.textAlign = 'left'; ctx.fillText(row.label, x + pad + swatch + 8, line);
      ctx.font = valueFont; ctx.textAlign = 'right'; ctx.fillText(row.value, x + width - pad, line);
      if (row.detail) {
        line += 14; ctx.font = detailFont; ctx.fillStyle = c.axis; ctx.textAlign = 'left';
        ctx.fillText(row.detail, x + pad + swatch + 8, line); line += detailHeight - 12;
      }
      line += rowHeight;
    });
    ctx.restore();
  }
  /** Finish a chart draw: remember its model and paint the hover crosshair and tooltip. */
  function finishChart(canvas, ctx, model, redraw) {
    let state = chartStates.get(canvas);
    if (!state) {
      state = {hoverX: null, model: null, redraw: null, frame: 0};
      chartStates.set(canvas, state);
      canvas.dataset.chart = 'hover';
      const update = (x) => {
        state.hoverX = x;
        if (state.frame || !state.redraw) return;
        state.frame = root.requestAnimationFrame(() => { state.frame = 0; state.redraw(); });
      };
      const position = (event) => event.clientX - canvas.getBoundingClientRect().left;
      canvas.addEventListener('pointermove', (event) => update(position(event)));
      canvas.addEventListener('pointerdown', (event) => update(position(event)));
      canvas.addEventListener('pointerleave', () => update(null));
    }
    state.model = model; state.redraw = redraw;
    paintOverlay(ctx, model, state.hoverX);
  }
  function prepareCanvas(canvas) {
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return null;
    const ratio = root.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    return {ctx, width, height};
  }
  const MAX_BAR_RUNS = 30;
  /** Grouped 0-100% bars, one group per run; validation series are hollow where runs are not comparable. */
  function drawYieldBars(canvas, ctx, {runs, series, width, height, redraw, format = (value) => `${value.toFixed(1)}%`}) {
    const c = chartColors();
    const omitted = Math.max(0, runs.length - MAX_BAR_RUNS), shown = runs.slice(omitted);
    const left = 44, right = 16, top = 14, bottom = 30;
    const plotW = Math.max(10, width - left - right), plotH = Math.max(10, height - top - bottom);
    const y = (value) => top + plotH * (1 - value / 100);
    ctx.font = `11px ${FONT}`; ctx.textBaseline = 'middle';
    for (let value = 0; value <= 100; value += 25) {
      ctx.strokeStyle = c.grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, y(value)); ctx.lineTo(left + plotW, y(value)); ctx.stroke();
      ctx.fillStyle = c.axis; ctx.textAlign = 'right'; ctx.fillText(`${value}%`, left - 6, y(value));
    }
    const group = plotW / shown.length, inner = Math.min(group * 0.8, 72), slot = inner / series.length;
    const center = (index) => left + group * (index + 0.5);
    shown.forEach((run, index) => {
      series.forEach((item, k) => {
        const value = item.values[omitted + index];
        if (value == null) return;
        const x = center(index) - inner / 2 + k * slot + 0.75, w = Math.max(1, slot - 1.5);
        const h = Math.max(value > 0 ? 2 : 1, y(0) - y(value));
        ctx.fillStyle = item.color;
        if (item.validation && run.incomparable) {
          ctx.globalAlpha = 0.18; ctx.fillRect(x, y(0) - h, w, h); ctx.globalAlpha = 1;
          ctx.strokeStyle = item.color; ctx.lineWidth = 1.25; ctx.strokeRect(x + 0.6, y(0) - h + 0.6, Math.max(0, w - 1.2), Math.max(0, h - 1.2));
        } else ctx.fillRect(x, y(0) - h, w, h);
      });
    });
    ctx.fillStyle = c.axis; ctx.textBaseline = 'top'; ctx.textAlign = 'center';
    const step = Math.max(1, Math.ceil(shown.length / Math.max(2, Math.floor(plotW / 64))));
    shown.forEach((run, index) => {
      if ((shown.length - 1 - index) % step) return;
      ctx.fillText(run.label || '', Math.min(Math.max(center(index), left + 14), left + plotW - 14), top + plotH + 8);
    });
    const columns = shown.map((run, index) => ({x: center(index), title: run.title, rows: series.flatMap((item) => {
      const value = item.values[omitted + index];
      if (value == null) return [];
      return [{label: item.label, value: format(value), color: item.color, y: y(value),
        detail: item.validation && run.incomparable ? 'validation not comparable' : (item.detail ? item.detail(omitted + index) : '')}];
    })}));
    finishChart(canvas, ctx, {width, height, plot: {left, top, plotW, plotH}, columns, band: group}, redraw);
    return omitted;
  }
  /** Progress-history series: the report's percentages over time, null where a point lacks the measure. */
  function historySeries(points) {
    return [
      {key: 'fuzzy', label: 'Fuzzy match', color: css('--series-fuzzy', '#8250df'), value: (p) => measuredNumber(p.fuzzy_match_percent)},
      {key: 'functions', label: 'Functions matched exactly', color: css('--series-functions', '#0969da'), value: (p) => measuredPercent(p, 'matched_functions_percent', 'matched_functions', 'total_functions')},
      {key: 'code', label: 'Code matched exactly', color: css('--series-code', '#bf3989'), value: (p) => measuredPercent(p, 'matched_code_percent', 'matched_code', 'total_code')},
      {key: 'linked', label: 'Code in fully linked units', color: css('--series-linked', '#bf8700'), value: (p) => measuredPercent(p, 'complete_code_percent', 'complete_code', 'total_code')},
    ].map((item) => ({...item, values: points.map((point) => ({point, value: item.value(point)}))}))
      .filter((item) => item.values.some((row) => row.value !== null));
  }

  // ── Page ──────────────────────────────────────────────────────────────────────────────────
  const state = {config: null, project: null, data: null, version: ''};
  const versionData = () => state.data?.versions?.[state.version];

  function skeleton(config, project) {
    const main = doc().querySelector('main');
    main.replaceChildren();
    const nav = node('nav', 'project-nav');
    nav.setAttribute('aria-label', 'Projects');
    for (const item of config.projects || []) {
      const link = node('a', '', item.name);
      link.href = `/?project=${encodeURIComponent(item.id)}`;
      if (item.id === project.id) link.setAttribute('aria-current', 'page');
      nav.append(link);
    }
    const toggle = node('button', 'theme-toggle');
    toggle.id = 'theme-toggle'; toggle.type = 'button'; toggle.setAttribute('aria-label', 'Toggle dark mode');
    nav.append(toggle);
    main.append(nav);
    main.insertAdjacentHTML('beforeend', `
      <header class="dashboard-header"><div><p class="eyebrow"><span id="project-id"></span> / Decompilation</p><h1 id="project-name"></h1></div>
        <div class="project-build"><label for="project-version">Version</label><select id="project-version"></select><span id="project-build-status">Loading build</span></div></header>
      <section class="worker-strip" aria-label="Local workers and m2c progress">
        <div class="worker-line"><strong>Local workers</strong><div id="shared-worker" role="status">Loading activity</div><time id="worker-updated"></time></div>
        <div id="executor-status" class="executor-status" aria-label="Remote m2c executors" hidden></div>
        <div id="m2c-project-overview" class="worker-project-grid" aria-label="m2c progress across projects"></div>
        <a id="permuter-farm-card" class="farm-card" href="#permuter-farm" hidden></a>
        <div id="shared-responses" class="response-badges" aria-label="Last four attempts"></div>
      </section>
      <section id="project-metrics" class="metrics progress-metrics" aria-label="Project progress"></section>
      <p id="metrics-updated" class="metrics-updated muted" role="status"></p>
      <section class="band" id="history-band"><div class="section-heading"><h2>Progress over time</h2><p id="project-history-updated" class="muted"></p></div>
        <div class="chart-controls"><label class="chart-floor">Min Y % <input id="history-floor" type="number" min="0" max="99" step="1" value="0" inputmode="numeric" aria-label="Lowest percentage shown on the progress chart"></label></div>
        <canvas id="project-history" aria-label="Progress over time, 0 to 100 percent"></canvas><div id="project-history-legend" class="legend history-legend"></div></section>
      <section class="band" id="categories-band" hidden><div class="section-heading"><h2>Categories</h2></div><div id="project-categories"></div></section>
      <section class="m2c-intro" id="m2c"><div class="section-heading"><h2>m2c sweep report</h2><p id="project-m2c-updated" class="muted"></p></div>
        <p id="project-m2c-notice" class="m2c-notice" hidden></p><div id="project-m2c-live" class="m2c-live" role="status"></div>
        <div id="project-m2c-metrics" class="metrics m2c-metrics"></div></section>
      <section id="permuter-farm" class="band farm-band" hidden></section>
      <section class="band"><div class="section-heading"><h2>Draft and validation yield</h2><p id="project-m2c-history-meta" class="muted"></p></div>
        <canvas id="project-m2c-history" aria-label="Draft and validation history"></canvas><div id="project-m2c-history-legend" class="legend"></div><div id="project-m2c-runs"></div></section>
      <section class="band m2c-details"><div><div class="section-heading"><h2>Latest diagnostics</h2></div><div id="project-m2c-detail"></div></div>
        <div class="m2c-reference-section"><div class="section-heading"><h2>Matched reference pool</h2></div><div id="project-reference-pool"></div></div></section>
      <section class="band"><div class="section-heading"><h2>Closest generated drafts</h2></div><div id="project-candidates"></div></section>
      <section class="band"><div class="section-heading"><h2>Deterministic refinement results</h2></div><div id="project-refinements"></div></section>
      <section id="m2c-unit-promotions" class="band" hidden></section>
      <section class="band" id="project-attempts"><div class="section-heading"><h2>Draft attempt activity</h2><p id="project-attempts-updated" class="muted"></p></div>
        <div id="project-model-runtime" class="model-runtime"></div><div id="project-attempts-summary" class="attempt-summary"></div><div id="project-attempts-list"></div></section>`);
  }

  function renderMetrics(data) {
    const measures = data.measures || {}, delta = data.stats_delta?.measures || {};
    const percentage = (value) => value === null ? 'Unknown' : precisePercent(value);
    const counts = (key, total, unit = '') => {
      const value = measuredNumber(measures[key]), denominator = measuredNumber(measures[total]);
      return value === null || denominator === null ? 'Unknown' : `${number.format(value)} / ${number.format(denominator)}${unit}`;
    };
    $('#project-metrics').replaceChildren(
      metric('Fuzzy match', percentage(measuredNumber(measures.fuzzy_match_percent)), 'objdiff report', measuredNumber(delta.fuzzy_match_percent), 'pp'),
      metric('Exact functions', counts('matched_functions', 'total_functions'), percentage(measuredPercent(measures, 'matched_functions_percent', 'matched_functions', 'total_functions')), measuredNumber(delta.matched_functions), 'functions'),
      metric('Matched code', counts('matched_code', 'total_code', ' bytes'), percentage(measuredPercent(measures, 'matched_code_percent', 'matched_code', 'total_code')), measuredNumber(delta.matched_code), 'bytes'),
      metric('Linked code', counts('complete_code', 'total_code', ' bytes'), percentage(measuredPercent(measures, 'complete_code_percent', 'complete_code', 'total_code')),
        measuredNumber(delta.complete_code) ?? measuredNumber(delta.complete_code_percent), measuredNumber(delta.complete_code) === null ? 'pp' : 'bytes'));
    const updated = $('#metrics-updated'), source = data.report_source;
    updated.textContent = source?.status === 'available'
      ? `Report ${date(source.report_at)}${source.report_behind_source ? ' · Source edits newer than report' : ''}${data.stats_delta?.from_at ? ` · Changes since ${date(data.stats_delta.from_at)}` : ' · No prior update'}`
      : source?.status === 'changing' ? 'Build report is being rewritten' : source?.status === 'invalid' ? 'Build report is invalid' : 'Build report unavailable';
    updated.title = source?.path || '';
    const band = $('#categories-band'), rows = data.categories || [];
    band.hidden = !rows.length;
    if (rows.length) $('#project-categories').replaceChildren(m2cTable(['Category', 'Fuzzy', 'Functions exact', 'Code matched', 'Code linked'],
      rows.map((row) => { const m = row.measures || {};
        const p = (value) => value === null ? '—' : precisePercent(value);
        return [row.name, p(measuredNumber(m.fuzzy_match_percent)), p(measuredPercent(m, 'matched_functions_percent', 'matched_functions', 'total_functions')),
          p(measuredPercent(m, 'matched_code_percent', 'matched_code', 'total_code')), p(measuredPercent(m, 'complete_code_percent', 'complete_code', 'total_code'))]; }),
      {numericColumns: [1, 2, 3, 4], rollupKey: 'categories'}));
  }

  function historyFloor() {
    const value = Math.floor(Number($('#history-floor')?.value));
    return Number.isFinite(value) ? Math.min(99, Math.max(0, value)) : 0;
  }
  function drawHistory() {
    const canvas = $('#project-history');
    if (!canvas) return;
    const points = (versionData()?.progress_history || []).filter((point) => Number.isFinite(Date.parse(point.at)))
      .map((point) => ({...point, t: Date.parse(point.at)})).sort((a, b) => a.t - b.t);
    const legend = $('#project-history-legend'), updated = $('#project-history-updated');
    const surface = prepareCanvas(canvas);
    if (!surface) return;
    const {ctx, width, height} = surface;
    if (!points.length) {
      updated.textContent = 'No exact build recorded';
      legend.textContent = 'Each new byte-exact build report adds a point while the dashboard runs.';
      return;
    }
    const theme = chartColors(), floor = historyFloor();
    const left = 54, right = 24, top = 18, bottom = 38;
    const plotW = Math.max(width - left - right, 12), plotH = Math.max(height - top - bottom, 12);
    const first = points[0], latest = points[points.length - 1];
    const x = (point) => left + (latest.t === first.t ? plotW / 2 : (point.t - first.t) * plotW / (latest.t - first.t));
    const y = (value) => top + plotH * (1 - (value - floor) / (100 - floor));
    const series = historySeries(points);
    ctx.font = `11px ${FONT}`; ctx.textBaseline = 'middle';
    const step = 100 - floor <= 20 ? 1 : 100 - floor <= 50 ? 5 : 10;
    for (let value = floor; value <= 100; value = Math.floor(value / step) * step + step) {
      const position = y(value);
      ctx.strokeStyle = theme.grid; ctx.lineWidth = 1;
      if (value === 100) { ctx.strokeStyle = theme.goal; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); }
      ctx.beginPath(); ctx.moveTo(left, position); ctx.lineTo(left + plotW, position); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = theme.axis; ctx.textAlign = 'right'; ctx.fillText(`${value}%`, left - 7, position);
    }
    ctx.save(); ctx.beginPath(); ctx.rect(left, top, plotW, plotH); ctx.clip();
    series.forEach((item) => {
      ctx.strokeStyle = item.color; ctx.lineWidth = 2.5; ctx.beginPath();
      let connected = false;
      item.values.forEach(({point, value}) => {
        if (value === null) { connected = false; return; }
        if (connected) ctx.lineTo(x(point), y(value)); else ctx.moveTo(x(point), y(value));
        connected = true;
      });
      ctx.stroke(); ctx.fillStyle = item.color; ctx.strokeStyle = theme.surface; ctx.lineWidth = 2;
      item.values.forEach(({point, value}) => {
        if (value === null || value < floor || value > 100) return;
        ctx.beginPath(); ctx.arc(x(point), y(value), 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      });
    });
    ctx.restore();
    ctx.fillStyle = theme.axis; ctx.textBaseline = 'top';
    const stamp = (point) => new Date(point.at).toLocaleDateString([], {month: 'short', day: 'numeric'});
    ctx.textAlign = latest.t === first.t ? 'center' : 'left'; ctx.fillText(stamp(first), latest.t === first.t ? x(first) : left, top + plotH + 8);
    if (latest.t !== first.t) { ctx.textAlign = 'right'; ctx.fillText(stamp(latest), x(latest), top + plotH + 8); }
    const columns = points.map((point, index) => ({x: x(point), title: date(point.at), rows: series.flatMap((item) => {
      const {value} = item.values[index];
      return value === null || value < 0 || value > 100 ? [] : [{label: item.label, value: precisePercent(value), color: item.color, y: value >= floor ? y(value) : null}];
    })}));
    finishChart(canvas, ctx, {width, height, plot: {left, top, plotW, plotH}, columns}, drawHistory);
    updated.textContent = `${number.format(points.length)} recorded ${points.length === 1 ? 'build' : 'builds'} · latest ${date(latest.at)}`;
    legend.replaceChildren(...series.map((item) => {
      const measured = item.values.filter((row) => row.value !== null), start = measured[0], end = measured[measured.length - 1];
      const change = end.value - start.value, key = node('span', 'history-key'), swatch = node('i', 'tile');
      swatch.style.background = item.color;
      key.append(swatch, node('span', '', `${item.label}: ${precisePercent(end.value)}; ${change >= 0 ? '+' : ''}${change.toFixed(2)} pts since ${date(start.point.at)}`));
      return key;
    }));
  }

  function m2cScored(validation) {
    const counts = validation?.counts;
    if (!counts || !['non_exact', 'scorer_exact'].some((key) => measuredNumber(counts[key]) !== null)) return null;
    return (measuredNumber(counts.non_exact) || 0) + (measuredNumber(counts.scorer_exact) || 0);
  }
  function drawM2cHistory(report) {
    const canvas = $('#project-m2c-history'), legend = $('#project-m2c-history-legend');
    if (!canvas || !legend) return;
    const surface = prepareCanvas(canvas);
    if (!surface) return;
    const {ctx, width, height} = surface;
    const runs = (report.runs || []).filter((run) => (run.selection || 'unmatched') === 'unmatched')
      .slice().sort((a, b) => (Date.parse(a.updated_at) || 0) - (Date.parse(b.updated_at) || 0));
    if (!runs.length) { legend.textContent = 'No completed m2c sweeps have been indexed yet.'; return; }
    const rate = (value, total) => measuredNumber(value) !== null && measuredNumber(total) > 0 ? 100 * value / total : null;
    const of = (value, total) => measuredNumber(value) === null || measuredNumber(total) === null ? '' : `${number.format(value)} of ${number.format(total)}`;
    const comparable = (run) => run.validation?.comparable;
    const series = [
      {label: 'Clean C drafts', color: css('--series-functions', '#0969da'), value: (run) => String(run.pipeline?.version || '').includes('assembly-only') ? null : rate(run.counts?.ok, run.total),
        detail: (run) => `${of(run.counts?.ok, run.total)} drafts`},
      {label: 'Compiled', color: css('--series-fuzzy', '#8250df'), validation: true, value: (run) => comparable(run) ? rate(run.validation.compiler_succeeded, run.validation.total) : null,
        detail: (run) => `${of(run.validation?.compiler_succeeded, run.validation?.total)} compiled`},
      {label: 'Objdiff scored', color: css('--series-linked', '#bf8700'), validation: true, value: (run) => comparable(run) ? rate(m2cScored(run.validation), run.validation.total) : null,
        detail: (run) => `${of(m2cScored(run.validation || {}), run.validation?.total)} scored`},
      {label: 'Scorer exact', color: css('--series-exact', '#1a7f37'), validation: true, value: (run) => comparable(run) ? rate(run.validation.counts?.scorer_exact, run.validation.total) : null,
        detail: (run) => `${of(run.validation?.counts?.scorer_exact, run.validation?.total)} exact`},
    ].map((item) => ({...item, values: runs.map((run) => item.value(run)), detail: (index) => item.detail(runs[index])}));
    const omitted = drawYieldBars(canvas, ctx, {width, height, redraw: () => drawM2cHistory(report), series,
      runs: runs.map((run, index) => ({label: new Date(run.updated_at).toLocaleDateString([], {month: 'short', day: 'numeric'}),
        incomparable: Boolean(run.validation) && !run.validation.comparable, title: `Run ${index + 1} of ${runs.length} · ${date(run.updated_at)}`}))});
    legend.replaceChildren(...series.map((item) => {
      const key = node('span', 'history-key'), swatch = node('i', 'tile'); swatch.style.background = item.color;
      key.append(swatch, node('span', '', `${item.label}${item.values.some((value) => value !== null) ? '' : ': unmeasured'}`)); return key;
    }), node('span', 'muted', `${omitted ? `Latest ${number.format(runs.length - omitted)} of ${number.format(runs.length)} sweeps; earlier runs stay in the table below. ` : ''}Hollow bars: validation not comparable. Scratch scores are not linked matches.`));
  }

  function liveRun(run) {
    const row = node('div', 'm2c-live-run'), heading = node('div', 'm2c-live-heading');
    const stages = {validation: 'MWCC + objdiff validation', refinement: 'Deterministic refinement', owner_trial: 'Incremental owner trial',
      unit_gate: 'Unit object + link gate', promotion: 'Full build + SHA promotion', candidate_bank: 'Candidate bank + link certification',
      asm_backed_trial: 'ASM-backed owner trial (link evidence only)', visible_units: 'Visible units: exact functions into NonMatching C',
      finalizing: 'Finalizing retained reports'};
    const stage = stages[run.stage] || 'Draft generation';
    heading.append(node('strong', '', `${stage} · ${run.selection || 'unmatched'} corpus`), node('span', 'm2c-live-state', String(run.state || 'running').replaceAll('_', ' ')));
    const total = measuredNumber(run.total), processed = measuredNumber(run.processed);
    row.append(heading, node('p', 'm2c-live-count', total !== null && total > 0 && processed !== null
      ? `${number.format(processed)} / ${number.format(total)} · ${Math.min(100, processed / total * 100).toFixed(1)}%` : 'Starting'));
    const progress = node('progress', 'm2c-live-progress');
    progress.setAttribute('aria-label', `${run.selection || 'unmatched'} ${run.stage || 'generation'} progress`);
    if (total > 0 && processed !== null) { progress.max = total; progress.value = Math.max(0, Math.min(processed, total)); }
    row.append(progress);
    const seconds = Number(run.elapsed_seconds || 0);
    row.append(node('p', 'm2c-live-meta', `${run.run_id || 'Awaiting checkpoint'} · pid ${run.pid ?? '—'} · ${seconds ? `${duration(seconds)} elapsed` : 'elapsed time unavailable'}`),
      node('p', 'm2c-live-meta', `Checkpoint ${date(run.updated_at)} · live check ${date(state.data?._cache?.updated_at)}`));
    if (run.current_function) row.append(node('p', 'm2c-live-current', run.current_function));
    if (run.current_work) row.append(node('p', 'm2c-live-meta', run.current_work));
    const gate = gateProgress(run.unit_gate); if (gate) row.append(gate);
    const visible = visibleProgress(run.visible_progress); if (visible) row.append(visible);
    const stall = stallNotice(run); if (stall) { row.append(stall); progress.classList.add('stalled'); }
    if (run.auto_promote === false) row.append(node('p', 'm2c-flag', 'Auto-promote is off for this run: the gate checks units but will not promote them.'));
    const refinement = refinementProgress(run); if (refinement) row.append(refinement);
    if (typeof run.compiler_succeeded === 'number') row.append(node('p', 'm2c-live-outcomes', `${number.format(run.compiler_succeeded)} compiler successes · ${number.format(run.compiled_functions || 0)} assigned functions`));
    const outcomes = Object.entries(run.counts || {}).sort((a, b) => b[1] - a[1]);
    if (outcomes.length) row.append(node('p', 'm2c-live-outcomes', outcomes.map(([name, count]) => `${number.format(count)} ${name.replaceAll('_', ' ')}`).join(' · ')));
    return row;
  }

  function renderM2c(data) {
    const report = data?.m2c || {}, activeRuns = report.active_runs || [];
    const live = $('#project-m2c-live');
    live.replaceChildren();
    const completedRefinement = report.latest?.validation?.refinement, completedGate = completedRefinement?.unit_gate;
    if (report.available === false) live.append(node('p', 'negative', report.reason || 'm2c report unavailable'));
    else if (!activeRuns.length && !(completedGate && Object.keys(completedGate).length))
      live.append(node('p', 'muted', report.activity_available === false ? 'Runner state unavailable on this host' : 'No active m2c sweep'));
    activeRuns.forEach((run) => live.append(liveRun(run)));
    if (report.harness_error) live.append(node('p', 'm2c-flag', `Harness m2c report reader unavailable (${report.harness_error}); showing the stored index.`));
    if (completedGate && Object.keys(completedGate).length) {
      const row = node('div', 'm2c-live-run'), heading = node('div', 'm2c-live-heading');
      heading.append(node('strong', '', 'Last promotion loop'), node('span', 'm2c-live-state', 'Completed'));
      const progress = node('progress', 'm2c-live-progress');
      progress.max = 1; progress.value = 1; progress.setAttribute('aria-label', 'Last promotion loop complete');
      row.append(heading,
        node('p', 'm2c-live-count', `${number.format(completedGate.promoted || 0)} units promoted · ${number.format(completedGate.candidate_linked || 0)} exact functions linked · ${number.format(completedGate.candidate_visible || 0)} candidates visible · ${number.format(completedGate.owner_links_verified || 0)} owner links`),
        progress,
        node('p', 'm2c-live-meta', `${number.format(completedGate.candidate_banked || 0)} functions banked · ${number.format(completedGate.owner_trials_run || 0)} incremental owner trials · ${number.format(completedGate.gates_run || 0)} complete-unit gates`),
        node('p', 'm2c-live-meta', `Completed ${date(completedRefinement.updated_at)}`));
      const visible = visibleUnitsText(completedGate.visible_units);
      if (visible) row.append(node('p', 'm2c-live-meta', visible));
      live.append(row);
    }
    $('#project-m2c-history-meta').textContent = `${number.format(report.history_count ?? report.runs?.length ?? 0)} completed sweeps retained`;
    const active = activeRuns.find((run) => run.stage === 'generation');
    const latest = active || report.latest;
    const attempts = data?.draft_attempts || {};
    $('#project-m2c-updated').textContent = active ? `Running ${number.format(active.processed || 0)} / ${number.format(active.total || 0)}`
      : report.generated_at ? `Indexed ${date(report.generated_at)}` : 'No indexed sweep';
    const notice = $('#project-m2c-notice'), runs = $('#project-m2c-runs'), detail = $('#project-m2c-detail');
    runs.replaceChildren(); detail.replaceChildren();
    promotions($('#m2c-unit-promotions'), data?.unit_promotions);
    drawM2cHistory(report);
    const pool = report.matched_reference_pool || {}, poolRoot = $('#project-reference-pool');
    poolRoot.replaceChildren();
    const effectivePool = Math.max(Number(pool.available || 0), Number(latest?.pipeline?.reference_pool || 0));
    poolRoot.append(node('strong', 'm2c-pool-count', number.format(effectivePool)),
      node('p', '', 'source-backed, byte-exact functions available as project-local C and assembly references'),
      node('p', 'm2c-footnote', latest?.pipeline?.reference_pool
        ? `Structural retrieval uses ${latest.pipeline.reference_projects?.join(' + ') || 'cross-project'} exact corpora.`
        : pool.similarity_index_ready ? 'Retrieval excludes stale assembly shapes.' : 'The assembly-similarity index is unavailable.'));
    if (report.matched_latest) {
      const matched = report.matched_latest, check = matched.validation;
      poolRoot.append(node('p', 'm2c-footnote', `Matched corpus sweep: ${number.format(matched.counts?.ok || 0)} clean drafts / ${number.format(matched.total || 0)} known matches.`));
      if (check) poolRoot.append(node('p', 'm2c-footnote', `${number.format(check.compiler_succeeded || 0)} compiled; ${number.format(m2cScored(check) || 0)} scored; ${number.format(check.counts?.scorer_exact || 0)} scorer-exact drafts.`));
    }
    $('#project-candidates').replaceChildren(latest?.validation?.top_candidates?.length
      ? m2cTable(['Function', 'Unit', 'Generated', 'Original', 'Gain', 'Scope'], latest.validation.top_candidates.map((row) => [row.symbol || '-', row.unit || '-',
        measuredNumber(row.pct) === null ? 'Unmeasured' : `${Number(row.pct).toFixed(2)}%`, row.original_pct == null ? '-' : `${Number(row.original_pct).toFixed(2)}%`,
        row.original_pct == null ? '-' : `${Number(row.pct) - Number(row.original_pct) >= 0 ? '+' : ''}${(Number(row.pct) - Number(row.original_pct)).toFixed(2)} pp`, row.scope || '-']),
      {numericColumns: [2, 3, 4], rollupKey: 'candidates'})
      : node('p', 'empty', 'No scored candidates recorded.'));
    const refinementRoot = $('#project-refinements'), refinement = latest?.validation?.refinement;
    refinementRoot.replaceChildren();
    if (!refinement) refinementRoot.append(node('p', 'empty', 'No deterministic refinement pass recorded.'));
    else {
      const gate = refinement.unit_gate || {};
      refinementRoot.append(node('p', 'm2c-footnote', `${number.format(refinement.processed || 0)} / ${number.format(refinement.selected || 0)} targets · ${number.format(refinement.improved || 0)} improved · ${number.format(refinement.exact || 0)} byte exact · ${number.format(gate.candidate_banked || 0)} functions banked · ${number.format(gate.candidate_linked || 0)} linked · ${number.format(gate.candidate_visible || 0)} visible candidates · ${number.format(gate.promoted || 0)} units promoted`),
        m2cTable(['Function', 'Validation', 'Refined best', 'Delta', 'Byte exact'], (refinement.rows || []).map((row) => [row.symbol || '-',
          row.baseline_pct == null ? '-' : `${Number(row.baseline_pct).toFixed(4)}%`, row.best_pct == null ? '-' : `${Number(row.best_pct).toFixed(4)}%`,
          row.delta_pp == null ? '-' : `${row.delta_pp >= 0 ? '+' : ''}${Number(row.delta_pp).toFixed(4)} pp`, row.object_exact ? 'Yes' : 'No']),
        {numericColumns: [1, 2, 3], rollupKey: 'refinements'}));
    }
    const metrics = $('#project-m2c-metrics');
    const count = (value) => measuredNumber(value) === null ? 'Unmeasured' : number.format(value);
    const ratio = (value, total) => measuredNumber(value) === null || measuredNumber(total) === null ? 'Unmeasured' : `${number.format(value)} / ${number.format(total)}`;
    if (!latest) {
      notice.hidden = false; notice.textContent = `No m2c sweep recorded for ${state.version}.`;
      metrics.replaceChildren();
      runs.append(node('p', 'empty', 'No runs yet.')); detail.append(node('p', 'empty', 'Diagnostics will appear after the first sweep.'));
      return;
    }
    const assemblyOnly = String(latest.pipeline?.version || '').includes('assembly-only');
    const validation = latest.validation_progress || latest.validation || {};
    const liveChecks = assemblyOnly && validation.total === undefined && attempts.batch === latest.run_id ? attempts.draft_validation : null;
    const draftTotal = attempts.total || latest.total || 0;
    notice.hidden = Boolean(latest.validation) && !assemblyOnly;
    notice.textContent = assemblyOnly && latest.validation ? 'Assembly-only m2c hypotheses with separate scratch MWCC/objdiff validation. Scores are diagnostic, not linked matches.'
      : assemblyOnly && liveChecks ? `Assembly-only m2c hypotheses. ${number.format(liveChecks.checked)} / ${number.format(draftTotal)} original drafts have scratch compiler and objdiff checks. Scratch scores are not linked matches.`
      : assemblyOnly ? 'Assembly-only m2c hypotheses. No source types, compile result, objdiff score, or byte-exact match is claimed.'
      : !latest.validation ? 'Drafts are available, but scratch compilation and objdiff validation have not run.' : '';
    if (active) {
      notice.hidden = false;
      notice.textContent = `Draft generation is running: ${number.format(active.processed || 0)} of ${number.format(active.total || 0)} functions processed.`;
    } else if (latest.validation_progress) {
      notice.hidden = false;
      notice.textContent = `Scratch validation ${validation.state}: ${number.format(validation.processed || 0)} / ${number.format(validation.total || 0)} checked. Counts are partial; these are not linked matches.`;
      $('#project-m2c-updated').textContent = `Validation checked ${date(validation.updated_at)}`;
    }
    metrics.replaceChildren(
      metric('Clean drafts', assemblyOnly ? 'Unmeasured' : ratio(latest.counts?.ok, latest.total), assemblyOnly ? `${count(latest.counts?.ok)} produced drafts; unresolved types or fields may remain` : 'm2c generated without errors'),
      metric('Compiled', liveChecks ? ratio(liveChecks.compiled, draftTotal) : ratio(validation.compiler_succeeded, validation.total), 'Scratch compiler'),
      metric('Objdiff scored', liveChecks ? ratio(liveChecks.scored, draftTotal) : ratio(m2cScored(validation), validation.total), 'Diagnostic only'),
      metric('Scorer exact', count(validation.counts?.scorer_exact), 'Relocation/link review pending'));
    if (measuredNumber(validation.instruction_exact) !== null) detail.append(node('p', 'muted', `${number.format(validation.instruction_exact)} instruction-exact scratch comparisons; link review remains.`));
    runs.append(m2cTable(['Completed', 'Corpus', 'Run', assemblyOnly ? 'Produced' : 'Clean', 'Validation', 'Compiled', 'Scored', 'Instruction exact'], [...(report.runs || [])].reverse().map((run) => {
      const checked = run.validation_progress || run.validation;
      return [date(run.updated_at), run.selection || 'unmatched', String(run.run_id || '').slice(0, 12), `${run.counts?.ok || 0}/${run.total || 0}`,
        run.validation_progress ? `${checked.state}: ${checked.processed || 0}/${checked.total || 0}` : checked ? 'Complete' : 'Not started',
        checked ? `${checked.compiler_succeeded || 0}/${checked.total || 0}` : '-',
        checked ? Number(checked.counts?.non_exact || 0) + Number(checked.counts?.scorer_exact || 0) : '-', checked?.instruction_exact ?? '-'];
    }), {numericColumns: [3, 5, 6, 7], rollupKey: 'm2c-runs'}));
    const statusRows = Object.entries(latest.counts || {}).sort((a, b) => b[1] - a[1]).map(([name, value]) => [name.replaceAll('_', ' '), value]);
    Object.entries(validation.counts || {}).sort((a, b) => b[1] - a[1]).forEach(([name, value]) => statusRows.push([`validation: ${name.replaceAll('_', ' ')}`, value]));
    detail.append(m2cTable(['Outcome', 'Functions'], statusRows, {numericColumns: [1], rollupKey: 'outcomes'}));
    if (Object.keys(latest.diagnostics || {}).length) detail.append(node('h3', 'project-subhead', 'Unresolved diagnostics'),
      m2cTable(['Diagnostic', 'Occurrences', 'Affected drafts'], Object.entries(latest.diagnostics).map(([name, value]) =>
        [name.replaceAll('_', ' '), value, latest.diagnostic_functions?.[name] || 0]), {numericColumns: [1, 2], rollupKey: 'diagnostics'}));
  }

  const RUNTIME = {running: 'Running', model_ready: 'Model ready', model_not_loaded: 'Model not loaded', rpc_unavailable: 'RPC unavailable',
    not_probed: 'Worker running (remote model not probed)', stopped: 'Stopped'};
  function renderAttempts(data) {
    const attempts = data?.draft_attempts || {};
    const runtimeRoot = $('#project-model-runtime'), summary = $('#project-attempts-summary'), list = $('#project-attempts-list');
    runtimeRoot.replaceChildren(); summary.replaceChildren(); list.replaceChildren();
    if (attempts.available === false || attempts.oversized_state) {
      $('#project-attempts-updated').textContent = attempts.oversized_state ? 'Attempt ledger is too large to read' : attempts.reason || 'Attempt ledger unavailable';
      return;
    }
    if (!attempts.total) {
      $('#project-attempts-updated').textContent = 'No draft attempt ledger recorded';
      summary.append(node('p', 'muted', 'Model-driven draft attempts (harness m2c_draft_attempts.py) appear here.'));
      return;
    }
    $('#project-attempts-updated').textContent = `Updated ${date(attempts.updated_at)}`;
    const runtime = attempts.model_runtime || {};
    if (runtime.status) {
      const details = [runtime.model || attempts.model || 'Unknown model'];
      if (runtime.backend) details.push(runtime.backend);
      if (runtime.context_size) details.push(`${number.format(runtime.context_size)} context`);
      if (runtime.latency_ms != null) details.push(`${number.format(runtime.latency_ms)} ms RPC`);
      if (runtime.worker?.pid) details.push(`worker ${runtime.worker.pid}${runtime.worker.elapsed_seconds ? ` · ${duration(runtime.worker.elapsed_seconds)}` : ''}`);
      runtimeRoot.append(node('span', `runtime-status ${runtime.status}`, RUNTIME[runtime.status] || runtime.status), node('span', 'runtime-facts', details.join(' · ')));
      if (runtime.rpc_error) runtimeRoot.append(node('span', 'runtime-error', runtime.rpc_error));
    }
    const activity = node('span');
    activity.append(node('strong', '', attempts.current || 'Idle'));
    if (attempts.current) activity.append(' working');
    summary.append(activity);
    [['Latest best', attempts.current_pct == null ? '-' : `${Number(attempts.current_pct).toFixed(2)}%`],
      ['Last Δ', attempts.current_delta == null ? '-' : `${Number(attempts.current_delta) >= 0 ? '+' : ''}${Number(attempts.current_delta).toFixed(2)}%`],
      ['Tried', `${attempts.attempted}/${attempts.total}`], ['Exact scratch', attempts.exact]].forEach(([label, value]) => {
      const item = node('span'); item.append(node('strong', '', String(value)), ` ${label.toLowerCase()}`); summary.append(item);
    });
    list.append(m2cTable(['Function', 'Attempt', 'Match', 'Delta best', 'Compiler'], (attempts.recent || []).map((attempt) => [
      attempt.symbol, attempt.index === 0 ? 'm2c' : `#${attempt.index ?? '-'}`,
      attempt.compiled ? `${Number(attempt.pct || 0).toFixed(2)}%` : 'Compile failed',
      attempt.delta == null ? '-' : `${Number(attempt.delta) >= 0 ? '+' : ''}${Number(attempt.delta).toFixed(2)} pp`, attempt.profile || '-']),
    {numericColumns: [1, 2, 3], rollupKey: 'attempts'}));
  }

  function render() {
    const data = versionData();
    $('#project-id').textContent = state.version || 'No version';
    if (!data) return;
    const build = data.build || {};
    $('#project-build-status').textContent = build.status === 'exact' ? `Byte-exact ${build.scope || 'build'} · ${date(build.built_at)}`
      : build.status === 'mismatch' ? `Build hash mismatch (${build.scope || 'main output'})` : build.status === 'changing' ? 'Build output is being written'
      : build.status === 'not_built' ? 'Not built' : 'Build not available';
    renderMetrics(data); drawHistory(); renderM2c(data); renderAttempts(data);
    const cache = state.data._cache || {};
    if (cache.error) $('#project-build-status').textContent = `Snapshot stale: ${cache.error}`;
    else if (cache.status === 'loading') $('#project-build-status').textContent = 'Loading snapshot…';
  }

  function applySnapshot(data) {
    state.data = data;
    const versions = Object.keys(data.versions || {});
    if (!versions.includes(state.version)) state.version = versions.includes(data.default_version) ? data.default_version : versions[0] || '';
    const selector = $('#project-version');
    if ([...selector.options].map((option) => option.value).join('\u001f') !== versions.join('\u001f')) {
      selector.replaceChildren(...versions.map((version) => { const option = node('option', '', version); option.value = version; return option; }));
    }
    selector.value = state.version;
    selector.hidden = versions.length < 2;
    $('label[for=project-version]').hidden = versions.length < 2;
    keepViewport(render);
  }

  function pollFarm() {
    let last = null;
    root.setInterval(() => { if (last && !doc().hidden) farmCard(last); }, 1000);
    poll(state.config.permuter_farm.url || '/api/permuter', {
      onData(data) { last = data; keepViewport(() => renderFarm(data)); },
      onUnchanged() { if (last) keepViewport(() => renderFarm(last)); },
      onError() {
        const card = $('#permuter-farm-card');
        if (card) { card.hidden = false; card.className = 'farm-card farm-warn'; card.replaceChildren(node('strong', '', 'Permuter farm'), node('span', 'worker-card-stall', 'Farm status unavailable from the dashboard server')); }
      },
    }, {interval: 5000});
  }

  function boot() {
    const config = root.DashboardConfig || {};
    const projects = config.projects || [];
    const project = projects.find((item) => item.id === config.selected_project) || projects[0];
    if (!project) { doc().querySelector('main').textContent = 'No projects configured. Add projects to the harness config (harness.local.json).'; return; }
    state.config = config; state.project = project;
    const params = new URLSearchParams(root.location.search);
    state.version = params.get('version') || project.default_version || project.versions?.[0] || '';
    doc().body.dataset.project = project.id;
    doc().title = `${project.name} | Decomp Dashboard`;
    skeleton(config, project);
    $('#project-name').textContent = project.name;
    $('#project-id').textContent = state.version;
    $('#project-version').addEventListener('change', (event) => {
      state.version = event.target.value;
      const url = new URL(root.location.href); url.searchParams.set('version', state.version); root.history.replaceState(null, '', url);
      render();
    });
    $('#history-floor').addEventListener('input', drawHistory);
    initTheme();
    root.addEventListener('resize', () => { drawHistory(); drawM2cHistory(versionData()?.m2c || {}); });
    poll(`/api/projects/${encodeURIComponent(project.id)}`, {
      onData: applySnapshot,
      onError(error) { $('#project-build-status').textContent = `Dashboard unavailable: ${error.message}`; },
    }, {interval: 1000});
    poll('/api/telemetry', {
      onData: (data) => keepViewport(() => telemetry(data)),
      onUnchanged(response) { const at = response.headers.get('X-Snapshot-At'); if (at) $('#worker-updated').textContent = `Checked ${new Date(at).toLocaleTimeString()}`; },
      onError() { $('#shared-worker').replaceChildren(node('span', 'negative', 'Worker telemetry unavailable')); $('#worker-updated').textContent = ''; },
    }, {interval: 1000});
    if (config.permuter_farm) pollFarm();
    if (root.location.hash) root.setTimeout(() => doc().getElementById(root.location.hash.slice(1))?.scrollIntoView(), 50);
  }

  /** Light/dark follows the OS (prefers-color-scheme) unless the header toggle stored an explicit choice. */
  function initTheme() {
    const KEY = 'dashboard-theme';
    const query = root.matchMedia ? root.matchMedia('(prefers-color-scheme: dark)') : null;
    const stored = () => { try { const value = root.localStorage.getItem(KEY); return value === 'dark' || value === 'light' ? value : null; } catch (_) { return null; } };
    const current = () => stored() || (query?.matches ? 'dark' : 'light');
    const apply = () => {
      const theme = current();
      if (stored()) doc().documentElement.dataset.theme = theme; else delete doc().documentElement.dataset.theme;
      const button = $('#theme-toggle');
      button.setAttribute('aria-pressed', String(theme === 'dark'));
      button.dataset.mode = theme;
      button.title = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    };
    $('#theme-toggle').addEventListener('click', () => {
      try { root.localStorage.setItem(KEY, current() === 'dark' ? 'light' : 'dark'); } catch (_) { /* storage unavailable */ }
      apply(); root.dispatchEvent(new Event('resize'));  // canvas charts read their colours from CSS
    });
    query?.addEventListener?.('change', () => { apply(); root.dispatchEvent(new Event('resize')); });
    apply();
  }

  return {node, metric, deltaBadge, matchClass, m2cTable, duration, checkpointAge, stallNotice, visibleUnitsText, visibleProgress,
    gateProgress, refinementProgress, promotions, telemetry, farmHealth, farmVerdict, farmPhase, farmSearchMetrics, farmCard, farmDetail,
    renderFarm, poll, nearest, historySeries, measuredNumber, measuredPercent, precisePercent, m2cScored, boot};
});
