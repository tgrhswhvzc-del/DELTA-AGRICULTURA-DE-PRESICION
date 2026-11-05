
import os
import json
import requests
import secrets
import tempfile

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

from fastapi import APIRouter, HTTPException, Header, Query, Depends

from supabase_client import supabase
from analisis_parcela import firma_perimetro

from motor_satelital import (
    obtener_zona,
    conectar_copernicus,
    exportar_ndmi,
    crear_png_ndmi,
    exportar_ndvi,
    exportar_rgb_reciente,
    crear_png_rgb,
)

from seguridad_satelital import verificar_propietario


router = APIRouter(prefix="/satelite", tags=["Satelite"])

BUCKET = "mapas-satelitales"
STAC_URL = "https://stac.dataspace.copernicus.eu/v1/search"


# =====================================================
# DELTA V2: VERIFICACIÓN DE MAPAS Y PROMEDIOS
# =====================================================

def _metadatos_v2(parcela_id, indice, fecha, perimetro_hash):
    ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.json"

    try:
        contenido = supabase.storage.from_(BUCKET).download(ruta)
        data = json.loads(contenido.decode("utf-8"))

        if (
            data.get("version_analisis") == 2
            and data.get("perimetro_recortado") is True
            and data.get("perimetro_hash") == perimetro_hash
            and data.get("escala_tipo") == "fija_absoluta"
            and data.get("fecha_escena_referencia") == fecha
            and isinstance(data.get("bounds"), list)
            and len(data["bounds"]) == 2
        ):
            return data

    except Exception as error:
        print("DELTA - Error verificando mapa:", repr(error))

    return None


def _ultimo_paquete_v2(parcela_id):
    parcela = (
        supabase.table("parcelas")
        .select("perimetro")
        .eq("id", parcela_id)
        .limit(1)
        .execute()
    )

    if not parcela.data or not parcela.data[0].get("perimetro"):
        return None, None

    perimetro_hash = firma_perimetro(
        parcela.data[0]["perimetro"]
    )

    registros = (
        supabase.table("indices_agricolas")
        .select(
            "fecha_escena_referencia,"
            "fecha_procesamiento,"
            "ndvi_promedio,"
            "ndmi_promedio"
        )
        .eq("parcela_id", parcela_id)
        .order("fecha_escena_referencia", desc=True)
        .limit(30)
        .execute()
    ).data or []

    for registro in registros:
        fecha = str(
            registro["fecha_escena_referencia"]
        )[:10]

        ndvi = _metadatos_v2(
            parcela_id,
            "ndvi",
            fecha,
            perimetro_hash
        )

        ndmi = _metadatos_v2(
            parcela_id,
            "ndmi",
            fecha,
            perimetro_hash
        )

        if not ndvi or not ndmi:
            continue

        if (
            abs(
                float(registro["ndvi_promedio"])
                - float(ndvi["promedio"])
            ) > 0.00001
            or abs(
                float(registro["ndmi_promedio"])
                - float(ndmi["promedio"])
            ) > 0.00001
        ):
            continue

        return registro, {
            "ndvi": ndvi,
            "ndmi": ndmi
        }

    return None, None


def _url_indice_v2(parcela_id, indice):
    registro, mapas = _ultimo_paquete_v2(parcela_id)

    if registro is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Mapa coherente no disponible todavía. "
                "DELTA lo actualizará al abrir la parcela."
            ),
        )

    fecha = str(
        registro["fecha_escena_referencia"]
    )[:10]

    ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.png"

    firmado = (
        supabase.storage.from_(BUCKET)
        .create_signed_url(ruta, 3600)
    )

    url = (
        firmado.get("signedURL")
        or firmado.get("signedUrl")
    )

    if not url:
        raise HTTPException(
            status_code=503,
            detail="No se pudo firmar el mapa"
        )

    return {
        **mapas[indice],
        "parcela_id": parcela_id,
        "indice": indice.upper(),
        "archivo": f"{fecha}.png",
        "formato": "PNG",
        "url": url,
        "expira_en_segundos": 3600,
    }


