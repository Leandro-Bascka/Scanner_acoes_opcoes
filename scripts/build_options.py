"""
Opcoes_B3 — coleta de dados de OPÇÕES (Wheel / Calls longas). Projeto 100% independente (não usa o Smc_B3).

  - cotações de opções ........ opcoes.net.br  (mesmo endpoint do seu listar_opcoes; requisições simples em paralelo)
  - preço do ativo ............ estimado pelas próprias opções (paridade put-call); nenhum outro site é usado
  - HV (volatilidade histórica)  OPCIONAL: só com --hv-yahoo (desligado por padrão)
  - Selic ..................... BCB SGS 432

Saída (site/data/opcoes/): meta.json, longas.json, <ATIVO>.json, <ATIVO>_px.json

Uso:  python build_options.py --out site                 (dados reais)
      python build_options.py --out site --demo          (dados SIMULADOS)
      python build_options.py --diag PETR4               (mostra as linhas cruas da API, para conferir os campos)
      python build_options.py --out site --hv-yahoo      (opcional: HV e candles via Yahoo)
"""
import argparse
import json
import math
import os
import sys
import threading
import time
import zlib
import subprocess
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

def demo_frame(ticker, bars=260):
    rng = np.random.default_rng(zlib.crc32(ticker.encode()))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=bars)
    price, rows = rng.uniform(8, 60), []
    for _ in idx:
        o = price * (1 + rng.normal(0, 0.004)); c = o * (1 + rng.normal(0.0004, 0.016))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.006))); l = min(o, c) * (1 - abs(rng.normal(0, 0.006)))
        rows.append((o, h, l, c, float(rng.lognormal(15, 0.45)))); price = max(c, 1.0)
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


COLS = ["ticker", "venc", "tipo", "strike", "preco", "neg", "ult", "vol"]
SELIC_PADRAO = 0.14
DTE_MAX = 730
BCB_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://opcoes.net.br/opcoes/bovespa",
}


def dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def num(x):
    try:
        x = float(x)
    except Exception:
        return None
    return None if (math.isnan(x) or math.isinf(x)) else x


# ----------------------------------------------------------------------------
# Acesso à API do opcoes.net.br — requisições simples e em PARALELO (sem fila nem espera entre elas).
# Só desacelera sozinho se o site responder HTTP 429.
# ----------------------------------------------------------------------------
API_BASE = os.environ.get("OPCOES_API", "https://opcoes.net.br")


class Net:
    def __init__(self, workers=12, intervalo=0.0):
        self.intervalo = intervalo
        self.base = intervalo
        self.n429 = 0
        self.lock = threading.Lock()
        self.sem = threading.Semaphore(workers)
        self.prox = 0.0
        self.pausa_ate = 0.0
        self.sess = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=workers, pool_maxsize=workers)
        self.sess.mount("https://", ad)
        self.sess.mount("http://", ad)
        self.erro = ""
        self.deadline = None          # prazo rígido (epoch): depois dele nenhuma requisição é tentada

    def _vez(self):
        with self.lock:
            agora = time.time()
            saida = max(agora, self.prox, self.pausa_ate)
            self.prox = saida + self.intervalo
        if saida > agora:
            time.sleep(saida - agora)

    def get_json(self, url, tentativas=3):
        for t in range(tentativas):
            if self.deadline and time.time() >= self.deadline:
                self.erro = "tempo máximo da etapa atingido"
                return None
            self._vez()
            try:
                with self.sem:
                    if t % 2 == 0:
                        r = self.sess.get(url, timeout=10)                      # requisição simples
                    else:
                        r = self.sess.get(url, timeout=15, headers=HEADERS)     # com cabeçalhos de navegador
                if r.status_code == 429:
                    try:
                        pausa = min(max(float(r.headers.get("Retry-After", "")), 5.0), 60.0)
                    except Exception:
                        pausa = min(5.0 * (2 ** t), 40.0)                       # 5s, 10s, 20s, 40s
                    self.pausa_ate = max(self.pausa_ate, time.time() + pausa)   # pausa GLOBAL: ninguém insiste junto
                    self.n429 += 1
                    self.intervalo = min(max(self.intervalo * 1.5, 0.6), 2.0)   # e as próximas ficam mais espaçadas
                    self.erro = "HTTP 429 – o site limitou as requisições"
                    continue
                if r.status_code != 200:
                    self.erro = f"HTTP {r.status_code} – {r.text[:120]!r}"
                else:
                    try:
                        if self.intervalo > self.base:                          # o site aliviou: acelera devagar
                            self.intervalo = max(self.base, self.intervalo * 0.97)
                        return r.json()
                    except ValueError:
                        self.erro = f"resposta não é JSON – {r.text[:120]!r}"
            except Exception as e:  # noqa: BLE001
                self.erro = f"{type(e).__name__}: {e}"
            time.sleep(0.5 * (t + 1))
        return None


def dados_ok(d):
    return isinstance(d, dict) and isinstance(d.get("data"), dict) and d.get("success", True) is not False


def vencimentos(net, ativo):
    for cot in ("false", "true"):
        d = net.get_json(f"{API_BASE}/listaopcoes/completa?idAcao={ativo}&listarVencimentos=true&cotacoes={cot}")
        if not dados_ok(d):
            return []
        v = [x["value"] for x in (d["data"].get("vencimentos") or [])]
        if v:
            return v
    return []


def baixar_venc(net, ativo, venc):
    """Linhas de um vencimento. None = falhou (será tentado de novo); [] = vencimento sem opções negociadas."""
    d = net.get_json(f"{API_BASE}/listaopcoes/completa?idAcao={ativo}&listarVencimentos=false"
                     f"&cotacoes=true&vencimentos={venc}")
    if not dados_ok(d):
        return None
    rows = []
    for i in d["data"].get("cotacoesOpcoes") or []:
        try:
            # Mapeamento do SEU listar_opcoes: [0] ticker | [2] tipo | [5] strike | [7] último | [8] negócios | [9] volume | [10] data/hora
            # Se o site tiver uma coluna a mais antes do preço, detecta pela data (fica em [10] ou em [11]) e desloca 1.
            b = 7
            if len(i) > 11 and not _eh_data(i[10]) and _eh_data(i[11]):
                b = 8
            tipo = str(i[2]).upper()
            if tipo not in ("CALL", "PUT"):
                continue
            strike = float(i[5]) if i[5] is not None else None
            preco = float(i[b]) if i[b] is not None else None
            if preco is None or preco <= 0 or strike is None or strike <= 0:
                continue
            neg = int(float(i[b + 1])) if len(i) > b + 1 and i[b + 1] is not None else 0
            vol = float(i[b + 2]) if len(i) > b + 2 and i[b + 2] is not None else 0.0
            if neg <= 0 or vol <= 0:               # só opções que negociaram de fato (como no seu código simples)
                continue
            ult = str(i[b + 3])[:10] if len(i) > b + 3 and i[b + 3] is not None else ""
            rows.append([str(i[0]).split("_")[0], venc, tipo, strike, preco, neg, ult, vol])
        except Exception:
            continue
    return rows


