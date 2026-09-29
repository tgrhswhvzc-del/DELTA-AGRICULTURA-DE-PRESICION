import random
import time
import requests


API_URL = "http://127.0.0.1:8000/mediciones"

SENSOR_ID = "19b708a5-4c1b-4299-be76-539373d04cce"


while True:

    medicion = {
        "sensor_id": SENSOR_ID,
        "humedad": round(random.uniform(40, 80), 1),
        "temperatura": round(random.uniform(20, 35), 1)
    }

    try:

        respuesta = requests.post(
            API_URL,
            json=medicion,
            timeout=10
        )

        print(respuesta.status_code)
        print(respuesta.json())

    except requests.exceptions.RequestException as e:

        print("ERROR DE CONEXIÓN:", e)

    time.sleep(10)