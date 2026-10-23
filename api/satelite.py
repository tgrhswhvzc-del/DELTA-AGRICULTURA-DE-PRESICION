
from fastapi import APIRouter, HTTPException
from supabase_client import supabase
from motor_satelital import obtener_zona

router = APIRouter(
    prefix="/satelite",
    tags=["Satelite"]
)


# ESTADO DEL SERVICIO

@router.get("/estado")
def estado_satelite():
    return {
        "servicio": "DELTA Satellite",
        "estado": "funcionando"
    }


# CONSULTAR ZONA DE UNA PARCELA

@router.get("/parcela/{parcela_id}/zona")
def consultar_zona_parcela(parcela_id: str):
    try:
        respuesta = (
            supabase.table("parcelas")
            .select("id,nombre,perimetro")
            .eq("id", parcela_id)
            .limit(1)
            .execute()
        )

        if not respuesta.data:
            raise HTTPException(
                status_code=404,
                detail="Parcela no encontrada"
            )

        parcela = respuesta.data[0]

        if not parcela.get("perimetro"):
            raise HTTPException(
                status_code=404,
                detail="La parcela no tiene perímetro"
            )

        zona = obtener_zona(parcela["perimetro"])

        return {
            "parcela_id": parcela["id"],
            "nombre": parcela["nombre"],
            "zona": zona
        }

    except HTTPException:
        raise

    except ValueError as error:
        print("ERROR DE COORDENADAS:", repr(error))

        raise HTTPException(
            status_code=400,
            detail="Perímetro de parcela inválido"
        )

    except Exception as error:
        print("ERROR SATELITAL:", repr(error))

        raise HTTPException(
            status_code=500,
            detail="Error al consultar la parcela"
        )
