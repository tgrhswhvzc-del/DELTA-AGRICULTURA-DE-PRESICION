"""Publicación controlada de SAVI, EVI2 y cambio NDVI, sin modificar índices base.

Requiere los módulos ya existentes de DELTA:
  probar_mapas_extra, mapas_extra_experimental, motor_satelital,
  analisis_parcela y supabase_client.

Variables: PARCELA_ID, FECHA_ESCENA, PUBLICAR_COMPLEMENTARIOS (false por defecto).
Los mapas se publican en el bucket privado mapas-satelitales bajo la fecha de escena.
"""
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import calculate_default_transform, transform_geom

import probar_mapas_extra as prueba
from analisis_parcela import VERSION_ANALISIS, firma_perimetro, normalizar_poligono
from motor_satelital import obtener_zona
from supabase_client import supabase

BUCKET = "mapas-satelitales"
DESTINO = Path("artefactos/complementarios_publicacion")
INDICES = ("savi", "evi2", "cambio_ndvi")


def _verificar_base(parcela_id, fecha, perimetro):
    """Exige mapa base real y consistente con fecha/perímetro actual."""
    filas = (supabase.table("indices_agricolas")
             .select("fecha_escena_referencia")
             .eq("parcela_id", parcela_id)
             .eq("fecha_escena_referencia", fecha)
             .limit(1).execute()).data or []
    if not filas:
        raise ValueError("Faltan NDVI/NDMI de referencia para esta parcela y fecha")

    firma = firma_perimetro(perimetro)
    for indice in ("ndvi", "ndmi"):
        ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.json"
        meta = json.loads(supabase.storage.from_(BUCKET).download(ruta).decode("utf-8"))
        if not (meta.get("perimetro_hash") == firma
                and meta.get("version_analisis") == VERSION_ANALISIS
                and meta.get("perimetro_recortado") is True
                and meta.get("fecha_escena_referencia") == fecha):
            raise ValueError(f"{indice.upper()} de referencia incompatible; no publicar")


def _preparar_metadatos(indice, carpeta, parcela_id, fecha, perimetro):
    """Inspecciona artefactos reales y devuelve metadatos aptos para Leaflet y DELTA IA."""
    tif = carpeta / f"{indice}.tif"
    png = carpeta / f"{indice}.png"
    origen_json = carpeta / f"{indice}.json"
    if not all(p.is_file() for p in (tif, png, origen_json)):
        raise ValueError(f"{indice}: archivos TIF/PNG/JSON incompletos")

    anterior = json.loads(origen_json.read_text(encoding="utf-8"))
    if anterior.get("indice") != indice.upper() or anterior.get("fecha_escena_referencia") != fecha:
        raise ValueError(f"{indice}: metadatos originales incompatibles")
    if indice == "cambio_ndvi":
        fecha_anterior = anterior.get("fecha_anterior")
        if not fecha_anterior or date.fromisoformat(fecha_anterior) >= date.fromisoformat(fecha):
            raise ValueError("Cambio NDVI sin fecha anterior válida")
    else:
        fecha_anterior = None

    with rasterio.open(tif) as src:
        if src.count != 1 or src.crs is None or src.nodata is None:
            raise ValueError(f"{indice}: TIFF inválido o sin georreferenciación/nodata")
        banda = src.read(1, masked=True)
        dentro = geometry_mask(
            [transform_geom("EPSG:4326", src.crs, perimetro)],
            out_shape=banda.shape, transform=src.transform,
            invert=True, all_touched=False,
        )
        valores = np.asarray(banda.data, dtype=np.float32)
        minimo, maximo = prueba.RANGOS[indice]
        validos = (dentro & ~np.ma.getmaskarray(banda) & np.isfinite(valores)
                   & (valores >= minimo) & (valores <= maximo))
        total = int(dentro.sum())
        cantidad = int(validos.sum())
        cobertura = cantidad / total if total else 0.0
        if cobertura < 0.50:
            raise ValueError(f"{indice}: cobertura insuficiente ({cobertura:.1%})")
        promedio = float(valores[validos].mean(dtype=np.float64))
        if cantidad != int(anterior.get("pixeles_validos", -1)):
            raise ValueError(f"{indice}: cantidad de píxeles no coincide con la prueba")
        if not np.isclose(promedio, float(anterior.get("promedio", 0)), atol=1e-5):
            raise ValueError(f"{indice}: promedio no coincide con la prueba")

        transf, ancho, alto = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds
        )
        oeste, norte = transf.c, transf.f
        este, sur = oeste + transf.a * ancho, norte + transf.e * alto
        resolucion_metros = round(float(max(abs(src.res[0]), abs(src.res[1]))), 3)

    from PIL import Image
    with Image.open(png) as img:
        if img.mode != "RGBA" or img.size != (ancho, alto):
            raise ValueError(f"{indice}: PNG no coincide con geometría del TIFF")

    ahora = datetime.now(timezone.utc).isoformat()
    metadata = {
        "indice": indice.upper(),
        "parcela_id": parcela_id,
        "fecha_escena_referencia": fecha,
        "ultima_fecha_escena_referencia": fecha,
        "fecha_inicio_periodo": fecha,
        "fecha_fin_periodo": fecha,
        "fecha_procesamiento": ahora,
        "fecha_anterior": fecha_anterior,
        "promedio": round(promedio, 6),
        "pixeles_validos": cantidad,
        "cobertura_parcela": round(cobertura, 4),
        "bounds": [[round(sur, 8), round(oeste, 8)],
                   [round(norte, 8), round(este, 8)]],
        "escala": [{"valor": v, "color": color} for v, color in prueba.PALETAS[indice]],
        "escala_tipo": "fija_absoluta",
        "version_analisis": VERSION_ANALISIS,
        "perimetro_recortado": True,
        "perimetro_hash": firma_perimetro(perimetro),
        "resolucion_bandas_metros": 10,
        "resolucion_raster_metros": resolucion_metros,
        "tipo_mapa": "cambio_temporal" if indice == "cambio_ndvi" else "indice_vegetacion",
        "calidad_atmosferica": "no_determinada",
        "nota_calidad": (
            "Cobertura geométrica no equivale a calidad atmosférica. SCL 7 sin clasificar. "
            "No diagnosticar plagas, enfermedades, estrés hídrico o nutrición con un solo mapa."
        ),
    }
    if fecha_anterior:
        metadata["dias_entre_escenas"] = (date.fromisoformat(fecha) - date.fromisoformat(fecha_anterior)).days
        metadata["nota_comparacion"] = (
            "Diferencia NDVI actual menos anterior; depende de cobertura del suelo, "
            "fenología, atmósfera y coincidencia espacial de píxeles."
        )
    return metadata