# =====================================================
# CONSULTA DE ESCENAS SATELITALES
# =====================================================

def consultar_escenas_referencia(
    zona,
    fecha_inicio,
    fecha_fin
):
    consulta = {
        "collections": ["sentinel-2-l2a"],
        "bbox": [
            zona["west"],
            zona["south"],
            zona["east"],
            zona["north"],
        ],
        "datetime": (
            f"{fecha_inicio}T00:00:00Z/"
            f"{fecha_fin}T23:59:59Z"
        ),
        "limit": 100,
    }

    try:
        respuesta = requests.post(
            STAC_URL,
            json=consulta,
            timeout=20,
        )

        respuesta.raise_for_status()

        escenas = respuesta.json().get(
            "features", []
        )

        fechas = sorted({
            imagen.get(
                "properties", {}
            ).get("datetime", "")[:10]

            for imagen in escenas

            if imagen.get(
                "properties", {}
            ).get("datetime")

            and imagen.get(
                "properties", {}
            ).get("eo:cloud_cover") is not None

            and imagen[
                "properties"
            ]["eo:cloud_cover"] <= 20
        })

        return fechas, len(escenas) >= 100

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        print("ADVERTENCIA STAC:", repr(error))
        return [], False


# =====================================================
# METADATOS NDMI
# =====================================================

def guardar_metadata_ndmi(ruta, metadata):
    contenido = json.dumps(
        metadata,
        ensure_ascii=False
    ).encode("utf-8")

    supabase.storage.from_(BUCKET).upload(
        ruta,
        contenido,
        {
            "content-type": "application/json",
            "upsert": "true",
        },
    )


def leer_metadata_ndmi(ruta):
    try:
        contenido = (
            supabase.storage.from_(BUCKET)
            .download(ruta)
        )

        return json.loads(
            contenido.decode("utf-8")
        )

    except Exception as error:
        print(
            "ADVERTENCIA METADATA NDMI:",
            repr(error)
        )
        return None


# =====================================================
# ESTADO DEL SERVICIO
# =====================================================

@router.get("/estado")
def estado_satelite():
    return {
        "servicio": "DELTA Satellite",
        "estado": "funcionando",
    }


# =====================================================
# CONSULTAR ZONA DE UNA PARCELA
# =====================================================

@router.get("/parcela/{parcela_id}/zona")
def consultar_zona_parcela(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario),
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
                detail="Parcela no encontrada",
            )

        parcela = respuesta.data[0]

        if not parcela.get("perimetro"):
            raise HTTPException(
                status_code=404,
                detail="La parcela no tiene perímetro",
            )

        return {
            "parcela_id": parcela["id"],
            "nombre": parcela["nombre"],
            "zona": obtener_zona(
                parcela["perimetro"]
            ),
        }

    except HTTPException:
        raise

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Perímetro de parcela inválido",
        )

    except Exception as error:
        print("ERROR SATELITAL:", repr(error))

        raise HTTPException(
            status_code=500,
            detail="Error al consultar la parcela",
        )


# =====================================================
# DIAGNÓSTICO PRIVADO DE COPERNICUS
# =====================================================

@router.get(
    "/diagnostico-copernicus",
    include_in_schema=False
)
def diagnostico_copernicus(
    x_delta_diagnostic_key: str = Header(default=""),
):
    clave = os.getenv("DELTA_DIAGNOSTIC_KEY")

    if (
        not clave
        or not secrets.compare_digest(
            x_delta_diagnostic_key,
            clave
        )
    ):
        raise HTTPException(
            status_code=404,
            detail="No encontrado",
        )

    try:
        conectar_copernicus()

        return {
            "copernicus": "conectado",
        }

    except Exception as error:
        print(
            "ERROR COPERNICUS:",
            repr(error)
        )

        raise HTTPException(
            status_code=503,
            detail="No se pudo conectar con Copernicus",
        )


