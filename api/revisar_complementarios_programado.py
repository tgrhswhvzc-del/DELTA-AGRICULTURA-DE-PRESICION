"""DELTA: despachar SAVI, EVI2 y cambio NDVI para parcelas con NDVI/NDMI válidos.

El despacho no calcula imágenes. La ejecución independiente publica bajo el
mismo ID de parcela y fecha de escena que NDVI/NDMI.

Variables: PARCELA_ID (opcional; si falta se usa lote del programador),
GH_TOKEN y SOLO_REVISAR (true para simular).
"""
import json
import os
from datetime import datetime, timezone
from uuid import UUID

import requests

from actualizar_programado import cargar_parcelas, seleccionar_lote
from analisis_parcela import VERSION_ANALISIS, firma_perimetro, normalizar_poligono
from supabase_client import supabase

BUCKET = "mapas-satelitales"
REPO = "tgrhswhvzc-del/DELTA-AGRICULTURA-DE-PRESICION"
WORKFLOW = "publicar-mapas-complementarios.yml"
API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}"


def _metadata(parcela_id, indice, fecha):
    ruta = f"parcelas/{parcela_id}/{indice}/{fecha}.json"
    try:
        return json.loads(supabase.storage.from_(BUCKET).download(ruta).decode("utf-8"))
    except Exception:
        return None


def _coherente(metadata, fecha, firma):
    return (
        isinstance(metadata, dict)
        and metadata.get("fecha_escena_referencia") == fecha
        and metadata.get("version_analisis") == VERSION_ANALISIS
        and metadata.get("perimetro_recortado") is True
        and metadata.get("perimetro_hash") == firma
        and metadata.get("escala_tipo") == "fija_absoluta"
        and isinstance(metadata.get("promedio"), (float, int))
        and isinstance(metadata.get("bounds"), list)
        and len(metadata["bounds"]) == 2
    )


def _ultima_base_coherente(parcela_id, firma):
    registros = (
        supabase.table("indices_agricolas")
        .select("fecha_escena_referencia")
        .eq("parcela_id", parcela_id)
        .order("fecha_escena_referencia", desc=True)
        .limit(20)
        .execute()
    ).data or []
    for fila in registros:
        fecha = str(fila["fecha_escena_referencia"])[:10]
        if all(_coherente(_metadata(parcela_id, i, fecha), fecha, firma)
               for i in ("ndvi", "ndmi")):
            return fecha
    return None


def _cabeceras(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _ya_solicitado(session, headers, parcela_id, fecha):
    respuesta = session.get(
        API + "/runs",
        params={"event": "workflow_dispatch", "per_page": 100},
        headers=headers,
        timeout=20,
    )
    respuesta.raise_for_status()
    titulo = f"SAVI-EVI2-CAMBIO {parcela_id} {fecha}"
    ahora = datetime.now(timezone.utc)
    for run in respuesta.json().get("workflow_runs", []):
        if run.get("display_title") != titulo:
            continue
        try:
            creada = datetime.fromisoformat(str(run.get("created_at", "")).replace("Z", "+00:00"))
            edad = (ahora - creada).total_seconds()
        except (TypeError, ValueError):
            edad = 10**9
        if run.get("status") != "completed" and 0 <= edad < 3 * 3600:
            return True
        # Evita reenvíos inmediatamente después de concluir y durante fallas recientes.
        if run.get("status") == "completed" and 0 <= edad < 3600:
            return True
        return False
    return False


def main():
    simulacion = os.getenv("SOLO_REVISAR", "false").lower() == "true"
    token = os.getenv("GH_TOKEN", "").strip()
    if not simulacion and not token:
        raise RuntimeError("Falta GH_TOKEN: usar github.token en GitHub Actions")

    objetivo = os.getenv("PARCELA_ID", "").strip()
    if objetivo:
        objetivo = str(UUID(objetivo))
    disponibles = cargar_parcelas()
    parcelas = (
        [p for p in disponibles if str(p["id"]) == objetivo]
        if objetivo else seleccionar_lote(disponibles)
    )
    if objetivo and not parcelas:
        raise ValueError("Parcela solicitada no encontrada o sin perímetro")
    print(f"DELTA complementarios: revisando {len(parcelas)} parcelas", flush=True)
    errores = []
    with requests.Session() as sesion:
        headers = _cabeceras(token)
        for p in parcelas:
            pid = str(p["id"])
            try:
                firma = firma_perimetro(normalizar_poligono(p["perimetro"]))
                fecha = _ultima_base_coherente(pid, firma)
                if not fecha:
                    print(f"{pid}: esperando NDVI y NDMI válidos", flush=True)
                    continue
                faltan = [i for i in ("savi", "evi2")
                          if not _coherente(_metadata(pid, i, fecha), fecha, firma)]
                if not faltan:
                    # Cambio NDVI es opcional: necesita un NDVI histórico comparable.
                    print(f"{pid}: SAVI y EVI2 completos ({fecha}); cambio NDVI sujeto a histórico", flush=True)
                    continue
                if simulacion:
                    print(f"SIMULADO {pid} {fecha}: faltan {', '.join(faltan)}", flush=True)
                    continue
                if _ya_solicitado(sesion, headers, pid, fecha):
                    print(f"{pid}: trabajo existente o demasiado reciente", flush=True)
                    continue
                resp = sesion.post(
                    API + "/dispatches",
                    headers=headers,
                    json={"ref": "main", "inputs": {
                        "parcela_id": pid, "fecha_escena": fecha, "publicar": "true"
                    }},
                    timeout=20,
                )
                if resp.status_code != 204:
                    raise RuntimeError(f"GitHub respondió {resp.status_code}: {resp.text[:200]}")
                print(f"SOLICITADO {pid} {fecha}: SAVI, EVI2 y cambio si hay histórico", flush=True)
            except Exception as exc:
                print(f"ERROR {pid}: {exc}", flush=True)
                errores.append(pid)
    if errores:
        raise SystemExit("Error en parcelas: " + ", ".join(errores))
    print("Revisión de complementarios finalizada", flush=True)


if __name__ == "__main__":
    main()
