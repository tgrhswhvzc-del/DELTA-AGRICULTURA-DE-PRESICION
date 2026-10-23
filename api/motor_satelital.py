
import os
import openeo
from dotenv import load_dotenv

load_dotenv()

OPENEO_URL = "https://openeo.dataspace.copernicus.eu"

def conectar_copernicus():
    token = os.getenv("OPENEO_REFRESH_TOKEN")

    if not token:
        raise RuntimeError("Falta configurar OPENEO_REFRESH_TOKEN")

    conexion = openeo.connect(OPENEO_URL)

    conexion.authenticate_oidc_refresh_token(
        provider_id="CDSE",
        refresh_token=token,
        store_refresh_token=False
    )

    return conexion


def calcular_indices(zona, fecha_inicio, fecha_fin):
    conexion = conectar_copernicus()

    cubo = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["B04", "B08", "B11", "SCL"],
        max_cloud_cover=20
    )

    rojo = cubo.band("B04")
    nir = cubo.band("B08")
    swir = cubo.band("B11")
    scl = cubo.band("SCL")

    mascara_valida = (scl == 4) | (scl == 5)

    ndvi = (nir - rojo) / (nir + rojo)
    ndmi = (nir - swir) / (nir + swir)

    ndvi = ndvi.mask(~mascara_valida)
    ndmi = ndmi.mask(~mascara_valida)

    ndvi = ndvi.reduce_dimension(
        dimension="t",
        reducer="median"
    )

    ndmi = ndmi.reduce_dimension(
        dimension="t",
        reducer="median"
    )

    return {
        "ndvi": ndvi,
        "ndmi": ndmi
    }


def obtener_zona(perimetro):
    if perimetro.get("type") != "Polygon":
        raise ValueError("El perímetro debe ser un polígono")

    puntos = perimetro["coordinates"][0]

    longitudes = [punto[0] for punto in puntos]
    latitudes = [punto[1] for punto in puntos]

    return {
        "west": min(longitudes),
        "south": min(latitudes),
        "east": max(longitudes),
        "north": max(latitudes)
    }
def exportar_ndvi(zona, fecha_inicio, fecha_fin, destino):
    indices = calcular_indices(zona, fecha_inicio, fecha_fin)

    ndvi = indices["ndvi"]

    ndvi.save_result(format="GTiff").download(destino)

    return destino

def exportar_ndmi(zona, fecha_inicio, fecha_fin, destino):
    indices = calcular_indices(zona, fecha_inicio, fecha_fin)

    ndmi = indices["ndmi"]

    ndmi.save_result(format="GTiff").download(destino)

    return destino