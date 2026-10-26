
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


def obtener_proyeccion_utm(zona):
    longitud = (zona["west"] + zona["east"]) / 2
    latitud = (zona["south"] + zona["north"]) / 2

    numero_zona = int((longitud + 180) / 6) + 1
    numero_zona = max(1, min(60, numero_zona))

    if latitud >= 0:
        return 32600 + numero_zona
    else:
        return 32700 + numero_zona


def calcular_indices(zona, fecha_inicio, fecha_fin):
    conexion = conectar_copernicus()

    proyeccion = obtener_proyeccion_utm(zona)

    # Bandas espectrales
    cubo = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["B04", "B08", "B11"],
        max_cloud_cover=20
    )

    # Remuestreo a una malla de 10 metros
    cubo = cubo.resample_spatial(
        resolution=10,
        projection=proyeccion,
        method="bilinear"
    )

    # Clasificación de escena (SCL)
    cubo_scl = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["SCL"],
        max_cloud_cover=20
    )

    # Alinear SCL sin interpolar sus categorías
    cubo_scl = cubo_scl.resample_cube_spatial(
        cubo,
        method="near"
    )

    # Extraer bandas después del remuestreo
    rojo = cubo.band("B04")
    nir = cubo.band("B08")
    swir = cubo.band("B11")
    scl = cubo_scl.band("SCL")

    # Vegetación y suelo descubierto
    mascara_valida = (scl == 4) | (scl == 5)

    # Índice de vegetación
    ndvi = (nir - rojo) / (nir + rojo)

    # Índice de contenido de agua en vegetación
    ndmi = (nir - swir) / (nir + swir)

    # Excluir píxeles no válidos
    ndvi = ndvi.mask(~mascara_valida)
    ndmi = ndmi.mask(~mascara_valida)

    # Mediana temporal
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
    indices = calcular_indices(
        zona,
        fecha_inicio,
        fecha_fin
    )

    ndvi = indices["ndvi"]

    ndvi.save_result(
        format="GTiff"
    ).download(destino)

    return destino


def exportar_ndmi(zona, fecha_inicio, fecha_fin, destino):
    indices = calcular_indices(
        zona,
        fecha_inicio,
        fecha_fin
    )

    ndmi = indices["ndmi"]

    ndmi.save_result(
        format="GTiff"
    ).download(destino)

    return destino
def crear_png_ndmi(archivo_tif, archivo_png):
    import numpy as np
    import tifffile
    from PIL import Image

    ndmi = tifffile.imread(archivo_tif).astype(np.float32)

    validos = np.isfinite(ndmi) & (ndmi >= -1) & (ndmi <= 1)

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

    rgba = np.zeros((*ndmi.shape, 4), dtype=np.uint8)

    for canal in range(3):
        rgba[:, :, canal] = np.interp(
            ndmi, valores, [color[canal] for color in colores]
        ).astype(np.uint8)

    rgba[:, :, 3] = np.where(validos, 255, 0)

    Image.fromarray(rgba, "RGBA").save(archivo_png)

    return archivo_png