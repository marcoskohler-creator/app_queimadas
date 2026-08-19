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
        # zip do shapefile (shp exige múltiplos arquivos: .shp .shx .dbf .prj)
        base_shp = resultado["shapefile_path"].replace(".shp", "")
        zip_path = os.path.join(workdir, "shapefile_area_queimada.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                caminho = base_shp + ext
                if os.path.exists(caminho):
                    zf.write(caminho, arcname=os.path.basename(caminho))

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
        JOBS[job_id]["shapefile_zip"] = zip_path
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
    job = JOBS.get(job_id)
    if not job or job.get("status") != "concluido":
        raise HTTPException(404, "resultado não disponível")
    return FileResponse(
        job["shapefile_zip"],
        media_type="application/zip",
        filename="shapefile_area_queimada.zip",
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