# =====================================================
# NDMI - GENERAR
# =====================================================

@router.post("/parcela/{parcela_id}/ndmi")
def generar_ndmi(
    parcela_id: str,

    fecha_inicio: date = Query(
        default_factory=lambda: (
            date.today() - timedelta(days=30)
        )
    ),

    fecha_fin: date = Query(
        default_factory=date.today
    ),

    usuario_id: str = Depends(verificar_propietario),
):
    if fecha_inicio > fecha_fin:
        raise HTTPException(
            status_code=400,
            detail="Rango de fechas inválido",
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
                detail="Parcela no encontrada",
            )

        parcela = respuesta.data[0]

        if not parcela.get("perimetro"):
            raise HTTPException(
                status_code=400,
                detail="La parcela no tiene perímetro",
            )

        zona = obtener_zona(
            parcela["perimetro"]
        )

        fechas = (
            f"{fecha_inicio.isoformat()}_"
            f"{fecha_fin.isoformat()}"
        )

        carpeta = f"parcelas/{parcela_id}/ndmi"

        ruta_tif = f"{carpeta}/{fechas}.tif"
        ruta_png = f"{carpeta}/{fechas}.png"
        ruta_metadata = f"{carpeta}/{fechas}.json"

        with tempfile.TemporaryDirectory() as temporal:
            archivo_tif = str(
                Path(temporal) / "ndmi.tif"
            )

            archivo_png = str(
                Path(temporal) / "ndmi.png"
            )

            exportar_ndmi(
                zona,
                fecha_inicio.isoformat(),
                fecha_fin.isoformat(),
                archivo_tif,
            )

            crear_png_ndmi(
                archivo_tif,
                archivo_png,
            )

            with open(archivo_tif, "rb") as imagen:
                supabase.storage.from_(BUCKET).upload(
                    ruta_tif,
                    imagen.read(),
                    {
                        "content-type": "image/tiff",
                        "upsert": "true",
                    },
                )

            with open(archivo_png, "rb") as imagen:
                supabase.storage.from_(BUCKET).upload(
                    ruta_png,
                    imagen.read(),
                    {
                        "content-type": "image/png",
                        "upsert": "true",
                    },
                )

        fechas_escenas, consulta_limitada = (
            consultar_escenas_referencia(
                zona,
                fecha_inicio.isoformat(),
                fecha_fin.isoformat(),
            )
        )

        metadata = {
            "fecha_inicio_periodo": fecha_inicio.isoformat(),
            "fecha_fin_periodo": fecha_fin.isoformat(),

            "fecha_procesamiento": datetime.now(
                timezone.utc
            ).isoformat(),

            "fechas_escenas_referencia": fechas_escenas,

            "ultima_fecha_escena_referencia": (
                fechas_escenas[-1]
                if fechas_escenas
                else None
            ),

            "consulta_escenas_limitada": consulta_limitada,

            "nota": (
                "NDMI calculado como mediana temporal "
                "por píxel en openEO. "
                "Las fechas STAC indican escenas "
                "candidatas con nubosidad global <=20 %, "
                "no prueban que todas contribuyeran "
                "al resultado ni representan una "
                "captura única."
            ),
        }

        try:
            guardar_metadata_ndmi(
                ruta_metadata,
                metadata,
            )

        except Exception as error:
            print(
                "ADVERTENCIA METADATA NDMI:",
                repr(error),
            )

        return {
            **metadata,
            "parcela_id": parcela_id,
            "nombre": parcela["nombre"],
            "indice": "NDMI",
            "fecha_inicio": fecha_inicio.isoformat(),
            "fecha_fin": fecha_fin.isoformat(),
            "archivo": ruta_tif,
            "archivo_tif": ruta_tif,
            "archivo_png": ruta_png,
            "almacenamiento": "Supabase Storage privado",
        }

    except HTTPException:
        raise

    except Exception as error:
        print(
            "ERROR GENERANDO NDMI:",
            repr(error)
        )

        raise HTTPException(
            status_code=500,
            detail="No se pudo generar y guardar el mapa NDMI",
        )


