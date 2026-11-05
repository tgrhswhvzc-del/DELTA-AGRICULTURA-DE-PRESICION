"""Diagnostico sin escrituras: NDVI original DELTA en batch (con SCL y reproyeccion)."""
import os
import time
from uuid import UUID

from supabase_client import supabase
from analisis_parcela import normalizar_poligono
from motor_satelital import obtener_zona, calcular_indices

FECHA_INICIO = "2026-11-03"
FECHA_FIN = "2026-11-04"
ESPERA_MAX_SEG = 240


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
    perimetro = normalizar_poligono(respuesta.data[0]["perimetro"])
    zona = obtener_zona(perimetro)
    print(f"NDVI COMPLETO: parcela {parcela_id}, zona {zona}", flush=True)
    print("Construyendo calculo ORIGINAL con SCL y reproyeccion a 10 m", flush=True)
    t0 = time.monotonic()
    ndvi = calcular_indices(zona, FECHA_INICIO, FECHA_FIN)["ndvi"]
    print(f"Grafo construido en {time.monotonic()-t0:.1f} s", flush=True)

    job = ndvi.save_result(format="GTiff").create_job(
        title="DELTA diagnostico NDVI completo original"
    )
    print(f"ID TRABAJO COPERNICUS: {job.job_id}", flush=True)
    job.start()
    print("Trabajo remoto iniciado", flush=True)
    limite = time.monotonic() + ESPERA_MAX_SEG

    while True:
        estado = job.status()
        print(f"ESTADO: {estado}; tiempo {time.monotonic()-t0:.1f} s", flush=True)
        if estado == "finished":
            print("EXITO: NDVI ORIGINAL (SCL + reproyeccion) terminado en batch", flush=True)
            print("No se publica ningun archivo en Supabase", flush=True)
            return
        if estado in ("error", "canceled", "cancelled"):
            try:
                print("LOGS:", job.logs(), flush=True)
            except Exception as exc:
                print("No se pudieron leer logs:", repr(exc), flush=True)
            raise RuntimeError(f"Trabajo de Copernicus: {estado}")
        if time.monotonic() >= limite:
            print("DIAGNOSTICO: NDVI completo no termino en cuatro minutos", flush=True)
            try:
                job.stop()
                print("Trabajo de prueba detenido", flush=True)
            except Exception as exc:
                print("No se pudo detener:", repr(exc), flush=True)
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
