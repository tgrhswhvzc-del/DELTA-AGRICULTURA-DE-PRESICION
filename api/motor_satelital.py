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

    # Bandas base
    rojo = cubo.band("B04")   # 10 m
    nir = cubo.band("B08")    # 10 m
    swir = cubo.band("B11")   # 20 m
    scl = cubo.band("SCL")    # 20 m

    # Reescalar B11 y SCL a la malla de 10 m de B08
    swir_10m = swir.resample_cube_spatial(nir, method="bilinear")
    scl_10m = scl.resample_cube_spatial(nir, method="near")

    # Máscara válida: vegetación y suelo descubierto
    mascara_valida = (scl_10m == 4) | (scl_10m == 5)

    # Índices
    ndvi = (nir - rojo) / (nir + rojo)
    ndmi = (nir - swir_10m) / (nir + swir_10m)

    # Aplicar máscara
    ndvi = ndvi.mask(~mascara_valida)
    ndmi = ndmi.mask(~mascara_valida)

    # Reducir en el tiempo
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