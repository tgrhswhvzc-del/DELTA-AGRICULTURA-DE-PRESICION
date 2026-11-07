"""Prueba aislada SAVI/EVI2 y, si hay histórico, cambio NDVI.

Inputs GitHub: PARCELA_ID, FECHA_ESCENA. Ninguna escritura en Supabase.
Los archivos generados son artefactos locales de GitHub Actions.
"""
import json
import os
import tempfile
import time
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from uuid import UUID

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom, calculate_default_transform, reproject, Resampling

from supabase_client import supabase
from motor_satelital import obtener_zona
from analisis_parcela import normalizar_poligono, firma_perimetro
from mapas_extra_experimental import calcular_savi_evi2

BUCKET = "mapas-satelitales"
DESTINO = Path("artefactos/complementarios")
PALETAS = {
    "savi": [(-1, "#704f35"), (0, "#bbaa80"), (0.2, "#e5d178"),
             (0.4, "#a9c765"), (0.6, "#4f9b3a"), (1, "#0b5a2d")],
    "evi2": [(-1, "#704f35"), (0, "#bbaa80"), (0.2, "#eadc79"),
             (0.4, "#a3ce62"), (0.6, "#4e9d40"), (1.2, "#0b5a2d")],
    "cambio_ndvi": [(-0.5, "#b2182b"), (-0.2, "#ef8a62"), (0, "#ffffbf"),
                    (0.2, "#91cf60"), (0.5, "#1a9850")],
}
RANGOS = {"savi": (-1, 1.5), "evi2": (-1, 2.5), "cambio_ndvi": (-2, 2)}


