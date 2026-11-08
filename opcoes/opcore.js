/*
 * Núcleo de opções — porte fiel do "Scanner Wheel V.2" (Leandro Bascheira) para JavaScript.
 * Mesmas fórmulas, constantes, pesos e filtros do bloco NUCLEO e das rotinas buscar() / processar_ativo().
 * Sem dependência de DOM: roda no navegador e no Node (testes).
 */
(function (root) {
  "use strict";

  // ---- constantes originais (editáveis na tela, em "Parâmetros do modelo") ----
  const DEF = {
    SELIC_PADRAO: 0.14,   // usada só se BCB e o último build falharem
    ALVO_ALTA: 0.20,      // cenário de alta no ROI das calls longas (+20%)
    PREMIO_MIN: 0.03,     // ignora prêmios menores que isso (ruído de tick)
    HV_NEUTRA: 0.5,       // peso neutro do componente IV/HV quando não há HV
    IDADE_MAX_DIAS: 4,    // wheel: ignora opção cujo último negócio é mais antigo que isso
    IDADE_MAX_LONGAS: 10  // calls longas negociam menos; tolerância maior
  };

  const clip = (x, lo = 0, hi = 1) => Math.min(Math.max(x, lo), hi);
  const round = (x, n) => { const k = Math.pow(10, n); return Math.round(x * k) / k; };

  // ---- distribuição normal acumulada (Hart/West, precisão ~1e-15) ----
  function cdf(x) {
    const a = Math.abs(x);
    let c;
    if (a > 37) c = 0;
    else {
      const e = Math.exp(-a * a / 2);
      if (a < 7.07106781186547) {
        let b = 3.52624965998911e-02 * a + 0.700383064443688;
        b = b * a + 6.37396220353165; b = b * a + 33.912866078383;
        b = b * a + 112.079291497871; b = b * a + 221.213596169931; b = b * a + 220.206867912376;
        c = e * b;
        b = 8.83883476483184e-02 * a + 1.75566716318264;
        b = b * a + 16.064177579207; b = b * a + 86.7807322029461; b = b * a + 296.564248779674;
        b = b * a + 637.333633378831; b = b * a + 793.826512519948; b = b * a + 440.413735824752;
        c = c / b;
      } else {
        let b = a + 0.65; b = a + 4 / b; b = a + 3 / b; b = a + 2 / b; b = a + 1 / b;
        c = e / b / 2.506628274631;
      }
    }
    return x > 0 ? 1 - c : c;
  }

  // ---- utilidades ----
  function valorOuPct(txt, S) {              // '2' -> R$ 2,00 | '10%' -> 10% do preço S
    const t = String(txt).trim().replace(",", ".");
    if (t === "") throw new Error("vazio");
    if (t.endsWith("%")) {
      const v = Number(t.slice(0, -1));
      if (!Number.isFinite(v)) throw new Error("inválido");
      return v / 100 * S;
    }
    const v = Number(t);
    if (!Number.isFinite(v)) throw new Error("inválido");
    return v;
  }

  function dateMs(s) {                       // 'AAAA-MM-DD...' -> ms UTC (meia-noite) ou null
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : null;
  }
  const DAY = 86400000;
  function idadeDias(ult, hojeMs) {          // dias desde o último negócio; null se não houver data
    const d = dateMs(ult);
    return d == null ? null : Math.round((hojeMs - d) / DAY);
  }
  const taxaCont = selic => Math.log1p(selic);
  const cdiMensalPct = selic => (Math.pow(1 + selic, 30 / 365) - 1) * 100;

  // ---- Black-Scholes ----
  function bsPreco(S, K, T, r, sigma, tipo) {
    if (T <= 0 || sigma <= 0) return tipo === "CALL" ? Math.max(S - K, 0) : Math.max(K - S, 0);
    const vt = sigma * Math.sqrt(T);
    const d1 = (Math.log(S / K) + (r + sigma * sigma / 2) * T) / vt;
    const d2 = d1 - vt;
    if (tipo === "CALL") return S * cdf(d1) - K * Math.exp(-r * T) * cdf(d2);
    return K * Math.exp(-r * T) * cdf(-d2) - S * cdf(-d1);
  }

  // Volatilidade implícita anual (0.32 = 32%). NaN se o prêmio for inconsistente. (Bisseção ≡ brentq 1%–500%)
  function ivBs(price, S, K, T, r, tipo) {
    if (!(price > 0 && S > 0 && K > 0 && T > 0)) return NaN;
    const descK = K * Math.exp(-r * T);
    const intr = tipo === "CALL" ? Math.max(S - descK, 0) : Math.max(descK - S, 0);
    const teto = tipo === "CALL" ? S : descK;
    if (price < intr - 1e-6 || price >= teto) return NaN;
    const f = s => bsPreco(S, K, T, r, s, tipo) - price;
    let lo = 0.01, hi = 5.0;
    if (f(lo) > 0 || f(hi) < 0) return NaN;
    for (let i = 0; i < 200 && hi - lo > 1e-9; i++) {
      const mid = (lo + hi) / 2;
      if (f(mid) > 0) hi = mid; else lo = mid;
    }
    return (lo + hi) / 2;
  }

  function deltaBs(S, K, T, r, sigma, tipo) {
    if (T <= 0 || !Number.isFinite(sigma) || sigma <= 0) return 0;
    const d1 = (Math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * Math.sqrt(T));
    return tipo === "CALL" ? cdf(d1) : -cdf(-d1);
  }

  function probAcima(S, B, T, r, sigma) {    // P(S_T > B) sob medida neutra ao risco
    if (B <= 0) return 1;
    if (T <= 0 || sigma <= 0) return S > B ? 1 : 0;
    const d2 = (Math.log(S / B) + (r - sigma * sigma / 2) * T) / (sigma * Math.sqrt(T));
    return cdf(d2);
  }

  function hvAnual(closes, n) {              // closes: array numérico; desvio amostral (ddof=1)
    const ret = [];
    for (let i = 1; i < closes.length; i++) ret.push(Math.log(closes[i] / closes[i - 1]));
    if (ret.length < 10) return NaN;
    const t = ret.slice(-n);
    const m = t.reduce((a, b) => a + b, 0) / t.length;
    const v = t.reduce((a, b) => a + (b - m) * (b - m), 0) / (t.length - 1);
    return Math.sqrt(v) * Math.sqrt(252);
  }

  const liqScore = neg => clip(Math.log10(Math.max(Number(neg), 0) + 1) / 2);

  // ---- venda de PUT garantida / CALL coberta (WHEEL) ----
  function avaliarVenda(tipo, S, K, premio, dias, r, hv, negocios, cfg) {
    const T = dias / 365;
    const iv = ivBs(premio, S, K, T, r, tipo);
    if (!Number.isFinite(iv)) return null;
    const delta = deltaBs(S, K, T, r, iv, tipo);
    const pAcimaK = probAcima(S, K, T, r, iv);
    let probEx, be, pop, capital, distBe;
    if (tipo === "PUT") {
      probEx = 1 - pAcimaK; be = K - premio; pop = probAcima(S, be, T, r, iv);
      capital = K; distBe = (S - be) / S * 100;
    } else {
      probEx = pAcimaK; be = K + premio; pop = 1 - probAcima(S, be, T, r, iv);
      capital = S; distBe = (be - S) / S * 100;
    }
    const roi = premio / capital * (30 / dias) * 100;
    const xcdi = roi / cdiMensalPct(cfg.selic);
    const ivHv = (Number.isFinite(hv) && hv > 0) ? iv / hv : NaN;
    const sRet = clip((xcdi - 1) / 3);
    const sVrp = Number.isFinite(ivHv) ? clip((ivHv - 0.8) / 0.8) : cfg.HV_NEUTRA;
    const sDlt = clip(1 - Math.abs(Math.abs(delta) - 0.30) / 0.30);
    const sSeg = clip((pop - 0.5) / 0.4);
    const sLiq = liqScore(negocios);
    const score = 100 * (0.30 * sRet + 0.20 * sVrp + 0.15 * sDlt + 0.20 * sSeg + 0.15 * sLiq);
    return { iv, delta, probEx, pop, roi, xcdi, ivHv, distBe, be, score: round(score, 2) };
  }

  // ---- compra de CALL longa ----
  function avaliarCallLonga(S, K, premio, dias, r, hv, negocios, cfg) {
    const T = dias / 365;
    const iv = ivBs(premio, S, K, T, r, "CALL");
    if (!Number.isFinite(iv)) return null;
    const ivHv = (Number.isFinite(hv) && hv > 0) ? iv / hv : NaN;
    let desc = NaN;
    if (Number.isFinite(hv) && hv > 0) {
      const teo = bsPreco(S, K, T, r, hv, "CALL");
      if (teo > 0) desc = (teo - premio) / teo;
    }
    const delta = deltaBs(S, K, T, r, iv, "CALL");
    const pop = probAcima(S, K + premio, T, r, iv);
    const payoff = Math.max(S * (1 + cfg.ALVO_ALTA) - K, 0);
    const roi = (payoff - premio) / premio;
    const distBe = ((K + premio) / S - 1) * 100;
    const sDesc = Number.isFinite(desc) ? clip(desc / 0.40) : 0;
    const sPop = clip(pop / 0.50);
    const temHv = Number.isFinite(desc);
    // com HV: IV relativa à HV; sem HV: quanto menor a IV absoluta (referência 60%), melhor
    const sIv = Number.isFinite(ivHv) ? clip((1.1 - ivHv) / 0.6) : (temHv ? cfg.HV_NEUTRA : clip((0.6 - iv) / 0.4));
    const sRoi = clip(roi / 3);
    const sLiq = liqScore(negocios);
    const score = temHv ? 100 * (0.30 * sDesc + 0.20 * sPop + 0.15 * sIv + 0.15 * sRoi + 0.20 * sLiq)
                        : 100 * (0.20 * sPop + 0.15 * sIv + 0.15 * sRoi + 0.20 * sLiq) / 0.70;   // sem HV: reescala os demais pesos
    return { iv, delta, pop, desc, roi, ivHv, distBe, semHv: !temHv, score: round(score, 2) };
  }

  // ---- sinais das PUTs (venda de prêmio) — análogos aos 💎 e 🚀 das calls longas ----
  //  💎 Prêmio rico  : IV/HV >= 1,2 e retorno >= 1,5x o CDI no mês (o mercado paga mais que a volatilidade realizada)
  //  🎯 Zona ideal   : 15 a 30 dias e |delta| entre 0,15 e 0,30
  //  🛡️ Colchão      : break-even >= 8% abaixo do preço e PoP >= 80%
  //  💧 Líquida      : 50 negócios ou mais
  //  ⚠️ Prêmio magro : IV/HV < 0,8 ou retorno abaixo do CDI (não paga o risco)
  function sinaisPut(o) {
    const ad = Math.abs(o.delta);
    let s = "";
    if (Number.isFinite(o.ivHv) && o.ivHv >= 1.2 && o.xcdi >= 1.5) s += "💎";
    if (o.dias >= 15 && o.dias <= 30 && ad >= 0.15 && ad <= 0.30) s += "🎯";
    if (o.distBe >= 8 && o.pop >= 0.80) s += "🛡️";
    if (o.neg >= 50) s += "💧";
    if ((Number.isFinite(o.ivHv) && o.ivHv < 0.8) || o.xcdi < 1) s += "⚠️";
    return s;
  }

  // ---- scanner de venda (equivale a buscar()) ----
  // A: {S, hv, rows:[{ticker,venc,tipo,strike,preco,neg,ult,vol}]}  p: parâmetros da tela  hojeMs: data da coleta
  function scanWheel(A, p, cfg, hojeMs) {
    const S = A.S, hv = A.hv, r = taxaCont(cfg.selic);
    const faixa = valorOuPct(p.range, S), premioMax = valorOuPct(p.premio, S);
    const etapas = ["prêmio", "strike", "DTE", "negócios", "preço atual", "IV válida", "delta"];
    const funil = { "lidas na API": A.rows.length };
    etapas.forEach(e => { funil[e] = 0; });
    const puts = [], calls = [];
    for (const o of A.rows) {
      const { strike, preco: premio, tipo } = o;
      if (premio < cfg.PREMIO_MIN || premio > premioMax) continue;
      funil["prêmio"]++;
      if ((tipo === "PUT" && (strike > S || strike < S - faixa)) ||
          (tipo === "CALL" && (strike < S || strike > S + faixa))) continue;
      funil["strike"]++;
      const dias = Math.round((dateMs(o.venc) - hojeMs) / DAY);
      if (!(dias > 0) || dias < p.dteMin || dias > p.dteMax) continue;
      funil["DTE"]++;
      if (o.neg < p.negMin) continue;
      funil["negócios"]++;
      const idade = idadeDias(o.ult, hojeMs);
      if (idade != null && idade > cfg.IDADE_MAX_DIAS) continue;
      funil["preço atual"]++;
      const m = avaliarVenda(tipo, S, strike, premio, dias, r, hv, o.neg, cfg);
      if (m == null || m.iv < 0.10) continue;
      funil["IV válida"]++;
      const [dmin, dmax] = tipo === "PUT" ? [p.putMin, p.putMax] : [p.callMin, p.callMax];
      if (Math.abs(m.delta) < dmin || Math.abs(m.delta) > dmax) continue;
      funil["delta"]++;
      const item = { ...o, ativo: A.ativo, dias, ...m };
      if (tipo === "PUT") item.sinal = sinaisPut(item);
      (tipo === "PUT" ? puts : calls).push(item);
    }
    puts.sort((a, b) => b.score - a.score);
    calls.sort((a, b) => b.score - a.score);
    return { puts, calls, funil, S, faixa, premioMax };
  }

  // ---- scanner de calls longas (equivale a processar_ativo()) ----
  function scanLong(A, p, cfg, hojeMs, funil) {
    const out = [];
    const S = A.S, hv = A.hv;
    const f = funil || {};
    const cnt = k => { f[k] = (f[k] || 0) + 1; };
    const temHv = Number.isFinite(hv);
    if (!temHv) f["ativo sem HV"] = (f["ativo sem HV"] || 0) + 1;     // sem HV: ranqueia por IV, PoP, ROI e liquidez (sem o desconto)
    const faixa = valorOuPct(p.range, S), premioMax = valorOuPct(p.premio, S);
    const descMin = p.desc / 100, r = taxaCont(cfg.selic);
    const dMin = Number.isFinite(p.dteMin) ? p.dteMin : 60, dMax = Number.isFinite(p.dteMax) ? p.dteMax : 730;
    for (const o of A.rows) {
      if (o.tipo !== "CALL") continue;
      cnt("calls lidas");
      const { strike, preco: premio } = o;
      if (premio < cfg.PREMIO_MIN || premio > premioMax) continue;
      cnt("prêmio");
      if (!(S - faixa <= strike && strike <= S + faixa)) continue;
      cnt("strike");
      if (o.neg < p.neg) continue;
      cnt("negócios");
      const idade = idadeDias(o.ult, hojeMs);
      if (idade != null && idade > cfg.IDADE_MAX_LONGAS) continue;
      cnt("preço atual");
      const dias = Math.round((dateMs(o.venc) - hojeMs) / DAY);
      if (dias > (f.maiorDte || 0)) f.maiorDte = dias;
      if (!(dias >= dMin && dias <= dMax)) continue;
      cnt("DTE");
      const m = avaliarCallLonga(S, strike, premio, dias, r, hv, o.neg, cfg);
      if (m == null || m.iv > 1.6) continue;
      cnt("IV válida");
      if (temHv && (!Number.isFinite(m.desc) || m.desc < descMin)) continue;
      cnt("desconto HV");
      const dist = (strike - S) / S * 100;
      const oi = 0;   // a fonte (opcoes.net.br) não traz interesse aberto; 🐋 🟣 🧲 dependem de OI e ficam inativos
      let sinal = "";
      if (o.vol > 400 && oi > 800) sinal += "🐋";
      if (m.desc >= 0.15 && Number.isFinite(m.ivHv) && m.ivHv <= 0.9) sinal += "💎";
      if (Math.abs(dist) < 2.5 && oi > 1500) sinal += "🟣";
      if (m.iv < 0.32 && Math.abs(dist) < 5 && dias > 120) sinal += "🚀";
      if (oi > 2500 && Math.abs(dist) < 2) sinal += "🧲";
      out.push({ ...o, ativo: A.ativo, dias, dist, sinal, ...m });
    }
    return out;
  }

  // ---- projeção do gráfico (cone ±1σ/±2σ pela IV e pela HV) ----
  function diasUteisApos(lastDate, vencDate) {       // 'AAAA-MM-DD' (exclusive) até venc (inclusive), sem fins de semana
    const out = [];
    let t = dateMs(lastDate) + DAY;
    const end = dateMs(vencDate);
    while (t <= end) {
      const wd = new Date(t).getUTCDay();
      if (wd !== 0 && wd !== 6) out.push(new Date(t).toISOString().slice(0, 10));
      t += DAY;
    }
    return out;
  }

  function projecao(A, closes, lastDate, o, cfg, hojeMs) {
    const S = A.S, r = taxaCont(cfg.selic);
    const dias = Math.max(Math.round((dateMs(o.venc) - hojeMs) / DAY), 1);
    const be = o.tipo === "PUT" ? o.strike - o.preco : o.strike + o.preco;
    let iv = ivBs(o.preco, S, o.strike, dias / 365, r, o.tipo);
    if (!Number.isFinite(iv)) {                      // sem IV válida: usa a vol. histórica de 60 dias
      const rets = [];
      const c = closes.slice(-61);
      for (let i = 1; i < c.length; i++) rets.push(c[i] / c[i - 1] - 1);
      const m = rets.reduce((a, b) => a + b, 0) / rets.length;
      iv = Math.sqrt(rets.reduce((a, b) => a + (b - m) * (b - m), 0) / (rets.length - 1)) * Math.sqrt(252);
    }
    let hvCone = A.hv;
    if (!Number.isFinite(hvCone)) hvCone = hvAnual(closes, 60);
    const datas = diasUteisApos(lastDate, o.venc);
    const k = datas.map((_, i) => i + 1);
    const cone = (sig, mult) => [S, ...k.map(i => S * Math.exp(mult * sig * Math.sqrt(i / 252)))];
    let pEx = probAcima(S, o.strike, dias / 365, r, iv);
    if (o.tipo === "PUT") pEx = 1 - pEx;
    return {
      dias, be, iv, hvCone, datas, pEx,
      ivUp1: cone(iv, 1), ivDn1: cone(iv, -1), ivUp2: cone(iv, 2), ivDn2: cone(iv, -2),
      hvUp1: Number.isFinite(hvCone) && hvCone > 0 ? cone(hvCone, 1) : null,
      hvDn1: Number.isFinite(hvCone) && hvCone > 0 ? cone(hvCone, -1) : null,
      hvUp2: Number.isFinite(hvCone) && hvCone > 0 ? cone(hvCone, 2) : null,
      hvDn2: Number.isFinite(hvCone) && hvCone > 0 ? cone(hvCone, -2) : null
    };
  }

  const API = { DEF, cdf, clip, valorOuPct, dateMs, idadeDias, taxaCont, cdiMensalPct, bsPreco, ivBs, deltaBs,
                probAcima, hvAnual, liqScore, avaliarVenda, avaliarCallLonga, sinaisPut, scanWheel, scanLong,
                diasUteisApos, projecao };
  root.OPCORE = API;
  if (typeof module !== "undefined" && module.exports) module.exports = API;
})(typeof window !== "undefined" ? window : globalThis);
