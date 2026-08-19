"""
Núcleo do pipeline de detecção de área queimada via Sentinel-2 (dNBR).

Versão 2: usa a API do Sentinel Hub (Catalog API para buscar cenas +
Process API para obter B08/B12 já recortadas na área de interesse como
GeoTIFF), autenticando com client_credentials (OAuth Client do Sentinel
Hub Dashboard). Evita baixar produtos SAFE inteiros.
"""
import os
import math
import datetime as dt

import numpy as np
import requests
from rasterio.io import MemoryFile
from rasterio.features import shapes as rio_shapes
import geopandas as gpd
from shapely.geometry import shape
from pyproj import Transformer

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
SH_CATALOG_URL = "https://sh.dataspace.copernicus.eu/catalog/v1/search"
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"

class PipelineError(Exception):
    pass


EVALSCRIPT_BANDAS = """
//VERSION=3
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
RESOLUCAO_M_FIXA = 10.0  # resolução (m/pixel) usada em toda a análise


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


def _resolucao_para_dimensoes(bbox_wgs84, resolucao_m=10, max_px=1500):
    """Calcula largura/altura em pixels a partir do bbox e resolução alvo,
    limitando o tamanho máximo para respeitar cotas da API."""
    epsg_utm = epsg_wgs84_utm((bbox_wgs84[0] + bbox_wgs84[2]) / 2,
                               (bbox_wgs84[1] + bbox_wgs84[3]) / 2)
    para_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_utm}", always_xy=True)
    x_min, y_min = para_utm.transform(bbox_wgs84[0], bbox_wgs84[1])
    x_max, y_max = para_utm.transform(bbox_wgs84[2], bbox_wgs84[3])

    largura = int(min(max_px, max(64, round((x_max - x_min) / resolucao_m))))
    altura = int(min(max_px, max(64, round((y_max - y_min) / resolucao_m))))
    return largura, altura


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


def gerar_poligono_area_queimada(dados_antes, dados_depois, perfil, lon, lat,
                                  limiar_dnbr=0.1, area_minima_m2=500.0):
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


def executar_pipeline(data_referencia, latitude, longitude,
                       janela_dias, nuvem_maxima, limiar_dnbr, workdir,
                       client_id, client_secret,
                       raio_km=3.0, filtro_pixels=3):
    """Executa o pipeline completo (via Sentinel Hub) e retorna um dicionário
    com o resultado. client_id/client_secret são as credenciais informadas
    pelo usuário no próprio formulário (não ficam armazenadas no servidor).
    NDVI de vegetação é fixo (ver constante no topo
    do arquivo), não ajustáveis via parâmetro. filtro_pixels define a área
    mínima em número de pixels (cada pixel = RESOLUCAO_M_FIXA²)."""
    os.makedirs(workdir, exist_ok=True)

    area_minima_m2 = max(0, filtro_pixels) * (RESOLUCAO_M_FIXA ** 2)

    token = obter_token(client_id, client_secret)
    bbox_wgs84 = calcular_bbox_wgs84(longitude, latitude, raio_km)

    cena_antes = buscar_cena_sentinel2(bbox_wgs84, data_referencia, "before",
                                        token, janela_dias, nuvem_maxima)
    cena_depois = buscar_cena_sentinel2(bbox_wgs84, data_referencia, "after",
                                         token, janela_dias, nuvem_maxima)

    if not cena_antes or not cena_depois:
        raise PipelineError(
            "Não foi possível encontrar cenas antes/depois dentro da janela "
            "de dias definida. Aumente 'janela_dias' ou 'nuvem_maxima'."
        )

    largura_px, altura_px = _resolucao_para_dimensoes(bbox_wgs84, resolucao_m=RESOLUCAO_M_FIXA)

    dados_antes_raw, perfil = obter_bandas(
        bbox_wgs84, cena_antes["data"], token, largura_px, altura_px)
    dados_depois_raw, _ = obter_bandas(
        bbox_wgs84, cena_depois["data"], token, largura_px, altura_px)

    dados_antes = tuple(dados_antes_raw)   # (B04, B08, B12, dataMask)
    dados_depois = tuple(dados_depois_raw)

    gdf_resultado, area_ha, epsg_utm = gerar_poligono_area_queimada(
        dados_antes, dados_depois, perfil, longitude, latitude,
        limiar_dnbr=limiar_dnbr, area_minima_m2=area_minima_m2,
    )

    caminho_shp = os.path.join(workdir, "area_queimada_sirgas2000.shp")
    gdf_resultado.to_file(caminho_shp, encoding="utf-8")

    caminho_geojson = os.path.join(workdir, "area_queimada.geojson")
    gdf_resultado.to_file(caminho_geojson, driver="GeoJSON")

    return {
        "cena_antes": cena_antes,
        "cena_depois": cena_depois,
        "area_ha": round(area_ha, 4),
        "epsg_utm_usado": epsg_utm,
        "epsg_saida": 4674,
        "bbox_consultado": bbox_wgs84,
        "shapefile_path": caminho_shp,
        "geojson_path": caminho_geojson,
    }
