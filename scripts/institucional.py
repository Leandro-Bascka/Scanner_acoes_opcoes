"""Scanner institucional B3 (acumulação / reversão), integrado ao build do site.

É o código do seu scanner V1.0, com a mesma lógica de indicadores, pontuações e critérios de sinal.
Diferenças em relação ao original (apenas de integração):
  - em vez de imprimir tabelas, devolve um dicionário por ativo, que o site mostra na lista e no gráfico;
  - o universo é o mesmo de tickers.txt (todos os ativos do site), não uma lista separada;
  - se o BOVA11 não puder ser baixado, o scanner continua e a "força relativa" deixa de pontuar;
  - SCORE_ANT_MIN / SCORE_CONF_MIN são constantes (no original eram os números 50 e 65 dentro do código);
  - o rompimento passou a ser guardado na coluna ROMPIMENTO para poder ser exibido.

Usado por build.py:  preparar_benchmark(df_bova11)  e  analisar(df_ohlcv, benchmark)
"""
import numpy as np
import pandas as pd

# ---------------- CONFIGURAÇÕES (as do scanner original) ----------------
DIAS_SINAL = 4              # o sinal vale se ocorreu nos últimos N pregões
VOLUME_RELATIVO_MIN = 1.30  # volume relativo mínimo para considerar absorção
ATR_STOP = 1.5              # stop = entrada - ATR_STOP x ATR
RISCO_RETORNO_1 = 2.0       # alvo 1 = entrada + risco x 2
RISCO_RETORNO_2 = 3.0       # alvo 2 = entrada + risco x 3
SCORE_ANT_MIN = 50          # pontuação mínima do sinal antecipado
SCORE_CONF_MIN = 65         # pontuação mínima do sinal confirmado
MIN_BARRAS = 210            # histórico mínimo para analisar o ativo
BENCHMARK = "BOVA11"


# ============================================================
# INDICADORES TÉCNICOS (idênticos ao scanner original)
# ============================================================

def calcular_rsi(serie, periodo=14):
    delta = serie.diff()
    ganhos = delta.clip(lower=0)
    perdas = -delta.clip(upper=0)
    media_ganhos = ganhos.ewm(alpha=1 / periodo, min_periods=periodo, adjust=False).mean()
    media_perdas = perdas.ewm(alpha=1 / periodo, min_periods=periodo, adjust=False).mean()
    rs = media_ganhos / media_perdas.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((media_perdas == 0) & (media_ganhos > 0), 100)
    rsi = rsi.mask((media_perdas == 0) & (media_ganhos == 0), 50)
    return rsi


def calcular_adx(df, periodo=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / periodo, min_periods=periodo, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / periodo, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / periodo, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1 / periodo, min_periods=periodo, adjust=False).mean()
    return adx, plus_di, minus_di


def calcular_indicadores(df):
    df = df.copy()
    close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]

    # Médias móveis
    df["EMA20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA50"] = close.ewm(span=50, adjust=False).mean()
    df["EMA200"] = close.ewm(span=200, adjust=False).mean()

    # RSI
    df["RSI"] = calcular_rsi(close)

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["MACD"] - df["MACD_SIGNAL"]

    # ATR e ADX
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["ADX"], df["PLUS_DI"], df["MINUS_DI"] = calcular_adx(df)

    # Volume relativo
    df["VOL_MEDIA"] = volume.rolling(20).mean()
    df["VOL_REL"] = volume / df["VOL_MEDIA"].replace(0, np.nan)

    # OBV
    direcao = np.sign(close.diff()).fillna(0)
    df["OBV"] = (direcao * volume).cumsum()
    df["OBV_MEDIA"] = df["OBV"].ewm(span=10, adjust=False).mean()

    # CMF - Chaikin Money Flow
    amplitude = (high - low).replace(0, np.nan)
    money_flow_multiplier = ((close - low) - (high - close)) / amplitude
    money_flow_volume = money_flow_multiplier * volume
    df["CMF"] = money_flow_volume.rolling(20).sum() / volume.rolling(20).sum().replace(0, np.nan)

    # MFI - Money Flow Index
    typical_price = (high + low + close) / 3
    money_flow = typical_price * volume
    variacao = typical_price.diff()
    fluxo_positivo = money_flow.where(variacao > 0, 0)
    fluxo_negativo = money_flow.where(variacao < 0, 0)
    positivo = fluxo_positivo.rolling(14).sum()
    negativo = fluxo_negativo.rolling(14).sum()
    money_ratio = positivo / negativo.replace(0, np.nan)
    df["MFI"] = 100 - (100 / (1 + money_ratio))
    df["MFI"] = df["MFI"].mask((negativo == 0) & (positivo > 0), 100)

    # Posição do fechamento dentro do candle
    df["POSICAO_CANDLE"] = (close - low) / amplitude

    # Retorno de 20 pregões (base da força relativa contra o BOVA11)
    df["RETORNO_20"] = close.pct_change(20)
    return df


