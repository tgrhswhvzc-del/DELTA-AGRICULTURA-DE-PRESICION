
import os
import secrets
import tempfile
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header, Query, Depends

from supabase_client import supabase
from motor_satelital import (
    obtener_zona,
    conectar_copernicus,
    exportar_ndmi
)
from seguridad_satelital import verificar_propietario


router = APIRouter(
    prefix="/satelite",
    tags=["Satelite"]
)


@router.get("/estado")
def estado_satelite():
    return {
        "servicio": "DELTA Satellite",
        "estado": "funcionando"
    }


@router.get("/parcela/{parcela_id}/zona")
def consultar_zona_parcela(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario)
):
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

        return {
            "parcela_id": parcela["id"],
            "nombre": parcela["nombre"],
            "zona": obtener_zona(parcela["perimetro"])
        }

    except HTTPException:
        raise

    except ValueError:
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


@router.get(
    "/diagnostico-copernicus",
    include_in_schema=False
)
def diagnostico_copernicus(
    x_delta_diagnostic_key: str = Header(default="")
):
    clave = os.getenv("DELTA_DIAGNOSTIC_KEY")

    if not clave or not secrets.compare_digest(
        x_delta_diagnostic_key, clave
    ):
        raise HTTPException(
            status_code=404,
            detail="No encontrado"
        )

    try:
        conectar_copernicus()

        return {
            "copernicus": "conectado"
        }

    except Exception as error:
        print("ERROR COPERNICUS:", repr(error))

        raise HTTPException(
            status_code=503,
            detail="No se pudo conectar con Copernicus"
        )


@router.post("/parcela/{parcela_id}/ndmi")
def generar_ndmi(
    parcela_id: str,
    fecha_inicio: date = Query(
        default_factory=lambda: date.today() - timedelta(days=30)
    ),
    fecha_fin: date = Query(
        default_factory=date.today
    ),
    usuario_id: str = Depends(verificar_propietario)
):
    if fecha_inicio > fecha_fin:
        raise HTTPException(
            status_code=400,
            detail="Rango de fechas inválido"
        )

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
                status_code=400,
                detail="La parcela no tiene perímetro"
            )

        zona = obtener_zona(
            parcela["perimetro"]
        )

        with tempfile.TemporaryDirectory() as temporal:
            archivo = str(
                Path(temporal) / "ndmi.tif"
            )

            exportar_ndmi(
                zona,
                fecha_inicio.isoformat(),
                fecha_fin.isoformat(),
                archivo
            )

            ruta_storage = (
                f"parcelas/{parcela_id}/ndmi/"
                f"{fecha_inicio.isoformat()}_"
                f"{fecha_fin.isoformat()}.tif"
            )

            with open(archivo, "rb") as imagen:
                supabase.storage.from_(
                    "mapas-satelitales"
                ).upload(
                    ruta_storage,
                    imagen.read(),
                    {
                        "content-type": "image/tiff",
                        "upsert": "true"
                    }
                )

        return {
            "parcela_id": parcela_id,
            "nombre": parcela["nombre"],
            "indice": "NDMI",
            "fecha_inicio": fecha_inicio.isoformat(),
            "fecha_fin": fecha_fin.isoformat(),
            "archivo": ruta_storage,
            "almacenamiento": "Supabase Storage privado"
        }

    except HTTPException:
        raise

    except Exception as error:
        print("ERROR GENERANDO NDMI:", repr(error))

        raise HTTPException(
            status_code=500,
            detail="No se pudo generar y guardar el mapa NDMI"
        )


@router.get("/parcela/{parcela_id}/ndmi/url")
def obtener_url_ndmi(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario)
):
    try:
        respuesta = (
            supabase.table("parcelas")
            .select("id")
            .eq("id", parcela_id)
            .limit(1)
            .execute()
        )

        if not respuesta.data:
            raise HTTPException(
                status_code=404,
                detail="Parcela no encontrada"
            )

        archivos = supabase.storage.from_(
            "mapas-satelitales"
        ).list(
            f"parcelas/{parcela_id}/ndmi"
        )

        if not archivos:
            raise HTTPException(
                status_code=404,
                detail="No hay mapas NDMI para esta parcela"
            )

        archivos_tif = sorted(
            [
                a["name"]
                for a in archivos
                if a["name"].endswith(".tif")
            ],
            reverse=True
        )

        if not archivos_tif:
            raise HTTPException(
                status_code=404,
                detail="No se encontraron mapas NDMI"
            )

        nombre_archivo = archivos_tif[0]

        ruta = (
            f"parcelas/{parcela_id}/ndmi/"
            f"{nombre_archivo}"
        )

        resultado = supabase.storage.from_(
            "mapas-satelitales"
        ).create_signed_url(
            ruta,
            3600
        )

        url = (
            resultado.get("signedURL")
            or resultado.get("signedUrl")
        )

        if not url:
            raise HTTPException(
                status_code=500,
                detail="No se pudo generar el enlace"
            )

        return {
            "parcela_id": parcela_id,
            "archivo": nombre_archivo,
            "url": url,
            "expira_en_segundos": 3600
        }

    except HTTPException:
        raise

    except Exception as error:
        print("ERROR URL NDMI:", repr(error))

        raise HTTPException(
            status_code=500,
            detail="No se pudo generar la URL del mapa NDMI"
        )