def _eh_data(x):
    return isinstance(x, str) and len(x) >= 8 and x[:4].isdigit() and "-" in x[:8] or (isinstance(x, str) and "/" in x[:6])


def vencimentos_validos(lista, hoje, dte_max=DTE_MAX):
    out = []
    for v in lista:
        try:
            dias = (pd.to_datetime(v).normalize() - hoje).days
        except Exception:
            continue
        if 1 <= dias <= dte_max:
            out.append(v)
    return out


# ----------------------------------------------------------------------------
# Preço / HV (Yahoo) e Selic (BCB)
# ----------------------------------------------------------------------------
def hv_anual(close, n):
    ret = np.log(close / close.shift(1)).dropna()
    if len(ret) < 10:
        return float("nan")
    return float(ret.tail(n).std() * np.sqrt(252))


def liquidos_smc(base, n):
    """As n ações mais líquidas, na ordem de liquidez, lidas do site do Smc_B3 (data/meta.json -> 'liquidos')."""
    try:
        r = requests.get(f"{base.rstrip('/')}/data/meta.json", timeout=20)
        lst = [str(t).upper() for t in (r.json().get("liquidos") or [])]
        print(f"  mais líquidas (Smc_B3): {min(n, len(lst))} de {len(lst)} disponíveis", flush=True)
        return lst[:n]
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] não consegui ler a lista de liquidez do Smc_B3 ({type(e).__name__}); usando só opcoes_ativos.txt", flush=True)
        return []


def historicos_url(base, ativos):
    """Candles diários já publicados pelo projeto Smc_B3 (GitHub Pages): <base>/data/<ATIVO>_1d.json.
    Não usa Yahoo: só baixa um arquivo pequeno por ativo do próprio GitHub Pages. Ativos fora do scanner SMC ficam sem."""
    base = base.rstrip("/")

    def um(a):
        try:
            r = requests.get(f"{base}/data/{a}_1d.json", timeout=20)
            if r.status_code != 200:
                return a, None
            c = r.json()["candles"]
            h = pd.DataFrame({"Open": [x["open"] for x in c], "High": [x["high"] for x in c], "Low": [x["low"] for x in c],
                              "Close": [x["close"] for x in c], "Volume": [x["volume"] for x in c]},
                             index=pd.to_datetime([x["time"] for x in c]))
            return a, (h if len(h) >= 30 else None)
        except Exception:
            return a, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = {a: h for a, h in ex.map(um, ativos) if h is not None}
    print(f"  histórico de preços do Smc_B3: {len(out)}/{len(ativos)} ativos ({base})", flush=True)
    return out


def _yahoo_um(a):
    import yfinance as yf
    h = yf.Ticker(a + ".SA").history(period="1y", interval="1d", auto_adjust=False, timeout=15)
    if h is None or len(h) < 30:
        return None
    h = h[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if getattr(h.index, "tz", None) is not None:
        h.index = h.index.tz_localize(None)
    return h


def _cache_ler(cdir, a, max_h):
    """Histórico em disco. max_h = idade máxima em horas (None = aceita qualquer idade, só como plano B)."""
    p = os.path.join(cdir, f"{a}.json")
    try:
        if max_h is not None and time.time() - os.path.getmtime(p) > max_h * 3600:
            return None
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        h = pd.DataFrame(c["r"], columns=["Open", "High", "Low", "Close", "Volume"], index=pd.to_datetime(c["d"]))
        return h if len(h) >= 30 else None
    except Exception:
        return None


def _cache_gravar(cdir, a, h):
    try:
        os.makedirs(cdir, exist_ok=True)
        dump(os.path.join(cdir, f"{a}.json"), {"d": [d.strftime("%Y-%m-%d") for d in h.index],
                                               "r": [[float(x) for x in row] for row in h.to_numpy()]})
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] não gravei cache de histórico de {a}: {e}", flush=True)


def historicos_yahoo(ativos, cdir="cache", cache_h=20.0, max_seg=90, workers=4):
    """Histórico diário (HV/candles). Ordem: cache em disco com menos de cache_h horas -> Yahoo em paralelo
    (só quem não está no cache) -> cache velho como plano B. O HV só muda 1x por dia: normalmente 0 requisições ao Yahoo."""
    out, faltam = {}, []
    for a in ativos:
        h = _cache_ler(cdir, a, cache_h)
        if h is not None:
            out[a] = h
        else:
            faltam.append(a)
    print(f"  histórico: {len(out)} do cache ({cache_h:.0f}h), {len(faltam)} a baixar no Yahoo", flush=True)
    if faltam:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            print("  [aviso] yfinance não instalado (pip install yfinance); seguindo sem HV novo.")
            faltam_yf = []
        else:
            faltam_yf = faltam
        limitado, t0 = threading.Event(), time.time()

        def um(a):
            if limitado.is_set() or time.time() - t0 > max_seg:
                return a, None
            try:
                return a, _yahoo_um(a)
            except Exception as e:  # noqa: BLE001
                if "ratelimit" in f"{type(e).__name__} {e}".lower() or "too many" in str(e).lower():
                    limitado.set()
                return a, None

        if faltam_yf:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for a, h in ex.map(um, faltam_yf):
                    if h is not None:
                        out[a] = h
                        _cache_gravar(cdir, a, h)
            if limitado.is_set():
                print("  [aviso] Yahoo limitou as requisições; o resto usa o cache antigo (se houver).", flush=True)
        for a in faltam:
            if a not in out:
                h = _cache_ler(cdir, a, None)
                if h is not None:
                    out[a] = h
                    print(f"  [info] {a}: usando histórico antigo do cache", flush=True)
    return out


