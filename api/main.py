from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase_client import supabase

app = FastAPI()

# Permite que Shopify se comunique con nuestra API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Medicion(BaseModel):
    sensor_id: str
    humedad: float
    temperatura: float


class Parcela(BaseModel):
    nombre: str
    ubicacion: str = ""
    superficie: float
    cultivo: str
    productor_id: str


@app.get("/")
def inicio():
    return {
        "mensaje": "API de Agricultura de Precisión funcionando"
    }


@app.post("/mediciones")
def recibir_medicion(medicion: Medicion):
    datos = {
        "sensor_id": medicion.sensor_id,
        "humedad": medicion.humedad,
        "temperatura": medicion.temperatura
    }

    try:
        respuesta = (
            supabase.table("mediciones")
            .insert(datos)
            .execute()
        )

        return {
            "mensaje": "Medición guardada",
            "datos": respuesta.data
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.post("/parcelas")
def crear_parcela(parcela: Parcela):
    datos = {
        "nombre": parcela.nombre,
        "ubicacion": parcela.ubicacion,
        "superficie": parcela.superficie,
        "cultivo": parcela.cultivo,
        "productor_id": parcela.productor_id
    }

    try:
        respuesta = (
            supabase.table("parcelas")
            .insert(datos)
            .execute()
        )

        return {
            "mensaje": "Parcela registrada correctamente",
            "datos": respuesta.data
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/productores/{productor_id}")
def obtener_productor(productor_id: str):
    productor = (
        supabase.table("productores")
        .select("*")
        .eq("id", productor_id)
        .execute()
    )

    sensores = (
        supabase.table("sensores")
        .select("*")
        .eq("productor_id", productor_id)
        .execute()
    )

    return {
        "productor": productor.data,
        "sensores": sensores.data
    }


@app.get("/sensores/{sensor_id}/mediciones")
def obtener_mediciones(sensor_id: str):
    sensor = (
        supabase.table("sensores")
        .select("*")
        .eq("id", sensor_id)
        .execute()
    )

    if not sensor.data:
        raise HTTPException(
            status_code=404,
            detail="Sensor no encontrado"
        )

    mediciones = (
        supabase.table("mediciones")
        .select("*")
        .eq("sensor_id", sensor_id)
        .order("creado_en", desc=True)
        .execute()
    )

    return {
        "sensor": sensor.data,
        "mediciones": mediciones.data
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000
    )
