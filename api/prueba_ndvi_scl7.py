"""Comparación aislada NDVI: máscara SCL original (4,5) versus candidata (4,5,7).
Solo lee de Supabase. No publica ni modifica ningún archivo remoto.
"""
import json
import os
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom

from supabase_client import supabase
from analisis_parcela import normalizar_poligono, procesar_indice
from motor_satelital import conectar_copernicus, obtener_proyeccion_utm, obtener_zona

BUCKET = "mapas-satelitales"
SALIDAS = Path("artefactos")


def _contar_validos(tif, perimetro):
    with rasterio.open(tif) as src:
        datos = src.read(1)
        geo = transform_geom("EPSG:4326", src.crs, perimetro)
        dentro = geometry_mask(
            [geo], out_shape=datos.shape, transform=src.transform,
            invert=True, all_touched=False
        )
        validos = dentro & np.isfinite(datos) & (datos >= -1) & (datos <= 1)
        if src.nodata is not None and np.isfinite(src.nodata):
            validos &= datos != src.nodata
        return int(dentro.sum()), int(validos.sum())


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    fecha = date.fromisoformat(os.environ["FECHA_ESCENA"].strip())
    inicio, fin = fecha.isoformat(), (fecha + timedelta(days=1)).isoformat()

    consulta = supabase.table("parcelas").select("perimetro").eq("id", parcela_id).limit(1).execute()
    if not consulta.data or not consulta.data[0].get("perimetro"):
        raise ValueError("Parcela inexistente o sin perímetro")
    perimetro = normalizar_poligono(consulta.data[0]["perimetro"])
    zona = obtener_zona(perimetro)
    SALIDAS.mkdir(exist_ok=True)
    print(f"PRUEBA SCL7: parcela {parcela_id} fecha {inicio}", flush=True)

    # Descargar dos archivos publicados para comparación, solo lectura.
    prefijo = f"parcelas/{parcela_id}/ndvi/{inicio}"
    original_tif = SALIDAS / "ndvi_original.tif"
    original_png = SALIDAS / "ndvi_original.png"
    original_tif.write_bytes(supabase.storage.from_(BUCKET).download(prefijo + ".tif"))
    original_png.write_bytes(supabase.storage.from_(BUCKET).download(prefijo + ".png"))
    total_original, validos_original = _contar_validos(original_tif, perimetro)
    print(f"ORIGINAL: {validos_original}/{total_original} pixeles; cobertura {100*validos_original/total_original:.2f}%", flush=True)

    # Mismos cálculos y tratamiento espacial del motor, salvo añadir SCL=7.
    conexion = conectar_copernicus()
    cubo = conexion.load_collection(
        "SENTINEL2_L2A", spatial_extent=zona,
        temporal_extent=[inicio, fin], bands=["B04", "B08", "B11"], max_cloud_cover=20,
    )
    cubo = cubo.resample_spatial(
        resolution=10, projection=obtener_proyeccion_utm(zona), method="bilinear"
    )
    scl = conexion.load_collection(
        "SENTINEL2_L2A", spatial_extent=zona,
        temporal_extent=[inicio, fin], bands=["SCL"], max_cloud_cover=20,
    )
    scl = scl.resample_cube_spatial(cubo, method="near").band("SCL")
    rojo, nir = cubo.band("B04"), cubo.band("B08")
    mascara = (scl == 4) | (scl == 5) | (scl == 7)
    ndvi = ((nir - rojo) / (nir + rojo)).mask(~mascara)
    ndvi = ndvi.reduce_dimension(dimension="t", reducer="median")

    resultado_tif = SALIDAS / "ndvi_scl7_origen.tif"
    resultado_png = SALIDAS / "ndvi_scl7.png"
    resultado_recortado = SALIDAS / "ndvi_scl7_recortado.tif"
    print("COPERNICUS: procesando candidato SCL 4,5,7 por lotes...", flush=True)
    ndvi.execute_batch(
        outputfile=str(resultado_tif), out_format="GTiff", title="DELTA prueba NDVI SCL7 no publicar"
    )
    print("COPERNICUS: GeoTIFF descargado", flush=True)
    estadistica = procesar_indice(
        str(resultado_tif), perimetro, "ndvi", str(resultado_png), str(resultado_recortado)
    )
    print(f"CANDIDATO SCL7: {estadistica['pixeles_validos']} pixeles; cobertura {100*estadistica['cobertura_parcela']:.2f}%", flush=True)
    print(f"PROMEDIO NDVI CANDIDATO: {estadistica['promedio']}", flush=True)
    print("Comparar ndvi_original.png contra ndvi_scl7.png en el artefacto del trabajo.", flush=True)
    print("NO SE MODIFICO SUPABASE NI EL MOTOR ORIGINAL.", flush=True)


if __name__ == "__main__":
    main()
