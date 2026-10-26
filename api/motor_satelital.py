

import os
from datetime import datetime, timedelta, timezone

import openeo
import requests
import numpy as np
import tifffile

from PIL import Image
from dotenv import load_dotenv

load_dotenv()

OPENEO_URL = "https://openeo.dataspace.copernicus.eu"
STAC_URL = "https://stac.dataspace.copernicus.eu/v1/search"


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


def obtener_proyeccion_utm(zona):
    longitud = (zona["west"] + zona["east"]) / 2
    latitud = (zona["south"] + zona["north"]) / 2

    numero_zona = int((longitud + 180) / 6) + 1
    numero_zona = max(1, min(60, numero_zona))

    return (
        32600 + numero_zona
        if latitud >= 0
        else 32700 + numero_zona
    )


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


# ---------------------------------------------
# NDVI Y NDMI
# ---------------------------------------------

def calcular_indices(zona, fecha_inicio, fecha_fin):
    conexion = conectar_copernicus()

    proyeccion = obtener_proyeccion_utm(zona)

    cubo = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["B04", "B08", "B11"],
        max_cloud_cover=20
    )

    cubo = cubo.resample_spatial(
        resolution=10,
        projection=proyeccion,
        method="bilinear"
    )

    cubo_scl = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["SCL"],
        max_cloud_cover=20
    )

    cubo_scl = cubo_scl.resample_cube_spatial(
        cubo,
        method="near"
    )

    rojo = cubo.band("B04")
    nir = cubo.band("B08")
    swir = cubo.band("B11")
    scl = cubo_scl.band("SCL")

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


def exportar_ndvi(zona, fecha_inicio, fecha_fin, destino):
    indices = calcular_indices(
        zona, fecha_inicio, fecha_fin
    )

    indices["ndvi"].save_result(
        format="GTiff"
    ).download(destino)

    return destino


def exportar_ndmi(zona, fecha_inicio, fecha_fin, destino):
    indices = calcular_indices(
        zona, fecha_inicio, fecha_fin
    )

    indices["ndmi"].save_result(
        format="GTiff"
    ).download(destino)

    return destino


def crear_png_ndmi(archivo_tif, archivo_png):
    ndmi = tifffile.imread(archivo_tif).astype(np.float32)

    validos = (
        np.isfinite(ndmi)
        & (ndmi >= -1)
        & (ndmi <= 1)
    )

    if not np.any(validos):
        raise ValueError("El mapa NDMI no contiene datos válidos")

    valores = [-1, -0.2, 0, 0.2, 0.4, 1]

    colores = [
        (165, 0, 38),
        (244, 109, 67),
        (254, 224, 139),
        (166, 217, 106),
        (26, 150, 65),
        (33, 102, 172)
    ]

    rgba = np.zeros(
        (*ndmi.shape, 4),
        dtype=np.uint8
    )

    for canal in range(3):
        rgba[:, :, canal] = np.interp(
            ndmi,
            valores,
            [color[canal] for color in colores]
        ).astype(np.uint8)

    rgba[:, :, 3] = np.where(
        validos, 255, 0
    ).astype(np.uint8)

    Image.fromarray(rgba, "RGBA").save(archivo_png)

    return archivo_png


# ---------------------------------------------
# BUSCAR IMÁGENES RECIENTES
# ---------------------------------------------

def buscar_imagenes_recientes(
    zona,
    dias=30,
    nubosidad_max=20
):
    ahora = datetime.now(timezone.utc)
    inicio = ahora - timedelta(days=dias)

    consulta = {
        "collections": ["sentinel-2-l2a"],
        "bbox": [
            zona["west"],
            zona["south"],
            zona["east"],
            zona["north"]
        ],
        "datetime": (
            inicio.strftime("%Y-%m-%dT%H:%M:%SZ")
            + "/" +
            ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        "limit": 100
    }

    respuesta = requests.post(
        STAC_URL,
        json=consulta,
        timeout=30
    )

    respuesta.raise_for_status()

    escenas = []

    for imagen in respuesta.json().get("features", []):
        datos = imagen.get("properties", {})

        escenas.append({
            "id": imagen.get("id"),
            "fecha": datos.get("datetime"),
            "nubosidad": datos.get("eo:cloud_cover")
        })

    escenas.sort(
        key=lambda x: x["fecha"] or "",
        reverse=True
    )

    utiles = [
        escena
        for escena in escenas
        if escena["nubosidad"] is not None
        and escena["nubosidad"] <= nubosidad_max
    ]

    return {
        "todas": escenas,
        "utiles": utiles,
        "mejor_disponible": utiles[0] if utiles else None
    }


# ---------------------------------------------
# IMÁGENES RGB EN COLOR NATURAL
# ---------------------------------------------

def exportar_rgb_reciente(
    zona,
    destino_tif,
    dias=30,
    nubosidad_max=20
):
    resultado = buscar_imagenes_recientes(
        zona,
        dias=dias,
        nubosidad_max=nubosidad_max
    )

    escena = resultado["mejor_disponible"]

    if not escena:
        raise ValueError(
            "No hay imágenes recientes con nubosidad aceptable"
        )

    fecha_captura = datetime.fromisoformat(
        escena["fecha"].replace("Z", "+00:00")
    )

    inicio = fecha_captura.date().isoformat()
    fin = (
        fecha_captura.date() + timedelta(days=1)
    ).isoformat()

    conexion = conectar_copernicus()

    cubo = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[inicio, fin],
        bands=["B04", "B03", "B02"],
        max_cloud_cover=nubosidad_max
    )

    cubo = cubo.resample_spatial(
        resolution=10,
        projection=obtener_proyeccion_utm(zona),
        method="bilinear"
    )

    cubo = cubo.reduce_dimension(
        dimension="t",
        reducer="median"
    )

    cubo.save_result(
        format="GTiff"
    ).download(destino_tif)

    return {
        "archivo": destino_tif,
        "fecha_captura": escena["fecha"],
        "escena_referencia": escena["id"],
        "nubosidad_escena": escena["nubosidad"],
        "resolucion_metros": 10
    }


def crear_png_rgb(archivo_tif, archivo_png):
    # GeoTIFF con bandas B04, B03 y B02
    with tifffile.TiffFile(archivo_tif) as tif:
        datos = tif.asarray()

    datos = np.asarray(datos, dtype=np.float32)

    if datos.ndim != 3:
        raise ValueError(
            "El archivo RGB debe contener tres bandas"
        )

    # Aceptar bandas primero o bandas al final
    if datos.shape[0] == 3:
        datos = np.moveaxis(datos, 0, -1)
    elif datos.shape[-1] != 3:
        raise ValueError(
            "No se encontraron las tres bandas RGB"
        )

    validos = np.all(np.isfinite(datos), axis=2)

    # Los valores Sentinel-2 L2A suelen estar
    # expresados en escala de reflectancia x10000.
    # Contraste visual fijo de 0 a 3000.
    rgb = np.clip(datos / 3000.0, 0, 1)

    # Corrección gamma para visualizar
    rgb = np.power(rgb, 1 / 1.2)

    rgba = np.zeros(
        (datos.shape[0], datos.shape[1], 4),
        dtype=np.uint8
    )

    rgba[:, :, :3] = (
        rgb * 255
    ).astype(np.uint8)

    rgba[:, :, 3] = np.where(
        validos, 255, 0
    ).astype(np.uint8)

    Image.fromarray(
        rgba,
        "RGBA"
    ).save(archivo_png)

    return archivo_png
