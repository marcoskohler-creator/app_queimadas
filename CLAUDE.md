# CLAUDE.md — Contexto do Projeto para Claude Code

Este arquivo é lido automaticamente pelo Claude Code ao abrir esta pasta.
Ele descreve o projeto, os comandos esperados e os pontos de atenção
conhecidos, para que qualquer sessão comece já com contexto.

## O que é este projeto

Aplicação web para detecção de área queimada usando imagens Sentinel-2
(bandas B04/B08/B12, índice dNBR + filtro de vegetação por NDVI) via
Sentinel Hub (Copernicus Data Space Ecosystem).

- Backend: FastAPI (`app/main.py`, `app/pipeline.py`)
- Frontend: HTML/JS estático com mapa Leaflet (`static/index.html`)
- Empacotado em Docker (imagem base já traz GDAL)
- Saída: shapefile em SIRGAS2000 (EPSG:4674) + área em hectares
- **Modelo de credenciais: cada usuário informa seu próprio Client
  ID/Secret do Sentinel Hub no formulário, por requisição.** O servidor
  NÃO armazena nenhuma credencial (nem em variável de ambiente, nem em
  disco, nem em banco).

## Objetivo desta sessão local

Buildar a imagem Docker, subir o container, validar que a API responde e
depurar quaisquer erros de dependência (GDAL/rasterio/geopandas costumam
ser os pontos mais frágeis) até o fluxo completo funcionar ponta a ponta.

## Comandos principais

### Opção A — Docker (recomendada, evita lidar com GDAL manualmente)

```bash
docker build -t area-queimada .
docker run -p 8000:8000 area-queimada
# Acessar http://localhost:8000 e preencher as credenciais NO FORMULÁRIO
```

### Opção B — Sem Docker, via conda/mamba (recomendada se não quiser Docker)

GDAL é uma dependência de sistema, não só uma lib Python — por isso
`pip install rasterio geopandas` puro costuma falhar ou dar erro de
versão incompatível. O conda-forge resolve isso empacotando o GDAL junto.

```bash
# Se ainda não tiver conda/mamba, instale o Miniforge:
# https://github.com/conda-forge/miniforge

mamba env create -f environment.yml
mamba activate area-queimada

uvicorn app.main:app --reload --port 8000
# Acessar http://localhost:8000
```

Se preferir `conda` em vez de `mamba`, troque o comando (mais lento para
resolver dependências, mas funciona igual):
```bash
conda env create -f environment.yml
conda activate area-queimada
```

### Opção C — Sem Docker e sem conda (pip puro)

Só recomendado se você **já tem GDAL instalado no sistema operacional**
(ex. `apt install gdal-bin libgdal-dev` no Ubuntu, ou `brew install gdal`
no macOS) e sabe a versão exata instalada:

```bash
gdalinfo --version
pip install GDAL==3.9.0   # casar com a versão exata do sistema
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
No Windows sem Docker, esse caminho costuma dar mais trabalho — WSL2 ou
a Opção B (conda) são mais confiáveis.

## Como testar rapidamente (sem usar o frontend)

Como as credenciais agora vêm no corpo da requisição (não em variável de
ambiente), o `curl` de teste precisa incluí-las:

```bash
curl -X POST http://localhost:8000/processar \
  -H "Content-Type: application/json" \
  -d '{
        "data_referencia": "2024-08-15",
        "latitude": -15.7801,
        "longitude": -47.9292,
        "janela_dias": 30,
        "nuvem_maxima": 30,
        "limiar_dnbr": 0.1,
        "raio_km": 3.0,
        "sentinel_hub_client_id": "SEU_CLIENT_ID",
        "sentinel_hub_client_secret": "SEU_CLIENT_SECRET"
      }'

# Copie o job_id retornado e consulte:
curl http://localhost:8000/status/SEU_JOB_ID
```

## Pontos de atenção conhecidos (leia antes de "consertar" algo)

1. **GDAL é a causa mais provável de erro, com ou sem Docker.** Com Docker,
   o `Dockerfile` usa `ghcr.io/osgeo/gdal:ubuntu-small-3.9.2` como base
   para evitar compilar GDAL na mão. Sem Docker, use `environment.yml`
   (conda/mamba) pelo mesmo motivo — não tente `pip install rasterio`
   isolado como primeira tentativa de correção, isso raramente resolve.

2. **Credenciais são por-requisição, não por-servidor — isso é
   intencional, não uma lacuna a "corrigir".** `pipeline.obter_token()`
   exige `client_id`/`client_secret` como parâmetros explícitos; não
   existe (e não deve existir) leitura de `CDSE_CLIENT_ID`/
   `CDSE_CLIENT_SECRET` via `os.environ` em lugar nenhum do código. Se
   um teste falhar por falta de credencial, a correção é passar
   `sentinel_hub_client_id`/`sentinel_hub_client_secret` na requisição —
   nunca reintroduzir uma credencial global no servidor.

3. **A busca de cenas usa o Catalog API do Sentinel Hub (STAC):**
   `https://sh.dataspace.copernicus.eu/catalog/v1/search` (não confundir
   com a URL antiga/depreciada `/api/v1/catalog/1.0.0/search`, nem com o
   OData do CDSE). `buscar_cena_sentinel2()` em `pipeline.py` faz POST com
   filtro CQL2 simples (`eo:cloud_cover < X`) e ordena os resultados no
   próprio Python (não usa `sortby` da API). O Process API também tem URL
   específica: `https://sh.dataspace.copernicus.eu/process/v1`.

