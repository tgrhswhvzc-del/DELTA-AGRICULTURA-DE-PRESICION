import requests
import random
import time
import json
import os

URL = "http://127.0.0.1:8000/mediciones"
SENSOR_ID = "19b708a5-4c1b-4299-be76-539373d04cce"

ARCHIVO_PENDIENTES = "mediciones_pendientes.json"


def cargar_pendientes():
    if not os.path.exists(ARCHIVO_PENDIENTES):
        return []

    with open(ARCHIVO_PENDIENTES, "r") as archivo:
        return json.load(archivo)


def guardar_pendientes(mediciones):
    with open(ARCHIVO_PENDIENTES, "w") as archivo:
        json.dump(mediciones, archivo, indent=4)


def enviar_medicion(datos):
    try:
        respuesta = requests.post(URL, json=datos, timeout=5)

        if respuesta.status_code == 200:
            print("Medición enviada correctamente")
            return True

        print("Servidor rechazó la medición")
        return False

    except requests.RequestException:
        print("Sin conexión. Medición guardada localmente")
        return False


while True:

    datos = {
        "sensor_id": SENSOR_ID,
        "humedad": round(random.uniform(30, 70), 1),
        "temperatura": round(random.uniform(20, 35), 1)
    }

    pendientes = cargar_pendientes()

    pendientes.append(datos)
    guardar_pendientes(pendientes)

    print("Nueva medición:", datos)

    pendientes = cargar_pendientes()
    restantes = []

    for medicion in pendientes:

        if not enviar_medicion(medicion):
            restantes.append(medicion)

    guardar_pendientes(restantes)

    print("Mediciones pendientes:", len(restantes))

    time.sleep(1200)