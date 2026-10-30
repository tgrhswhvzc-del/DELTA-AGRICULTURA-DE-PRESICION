
"""Actualización automática de parcelas DELTA."""

from datetime import datetime, timezone
from uuid import UUID

from supabase_client import supabase
from actualizar_indices import actualizar_parcela

LOTE = 5
PAGINA = 500


def cargar_parcelas():
    parcelas = []
    desde = 0

    while True:
        respuesta = (
            supabase.table("parcelas")
            .select("id,perimetro")
            .order("id")
            .range(desde, desde + PAGINA - 1)
            .execute()
        )

        bloque = respuesta.data or []

        parcelas.extend(
            p for p in bloque if p.get("perimetro")
        )

        if len(bloque) < PAGINA:
            break

        desde += PAGINA

    return parcelas


def seleccionar_lote(parcelas):
    if not parcelas:
        return []

    ahora = datetime.now(timezone.utc)
    turno = int(ahora.timestamp() // (6 * 3600))
    inicio = (turno * LOTE) % len(parcelas)
    cantidad = min(LOTE, len(parcelas))

    return [
        parcelas[(inicio + i) % len(parcelas)]
        for i in range(cantidad)
    ]


def main():
    parcelas = cargar_parcelas()
    lote = seleccionar_lote(parcelas)

    print(
        f"DELTA: {len(parcelas)} parcelas registradas; "
        f"revisando {len(lote)}.",
        flush=True
    )

    errores = []

    for parcela in lote:
        parcela_id = str(parcela["id"])

        try:
            UUID(parcela_id)

            print(
                f"Revisando parcela: {parcela_id}",
                flush=True
            )

            actualizar_parcela(parcela)

        except Exception as error:
            errores.append(parcela_id)

            print(
                f"ERROR {parcela_id}: {error}",
                flush=True
            )

    if errores:
        raise SystemExit(
            f"Fallaron {len(errores)} parcelas: "
            + ", ".join(errores)
        )

    print(
        "Revisión programada completada.",
        flush=True
    )


if __name__ == "__main__":
    main()
