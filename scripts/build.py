"""Baixa cotações (Yahoo Finance), calcula indicadores e gera JSONs para o site.

Uso:  python scripts/build.py [--limit N]
Saída: docs/data/index.json  +  docs/data/<TICKER>.json
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import institucional  # scanner de acumulação / reversão (scripts/institucional.py)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
BARS = 500          # candles exibidos no gráfico
HISTORY = "3y"      # histórico baixado (EMA200 precisa de aquecimento)
LIQ_DAYS = 60       # janela da liquidez (pregões, ~3 meses). Ajuste se quiser
BATCH = 25          # tickers por requisição
CROSS_DAYS = 3      # um cruzamento de médias vale se ocorreu nos últimos N pregões (1 = só no último)

# divergência de alta (preço faz fundo mais baixo, indicador faz fundo mais alto)
DIV_PIVOT = 3       # um fundo é a mínima mais baixa entre N pregões antes e N depois (precisa de N pregões de confirmação)
DIV_RECENT = 10     # o 2º fundo precisa ter ocorrido nos últimos N pregões
DIV_MIN_GAP = 5     # distância mínima (pregões) entre os dois fundos
DIV_MAX_GAP = 60    # distância máxima (pregões) entre os dois fundos
DIV_PRICE_TOL = 0.003  # o 2º fundo precisa ser pelo menos 0,3% mais baixo que o 1º
DIV_RSI_MARGIN = 1.0   # o RSI do 2º fundo precisa ser pelo menos 1 ponto maior
DIV_RSI_MAX = 45    # RSI do 1º fundo precisa estar abaixo disso (queda de fato)

# projeção de volatilidade (desvio padrão dos retornos diários, projetado com a raiz do tempo)
VOL_WINDOW = 60     # pregões usados para medir a volatilidade histórica
VOL_HORIZON = 20    # quantos pregões à frente projetar no gráfico
VOL_STEPS = (5, 10, 20)  # horizontes mostrados na tabela

# Fibonacci sobre o último grande movimento (maior máxima x menor mínima da janela)
FIB_LOOKBACK = 120  # pregões olhados para achar a máxima e a mínima do movimento
FIB_RET = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXT = (1.272, 1.618)


def load_tickers():
    seen, out = set(), []
    for line in (ROOT / "tickers.txt").read_text().splitlines():
        line = line.split("#")[0]
        for t in line.split():
            t = t.strip().upper().replace(".SA", "")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


# ---------- indicadores ----------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()   # suavização de Wilder
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def macd(close, fast=12, slow=26, sig=9):
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal


def slow_stoch(high, low, close, k=5, d=3):
    ll, hh = low.rolling(k).min(), high.rolling(k).max()
    fast_k = 100 * (close - ll) / (hh - ll).replace(0, np.nan)
    slow_k = fast_k.rolling(3).mean()
    return slow_k, slow_k.rolling(d).mean()


def atr(high, low, close, n=14):
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def add_indicators(df):
    c, h, l = df["Close"], df["High"], df["Low"]
    df["MA9"] = c.rolling(9).mean()
    df["SMA20"] = c.rolling(20).mean()
    sd = c.rolling(20).std()
    df["BB_UP"] = df["SMA20"] + 2 * sd
    df["BB_LO"] = df["SMA20"] - 2 * sd
    df["EMA50"] = c.ewm(span=50, adjust=False).mean()
    df["EMA200"] = c.ewm(span=200, adjust=False).mean()
    df["RSI"] = rsi(c)
    df["MACD"], df["SIGNAL"], df["HIST"] = macd(c)
    df["STK"], df["STD"] = slow_stoch(h, l, c)
    df["ATR"] = atr(h, l, c)
    return df


def recent_cross(a, b, days=CROSS_DAYS):
    """Cruzamento mais recente de a sobre b nos últimos `days` pregões: 1 = a cruzou para cima, -1 = para baixo."""
    diff = (a - b).tail(days + 1).to_numpy()
    result = 0
    for prev, cur in zip(diff[:-1], diff[1:]):
        if np.isnan(prev) or np.isnan(cur):
            continue
        if prev <= 0 < cur:
            result = 1
        elif prev >= 0 > cur:
            result = -1
    return result


def pivot_lows(low, k=DIV_PIVOT):
    """Índices dos fundos: mínima mais baixa que as k anteriores e não maior que as k seguintes."""
    a = low.to_numpy(dtype=float)
    out = []
    for i in range(k, len(a) - k):
        if np.isnan(a[i - k : i + k + 1]).any():
            continue
        if a[i] < a[i - k : i].min() and a[i] <= a[i + 1 : i + k + 1].min():
            out.append(i)
    return out


def bullish_divergence(low, ind, ind_max=None, margin=0.0):
    """Divergência de alta: 2º fundo do preço mais baixo que o 1º, mas o indicador (RSI/MACD) mais alto.

    Devolve dict com datas/valores dos dois fundos, ou None. O 2º fundo precisa ser recente e não pode ter
    sido rompido depois. `ind_max` exige que o indicador do 1º fundo esteja abaixo desse valor.
    """
    n = len(low)
    lows = low.to_numpy(dtype=float)
    vals = ind.to_numpy(dtype=float)
    piv = pivot_lows(low)
    if len(piv) < 2:
        return None

    def ind_at(i):  # menor valor do indicador perto do fundo (o fundo do indicador pode ser 1-2 pregões deslocado)
        w = vals[max(0, i - 2) : i + 3]
        return None if np.isnan(w).all() else float(np.nanmin(w))

    p2 = piv[-1]
    if n - 1 - p2 > DIV_RECENT or lows[p2 + 1 :].min() < lows[p2]:
        return None  # fundo antigo demais ou já rompido
    v2 = ind_at(p2)
    if v2 is None:
        return None
    for p1 in reversed(piv[:-1]):
        gap = p2 - p1
        if gap > DIV_MAX_GAP:
            break
        if gap < DIV_MIN_GAP:
            continue
        if lows[p2] >= lows[p1] * (1 - DIV_PRICE_TOL):
            continue  # preço não fez fundo mais baixo
        if lows[p1 : p2 + 1].min() < lows[p2]:
            continue  # existe fundo ainda mais baixo no meio
        v1 = ind_at(p1)
        if v1 is None or v2 <= v1 + margin:
            continue  # indicador não fez fundo mais alto
        if ind_max is not None and v1 >= ind_max:
            continue
        d = low.index
        return {
            "d1": d[p1].strftime("%Y-%m-%d"), "p1": round(float(lows[p1]), 2), "v1": round(v1, 3),
            "d2": d[p2].strftime("%Y-%m-%d"), "p2": round(float(lows[p2]), 2), "v2": round(v2, 3),
        }
    return None


def vol_projection(df):
    """Cone de volatilidade: preço esperado em ±1σ e ±2σ nos próximos pregões.

    σ = desvio padrão dos retornos logarítmicos diários (últimos VOL_WINDOW pregões). Em h pregões o desvio
    cresce com a raiz de h. Sem tendência (deriva zero). Teoria: assume retornos ~ normais, então ±1σ cobre ~68%
    e ±2σ ~95% dos casos; na prática as caudas são mais pesadas e o mercado pode sair do cone.
    """
    c = df["Close"]
    r = np.log(c / c.shift()).dropna().tail(VOL_WINDOW)
    if len(r) < 20:
        return None
    s = float(r.std())
    if not np.isfinite(s) or s <= 0:
        return None
    last = float(c.iloc[-1])
    start = pd.Timestamp(df.index[-1]).tz_localize(None) + pd.Timedelta(days=1)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=VOL_HORIZON)]

    def at(h, k):
        return round(last * float(np.exp(k * s * np.sqrt(h))), 2)

    hs = range(1, VOL_HORIZON + 1)
    return {
        "sigma": round(s, 5), "ann": round(s * np.sqrt(252), 4), "n": int(len(r)), "last": round(last, 2),
        "dates": dates,
        "u1": [at(h, 1) for h in hs], "l1": [at(h, -1) for h in hs],
        "u2": [at(h, 2) for h in hs], "l2": [at(h, -2) for h in hs],
        "steps": [{"n": h, "u1": at(h, 1), "l1": at(h, -1), "u2": at(h, 2), "l2": at(h, -2)} for h in VOL_STEPS],
    }


def fib_levels(df):
    """Retração e extensão de Fibonacci sobre o último grande movimento da janela FIB_LOOKBACK."""
    w = df.tail(FIB_LOOKBACK)
    if len(w) < 30:
        return None
    highs, lows = w["High"].to_numpy(dtype=float), w["Low"].to_numpy(dtype=float)
    hi_i, lo_i = int(np.nanargmax(highs)), int(np.nanargmin(lows))
    hi, lo = float(highs[hi_i]), float(lows[lo_i])
    rng = hi - lo
    if hi_i == lo_i or rng <= 0 or rng / hi < 0.03:   # movimento pequeno demais para ser útil
        return None
    up = lo_i < hi_i                                   # mínima antes da máxima = movimento de alta
    if up:   # retrações descem a partir da máxima; extensões projetam acima dela
        ret = [(r, hi - r * rng) for r in FIB_RET]
        ext = [(e, lo + e * rng) for e in FIB_EXT]
    else:    # movimento de baixa: retrações sobem a partir da mínima; extensões projetam abaixo dela
        ret = [(r, lo + r * rng) for r in FIB_RET]
        ext = [(e, hi - e * rng) for e in FIB_EXT]
    ext = [(e, p) for e, p in ext if p > 0]
    return {
        "dir": "alta" if up else "baixa",
        "hi": round(hi, 2), "lo": round(lo, 2),
        "d_hi": w.index[hi_i].strftime("%Y-%m-%d"), "d_lo": w.index[lo_i].strftime("%Y-%m-%d"),
        "ret": [[r, round(p, 2)] for r, p in ret],
        "ext": [[e, round(p, 2)] for e, p in ext],
    }


def series(df, col, nd=4):
    s = df[col]
    return [None if pd.isna(v) else round(float(v), nd) for v in s]


def download(batch):
    """Baixa um lote; tenta de novo com espera crescente se o Yahoo limitar/recusar."""
    raw = None
    for attempt in range(1, 5):
        try:
            raw = yf.download(
                [t + ".SA" for t in batch], period=HISTORY, interval="1d",
                group_by="ticker", auto_adjust=False, threads=False, progress=False,
            )
            if raw is not None and not raw.empty:
                break
            print(f"    tentativa {attempt}: resposta vazia")
        except Exception as e:  # noqa: BLE001
            print(f"    tentativa {attempt}: {type(e).__name__}: {str(e)[:150]}")
        time.sleep(10 * attempt)
    if raw is None or raw.empty:
        return {}
    res = {}
    for t in batch:
        try:
            sub = raw[t + ".SA"] if isinstance(raw.columns, pd.MultiIndex) else raw
            sub = sub.dropna(subset=["Close"])
            if len(sub):
                res[t] = sub[["Open", "High", "Low", "Close", "Volume"]].copy()
        except (KeyError, TypeError):
            pass  # ticker inexistente/deslistado
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="processa só os N primeiros (teste)")
    args = ap.parse_args()

    tickers = load_tickers()
    if args.limit:
        tickers = tickers[: args.limit]
    print(f"{len(tickers)} tickers na lista")

    frames = {}
    for i in range(0, len(tickers), BATCH):
        b = tickers[i : i + BATCH]
        print(f"lote {i // BATCH + 1}/{-(-len(tickers) // BATCH)}")
        frames.update(download(b))
        print(f"  baixados {len(frames)} válidos até agora")
        time.sleep(3)  # pausa entre lotes para não ser limitado

    # benchmark do scanner de acumulação/reversão (BOVA11); se falhar, o scanner segue sem a força relativa
    bench = None
    try:
        bench = institucional.preparar_benchmark(download([institucional.BENCHMARK]).get(institucional.BENCHMARK))
    except Exception as e:  # noqa: BLE001
        print(f"  BOVA11 indisponível ({type(e).__name__}); força relativa não será usada")
    print("  BOVA11:", "ok" if bench is not None else "indisponível")

    # liquidez = média do volume financeiro (Close*Volume) nos últimos LIQ_DAYS pregões
    liq = {}
    for t, df in frames.items():
        tail = df.tail(LIQ_DAYS)
        liq[t] = float((tail["Close"] * tail["Volume"]).mean())
    ranking = sorted(liq, key=liq.get, reverse=True)
    rank = {t: i + 1 for i, t in enumerate(ranking)}

    if OUT.exists() and not OUT.is_dir():  # "data" foi enviado ao repositório como arquivo
        OUT.unlink()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()

    index = []
    for t in ranking:
        df = frames[t]
        if len(df) < 60:
            continue
        try:
            inst = institucional.analisar(df, bench)   # acumulação / reversão (None se faltar histórico)
        except Exception as e:  # noqa: BLE001
            inst = None
            print(f"  {t}: scanner acumulação/reversão falhou ({type(e).__name__}: {str(e)[:100]})")
        df = add_indicators(df.copy())
        last, prev = df.iloc[-1], df.iloc[-2]
        view = df.tail(BARS)
        div_rsi = bullish_divergence(df["Low"], df["RSI"], ind_max=DIV_RSI_MAX, margin=DIV_RSI_MARGIN)
        div_macd = bullish_divergence(df["Low"], df["MACD"], ind_max=0.0)  # MACD do 1º fundo abaixo de zero
        payload = {
            "div": {k: v for k, v in (("rsi", div_rsi), ("macd", div_macd)) if v},
            "vol": vol_projection(df),
            "fib": fib_levels(df),
            "inst": inst,
            "ticker": t,
            "rank": rank[t],
            "liq": liq[t],
            "t": [d.strftime("%Y-%m-%d") for d in view.index],
            "o": series(view, "Open", 2), "h": series(view, "High", 2),
            "l": series(view, "Low", 2), "c": series(view, "Close", 2),
            "v": [int(x) for x in view["Volume"]],
        }
        for key, col in [("ma9", "MA9"), ("sma20", "SMA20"), ("bbu", "BB_UP"), ("bbl", "BB_LO"),
                         ("ema50", "EMA50"), ("ema200", "EMA200"), ("rsi", "RSI"), ("macd", "MACD"),
                         ("sig", "SIGNAL"), ("hist", "HIST"), ("stk", "STK"), ("std", "STD"), ("atr", "ATR")]:
            payload[key] = series(view, col)
        (OUT / f"{t}.json").write_text(json.dumps(payload, separators=(",", ":")))

        def f(x, nd=2):
            return None if pd.isna(x) else round(float(x), nd)

        index.append({
            "t": t, "rank": rank[t], "liq": round(liq[t]),
            "close": f(last["Close"]), "chg": f((last["Close"] / prev["Close"] - 1) * 100),
            "rsi": f(last["RSI"]), "macd": f(last["MACD"], 3),
            "above200": bool(last["Close"] > last["EMA200"]) if not pd.isna(last["EMA200"]) else None,
            # cruzamentos nos últimos CROSS_DAYS pregões: 1 = para cima, -1 = para baixo, 0 = nenhum
            "cross": recent_cross(df["MA9"], df["SMA20"]),        # MA9 x SMA20
            "c2050": recent_cross(df["SMA20"], df["EMA50"]),      # SMA20 x EMA50
            "divrsi": div_rsi is not None,     # divergência de alta no RSI
            "divmacd": div_macd is not None,   # divergência de alta no MACD
            # scanner de acumulação/reversão: sinal ('acum' | 'rev' | None) e pontuações
            "isig": inst["sinal"] if inst else None,
            "isa": inst["sa"] if inst else None, "isc": inst["sc"] if inst else None,
            "date": df.index[-1].strftime("%Y-%m-%d"),
        })

    config = {  # parâmetros em uso; o site mostra isso na "Legenda"
        "cross_days": CROSS_DAYS, "liq_days": LIQ_DAYS,
        "div": {"pivot": DIV_PIVOT, "recent": DIV_RECENT, "min_gap": DIV_MIN_GAP, "max_gap": DIV_MAX_GAP,
                "price_tol_pct": round(DIV_PRICE_TOL * 100, 2), "rsi_margin": DIV_RSI_MARGIN, "rsi_max": DIV_RSI_MAX},
        "vol": {"window": VOL_WINDOW, "horizon": VOL_HORIZON, "steps": list(VOL_STEPS)},
        "fib": {"lookback": FIB_LOOKBACK, "ret": list(FIB_RET), "ext": list(FIB_EXT)},
        "inst": {**institucional.config(), "bench_ok": bench is not None},
    }
    meta = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "count": len(index),
            "cross_days": CROSS_DAYS, "config": config, "items": index}
    (OUT / "index.json").write_text(json.dumps(meta, separators=(",", ":")))
    print(f"OK: {len(index)} ativos gerados em {OUT}")
    if not index:
        sys.exit("Nenhum ativo gerado")


if __name__ == "__main__":
    main()