# =====================================================
# NDMI - CONSULTAR URL
# =====================================================

@router.get("/parcela/{parcela_id}/ndmi/url")
def obtener_url_ndmi(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario),
):
    try:
        return _url_indice_v2(
            parcela_id,
            "ndmi"
        )

    except HTTPException:
        raise

    except Exception as error:
        print(
            "ERROR URL NDMI V2:",
            repr(error)
        )

        raise HTTPException(
            status_code=503,
            detail="No se pudo consultar el mapa NDMI",
        )


# =====================================================
# RGB - GENERAR
# =====================================================

@router.post("/parcela/{parcela_id}/rgb")
def generar_rgb_reciente(
    parcela_id: str,

    dias: int = Query(
        default=30,
        ge=1,
        le=90
    ),

    nubosidad_max: float = Query(
        default=20,
        ge=0,
        le=100
    ),

    usuario_id: str = Depends(verificar_propietario),
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
                detail="Parcela no encontrada",
            )

        parcela = respuesta.data[0]

        if not parcela.get("perimetro"):
            raise HTTPException(
                status_code=400,
                detail="La parcela no tiene perímetro",
            )

        try:
            zona = obtener_zona(
                parcela["perimetro"]
            )

        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Perímetro de parcela inválido",
            )

        with tempfile.TemporaryDirectory() as temporal:
            archivo_tif = str(
                Path(temporal) / "rgb.tif"
            )

            archivo_png = str(
                Path(temporal) / "rgb.png"
            )

            resultado = exportar_rgb_reciente(
                zona,
                archivo_tif,
                dias=dias,
                nubosidad_max=nubosidad_max,
            )

            crear_png_rgb(
                archivo_tif,
                archivo_png,
            )

            fecha = date.fromisoformat(
                resultado["fecha_captura"][:10]
            ).isoformat()

            carpeta = f"parcelas/{parcela_id}/rgb"

            ruta_tif = f"{carpeta}/{fecha}.tif"
            ruta_png = f"{carpeta}/{fecha}.png"

            with open(archivo_tif, "rb") as archivo:
                supabase.storage.from_(BUCKET).upload(
                    ruta_tif,
                    archivo.read(),
                    {
                        "content-type": "image/tiff",
                        "upsert": "true",
                    },
                )

            with open(archivo_png, "rb") as archivo:
                supabase.storage.from_(BUCKET).upload(
                    ruta_png,
                    archivo.read(),
                    {
                        "content-type": "image/png",
                        "upsert": "true",
                    },
                )

        return {
            "parcela_id": parcela_id,
            "nombre": parcela["nombre"],
            "tipo": "RGB",
            "archivo_tif": ruta_tif,
            "archivo_png": ruta_png,

            "fecha_referencia": resultado["fecha_captura"],

            "escena_referencia": resultado["escena_referencia"],

            "nubosidad_escena": resultado["nubosidad_escena"],

            "resolucion_metros": resultado["resolucion_metros"],

            "nota": (
                "La fecha corresponde a la escena "
                "de referencia STAC. "
                "El motor openEO utiliza un intervalo "
                "diario y puede combinar escenas "
                "del mismo día."
            ),
        }

    except HTTPException:
        raise

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        )

    except Exception as error:
        print(
            "ERROR GENERANDO RGB:",
            repr(error)
        )

        raise HTTPException(
            status_code=500,
            detail="No se pudo generar y guardar la imagen RGB",
        )


# =====================================================
# RGB - CONSULTAR URL
# =====================================================

