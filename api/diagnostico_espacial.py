"""Resumen espacial prudente de GeoTIFFs ya publicados por DELTA.

Solo debe invocarse tras verificar la propiedad de la parcela y validar
metadatos V2 de igual perímetro y fecha mediante satelite._metadatos_v2.
No descarga imágenes nuevas de Copernicus ni modifica los mapas almacenados.
"""
from __future__ import annotations

from io import BytesIO
import math

import numpy as np
from rasterio.io import MemoryFile

from supabase_client import supabase

BUCKET = "mapas-satelitales"
ORDEN = ("ndvi", "ndmi", "cambio_ndvi", "ndre", "msi", "savi", "evi2")
MAX_PIXELES = 1_500_000
MAX_TIF_BYTES = 12_000_000
MAX_TOTAL_BYTES = 42_000_000
RANGOS = {
    "ndvi": (-1.0, 1.0),
    "ndmi": (-1.0, 1.0),
    "ndre": (-1.0, 1.0),
    "msi": (0.0, 20.0),
    "savi": (-1.5, 1.5),
    "evi2": (-1.0, 2.5),
    "cambio_ndvi": (-2.0, 2.0),
}


def _r(n, cifras=3):
    return round(float(n), cifras)


def _porcentaje(condicion, valido):
    cantidad = int(np.count_nonzero(valido))
    if cantidad == 0:
        return None
    return round(100 * int(np.count_nonzero(condicion & valido)) / cantidad, 1)


def _estadisticas(indice, datos, validos):
    valores = datos[validos].astype(np.float64)
    p10, p25, p50, p75, p90 = np.percentile(valores, (10, 25, 50, 75, 90))
    salida = {
        "pixeles_analizados": int(valores.size),
        "promedio": _r(valores.mean()),
        "mediana": _r(p50),
        "percentil_10": _r(p10),
        "percentil_90": _r(p90),
        "variacion_p10_p90": _r(p90 - p10),
    }
    # Son umbrales descriptivos, NO diagnóstico de estrés o enfermedad.
    if indice == "ndvi":
        salida["porcentaje_pixeles_ndvi_menor_0_30"] = _porcentaje(datos < 0.30, validos)
        salida["porcentaje_pixeles_ndvi_mayor_0_50"] = _porcentaje(datos > 0.50, validos)
    elif indice == "ndmi":
        salida["porcentaje_pixeles_ndmi_menor_0"] = _porcentaje(datos < 0, validos)
    elif indice == "cambio_ndvi":
        salida["porcentaje_pixeles_disminucion_mayor_0_10"] = _porcentaje(datos < -0.10, validos)
        salida["porcentaje_pixeles_aumento_mayor_0_10"] = _porcentaje(datos > 0.10, validos)
    return salida


def _sectores_ndvi(ndvi, validos, transform):
    """Muestra diferencias en una cuadrícula 3x3, NO puntos GPS de diagnóstico."""
    if not (math.isclose(transform.b, 0, abs_tol=1e-8)
            and math.isclose(transform.d, 0, abs_tol=1e-8)
            and transform.a > 0 and transform.e < 0):
        return None
    alto, ancho = ndvi.shape
    if alto < 9 or ancho < 9:
        return None
    nombres = ("noroeste", "norte", "noreste", "oeste", "centro", "este",
               "suroeste", "sur", "sureste")
    divisiones_f = np.linspace(0, alto, 4).astype(int)
    divisiones_c = np.linspace(0, ancho, 4).astype(int)
    sectores = []
    for f in range(3):
        for c in range(3):
            sl = np.s_[divisiones_f[f]:divisiones_f[f+1],
                       divisiones_c[c]:divisiones_c[c+1]]
            v = validos[sl]
            n = int(v.sum())
            if n < 20 or n < 0.20 * v.size:
                continue
            sectores.append({"sector": nombres[3*f+c], "ndvi_promedio": _r(ndvi[sl][v].mean()),
                             "pixeles_validos": n})
    if len(sectores) < 3:
        return None
    sectores.sort(key=lambda x: x["ndvi_promedio"])
    diferencia = sectores[-1]["ndvi_promedio"] - sectores[0]["ndvi_promedio"]
    if diferencia < 0.07:
        return {"heterogeneidad_relevante": False, "diferencia_extremos": _r(diferencia)}
    return {"heterogeneidad_relevante": True,
            "diferencia_extremos": _r(diferencia),
            "sectores_ndvi_mas_bajo": sectores[:2],
            "sector_ndvi_mas_alto": sectores[-1],
            "nota": "Sectores aproximados en cuadrícula 3x3; no indican causa ni límite exacto."}


