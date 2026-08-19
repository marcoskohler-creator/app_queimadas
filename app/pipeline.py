"""
Núcleo do pipeline de detecção de área queimada via satélite (dNBR).

Suporta dois satélites, com o mesmo cálculo de dNBR/NDVI ao final:

- Sentinel-2 (10m/pixel): via Sentinel Hub (Catalog API + Process API),
  exige credenciais do usuário (Client ID/Secret do Sentinel Hub).
- Landsat 8/9 (30m/pixel): via Microsoft Planetary Computer (Catalog
  STAC + assinatura de URL), API pública, SEM necessidade de credencial.
"""
import os
import math
import datetime as dt

import numpy as np
import requests
import rasterio
from rasterio.io import MemoryFile
from rasterio.features import shapes as rio_shapes
from rasterio.windows import from_bounds as janela_de_bounds, transform as transform_da_janela
import geopandas as gpd
from shapely.geometry import shape, Polygon
from pyproj import Transformer

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
SH_CATALOG_URL = "https://sh.dataspace.copernicus.eu/catalog/v1/search"
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"  # não usado (bucket requester-pays)
PC_STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

class PipelineError(Exception):
    pass


EVALSCRIPT_BANDAS = """
//VERSION=3
// Retorna as bandas em reflectancia de superficie (valores analiticos,
// nao visualizacao), como FLOAT32 - equivalente ao download "Analytical"
// do Copernicus Browser em TIFF 32-bit float.
function setup() {
  return {
    input: ["B04", "B08", "B12", "dataMask"],
    output: { bands: 4, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(sample) {
  return [sample.B04, sample.B08, sample.B12, sample.dataMask];
}
"""

# Parâmetros fixos de qualidade do polígono (não ajustáveis via API/UI).
# Calibrados para reduzir falso-positivo em área urbana/pixel misto de
# borda. Alterar aqui exige redeploy, é intencional não expor no formulário.
LIMIAR_NDVI_VEGETACAO_FIXO = 0.35
RESOLUCAO_M_SENTINEL2 = 10.0
RESOLUCAO_M_LANDSAT = 30.0

# Escala/offset oficiais USGS para converter DN -> refletância de
# superfície nos produtos Landsat Collection 2 Level-2 (bandas óticas).
LANDSAT_SR_ESCALA = 0.0000275
LANDSAT_SR_OFFSET = -0.2

# Bits de QA_PIXEL (Landsat C2 L2) considerados inválidos para a análise:
# fill(0) + dilated cloud(1) + cloud(3) + cloud shadow(4).
LANDSAT_QA_MASCARA_INVALIDA = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 4)


def obter_token(client_id, client_secret):
    if not client_id or not client_secret:
        raise PipelineError(
            "Client ID / Client Secret do Sentinel Hub não informados."
        )
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if resp.status_code == 401:
        raise PipelineError(
            "401 Unauthorized ao gerar token. Confira se o Client ID e o "
            "Client Secret informados são os do OAuth Client criado no "
            "dashboard do Sentinel Hub/CDSE."
        )
    resp.raise_for_status()
    return resp.json()["access_token"]


def epsg_sirgas2000_utm(lon, lat):
    """Retorna o EPSG SIRGAS2000/UTM correto para o ponto (lon, lat)."""
    zona = int(math.floor((lon + 180) / 6) + 1)
    if lat >= 0:
        return 31960 + zona - 6
    return 31960 + zona


def epsg_wgs84_utm(lon, lat):
    """EPSG WGS84/UTM (usado só para calcular buffer em metros com precisão)."""
    zona = int(math.floor((lon + 180) / 6) + 1)
    return (32600 + zona) if lat >= 0 else (32700 + zona)