def _procesar_y_guardar(imagen, validos, perfil, indice, fecha, fecha_anterior=None):
    """Conserva el TIFF original y genera PNG RGBA reproyectado a WGS84."""
    cantidad = int(validos.sum())
    if not cantidad:
        raise ValueError(f"{indice}: sin pixeles válidos")
    nodata = -9999.0
    datos = np.where(validos, imagen, nodata).astype("float32")
    p = perfil.copy()
    p.update(driver="GTiff", count=1, dtype="float32", nodata=nodata, compress="deflate")
    tif = DESTINO / f"{indice}.tif"
    with rasterio.open(tif, "w", **p) as dst:
        dst.write(datos, 1)
    with rasterio.open(tif) as src:
        transf, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
        repro = np.full((height, width), nodata, dtype=np.float32)
        reproject(source=rasterio.band(src, 1), destination=repro,
                  src_transform=src.transform, src_crs=src.crs,
                  src_nodata=nodata, dst_transform=transf,
                  dst_crs="EPSG:4326", dst_nodata=nodata,
                  resampling=Resampling.nearest)
    posiciones = np.array([v for v, _ in PALETAS[indice]], dtype=np.float32)
    colores = np.array([tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
                        for _, c in PALETAS[indice]], dtype=np.uint8)
    visibles = (repro != nodata) & np.isfinite(repro)
    recortado = np.clip(np.nan_to_num(repro, nan=float(posiciones[0]),
                                      posinf=float(posiciones[-1]),
                                      neginf=float(posiciones[0])),
                        posiciones[0], posiciones[-1])
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    for canal in range(3):
        rgba[:, :, canal] = np.interp(recortado, posiciones, colores[:, canal]).astype(np.uint8)
    rgba[:, :, 3] = (visibles * 255).astype(np.uint8)
    Image.fromarray(rgba, "RGBA").save(DESTINO / f"{indice}.png")
    resumen = {"indice": indice.upper(), "fecha_escena_referencia": fecha,
               "fecha_anterior": fecha_anterior, "promedio": round(float(imagen[validos].mean()), 6),
               "pixeles_validos": cantidad, "nota_calidad":
               "Prueba experimental. SCL 7 no clasificado; verificar antes de recomendar insumos."}
    (DESTINO / f"{indice}.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"VALIDADO {indice.upper()}: promedio={resumen['promedio']} pixeles={cantidad}", flush=True)


def _probar_indice(indice, origen, poligono, fecha):
    with rasterio.open(origen) as src:
        if src.crs is None or src.count != 1:
            raise ValueError(f"{indice}: TIFF sin CRS o número de bandas erróneo")
        datos = src.read(1, masked=True)
        valores = np.asarray(datos.data, dtype=np.float32)
        dentro = geometry_mask([transform_geom("EPSG:4326", src.crs, poligono)],
                               out_shape=valores.shape, transform=src.transform,
                               invert=True, all_touched=False)
        minimo, maximo = RANGOS[indice]
        validos = (dentro & ~np.ma.getmaskarray(datos) & np.isfinite(valores)
                   & (valores >= minimo) & (valores <= maximo))
        cobertura = int(validos.sum()) / max(1, int(dentro.sum()))
        if cobertura < 0.50:
            raise ValueError(f"{indice}: cobertura insuficiente {cobertura:.1%}")
        print(f"{indice.upper()} cobertura={cobertura:.1%}", flush=True)
        _procesar_y_guardar(valores, validos, src.profile, indice, fecha)


def _mapa_cambios_ndvi(parcela_id, fecha_actual, poligono):
    """Opcional: produce delta NDVI solo si existe fecha anterior coherente."""
    rows = (supabase.table("indices_agricolas")
            .select("fecha_escena_referencia")
            .eq("parcela_id", parcela_id)
            .lt("fecha_escena_referencia", fecha_actual)
            .order("fecha_escena_referencia", desc=True).limit(8).execute()).data or []
    hash_actual = firma_perimetro(poligono)
    for fila in rows:
        anterior = str(fila["fecha_escena_referencia"])[:10]
        try:
            metas = []
            archivos = []
            for fecha in (fecha_actual, anterior):
                prefijo = f"parcelas/{parcela_id}/ndvi/{fecha}"
                metadatos = json.loads(supabase.storage.from_(BUCKET).download(prefijo + ".json").decode("utf-8"))
                if (metadatos.get("perimetro_hash") != hash_actual or metadatos.get("version_analisis") != 2):
                    raise ValueError("Perímetros o versiones no compatibles")
                metas.append(metadatos)
                archivos.append(supabase.storage.from_(BUCKET).download(prefijo + ".tif"))
        except Exception as exc:
            print(f"Cambio NDVI: fecha {anterior} no comparable: {exc}", flush=True)
            continue
        with tempfile.TemporaryDirectory() as td:
            caminos = [Path(td) / "actual.tif", Path(td) / "anterior.tif"]
            for p, contenido in zip(caminos, archivos):
                p.write_bytes(contenido)
            with rasterio.open(caminos[0]) as actual, rasterio.open(caminos[1]) as previo:
                a = actual.read(1, masked=True).filled(-9999).astype("float32")
                b = np.full_like(a, -9999, dtype="float32")
                reproject(source=rasterio.band(previo, 1), destination=b,
                          src_transform=previo.transform, src_crs=previo.crs,
                          src_nodata=previo.nodata, dst_transform=actual.transform,
                          dst_crs=actual.crs, dst_nodata=-9999,
                          resampling=Resampling.nearest)
                validos = (a != -9999) & (b != -9999) & np.isfinite(a) & np.isfinite(b)
                if not np.any(validos):
                    print("Cambio NDVI: sin píxeles coincidentes", flush=True)
                    return
                dif = a - b
                _procesar_y_guardar(dif, validos, actual.profile, "cambio_ndvi", fecha_actual, anterior)
        print(f"CAMBIO NDVI: {anterior} -> {fecha_actual}", flush=True)
        return
    print("CAMBIO NDVI OMITIDO: falta una fecha histórica comparable", flush=True)


def _ejecutar_batch_con_reintentos(cubo, indice, destino):
    import openeo
    espera = [15, 30, 60, 120]
    for intento in range(len(espera) + 1):
        try:
            cubo.execute_batch(outputfile=str(destino), out_format="GTiff", title=f"DELTA experimento {indice.upper()}")
            return
        except Exception as exc:
            s = str(exc).lower()
            if intento == len(espera) or not ("429" in s or "too many requests" in s):
                raise
            print(f"Copernicus 429; reintentando en {espera[intento]} s", flush=True)
            time.sleep(espera[intento])


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    fecha = date.fromisoformat(os.environ["FECHA_ESCENA"].strip()).isoformat()
    fin = (date.fromisoformat(fecha) + timedelta(days=1)).isoformat()
    fila = (supabase.table("parcelas").select("perimetro")
            .eq("id", parcela_id).limit(1).execute()).data
    if not fila or not fila[0].get("perimetro"):
        raise ValueError("Parcela no encontrada o sin perimetro")
    poligono = normalizar_poligono(fila[0]["perimetro"])
    zona = obtener_zona(poligono)
    DESTINO.mkdir(parents=True, exist_ok=True)
    print(f"DELTA PRUEBA NUEVOS MAPAS: {parcela_id} / {fecha}", flush=True)
    indices = calcular_savi_evi2(zona, fecha, fin)
    with tempfile.TemporaryDirectory() as td:
        for indice in ("savi", "evi2"):
            print(f"BATCH {indice.upper()}: iniciando", flush=True)
            origen = Path(td) / f"{indice}.tif"
            _ejecutar_batch_con_reintentos(indices[indice], indice, origen)
            _probar_indice(indice, origen, poligono, fecha)
    try:
        _mapa_cambios_ndvi(parcela_id, fecha, poligono)
    except Exception as exc:
        print(f"CAMBIO NDVI OMITIDO por fallo al consultar histórico: {exc}", flush=True)
    print("PRUEBA TERMINADA: NO SE MODIFICO SUPABASE", flush=True)


if __name__ == "__main__":
    main()