4. **As bandas vêm via Process API, já recortadas na área de interesse**
   (parâmetro `raio_km`, padrão 3km) — não baixamos produtos SAFE
   completos. O evalscript (`EVALSCRIPT_BANDAS`) retorna B04, B08, B12 e
   `dataMask` em uma única chamada por data (antes/depois). B04 existe
   especificamente para calcular NDVI pré-evento e filtrar áreas que não
   eram vegetação (ver ponto 5) — qualquer mudança nas bandas usadas
   precisa refletir tanto no evalscript quanto no cálculo em
   `gerar_poligono_area_queimada()`.

5. **Filtro de vegetação (NDVI) evita falso-positivo em área urbana.**
   Um pixel só entra no polígono se o dNBR passar do limiar **E** o NDVI
   da imagem "antes" for >= `LIMIAR_NDVI_VEGETACAO_FIXO` (constante fixa
   em `pipeline.py`, atualmente 0.35) — ou seja, precisa já ter sido
   vegetação antes do evento. Além disso, o polígono final passa por uma
   **erosão fixa** (`EROSAO_M_FIXO`, 3m) que remove a faixa de borda onde
   pixels mistos (parte vegetação, parte estrada/construção) podem
   escapar do filtro de NDVI. Essas duas constantes são intencionalmente
   **fixas, não expostas via API/formulário** — alterar exige mexer no
   código e redeploy. Só `filtro_pixels` (área mínima expressa em número
   de pixels conectados, não em m²) é ajustável pelo usuário.

6. **Estado de jobs é em memória (`JOBS = {}` em `main.py`).** Isso é uma
   limitação conhecida e aceita para uso local/individual — não é algo a
   "corrigir" automaticamente trocando por um banco de dados sem que isso
   seja pedido explicitamente.

7. **Não versionar credenciais.** Nunca commitar `APP_USERNAME`/
   `APP_PASSWORD` em código ou `.env` versionado. As credenciais Sentinel
   Hub dos usuários nunca são persistidas em lugar nenhum — não adicionar
   logging, cache ou armazenamento delas em hipótese alguma.

8. **`APP_USERNAME`/`APP_PASSWORD` são opcionais**, protegem o site
   inteiro (frontend + API) via HTTP Basic Auth quando definidos — ver
   `MiddlewareAutenticacao` em `main.py`. Sem elas, o app fica aberto
   (aceitável nesse modelo, já que não há credencial compartilhada em
   risco, só o uso do servidor gratuito em si).

9. **Cada teste ponta a ponta consome cota de processamento (Processing
   Units) da conta Sentinel Hub usada no teste.** Evite rodar o pipeline
   completo repetidamente só para depurar erros de sintaxe — prefira
   testar funções isoladas (ex. `epsg_sirgas2000_utm`, `calcular_nbr`,
   `calcular_bbox_wgs84`) com dados sintéticos primeiro.

10. **Deploy alvo é o plano free do Render, sem disco persistente**
    (`render.yaml`). Isso é aceitável porque o fluxo completo (processar
    → baixar) acontece na mesma sessão viva da instância — não
    "corrigir" adicionando disco pago sem que isso seja pedido.

## Padrões do projeto (não alterar sem necessidade)

- CRS de saída do shapefile: **sempre EPSG:4674 (SIRGAS2000)**.
- Cálculo de área: sempre reprojetado para SIRGAS2000/UTM da zona correta
  via `epsg_sirgas2000_utm()` — não usar CRS geográfico (graus) para
  calcular área.
- Limiar padrão de dNBR: `0.1` (calibrado pelo usuário) — ajustável via
  parâmetro da API, não hardcoded em outro lugar do código.
- Limiar padrão de NDVI-vegetação: `0.35` — **fixo em código**
  (`LIMIAR_NDVI_VEGETACAO_FIXO` em `pipeline.py`), não exposto via API/UI.
- Erosão de borda: `3m` — **fixo em código** (`EROSAO_M_FIXO`), mesmo
  motivo.
- Filtro por pixel padrão: `3` pixels (~300 m², resolução de 10m/pixel via
  `RESOLUCAO_M_FIXA`) — esse sim é ajustável via parâmetro `filtro_pixels`
  (API e formulário). A conversão pixels→m² acontece em
  `executar_pipeline()`, nunca hardcoded em outro lugar.
- Coordenadas no frontend aceitam **graus decimais e graus/minutos/
  segundos** (conversão feita em JS puro em `static/index.html`, sem
  chamada ao backend).
