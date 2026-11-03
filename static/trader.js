/* Вкладка «Трейдер»: Time Machine для ордер-блоков, статистика правила OB, сканер сетапов, новости, риск.
   Данные — data/trader.json (trader.py), ручной Time Machine считает в браузере по свечам 4H из data/chart/<T>.json.
   Использует общие помощники страницы: $, api, esc, fmt, pct, dtf, store, chipGroup, TYPE, SIDE, kindTag, openChart. */
(() => {
  const root = $('#v-trader');
  if (!root) return;
  const n0 = x => x == null ? '—' : Math.round(x).toLocaleString('ru-RU');
  const p0 = x => x == null ? '—' : Math.round(x) + '%';
  const r2 = x => x == null ? '—' : (x > 0 ? '+' : '') + Number(x).toFixed(2).replace('.', ',') + 'R';
  const SETUP = {breakout_20d: ['Пробой 20 дн.', 'Закрытие выше всех закрытий 20 предыдущих дней'],
    bull_flag: ['Бычий флаг', 'Рост > 10% за 10 дней, затем 5 закрытий в коридоре < 5%'],
    reclaim_ma20: ['Над MA20', 'Вчера закрытие ниже 20-дневной средней, сегодня — выше'],
    oversold: ['RSI < 30', 'RSI 14 (Уайлдер) ниже 30'],
    oversold_bounce: ['Отскок', 'Вчера RSI < 30, сегодня закрытие выше вчерашнего']};
  const LEAN = {2: 'bull', 1: 'bull', 0: '', '-1': 'bear', '-2': 'bear'};

  root.innerHTML = `
    <div class="toolbar">
      <div class="chips" id="trSub">
        <button class="chip on" data-f="all">Time Machine</button>
        <button class="chip" data-f="scan">Сканер сетапов</button>
        <button class="chip" data-f="news">Новости</button>
        <button class="chip" data-f="risk">Риск</button>
      </div>
      <span class="count" id="trUpd"></span>
    </div>

    <div id="trTm">
      <div class="card tr-pad" id="trRules"><div class="empty">Загрузка…</div></div>

      <div class="card tr-pad">
        <div class="tr-h">Свой план <span class="muted">— прогоны считаются здесь, в браузере, по свечам 4H акции (последние ~200 дней)</span></div>
        <div class="tr-form">
          <label>Тикер<input type="text" id="tmT" list="tmList" placeholder="AAPL" autocomplete="off" style="width:90px;text-transform:uppercase"></label>
          <datalist id="tmList"></datalist>
          <label>Сторона<select id="tmSide"><option value="long">Покупка</option><option value="short">Продажа</option></select></label>
          <label>Вход<input type="number" id="tmE" step="any" style="width:96px"></label>
          <label>Вход по<select id="tmHow"><option value="market">закрытию</option><option value="limit">лимиту</option></select></label>
          <label>Стоп<input type="number" id="tmS" step="any" style="width:96px"></label>
          <label>Цель, R<input type="number" id="tmR" step="0.5" min="0.5" value="2" style="width:64px"></label>
          <label>Свечей 4H<input type="number" id="tmH" step="1" min="2" max="120" value="20" style="width:64px" title="20 свечей ≈ 10 торговых дней"></label>
          <button class="btn primary" id="tmRun">Прогнать 10 000 раз</button>
        </div>
        <div id="tmOut" class="tr-out muted">Введите тикер, вход и стоп. Цель = вход ± R × риск. Лимит — ждём, пока цена дойдёт до входа.</div>
      </div>

      <div class="toolbar">
        <div class="chips" id="trType">
          <button class="chip on" data-f="all">Все сигналы</button>
          <button class="chip" data-f="formed">Новые OB</button>
          <button class="chip" data-f="touched">Касания</button>
        </div>
        <div class="chips" id="trLean">
          <button class="chip on" data-f="all">Любой вердикт</button>
          <button class="chip" data-f="up">К цели</button>
          <button class="chip" data-f="down">К стопу</button>
        </div>
        <input type="search" id="trSearch" placeholder="Тикер…" style="width:120px">
        <span class="count" id="trCount"></span>
      </div>
      <div class="card tbl-wrap">
        <table>
          <thead><tr>
            <th>Свеча</th><th>Тикер</th><th>Сигнал</th>
            <th class="r hide-sm" title="Вход · стоп · цель 2R. Новый OB — лимит на границе блока, касание — по закрытию">План</th>
            <th class="r" title="Доля прогонов, где цена дошла до лимитного входа за 20 свечей">Вход</th>
            <th title="Чем закончились прогоны после входа: цель / стоп / ещё открыта">Прогоны</th>
            <th class="r" title="Среди прогонов, дошедших до цели или стопа, — доля, где цель была первой. Безубыток при 2R — 33%">Цель раньше стопа</th>
            <th class="r" title="То же правило на прошлых OB этой акции (настоящие свечи): целей из (целей + стопов)">История</th>
            <th class="r hide-sm" title="Средний результат прогонов после входа, в рисках">Ср. R</th>
            <th>Вердикт</th>
          </tr></thead>
          <tbody id="trBody"><tr><td colspan="10" class="empty">Загрузка…</td></tr></tbody>
        </table>
      </div>
      <p class="tr-note">Прогоны собирают следующие 20 свечей из настоящих свечей этой же акции за последний год — поэтому несут её тренд за год.
        Главная проверка — «История» и статистика правила по всем акциям выше. Это симуляция по прошлому, не прогноз и не рекомендация.</p>
    </div>

    <div id="trScan" hidden>
      <div class="toolbar">
        <div class="chips" id="trSet">
          <button class="chip on" data-f="all">Все</button>
          ${Object.entries(SETUP).map(([k, v]) => `<button class="chip" data-f="${k}" title="${v[1]}">${v[0]}</button>`).join('')}
        </div>
        <span class="switch" id="trScOb" role="switch" aria-checked="false" title="Только акции с сигналом ордер-блока за неделю"><i></i>Есть OB</span>
        <span class="count" id="trScCount"></span>
      </div>
      <div class="card tbl-wrap">
        <table>
          <thead><tr><th>Тикер</th><th class="r">Цена</th><th class="r">За день</th><th class="r" title="RSI 14 по дневным закрытиям">RSI</th>
            <th>Сетапы</th><th class="r hide-sm" title="Средний дневной оборот за 20 дней">Оборот</th><th title="Сигнал OB за последнюю неделю">OB</th></tr></thead>
          <tbody id="trScBody"></tbody>
        </table>
      </div>
      <p class="tr-note">Дневные закрытия собраны из свечей 4H (S&amp;P 500 и Nasdaq-100), незакрытый день не учитывается. Сетап — совпадение с правилом, не сигнал к покупке.</p>
    </div>

    <div id="trNews" hidden>
      <div class="toolbar"><input type="search" id="trNwSearch" placeholder="Тикер…" style="width:120px"><span class="count" id="trNwCount"></span></div>
      <div id="trNwBody"></div>
      <p class="tr-note">Заголовки Yahoo Finance за 7 дней и документы 8-K в SEC за 14 дней — по акциям с сигналами OB за последние 3 дня.</p>
    </div>

    <div id="trRisk" hidden>
      <div class="tr-grid">
        <div class="card tr-pad">
          <div class="tr-h">Акции</div>
          <div class="tr-form col">
            <label>Счёт, $<input type="number" id="rkAcc" step="any" value="10000"></label>
            <label>Риск на сделку, %<input type="number" id="rkPct" step="0.1" value="1"></label>
            <label>Сторона<select id="rkSide"><option value="long">Покупка</option><option value="short">Продажа</option></select></label>
            <label>Вход<input type="number" id="rkE" step="any"></label>
            <label>Стоп<input type="number" id="rkS" step="any"></label>
            <label>Цель, R<input type="number" id="rkR" step="0.5" value="2"></label>
            <label>Макс. позиция, % счёта<input type="number" id="rkCap" step="1" value="25"></label>
          </div>
          <div id="rkOut" class="tr-out"></div>
        </div>
        <div class="card tr-pad">
          <div class="tr-h">Опционы</div>
          <div class="tr-form col">
            <label>Макс. убыток на 1 контракт, $<input type="number" id="ocLoss" step="any" placeholder="бабочка: премия × 100" title="Купленная бабочка или дебетовый спред — уплаченная премия × 100. Проданный пут-спред — (ширина − кредит) × 100"></label>
            <label>Макс. прибыль на 1 контракт, $<input type="number" id="ocGain" step="any" placeholder="необязательно"></label>
          </div>
          <div id="ocOut" class="tr-out"></div>
          <div class="tr-h" style="margin-top:16px">День</div>
          <div class="tr-form col">
            <label>Дневной лимит убытка, %<input type="number" id="dlPct" step="0.5" value="3"></label>
          </div>
          <div id="dlOut" class="tr-out"></div>
        </div>
      </div>
      <p class="tr-note">Числа сохраняются только в этом браузере. Стоп — план, а не гарантия: при гэпе исполнение бывает хуже.</p>
    </div>`;

  const css = document.createElement('style');
  css.textContent = `
    #v-trader .tr-pad{padding:14px 16px;margin-bottom:12px}
    #v-trader .tr-h{font-weight:600;margin-bottom:10px}
    #v-trader .muted{color:var(--muted);font-weight:400}
    #v-trader .tr-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
    #v-trader .tr-form.col{display:grid;grid-template-columns:1fr;gap:8px}
    #v-trader .tr-form label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}
    #v-trader .tr-form.col label{flex-direction:row;justify-content:space-between;align-items:center}
    #v-trader .tr-form input,#v-trader .tr-form select{border:1px solid var(--line);background:var(--panel);color:var(--text);font:inherit;font-size:13.5px;padding:6px 8px;border-radius:8px;min-width:0}
    #v-trader .tr-form.col input{width:130px;text-align:right}
    #v-trader .tr-out{margin-top:12px;font-size:13.5px;line-height:1.6}
    #v-trader .tr-out b{font-variant-numeric:tabular-nums}
    #v-trader .tr-note{color:var(--muted);font-size:12.5px;margin:10px 2px 0}
    #v-trader .tr-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    @media (max-width:900px){#v-trader .tr-grid{grid-template-columns:1fr}}
    #v-trader .sb{display:flex;width:120px;height:10px;border-radius:3px;overflow:hidden;background:var(--panel2)}
    #v-trader .sb i{display:block;height:100%}
    #v-trader .sb .t{background:var(--bull)} #v-trader .sb .s{background:var(--bear)} #v-trader .sb .o{background:var(--muted);opacity:.45}
    #v-trader .vd{font-weight:600;font-size:12.5px} #v-trader .vd.bull{color:var(--bull)} #v-trader .vd.bear{color:var(--bear)}
    #v-trader .tag{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:999px;background:var(--panel2);color:var(--muted);margin-right:4px}
    #v-trader .rules{width:100%;border-collapse:collapse;font-size:13.5px}
    #v-trader .rules td,#v-trader .rules th{position:static;padding:6px 10px}
    #v-trader .rules td:first-child{white-space:normal;min-width:190px}
    #v-trader .nw{padding:12px 16px;margin-bottom:10px}
    #v-trader .nw-h{display:flex;gap:10px;align-items:center;margin-bottom:6px;font-weight:600}
    #v-trader .nw-i{display:flex;gap:10px;padding:5px 0;border-top:1px solid var(--line);font-size:13.5px;align-items:baseline}
    #v-trader .nw-i time{color:var(--muted);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums;min-width:82px}
    #v-trader .src{font:600 10px/1.6 "JetBrains Mono",monospace;padding:0 5px;border-radius:4px;background:var(--panel2);color:var(--muted)}
    #v-trader .src.k{color:var(--accent)}
    #v-trader .nw-i a{text-decoration:none} #v-trader .nw-i a:hover{text-decoration:underline}`;
  document.head.appendChild(css);

  let D = null, loading = null;
  const sub = chipGroup('#trSub', paintSub);
  function paintSub() {
    const v = sub();
    $('#trTm').hidden = v !== 'all';
    $('#trScan').hidden = v !== 'scan';
    $('#trNews').hidden = v !== 'news';
    $('#trRisk').hidden = v !== 'risk';
  }
  paintSub();

  async function load() {
    if (D || loading) return loading;
    loading = (async () => {
      try { D = await api('/api/trader'); } catch (e) { D = null; }
      if (!D || D.error) {
        $('#trRules').innerHTML = '<div class="empty">Данных пока нет — они появятся после ближайшей проверки свечей 4H.</div>';
        $('#trBody').innerHTML = '<tr><td colspan="10" class="empty">Нет данных</td></tr>';
        return;
      }
      $('#trUpd').textContent = 'обновлено ' + dtf(D.updated) + ' ET';
      renderRules(); renderTm(); renderScan(); renderNews();
    })();
    try { const snap = await api('/api/snapshot'); $('#tmList').innerHTML = (snap.rows || []).map(r => `<option value="${esc(r.ticker)}">`).join(''); } catch (e) {}
    return loading;
  }
  window.trShow = load;

  /* ---------- статистика правила ---------- */
  function renderRules() {
    const R = D.rules || {}, I = D.rules_info || {};
    const NAME = {formed_bull: 'Новый бычий OB → покупка лимитом у верхней границы', formed_bear: 'Новый медвежий OB → продажа лимитом у нижней границы',
      touched_bull: 'Касание бычьего OB → покупка по закрытию', touched_bear: 'Касание медвежьего OB → продажа по закрытию'};
    const rows = Object.keys(NAME).filter(k => R[k]).map(k => {
      const a = R[k], lean = a.first == null ? '' : a.first - I.be >= 5 ? 'bull' : I.be - a.first >= 5 ? 'bear' : '';
      return `<tr><td>${NAME[k]}</td><td class="r mono">${n0(a.n)}</td><td class="r mono">${p0(a.fill)}</td>
        <td class="r mono">${n0(a.target)} / ${n0(a.stop)} / ${n0(a.open)}</td>
        <td class="r mono"><b class="vd ${lean}">${p0(a.first)}</b></td><td class="r mono hide-sm">${r2(a.avg_r)}</td>
        <td><span class="vd ${lean}">${esc(a.verdict || '')}</span></td></tr>`;
    }).join('');
    $('#trRules').innerHTML = `<div class="tr-h">Работают ли ордер-блоки: правило на всех прошлых OB, ${n0(I.tickers)} акций
        <span class="muted">— настоящие свечи 4H за ~2 года, стоп за границей блока, цель 2R, 20 свечей</span></div>
      <div class="tbl-wrap"><table class="rules"><thead><tr><th>Правило</th><th class="r">OB</th><th class="r" title="Доля OB, где цена дошла до входа">Вход</th>
        <th class="r" title="Целей / стопов / открытых к концу 20 свечей">Цель / стоп / открыта</th>
        <th class="r" title="Цель раньше стопа среди дошедших. Безубыток при 2R — ${I.be}%">Цель раньше стопа</th><th class="r hide-sm">Ср. R</th><th>Вердикт</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="empty">Нет данных</td></tr>'}</tbody></table></div>`;
  }

  /* ---------- сигналы ---------- */
  const fType = chipGroup('#trType', renderTm), fLean = chipGroup('#trLean', renderTm);
  $('#trSearch').oninput = renderTm;
  function bar(mc) {
    if (mc.target == null) return '';
    return `<span class="sb" title="Цель ${p0(mc.target)} · стоп ${p0(mc.stop)} · открыта ${p0(mc.open)}"><i class="t" style="width:${mc.target}%"></i><i class="s" style="width:${mc.stop}%"></i><i class="o" style="width:${mc.open}%"></i></span>`;
  }
  function renderTm() {
    if (!D) return;
    const q = $('#trSearch').value.trim().toUpperCase(), ty = fType(), ln = fLean();
    const rows = (D.tm || []).filter(r => (ty === 'all' || r.type === ty) && (!q || r.ticker.includes(q))
      && (ln === 'all' || (ln === 'up' ? r.mc.lean > 0 : r.mc.lean < 0)));
    $('#trCount').textContent = rows.length + ' из ' + (D.tm || []).length;
    $('#trBody').innerHTML = rows.map(r => {
      const p = r.plan, mc = r.mc, h = r.hist, hit = h.target + h.stop;
      const hl = hit >= 5 ? (h.target / hit * 100 - mc.be >= 5 ? 'bull' : mc.be - h.target / hit * 100 >= 5 ? 'bear' : '') : '';
      return `<tr>
        <td class="mono">${dtf(r.bar_time)}</td>
        <td><a href="#chart/${esc(r.ticker)}"><b>${esc(r.ticker)}</b></a></td>
        <td><span class="vd ${r.side}">${r.side === 'bull' ? '▲' : '▼'}</span> ${TYPE[r.type]} ${kindTag(r)}</td>
        <td class="r mono hide-sm" title="${p.side === 'long' ? 'Покупка' : 'Продажа'} ${p.how === 'limit' ? 'лимитом' : 'по закрытию'}, риск ${String(p.risk_pct).replace('.', ',')}%">${fmt(p.entry)} · <span style="color:var(--bear)">${fmt(p.stop)}</span> · <span style="color:var(--bull)">${fmt(p.target)}</span></td>
        <td class="r mono">${p.how === 'limit' ? p0(mc.fill) : '<span class="muted">сразу</span>'}</td>
        <td>${bar(mc)}</td>
        <td class="r mono">${p0(mc.first)} <span class="muted">/ ${Math.round(mc.be)}</span></td>
        <td class="r mono"><span class="vd ${hl}">${hit ? h.target + ' из ' + hit : '—'}</span></td>
        <td class="r mono hide-sm">${r2(mc.avg_r)}</td>
        <td><span class="vd ${LEAN[mc.lean]}">${esc(mc.verdict)}</span></td></tr>`;
    }).join('') || '<tr><td colspan="10" class="empty">Сигналов за неделю нет</td></tr>';
  }

  /* ---------- ручной Time Machine (в браузере) ---------- */
  function rng(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  function simulate(c, plan, H, N = 10000) {
    const pool = [[], []];
    const from = Math.max(1, c.length - 500);
    for (let i = from; i < c.length; i++) {
      const pc = c[i - 1].close; if (!(pc > 0)) continue;
      const slot = new Date(c[i].time * 1000).getUTCHours() >= 12 ? 1 : 0;
      pool[slot].push([c[i].open / pc, c[i].high / pc, c[i].low / pc, c[i].close / pc]);
    }
    if (pool[0].length + pool[1].length < 60) return null;
    if (Math.min(pool[0].length, pool[1].length) < 20) { const all = pool[0].concat(pool[1]); pool[0] = all; pool[1] = all; }
    const R = rng(19), long = plan.side === 'long', risk = Math.abs(plan.entry - plan.stop);
    let tg = 0, st = 0, op = 0, nf = 0, sumR = 0;
    const slot0 = new Date(c[c.length - 1].time * 1000).getUTCHours() >= 12 ? 0 : 1;
    for (let k = 0; k < N; k++) {
      let p = plan.how === 'market' ? plan.entry : c[c.length - 1].close, filled = plan.how === 'market', out = 0, s = slot0;
      for (let j = 0; j < H; j++) {
        const pl = pool[s], b = pl[(R() * pl.length) | 0];
        const h = p * b[1], l = p * b[2];
        const nfill = !filled && (long ? l <= plan.entry : h >= plan.entry);
        if (filled || nfill) {
          filled = true;
          if (long ? l <= plan.stop : h >= plan.stop) { out = 2; break; }
          if (long ? h >= plan.target : l <= plan.target) { out = 1; break; }
        }
        p = p * b[3]; s ^= 1;
      }
      if (!filled) continue;
      nf++;
      if (out === 1) { tg++; sumR += plan.r; } else if (out === 2) { st++; sumR -= 1; }
      else { op++; sumR += (long ? p - plan.entry : plan.entry - p) / risk; }
    }
    return {n: N, fill: nf / N * 100, target: nf ? tg / nf * 100 : null, stop: nf ? st / nf * 100 : null,
      open: nf ? op / nf * 100 : null, first: tg + st ? tg / (tg + st) * 100 : null, be: 100 / (1 + plan.r), avg_r: nf ? sumR / nf : null, bars: c.length};
  }
  const tmKeys = ['tmT', 'tmSide', 'tmE', 'tmHow', 'tmS', 'tmR', 'tmH'];
  tmKeys.forEach(k => { const v = store.get('tr.' + k, null); if (v !== null) $('#' + k).value = v; $('#' + k).onchange = () => store.set('tr.' + k, $('#' + k).value); });
  $('#tmRun').onclick = async () => {
    const t = $('#tmT').value.trim().toUpperCase(), side = $('#tmSide').value, how = $('#tmHow').value;
    const entry = +$('#tmE').value, stop = +$('#tmS').value, r = +$('#tmR').value || 2, H = Math.min(120, Math.max(2, +$('#tmH').value || 20));
    const out = $('#tmOut');
    if (!t || !(entry > 0) || !(stop > 0)) { out.innerHTML = '<span class="warnc">Нужны тикер, вход и стоп.</span>'; return; }
    if (side === 'long' ? stop >= entry : stop <= entry) { out.innerHTML = `<span class="warnc">Стоп должен быть ${side === 'long' ? 'ниже' : 'выше'} входа.</span>`; return; }
    const risk = Math.abs(entry - stop), target = side === 'long' ? entry + r * risk : entry - r * risk;
    out.innerHTML = 'Считаю…';
    let ch; try { ch = await api('/api/chart/' + encodeURIComponent(t)); } catch (e) { ch = null; }
    if (!ch || ch.error || !ch.candles) { out.innerHTML = `<span class="warnc">${esc(ch && ch.error || 'Нет свечей по ' + t)}</span>`; return; }
    const last = ch.candles[ch.candles.length - 1].close;
    const plan = {side, how, entry, stop, target, r};
    const m = simulate(ch.candles, plan, H);
    if (!m) { out.innerHTML = '<span class="warnc">Слишком мало свечей для прогонов.</span>'; return; }
    const lean = m.first == null ? '' : m.first - m.be >= 5 ? 'bull' : m.be - m.first >= 5 ? 'bear' : '';
    const vd = m.first == null ? 'вход почти не срабатывает' : Math.abs(m.first - m.be) < 5 ? 'около безубытка'
      : m.first > m.be ? (m.first - m.be < 15 ? 'склоняется к цели' : 'сильно к цели') : (m.be - m.first < 15 ? 'склоняется к стопу' : 'сильно к стопу');
    out.innerHTML = `<b>${esc(t)}</b> · последнее закрытие ${fmt(last)} · цель <b>${fmt(target)}</b> (${r}R), риск ${(risk / entry * 100).toFixed(2).replace('.', ',')}% · ${H} свечей 4H, 10 000 прогонов из ${Math.min(500, m.bars - 1)} свечей<br>
      ${how === 'limit' ? `Вход сработал в <b>${p0(m.fill)}</b> прогонов. ` : ''}После входа: цель <b style="color:var(--bull)">${p0(m.target)}</b> · стоп <b style="color:var(--bear)">${p0(m.stop)}</b> · открыта к концу ${p0(m.open)} ${bar(m)}<br>
      Цель раньше стопа <b class="vd ${lean}">${p0(m.first)}</b> при безубытке ${Math.round(m.be)}% · средний результат <b>${r2(m.avg_r)}</b> → <span class="vd ${lean}">${vd}</span>
      <div class="tr-note" style="margin:6px 0 0">Прогоны несут тренд акции за период свечей. Не прогноз.</div>`;
  };

  /* ---------- сканер ---------- */
  const fSet = chipGroup('#trSet', renderScan);
  let scOb = store.get('trScOb', false);
  function setScOb(v) { scOb = v; store.set('trScOb', v); const el = $('#trScOb'); el.classList.toggle('on', v); el.setAttribute('aria-checked', v); renderScan(); }
  $('#trScOb').onclick = () => setScOb(!scOb);
  $('#trScOb').classList.toggle('on', scOb);
  function renderScan() {
    if (!D) return;
    const sc = D.scan || {rows: []}, f = fSet();
    const rows = sc.rows.filter(r => (f === 'all' || r.tags.includes(f)) && (!scOb || r.ob));
    $('#trScCount').textContent = rows.length + ' акций' + (sc.as_of ? ' · закрытие ' + sc.as_of.slice(8, 10) + '.' + sc.as_of.slice(5, 7) : '');
    $('#trScBody').innerHTML = rows.map(r => `<tr>
      <td><a href="#chart/${esc(r.ticker)}"><b>${esc(r.ticker)}</b></a></td><td class="r mono">${fmt(r.close)}</td>
      <td class="r mono ${r.chg >= 0 ? 'better' : 'worse'}">${pct(r.chg)}</td><td class="r mono">${r.rsi == null ? '—' : String(r.rsi).replace('.', ',')}</td>
      <td>${r.tags.map(k => `<span class="tag" title="${esc((SETUP[k] || ['', ''])[1])}">${esc((SETUP[k] || [k])[0])}</span>`).join('')}</td>
      <td class="r mono hide-sm">$${n0(r.dvol / 1e6)} млн</td><td>${esc(r.ob || '')}</td></tr>`).join('')
      || '<tr><td colspan="7" class="empty">Нет совпадений</td></tr>';
  }

  /* ---------- новости ---------- */
  $('#trNwSearch').oninput = renderNews;
  function renderNews() {
    if (!D) return;
    const nw = D.news || {items: {}}, q = $('#trNwSearch').value.trim().toUpperCase();
    const order = (nw.tickers || Object.keys(nw.items)).filter(t => nw.items[t] && (!q || t.includes(q)));
    const sig = {};
    (D.tm || []).forEach(r => { if (!sig[r.ticker]) sig[r.ticker] = r; });
    $('#trNwCount').textContent = order.length + ' акций с новостями' + (nw.errors && nw.errors.length ? ' · часть источников не ответила' : '');
    $('#trNwBody').innerHTML = order.map(t => {
      const s = sig[t];
      return `<div class="card nw"><div class="nw-h"><a href="#chart/${esc(t)}">${esc(t)}</a>
        ${s ? `<span class="vd ${s.side}">${s.side === 'bull' ? '▲' : '▼'} ${TYPE[s.type]}</span><span class="muted" style="font-weight:400;font-size:12.5px">${dtf(s.bar_time)}</span>` : ''}</div>
        ${nw.items[t].slice(0, 8).map(it => {
          const u = /^https?:\/\//.test(it.url || '') ? it.url : '#';
          return `<div class="nw-i"><time>${esc(String(it.time).slice(5, 16).replace('T', ' ').replace(/^(\d\d)-(\d\d)/, '$2.$1'))}</time>
          <span class="src ${it.src === '8-K' ? 'k' : ''}">${it.src === '8-K' ? 'SEC' : 'YH'}</span>
          <span>${(it.tags || []).map(g => `<span class="tag">${esc(g)}</span>`).join('')}<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a></span></div>`;
        }).join('')}</div>`;
    }).join('') || '<div class="card empty">Новостей по акциям с сигналами нет' + (D.news && D.news.errors && D.news.errors.length ? ' — источники не ответили при последней проверке' : '') + '</div>';
  }

  /* ---------- риск ---------- */
  const rkKeys = ['rkAcc', 'rkPct', 'rkSide', 'rkE', 'rkS', 'rkR', 'rkCap', 'ocLoss', 'ocGain', 'dlPct'];
  rkKeys.forEach(k => { const v = store.get('tr.' + k, null); if (v !== null) $('#' + k).value = v; $('#' + k).oninput = () => { store.set('tr.' + k, $('#' + k).value); risk(); }; });
  $('#rkSide').onchange = () => { store.set('tr.rkSide', $('#rkSide').value); risk(); };
  const usd = x => '$' + Number(x).toLocaleString('ru-RU', {maximumFractionDigits: 0});
  function risk() {
    const acc = +$('#rkAcc').value, pc = +$('#rkPct').value, e = +$('#rkE').value, s = +$('#rkS').value, r = +$('#rkR').value || 2;
    const cap = +$('#rkCap').value || 100, long = $('#rkSide').value === 'long';
    const budget = acc * pc / 100;
    let html = `Риск на сделку: <b>${usd(budget)}</b>`;
    if (e > 0 && s > 0) {
      if (long ? s >= e : s <= e) html += `<br><span class="warnc">Стоп должен быть ${long ? 'ниже' : 'выше'} входа.</span>`;
      else {
        const per = Math.abs(e - s), byRisk = Math.floor(budget / per), byCap = Math.floor(acc * cap / 100 / e);
        const n = Math.max(0, Math.min(byRisk, byCap)), tgt = long ? e + r * per : e - r * per;
        html += `<br>Акций: <b>${n0(n)}</b> ${byCap < byRisk ? `<span class="warnc">(ограничено позицией ${cap}% счёта)</span>` : ''}
          <br>Позиция: <b>${usd(n * e)}</b> (${acc ? (n * e / acc * 100).toFixed(1).replace('.', ',') : '—'}% счёта)
          <br>Убыток на стопе: <b style="color:var(--bear)">${usd(n * per)}</b> · на цели ${fmt(tgt)}: <b style="color:var(--bull)">${usd(n * per * r)}</b>
          <br>Риск на акцию ${fmt(per)} (${(per / e * 100).toFixed(2).replace('.', ',')}%)`;
      }
    }
    $('#rkOut').innerHTML = html;
    const loss = +$('#ocLoss').value, gain = +$('#ocGain').value;
    $('#ocOut').innerHTML = loss > budget ? `<span class="warnc">Один контракт рискует ${usd(loss)} — больше риска на сделку ${usd(budget)}.</span> Уменьшите ширину/премию или поднимите риск до ${acc ? (loss / acc * 100).toFixed(2).replace('.', ',') : '—'}%.`
      : loss > 0 ? `Контрактов: <b>${n0(Math.floor(budget / loss))}</b> при риске ${usd(budget)}
      <br>Макс. убыток: <b style="color:var(--bear)">${usd(Math.floor(budget / loss) * loss)}</b>${gain > 0 ? ` · макс. прибыль: <b style="color:var(--bull)">${usd(Math.floor(budget / loss) * gain)}</b> (${(gain / loss).toFixed(1).replace('.', ',')}R)` : ''}`
      : '<span class="muted">Введите макс. убыток на один контракт.</span>';
    const dl = +$('#dlPct').value;
    $('#dlOut').innerHTML = dl > 0 && pc > 0 ? `Лимит дня: <b>${usd(acc * dl / 100)}</b> — это <b>${Math.floor(dl / pc)}</b> стопов подряд при риске ${pc}% на сделку.` : '';
  }
  risk();

  if (decodeURIComponent(location.hash.slice(1)) === 'trader') load();
})();
