"""DELTA: cálculos experimentales SAVI/EVI2; no modifica NDVI/NDMI.

Sentinel-2 L2A en openEO entrega valores digitales (DN). Convertir a
reflectancia (DN * 0.0001) ES NECESARIO para SAVI y EVI2 porque incluyen
constantes aditivas. Para NDVI/NDMI el factor se cancela.
"""

from motor_satelital import conectar_copernicus, obtener_proyeccion_utm


def calcular_savi_evi2(zona, fecha_inicio, fecha_fin):
    conexion = conectar_copernicus()
    cubo = conexion.load_collection(
        "SENTINEL2_L2A", spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["B04", "B08"], max_cloud_cover=20,
    ).resample_spatial(
        resolution=10, projection=obtener_proyeccion_utm(zona), method="bilinear",
    )
    scl = conexion.load_collection(
        "SENTINEL2_L2A", spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["SCL"], max_cloud_cover=20,
    ).resample_cube_spatial(cubo, method="near").band("SCL")

    # SCL 7 es "sin clasificar"; se conserva por consistencia con DELTA V2,
    # pero la cobertura no equivale a fiabilidad agronómica.
    valido = (scl == 4) | (scl == 5) | (scl == 7)
    rojo = cubo.band("B04") * 0.0001
    nir = cubo.band("B08") * 0.0001

    savi = 1.5 * (nir - rojo) / (nir + rojo + 0.5)
    evi2 = 2.5 * (nir - rojo) / (nir + 2.4 * rojo + 1.0)
    return {
        "savi": savi.mask(~valido).reduce_dimension("t", reducer="median"),
        "evi2": evi2.mask(~valido).reduce_dimension("t", reducer="median"),
    }


# LAI físico: requiere modelo biofísico validado (bandas, ángulos solares y
# de observación). NO derivarlo con una simple fórmula de EVI2 sin calibración.
# Temperatura: requiere Landsat L2 ST, escala, máscara QA y fecha propia.
