# Mapeamento de Cicatriz de Incêndio (dNBR) — App Web

Aplicação web (FastAPI + frontend com mapa Leaflet) que detecta área queimada
usando as bandas B08/B12 do Sentinel-2 (via Sentinel Hub / Copernicus Data
Space Ecosystem), calcula o índice dNBR, vetoriza a área queimada em polígono
e retorna:

1. Shapefile georreferenciado em **SIRGAS2000 (EPSG:4674)**
2. Área queimada em **hectares** (calculada em SIRGAS2000/UTM, zona automática)

## Satélites disponíveis

- **Sentinel-2** (10m/pixel, padrão): via Sentinel Hub, exige credencial
  própria do usuário (ver seção abaixo).
- **Landsat 8/9** (30m/pixel): via Earth Search (Element84/AWS), API
  pública, **sem necessidade de credencial nenhuma**. Boa alternativa
  quando não há cena Sentinel-2 disponível na janela de datas, ou para
  quem não quer criar conta no Copernicus. Contorno menos detalhado por
  causa da resolução mais grossa.

## Modelo de credenciais: cada usuário usa as próprias

O servidor **não guarda nenhuma credencial do Sentinel Hub**. Cada pessoa
que usa o app informa seu próprio Client ID / Client Secret diretamente no
formulário, a cada consulta — eles são enviados só para processar aquela
requisição específica e não ficam salvos em banco de dados, arquivo ou
variável de ambiente do servidor. Isso permite manter o deploy no **plano
gratuito** do Render (sem custo de disco persistente) e faz com que o
consumo de cota (Processing Units) seja sempre da conta de quem está
usando, não de uma conta compartilhada.

**Onde cada usuário obtém suas próprias credenciais:**
1. Acesse https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings
   (crie uma conta Copernicus grátis, se ainda não tiver)
2. Vá na aba **"OAuth clients"**
3. Clique em **"Create new OAuth client"**, dê um nome qualquer
4. Copie o **Client ID** e o **Client Secret** exibidos (o secret só
   aparece uma vez) e cole no formulário do app, na seção "Suas
   credenciais Sentinel Hub"

## Proteção do site (opcional, recomendado)

Como as credenciais agora são de cada usuário, publicar sem senha nenhuma
já não arrisca a cota de ninguém além de quem está usando — o risco que
resta é alguém usar o servidor gratuito à toa (gastando as poucas horas
mensais do free tier do Render). Por isso, uma senha simples continua
disponível, mas agora é opcional.

Se quiser ativar, defina `APP_USERNAME` e `APP_PASSWORD` (HTTP Basic Auth,
protege o site inteiro). Sem essas variáveis, o app fica aberto.

## Estrutura

```
app_queimada/
├── app/
│   ├── main.py        # API FastAPI (endpoints, jobs assíncronos, auth opcional)
│   └── pipeline.py     # Lógica de busca/download/dNBR/vetorização
├── static/
│   └── index.html      # Frontend (mapa Leaflet + formulário + campos de credencial)
├── requirements.txt
├── Dockerfile
├── render.yaml          # Blueprint de deploy no Render (plano free)
└── README.md
```

## Rodando localmente

### Com Docker (mais simples — GDAL já vem resolvido na imagem)

```bash
docker build -t area-queimada .
docker run -p 8000:8000 area-queimada

# Opcional, para exigir senha:
docker run -p 8000:8000 \
  -e APP_USERNAME=escolha_um_usuario \
  -e APP_PASSWORD=escolha_uma_senha \
  area-queimada
```

### Sem Docker (via conda/mamba)

```bash
mamba env create -f environment.yml
mamba activate area-queimada

uvicorn app.main:app --reload --port 8000
```

Acesse `http://localhost:8000`. No formulário, preencha suas próprias
credenciais Sentinel Hub na seção "Suas credenciais Sentinel Hub" antes
de clicar em "Processar".

> GDAL é uma dependência de sistema, não só uma lib Python — instalar via
> `pip install rasterio geopandas` puro costuma quebrar por incompatibilidade
> de versão. O `environment.yml` usa o canal `conda-forge`, que empacota o
> GDAL certinho junto com os bindings Python.

## Publicar de verdade na web (deploy no Render, plano free)

Passo a passo completo, do zero:

1. **Crie um repositório no GitHub** e suba esta pasta inteira nele
   (via interface web do GitHub, "uploading an existing file", ou
   `git init && git add . && git commit -m "app queimada" && git push`
   se preferir linha de comando).
2. Crie uma conta grátis em https://render.com (dá pra logar com GitHub).
3. No painel do Render, clique em **New → Blueprint**.
4. Selecione o repositório que você acabou de criar — o Render lê o
   `render.yaml` automaticamente e já propõe o serviço configurado no
   **plano free**.
5. Se quiser ativar a senha opcional do site, preencha `APP_USERNAME` e
   `APP_PASSWORD` (não ficam expostas no `render.yaml` — `sync: false`).
   Se deixar em branco, o app fica sem senha.
6. Clique em **Apply/Deploy**. O Render builda a imagem Docker (leva
   alguns minutos, por causa do GDAL) e, ao terminar, mostra uma URL
   pública do tipo `https://area-queimada-sentinel2.onrender.com`.
7. Essa URL já é permanente — pode compartilhar, favoritar, usar no
   celular. Ela só muda se você deletar o serviço e criar outro.

**Sobre as limitações do plano free do Render (leia antes de usar):**
- A instância **"dorme" após ~15 minutos sem uso** e leva alguns segundos
  para "acordar" na próxima visita — normal, sem custo.
- **Sem disco persistente**: os arquivos de cada job (shapefile, geojson)
  ficam só na memória/disco temporário da instância enquanto ela está
  ativa. Isso é aceitável no modelo atual porque o fluxo todo (processar →
  baixar o shapefile) acontece na mesma sessão, com a instância acordada.
  Se você processar algo, deixar a aba aberta por muito tempo sem baixar,
  e a instância dormir nesse meio-tempo, pode ser necessário processar de
  novo antes de conseguir baixar.
- Free tier tem limite de horas mensais — não é "ilimitado para sempre",
  mas é suficiente para uso esporádico/institucional.

## Deploy no Railway (alternativa)

1. Crie um novo projeto no Railway e conecte o repositório.
2. Railway detecta o `Dockerfile` automaticamente.
3. Em **Variables**, opcionalmente adicione `APP_USERNAME` e `APP_PASSWORD`.
4. Railway atribui a porta via `$PORT` automaticamente — o `Dockerfile`
   já está preparado para isso (`--port ${PORT:-8000}`).

## Limitações conhecidas (importante)

- **Estado em memória**: o dicionário `JOBS` em `main.py` é um MVP — se o
  serviço reiniciar (ou tiver mais de 1 worker/réplica), o histórico de
  jobs se perde. Para produção real, trocar por Redis ou banco de dados.
- **Sem fila real**: usa `BackgroundTasks` do FastAPI (roda na mesma
  instância). Para volume alto de requisições simultâneas, migrar para
  Celery/RQ + Redis.
- **Credenciais trafegam por requisição HTTPS** (o Render fornece TLS
  gratuito) — não ficam salvas em lugar nenhum do servidor, mas cada
  usuário precisa reinserir a cada sessão nova do navegador (não há
  "lembrar credenciais" implementado, por design, para não guardar segredo
  de terceiros).
- **Limpeza de disco**: os arquivos de cada job não são apagados
  automaticamente enquanto a instância está viva — use o endpoint
  `DELETE /job/{job_id}` ou reinicie o serviço periodicamente.
