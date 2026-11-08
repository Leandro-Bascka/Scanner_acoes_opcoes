# Scanner_Acoes_Opcoes

Um só site no GitHub Pages, com duas partes que trabalham em sequência:

1. **Gráficos** (abre primeiro, na raiz do site): baixa ~420 ações da B3, calcula os indicadores e **ordena por liquidez**.
2. **Opções** (`/opcoes/`): usa as **50 ações mais líquidas** desse ranking e coleta as opções (**PUT e CALL**) só delas, em **lotes de 10**.

As opções usam os candles já gerados pelos Gráficos para a volatilidade histórica (HV20/HV60): não há segunda ida ao Yahoo.

## Como o workflow roda (`.github/workflows/atualizar.yml`)
| Quando | O que faz |
|---|---|
| 18:30 (Brasília), seg–sex | Recalcula os gráficos (Yahoo) e depois as opções das 50 mais líquidas |
| 10:05 a 17:05, seg–sex | Só as opções, reaproveitando os gráficos guardados em cache |
| Manual (*Run workflow*) | Pergunta se quer recalcular os gráficos |

Na primeira execução (sem cache) os gráficos são sempre calculados.

## Publicar
1. Crie o repositório **Scanner_Acoes_Opcoes** (público), envie todos os arquivos (a pasta `.github/workflows` inclusive).
2. **Settings → Pages → Source: GitHub Actions**.
3. **Actions → Atualizar gráficos e opções → Run workflow**.
4. Site: `https://SEU_USUARIO.github.io/Scanner_Acoes_Opcoes/` (Gráficos) e `.../opcoes/` (Opções).

## Rodar no seu computador (PyCharm)
```bash
pip install -r requirements.txt
python scripts/build.py                                   # 1) gráficos -> docs/data
python scripts/build_options.py --out site/opcoes --graficos-dir docs/data \
       --n-universo 50 --lote 0 --lote-tam 10 --put-dte 60 --leaps-min 90
# montar o site: copie docs/* para site/ e opcoes/{index.html,opcoes.js,opcore.js,opcoes.css,manifest.webmanifest} para site/opcoes/
python -m http.server -d site 8000
```

## Navegação
- Nos Gráficos, o botão **📊 Opções** abre a tela de opções; dentro de um gráfico, o mesmo botão abre as opções **daquele ticker**.
- Nas Opções, **← Gráficos** volta.

## Ajustes úteis
- Quantas ações: `--n-universo` (50) e `--lote-tam` (10) no workflow.
- Prazos: `--put-dte 60` (PUT até 60 dias) e `--leaps-min 90` (CALLs longas acima de 90 dias; use 365 para só LEAPS de verdade).
- Sinais das PUTs e CALLs longas: `opcoes/opcore.js`. Indicadores dos gráficos: `scripts/build.py`.
- **Não existe lista fixa de ações**: o universo é sempre as 50 primeiras do ranking de liquidez que os Gráficos acabaram de gerar.
- Ações sem opções negociadas entre as 50 aparecem como "falhas" e não atrapalham o resto.

Ferramenta educacional, não é recomendação de investimento.