def estimar_spot(rows, hoje, selic):
    """Preço do ativo estimado só pelas opções (paridade put-call: C - P = S - K·e^(-rT)), usando os pares
    mais próximos de 'no dinheiro'. Plano C: só é usado se não houver histórico de preços. None se faltar dado."""
    r = math.log(1 + selic)
    calls, puts = {}, {}
    for t, venc, tipo, k, p, neg, ult, vol in rows:
        try:
            dias = (pd.to_datetime(venc) - hoje).days
        except Exception:
            continue
        if 20 <= dias <= 120 and neg >= 3 and p >= 0.05:
            (calls if tipo == "CALL" else puts)[(venc, k)] = (p, dias)
    est = []
    for key, (c, dias) in calls.items():
        if key in puts:
            pp = puts[key][0]
            est.append((abs(c - pp), c - pp + key[1] * math.exp(-r * dias / 365)))
    if len(est) < 3:
        return None
    est.sort()
    v = sorted(x[1] for x in est[:7])
    return float(v[len(v) // 2])


def estatisticas(h):
    close = h["Close"]
    hv20, hv60 = hv_anual(close, 20), hv_anual(close, 60)
    vals = [v for v in (hv20, hv60) if not math.isnan(v)]
    hv = float(np.mean(vals)) if vals else float("nan")
    return float(close.iloc[-1]), hv20, hv60, hv


def selic_bcb():
    try:
        r = requests.get(BCB_URL, timeout=10)
        r.raise_for_status()
        ult = r.json()[-1]
        v = float(str(ult["valor"]).replace(",", ".")) / 100
        if 0.01 < v < 0.50:
            return v, str(ult.get("data", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] Selic/BCB indisponível: {e}")
    return None, ""


# ----------------------------------------------------------------------------
# Modo demonstração (dados SIMULADOS)
# ----------------------------------------------------------------------------
def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, r, sig, tipo):
    vt = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + sig * sig / 2) * T) / vt
    d2 = d1 - vt
    if tipo == "CALL":
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def _terceiras_sextas(hoje, meses):
    out = []
    y, m = hoje.year, hoje.month
    for _ in range(meses):
        d = pd.Timestamp(year=y, month=m, day=1)
        sextas = [d + pd.Timedelta(days=i) for i in range(31) if (d + pd.Timedelta(days=i)).month == m
                  and (d + pd.Timedelta(days=i)).weekday() == 4]
        out.append(sextas[2])
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return [x for x in out if x > hoje]


def demo_opcoes(ativo, S, hv, hoje):
    rng = np.random.default_rng(zlib.crc32(ativo.encode()) ^ 0xBEEF)
    r = math.log(1 + SELIC_PADRAO)
    vencs = _terceiras_sextas(hoje, 8) + [pd.Timestamp(year=hoje.year + 1, month=1, day=15 + (4 - pd.Timestamp(year=hoje.year + 1, month=1, day=15).weekday()) % 7)]
    step = 0.5 if S < 25 else 1.0
    ks = np.arange(round(S * 0.7 / step) * step, S * 1.3, step)
    rows = []
    for v in vencs:
        dias = (v - hoje).days
        if dias < 1 or dias > DTE_MAX:
            continue
        base = (hv if hv == hv else 0.3) * rng.uniform(0.8, 1.35)
        for K in ks:
            for tipo in ("CALL", "PUT"):
                sig = max(0.12, base * (1 + 0.25 * (S - K) / S))
                p = _bs(S, float(K), dias / 365, r, sig, tipo) + rng.normal(0, 0.01)
                if p < 0.03:
                    continue
                letra = chr(65 + v.month - 1 + (12 if tipo == "PUT" else 0))
                rows.append([f"{ativo[:4]}{letra}{int(K * 10)}", v.strftime("%Y-%m-%d"), tipo, round(float(K), 2),
                             round(float(p), 2), int(rng.lognormal(3.0, 1.1)),
                             (hoje - pd.Timedelta(days=int(rng.integers(0, 3)))).strftime("%Y-%m-%d"),
                             float(int(rng.lognormal(8, 1)))])
    return rows


# ----------------------------------------------------------------------------
FUNDAMENTUS_URL = "https://www.fundamentus.com.br/resultado.php"


def _tickers_fundamentus(html, n, com_liq=False):
    """Tabela do Fundamentus -> tickers ordenados por liquidez média de 2 meses (Liq.2meses), maiores primeiro."""
    df = pd.read_html(StringIO(html), decimal=",", thousands=".")[0][["Papel", "Liq.2meses"]].copy()
    liq = df["Liq.2meses"]
    if liq.dtype == object:                                   # texto tipo "1.234.567,89" que o pandas não converteu
        liq = liq.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    df["liq"] = pd.to_numeric(liq, errors="coerce")
    df["Papel"] = df["Papel"].astype(str).str.strip().str.upper()
    df = df[df["Papel"].str.fullmatch(r"[A-Z]{4}(3|4|5|6|11)") & (df["liq"] > 0)]    # só ações/units com opções possíveis
    df = df.sort_values("liq", ascending=False).drop_duplicates("Papel")
    if com_liq:
        return [(a, float(b)) for a, b in zip(df["Papel"].head(n), df["liq"].head(n))]
    return df["Papel"].head(n).tolist()