# ============================================================
# DETECÇÃO DE POSSÍVEL REVERSÃO (idêntica ao scanner original)
# ============================================================

def analisar_ativo(df, benchmark):
    df = calcular_indicadores(df)

    if benchmark is not None:
        df = df.join(benchmark[["RETORNO_20"]].rename(columns={"RETORNO_20": "RETORNO_BOVA"}), how="left")
    else:
        df["RETORNO_BOVA"] = np.nan
    df["FORCA_RELATIVA"] = df["RETORNO_20"] - df["RETORNO_BOVA"]

    # Divergência positiva simplificada: preço faz mínima recente, mas RSI não confirma
    preco_minimo = df["Close"].rolling(20).min()
    rsi_minimo = df["RSI"].rolling(20).min()
    df["DIVERGENCIA_RSI"] = (df["Close"] <= preco_minimo * 1.015) & (df["RSI"] > rsi_minimo.shift(1) + 3)

    # Possível absorção vendedora: volume elevado, fechamento na parte superior do candle e CMF positivo
    df["ABSORCAO"] = (
        (df["VOL_REL"] >= VOLUME_RELATIVO_MIN) & (df["POSICAO_CANDLE"] >= 0.65) & (df["CMF"] > 0)
    )

    # Enfraquecimento vendedor
    df["ENFRAQUECIMENTO"] = (df["MINUS_DI"] < df["MINUS_DI"].shift(1)) & (df["RSI"] > df["RSI"].shift(1))

    # Recuperação de momentum
    df["MOMENTUM"] = (df["MACD_HIST"] > df["MACD_HIST"].shift(1)) & (df["RSI"] > df["RSI"].shift(1))

    # PONTUAÇÃO DE REVERSÃO ANTECIPADA
    df["SCORE_ANTECIPADO"] = (
        df["ABSORCAO"].astype(int) * 20
        + df["DIVERGENCIA_RSI"].astype(int) * 20
        + (df["CMF"] > 0).astype(int) * 15
        + (df["OBV"] > df["OBV_MEDIA"]).astype(int) * 10
        + (df["MFI"] > df["MFI"].shift(1)).astype(int) * 10
        + df["ENFRAQUECIMENTO"].astype(int) * 10
        + (df["FORCA_RELATIVA"] > 0).astype(int) * 15
    )

    # PONTUAÇÃO DE REVERSÃO CONFIRMADA
    rompimento = df["Close"] > df["High"].shift(1).rolling(10).max()
    df["ROMPIMENTO"] = rompimento

    df["SCORE_CONFIRMADO"] = (
        (df["Close"] > df["EMA20"]).astype(int) * 20
        + (df["EMA20"] > df["EMA50"]).astype(int) * 15
        + (df["MACD_HIST"] > 0).astype(int) * 15
        + (df["CMF"] > 0).astype(int) * 10
        + (df["VOL_REL"] > 1.2).astype(int) * 10
        + (df["PLUS_DI"] > df["MINUS_DI"]).astype(int) * 10
        + rompimento.astype(int) * 15
        + (df["FORCA_RELATIVA"] > 0).astype(int) * 5
    )

    # Critérios de sinal
    df["SINAL_ANTECIPADO"] = (df["SCORE_ANTECIPADO"] >= SCORE_ANT_MIN) & (df["ABSORCAO"] | df["DIVERGENCIA_RSI"])
    df["SINAL_CONFIRMADO"] = (df["SCORE_CONFIRMADO"] >= SCORE_CONF_MIN) & rompimento & (df["Close"] > df["EMA20"])
    return df


def obter_sinal(df):
    """'rev' (reversão confirmada) tem prioridade sobre 'acum' (possível acumulação); None = sem sinal."""
    ultimos = df.tail(DIAS_SINAL)
    confirmados = ultimos[ultimos["SINAL_CONFIRMADO"].fillna(False)]
    antecipados = ultimos[ultimos["SINAL_ANTECIPADO"].fillna(False)]
    if not confirmados.empty:
        return "rev", confirmados.index[-1]
    if not antecipados.empty:
        return "acum", antecipados.index[-1]
    return None, None


