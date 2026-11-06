"""Cálculos experimentales MSI y NDRE, aislados del motor NDVI/NDMI de DELTA.

No escribe en Supabase. Se utiliza primero con prueba_indices_avanzados.py.
"""
from motor_satelital import conectar_copernicus, obtener_proyeccion_utm


def calcular_indices_avanzados(zona, fecha_inicio, fecha_fin):
    conexion = conectar_copernicus()

    # Las tres bandas espectrales tienen resolución nativa de 20 m.
    espectral = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["B05", "B8A", "B11"],
        max_cloud_cover=20,
    )
    espectral = espectral.resample_spatial(
        resolution=20,
        projection=obtener_proyeccion_utm(zona),
        method="bilinear",
    )

    # Igual que el NDVI original: SCL se usa como máscara, no en aritmética espectral.
    clasificacion = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[fecha_inicio, fecha_fin],
        bands=["SCL"],
        max_cloud_cover=20,
    )
    clasificacion = clasificacion.resample_cube_spatial(
        espectral, method="near"
    )

    red_edge = espectral.band("B05")
    nir_estrecho = espectral.band("B8A")
    swir = espectral.band("B11")
    scl = clasificacion.band("SCL")
    valido = (scl == 4) | (scl == 5) | (scl == 7)

    # No mezclamos bandas de cubos diferentes en operaciones aritméticas.
    ndre = (nir_estrecho - red_edge) / (nir_estrecho + red_edge)
    msi = swir / nir_estrecho

    return {
        "ndre": ndre.mask(~valido).reduce_dimension("t", reducer="median"),
        "msi": msi.mask(~valido).reduce_dimension("t", reducer="median"),
    }
