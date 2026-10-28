
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

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

    # Buscar la observación más reciente no procesada.
    escena_nueva = None

    for escena in escenas:
        fecha = escena["fecha"][:10]

        existente = (
            supabase.table("indices_agricolas")
            .select("id")
            .eq("parcela_id", parcela_id)
            .eq("fecha_escena_referencia", fecha)
            .limit(1)
            .execute()
        )

        if existente.data:
            print("Última observación ya registrada:", parcela_id)
            return

        escena_nueva = escena
        break

    if not escena_nueva:
        return

    fecha_escena = escena_nueva["fecha"][:10]

    # Procesar únicamente el día de la escena seleccionada.
    fecha_inicio = datetime.fromisoformat(fecha_escena).date()
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
    respuesta = (
        supabase.table("parcelas")
        .select("id,perimetro")
        .not_.is_("perimetro", "null")
        .limit(20)
        .execute()
    )

    parcelas = respuesta.data or []

    print("Parcelas encontradas:", len(parcelas))

    for parcela in parcelas:
        try:
            actualizar_parcela(parcela)

        except Exception as error:
            print(
                "Error procesando parcela:",
                parcela["id"],
                repr(error),
            )


if __name__ == "__main__":
    main()
