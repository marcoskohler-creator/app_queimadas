import os
import uuid
import shutil
import zipfile
import secrets
import traceback
from datetime import datetime

from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from pydantic import BaseModel, Field

from . import pipeline

app = FastAPI(title="Mapeamento de Cicatriz de Incêndio - CBMMG (SIRGAS2000)")

# --- Proteção por senha (HTTP Basic Auth) ---
# Se APP_USERNAME/APP_PASSWORD não estiverem definidos, o app fica
# ABERTO (sem senha) - só recomendado para testes locais. Ao publicar
# na internet (Render/Railway), defina essas duas variáveis de ambiente.
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
_seguranca = HTTPBasic()


def _checar_credenciais(credenciais: HTTPBasicCredentials = Depends(_seguranca)):
    usuario_ok = secrets.compare_digest(credenciais.username, APP_USERNAME)
    senha_ok = secrets.compare_digest(credenciais.password, APP_PASSWORD)
    if not (usuario_ok and senha_ok):
        raise HTTPException(
            status_code=401,
            detail="Credenciais inválidas.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credenciais.username


class MiddlewareAutenticacao(BaseHTTPMiddleware):
    """Exige HTTP Basic Auth em TODAS as rotas (frontend + API) quando
    APP_USERNAME/APP_PASSWORD estão configurados."""
    async def dispatch(self, request, call_next):
        if not APP_USERNAME or not APP_PASSWORD:
            return await call_next(request)

        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            import base64
            try:
                usuario, senha = base64.b64decode(auth[6:]).decode().split(":", 1)
            except Exception:
                usuario, senha = "", ""
            if secrets.compare_digest(usuario, APP_USERNAME) and \
               secrets.compare_digest(senha, APP_PASSWORD):
                return await call_next(request)

        return Response(
            content="Autenticação necessária.",
            status_code=401,
            headers={"WWW-Authenticate": "Basic"},
        )


if APP_USERNAME and APP_PASSWORD:
    app.add_middleware(MiddlewareAutenticacao)
else:
    print("=" * 70)
    print("AVISO: APP_USERNAME/APP_PASSWORD não configurados - app SEM SENHA.")
    print("Configure essas variáveis antes de publicar na internet.")
    print("=" * 70)

JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/jobs_queimada")
os.makedirs(JOBS_DIR, exist_ok=True)

# Estado em memória (MVP). Para produção real com múltiplos workers,
# trocar por Redis/DB (ex.: Redis + RQ/Celery).
JOBS = {}


class ProcessarRequest(BaseModel):
    data_referencia: str = Field(..., description="Formato AAAA-MM-DD")
    latitude: float
    longitude: float
    janela_dias: int = 30
    nuvem_maxima: int = 30
    limiar_dnbr: float = 0.1
    raio_km: float = Field(3.0, description="Raio (km) da área ao redor do ponto")
    filtro_pixels: int = Field(
        3, description="Filtro por pixel: número mínimo de pixels "
                        "(~10m cada, ~100 m² por pixel) conectados para "
                        "considerar uma mancha como área queimada real, "
                        "descartando ruído/fragmentos menores."
    )
    preencher_falhas_pixels: int = Field(
        5, description="Preenchimento de falhas: tamanho máximo (em pixels) "
                        "de um buraco interno ao polígono que será "
                        "preenchido. Buracos maiores são preservados como "
                        "área não queimada."
    )
    satelite: str = Field(
        "sentinel2", description="'sentinel2' (10m, exige credenciais) ou "
                                  "'landsat' (30m, sem credenciais)."
    )
    sentinel_hub_client_id: Optional[str] = Field(
        None, description="Obrigatório se satelite='sentinel2'. Client ID "
                           "do OAuth Client do Sentinel Hub do próprio "
                           "usuário (não fica armazenado no servidor)."
    )
    sentinel_hub_client_secret: Optional[str] = Field(
        None, description="Obrigatório se satelite='sentinel2'."
    )


def _montar_nome_pacote(req: ProcessarRequest):
    """
    Monta um nome de pasta/arquivo identificável pelo usuário, a partir
    de coordenada e data. Formato:
      AreaQueimada_-19.9167_-43.9345_2024-08-15
    Coordenadas em graus decimais (mesmo formato aceito por QGIS, Google
    Maps etc.), permitindo copiar e colar direto do nome do arquivo.
    """
    # 4 casas decimais ~ 11 m de precisão, suficiente para identificar
    lat_txt = f"{req.latitude:.4f}"
    lon_txt = f"{req.longitude:.4f}"

    return f"AreaQueimada_{lat_txt}_{lon_txt}_{req.data_referencia}"


def _montar_texto_informacoes(req: ProcessarRequest, resultado: dict):
    """Gera o conteúdo do INFORMACOES.txt incluído no pacote, com os
    parâmetros usados e as ressalvas técnicas."""
    nome_satelite = ("Landsat 8/9" if resultado["satelite_usado"] == "landsat"
                     else "Sentinel-2")
    return f"""MAPEAMENTO DE CICATRIZ DE INCENDIO
Corpo de Bombeiros Militar de Minas Gerais
================================================================

RESULTADO
  Area queimada.............: {resultado['area_ha']} hectares
  Sistema de coordenadas....: EPSG:{resultado['epsg_saida']} (SIRGAS2000)
  CRS usado no calculo......: EPSG:{resultado['epsg_utm_usado']} (SIRGAS2000/UTM)

CONSULTA
  Data de referencia........: {req.data_referencia}
  Latitude..................: {req.latitude}
  Longitude.................: {req.longitude}
  Raio da area..............: {req.raio_km} km

IMAGENS UTILIZADAS
  Satelite..................: {nome_satelite} ({resultado['resolucao_m']} m/pixel)
  Cena anterior.............: {resultado['cena_antes']['data'][:10]}
  Cena posterior............: {resultado['cena_depois']['data'][:10]}

PARAMETROS DE PROCESSAMENTO
  Metodo....................: dNBR (differenced Normalized Burn Ratio)
  Limiar dNBR...............: {req.limiar_dnbr}
  Janela de busca...........: {req.janela_dias} dias
  Nuvem maxima..............: {req.nuvem_maxima}%
  Filtro por pixel..........: {req.filtro_pixels} pixels
  Preenchimento de falhas...: {req.preencher_falhas_pixels} pixels

CONTEUDO DO PACOTE
  /poligono ....... Shapefile (.shp/.shx/.dbf/.prj) e GeoJSON da area
                    queimada, em SIRGAS2000 (EPSG:4674).
  /bandas/antes ... Bandas B04 (vermelho), B08 (infravermelho proximo)
                    e B12 (infravermelho de ondas curtas) da cena
                    anterior ao evento, em GeoTIFF georreferenciado.
  /bandas/depois .. As mesmas bandas da cena posterior ao evento.

RESSALVAS TECNICAS
  Este e um levantamento automatizado e auxiliar, sujeito a imprecisoes.
  Antes de utilizar em relatorio, pericia ou documento oficial:
    - Confira a delimitacao em software de geoprocessamento (QGIS,
      ArcGIS), sobrepondo o poligono as bandas fornecidas;
    - Valide o resultado com vistoria em campo.

  Limitacoes conhecidas: areas queimadas ha muito tempo perdem a
  assinatura espectral com a rebrota da vegetacao; cicatrizes muito
  pequenas podem nao ser detectaveis; cobertura de nuvens pode afetar
  a disponibilidade e a qualidade das imagens.

Imagens: Copernicus/Sentinel-2 (ESA) e Landsat (USGS/NASA)
Desenvolvimento: 3o SGT BM Marcos A. de Souza Junior - 148.070-6
"""


def _validar_request(req: ProcessarRequest):
    try:
        datetime.strptime(req.data_referencia, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "data_referencia deve estar no formato AAAA-MM-DD")
    if not (-90 <= req.latitude <= 90):
        raise HTTPException(400, "latitude inválida")
    if not (-180 <= req.longitude <= 180):
        raise HTTPException(400, "longitude inválida")
    if req.satelite not in ("sentinel2", "landsat"):
        raise HTTPException(400, "satelite deve ser 'sentinel2' ou 'landsat'")
    if req.satelite == "sentinel2" and (
        not req.sentinel_hub_client_id or not req.sentinel_hub_client_secret
    ):
        raise HTTPException(
            400, "sentinel_hub_client_id/sentinel_hub_client_secret são "
                 "obrigatórios quando satelite='sentinel2'. Para usar sem "
                 "credenciais, selecione satelite='landsat'."
        )


def _rodar_job(job_id: str, req: ProcessarRequest):
    workdir = os.path.join(JOBS_DIR, job_id)
    JOBS[job_id]["status"] = "processando"
    try:
        resultado = pipeline.executar_pipeline(
            data_referencia=req.data_referencia,
            latitude=req.latitude,
            longitude=req.longitude,
            janela_dias=req.janela_dias,
            nuvem_maxima=req.nuvem_maxima,
            limiar_dnbr=req.limiar_dnbr,
            workdir=workdir,
            client_id=req.sentinel_hub_client_id,
            client_secret=req.sentinel_hub_client_secret,
            raio_km=req.raio_km,
            filtro_pixels=req.filtro_pixels,
            preencher_falhas_pixels=req.preencher_falhas_pixels,
            satelite=req.satelite,
        )
        # Pacote único: um zip contendo o shapefile e as bandas em pastas
        # separadas, nomeado por coordenada e data para fácil identificação.
        nome_pacote = _montar_nome_pacote(req)
        zip_path = os.path.join(workdir, f"{nome_pacote}.zip")

        base_shp = resultado["shapefile_path"].replace(".shp", "")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Pasta 1: poligono (shapefile exige múltiplos arquivos juntos)
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                caminho = base_shp + ext
                if os.path.exists(caminho):
                    zf.write(caminho,
                             arcname=f"{nome_pacote}/poligono/{os.path.basename(caminho)}")

            # GeoJSON junto do shapefile (mesma pasta, formato alternativo)
            if os.path.exists(resultado["geojson_path"]):
                zf.write(resultado["geojson_path"],
                         arcname=f"{nome_pacote}/poligono/{os.path.basename(resultado['geojson_path'])}")

            # Pasta 2: bandas em GeoTIFF, separadas por data
            for nome, caminho in resultado.get("bandas_paths", {}).items():
                if not os.path.exists(caminho):
                    continue
                subpasta = "depois" if nome.endswith("_depois") else "antes"
                zf.write(caminho,
                         arcname=f"{nome_pacote}/bandas/{subpasta}/{os.path.basename(caminho)}")

            # Arquivo de informações do processamento
            zf.writestr(f"{nome_pacote}/INFORMACOES.txt",
                        _montar_texto_informacoes(req, resultado))

        JOBS[job_id]["status"] = "concluido"
        JOBS[job_id]["resultado"] = {
            "area_ha": resultado["area_ha"],
            "epsg_saida": resultado["epsg_saida"],
            "epsg_utm_usado": resultado["epsg_utm_usado"],
            "cena_antes": resultado["cena_antes"],
            "cena_depois": resultado["cena_depois"],
            "satelite_usado": resultado["satelite_usado"],
            "resolucao_m": resultado["resolucao_m"],
        }
        JOBS[job_id]["pacote_zip"] = zip_path
        JOBS[job_id]["nome_pacote"] = nome_pacote
        JOBS[job_id]["geojson_path"] = resultado["geojson_path"]
    except Exception as e:
        JOBS[job_id]["status"] = "erro"
        JOBS[job_id]["erro"] = str(e)
        JOBS[job_id]["traceback"] = traceback.format_exc()


@app.post("/processar")
def processar(req: ProcessarRequest, background_tasks: BackgroundTasks):
    _validar_request(req)
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "pendente", "criado_em": datetime.utcnow().isoformat()}
    background_tasks.add_task(_rodar_job, job_id, req)
    return {"job_id": job_id, "status": "pendente"}


