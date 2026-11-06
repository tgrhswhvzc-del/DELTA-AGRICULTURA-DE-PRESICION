"""DELTA: validación/publicación manual de MSI y NDRE sin alterar NDVI/NDMI.

Dependencias locales ya existentes: motor_satelital, supabase_client,
analisis_parcela e indices_avanzados_experimental.

PARCELA_ID: UUID de la parcela.
FECHA_ESCENA: YYYY-MM-DD, igual a la fecha de los mapas NDVI/NDMI vigentes.
PUBLICAR_AVANZADOS: true para escribir los mapas; cualquier otro valor valida solamente.
"""
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import geometry_mask
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_geom

from supabase_client import supabase
from motor_satelital import obtener_zona
from analisis_parcela import normalizar_poligono, firma_perimetro, VERSION_ANALISIS
from indices_avanzados_experimental import calcular_indices_avanzados

BUCKET = "mapas-satelitales"
NODATA = -9999.0
COBERTURA_MINIMA = 0.50
ESCALAS = {
    "ndre": [(-1.0, "#a52a2a"), (-0.2, "#e57942"), (0.2, "#f5d864"),
             (0.5, "#66aa4b"), (1.0, "#0c512c")],
    "msi": [(0.0, "#0c512c"), (0.5, "#66aa4b"), (1.0, "#ffeb00"),
            (1.5, "#ff6900"), (3.0, "#c30019")],
}
RANGOS = {"ndre": (-1.0, 1.0), "msi": (0.0, 5.0)}


def _verificar_fecha_y_perimetro(parcela_id, fecha, hash_perimetro):
    """Exige un paquete NDVI/NDMI de referencia antes de producir MSI/NDRE."""
    registro = (
        supabase.table("indices_agricolas")
        .select("fecha_escena_referencia")
        .eq("parcela_id", parcela_id)
        .eq("fecha_escena_referencia", fecha)
        .limit(1)
        .execute()
    )
    if not registro.data:
        raise ValueError("No existe NDVI/NDMI para esa parcela y fecha. Sin cambios.")
    for indice in ("ndvi", "ndmi"):
        ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.json"
        metadata = json.loads(supabase.storage.from_(BUCKET).download(ruta).decode("utf-8"))
        if not (
            metadata.get("perimetro_hash") == hash_perimetro
            and metadata.get("version_analisis") == VERSION_ANALISIS
            and metadata.get("perimetro_recortado") is True
            and metadata.get("fecha_escena_referencia") == fecha
        ):
            raise ValueError(f"Paquete {indice.upper()} no coherente. Sin cambios.")


def _procesar_mapa(tif_origen, png_destino, tif_destino, perimetro, indice):
    """Recorta el TIFF y produce PNG EPSG:4326, como analisis_parcela.py."""
    with rasterio.open(tif_origen) as fuente:
        if fuente.count != 1 or fuente.crs is None:
            raise ValueError(f"{indice.upper()}: GeoTIFF inválido")

        banda = fuente.read(1, masked=True)
        valores = banda.data.astype(np.float32)
        geometria = geometry_mask(
            [transform_geom("EPSG:4326", fuente.crs, perimetro)],
            out_shape=valores.shape,
            transform=fuente.transform,
            invert=True,
            all_touched=False,
        )
        if not geometria.any():
            raise ValueError(f"{indice.upper()}: perímetro fuera del raster")

        minimo, maximo = RANGOS[indice]
        validos = (
            geometria
            & ~np.ma.getmaskarray(banda)
            & np.isfinite(valores)
            & (valores >= minimo)
            & (valores <= maximo)
        )
        cantidad = int(validos.sum())
        cobertura = cantidad / int(geometria.sum())
        if not cantidad or cobertura < COBERTURA_MINIMA:
            raise ValueError(f"{indice.upper()}: cobertura insuficiente ({cobertura:.1%})")

        promedio = float(valores[validos].mean(dtype=np.float64))
        recortado = np.where(validos, valores, NODATA).astype(np.float32)

        perfil = fuente.profile.copy()
        perfil.update(driver="GTiff", count=1, dtype="float32", nodata=NODATA, compress="deflate")
        with rasterio.open(tif_destino, "w", **perfil) as salida:
            salida.write(recortado, 1)

        transf, ancho, alto = calculate_default_transform(
            fuente.crs, "EPSG:4326", fuente.width, fuente.height, *fuente.bounds
        )
        imagen = np.full((alto, ancho), NODATA, dtype=np.float32)
        reproject(
            source=recortado,
            destination=imagen,
            src_transform=fuente.transform,
            src_crs=fuente.crs,
            src_nodata=NODATA,
            dst_transform=transf,
            dst_crs="EPSG:4326",
            dst_nodata=NODATA,
            resampling=Resampling.nearest,
        )

    visibles = (
        np.isfinite(imagen) & (imagen >= RANGOS[indice][0])
        & (imagen <= RANGOS[indice][1]) & (imagen != NODATA)
    )
    if not visibles.any():
        raise ValueError(f"{indice.upper()}: sin píxeles tras reproyección")

    paradas = ESCALAS[indice]
    posiciones = np.array([v for v, _ in paradas], dtype=np.float32)
    colores = np.array([
        tuple(int(c[i:i+2], 16) for i in (1, 3, 5))
        for _, c in paradas
    ], dtype=np.uint8)
    rgba = np.zeros((alto, ancho, 4), dtype=np.uint8)
    imagen_limpia = np.nan_to_num(
        imagen, nan=float(posiciones[0]), posinf=float(posiciones[-1]),
        neginf=float(posiciones[0]),
    )
    for canal in range(3):
        rgba[:, :, canal] = np.interp(
            np.clip(imagen_limpia, posiciones[0], posiciones[-1]),
            posiciones, colores[:, canal],
        ).astype(np.uint8)
    rgba[:, :, 3] = np.where(visibles, 255, 0).astype(np.uint8)
    Image.fromarray(rgba, "RGBA").save(png_destino)

    oeste, norte = transf.c, transf.f
    este, sur = oeste + transf.a * ancho, norte + transf.e * alto
    return {
        "promedio": round(promedio, 6),
        "pixeles_validos": cantidad,
        "cobertura_parcela": round(cobertura, 4),
        "bounds": [[round(sur, 8), round(oeste, 8)],
                   [round(norte, 8), round(este, 8)]],
        "escala": [{"valor": v, "color": c} for v, c in paradas],
        "escala_tipo": "fija_absoluta",
        "version_analisis": VERSION_ANALISIS,
        "perimetro_recortado": True,
        "perimetro_hash": firma_perimetro(perimetro),
        "resolucion_bandas_metros": 20,
        "nota_calidad": (
            "Incluye SCL 7 (sin clasificar); cobertura completa no garantiza "
            "calidad completa. No usar automáticamente para riego o fertilización."
        ),
    }