# ============================================================
# INTEGRAÇÃO COM O SITE
# ============================================================

def preparar_benchmark(df_ohlcv):
    """Indicadores do BOVA11 (usado só para o retorno de 20 pregões). None se não houver dados."""
    if df_ohlcv is None or len(df_ohlcv) < 30:
        return None
    return calcular_indicadores(df_ohlcv[["Open", "High", "Low", "Close", "Volume"]].dropna())


def _n(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def analisar(df_ohlcv, benchmark):
    """Resultado do scanner para um ativo (dicionário pronto para JSON) ou None se faltar histórico."""
    df = df_ohlcv[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if len(df) < MIN_BARRAS:
        return None
    df = analisar_ativo(df, benchmark)
    atual, ant = df.iloc[-1], df.iloc[-2]
    sinal, data = obter_sinal(df)

    preco, atr = float(atual["Close"]), float(atual["ATR"])
    risco = ATR_STOP * atr
    stop = preco - risco

    fr = atual["FORCA_RELATIVA"]
    fr_ok = bool(fr > 0) if not pd.isna(fr) else False
    cmf_ok = bool(atual["CMF"] > 0) if not pd.isna(atual["CMF"]) else False

    def b(x):
        return bool(x) if not pd.isna(x) else False

    # componentes das duas pontuações no último pregão: (nome, pontos, ativo?)
    comp_ant = [
        ["Absorção vendedora", 20, b(atual["ABSORCAO"])],
        ["Divergência positiva no RSI", 20, b(atual["DIVERGENCIA_RSI"])],
        ["CMF acima de zero", 15, cmf_ok],
        ["OBV acima da média", 10, b(atual["OBV"] > atual["OBV_MEDIA"])],
        ["MFI subindo", 10, b(atual["MFI"] > ant["MFI"])],
        ["Enfraquecimento vendedor", 10, b(atual["ENFRAQUECIMENTO"])],
        ["Força relativa > BOVA11", 15, fr_ok],
    ]
    comp_conf = [
        ["Fechamento acima da EMA20", 20, b(atual["Close"] > atual["EMA20"])],
        ["EMA20 acima da EMA50", 15, b(atual["EMA20"] > atual["EMA50"])],
        ["Histograma do MACD > 0", 15, b(atual["MACD_HIST"] > 0)],
        ["CMF acima de zero", 10, cmf_ok],
        ["Volume relativo > 1,2x", 10, b(atual["VOL_REL"] > 1.2)],
        ["+DI acima de −DI", 10, b(atual["PLUS_DI"] > atual["MINUS_DI"])],
        ["Rompe a máxima de 10 pregões", 15, b(atual["ROMPIMENTO"])],
        ["Força relativa > BOVA11", 5, fr_ok],
    ]
    return {
        "sinal": sinal,
        "data": data.strftime("%Y-%m-%d") if data is not None else None,
        "idade": (df.index[-1].date() - data.date()).days if data is not None else None,
        "sa": int(atual["SCORE_ANTECIPADO"]), "sc": int(atual["SCORE_CONFIRMADO"]),
        "rsi": _n(atual["RSI"], 1), "cmf": _n(atual["CMF"], 3), "mfi": _n(atual["MFI"], 1),
        "vrel": _n(atual["VOL_REL"], 2), "adx": _n(atual["ADX"], 1),
        "fr": _n(fr * 100, 1) if not pd.isna(fr) else None,   # pontos percentuais vs BOVA11 em 20 pregões
        "plano": {
            "entrada": _n(preco), "stop": _n(stop),
            "alvo1": _n(preco + risco * RISCO_RETORNO_1), "alvo2": _n(preco + risco * RISCO_RETORNO_2),
        },
        "ant": comp_ant, "conf": comp_conf,
        "bench": benchmark is not None,
    }


def config():
    """Parâmetros em uso, mostrados na 'Legenda' do site."""
    return {
        "dias_sinal": DIAS_SINAL, "vol_rel_min": VOLUME_RELATIVO_MIN, "atr_stop": ATR_STOP,
        "rr1": RISCO_RETORNO_1, "rr2": RISCO_RETORNO_2,
        "score_ant_min": SCORE_ANT_MIN, "score_conf_min": SCORE_CONF_MIN, "benchmark": BENCHMARK,
    }