@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job_id não encontrado")
    resposta = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "concluido":
        resposta["resultado"] = job["resultado"]
    if job["status"] == "erro":
        resposta["erro"] = job.get("erro")
    return resposta


@app.get("/geojson/{job_id}")
def geojson(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "concluido":
        raise HTTPException(404, "resultado não disponível")
    return FileResponse(job["geojson_path"], media_type="application/geo+json")


@app.get("/download/{job_id}")
def download(job_id: str):
    """Baixa o pacote completo: polígono (shapefile + geojson), bandas
    B04/B08/B12 em GeoTIFF (antes e depois) e arquivo de informações,
    organizados em pastas e nomeados por coordenada e data."""
    job = JOBS.get(job_id)
    if not job or job.get("status") != "concluido":
        raise HTTPException(404, "resultado não disponível")
    caminho = job.get("pacote_zip")
    if not caminho or not os.path.exists(caminho):
        raise HTTPException(404, "pacote não disponível")
    return FileResponse(
        caminho,
        media_type="application/zip",
        filename=f"{job.get('nome_pacote', 'area_queimada')}.zip",
    )


@app.delete("/job/{job_id}")
def limpar_job(job_id: str):
    workdir = os.path.join(JOBS_DIR, job_id)
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    JOBS.pop(job_id, None)
    return {"ok": True}


# Serve o frontend estático (index.html com o mapa)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