def calcular_bbox_wgs84(lon, lat, raio_km):
    """Gera um bbox (WGS84) quadrado de raio_km ao redor do ponto (lon, lat),
    calculado com precisão via projeção UTM local."""
    epsg_utm = epsg_wgs84_utm(lon, lat)
    para_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_utm}", always_xy=True)
    para_wgs84 = Transformer.from_crs(f"EPSG:{epsg_utm}", "EPSG:4326", always_xy=True)

    x, y = para_utm.transform(lon, lat)
    raio_m = raio_km * 1000.0
    x_min, y_min = x - raio_m, y - raio_m
    x_max, y_max = x + raio_m, y + raio_m

    lon_min, lat_min = para_wgs84.transform(x_min, y_min)
    lon_max, lat_max = para_wgs84.transform(x_max, y_max)
    return [lon_min, lat_min, lon_max, lat_max]


def buscar_cena_sentinel2(bbox_wgs84, data_ref, direcao, token,
                           janela_dias=30, nuvem_max=30):
    """
    Busca no Catalog API (STAC) do Sentinel Hub a cena Sentinel-2 L2A mais
    próxima da data_ref, ANTES ou DEPOIS, cobrindo o bbox informado.
    """
    data_ref_dt = dt.datetime.strptime(data_ref, "%Y-%m-%d")

    if direcao == "before":
        data_ini = (data_ref_dt - dt.timedelta(days=janela_dias)).strftime("%Y-%m-%dT00:00:00Z")
        data_fim = data_ref_dt.strftime("%Y-%m-%dT23:59:59Z")
    else:
        data_ini = data_ref_dt.strftime("%Y-%m-%dT00:00:00Z")
        data_fim = (data_ref_dt + dt.timedelta(days=janela_dias)).strftime("%Y-%m-%dT23:59:59Z")

    corpo = {
        "collections": ["sentinel-2-l2a"],
        "bbox": bbox_wgs84,
        "datetime": f"{data_ini}/{data_fim}",
        "limit": 30,
        "filter": f"eo:cloud_cover < {nuvem_max}",
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(SH_CATALOG_URL, json=corpo, headers=headers)
    if r.status_code != 200:
        raise PipelineError(
            f"Erro no Catalog API ({r.status_code}) ao buscar cena "
            f"'{direcao}': {r.text[:500]}"
        )
    features = r.json().get("features", [])
    if not features:
        return None

    # Ordena no cliente: para "before" queremos a data mais próxima (mais
    # recente) do limite superior da janela; para "after", a mais próxima
    # (mais antiga) do limite inferior.
    features.sort(key=lambda f: f["properties"]["datetime"],
                   reverse=(direcao == "before"))
    feat = features[0]
    return {
        "id": feat["id"],
        "data": feat["properties"]["datetime"],
        "nuvem": feat["properties"].get("eo:cloud_cover"),
    }


def _resolucao_para_dimensoes(bbox_wgs84, resolucao_m=10, max_px=2500):
    """
    Calcula largura/altura em pixels a partir do bbox e da resolução
    nativa do satélite.

    O teto de max_px existe porque o Process API do Sentinel Hub tem
    limite de dimensão por requisição. Quando a área pedida é grande
    demais para caber na resolução nativa, a imagem é reamostrada — ou
    seja, a resolução efetiva piora. A função devolve também a resolução
    efetiva resultante, para que isso possa ser informado ao usuário em
    vez de degradar silenciosamente.

    Retorna (largura, altura, resolucao_efetiva_m).
    """
    epsg_utm = epsg_wgs84_utm((bbox_wgs84[0] + bbox_wgs84[2]) / 2,
                               (bbox_wgs84[1] + bbox_wgs84[3]) / 2)
    para_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_utm}", always_xy=True)
    x_min, y_min = para_utm.transform(bbox_wgs84[0], bbox_wgs84[1])
    x_max, y_max = para_utm.transform(bbox_wgs84[2], bbox_wgs84[3])

    largura_m = x_max - x_min
    altura_m = y_max - y_min

    largura_nativa = round(largura_m / resolucao_m)
    altura_nativa = round(altura_m / resolucao_m)

    largura = int(min(max_px, max(64, largura_nativa)))
    altura = int(min(max_px, max(64, altura_nativa)))

    # Se foi necessário limitar, a resolução efetiva piora proporcionalmente
    resolucao_efetiva = max(largura_m / largura, altura_m / altura)

    return largura, altura, resolucao_efetiva