@router.get("/parcela/{parcela_id}/rgb/url")
def obtener_url_rgb(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario),
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
                detail="Parcela no encontrada",
            )

        carpeta = f"parcelas/{parcela_id}/rgb"

        archivos = (
            supabase.storage.from_(BUCKET)
            .list(carpeta)
        )

        nombres = sorted(
            [
                archivo["name"]
                for archivo in archivos

                if archivo["name"].endswith(".png")
            ],
            reverse=True,
        )

        if not nombres:
            raise HTTPException(
                status_code=404,
                detail="No hay imágenes RGB para esta parcela",
            )

        nombre = nombres[0]

        url_firmada = (
            supabase.storage.from_(BUCKET)
            .create_signed_url(
                f"{carpeta}/{nombre}",
                3600,
            )
        )

        url = (
            url_firmada.get("signedURL")
            or url_firmada.get("signedUrl")
        )

        if not url:
            raise HTTPException(
                status_code=500,
                detail="No se pudo generar el enlace RGB",
            )

        return {
            "parcela_id": parcela_id,
            "archivo": nombre,
            "fecha_referencia": Path(nombre).stem,
            "formato": "PNG",
            "url": url,
            "expira_en_segundos": 3600,
        }

    except HTTPException:
        raise

    except Exception as error:
        print(
            "ERROR URL RGB:",
            repr(error)
        )

        raise HTTPException(
            status_code=500,
            detail="No se pudo obtener la URL de la imagen RGB",
        )


# =====================================================
# NDVI - CONVERTIR GEOTIFF A PNG
# FUNCIÓN DE COMPATIBILIDAD
# =====================================================

def crear_png_ndvi(archivo_tif, archivo_png):
    datos = (
        tifffile.imread(archivo_tif)
        .astype(np.float32)
    )

    ndvi = np.squeeze(datos)

    if ndvi.ndim != 2:
        raise ValueError(
            "El NDVI debe contener una sola banda"
        )

    validos = (
        np.isfinite(ndvi)
        & (ndvi >= -1)
        & (ndvi <= 1)
    )

    if not np.any(validos):
        raise ValueError(
            "El NDVI no contiene datos válidos"
        )

    valores = [
        -1,
        0,
        0.2,
        0.4,
        0.6,
        0.8,
        1,
    ]

    colores = [
        (112, 79, 53),
        (190, 165, 112),
        (229, 209, 120),
        (185, 207, 94),
        (102, 170, 75),
        (36, 125, 57),
        (12, 81, 44),
    ]

    limpio = np.nan_to_num(
        ndvi,
        nan=-1,
        posinf=1,
        neginf=-1,
    )

    rgba = np.zeros(
        (*ndvi.shape, 4),
        dtype=np.uint8,
    )

    for canal in range(3):
        rgba[:, :, canal] = np.interp(
            limpio,
            valores,
            [
                color[canal]
                for color in colores
            ],
        ).astype(np.uint8)

    rgba[:, :, 3] = np.where(
        validos,
        255,
        0,
    ).astype(np.uint8)

    Image.fromarray(
        rgba,
        "RGBA",
    ).save(archivo_png)

    return archivo_png


# =====================================================
# NDVI - GENERAR
# =====================================================