def analizar_mapas_espaciales(parcela_id: str, fecha: str, metadatos_validados: dict) -> dict:
    """Calcula evidencia numérica compacta. Cada TIFF debe concordar con su JSON V2.

    No almacena los datos, no modifica mapas y no requiere credenciales de OpenAI.
    """
    if not fecha or not metadatos_validados:
        return {"estado": "sin_mapas_validados"}

    salida = {
        "estado": "analisis_parcial",
        "fecha_referencia": fecha,
        "naturaleza": "Señales espectrales, no diagnóstico clínico ni medición directa del suelo.",
        "indices_espaciales": {},
        "indices_omitidos": [],
        "criterio_porcentajes": "Porcentaje de píxeles válidos observados para cada índice, no del total de hectáreas.",
        "advertencias": [
            "NDVI bajo puede representar suelo descubierto o una etapa temprana del cultivo.",
            "NDMI y MSI no demuestran por sí solos falta de riego.",
            "NDRE/MSI pueden tener menor cobertura válida por máscara de nubes o SCL no clasificado.",
        ],
    }
    rasters = {}
    bytes_acumulados = 0
    for indice in ORDEN:
        meta = metadatos_validados.get(indice)
        if not meta:
            continue
        n_meta = meta.get("pixeles_validos")
        if isinstance(n_meta, (int, float)) and n_meta > MAX_PIXELES:
            salida["indices_omitidos"].append(indice + ": mapa demasiado grande")
            continue
        ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.tif"
        try:
            contenido = supabase.storage.from_(BUCKET).download(ruta)
            if len(contenido) > MAX_TIF_BYTES or bytes_acumulados + len(contenido) > MAX_TOTAL_BYTES:
                salida["indices_omitidos"].append(indice + ": TIFF supera límite de análisis")
                continue
            bytes_acumulados += len(contenido)
            with MemoryFile(contenido) as mem, mem.open() as src:
                if src.count != 1 or src.width * src.height > MAX_PIXELES or not src.crs:
                    raise ValueError("GeoTIFF sin geometría utilizable")
                datos = src.read(1, masked=True)
                m = np.ma.getmaskarray(datos)
                a = np.asarray(datos.data, dtype=np.float32)
                lo, hi = RANGOS[indice]
                validos = ~m & np.isfinite(a) & (a >= lo) & (a <= hi)
                n = int(validos.sum())
                if n < 50:
                    raise ValueError("menos de 50 píxeles válidos")
                if meta.get("pixeles_validos") is not None:
                    if abs(int(meta["pixeles_validos"]) - n) > max(10, int(n * .005)):
                        raise ValueError("píxeles distintos de metadatos validados")
                if meta.get("promedio") is not None:
                    if abs(float(meta["promedio"]) - float(a[validos].mean(dtype=np.float64))) > .002:
                        raise ValueError("promedio no coincide con metadatos validados")
                salida["indices_espaciales"][indice] = _estadisticas(indice, a, validos)
                if indice in ("ndvi", "ndmi"):
                    rasters[indice] = (a, validos, src.transform, src.crs)
                if indice == "ndvi":
                    salida["sectores"] = _sectores_ndvi(a, validos, src.transform)
        except Exception as e:
            # Se omite el mapa defectuoso en lugar de inventar valores o fallar la consulta IA.
            salida["indices_omitidos"].append(indice + ": " + type(e).__name__)

    if "ndvi" in rasters and "ndmi" in rasters:
        vi, mv, t1, crs1 = rasters["ndvi"]
        mi, mm, t2, crs2 = rasters["ndmi"]
        if vi.shape == mi.shape and t1 == t2 and crs1 == crs2:
            comun = mv & mm
            if int(comun.sum()) >= 100:
                # Doble anomalía *relativa* al lote, sin atribuir causalidad ni gravedad.
                ndvi_central = float(np.median(vi[comun]))
                ndmi_central = float(np.median(mi[comun]))
                coincidencia = (vi < ndvi_central - .08) & (mi < ndmi_central - .05)
                salida["coincidencia_ndvi_ndmi"] = {
                    "porcentaje_pixeles_ambas_senales_relativamente_bajas": _porcentaje(coincidencia, comun),
                    "pixeles_comparables": int(comun.sum()),
                    "umbral_ndvi_relativo": _r(ndvi_central - .08),
                    "umbral_ndmi_relativo": _r(ndmi_central - .05),
                    "nota": "Anomalía relativa exploratoria: no demuestra estrés hídrico, nutrición ni enfermedad.",
                }
        else:
            salida["advertencias"].append("NDVI/NDMI con distinta malla: no se cruzan píxeles sin alineación.")
    if salida["indices_espaciales"]:
        salida["estado"] = "analisis_disponible"
    return salida
