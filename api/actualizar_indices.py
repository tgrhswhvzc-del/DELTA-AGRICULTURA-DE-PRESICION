
"""Worker bajo demanda: una parcela, una fecha,
mapas y promedios coherentes.
"""

import json
import os
import tempfile

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from supabase_client import supabase

from motor_satelital import (
    obtener_zona,
    buscar_imagenes_recientes,
    exportar_ndvi,
    exportar_ndmi,
)

from analisis_parcela import (
    normalizar_poligono,
    procesar_indice,
    VERSION_ANALISIS,
    firma_perimetro,
)

BUCKET = "mapas-satelitales"


def metadata_remota(parcela_id, indice, fecha):
    ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.json"

    try:
        contenido = supabase.storage.from_(BUCKET).download(ruta)
        return json.loads(contenido.decode("utf-8"))
    except Exception:
        return None


def _ya_procesado(parcela_id, fecha, perimetro_hash):
    existente = (
        supabase.table("indices_agricolas")
        .select("ndvi_promedio,ndmi_promedio")
        .eq("parcela_id", parcela_id)
        .eq("fecha_escena_referencia", fecha)
        .limit(1)
        .execute()
    )

    if not existente.data:
        return False

    fila = existente.data[0]

    for indice in ("ndvi", "ndmi"):
        metadata = metadata_remota(
            parcela_id, indice, fecha
        )

        if not metadata:
            return False

        if metadata.get("version_analisis") != VERSION_ANALISIS:
            return False

        if metadata.get("perimetro_recortado") is not True:
            return False

        if metadata.get("perimetro_hash") != perimetro_hash:
            return False

        if abs(
            float(fila[f"{indice}_promedio"])
            - float(metadata["promedio"])
        ) > 0.00001:
            return False

    return True


def _guardar(ruta, archivo, content_type):
    with open(archivo, "rb") as contenido:
        supabase.storage.from_(BUCKET).upload(
            ruta,
            contenido.read(),
            {
                "content-type": content_type,
                "upsert": "true",
            },
        )


def actualizar_parcela(parcela):
    parcela_id = str(parcela["id"])

    perimetro = normalizar_poligono(
        parcela["perimetro"]
    )

    zona = obtener_zona(perimetro)
    perimetro_hash = firma_perimetro(perimetro)

    escenas = buscar_imagenes_recientes(
        zona,
        dias=30,
        nubosidad_max=20,
    )["utiles"]

    if not escenas:
        print(
            "Sin escenas candidatas en los últimos 30 días:",
            parcela_id,
        )
        return

    fechas = list(dict.fromkeys(
        e["fecha"][:10]
        for e in escenas
        if e.get("fecha")
    ))

    for fecha in fechas:
        if _ya_procesado(
            parcela_id, fecha, perimetro_hash
        ):
            print(
                "Observación y mapas vigentes:",
                parcela_id,
                fecha,
            )
            return

        inicio = fecha

        fin = (
            datetime.fromisoformat(fecha).date()
            + timedelta(days=1)
        ).isoformat()

        print(
            "Procesando parcela",
            parcela_id,
            "escena candidata",
            fecha,
            flush=True,
        )

        try:
            with tempfile.TemporaryDirectory() as temporal:
                directorio = Path(temporal)
                resultado = {}

                for indice, exportar in (
                    ("ndvi", exportar_ndvi),
                    ("ndmi", exportar_ndmi),
                ):
                    tif_origen = str(
                        directorio / f"{indice}_origen.tif"
                    )

                    tif_final = str(
                        directorio / f"{indice}.tif"
                    )

                    png_final = str(
                        directorio / f"{indice}.png"
                    )

                    exportar(
                        zona,
                        inicio,
                        fin,
                        tif_origen,
                    )

                    estadistica = procesar_indice(
                        tif_origen,
                        perimetro,
                        indice,
                        png_final,
                        tif_final,
                    )

                    resultado[indice] = {
                        "estadistica": estadistica,
                        "tif": tif_final,
                        "png": png_final,
                    }

                ahora = datetime.now(
                    timezone.utc
                ).isoformat()

                for indice in ("ndvi", "ndmi"):
                    item = resultado[indice]
                    carpeta = f"parcelas/{parcela_id}/{indice}"

                    metadata = {
                        **item["estadistica"],
                        "indice": indice.upper(),
                        "parcela_id": parcela_id,
                        "fecha_escena_referencia": fecha,
                        "ultima_fecha_escena_referencia": fecha,
                        "fecha_inicio_periodo": inicio,
                        "fecha_fin_periodo": inicio,
                        "fecha_procesamiento": ahora,
                        "nota": (
                            "Fecha de referencia STAC. "
                            "Puede haber más de una adquisición "
                            "del mismo día."
                        ),
                    }

                    _guardar(
                        f"{carpeta}/{fecha}.tif",
                        item["tif"],
                        "image/tiff",
                    )

                    _guardar(
                        f"{carpeta}/{fecha}.png",
                        item["png"],
                        "image/png",
                    )

                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        suffix=".json",
                        encoding="utf-8",
                        delete=False,
                    ) as f:
                        json.dump(
                            metadata,
                            f,
                            ensure_ascii=False,
                        )
                        ruta_temporal = f.name

                    try:
                        _guardar(
                            f"{carpeta}/{fecha}.json",
                            ruta_temporal,
                            "application/json",
                        )
                    finally:
                        os.unlink(ruta_temporal)

                supabase.table("indices_agricolas").upsert(
                    {
                        "parcela_id": parcela_id,
                        "fecha_escena_referencia": fecha,
                        "fecha_procesamiento": ahora,
                        "ndvi_promedio": resultado["ndvi"][
                            "estadistica"
                        ]["promedio"],
                        "ndmi_promedio": resultado["ndmi"][
                            "estadistica"
                        ]["promedio"],
                    },
                    on_conflict=(
                        "parcela_id,fecha_escena_referencia"
                    ),
                ).execute()

                print(
                    "PUBLICADO",
                    parcela_id,
                    fecha,
                    "NDVI",
                    resultado["ndvi"]["estadistica"]["promedio"],
                    "NDMI",
                    resultado["ndmi"]["estadistica"]["promedio"],
                    flush=True,
                )
                return

        except ValueError as error:
            print(
                "Escena descartada:",
                fecha,
                str(error),
                flush=True,
            )
            continue

    print(
        "Ninguna escena válida para la parcela:",
        parcela_id,
    )


def main():
    parcela_id = os.getenv(
        "PARCELA_ID", ""
    ).strip()

    if not parcela_id:
        raise ValueError(
            "Falta PARCELA_ID. No se procesará ninguna parcela."
        )

    try:
        parcela_id = str(UUID(parcela_id))
    except ValueError as error:
        raise ValueError(
            "PARCELA_ID debe ser un UUID válido"
        ) from error

    respuesta = (
        supabase.table("parcelas")
        .select("id,perimetro")
        .eq("id", parcela_id)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        raise ValueError("Parcela no encontrada")

    if not respuesta.data[0].get("perimetro"):
        raise ValueError("La parcela no tiene perímetro")

    actualizar_parcela(respuesta.data[0])


if __name__ == "__main__":
    main()
