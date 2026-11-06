"""Revisión programada de MSI/NDRE, independiente de NDVI/NDMI.

No calcula imágenes en el programador: solicita trabajos por parcela/fecha
al workflow probado publicar-indices-avanzados.yml.
"""

import json
import os
from datetime import datetime, timezone

import requests

from actualizar_programado import cargar_parcelas, seleccionar_lote
from analisis_parcela import normalizar_poligono, firma_perimetro, VERSION_ANALISIS
from supabase_client import supabase

BUCKET = "mapas-satelitales"
WORKFLOW = "publicar-indices-avanzados.yml"
REPO = "tgrhswhvzc-del/DELTA-AGRICULTURA-DE-PRESICION"
API_GITHUB = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}"


def _metadata(parcela_id, indice, fecha):
    ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.json"
    try:
        datos = supabase.storage.from_(BUCKET).download(ruta)
        return json.loads(datos.decode("utf-8"))
    except Exception:
        return None


def _coherente(meta, fecha, perimetro_hash):
    return (
        isinstance(meta, dict)
        and meta.get("fecha_escena_referencia") == fecha
        and meta.get("version_analisis") == VERSION_ANALISIS
        and meta.get("perimetro_recortado") is True
        and meta.get("perimetro_hash") == perimetro_hash
        and isinstance(meta.get("promedio"), (int, float))
    )


def _ultima_fecha_base(parcela_id):
    respuesta = (
        supabase.table("indices_agricolas")
        .select("fecha_escena_referencia")
        .eq("parcela_id", parcela_id)
        .order("fecha_escena_referencia", desc=True)
        .limit(1)
        .execute()
    )
    return str(respuesta.data[0]["fecha_escena_referencia"])[:10] if respuesta.data else None


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _ejecucion_en_curso(sesion, headers, parcela_id, fecha):
    respuesta = sesion.get(
        API_GITHUB + "/runs",
        params={"event": "workflow_dispatch", "per_page": 100},
        headers=headers,
        timeout=20,
    )
    respuesta.raise_for_status()
    titulo = f"MSI-NDRE {parcela_id} {fecha}"
    ahora = datetime.now(timezone.utc)
    for ejecucion in respuesta.json().get("workflow_runs", []):
        if ejecucion.get("display_title") != titulo:
            continue
        try:
            fecha_creacion = datetime.fromisoformat(
                str(ejecucion.get("created_at", "")).replace("Z", "+00:00")
            )
            edad = (ahora - fecha_creacion).total_seconds()
        except ValueError:
            edad = 10**9
        if ejecucion.get("status") != "completed" and 0 <= edad < 3 * 3600:
            return True
        if ejecucion.get("status") == "completed" and 0 <= edad < 600:
            return True
        return False
    return False


def main():
    solo_revisar = os.getenv("SOLO_REVISAR", "false").strip().lower() == "true"
    token = os.getenv("GH_TOKEN", "").strip()
    if not token and not solo_revisar:
        raise RuntimeError("Falta GH_TOKEN (usar secrets.GITHUB_TOKEN en Actions)")

    # DELTA_AUTO_PARCELA_OBJETIVO: si el workflow base indica una parcela,
    # se revisa solo esa. Sin PARCELA_ID se conserva el lote programado usual.
    parcela_objetivo = os.getenv("PARCELA_ID", "").strip()
    disponibles = cargar_parcelas()
    if parcela_objetivo:
        parcelas = [p for p in disponibles if str(p["id"]) == parcela_objetivo]
        if not parcelas:
            raise ValueError(f"La parcela solicitada no existe o no tiene perimetro: {parcela_objetivo}")
    else:
        parcelas = seleccionar_lote(disponibles)
    print(f"DELTA avanzados: revisando {len(parcelas)} parcelas", flush=True)
    errores = []
    with requests.Session() as sesion:
        cabeceras = _headers(token)
        for parcela in parcelas:
            parcela_id = str(parcela["id"])
            try:
                perimetro = normalizar_poligono(parcela["perimetro"])
                perimetro_hash = firma_perimetro(perimetro)
                fecha = _ultima_fecha_base(parcela_id)
                if not fecha:
                    print(f"{parcela_id}: esperando NDVI/NDMI", flush=True)
                    continue

                if not all(_coherente(_metadata(parcela_id, indice, fecha), fecha, perimetro_hash)
                           for indice in ("ndvi", "ndmi")):
                    print(f"{parcela_id}: paquete base no coherente; se omite", flush=True)
                    continue

                faltantes = [indice for indice in ("ndre", "msi")
                             if not _coherente(_metadata(parcela_id, indice, fecha), fecha, perimetro_hash)]
                if not faltantes:
                    print(f"{parcela_id}: MSI y NDRE ya publicados para {fecha}", flush=True)
                    continue
                if solo_revisar:
                    print(f"SIMULADO {parcela_id} {fecha}: faltarían {','.join(faltantes)}", flush=True)
                    continue
                if _ejecucion_en_curso(sesion, cabeceras, parcela_id, fecha):
                    print(f"{parcela_id}: ejecución existente para {fecha}", flush=True)
                    continue

                respuesta = sesion.post(
                    API_GITHUB + "/dispatches",
                    headers=cabeceras,
                    json={"ref": "main", "inputs": {
                        "parcela_id": parcela_id,
                        "fecha_escena": fecha,
                        "publicar": "true",
                    }},
                    timeout=20,
                )
                if respuesta.status_code != 204:
                    raise RuntimeError(f"GitHub respondió {respuesta.status_code}: {respuesta.text[:200]}")
                print(f"SOLICITADO {parcela_id} {fecha}: {','.join(faltantes)}", flush=True)
            except Exception as exc:
                errores.append(parcela_id)
                print(f"ERROR {parcela_id}: {exc}", flush=True)

    if errores:
        raise SystemExit("Falló revisión avanzada: " + ", ".join(errores))
    print("Revisión avanzada completada", flush=True)


if __name__ == "__main__":
    main()
