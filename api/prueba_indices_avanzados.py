"""Prueba manual MSI/NDRE: genera GeoTIFF/PNG locales y un reporte, sin publicar.

Entradas: PARCELA_ID, FECHA_ESCENA (YYYY-MM-DD).
"""
import os
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom

from supabase_client import supabase
from motor_satelital import obtener_zona
from analisis_parcela import normalizar_poligono
from indices_avanzados_experimental import calcular_indices_avanzados

PALETAS = {
    "ndre": [(-1.0, "#a52a2a"), (-0.2, "#e57942"), (0.2, "#f5d864"),
             (0.5, "#66aa4b"), (1.0, "#0c512c")],
    "msi": [(0.0, "#0c512c"), (0.5, "#66aa4b"), (1.0, "#ffeb00"),
            (1.5, "#ff6900"), (3.0, "#c30019")],
}
RANGOS = {"ndre": (-1.0, 1.0), "msi": (0.0, 5.0)}


def revisar_y_crear_vista(tif, png, poligono, indice):
    with rasterio.open(tif) as src:
        if src.count != 1 or src.crs is None:
            raise ValueError(f"{indice}: GeoTIFF no válido")
        datos = src.read(1, masked=True)
        valores = np.asarray(datos.data, dtype=np.float32)
        dentro = geometry_mask(
            [transform_geom("EPSG:4326", src.crs, poligono)],
            out_shape=valores.shape,
            transform=src.transform,
            invert=True,
            all_touched=False,
        )
        minimo, maximo = RANGOS[indice]
        validos = (
            dentro & ~np.ma.getmaskarray(datos) & np.isfinite(valores)
            & (valores >= minimo) & (valores <= maximo)
        )
        if not np.any(validos):
            raise ValueError(f"{indice}: sin píxeles válidos dentro de la parcela")

        paleta = PALETAS[indice]
        posiciones = np.array([p for p, _ in paleta])
        colores = np.array([
            tuple(int(c[i:i+2], 16) for i in (1, 3, 5)) for _, c in paleta
        ])
        rgba = np.zeros((*valores.shape, 4), dtype=np.uint8)
        for i in range(3):
            rgba[:, :, i] = np.interp(
                np.clip(np.nan_to_num(valores, nan=minimo, posinf=maximo, neginf=minimo),
                        posiciones[0], posiciones[-1]),
                posiciones, colores[:, i],
            ).astype(np.uint8)
        rgba[:, :, 3] = np.where(validos, 255, 0).astype(np.uint8)
        Image.fromarray(rgba, "RGBA").save(png)
        cobertura = int(validos.sum()) / max(int(dentro.sum()), 1)
        promedio = float(np.mean(valores[validos], dtype=np.float64))
        print(
            f"VALIDADO {indice.upper()}: promedio={promedio:.6f}, "
            f"cobertura={cobertura:.2%}, "
            f"min={float(valores[validos].min()):.3f}, "
            f"max={float(valores[validos].max()):.3f}", flush=True,
        )


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    fecha = date.fromisoformat(os.environ["FECHA_ESCENA"].strip())
    inicio, fin = fecha.isoformat(), (fecha + timedelta(days=1)).isoformat()
    respuesta = (
        supabase.table("parcelas").select("perimetro")
        .eq("id", parcela_id).limit(1).execute()
    )
    if not respuesta.data or not respuesta.data[0].get("perimetro"):
        raise ValueError("Parcela inexistente o sin perímetro")
    perimetro = normalizar_poligono(respuesta.data[0]["perimetro"])
    zona = obtener_zona(perimetro)
    print(f"PRUEBA MSI/NDRE: parcela={parcela_id} fecha={inicio} zona={zona}", flush=True)
    indices = calcular_indices_avanzados(zona, inicio, fin)
    salida = Path("artefactos/avanzados")
    salida.mkdir(parents=True, exist_ok=True)

    # Trabajos individuales para saber qué índice falla y controlar su coste.
    for indice in ("ndre", "msi"):
        tif = salida / f"{indice}.tif"
        png = salida / f"{indice}.png"
        print(f"INICIANDO BATCH {indice.upper()}", flush=True)
        indices[indice].execute_batch(
            outputfile=str(tif), out_format="GTiff",
            title=f"DELTA prueba {indice.upper()} {inicio}",
        )
        print(f"DESCARGADO {indice.upper()}", flush=True)
        revisar_y_crear_vista(tif, png, perimetro, indice)
    print("PRUEBA COMPLETADA. NO SE MODIFICO SUPABASE.", flush=True)


if __name__ == "__main__":
    main()