@router.post("/parcela/{parcela_id}/ndvi")
def generar_ndvi(
    parcela_id: str,

    fecha_inicio: date = Query(
        default_factory=lambda: (
            date.today() - timedelta(days=30)
        )
    ),

    fecha_fin: date = Query(
        default_factory=date.today
    ),

    usuario_id: str = Depends(verificar_propietario),
):
    if fecha_inicio > fecha_fin:
        raise HTTPException(
            status_code=400,
            detail="Rango de fechas inválido",
        )

    if fecha_fin > date.today():
        raise HTTPException(
            status_code=400,
            detail="La fecha final no puede ser futura",
        )

    if (fecha_fin - fecha_inicio).days > 90:
        raise HTTPException(
            status_code=400,
            detail="El período máximo es de 90 días",
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
                detail="Parcela no encontrada",
            )

        parcela = respuesta.data[0]

        if not parcela.get("perimetro"):
            raise HTTPException(
                status_code=400,
                detail="La parcela no tiene perímetro",
            )

        zona = obtener_zona(
            parcela["perimetro"]
        )

        periodo = (
            f"{fecha_inicio.isoformat()}_"
            f"{fecha_fin.isoformat()}"
        )

        carpeta = f"parcelas/{parcela_id}/ndvi"

        ruta_tif = f"{carpeta}/{periodo}.tif"
        ruta_png = f"{carpeta}/{periodo}.png"
        ruta_json = f"{carpeta}/{periodo}.json"

        with tempfile.TemporaryDirectory() as temporal:
            tif_local = str(
                Path(temporal) / "ndvi.tif"
            )

            png_local = str(
                Path(temporal) / "ndvi.png"
            )

            exportar_ndvi(
                zona,
                fecha_inicio.isoformat(),
                fecha_fin.isoformat(),
                tif_local,
            )

            crear_png_ndvi(
                tif_local,
                png_local,
            )

            for local, remoto, tipo in (
                (
                    tif_local,
                    ruta_tif,
                    "image/tiff"
                ),
                (
                    png_local,
                    ruta_png,
                    "image/png"
                ),
            ):
                with open(local, "rb") as archivo:
                    supabase.storage.from_(BUCKET).upload(
                        remoto,
                        archivo.read(),
                        {
                            "content-type": tipo,
                            "upsert": "true",
                        },
                    )

        fechas, limitada = consultar_escenas_referencia(
            zona,
            fecha_inicio.isoformat(),
            fecha_fin.isoformat(),
        )

        metadata = {
            "fecha_inicio_periodo": fecha_inicio.isoformat(),
            "fecha_fin_periodo": fecha_fin.isoformat(),

            "fecha_procesamiento": datetime.now(
                timezone.utc
            ).isoformat(),

            "fechas_escenas_referencia": fechas,

            "ultima_fecha_escena_referencia": (
                fechas[-1]
                if fechas
                else None
            ),

            "consulta_escenas_limitada": limitada,

            "nota": (
                "NDVI calculado mediante mediana temporal "
                "por píxel. Las escenas STAC son candidatas "
                "y no representan necesariamente la fecha "
                "exacta de cada píxel."
            ),
        }

        try:
            supabase.storage.from_(BUCKET).upload(
                ruta_json,

                json.dumps(
                    metadata,
                    ensure_ascii=False,
                ).encode("utf-8"),

                {
                    "content-type": "application/json",
                    "upsert": "true",
                },
            )

        except Exception as error:
            print(
                "ADVERTENCIA METADATOS NDVI:",
                repr(error),
            )

        return {
            **metadata,
            "parcela_id": parcela_id,
            "nombre": parcela["nombre"],
            "indice": "NDVI",
            "archivo_tif": ruta_tif,
            "archivo_png": ruta_png,
            "almacenamiento": "Supabase Storage privado",
        }

    except HTTPException:
        raise

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        )

    except Exception as error:
        print(
            "ERROR GENERANDO NDVI:",
            repr(error),
        )

        raise HTTPException(
            status_code=500,
            detail="No se pudo generar NDVI",
        )


# =====================================================
# NDVI - CONSULTAR URL Y METADATOS
# =====================================================

@router.get("/parcela/{parcela_id}/ndvi/url")
def obtener_url_ndvi(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario),
):
    try:
        return _url_indice_v2(
            parcela_id,
            "ndvi"
        )

    except HTTPException:
        raise

    except Exception as error:
        print(
            "ERROR URL NDVI V2:",
            repr(error)
        )

        raise HTTPException(
            status_code=503,
            detail="No se pudo consultar el mapa NDVI",
        )


# =====================================================
# ÍNDICES AGRÍCOLAS - CONSULTA
# =====================================================