def _subir(ruta, origen, content_type):
    with open(origen, "rb") as archivo:
        supabase.storage.from_(BUCKET).upload(
            ruta, archivo.read(),
            {"content-type": content_type, "upsert": "true"},
        )


def _ejecutar_batch_con_reintentos(cubo, origen, indice, fecha):
    """Recupera errores HTTP 429 sin repetir exitos ni ocultar otros errores."""
    esperas = (15, 30, 60, 120, 180)
    for intento in range(len(esperas) + 1):
        try:
            cubo.execute_batch(
                outputfile=str(origen), out_format="GTiff",
                title=f"DELTA {indice.upper()} {fecha}",
            )
            return
        except Exception as error:
            mensaje = str(error).lower()
            limitado = "[429]" in mensaje or "too many requests" in mensaje
            if not limitado or intento >= len(esperas):
                raise
            espera = esperas[intento]
            print(
                f"DELTA {indice.upper()}: Copernicus limito solicitudes (429); "
                f"reintento {intento + 1}/{len(esperas)} en {espera}s",
                flush=True,
            )
            time.sleep(espera)


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    fecha = date.fromisoformat(os.environ["FECHA_ESCENA"].strip()).isoformat()
    publicar = os.getenv("PUBLICAR_AVANZADOS", "false").strip().lower() == "true"
    fin = (date.fromisoformat(fecha) + timedelta(days=1)).isoformat()

    consulta = (
        supabase.table("parcelas").select("perimetro")
        .eq("id", parcela_id).limit(1).execute()
    )
    if not consulta.data or not consulta.data[0].get("perimetro"):
        raise ValueError("Parcela inexistente o sin perímetro")
    perimetro = normalizar_poligono(consulta.data[0]["perimetro"])
    _verificar_fecha_y_perimetro(parcela_id, fecha, firma_perimetro(perimetro))
    zona = obtener_zona(perimetro)
    print(f"DELTA AVANZADOS: parcela {parcela_id}, fecha {fecha}, publicar={publicar}", flush=True)

    with tempfile.TemporaryDirectory() as temp:
        carpeta = Path(temp)
        indices = calcular_indices_avanzados(zona, fecha, fin)
        resultados = {}
        for indice in ("ndre", "msi"):
            origen = carpeta / f"{indice}_origen.tif"
            tif = carpeta / f"{indice}.tif"
            png = carpeta / f"{indice}.png"
            print(f"BATCH {indice.upper()}: iniciando", flush=True)
            _ejecutar_batch_con_reintentos(indices[indice], origen, indice, fecha)
            estadistica = _procesar_mapa(origen, png, tif, perimetro, indice)
            print(
                f"VALIDADO {indice.upper()}: promedio={estadistica['promedio']}, "
                f"cobertura={estadistica['cobertura_parcela']:.2%}", flush=True,
            )
            resultados[indice] = {"tif": tif, "png": png, "estadistica": estadistica}

        # Ningún mapa se publica hasta que ambos superen los controles.
        if not publicar:
            print("PRUEBA COMPLETA: SIN CAMBIOS EN SUPABASE", flush=True)
            return

        ahora = datetime.now(timezone.utc).isoformat()
        for indice in ("ndre", "msi"):
            item = resultados[indice]
            prefijo = f"parcelas/{parcela_id}/{indice}/{fecha}"
            metadata = {
                **item["estadistica"],
                "indice": indice.upper(),
                "parcela_id": parcela_id,
                "fecha_escena_referencia": fecha,
                "ultima_fecha_escena_referencia": fecha,
                "fecha_inicio_periodo": fecha,
                "fecha_fin_periodo": fecha,
                "fecha_procesamiento": ahora,
                "nota": "Índice Sentinel-2 calculado por fecha de referencia; puede haber varias adquisiciones diarias.",
            }
            # Metadatos al final: no se marca disponible hasta tener TIF y PNG.
            _subir(prefijo + ".tif", item["tif"], "image/tiff")
            _subir(prefijo + ".png", item["png"], "image/png")
            archivo_json = carpeta / f"{indice}.json"
            archivo_json.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            _subir(prefijo + ".json", archivo_json, "application/json")
            print(f"PUBLICADO {indice.upper()} {fecha}: {metadata['promedio']}", flush=True)

        print("PUBLICACION MSI/NDRE COMPLETADA", flush=True)


if __name__ == "__main__":
    main()
