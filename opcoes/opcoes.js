/* Tela "Opções" — Scanner Wheel (venda de PUT/CALL) e Calls Longas. Depende de opcore.js e lightweight-charts. */
(function () {
  "use strict";
  const C = window.OPCORE;
  const $ = (s, r = document) => r.querySelector(s);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const NF = d => new Intl.NumberFormat("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const F0 = NF(0), F1 = NF(1), F2 = NF(2), F3 = NF(3);
  const fx = (v, f = F2) => (Number.isFinite(v) ? f.format(v) : "n/d");
  const pct = v => (Number.isFinite(v) ? F0.format(v * 100) + "%" : "n/d");
  const ddmmyy = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ""); return m ? `${m[3]}/${m[2]}/${m[1].slice(2)}` : s; };
  const store = {
    get(k, d) { try { const v = localStorage.getItem("smc.op." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("smc.op." + k, JSON.stringify(v)); } catch (e) {} }
  };
  const COL = { up: "#26c281", dn: "#ef5350", amb: "#f5a524", acc: "#4da3ff", mut: "#8793a4", hv: "#c084fc", venc: "#f472b6" };

  // ---------------------------------------------------------------- parâmetros (persistem no aparelho)
  const PRESETS = { SAFE: [0.10, 0.25], WHEEL: [0.20, 0.32], AGRESSIVO: [0.25, 0.55], RENDA: [0.08, 0.20] };
  const DEFAULTS = {
    mode: "wheel",
    wheel: { ativo: "PETR4", modo: "WHEEL", dteMin: 7, dteMax: 45, negMin: 10, range: "10%", premio: "3%",
             putMin: 0.20, putMax: 0.32, callMin: 0.20, callMax: 0.32 },
    long: { todos: true, ativo: "PETR4", range: "20%", premio: "15%", desc: 10, neg: 1, top: 15, dteMin: 365, dteMax: 730 },
    model: { selicAuto: true, selic: 14, alvoAlta: 20, premioMin: 0.03, idadeMax: 4, idadeMaxLong: 10, projDias: 0, projGap: 1, projV: 0 }
  };
  const clone = o => JSON.parse(JSON.stringify(o));
  function merge(base, over) {
    const o = clone(base);
    if (over && typeof over === "object") for (const k in o) {
      if (over[k] === undefined) continue;
      if (o[k] && typeof o[k] === "object") o[k] = merge(o[k], over[k]); else o[k] = over[k];
    }
    return o;
  }
  let P = merge(DEFAULTS, store.get("p", {}));
  if (P.model.projV !== 2) { P.model.projDias = 0; P.model.projGap = 1; P.model.projV = 2; store.set("p", P); }   // migra o padrão antigo (1 dia = só um traço)
  const save = () => store.set("p", P);

  // ---------------------------------------------------------------- estado
  let META = null, selicLive = null, built = false, runId = 0, timer = 0;
  const cacheA = {}, cacheP = {};
  let cur = { put: [], call: [], long: [] }, sel = null;
  // varredura da B3 (aba Wheel)
  const TOPN = 80, TOPP = 8;
  let RANK = [], view = "scan", SCANA = null, scanning = false, scanDone = 0, showCalls = false;
  let ch = null, cs = null, vs = null, e9 = null, vw = null, projS = [], lines = [], chAtivo = null;

  async function jget(path, bust) {
    const r = await fetch("data/opcoes/" + path + (bust ? "?v=" + encodeURIComponent(bust) : ""), bust ? {} : { cache: "no-store" });
    if (!r.ok) throw new Error(path + " → HTTP " + r.status);
    return r.json();
  }
  const brtDateMs = iso => { const x = new Date(new Date(iso).getTime() - 3 * 3600e3); return Date.UTC(x.getUTCFullYear(), x.getUTCMonth(), x.getUTCDate()); };

  function loadAsset(t) {
    if (!cacheA[t]) {
      cacheA[t] = jget(t + ".json", META.updated_at).then(a => {
        const rows = a.rows.map(r => { const o = {}; a.cols.forEach((c, i) => { o[c] = r[i]; }); return o; });
        const n = v => (v == null ? NaN : v);
        return { ativo: a.ativo, S: a.S, hv20: n(a.hv20), hv60: n(a.hv60), hv: n(a.hv), rows, fetched_at: a.fetched_at, hojeMs: brtDateMs(a.fetched_at) };
      }).catch(e => { delete cacheA[t]; throw e; });
    }
    return cacheA[t];
  }
  let LONG = null;   // promessa do longas.json (todas as calls de 60–730 dias, num só arquivo)
  function loadLong() {
    if (!LONG) {
      LONG = jget("longas.json", META.updated_at).then(j => {
        const out = {}, n = v => (v == null ? NaN : v);
        for (const t in j.assets) {
          const a = j.assets[t];
          out[t] = { ativo: t, S: a.S, hv: n(a.hv), fetched_at: a.fetched_at, hojeMs: brtDateMs(a.fetched_at),
                     rows: a.rows.map(r => { const o = {}; j.cols.forEach((c, i) => { o[c] = r[i]; }); return o; }) };
        }
        return out;
      }).catch(() => null);      // coleta antiga sem longas.json: cai no caminho lento (um arquivo por ativo)
    }
    return LONG;
  }
  function loadPx(t) {
    if (!cacheP[t]) {
      cacheP[t] = jget(t + "_px.json", META.updated_at).then(j => j.candles.map(c => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4], volume: c[5] })))
        .catch(e => { delete cacheP[t]; throw e; });
    }
    return cacheP[t];
  }

  // ---------------------------------------------------------------- Selic (automática)
  async function fetchSelic() {
    try {
      const ctl = new AbortController(); const to = setTimeout(() => ctl.abort(), 5000);
      const r = await fetch("https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json", { signal: ctl.signal });
      clearTimeout(to);
      const j = await r.json(); const u = j[j.length - 1];
      const v = parseFloat(String(u.valor).replace(",", ".")) / 100;
      if (v > 0.01 && v < 0.5) { selicLive = { v, data: u.data }; return true; }
    } catch (e) { /* cai para o valor do último build */ }
    return false;
  }
  function selicInfo() {
    if (!P.model.selicAuto && P.model.selic > 0) return { v: P.model.selic / 100, src: "manual" };
    if (selicLive) return { v: selicLive.v, src: "BCB ao vivo" };
    if (META && META.selic != null && META.selic_fonte === "bcb") return { v: META.selic, src: "BCB, última coleta" };
    if (META && META.selic != null && META.selic_fonte === "demo") return { v: META.selic, src: "simulada" };
    return { v: C.DEF.SELIC_PADRAO, src: "padrão" };
  }
  function cfg() {
    const m = P.model;
    return { selic: selicInfo().v, ALVO_ALTA: m.alvoAlta / 100, PREMIO_MIN: m.premioMin, HV_NEUTRA: C.DEF.HV_NEUTRA,
             IDADE_MAX_DIAS: m.idadeMax, IDADE_MAX_LONGAS: m.idadeMaxLong, PROJ_DIAS: m.projDias, PROJ_GAP: m.projGap };
  }

  // ---------------------------------------------------------------- construção da tela
  function build() {
    const root = $("#opcoes");
    root.innerHTML = `
      <div class="op">
        <div class="op-top">
          <div class="seg" id="opTabs"><button data-m="wheel">Wheel · vender PUT/CALL</button><button data-m="long">Calls longas</button></div>
          <div class="op-status" id="opStatus">carregando…</div>
        </div>
        <div class="op-ctrl" id="opCtrl"></div>
        <div class="op-body">
          <div class="op-tables" id="opTables"></div>
          <div class="op-chartcol">
            <div class="op-card"><h3><span id="opChTitle">Gráfico</span></h3><div id="opInfo"></div>
              <div id="opChartWrap" style="margin-top:10px"><div id="opChart"></div></div><div class="legend" id="opLegend"></div></div>
          </div>
        </div>
        <p class="hint" id="opDisc">Ferramenta educacional. IV, delta, probabilidades e scores são calculados por Black-Scholes (opção europeia, sem dividendos)
        sobre o último preço negociado de cada opção — opções ilíquidas podem ter preço velho. Não é recomendação de investimento.
        Venda de opções tem risco: PUT pode ser exercida (compra a ação pelo strike) e CALL coberta limita a alta.</p>
      </div>`;
    $("#opTabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { P.mode = b.dataset.m; save(); renderCtrl(); run(); } });
    $("#opCtrl").addEventListener("input", onInput);
    $("#opCtrl").addEventListener("change", onInput);
    $("#opCtrl").addEventListener("click", onCtrlClick);
    $("#opTables").addEventListener("click", onRowClick);
    built = true;
  }

  const fld = (s, k, label, o = {}) => {
    const v = P[s][k];
    return `<label>${label}<input data-s="${s}" data-k="${k}" data-t="${o.t || "s"}" class="${o.cls || ""}" value="${esc(v)}" inputmode="${o.t === "s" ? "text" : "decimal"}" autocomplete="off"></label>`;
  };
  function assetOptions(s) {
    const op = (t, txt) => `<option value="${t}"${P[s].ativo === t ? " selected" : ""}>${txt}</option>`;
    const todos = (META.ativos || []).map(a => a.t);
    if (s !== "wheel" || !RANK.length) return todos.map(t => op(t, t)).join("");
    const nos = new Set(RANK.map(r => r.t));           // depois da varredura: os melhores primeiro, na ordem do ranking
    return `<optgroup label="🏆 Melhores PUTs da B3 (varredura)">${RANK.map((r, k) => op(r.t, `${k + 1}. ${r.t} · nota ${F1.format(r.score)} · ${r.n} PUT${r.n > 1 ? "s" : ""}`)).join("")}</optgroup>` +
           `<optgroup label="Demais ativos">${todos.filter(t => !nos.has(t)).map(t => op(t, t)).join("")}</optgroup>`;
  }
  function assetSelect(s) {
    return `<label>Ativo<select data-s="${s}" data-k="ativo" data-t="sel">${assetOptions(s)}</select></label>`;
  }
  function fillWheelSelect() {                          // atualiza só as opções do combo (não mexe nos campos que estão sendo editados)
    const el = document.querySelector('#opCtrl select[data-s="wheel"][data-k="ativo"]');
    if (el) el.innerHTML = assetOptions("wheel");
  }
  function renderCtrl() {
    document.querySelectorAll("#opTabs button").forEach(b => b.classList.toggle("on", b.dataset.m === P.mode));
    const m = P.model;
    const model = `<details class="adv"><summary>Parâmetros do modelo (Selic, alvo de alta, filtros de idade do preço)</summary>
      <div class="op-row">
        <label class="chk"><input type="checkbox" data-s="model" data-k="selicAuto" data-t="b"${m.selicAuto ? " checked" : ""}>Selic automática (BCB)</label>
        ${fld("model", "selic", "Selic manual (% a.a.)", { t: "n" })}
        ${fld("model", "alvoAlta", "Alvo de alta calls longas (%)", { t: "n" })}
        ${fld("model", "premioMin", "Prêmio mínimo (R$)", { t: "n" })}
        ${fld("model", "idadeMax", "Idade máx. preço wheel (dias)", { t: "i" })}
        ${fld("model", "idadeMaxLong", "Idade máx. preço longas (dias)", { t: "i" })}
        ${fld("model", "projDias", "Projeção IV/HV: duração em pregões (0 = até o vencimento)", { t: "i" })}
        ${fld("model", "projGap", "Projeção IV/HV: início (0 = colado no candle; 1 = 1 pregão depois)", { t: "i" })}
        <button class="btn" data-act="reset">Restaurar padrões</button>
      </div>
      <p class="hint">Todos os campos desta tela ficam salvos neste aparelho. "Restaurar padrões" volta tudo ao pré-definido (modo WHEEL).</p></details>`;
    const wrapCtl = inner => {
      const open = store.get("ctlOpen", !matchMedia("(max-width:900px)").matches);
      $("#opCtrl").innerHTML = `<details class="ctl"${open ? " open" : ""}><summary>Filtros e parâmetros <span class="hint">(salvos neste aparelho)</span></summary>${inner}</details>`;
      $("#opCtrl .ctl").addEventListener("toggle", e => store.set("ctlOpen", e.target.open));
    };
    if (P.mode === "wheel") {
      wrapCtl(`
        <div class="op-row"><button class="btn go" data-act="varrer">🔎 Varrer a B3</button>${assetSelect("wheel")}
          ${["SAFE", "WHEEL", "AGRESSIVO", "RENDA"].map(k => `<button class="btn m-${k}${P.wheel.modo === k ? " on" : ""}" data-act="modo" data-v="${k}">${k}</button>`).join("")}
        </div>
        <div class="op-row">
          ${fld("wheel", "dteMin", "DTE mín", { t: "i", cls: "w1" })}${fld("wheel", "dteMax", "DTE máx", { t: "i", cls: "w1" })}${fld("wheel", "negMin", "Neg. mín", { t: "i", cls: "w1" })}
          ${fld("wheel", "range", "Range +/- (R$ ou %)")}${fld("wheel", "premio", "Prêmio máx (R$ ou %)")}
          <div class="grp put">${fld("wheel", "putMin", "Δ PUT mín", { t: "n", cls: "w1" })}${fld("wheel", "putMax", "Δ PUT máx", { t: "n", cls: "w1" })}</div>
          <div class="grp call">${fld("wheel", "callMin", "Δ CALL mín", { t: "n", cls: "w1" })}${fld("wheel", "callMax", "Δ CALL máx", { t: "n", cls: "w1" })}</div>
        </div>${model}`);
    } else {
      wrapCtl(`
        <div class="op-row">
          <label class="chk"><input type="checkbox" data-s="long" data-k="todos" data-t="b"${P.long.todos ? " checked" : ""}>Varrer TODOS os ativos</label>
          ${assetSelect("long")}
          ${fld("long", "range", "Range +/- (R$ ou %)")}${fld("long", "premio", "Prêmio máx (R$ ou %)")}
          ${fld("long", "dteMin", "DTE mín", { t: "i", cls: "w1" })}${fld("long", "dteMax", "DTE máx", { t: "i", cls: "w1" })}${fld("long", "desc", "Desc HV % mín", { t: "n", cls: "w1" })}${fld("long", "neg", "Neg. mín", { t: "i", cls: "w1" })}${fld("long", "top", "Top", { t: "i", cls: "w1" })}
        </div>
        <p class="hint">Calls com vencimento entre DTE mín e DTE máx (padrão: acima de 1 ano, até 730 dias). Ao escolher um ativo, a varredura passa a ser só dele; marque "Varrer TODOS" para voltar a varrer tudo. "Desc HV" = quanto o prêmio está abaixo do preço teórico de Black-Scholes calculado com a volatilidade histórica (HV20/HV60).</p>${model}`);
    }
  }

  function onInput(e) {
    const el = e.target; if (!el.dataset || !el.dataset.k) return;
    const s = el.dataset.s, k = el.dataset.k, t = el.dataset.t;
    let v;
    if (t === "b") v = el.checked;
    else if (t === "sel") v = el.value;
    else if (t === "s") v = el.value;
    else {
      v = Number(String(el.value).replace(",", "."));
      if (el.value.trim() === "" || !Number.isFinite(v) || (t === "i" && !Number.isInteger(v))) { el.style.borderColor = COL.dn; return; }
    }
    el.style.borderColor = "";
    P[s][k] = v;
    if (s === "wheel" && k === "ativo") view = "asset";
    if (s === "long" && k === "ativo" && P.long.todos) { P.long.todos = false; const cb = document.querySelector('#opCtrl input[data-s="long"][data-k="todos"]'); if (cb) cb.checked = false; }
    if (s === "wheel" && ["putMin", "putMax", "callMin", "callMax"].includes(k)) P.wheel.modo = "";
    save();
    if (s === "model" && k === "selicAuto") renderCtrl();
    run(true);
  }
  function onCtrlClick(e) {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    if (b.dataset.act === "varrer") { view = "scan"; startScan(true); return; }
    if (b.dataset.act === "modo") {
      const k = b.dataset.v, [lo, hi] = PRESETS[k];
      Object.assign(P.wheel, { modo: k, putMin: lo, putMax: hi, callMin: lo, callMax: hi });
      save(); renderCtrl(); run();
    } else if (b.dataset.act === "reset") {
      const keep = P.mode; P = clone(DEFAULTS); P.mode = keep;
      const first = META && META.ativos && META.ativos[0]; if (first && !META.ativos.some(a => a.t === P.wheel.ativo)) { P.wheel.ativo = P.long.ativo = first.t; }
      save(); renderCtrl(); run();
    }
  }
  function run(debounce) {
    clearTimeout(timer);
    timer = setTimeout(doRun, debounce ? 220 : 0);
  }

  // ---------------------------------------------------------------- tabelas
  const sc = s => (s > 75 ? "g" : s > 60 ? "b" : s > 45 ? "y" : "n");
  const WCOLS = ["Ticker", "Venc", "DTE", "Strike", "Prêmio", "IV%", "IV/HV", "Delta", "ROI%", "xCDI", "Prob%", "PoP%", "Score", "Sinal"];
  const LEG_PUT = `<div class="op-card"><h3>Sinais das PUTs</h3><div class="sig-leg">
    <div>💎 <b>Prêmio rico</b> — IV/HV ≥ 1,2 e retorno ≥ 1,5× o CDI no mês.</div>
    <div>🎯 <b>Zona ideal</b> — 15 a 30 dias e Δ entre 0,15 e 0,30.</div>
    <div>🛡️ <b>Colchão</b> — break-even ≥ 8% abaixo do preço e PoP ≥ 80%.</div>
    <div>💧 <b>Líquida</b> — 50 negócios ou mais.</div>
    <div>⚠️ <b>Prêmio magro</b> — IV/HV &lt; 0,8 ou retorno abaixo do CDI.</div>
    <div class="hint">Sinais para investigar, não recomendação. Sem HV disponível o 💎 e o ⚠️ de IV/HV ficam inativos.</div></div></div>`;

  const wrow = o => [esc(o.ticker), ddmmyy(o.venc), o.dias, F2.format(o.strike), F2.format(o.preco), F1.format(o.iv * 100), fx(o.ivHv),
                     F3.format(o.delta), F2.format(o.roi), F1.format(o.xcdi), F1.format(o.probEx * 100), F1.format(o.pop * 100), F2.format(o.score), o.sinal || "—"];
  function table(cols, rows, g, scoreIdx) {
    const th = cols.map(c => `<th>${c}</th>`).join("");
    const tr = rows.map((cells, i) => `<tr data-g="${g}" data-i="${i}" class="${sc(cur[g][i].score)}${sel && sel.g === g && sel.i === i ? " sel" : ""}">` +
      cells.map((c, j) => `<td${j === scoreIdx ? ` class="sc ${sc(cur[g][i].score)}"` : ""}>${c}</td>`).join("") + "</tr>").join("");
    return `<div class="tw"><table class="ot"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
  }
  const DICAS = {
    "prêmio": "aumente 'Prêmio máx' (ex.: 5%)", "strike": "aumente 'Range +/-' (ex.: 15%)", "DTE": "amplie 'DTE mín/máx'",
    "negócios": "reduza 'Neg. mín'", "preço atual": "as opções não negociam há dias (veja 'Idade máx. preço' nos parâmetros do modelo)",
    "IV válida": "prêmios inconsistentes (abaixo do intrínseco)", "delta": "amplie os campos Δ mín/máx (ou escolha outro modo)"
  };
  function textoFunil(f, S, faixa, premioMax) {
    const linhas = Object.entries(f).map(([k, v]) => `${k}: ${v}`);
    const culpado = Object.entries(f).find(([k, v]) => v === 0 && k !== "lidas na API");
    return `Nenhuma opção passou nos filtros.\n\nPreço do ativo: R$ ${F2.format(S)}\nRange efetivo: ±R$ ${F2.format(faixa)} | Prêmio máx efetivo: R$ ${F2.format(premioMax)}\n\n` +
      `Opções que sobraram após cada filtro:\n${linhas.join("\n")}` + (culpado ? `\n\nO filtro que zerou foi '${culpado[0]}': ${DICAS[culpado[0]] || ""}.` : "");
  }

  function textoFunilLong(f, n) {
    const ordem = ["calls lidas", "prêmio", "strike", "negócios", "preço atual", "DTE", "IV válida", "desconto HV"];
    const dicas = { "prêmio": "aumente 'Prêmio máx'", "strike": "aumente 'Range +/-' (para LEAPS, 15–25% costuma ser melhor)", "negócios": "reduza 'Neg. mín'",
      "preço atual": "as calls não negociam há dias (veja 'Idade máx. preço longas')", "DTE": "não há calls com negócio nesse prazo; reduza 'DTE mín' (ex.: 180) — LEAPS negociam pouco no opcoes.net.br",
      "IV válida": "prêmios inconsistentes", "desconto HV": "reduza 'Desc HV % mín'" };
    const linhas = ordem.map(k => `${k}: ${f[k] || 0}`);
    const culpado = ordem.find(k => k !== "calls lidas" && !f[k]);
    const maior = f.maiorDte ? `\nMaior prazo com negócio nas calls que passaram até aqui: ${f.maiorDte} dias.` : "";
    return `Nenhuma call passou nos filtros (${n} ativo(s) varridos${f["ativo sem HV"] ? `, ${f["ativo sem HV"]} sem HV` : ""}).\n\nOpções que sobraram após cada filtro:\n${linhas.join("\n")}${maior}` +
      (culpado ? `\n\nO filtro que zerou foi '${culpado}': ${dicas[culpado] || ""}.` : "");
  }

  function setStatus(extra) {
    const si = selicInfo();
    const old = META && META.updated_at && (Date.now() - new Date(META.updated_at).getTime() > 26 * 3600e3);
    $("#opStatus").innerHTML = `Opções coletadas em <b>${esc(META.updated_brt || "—")}</b> (BRT) · Selic <b>${F2.format(si.v * 100)}%</b> (${si.src})` +
      (META.demo ? ` · <span class="warn">DADOS SIMULADOS</span>` : "") + (old ? ` · <span class="warn">⚠ dados com mais de 1 dia</span>` : "") +
      (META.falhas && META.falhas.length ? `<br><span class="warn">${META.falhas.length} ativo(s) sem dados nesta coleta: ` +
        META.falhas.slice(0, 6).map(t => esc(t) + (META.falhas_motivo && META.falhas_motivo[t] ? " (" + esc(META.falhas_motivo[t].slice(0, 60)) + ")" : "")).join(", ") + `</span>` : "") +
      (extra ? `<br>${extra}` : "");
  }

  async function doRun() {
    if (!META || !META.ativos || !META.ativos.length) return;
    const my = ++runId, tb = $("#opTables"), c = cfg();
    try {
      if (P.mode === "wheel") {
        if (view === "scan") { await renderScan(my); return; }
        if (!META.ativos.some(a => a.t === P.wheel.ativo)) P.wheel.ativo = META.ativos[0].t;
        const A = await loadAsset(P.wheel.ativo); if (my !== runId) return;
        let res;
        try { res = C.scanWheel(A, P.wheel, c, A.hojeMs); }
        catch (e) { tb.innerHTML = `<div class="op-card"><div class="empty-msg">Range e Prêmio máx precisam ser números (R$ ou %, ex.: 2 ou 10%).</div></div>`; return; }
        cur = { put: res.puts.slice(0, TOPP), call: showCalls ? res.calls : [], long: [] }; sel = null;
        const stale = Date.now() - new Date(A.fetched_at).getTime() > 26 * 3600e3 ? ` <span class="warn">⚠ coleta de ${esc(A.fetched_at.slice(0, 10))}</span>` : "";
        const semHv = !Number.isFinite(A.hv) ? ` <span class="warn">· sem HV (IV/HV indisponível; rode com --hv-yahoo para ter HV)</span>` : "";
        setStatus(`<b>${esc(A.ativo)}</b> · Último R$ <b>${F2.format(A.S)}</b> · HV20 ${pct(A.hv20)} · HV60 ${pct(A.hv60)} · ${A.rows.length} opções lidas${stale}${semHv}`);
        const empty = !res.puts.length && !res.calls.length;
        const back = `<button class="btn" data-act="voltar">← Voltar à varredura</button>`;
        tb.innerHTML = empty
          ? `<div class="op-card"><h3>Sem resultados ${back}</h3><div class="empty-msg">${esc(textoFunil(res.funil, res.S, res.faixa, res.premioMax))}</div></div>`
          : `<div class="op-card"><h3><span class="c-put">${esc(A.ativo)} · ${cur.put.length} melhores PUTs <small class="hint">(de ${res.puts.length} que passaram nos filtros)</small></span>${back}</h3>
              ${cur.put.length ? table(WCOLS, cur.put.map(wrow), "put", 12) : '<div class="empty-msg">Nenhuma PUT passou nos filtros.</div>'}
              <div class="hint" style="margin-top:8px">toque numa linha para projetar · <button class="lnk" data-act="calls">${showCalls ? "ocultar" : "mostrar"} CALLs (${res.calls.length})</button></div></div>` +
            (showCalls ? `<div class="op-card"><h3><span class="c-call">CALLs · ${res.calls.length}</span></h3>${res.calls.length ? table(WCOLS, res.calls.map(wrow), "call", 12) : '<div class="empty-msg">Nenhuma CALL passou nos filtros.</div>'}</div>` : "") + LEG_PUT;
        await showAsset(A.ativo, null);
      } else {
        const todos = P.long.todos, list = todos ? META.ativos.map(a => a.t) : [P.long.ativo];
        const out = []; let done = 0, fail = 0; const fn = {};
        const LG = await loadLong(); if (my !== runId) return;
        if (LG && !Object.keys(LG).length) { tb.innerHTML = `<div class="op-card"><h3>Sem calls longas na coleta</h3><div class="empty-msg">A última coleta não trouxe nenhuma CALL negociada com vencimento acima do DTE mínimo configurado na coleta (--leaps-min). Os filtros desta tela não são o problema: veja o log da coleta (linha “CALLs negociadas coletadas por prazo”) e reduza --leaps-min.</div></div>`; return; }
        const prog = () => { tb.innerHTML = `<div class="op-card"><div class="empty-msg">Carregando opções… ${done}/${list.length}</div><div class="prog"><s style="width:${done / list.length * 100}%"></s></div></div>`; };
        prog();
        let i = 0;
        const worker = async () => {
          while (i < list.length) {
            const t = list[i++];
            try {
              const A = LG ? LG[t] : await loadAsset(t);
              if (A) out.push(...C.scanLong(A, P.long, c, A.hojeMs, fn)); else if (!LG) throw new Error("sem dados");
            } catch (e) { fail++; if (/inválido|vazio/.test(e.message)) throw e; }
            done++; if (my === runId && done < list.length) prog();
          }
        };
        try { await Promise.all(LG ? [worker()] : [worker(), worker(), worker(), worker()]); }
        catch (e) { tb.innerHTML = `<div class="op-card"><div class="empty-msg">Range e Prêmio máx precisam ser números (R$ ou %, ex.: 2 ou 10%).</div></div>`; return; }
        if (my !== runId) return;
        out.sort((a, b) => b.score - a.score);
        const top = out.slice(0, Math.max(1, P.long.top | 0));
        cur = { put: [], call: [], long: top }; sel = null;
        const semHv = LG ? list.filter(t => LG[t] && !Number.isFinite(LG[t].hv)).length : 0;
        setStatus(`Varridos <b>${list.length - fail}</b> ativo(s) · <b>${out.length}</b> calls passaram nos filtros · mostrando as ${top.length} melhores` +
          (semHv ? ` <span class="warn">· ${semHv} ativo(s) sem HV: ranqueados sem o desconto (coluna Desc% = n/d)</span>` : ""));
        const cols = ["#", "Opção", "Ativo", "Strike", "Prêmio", "Desc%", "IV%", "PoP%", `ROI(+${P.model.alvoAlta}%)`, "Score", "Sinal", "Venc", "DTE", "IV/HV"];
        const rows = top.map((o, k) => [k + 1, esc(o.ticker), esc(o.ativo), F2.format(o.strike), F2.format(o.preco), Number.isFinite(o.desc) ? F1.format(o.desc * 100) : "n/d", F1.format(o.iv * 100),
          F1.format(o.pop * 100), F2.format(o.roi), F1.format(o.score), o.sinal || "—", ddmmyy(o.venc), o.dias, fx(o.ivHv)]);
        tb.innerHTML = `<div class="op-card"><h3><span>Melhores calls longas</span></h3>${top.length ? table(cols, rows, "long", 9) :
          `<div class="empty-msg">${esc(textoFunilLong(fn, list.length - fail))}</div>`}</div>
          <div class="op-card"><h3>Sinais</h3><div class="sig-leg">
            <div>💎 <b>Opção barata</b> — IV/HV ≤ 0,9 e desconto ≥ 15% vs Black-Scholes pela HV.</div>
            <div>🚀 <b>Explosão</b> — IV &lt; 32%, strike a menos de 5% do preço e mais de 120 dias.</div>
            <div class="off">🐋 Smart money · 🟣 Gama squeeze · 🧲 Strike ímã — dependem de interesse aberto (OI), que a fonte de dados não fornece; ficam inativos.</div>
            <div class="hint">O score soma desconto vs HV (30%), probabilidade de lucro (20%), IV/HV (15%), ROI no cenário de alta (15%) e liquidez (20%).</div></div></div>`;
        if (top.length) { sel = { g: "long", i: 0 }; document.querySelector('tr[data-g="long"][data-i="0"]')?.classList.add("sel"); await project(top[0]); }
        else { clearProj(); $("#opInfo").innerHTML = ""; }
      }
    } catch (e) {
      if (my === runId) tb.innerHTML = `<div class="op-card"><div class="empty-msg">Erro ao carregar os dados: ${esc(e.message)}</div></div>`;
    }
  }

  // ---------------------------------------------------------------- varredura da B3 (top 15 ativos)
  async function startScan(force) {
    if (scanning) return;
    if (SCANA && !force) { run(); return; }
    scanning = true; scanDone = 0; SCANA = {};
    const list = META.ativos.map(a => a.t); let i = 0;
    run();
    const worker = async () => {
      while (i < list.length) {
        const t = list[i++];
        try { SCANA[t] = await loadAsset(t); } catch (e) { /* ativo sem arquivo: ignora */ }
        scanDone++;
        if (scanDone % 5 === 0 || scanDone === list.length) run();
      }
    };
    await Promise.all([worker(), worker(), worker(), worker(), worker(), worker()]);
    scanning = false; run();
  }
  async function renderScan(my) {
    const tb = $("#opTables"), c = cfg(), n = META.ativos.length;
    clearProj(); $("#opInfo").innerHTML = ""; $("#opChTitle").textContent = "Gráfico";
    if (scanning) {
      setStatus(`Varrendo a B3… ${scanDone}/${n} ativos`);
      tb.innerHTML = `<div class="op-card"><div class="empty-msg">🔎 Varrendo a B3… ${scanDone}/${n} ativos</div><div class="prog"><s style="width:${scanDone / n * 100}%"></s></div></div>`;
      return;
    }
    if (!SCANA) {
      setStatus();
      tb.innerHTML = `<div class="op-card"><h3>Varrer a B3</h3><div class="empty-msg">Toque em <b>🔎 Varrer a B3</b> para o sistema analisar as ${n} ações coletadas e mostrar as ${TOPN} com as melhores PUTs para vender, segundo os filtros abaixo (DTE, delta, prêmio, range).\n\nDepois escolha uma ação da lista para ver as ${TOPP} melhores PUTs dela.\n\nOu escolha um ativo direto no campo “Ativo”.</div>
        <div style="margin-top:12px"><button class="btn go" data-act="varrer">🔎 Varrer a B3</button></div></div>`;
      return;
    }
    const rows = []; let analisados = 0;
    for (const t in SCANA) {
      const A = SCANA[t]; analisados++;
      let res;
      try { res = C.scanWheel(A, P.wheel, c, A.hojeMs); }
      catch (e) { tb.innerHTML = `<div class="op-card"><div class="empty-msg">Range e Prêmio máx precisam ser números (R$ ou %, ex.: 2 ou 10%).</div></div>`; return; }
      if (!res.puts.length) continue;
      const top3 = res.puts.slice(0, 3), avg = top3.reduce((a, o) => a + o.score, 0) / top3.length;
      rows.push({ t, A, n: res.puts.length, best: res.puts[0], avg });
    }
    if (my !== runId) return;
    rows.sort((a, b) => b.best.score - a.best.score || b.avg - a.avg);
    const top = rows.slice(0, TOPN);
    RANK = top.map(r => ({ t: r.t, score: r.best.score, n: r.n })); fillWheelSelect();
    setStatus(`Varredura: <b>${analisados}</b> ativos analisados · <b>${rows.length}</b> têm PUTs dentro dos filtros · mostrando os ${top.length} melhores`);
    const li = top.map((r, k) => {
      const o = r.best;
      return `<button class="scanrow" data-t="${esc(r.t)}"><span class="rk">${k + 1}</span><span class="tk">${esc(r.t)}</span>
        <span class="px">R$ ${F2.format(r.A.S)}</span>
        <span class="bo">${o.sinal ? o.sinal + " " : ""}PUT ${F2.format(o.strike)} · ${o.dias}d · Δ ${F2.format(o.delta)} · prêmio ${F2.format(o.preco)} · ROI ${F2.format(o.roi)}%</span>
        <span class="n">${r.n} PUT${r.n > 1 ? "s" : ""}${META.liquidos && META.liquidos.indexOf(r.t) >= 0 ? " · liq #" + (META.liquidos.indexOf(r.t) + 1) : ""}</span><span class="scb sc ${sc(o.score)}">${F1.format(o.score)}</span></button>`;
    }).join("");
    tb.innerHTML = `<div class="op-card"><h3><span>Top ${top.length} da B3 — melhores PUTs para vender</span><span class="hint">toque num ativo</span></h3>${top.length ? `<div class="scanlist">${li}</div>`
      : '<div class="empty-msg">Nenhum ativo tem PUTs dentro dos filtros. Amplie DTE, Δ, Range ou Prêmio máx.</div>'}
      <div class="hint" style="margin-top:8px">A nota do ativo é o score da sua melhor PUT (os sinais são os dela). Os filtros acima valem para a varredura.</div></div>` + LEG_PUT;
  }

  async function onRowClick(e) {
    const sr = e.target.closest(".scanrow");
    if (sr) { P.wheel.ativo = sr.dataset.t; save(); view = "asset"; renderCtrl(); run(); return; }
    const bt = e.target.closest("[data-act]");
    if (bt && bt.dataset.act === "varrer") { view = "scan"; startScan(true); return; }
    if (bt && bt.dataset.act === "voltar") { view = "scan"; run(); return; }
    if (bt && bt.dataset.act === "calls") { showCalls = !showCalls; run(); return; }
    const tr = e.target.closest("tr[data-g]"); if (!tr) return;
    const g = tr.dataset.g, i = +tr.dataset.i, o = cur[g][i]; if (!o) return;
    sel = { g, i };
    document.querySelectorAll("#opTables tr.sel").forEach(x => x.classList.remove("sel")); tr.classList.add("sel");
    await project(o);
    if (matchMedia("(max-width:1100px)").matches) $("#opChartWrap").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ---------------------------------------------------------------- gráfico
  function ensureChart() {
    if (ch) return;
    ch = LightweightCharts.createChart($("#opChart"), {
      layout: { background: { type: "solid", color: "transparent" }, textColor: "#8793a4", fontSize: 11 },
      grid: { vertLines: { color: "rgba(255,255,255,.035)" }, horzLines: { color: "rgba(255,255,255,.035)" } },
      rightPriceScale: { borderColor: "#1f2733", scaleMargins: { top: .06, bottom: .2 } },
      timeScale: { borderColor: "#1f2733", rightOffset: 4, timeVisible: false },
      localization: { priceFormatter: p => F2.format(p) }
    });
    cs = ch.addCandlestickSeries({ upColor: COL.up, downColor: COL.dn, borderVisible: false, wickUpColor: COL.up, wickDownColor: COL.dn });
    vs = ch.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    ch.priceScale("vol").applyOptions({ scaleMargins: { top: .88, bottom: 0 } });
    const ln = (color, w) => ch.addLineSeries({ color, lineWidth: w, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    e9 = ln("#fbbf24", 1); vw = ln("#60a5fa", 2);
    const w = $("#opChartWrap");
    new ResizeObserver(() => { if (w.clientWidth > 0 && w.clientHeight > 0) ch.applyOptions({ width: w.clientWidth, height: w.clientHeight }); }).observe(w);
  }
  function clearProj() {
    projS.forEach(s => { try { ch.removeSeries(s); } catch (e) {} }); projS = [];
    lines.forEach(l => { try { cs.removePriceLine(l); } catch (e) {} }); lines = [];
  }
  async function showAsset(ativo, o) {
    ensureChart();
    let [A, px] = await Promise.all([loadAsset(ativo), loadPx(ativo)]);
    if (!px.length) {            // ativo sem histórico de preços: ancora o gráfico só no preço atual
      const d = new Date(A.hojeMs).toISOString().slice(0, 10);
      px = [{ time: d, open: A.S, high: A.S, low: A.S, close: A.S, volume: 0 }];
      chAtivo = null;
    }
    if (chAtivo !== ativo) {
      chAtivo = ativo;
      cs.setData(px.map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })));
      vs.setData(px.map(c => ({ time: c.time, value: c.volume, color: c.close >= c.open ? "rgba(38,194,129,.3)" : "rgba(239,83,80,.3)" })));
      let m = null; const k = 2 / 10;
      e9.setData(px.map(c => { m = m == null ? c.close : c.close * k + m * (1 - k); return { time: c.time, value: m }; }));
      let pv = 0, vv = 0;
      vw.setData(px.map(c => { pv += (c.high + c.low + c.close) / 3 * c.volume; vv += c.volume; return { time: c.time, value: vv ? pv / vv : c.close }; }));
    }
    clearProj();
    lines.push(cs.createPriceLine({ price: A.S, color: COL.acc, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "Fech." }));
    $("#opChTitle").textContent = `${ativo} · gráfico diário` + (o ? " + projeção" : "");
    $("#opLegend").innerHTML = o ? "" : `<span><i style="--c:#fbbf24"></i>EMA9</span><span><i style="--c:#60a5fa"></i>VWAP</span>`;
    if (!o) $("#opInfo").innerHTML = `<div><span>Fechamento</span><span>R$ ${F2.format(A.S)}</span></div><div><span>HV20</span><span>${pct(A.hv20)}</span></div><div><span>HV60</span><span>${pct(A.hv60)}</span></div>`;
    const n = px.length; ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 110), to: n + 6 });
    return { A, px };
  }
  async function project(o) {
    const ativo = o.ativo || P.wheel.ativo;
    const { A, px } = await showAsset(ativo, o);
    const c = cfg(), closes = px.map(x => x.close), last = px[px.length - 1].time;
    const pr = C.projecao(A, closes, last, o, c, A.hojeMs);
    lines.push(cs.createPriceLine({ price: o.strike, color: COL.amb, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "Strike" }));
    lines.push(cs.createPriceLine({ price: pr.be, color: COL.dn, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "BE" }));
    const add = (vals, color, w, style) => {
      if (!vals) return null;
      const s = ch.addLineSeries({ color, lineWidth: w, lineStyle: style, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      s.setData(pr.xs.map((t, i) => ({ time: t, value: vals[i] }))); projS.push(s); return s;
    };
    const sUp = add(pr.ivUp1, COL.up, 1, 0); add(pr.ivDn1, COL.dn, 1, 0); add(pr.ivUp2, COL.up, 1, 1); add(pr.ivDn2, COL.dn, 1, 1);
    add(pr.hvUp1, COL.hv, 1, 0); add(pr.hvDn1, COL.hv, 1, 0); add(pr.hvUp2, COL.hv, 1, 2); add(pr.hvDn2, COL.hv, 1, 2);
    if (sUp && pr.colado && pr.datas.length) sUp.setMarkers([{ time: pr.datas[pr.datas.length - 1], position: "aboveBar", color: COL.venc, shape: "arrowDown", text: ddmmyy(o.venc).slice(0, 5) }]);
    const n = px.length; ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 100), to: n + pr.xs.length + 12 });
    $("#opChTitle").textContent = `${ativo} · ${o.ticker} (${o.tipo}) · projeção`;
    const row = (a, b) => `<div><span>${a}</span><span>${b}</span></div>`;
    $("#opInfo").innerHTML = row("Fechamento", "R$ " + F2.format(A.S)) + row("Strike", "R$ " + F2.format(o.strike)) + row("Breakeven", "R$ " + F2.format(pr.be)) +
      row("Prêmio", "R$ " + F2.format(o.preco)) + row("DTE", pr.dias) + row("IV", pct(pr.iv)) + row("HV", pct(pr.hvCone)) + row("IV/HV", fx(o.ivHv)) +
      row("Delta", F3.format(o.delta)) + row("Prob. exercício", F1.format(pr.pEx * 100) + "%") + row("PoP", F1.format(o.pop * 100) + "%") +
      (o.desc != null ? row("Desc. vs HV", F1.format(o.desc * 100) + "%") : row("xCDI", F1.format(o.xcdi)));
    $("#opLegend").innerHTML = `<span><i style="--c:${COL.up}"></i>IV +1σ/+2σ</span><span><i style="--c:${COL.dn}"></i>IV −1σ/−2σ</span>
      <span><i class="dash" style="--c:${COL.hv}"></i>HV ±1σ/±2σ</span><span><i style="--c:${COL.amb}"></i>Strike</span><span><i style="--c:${COL.dn}"></i>Breakeven</span>
      <span><i style="--c:#fbbf24"></i>EMA9</span><span><i style="--c:#60a5fa"></i>VWAP</span>`;
  }

  // ---------------------------------------------------------------- ciclo de vida
  async function loadMeta() {
    const m = await jget("meta.json");
    const changed = META && META.updated_at !== m.updated_at;
    META = m;
    if (changed) { LONG = null; SCANA = null; RANK = []; for (const k in cacheA) delete cacheA[k]; for (const k in cacheP) delete cacheP[k]; chAtivo = null; }
    return changed;
  }
  async function show() {
    const root = $("#opcoes");
    if (!built) build();
    try {
      const changed = await loadMeta();
      if (!META.ativos || !META.ativos.length) throw new Error(META.erro || "sem dados de opções");
      if (!show.hashDone) {                                   // link vindo dos Gráficos: opcoes/#PETR4
        show.hashDone = true;
        const hs = (location.hash || "").replace("#", "").toUpperCase();
        if (hs && META.ativos.some(a => a.t === hs)) { P.wheel.ativo = P.long.ativo = hs; view = "asset"; }
      }
      if (!META.ativos.some(a => a.t === P.wheel.ativo)) P.wheel.ativo = META.ativos[0].t;
      if (!META.ativos.some(a => a.t === P.long.ativo)) P.long.ativo = META.ativos[0].t;
      renderCtrl(); setStatus();
      const selicAntes = selicInfo().v;
      fetchSelic().then(ok => { if (ok) { setStatus(); if (Math.abs(selicInfo().v - selicAntes) > 1e-9) run(); } });
      if (!show.ran || changed) { show.ran = true; run(); }
    } catch (e) {
      $("#opCtrl").innerHTML = ""; $("#opTables").innerHTML = "";
      $("#opStatus").innerHTML = `<span class="warn">Dados de opções indisponíveis.</span>`;
      $("#opTables").innerHTML = `<div class="op-card"><div class="empty-msg">Não foi possível carregar as opções (${esc(e.message)}).\n\nCausas comuns:\n• a primeira coleta ainda não rodou (aba Actions → "Atualizar dados e publicar" → Run workflow);\n• o site opcoes.net.br bloqueou/limitou a coleta nesta rodada — a próxima atualização horária tenta de novo.</div></div>`;
    }
  }

  window.Opcoes = { show, _state: () => ({ P, META, cur }) };
})();
