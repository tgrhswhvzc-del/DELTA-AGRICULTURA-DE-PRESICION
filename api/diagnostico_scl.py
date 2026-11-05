"""Diagnóstico sin escrituras: clases SCL en los huecos del NDVI de DELTA.

No altera mapas existentes, ni escribe en Supabase; descarga datos a carpeta temporal.
La SCL corresponde a la primera observación de la fecha elegida. Si hubo
múltiples observaciones del mismo día, el diagnóstico es aproximado.
"""
import os
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import reproject, Resampling, transform_geom

from supabase_client import supabase
from analisis_parcela import normalizar_poligono
from motor_satelital import conectar_copernicus, obtener_proyeccion_utm, obtener_zona

BUCKET = "mapas-satelitales"
CLASES_SCL = {
    0: "Sin datos", 1: "Saturado/defectuoso", 2: "Zona oscura/sombra",
    3: "Sombra de nube", 4: "Vegetacion", 5: "Suelo descubierto",
    6: "Agua", 7: "Sin clasificar", 8: "Nube probabilidad media",
    9: "Nube probabilidad alta", 10: "Cirros", 11: "Nieve/hielo",
}


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    fecha = date.fromisoformat(os.environ["FECHA_ESCENA"].strip())
    inicio = fecha.isoformat()
    fin = (fecha + timedelta(days=1)).isoformat()

    respuesta = (
        supabase.table("parcelas").select("perimetro")
        .eq("id", parcela_id).limit(1).execute()
    )
    if not respuesta.data or not respuesta.data[0].get("perimetro"):
        raise ValueError("No existe la parcela o no tiene perimetro")

    perimetro = normalizar_poligono(respuesta.data[0]["perimetro"])
    zona = obtener_zona(perimetro)
    print(f"DIAGNOSTICO SCL: parcela {parcela_id}, fecha {inicio}", flush=True)
    print(f"ZONA: {zona}", flush=True)

    conexion = conectar_copernicus()
    scl = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=[inicio, fin],
        bands=["SCL"],
        max_cloud_cover=20,
    )
    scl = scl.resample_spatial(
        resolution=10,
        projection=obtener_proyeccion_utm(zona),
        method="near",
    )
    # Primera observación del día: no promediar códigos categóricos SCL.
    scl = scl.reduce_dimension(dimension="t", reducer="first")

    with tempfile.TemporaryDirectory() as temporal:
        carpeta = Path(temporal)
        scl_local = carpeta / "scl.tif"
        ndvi_local = carpeta / "ndvi.tif"

        print("Procesando SCL en Copernicus (batch)...", flush=True)
        scl.execute_batch(
            outputfile=str(scl_local),
            out_format="GTiff",
            title="DELTA diagnostico SCL - solo lectura",
        )
        print("SCL descargada", flush=True)

        ruta = f"parcelas/{parcela_id}/ndvi/{inicio}.tif"
        print(f"Descargando NDVI existente: {ruta}", flush=True)
        ndvi_local.write_bytes(supabase.storage.from_(BUCKET).download(ruta))

        with rasterio.open(ndvi_local) as ndvi_src:
            matriz = ndvi_src.read(1)
            geom = transform_geom("EPSG:4326", ndvi_src.crs, perimetro)
            adentro = geometry_mask(
                [geom], out_shape=matriz.shape,
                transform=ndvi_src.transform, invert=True, all_touched=False
            )
            validos = adentro & np.isfinite(matriz) & (matriz >= -1) & (matriz <= 1)
            if ndvi_src.nodata is not None:
                validos &= matriz != ndvi_src.nodata
            huecos = adentro & ~validos
            destino_scl = np.full(matriz.shape, np.nan, dtype=np.float32)

            with rasterio.open(scl_local) as scl_src:
                reproject(
                    source=rasterio.band(scl_src, 1),
                    destination=destino_scl,
                    src_transform=scl_src.transform,
                    src_crs=scl_src.crs,
                    src_nodata=scl_src.nodata,
                    dst_transform=ndvi_src.transform,
                    dst_crs=ndvi_src.crs,
                    dst_nodata=np.nan,
                    resampling=Resampling.nearest,
                )

        total = int(adentro.sum())
        faltan = int(huecos.sum())
        print(f"PIXeles dentro del perimetro: {total}", flush=True)
        print(f"PIXeles NDVI validos: {int(validos.sum())}", flush=True)
        print(f"HUECOS NDVI: {faltan} ({100 * faltan / total:.2f}%)", flush=True)
        if faltan == 0:
            print("No hay huecos en este GeoTIFF", flush=True)
            return
        redondeados = np.where(np.isfinite(destino_scl), np.rint(destino_scl), -1).astype(int)
        conteo = Counter(redondeados[huecos].tolist())
        print("CLASES SCL DENTRO DE LOS HUECOS:", flush=True)
        for codigo, cantidad in conteo.most_common():
            nombre = CLASES_SCL.get(codigo, "No disponible" if codigo == -1 else "Otra")
            print(f"  {codigo}: {nombre}: {cantidad} pixeles ({100 * cantidad / faltan:.2f}%)", flush=True)
        permitidos = sum(conteo[c] for c in (4, 5))
        print(f"HUECOS clasificados 4 o 5 por SCL de primera observacion: {permitidos}", flush=True)
        print("NOTA: si hay varias capturas del dia, la SCL de primera observacion es orientativa.", flush=True)
        print("DIAGNOSTICO TERMINADO. No se modifico Supabase.", flush=True)


if __name__ == "__main__":
    main()