def obter_bandas(bbox_wgs84, data_iso, token, largura_px, altura_px):
    """
    Chama o Process API do Sentinel Hub e retorna um array numpy (4, H, W):
    banda 0 = B04, banda 1 = B08, banda 2 = B12,
    banda 3 = dataMask (1=válido, 0=sem dado),
    junto com o perfil rasterio (georreferenciamento).
    """
    data_dt = dt.datetime.strptime(data_iso[:10], "%Y-%m-%d")
    data_ini = data_dt.strftime("%Y-%m-%dT00:00:00Z")
    data_fim = data_dt.strftime("%Y-%m-%dT23:59:59Z")

    corpo = {
        "input": {
            "bounds": {
                "bbox": bbox_wgs84,
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {"timeRange": {"from": data_ini, "to": data_fim}},
                # Reamostragem por vizinho mais próximo: preserva o valor
                # medido de cada pixel, sem interpolação/suavização. É o
                # esperado para uso analítico (equivalente ao download
                # "Analytical" do Copernicus Browser).
                "processing": {
                    "upsampling": "NEAREST",
                    "downsampling": "NEAREST",
                },
            }],
        },
        "output": {
            "width": largura_px,
            "height": altura_px,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": EVALSCRIPT_BANDAS,
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(SH_PROCESS_URL, json=corpo, headers=headers)
    if r.status_code != 200:
        raise PipelineError(f"Erro no Process API ({r.status_code}): {r.text[:500]}")

    with MemoryFile(r.content) as memfile:
        with memfile.open() as src:
            dados = src.read().astype("float32")  # (4, H, W)
            perfil = src.profile

    return dados, perfil


# =============================================================================
# LANDSAT 8/9 — via Earth Search (Element84/AWS), API STAC pública, sem
# necessidade de credencial. Cenas Collection 2 Level-2 (reflectância de
# superfície já corrigida), Cloud-Optimized GeoTIFF lidos via HTTP direto
# (GDAL faz leitura parcial/"windowed" sem baixar o arquivo inteiro).
# =============================================================================

# =============================================================================
# LANDSAT 8/9 — via Microsoft Planetary Computer, API STAC pública, sem
# necessidade de conta/credencial. Cenas Collection 2 Level-2 (reflectância
# de superfície já corrigida). Os arquivos ficam em Azure Blob Storage e
# exigem uma URL "assinada" (token temporário obtido num endpoint público,
# sem autenticação) antes de serem lidos — diferente do AWS Earth Search,
# cujo bucket Landsat é "requester pays" (exigiria credencial AWS própria,
# por isso não é usado aqui).
# =============================================================================

def _assinar_url_planetary_computer(href):
    """Troca uma URL de asset por uma URL assinada (com token temporário),
    exigido pelo Planetary Computer para ler arquivos no Azure Blob
    Storage. O endpoint de assinatura em si é público, sem necessidade de
    conta ou API key."""
    r = requests.get(PC_SIGN_URL, params={"href": href})
    if r.status_code != 200:
        raise PipelineError(
            f"Erro ao assinar URL no Planetary Computer ({r.status_code}): "
            f"{r.text[:300]}"
        )
    return r.json()["href"]


def buscar_cena_landsat(bbox_wgs84, data_ref, direcao, janela_dias=30, nuvem_max=30):
    """
    Busca no Planetary Computer (STAC) a cena Landsat Collection 2 Level-2
    mais próxima da data_ref, ANTES ou DEPOIS, cobrindo o bbox informado.
    Não exige autenticação (API pública).
    """
    data_ref_dt = dt.datetime.strptime(data_ref, "%Y-%m-%d")

    if direcao == "before":
        data_ini = (data_ref_dt - dt.timedelta(days=janela_dias)).strftime("%Y-%m-%dT00:00:00Z")
        data_fim = data_ref_dt.strftime("%Y-%m-%dT23:59:59Z")
    else:
        data_ini = data_ref_dt.strftime("%Y-%m-%dT00:00:00Z")
        data_fim = (data_ref_dt + dt.timedelta(days=janela_dias)).strftime("%Y-%m-%dT23:59:59Z")

    corpo = {
        "collections": ["landsat-c2-l2"],
        "bbox": bbox_wgs84,
        "datetime": f"{data_ini}/{data_fim}",
        "limit": 30,
        "query": {"eo:cloud_cover": {"lt": nuvem_max}},
    }
    r = requests.post(PC_STAC_SEARCH_URL, json=corpo)
    if r.status_code != 200:
        raise PipelineError(
            f"Erro no Planetary Computer ({r.status_code}) ao buscar cena "
            f"Landsat '{direcao}': {r.text[:500]}"
        )
    features = r.json().get("features", [])
    if not features:
        return None

    features.sort(key=lambda f: f["properties"]["datetime"],
                   reverse=(direcao == "before"))
    feat = features[0]
    return {
        "id": feat["id"],
        "data": feat["properties"]["datetime"],
        "nuvem": feat["properties"].get("eo:cloud_cover"),
        "assets": feat["assets"],  # dict com URLs das bandas (Azure Blob Storage)
    }


def _ler_janela_remota(url, bbox_wgs84):
    """
    Abre um Cloud-Optimized GeoTIFF remoto (HTTP) e lê só a janela
    correspondente ao bbox informado, sem baixar a cena inteira. Retorna
    o array 2D recortado e o perfil rasterio (transform/crs) da janela.
    """
    with rasterio.open(url) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x_min, y_min = transformer.transform(bbox_wgs84[0], bbox_wgs84[1])
        x_max, y_max = transformer.transform(bbox_wgs84[2], bbox_wgs84[3])

        janela = janela_de_bounds(x_min, y_min, x_max, y_max, transform=src.transform)
        dados = src.read(1, window=janela, boundless=True, fill_value=0)
        transform_janela = transform_da_janela(janela, src.transform)

        perfil = {
            "transform": transform_janela,
            "crs": src.crs,
            "height": dados.shape[0],
            "width": dados.shape[1],
        }
    return dados.astype("float32"), perfil


def obter_bandas_landsat(assets, bbox_wgs84):
    """
    Lê as bandas equivalentes (vermelho, NIR, SWIR2) + QA_PIXEL de uma
    cena Landsat (Planetary Computer), recortadas na área de interesse, e
    devolve no MESMO formato usado pelo Sentinel-2: array (4, H, W) =
    [B04, B08, B12, dataMask] + perfil. Isso permite reaproveitar sem
    alteração o resto do pipeline (cálculo de NBR/NDVI e vetorização).

    Conversão DN -> refletância de superfície usa a escala/offset oficial
    USGS para produtos Collection 2 Level-2 (necessário para o cálculo de
    NBR/NDVI ficar correto — a razão entre bandas NÃO é invariante à
    escala quando há offset aditivo).
    """
    try:
        url_vermelho = assets["red"]["href"]
        url_nir = assets["nir08"]["href"]
        url_swir2 = assets["swir22"]["href"]
        url_qa = assets["qa_pixel"]["href"]
    except KeyError as e:
        raise PipelineError(f"Asset de banda Landsat não encontrado: {e}")

    # Assina cada URL (token temporário, endpoint público) antes de ler -
    # exigido pelo Planetary Computer para acessar o Azure Blob Storage.
    url_vermelho = _assinar_url_planetary_computer(url_vermelho)
    url_nir = _assinar_url_planetary_computer(url_nir)
    url_swir2 = _assinar_url_planetary_computer(url_swir2)
    url_qa = _assinar_url_planetary_computer(url_qa)

    dn_vermelho, perfil = _ler_janela_remota(url_vermelho, bbox_wgs84)
    dn_nir, _ = _ler_janela_remota(url_nir, bbox_wgs84)
    dn_swir2, _ = _ler_janela_remota(url_swir2, bbox_wgs84)
    qa, _ = _ler_janela_remota(url_qa, bbox_wgs84)

    def para_refletancia(dn):
        refl = dn * LANDSAT_SR_ESCALA + LANDSAT_SR_OFFSET
        refl[dn == 0] = np.nan  # 0 = sem dado nos produtos Collection 2
        return refl

    b04 = para_refletancia(dn_vermelho)
    b08 = para_refletancia(dn_nir)
    b12 = para_refletancia(dn_swir2)

    qa_int = qa.astype("uint32")
    data_mask = np.where((qa_int & LANDSAT_QA_MASCARA_INVALIDA) == 0, 1.0, 0.0).astype("float32")
    data_mask[np.isnan(b04) | np.isnan(b08) | np.isnan(b12)] = 0.0

    dados = np.stack([b04, b08, b12, data_mask])  # (4, H, W), mesmo formato do Sentinel-2
    return dados, perfil


def calcular_ndvi(b08, b04):
    b08 = b08.astype("float32")
    b04 = b04.astype("float32")
    denom = b08 + b04
    denom[denom == 0] = np.nan
    return (b08 - b04) / denom


def calcular_nbr(b08, b12):
    b08 = b08.astype("float32")
    b12 = b12.astype("float32")
    denom = b08 + b12
    denom[denom == 0] = np.nan
    return (b08 - b12) / denom


def preencher_falhas_internas(geometria, area_maxima_falha_m2):
    """
    Preenche buracos (interior rings) pequenos dentro de um polígono.

    Buracos pequenos costumam ser ruído: pixels isolados dentro da mancha
    queimada que ficaram logo abaixo do limiar de dNBR/NDVI. Buracos
    grandes são preservados, porque tendem a representar áreas realmente
    não atingidas (clareira, açude, afloramento rochoso, ilha de mata que
    não queimou) — preenchê-los inflaria a área medida indevidamente.
    """
    if geometria.geom_type != "Polygon":
        return geometria
    if not geometria.interiors:
        return geometria

    # Mantém apenas os buracos GRANDES (áreas genuinamente não queimadas).
    # Os pequenos são descartados, o que na prática os preenche.
    buracos_preservados = [
        anel for anel in geometria.interiors
        if Polygon(anel).area > area_maxima_falha_m2
    ]
    return Polygon(geometria.exterior, buracos_preservados)


def gerar_poligono_area_queimada(dados_antes, dados_depois, perfil, lon, lat,
                                  limiar_dnbr=0.1, area_minima_m2=500.0,
                                  area_maxima_falha_m2=500.0):
    b04_antes, b08_antes, b12_antes, mask_antes = dados_antes
    b04_depois, b08_depois, b12_depois, mask_depois = dados_depois

    nbr_antes = calcular_nbr(b08_antes, b12_antes)
    nbr_depois = calcular_nbr(b08_depois, b12_depois)
    dnbr = nbr_antes - nbr_depois

    # Só considera "queimado" um pixel que já era vegetação ANTES do
    # evento (NDVI pré-fogo acima do limiar fixo). Filtra áreas urbanas,
    # solo exposto e estradas, que têm NDVI baixo mesmo sem incêndio.
    ndvi_antes = calcular_ndvi(b08_antes, b04_antes)
    era_vegetacao = ndvi_antes >= LIMIAR_NDVI_VEGETACAO_FIXO

    valido = (mask_antes > 0) & (mask_depois > 0)
    mascara = ((dnbr >= limiar_dnbr) & valido & era_vegetacao).astype("uint8")

    geoms = []
    for geom, valor in rio_shapes(mascara, mask=mascara == 1, transform=perfil["transform"]):
        geoms.append(shape(geom))

    if not geoms:
        raise PipelineError(
            "Nenhuma área queimada detectada com o limiar atual. "
            "Tente reduzir 'limiar_dnbr' ou aumentar o raio da área."
        )

    gdf = gpd.GeoDataFrame({"id": range(len(geoms))}, geometry=geoms, crs=perfil["crs"])
    gdf_sirgas = gdf.to_crs(epsg=4674)

    epsg_utm = epsg_sirgas2000_utm(lon, lat)
    gdf_utm = gdf_sirgas.to_crs(epsg=epsg_utm)

    # Separa multipolígonos em partes individuais para poder filtrar por
    # área mínima peça a peça (evita que um fragmento minúsculo de ruído
    # "carona" num polígono grande escape do filtro).
    gdf_utm = gdf_utm.explode(index_parts=False).reset_index(drop=True)

    # Preenche falhas internas pequenas (ruído dentro da mancha queimada),
    # preservando buracos grandes (áreas realmente não atingidas).
    if area_maxima_falha_m2 and area_maxima_falha_m2 > 0:
        gdf_utm["geometry"] = gdf_utm.geometry.apply(
            lambda g: preencher_falhas_internas(g, area_maxima_falha_m2)
        )

    if area_minima_m2 and area_minima_m2 > 0:
        gdf_utm = gdf_utm[gdf_utm.geometry.area >= area_minima_m2].reset_index(drop=True)

    if len(gdf_utm) == 0:
        raise PipelineError(
            "Todas as áreas detectadas ficaram abaixo do tamanho mínimo "
            f"({area_minima_m2:.0f} m² / equivalente ao 'filtro_pixels' "
            "definido). Tente reduzir 'filtro_pixels' "
            "ou revisar o limiar de dNBR."
        )

    gdf_sirgas = gdf_utm.to_crs(epsg=4674)
    area_ha_total = float(gdf_utm.geometry.area.sum() / 10000.0)

    return gdf_sirgas, area_ha_total, epsg_utm


def salvar_bandas_geotiff(dados, perfil, workdir, sufixo):
    """
    Salva as bandas (B04/vermelho, B08/NIR, B12/SWIR2) como arquivos
    GeoTIFF georreferenciados, prontos para abrir em QGIS/ArcGIS e
    permitir conferência independente do resultado.

    dados: tupla (b04, b08, b12, dataMask)
    sufixo: "antes" ou "depois"
    Retorna dict {nome_banda: caminho}.
    """
    b04, b08, b12, _mask = dados
    bandas = {"B04_vermelho": b04, "B08_nir": b08, "B12_swir2": b12}
    caminhos = {}

    for nome, matriz in bandas.items():
        caminho = os.path.join(workdir, f"{nome}_{sufixo}.tif")
        perfil_saida = {
            "driver": "GTiff",
            "height": matriz.shape[0],
            "width": matriz.shape[1],
            "count": 1,
            "dtype": "float32",
            "crs": perfil["crs"],
            "transform": perfil["transform"],
            "nodata": np.nan,
            "compress": "deflate",
        }
        with rasterio.open(caminho, "w", **perfil_saida) as dst:
            dst.write(matriz.astype("float32"), 1)
        caminhos[f"{nome}_{sufixo}"] = caminho

    return caminhos


def executar_pipeline(data_referencia, latitude, longitude,
                       janela_dias, nuvem_maxima, limiar_dnbr, workdir,
                       client_id=None, client_secret=None,
                       raio_km=3.0, filtro_pixels=3, preencher_falhas_pixels=5,
                       satelite="sentinel2"):
    """Executa o pipeline completo e retorna um dicionário com o resultado.

    satelite: "sentinel2" (10m, exige client_id/client_secret do Sentinel
    Hub, informados pelo usuário no formulário - não ficam armazenados no
    servidor) ou "landsat" (30m, via Earth Search/AWS, API pública, SEM
    necessidade de credencial).

    NDVI de vegetação é fixo (ver constante no topo do arquivo), não
    ajustável via parâmetro. filtro_pixels define a área mínima de uma
    mancha; preencher_falhas_pixels define o tamanho máximo de um buraco
    interno que será preenchido (buracos maiores são preservados como
    área não queimada). Ambos em número de pixels — a área por pixel
    depende do satélite escolhido (10m ou 30m)."""
    os.makedirs(workdir, exist_ok=True)

    if satelite not in ("sentinel2", "landsat"):
        raise PipelineError(f"Satélite desconhecido: '{satelite}' (use 'sentinel2' ou 'landsat').")

    resolucao_m = RESOLUCAO_M_SENTINEL2 if satelite == "sentinel2" else RESOLUCAO_M_LANDSAT
    area_por_pixel = resolucao_m ** 2
    area_minima_m2 = max(0, filtro_pixels) * area_por_pixel
    area_maxima_falha_m2 = max(0, preencher_falhas_pixels) * area_por_pixel

    bbox_wgs84 = calcular_bbox_wgs84(longitude, latitude, raio_km)

    if satelite == "sentinel2":
        token = obter_token(client_id, client_secret)

        cena_antes = buscar_cena_sentinel2(bbox_wgs84, data_referencia, "before",
                                            token, janela_dias, nuvem_maxima)
        cena_depois = buscar_cena_sentinel2(bbox_wgs84, data_referencia, "after",
                                             token, janela_dias, nuvem_maxima)

        if not cena_antes or not cena_depois:
            raise PipelineError(
                "Não foi possível encontrar cenas Sentinel-2 antes/depois "
                "dentro da janela de dias definida. Aumente 'janela_dias', "
                "'nuvem_maxima', ou tente o satélite Landsat."
            )

        largura_px, altura_px, resolucao_efetiva = _resolucao_para_dimensoes(
            bbox_wgs84, resolucao_m=resolucao_m)

        dados_antes_raw, perfil = obter_bandas(
            bbox_wgs84, cena_antes["data"], token, largura_px, altura_px)
        dados_depois_raw, _ = obter_bandas(
            bbox_wgs84, cena_depois["data"], token, largura_px, altura_px)

    else:  # landsat
        cena_antes = buscar_cena_landsat(bbox_wgs84, data_referencia, "before",
                                          janela_dias, nuvem_maxima)
        cena_depois = buscar_cena_landsat(bbox_wgs84, data_referencia, "after",
                                           janela_dias, nuvem_maxima)

        if not cena_antes or not cena_depois:
            raise PipelineError(
                "Não foi possível encontrar cenas Landsat antes/depois "
                "dentro da janela de dias definida. Aumente 'janela_dias', "
                "'nuvem_maxima', ou tente o satélite Sentinel-2."
            )

        dados_antes_raw, perfil = obter_bandas_landsat(cena_antes["assets"], bbox_wgs84)
        # Landsat é lido direto do COG na resolução nativa (sem limite de
        # dimensão por requisição como no Process API do Sentinel Hub).
        resolucao_efetiva = resolucao_m
        dados_depois_raw, _ = obter_bandas_landsat(cena_depois["assets"], bbox_wgs84)

    dados_antes = tuple(dados_antes_raw)   # (B04, B08, B12, dataMask)
    dados_depois = tuple(dados_depois_raw)

    gdf_resultado, area_ha, epsg_utm = gerar_poligono_area_queimada(
        dados_antes, dados_depois, perfil, longitude, latitude,
        limiar_dnbr=limiar_dnbr, area_minima_m2=area_minima_m2,
        area_maxima_falha_m2=area_maxima_falha_m2,
    )

    caminho_shp = os.path.join(workdir, "area_queimada_sirgas2000.shp")
    gdf_resultado.to_file(caminho_shp, encoding="utf-8")

    caminho_geojson = os.path.join(workdir, "area_queimada.geojson")
    gdf_resultado.to_file(caminho_geojson, driver="GeoJSON")

    # Salva as bandas usadas no cálculo como GeoTIFF, para permitir
    # conferência independente do resultado em software de GIS.
    caminhos_bandas = {}
    caminhos_bandas.update(salvar_bandas_geotiff(dados_antes, perfil, workdir, "antes"))
    caminhos_bandas.update(salvar_bandas_geotiff(dados_depois, perfil, workdir, "depois"))

    # Remove URLs de asset do dict de retorno da cena (poluem o JSON de resposta)
    cena_antes_out = {k: v for k, v in cena_antes.items() if k != "assets"}
    cena_depois_out = {k: v for k, v in cena_depois.items() if k != "assets"}

    return {
        "satelite_usado": satelite,
        "resolucao_m": resolucao_m,
        "resolucao_efetiva_m": round(resolucao_efetiva, 2),
        "cena_antes": cena_antes_out,
        "cena_depois": cena_depois_out,
        "area_ha": round(area_ha, 4),
        "epsg_utm_usado": epsg_utm,
        "epsg_saida": 4674,
        "bbox_consultado": bbox_wgs84,
        "shapefile_path": caminho_shp,
        "geojson_path": caminho_geojson,
        "bandas_paths": caminhos_bandas,
    }
