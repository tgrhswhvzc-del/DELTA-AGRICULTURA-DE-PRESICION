from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase_client import supabase

app = FastAPI(
    title="Sistema de Agricultura de Precisión",
    version="1.0.0"
)


class Medicion(BaseModel):
    sensor_id: str
    humedad: float
    temperatura: float


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
            supabase
            .table("mediciones")
            .insert(datos)
            .execute()
        )

        return {
            "mensaje": "Medición guardada",
            "datos": respuesta.data
        }

    except Exception as e:
        print("ERROR SUPABASE:", e)

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/productores/{productor_id}")
def obtener_productor(productor_id: str):

    try:
        productor = (
            supabase
            .table("productores")
            .select("*")
            .eq("id", productor_id)
            .execute()
        )

        if not productor.data:
            raise HTTPException(
                status_code=404,
                detail="Productor no encontrado"
            )

        sensores = (
            supabase
            .table("sensores")
            .select("*")
            .eq("productor_id", productor_id)
            .execute()
        )

        return {
            "productor": productor.data,
            "sensores": sensores.data
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/sensores/{sensor_id}/mediciones")
def obtener_mediciones(sensor_id: str):

    try:
        sensor = (
            supabase
            .table("sensores")
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
            supabase
            .table("mediciones")
            .select("*")
            .eq("sensor_id", sensor_id)
            .order("created_at", desc=True)
            .execute()
        )

        return {
            "sensor": sensor.data,
            "mediciones": mediciones.data
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000
    )