def universo_fundamentus(n, cdir="cache", max_h=24.0):
    """Candidatos = as n ações mais líquidas do Fundamentus. Cache de 24h (a liquidez de 2 meses muda devagar).
    Falhou e não há cache? devolve [] e o chamador usa o opcoes_ativos.txt."""
    p = os.path.join(cdir, "_universo.json")
    try:
        if time.time() - os.path.getmtime(p) < max_h * 3600:
            with open(p, encoding="utf-8") as f:
                lst = json.load(f)["tickers"]
            if len(lst) >= min(n, 30):
                print(f"  universo: {min(n, len(lst))} tickers do cache (<{max_h:.0f}h)", flush=True)
                return lst[:n]
    except Exception:
        pass
    try:
        r = requests.get(FUNDAMENTUS_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        rk = _tickers_fundamentus(r.text, max(n, 250), com_liq=True)
        lst = [a for a, _ in rk]
        if len(lst) < 30:
            raise ValueError(f"tabela com poucos tickers ({len(lst)})")
        os.makedirs(cdir, exist_ok=True)
        dump(p, {"tickers": lst, "liq": {a: b for a, b in rk}, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        print(f"  universo: {min(n, len(lst))} tickers mais líquidos do Fundamentus", flush=True)
        return lst[:n]
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] Fundamentus indisponível ({type(e).__name__}: {e}); tentando cache antigo / opcoes_ativos.txt", flush=True)
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)["tickers"][:n]
        except Exception:
            return []


def escrever_universo(path, tickers, cdir="cache", tam_lote=10):
    """Grava a lista final (ordem de liquidez) com o lote de cada ativo. Serve de conferência e fica publicada no site."""
    liq = {}
    try:
        with open(os.path.join(cdir, "_universo.json"), encoding="utf-8") as f:
            liq = json.load(f).get("liq", {})
    except Exception:
        pass
    itens = [{"pos": i + 1, "t": t, "lote": i // tam_lote + 1, "liq_2meses": liq.get(t)} for i, t in enumerate(tickers)]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        dump(path, {"gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fonte": "Fundamentus (Liq.2meses)",
                    "tamanho_lote": tam_lote, "n": len(itens), "ativos": itens})
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] não gravei {path}: {e}", flush=True)
    return itens