def _subir(ruta, origen, tipo):
    supabase.storage.from_(BUCKET).upload(
        ruta, origen.read_bytes(),
        {"content-type": tipo, "upsert": "true"},
    )


def _publicar(indice, carpeta, parcela_id, fecha, metadata):
    prefijo = f"parcelas/{parcela_id}/{indice}/{fecha}"
    _subir(prefijo + ".tif", carpeta / f"{indice}.tif", "image/tiff")
    _subir(prefijo + ".png", carpeta / f"{indice}.png", "image/png")
    json_final = carpeta / f"{indice}.json"
    json_final.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    # JSON se publica al final: actúa como marca de mapa disponible.
    _subir(prefijo + ".json", json_final, "application/json")
    print(f"PUBLICADO {indice.upper()} {fecha}", flush=True)


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    fecha = date.fromisoformat(os.environ["FECHA_ESCENA"].strip()).isoformat()
    publicar = os.getenv("PUBLICAR_COMPLEMENTARIOS", "false").strip().lower() == "true"
    fin = (date.fromisoformat(fecha) + timedelta(days=1)).isoformat()

    filas = (supabase.table("parcelas").select("perimetro")
             .eq("id", parcela_id).limit(1).execute()).data or []
    if not filas or not filas[0].get("perimetro"):
        raise ValueError("Parcela inexistente o sin perímetro")
    perimetro = normalizar_poligono(filas[0]["perimetro"])
    _verificar_base(parcela_id, fecha, perimetro)
    zona = obtener_zona(perimetro)

    # Esta ruta sólo se usa para los nuevos mapas; no se tocan artefactos del sistema base.
    carpeta = DESTINO / parcela_id / fecha
    carpeta.mkdir(parents=True, exist_ok=True)
    prueba.DESTINO = carpeta
    # Evitar arrastrar un mapa de cambios de otra ejecución local.
    for indice in INDICES:
        for extension in ("tif", "png", "json"):
            (carpeta / f"{indice}.{extension}").unlink(missing_ok=True)

    print(f"DELTA COMPLEMENTARIOS: parcela={parcela_id}, fecha={fecha}, publicar={publicar}", flush=True)
    indices = prueba._calcular_con_reintentos(zona, fecha, fin)
    with tempfile.TemporaryDirectory() as tmp:
        for indice in ("savi", "evi2"):
            origen = Path(tmp) / f"{indice}_origen.tif"
            print(f"BATCH {indice.upper()}: iniciando", flush=True)
            prueba._ejecutar_batch_con_reintentos(indices[indice], indice, origen)
            prueba._probar_indice(indice, origen, perimetro, fecha)

    # Opcional. Si no hay fecha anterior comparable, publicar sólo SAVI y EVI2.
    try:
        prueba._mapa_cambios_ndvi(parcela_id, fecha, perimetro)
    except Exception as exc:
        print(f"CAMBIO NDVI omitido por error de histórico: {exc}", flush=True)

    disponibles = [i for i in INDICES if (carpeta / f"{i}.tif").exists()]
    if not all(i in disponibles for i in ("savi", "evi2")):
        raise RuntimeError("Faltan SAVI o EVI2; publicación cancelada")
    metadata = {i: _preparar_metadatos(i, carpeta, parcela_id, fecha, perimetro)
                for i in disponibles}
    for i, meta in metadata.items():
        print(f"VALIDADO {i.upper()}: promedio={meta['promedio']} "
              f"cobertura={meta['cobertura_parcela']:.1%}", flush=True)

    if not publicar:
        print("VALIDACIÓN COMPLETA: NO SE MODIFICO SUPABASE", flush=True)
        return

    # Una edición del perímetro durante los cálculos invalida la publicación.
    filas_nuevas = (supabase.table("parcelas").select("perimetro")
                    .eq("id", parcela_id).limit(1).execute()).data or []
    if not filas_nuevas or firma_perimetro(normalizar_poligono(filas_nuevas[0]["perimetro"])) != firma_perimetro(perimetro):
        raise ValueError("El perímetro cambió durante el cálculo; no se publica")
    _verificar_base(parcela_id, fecha, perimetro)
    for indice in disponibles:
        _publicar(indice, carpeta, parcela_id, fecha, metadata[indice])
    print("PUBLICACIÓN COMPLEMENTARIA COMPLETA", flush=True)


if __name__ == "__main__":
    main()
