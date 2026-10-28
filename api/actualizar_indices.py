
import os
import tempfile

from uuid import UUID
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import tifffile

from supabase_client import supabase
from motor_satelital import (
    obtener_zona,
    buscar_imagenes_recientes,
    exportar_ndvi,
    exportar_ndmi,
)


def promedio_indice(archivo):
    datos = np.squeeze(
        tifffile.imread(archivo).astype(np.float32)
    )

    validos = (
        np.isfinite(datos)
        & (datos >= -1)
        & (datos <= 1)
    )

    if not np.any(validos):
        raise ValueError("No hay píxeles válidos")

    return float(np.mean(datos[validos]))


def actualizar_parcela(parcela):
    parcela_id = parcela["id"]
    zona = obtener_zona(parcela["perimetro"])

    resultado = buscar_imagenes_recientes(
        zona,
        dias=30,
        nubosidad_max=20,
    )

    escenas = resultado["utiles"]

    if not escenas:
        print("Sin imágenes útiles:", parcela_id)
        return

    # Usar únicamente la escena más reciente.
    escena = escenas[0]
    fecha_escena = escena["fecha"][:10]

    existente = (
        supabase.table("indices_agricolas")
        .select("id")
        .eq("parcela_id", parcela_id)
        .eq("fecha_escena_referencia", fecha_escena)
        .limit(1)
        .execute()
    )

    if existente.data:
        print(
            "Índices ya actualizados:",
            parcela_id,
            fecha_escena,
        )
        return

    fecha_inicio = datetime.fromisoformat(
        fecha_escena
    ).date()

    fecha_fin = fecha_inicio + timedelta(days=1)

    print("Procesando parcela:", parcela_id)
    print("Escena de referencia:", fecha_escena)

    with tempfile.TemporaryDirectory() as carpeta:
        archivo_ndvi = str(Path(carpeta) / "ndvi.tif")
        archivo_ndmi = str(Path(carpeta) / "ndmi.tif")

        exportar_ndvi(
            zona,
            fecha_inicio.isoformat(),
            fecha_fin.isoformat(),
            archivo_ndvi,
        )

        exportar_ndmi(
            zona,
            fecha_inicio.isoformat(),
            fecha_fin.isoformat(),
            archivo_ndmi,
        )

        ndvi = promedio_indice(archivo_ndvi)
        ndmi = promedio_indice(archivo_ndmi)

    supabase.table("indices_agricolas").upsert(
        {
            "parcela_id": parcela_id,
            "fecha_escena_referencia": fecha_escena,
            "fecha_procesamiento": datetime.now(
                timezone.utc
            ).isoformat(),
            "ndvi_promedio": ndvi,
            "ndmi_promedio": ndmi,
        },
        on_conflict="parcela_id,fecha_escena_referencia",
    ).execute()

    print(
        "Actualización completada:",
        parcela_id,
        "NDVI:", round(ndvi, 4),
        "NDMI:", round(ndmi, 4),
    )


def main():
    parcela_id = os.getenv("PARCELA_ID", "").strip()

    if not parcela_id:
        raise ValueError(
            "Falta PARCELA_ID. No se procesará ninguna parcela."
        )

    try:
        parcela_id = str(UUID(parcela_id))
    except ValueError:
        raise ValueError("PARCELA_ID no es un UUID válido")

    respuesta = (
        supabase.table("parcelas")
        .select("id,perimetro")
        .eq("id", parcela_id)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        raise ValueError("Parcela no encontrada")

    parcela = respuesta.data[0]

    if not parcela.get("perimetro"):
        raise ValueError("La parcela no tiene perímetro")

    actualizar_parcela(parcela)


if __name__ == "__main__":
    main()