def universo_graficos(gdir, n):
    """As n ações mais líquidas segundo o projeto de Gráficos (docs/data/index.json, já ordenado por liquidez)."""
    try:
        with open(os.path.join(gdir, "index.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        items = sorted(items, key=lambda x: x.get("rank", 10 ** 9))
        lst = [str(x["t"]).upper() for x in items][:n]
        print(f"  universo: {len(lst)} ações mais líquidas dos Gráficos ({gdir})", flush=True)
        return lst
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] não consegui ler {gdir}/index.json ({type(e).__name__}: {e})", flush=True)
        return []


def historicos_graficos(gdir, ativos):
    """Candles diários que o projeto de Gráficos já gerou (docs/data/<ATIVO>.json): HV20/HV60 sem nenhuma requisição ao Yahoo."""
    out = {}
    for a in ativos:
        try:
            with open(os.path.join(gdir, f"{a}.json"), encoding="utf-8") as f:
                d = json.load(f)
            h = pd.DataFrame({"Open": d["o"], "High": d["h"], "Low": d["l"], "Close": d["c"], "Volume": d["v"]},
                             index=pd.to_datetime(d["t"])).dropna(subset=["Close"])
            if len(h) >= 30:
                out[a] = h
        except Exception:
            pass
    print(f"  histórico dos Gráficos: {len(out)}/{len(ativos)} ativos ({gdir})", flush=True)
    return out


def obter_universo(args):
    if args.graficos_dir:
        lst = universo_graficos(args.graficos_dir, args.n_universo)
        if lst:
            return lst
        print("  [aviso] Gráficos indisponíveis; usando o Fundamentus como plano B", flush=True)
    """As N ações mais líquidas (Fundamentus; cache 24h). Plano B: opcoes_ativos.txt."""
    lst = universo_fundamentus(args.n_universo, args.hist_cache)
    if not lst:
        print("  [ERRO] sem universo: não há ranking dos Gráficos nem do Fundamentus. Rode antes o scripts/build.py.", flush=True)
    return lst


def rodar_lotes(args):
    """--lote 0: roda os lotes (de --lote-tam ativos) um por vez, cada um em um processo próprio e com tempo limite.
    Um lote que falhar ou estourar o tempo não derruba os outros; os dados dos lotes anteriores ficam preservados."""
    universo = obter_universo(args)
    if not universo:
        return 0
    escrever_universo(os.path.join(args.out, "data", "opcoes", "universo_80.json"), universo, args.hist_cache, args.lote_tam)
    n = -(-len(universo) // args.lote_tam)
    base, skip, i = [], False, 1
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--lote":
            skip = True
            continue
        if a.startswith("--lote="):
            continue
        base.append(a)
    print(f"Universo: {len(universo)} ações em {n} lote(s) de {args.lote_tam}", flush=True)
    t0 = time.time()
    for i in range(1, n + 1):
        if i > 1 and args.pausa_lotes > 0:
            time.sleep(args.pausa_lotes)
        print(f"\n=== LOTE {i}/{n}: {', '.join(universo[(i - 1) * args.lote_tam:i * args.lote_tam])} ===", flush=True)
        try:
            subprocess.run([sys.executable, "-u", os.path.abspath(__file__)] + base + ["--lote", str(i)],
                           timeout=(args.max_min + 4) * 60)
        except subprocess.TimeoutExpired:
            print(f"[aviso] lote {i} passou do tempo e foi interrompido; os demais seguem.", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[aviso] lote {i} falhou: {e}", flush=True)
    print(f"\nTodos os lotes terminaram em {time.time() - t0:.0f}s.", flush=True)
    return 0


def ler_ativos(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        ts = [l.split("#")[0].strip().upper() for l in f]
    seen, out = set(), []
    for t in ts:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def recuperar_anterior(prev_url, ativo, out):
    """Última coleta boa publicada no Pages (quando a coleta atual do ativo falhar)."""
    try:
        base = prev_url.rstrip("/") + "/data/opcoes/"
        a = requests.get(base + f"{ativo}.json", timeout=20)
        p = requests.get(base + f"{ativo}_px.json", timeout=20)
        if a.status_code == 200 and p.status_code == 200:
            ja, jp = a.json(), p.json()
            if ja.get("rows"):
                dump(os.path.join(out, f"{ativo}.json"), ja)
                dump(os.path.join(out, f"{ativo}_px.json"), jp)
                return ja
    except Exception as e:  # noqa: BLE001
        print(f"   [aviso] sem coleta anterior para {ativo}: {e}")
    return None


def restaurar_tudo(prev_url, out, lista):
    """Se a coleta atual quase não trouxe nada, recupera TODOS os ativos da última coleta boa publicada (marcados 'stale')."""
    try:
        r = requests.get(prev_url.rstrip("/") + "/data/opcoes/meta.json", timeout=20)
        m = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] sem coleta anterior para restaurar ({type(e).__name__})", flush=True)
        return [], []
    tem = {x["t"] for x in lista}
    novos = []
    alvo = [x for x in m.get("ativos", []) if x["t"] not in tem]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for x, ja in zip(alvo, ex.map(lambda x: recuperar_anterior(prev_url, x["t"], out), alvo)):
            if ja:
                novos.append({"t": x["t"], "n": len(ja["rows"]), "fetched_at": ja.get("fetched_at"), "stale": True})
    print(f"  restaurados {len(novos)} ativo(s) da última coleta boa publicada", flush=True)
    return novos, m.get("liquidos", [])


def gerar_longas(out, lista, dmin=60):
    """Um único arquivo com só as CALLs de 60–730 dias de todos os ativos: a tela "Calls longas" baixa
    1 arquivo pequeno em vez de um por ativo (muito mais rápido, principalmente no celular)."""
    assets, n = {}, 0
    faixas = {"<60d": 0, "60-179d": 0, "180-364d": 0, ">=365d": 0}
    for item in lista:
        try:
            with open(os.path.join(out, f"{item['t']}.json"), encoding="utf-8") as f:
                a = json.load(f)
            base = pd.Timestamp((pd.Timestamp(a["fetched_at"]) - timedelta(hours=3)).date())
            rows = []
            for r in a["rows"]:
                if r[2] != "CALL":
                    continue
                dias = (pd.to_datetime(r[1]) - base).days
                faixas["<60d" if dias < 60 else "60-179d" if dias < 180 else "180-364d" if dias < 365 else ">=365d"] += 1
                if dmin <= dias <= 730:
                    rows.append(r)
            if rows:
                assets[item["t"]] = {"S": a["S"], "hv": a.get("hv"), "fetched_at": a["fetched_at"], "rows": rows}
                n += len(rows)
        except Exception as e:  # noqa: BLE001
            print(f"   [aviso] longas: {item['t']} ignorado ({e})")
    print(f"  CALLs negociadas coletadas por prazo: {faixas}  (entram no longas.json: DTE {dmin}-730)", flush=True)
    if n == 0:
        print(f"  [aviso] nenhuma CALL negociada com DTE >= {dmin}: baixe --leaps-min (ex.: 90) ou a fonte não tem LEAPS com negócios.", flush=True)
    dump(os.path.join(out, "longas.json"), {"cols": COLS, "assets": assets})
    return n


def diagnostico(ativo):
    net = Net(2, 0.0)
    print(f"[diag] endereço base: {API_BASE}")
    d = net.get_json(f"{API_BASE}/listaopcoes/completa?idAcao={ativo}&listarVencimentos=true&cotacoes=false")
    if not dados_ok(d):
        print(f"[diag] FALHOU ao listar vencimentos: {net.erro or str(d)[:300]}")
        return 1
    vs = [x.get("value") for x in (d["data"].get("vencimentos") or [])]
    print(f"[diag] {len(vs)} vencimentos: {vs[:6]} ...")
    hoje = pd.Timestamp((datetime.now(timezone.utc) - timedelta(hours=3)).date())
    vv = vencimentos_validos(vs, hoje)
    if not vv:
        print("[diag] nenhum vencimento futuro."); return 1
    d = net.get_json(f"{API_BASE}/listaopcoes/completa?idAcao={ativo}&listarVencimentos=false&cotacoes=true&vencimentos={vv[0]}")
    if not dados_ok(d):
        print(f"[diag] FALHOU ao baixar {vv[0]}: {net.erro}"); return 1
    cot = d["data"].get("cotacoesOpcoes") or []
    print(f"[diag] chaves de data: {list(d['data'].keys())}")
    print(f"[diag] vencimento {vv[0]}: {len(cot)} linhas cruas. As 3 primeiras (índice: valor):")
    for r in cot[:3]:
        print("   ", {k: v for k, v in enumerate(r)})
    ok = baixar_venc(net, ativo, vv[0])
    print(f"[diag] linhas aproveitadas (negócios>0 e volume>0): {len(ok or [])}")
    for r in (ok or [])[:3]:
        print("   ", dict(zip(COLS, r)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--ativos", default="", help="(opcional, modo antigo sem lotes) arquivo com a lista de tickers")
    ap.add_argument("--universo", default="fundamentus", choices=["fundamentus", "arquivo"],
                    help="origem dos candidatos: 'fundamentus' = ações mais líquidas (Liq.2meses); 'arquivo' = só opcoes_ativos.txt")
    ap.add_argument("--cand", type=int, default=120, help="quantos candidatos (mais líquidos no Fundamentus) entram na triagem de opções; o --top escolhe os finais")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--prev-url", default="")
    ap.add_argument("--workers", type=int, default=6, help="requisições simultâneas ao opcoes.net.br (mais = mais rápido, mais risco de HTTP 429)")
    ap.add_argument("--intervalo", type=float, default=0.25, help="segundos de espera entre requisições (0 = sem espera; sobe sozinho se o site limitar)")
    ap.add_argument("--max-min", type=float, default=14.0, help="tempo máximo da coleta de opções, em minutos")
    ap.add_argument("--top", type=int, default=0, help="entre os candidatos de opcoes_ativos.txt, mantém só os N mais líquidos EM OPÇÕES (volume negociado no opcoes.net.br); 0 = todos")
    ap.add_argument("--wheel-dte", type=int, default=45, help="na triagem de liquidez (--top) olha só vencimentos até N dias; as LEAPS dos N escolhidos são baixadas depois")
    ap.add_argument("--leaps-min", type=int, default=365, help="LEAPS: baixa só vencimentos com mais de N dias (padrão 365)")
    ap.add_argument("--leaps-max-venc", type=int, default=4, help="por ação, baixa só os N vencimentos LEAPS mais longos (padrão 4): é o que mais reduz as requisições")
    ap.add_argument("--pausa-lotes", type=float, default=20.0, help="segundos de descanso entre lotes (deixa o site aliviar o limite de requisições)")
    ap.add_argument("--put-dte", type=int, default=60, help="PUT/Wheel: baixa todos os vencimentos de 1 a N dias (padrão 60)")
    ap.add_argument("--leaps-ttl-h", type=float, default=6.0, help="reaproveita os vencimentos LEAPS da coleta anterior se tiverem menos de N horas (0 = sempre baixa)")
    ap.add_argument("--hist-cache", default="cache", help="pasta do cache de histórico diário (HV)")
    ap.add_argument("--hist-cache-h", type=float, default=20.0, help="idade máxima (h) do cache de histórico antes de consultar o Yahoo de novo")
    ap.add_argument("--hist-url", default="", help="opcional: endereço de outro site (GitHub Pages) com candles já publicados; não é usado por padrão")
    ap.add_argument("--hv-yahoo", action="store_true", help="opcional: baixa histórico no Yahoo para HV/candles (padrão: desligado)")
    ap.add_argument("--graficos-dir", default="", help="pasta docs/data do projeto de Gráficos: define o universo (mais líquidas) e fornece os candles/HV")
    ap.add_argument("--lote", type=int, default=None, help="coleta por lotes: N = só o lote N; 0 = roda todos os lotes em sequência")
    ap.add_argument("--lote-tam", type=int, default=10, help="ativos por lote (padrão 10)")
    ap.add_argument("--n-universo", type=int, default=80, help="quantas ações mais líquidas (Fundamentus) formam o universo dos lotes")
    ap.add_argument("--gerar-universo", action="store_true", help="só gera/mostra a lista das N ações mais líquidas (universo_80.json) e sai")
    ap.add_argument("--diag", default="", help="só diagnóstico: mostra as linhas cruas da API para o ativo (ex.: --diag PETR4)")
    ap.add_argument("--only", default="", help="só estes ativos, separados por vírgula (ex.: PETR4,VALE3)")
    args = ap.parse_args()

    if args.diag:
        return diagnostico(args.diag.strip().upper())
    if args.gerar_universo:
        uni = obter_universo(args)
        itens = escrever_universo("universo_80.json", uni, args.hist_cache, args.lote_tam)
        for it in itens:
            print(f"  {it['pos']:>3}. {it['t']:<7} lote {it['lote']}  liq 2m: {it['liq_2meses'] if it['liq_2meses'] is not None else 'n/d'}")
        print(f"universo_80.json gravado ({len(itens)} ações).")
        return 0
    if args.lote == 0:
        return rodar_lotes(args)
    out = os.path.join(args.out, "data", "opcoes")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    prazo = t0 + args.max_min * 60
    agora = datetime.now(timezone.utc)
    hoje = pd.Timestamp((agora - timedelta(hours=3)).date())
    ativos, universo = [], []
    if args.lote is not None and not args.demo:
        universo = obter_universo(args)
        ativos = universo[(args.lote - 1) * args.lote_tam:args.lote * args.lote_tam]
        args.top = 0                                        # universo já é o das mais líquidas: sem triagem
        if not ativos:
            print(f"Lote {args.lote}: vazio (universo tem {len(universo)} ações).")
            return 0
        print(f"Lote {args.lote}: {', '.join(ativos)}", flush=True)
    elif args.universo == "fundamentus" and not args.demo:
        ativos = universo_fundamentus(args.cand, args.hist_cache)
    if not ativos and args.lote is None:
        ativos = ler_ativos(args.ativos)                    # plano B / modo arquivo / demo
    if args.only:
        pedidos = [t.strip().upper() for t in args.only.split(",") if t.strip()]
        ativos = [t for t in pedidos if t in ativos] or pedidos
    print(f"Opções: {len(ativos)} ativos ({'DEMO' if args.demo else 'reais'})")

    selic, selic_data = (SELIC_PADRAO, "demo") if args.demo else selic_bcb()
    fonte_selic = "demo" if args.demo else ("bcb" if selic else "padrao")
    if selic is None:
        selic = SELIC_PADRAO
    print(f"Selic: {selic * 100:.2f}% ({fonte_selic}{' ' + selic_data if selic_data else ''})")

    if args.lote is not None and not args.demo and args.prev_url and not os.path.exists(os.path.join(out, "meta.json")):
        novos_, liq_ = restaurar_tudo(args.prev_url, out, [])
        if novos_:
            dump(os.path.join(out, "meta.json"), {"ativos": novos_, "falhas": [], "liquidos": liq_, "sem_hv": []})
    net = Net(args.workers, args.intervalo)
    lista, falhas, motivos = [], [], {}
    total = len(ativos)
    hoje_str = agora.isoformat(timespec="seconds")

    def gravar(ativo, h, rows):
        if h is not None:
            S, hv20, hv60, hv = estatisticas(h)
            fonte = "graficos"
            if pd.Timestamp(h.index[-1]).normalize() < hoje:     # candle de dias anteriores: o preço do ativo vem das opções (mais atual)
                Sp = estimar_spot(rows, hoje, selic)
                if Sp:
                    S, fonte = Sp, "paridade+hv"
            candles = [[d.strftime("%Y-%m-%d"), round(float(r.Open), 2), round(float(r.High), 2), round(float(r.Low), 2),
                        round(float(r.Close), 2), int(r.Volume)] for d, r in h.tail(170).iterrows()]
        else:                                               # sem histórico: preço pela paridade, sem HV nem candles
            S = estimar_spot(rows, hoje, selic)
            if S is None:
                raise ValueError("sem pares put/call negociados para estimar o preço (use --hv-yahoo para obter o preço pelo Yahoo)")
            hv20 = hv60 = hv = float("nan")
            fonte, candles = "paridade", []
        dump(os.path.join(out, f"{ativo}_px.json"), {"candles": candles})
        dump(os.path.join(out, f"{ativo}.json"), {
            "ativo": ativo, "fetched_at": hoje_str, "S": S, "S_fonte": fonte, "hv20": num(hv20), "hv60": num(hv60),
            "hv": num(hv), "leaps_at": leaps_at_por.get(ativo, hoje_str), "cols": COLS, "rows": rows})
        return {"t": ativo, "n": len(rows), "fetched_at": hoje_str, "hv": num(hv) is not None}, S, hv

    def historico(ativo):
        return demo_frame(ativo, bars=260).tail(252) if args.demo else None

    rows_por = {a: [] for a in ativos}
    leaps_at_por = {}
    erro_ativo = {}
    liquidos = []
    hists = {}

    def coletar(pares, ate):
        """Baixa (ativo, vencimento) em paralelo até o instante 'ate'. Devolve os pares que falharam/ficaram pendentes."""
        falhou = []
        if not pares:
            return falhou
        net.deadline = ate
        t1, passo = time.time(), max(1, len(pares) // 5)
        ex = ThreadPoolExecutor(max_workers=args.workers)
        futs = {ex.submit(baixar_venc, net, a, v): (a, v) for a, v in pares}
        try:
            for k, f in enumerate(as_completed(futs, timeout=max(5.0, ate - time.time() + 20)), 1):
                a, v = futs[f]
                r = f.result()
                if r is None:
                    falhou.append((a, v))
                else:
                    rows_por[a].extend(r)
                if k % passo == 0 or k == len(pares):
                    print(f"    {k}/{len(pares)} vencimentos ({time.time() - t1:.0f}s)", flush=True)
        except TimeoutError:
            pass
        pend = [av for f, av in futs.items() if not f.done()]
        if pend:
            print(f"  [aviso] tempo da etapa esgotado; {len(pend)} vencimento(s) ficaram sem baixar.", flush=True)
            for a_, v_ in pend:
                erro_ativo.setdefault(a_, "tempo esgotado em alguns vencimentos")
        ex.shutdown(wait=False, cancel_futures=True)
        return falhou

    def prev_leaps(ativo):
        """Linhas LEAPS por vencimento da coleta anterior, se ela for recente (--leaps-ttl-h). None se não houver."""
        try:
            p = os.path.join(out, f"{ativo}.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    a = json.load(f)
            elif args.prev_url:
                r = requests.get(args.prev_url.rstrip("/") + f"/data/opcoes/{ativo}.json", timeout=15)
                if r.status_code != 200:
                    return None
                a = r.json()
            else:
                return None
            ref = a.get("leaps_at") or a.get("fetched_at")
            if (agora - pd.Timestamp(ref).tz_convert("UTC").to_pydatetime()).total_seconds() > args.leaps_ttl_h * 3600:
                return None
            lim = args.leaps_min
            por = {}
            for r in a["rows"]:
                if (pd.to_datetime(r[1]).normalize() - pd.Timestamp((pd.Timestamp(a["fetched_at"]) - timedelta(hours=3)).date())).days >= lim:
                    por.setdefault(r[1], []).append(r)
            return (por, ref) if por else None
        except Exception:
            return None

    def carregar_hist(lista):
        h = {a: historico(a) for a in lista}
        if args.graficos_dir and not args.demo:
            h.update(historicos_graficos(args.graficos_dir, lista))
        if args.hist_url and not args.demo:
            h.update(historicos_url(args.hist_url, lista))
        if args.hv_yahoo and not args.demo:
            h.update(historicos_yahoo([a for a in lista if h[a] is None], args.hist_cache, args.hist_cache_h))
        sem = [a for a in lista if h[a] is None]
        if sem and not args.demo:
            print(f"  [info] sem HV/candles (preço pela paridade put-call) em {len(sem)} ativo(s); use --hv-yahoo para ter HV.", flush=True)
        return h

    if args.demo:
        hists = carregar_hist(ativos)
        for a in ativos:
            S, _, _, hv = estatisticas(hists[a])
            rows_por[a] = demo_opcoes(a, S, hv, hoje)
    else:
        t1 = time.time()
        meio = t0 + (prazo - t0) * 0.35                       # triagem de liquidez: no máximo 35% do tempo total
        net.deadline = meio if args.top else prazo
        # Os vencimentos da B3 são os mesmos para (quase) todas as ações: 1 consulta em vez de 1 por ativo.
        # Vencimento que não existir para um ativo volta vazio (barato).
        glob = []
        for ref in ("PETR4", "VALE3", "ITUB4", "BBAS3"):
            glob = vencimentos_validos(vencimentos(net, ref), hoje, DTE_MAX)
            if glob:
                break
        venc_por = {a: list(glob) for a in ativos}
        if not glob:
            for a in ativos:
                erro_ativo[a] = net.erro or "nenhum vencimento retornado"
        dias = lambda v: (pd.to_datetime(v).normalize() - hoje).days
        print(f"  vencimentos listados em {time.time() - t1:.0f}s", flush=True)
        if args.top:
            # 1ª etapa: triagem de liquidez só com os 2 vencimentos curtos mais próximos de cada candidato (rápido)
            def _rep(vs):
                cand = [x for x in vs if dias(x) <= args.wheel_dte]
                boa = [x for x in cand if dias(x) >= 10]
                return (boa or cand)[:1]
            curtos = [(a, v) for a in ativos for v in _rep(venc_por[a])]
            print(f"  triagem de liquidez: {len(ativos)} candidatos, {len(curtos)} vencimentos...", flush=True)
            falhou = coletar(curtos, meio)
            volume = {a: sum(r[7] for r in rows_por[a]) for a in ativos}
            liquidos = sorted([a for a in ativos if rows_por[a]], key=lambda a: -volume[a])
            sem_dados = [a for a in ativos if not rows_por[a]]
            if sem_dados:
                print(f"  [info] {len(sem_dados)} candidato(s) sem opções negociadas ou sem dados (ignorados): {', '.join(sem_dados)}", flush=True)
            ativos = liquidos[:args.top]
            print(f"  mantidos os {len(ativos)} mais líquidos em opções: {', '.join(ativos)}", flush=True)
            for a in list(rows_por):
                if a not in ativos:
                    del rows_por[a]
            # 2ª etapa (só dos escolhidos): (a) demais vencimentos de PUT até --put-dte; (b) LEAPS > --leaps-min
            falhou = [x for x in falhou if x[0] in rows_por]
            ja = {(a, v) for a in rows_por for v in {r[1] for r in rows_por[a]}} | {(a, v) for a, v in curtos}
            put_venc = [(a, v) for a in ativos for v in venc_por[a] if 1 <= dias(v) <= args.put_dte and (a, v) not in ja]
            reaproveitados = 0
            longos = []
            for a in ativos:
                lv = [v for v in venc_por[a] if dias(v) >= args.leaps_min][-max(1, args.leaps_max_venc):]
                pr = prev_leaps(a) if (lv and args.leaps_ttl_h > 0) else None
                prev = pr[0] if pr else None
                if pr:
                    leaps_at_por[a] = pr[1]          # mantém a data da coleta original: o prazo (TTL) continua valendo
                for v in lv:
                    if prev is not None and v in prev:
                        rows_por[a].extend(prev[v])
                        reaproveitados += 1
                    else:
                        longos.append((a, v))
            print(f"  PUT ≤{args.put_dte}d: {len(put_venc)} vencimentos; LEAPS >{args.leaps_min}d: {len(longos)} a baixar, "
                  f"{reaproveitados} reaproveitados (<{args.leaps_ttl_h:g}h)...", flush=True)
            falhou += coletar(put_venc + longos, prazo)
        else:
            pares, reaproveitados = [], 0
            for a in ativos:
                lv = [v for v in venc_por[a] if dias(v) >= args.leaps_min][-max(1, args.leaps_max_venc):]
                pr = prev_leaps(a) if (lv and args.leaps_ttl_h > 0) else None
                prev = pr[0] if pr else None
                if pr:
                    leaps_at_por[a] = pr[1]
                pares += [(a, v) for v in venc_por[a] if 1 <= dias(v) <= args.put_dte]
                for v in lv:
                    if prev is not None and v in prev:
                        rows_por[a].extend(prev[v])
                        reaproveitados += 1
                    else:
                        pares.append((a, v))
            print(f"  PUT ≤{args.put_dte}d + LEAPS >{args.leaps_min}d: {len(pares)} vencimentos a baixar, "
                  f"{reaproveitados} LEAPS reaproveitados (<{args.leaps_ttl_h:g}h)", flush=True)
            falhou = coletar(pares, prazo)
        net.deadline = None
        hists = carregar_hist(ativos)
        if falhou and time.time() < prazo:                   # nova passada, mais devagar, só dos que falharam
            print(f"  refazendo {len(falhou)} vencimento(s) que falharam, em ritmo mais lento...", flush=True)
            net.intervalo = max(net.intervalo, 0.3)
            net.pausa_ate = 0.0
            net.deadline = prazo + 120
            with ThreadPoolExecutor(max_workers=3) as ex:
                for (a, v), r in zip(falhou, ex.map(lambda av: baixar_venc(net, av[0], av[1]), falhou)):
                    if r is None:
                        erro_ativo.setdefault(a, f"vencimento {v}: {net.erro}")
                    else:
                        rows_por[a].extend(r)
    total = len(ativos)

    for k, ativo in enumerate(ativos, 1):
        try:
            rows = rows_por[ativo]
            if not rows:
                raise ValueError(erro_ativo.get(ativo) or net.erro or "nenhuma cotação de opção")
            item, S, hv = gravar(ativo, hists[ativo], rows)
            lista.append(item)
            print(f"  [{k:>2}/{total}] {ativo:<7} {item['n']:>5} opções  S={S:.2f}  HV={hv * 100:.0f}%", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{k:>2}/{total}] {ativo:<7} FALHOU: {e}", flush=True)
            prev = recuperar_anterior(args.prev_url, ativo, out) if args.prev_url else None
            if prev:
                lista.append({"t": ativo, "n": len(prev["rows"]), "fetched_at": prev.get("fetched_at"), "stale": True})
                print(f"           {ativo:<7} usando coleta anterior ({prev.get('fetched_at')})")
            else:
                falhas.append(ativo)
                motivos[ativo] = str(e)[:140]
    if net.n429:
        print(f"[info] o site limitou {net.n429}x (HTTP 429); a coleta desacelerou sozinha.")

    sem_hv_prev = []
    if args.lote is not None and not args.demo:
        try:
            with open(os.path.join(out, "meta.json"), encoding="utf-8") as f:
                antigo = json.load(f)
        except Exception:
            antigo = {}
        uset, nesta, tem = set(universo), set(ativos), {x["t"] for x in lista}
        for x in antigo.get("ativos", []):
            if x["t"] not in nesta and x["t"] not in tem and x["t"] in uset:
                lista.append(x)
                tem.add(x["t"])
        for t in antigo.get("falhas", []):
            if t not in nesta and t in uset and t not in tem and t not in falhas:
                falhas.append(t)
                motivos[t] = (antigo.get("falhas_motivo") or {}).get(t, "")
        sem_hv_prev = [t for t in antigo.get("sem_hv", []) if t not in nesta and t in uset]
        liquidos = [t for t in universo if t in tem]
    n_frescos = sum(1 for x in lista if not x.get("stale"))
    if args.prev_url and not args.demo and args.lote is None and n_frescos < 5:
        print(f"[aviso] a coleta trouxe só {n_frescos} ativo(s) novo(s); mantendo também os dados da última coleta boa.", flush=True)
        novos, liq_prev = restaurar_tudo(args.prev_url, out, lista)
        lista += novos
        if not liquidos:
            liquidos = liq_prev
        falhas[:] = [t for t in falhas if t not in {x["t"] for x in lista}]
    n_long = gerar_longas(out, lista, args.leaps_min)
    print(f"longas.json: {n_long} calls de {args.leaps_min} a 730 dias ({os.path.getsize(os.path.join(out, 'longas.json')) // 1024} KB)")

    meta = {
        "updated_at": agora.isoformat(timespec="seconds"),
        "updated_brt": (agora - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M"),
        "selic": selic, "selic_fonte": fonte_selic, "selic_data": selic_data,
        "demo": bool(args.demo), "ativos": lista, "falhas": falhas, "falhas_motivo": motivos, "liquidos": liquidos or [x["t"] for x in lista], "sem_hv": [a for a in ativos if hists.get(a) is None] + sem_hv_prev, "seconds": round(time.time() - t0, 1),
        "erro": "" if lista else (net.erro or "nenhum ativo coletado"),
    }
    dump(os.path.join(out, "meta.json"), meta)
    fresh = sum(1 for a in lista if not a.get("stale"))
    print(f"Concluído: {fresh} atualizados, {len(lista) - fresh} da coleta anterior, {len(falhas)} falhas "
          f"em {time.time() - t0:.0f}s.")
    return 0   # nunca derruba o deploy do site: sem opções, a aba mostra o aviso


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)          # não espera threads (Yahoo/requests) que eventualmente fiquem presas