@router.get("/parcela/{parcela_id}/indices")
def consultar_indices_parcela(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario),
):
    try:
        registro, mapas = _ultimo_paquete_v2(
            parcela_id
        )

        return {
            "parcela_id": parcela_id,
            "disponible": registro is not None,
            "coherente": registro is not None,
            "indices": registro,

            "fecha_referencia": (
                registro["fecha_escena_referencia"]
                if registro
                else None
            ),

            "version_analisis": (
                2 if registro else None
            ),
        }

    except Exception as error:
        print(
            "ERROR CONSULTANDO INDICES:",
            repr(error)
        )

        raise HTTPException(
            status_code=503,
            detail="No se pudieron consultar los índices",
        )


# =====================================================
# ÍNDICES AGRÍCOLAS - ACTUALIZACIÓN AUTOMÁTICA
# =====================================================

@router.post(
    "/parcela/{parcela_id}/indices/actualizar",
    status_code=202
)
def solicitar_actualizacion_indices(
    parcela_id: str,
    usuario_id: str = Depends(verificar_propietario),
):
    from motor_satelital import buscar_imagenes_recientes

    try:
        respuesta = (
            supabase.table("parcelas")
            .select("id,perimetro")
            .eq("id", parcela_id)
            .limit(1)
            .execute()
        )

        if not respuesta.data:
            raise HTTPException(
                status_code=404,
                detail="Parcela no encontrada",
            )

        perimetro = respuesta.data[0].get(
            "perimetro"
        )

        if not perimetro:
            raise HTTPException(
                status_code=400,
                detail="La parcela no tiene perímetro",
            )

        zona = obtener_zona(perimetro)

        escenas = buscar_imagenes_recientes(
            zona,
            dias=30,
            nubosidad_max=20,
        )["utiles"]

        if not escenas:
            return {
                "estado": "sin_escenas_utiles",
                "parcela_id": parcela_id,
            }

        fecha_escena = escenas[0]["fecha"][:10]

        existente = (
            supabase.table("indices_agricolas")
            .select("id")
            .eq("parcela_id", parcela_id)
            .eq(
                "fecha_escena_referencia",
                fecha_escena
            )
            .limit(1)
            .execute()
        )

        if existente.data:
            registro, _ = _ultimo_paquete_v2(
                parcela_id
            )

            if (
                registro
                and str(
                    registro["fecha_escena_referencia"]
                )[:10] == fecha_escena
            ):
                return {
                    "estado": "actualizado",
                    "parcela_id": parcela_id,
                    "fecha_escena_referencia": fecha_escena,
                }

        token = os.getenv(
            "GITHUB_ACTIONS_TOKEN"
        )

        if not token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Actualización bajo demanda "
                    "aún no configurada"
                ),
            )

        url = (
            "https://api.github.com/repos/"
            "tgrhswhvzc-del/"
            "DELTA-AGRICULTURA-DE-PRESICION/"
            "actions/workflows/"
            "indices-agricolas.yml/dispatches"
        )

        lanzamiento = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "ref": "main",
                "inputs": {
                    "parcela_id": parcela_id
                }
            },
            timeout=15,
        )

        if lanzamiento.status_code != 204:
            print(
                "ERROR GITHUB DISPATCH:",
                lanzamiento.status_code,
                lanzamiento.text[:250],
            )

            raise HTTPException(
                status_code=503,
                detail=(
                    "No se pudo iniciar el "
                    "procesamiento satelital"
                ),
            )

        return {
            "estado": "procesamiento_solicitado",
            "parcela_id": parcela_id,
            "fecha_escena_referencia": fecha_escena,
            "mensaje": (
                "Los índices actuales seguirán "
                "disponibles hasta que termine "
                "el cálculo"
            ),
        }

    except HTTPException:
        raise

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Perímetro inválido",
        )

    except requests.RequestException as error:
        print(
            "ERROR COMUNICACIÓN GITHUB:",
            repr(error)
        )

        raise HTTPException(
            status_code=503,
            detail="GitHub no disponible",
        )

    except Exception as error:
        print(
            "ERROR SOLICITANDO ÍNDICES:",
            repr(error)
        )

        raise HTTPException(
            status_code=500,
            detail="No se pudo solicitar la actualización",
        )
