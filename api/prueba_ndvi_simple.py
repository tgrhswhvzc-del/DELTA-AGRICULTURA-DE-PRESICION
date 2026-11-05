"""Prueba aislada NDVI para diagnosticar Copernicus. No publica datos."""
import os
import time
from uuid import UUID

from supabase_client import supabase
from motor_satelital import conectar_copernicus, obtener_zona


def main():
    parcela_id = str(UUID(os.environ["PARCELA_ID"].strip()))
    respuesta = (
        supabase.table("parcelas")
        .select("perimetro")
        .eq("id", parcela_id)
        .limit(1)
        .execute()
    )
    if not respuesta.data or not respuesta.data[0].get("perimetro"):
        raise ValueError("Parcela inexistente o sin perimetro")

    from analisis_parcela import normalizar_poligono
    perimetro = normalizar_poligono(respuesta.data[0]["perimetro"])
    zona = obtener_zona(perimetro)
    print(f"PRUEBA AISLADA: parcela {parcela_id}, zona {zona}", flush=True)

    conexion = conectar_copernicus()
    print("Conectado a Copernicus", flush=True)

    cubo = conexion.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=zona,
        temporal_extent=["2026-11-03", "2026-11-04"],
        bands=["B04", "B08"],
        max_cloud_cover=20,
    )
    rojo = cubo.band("B04")
    nir = cubo.band("B08")
    ndvi = ((nir - rojo) / (nir + rojo)).reduce_dimension(
        dimension="t", reducer="median"
    )

    print("Creando trabajo remoto NDVI basico (sin SCL ni reproyeccion)", flush=True)
    job = ndvi.save_result(format="GTiff").create_job(title="DELTA prueba NDVI basico")
    print(f"ID TRABAJO COPERNICUS: {job.job_id}", flush=True)
    job.start()
    print("Trabajo iniciado", flush=True)

    limite = time.monotonic() + 180
    while True:
        estado = job.status()
        print(f"ESTADO COPERNICUS: {estado}", flush=True)
        if estado == "finished":
            print("PRUEBA COMPLETADA: NDVI basico calculado", flush=True)
            return
        if estado in ("error", "canceled"):
            try:
                print("LOGS COPERNICUS:", job.logs(), flush=True)
            except Exception as e:
                print("No se pudieron consultar logs:", repr(e), flush=True)
            raise RuntimeError(f"Copernicus termino en estado: {estado}")
        if time.monotonic() >= limite:
            print("DIAGNOSTICO: Copernicus no termino en 3 minutos.", flush=True)
            try:
                job.stop()
                print("Trabajo de prueba detenido", flush=True)
            except Exception as e:
                print("No se pudo detener el trabajo:", repr(e), flush=True)
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
