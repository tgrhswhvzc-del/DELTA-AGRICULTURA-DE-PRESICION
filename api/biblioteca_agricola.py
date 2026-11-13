"""DELTA IA: base agronómica inicial por cultivo (V2).

Notas orientativas, NO diagnósticos ni recetas. Se incorporan al contexto
solamente para el cultivo de la parcela. Para bibliografía completa y
actualizada se habilitará búsqueda documental con citas verificadas.
"""
import unicodedata

FUENTES_GENERALES = [
    {"titulo": "FAO: Crop water needs, capítulos 2 y 3", "url": "https://www.fao.org/4/s2022e/s2022e02.htm"},
    {"titulo": "FAO: Growth stages and crop water needs", "url": "https://www.fao.org/4/s2022e/s2022e07.htm"},
]

# Observaciones de campo sugeridas; no hay umbrales de NDVI, dosis ni plagas diagnosticadas.
CULTIVOS = {
    "maiz": {
        "nombre": "Maíz", "alias": ["maiz", "zea mays", "corn"],
        "etapas": "Emergencia, desarrollo vegetativo, floración/polinización, llenado de grano, maduración o cosecha.",
        "factores": "La cobertura del suelo y el NDVI cambian mucho con la etapa. En maduración o después de cosecha una caída de NDVI puede ser esperada.",
        "verificar": "Fecha de siembra, híbrido, etapa fenológica, uniformidad de emergencia, distribución de riego, hojas afectadas, labores y cosecha.",
    },
    "guayaba": {
        "nombre": "Guayaba", "alias": ["guayaba", "psidium guajava", "guava"],
        "etapas": "Brotación vegetativa, floración, cuajado, crecimiento y cosecha de frutos; pueden coexistir estados en un huerto.",
        "factores": "En frutales el NDVI de la parcela incluye copas, suelo entre hileras, malezas y sombras. Los índices promedio no separan automáticamente cada árbol.",
        "verificar": "Edad de árboles, poda, floración/fructificación, uniformidad entre hileras, riego, hojas internas y externas, historial de cosecha.",
    },
    "tomate": {
        "nombre": "Tomate", "alias": ["tomate", "jitomate", "solanum lycopersicum"],
        "etapas": "Establecimiento/trasplante, crecimiento vegetativo, floración, cuajado, desarrollo y cosecha de frutos.",
        "factores": "La conducción, poda, cobertura plástica y suelo visible modifican las lecturas espectrales.",
        "verificar": "Fecha de trasplante, sistema protegido o abierto, floración, fertirrigación, drenaje, distribución de síntomas y envés de hojas.",
    },
    "pepino": {
        "nombre": "Pepino", "alias": ["pepino", "cucumis sativus", "cucumber"],
        "etapas": "Emergencia/trasplante, guías y hojas, floración, cuajado y cosecha continua.",
        "factores": "El crecimiento rápido cambia la cobertura en días; enfermedad foliar, poda, sombra y suelo pueden afectar la señal sin identificar causas.",
        "verificar": "Edad del cultivo, estado de hojas nuevas y viejas, envés, raíces, riego, ventilación y distribución de lesiones.",
    },
    "chile": {
        "nombre": "Chile", "alias": ["chile", "pimiento", "capsicum", "chilli"],
        "etapas": "Trasplante, crecimiento vegetativo, floración, amarre, llenado y cosechas sucesivas.",
        "factores": "La densidad de follaje, fruto y podas modifican la señal; NDRE no prueba una deficiencia específica.",
        "verificar": "Variedad, fecha de plantación, vigor por surco, hojas jóvenes/viejas, manejo de agua, fertilización y antecedentes.",
    },
    "frijol": {
        "nombre": "Frijol", "alias": ["frijol", "phaseolus vulgaris", "bean"],
        "etapas": "Emergencia, desarrollo vegetativo, floración, formación y llenado de vainas, secado/cosecha.",
        "factores": "En el secado natural puede caer el NDVI sin implicar un problema; al inicio hay mayor influencia del suelo.",
        "verificar": "Fecha de siembra, porcentaje de cobertura, floración/vainas, humedad del suelo y manchas en hojas.",
    },
    "trigo": {
        "nombre": "Trigo", "alias": ["trigo", "triticum", "wheat"],
        "etapas": "Emergencia, macollamiento, encañado, espigamiento, llenado de grano y maduración.",
        "factores": "Los índices varían con macollamiento y senescencia; no inferir nitrógeno a partir de NDRE aislado.",
        "verificar": "Fecha de siembra, uniformidad, etapa de espiga, color de hojas, labores y patrón de riego.",
    },
    "sandia": {
        "nombre": "Sandía", "alias": ["sandia", "citrullus lanatus", "watermelon"],
        "etapas": "Establecimiento, desarrollo de guías, floración, cuajado, crecimiento y cosecha.",
        "factores": "Suelo entre guías y acolchados pueden dominar el índice en etapas tempranas; cambios de manejo pueden alterar el mapa.",
        "verificar": "Fecha de siembra, desarrollo de guías, cuajado, cobertura de suelo y distribución del riego.",
    },
    "cebolla": {
        "nombre": "Cebolla", "alias": ["cebolla", "allium cepa", "onion"],
        "etapas": "Establecimiento, crecimiento foliar, inicio/engrosamiento de bulbo, maduración y cosecha.",
        "factores": "Hojas estrechas y suelo expuesto pueden reducir el índice promedio incluso con plantas en desarrollo.",
        "verificar": "Fecha de siembra/trasplante, uniformidad, desarrollo de bulbo, riego, salinidad si existe análisis y secado previo a cosecha.",
    },
    "fresa": {
        "nombre": "Fresa", "alias": ["fresa", "fragaria", "strawberry"],
        "etapas": "Establecimiento, desarrollo de corona/follaje, floración, fructificación y cosechas repetidas.",
        "factores": "Acolchado, pasillos, sombras y cobertura variable pueden alterar valores de imagen.",
        "verificar": "Edad y variedad, floración/frutos, hojas jóvenes y viejas, cama de cultivo, riego y síntomas por planta.",
    },
}


def _normalizar(texto):
    t = unicodedata.normalize("NFD", str(texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def conocimiento_para(cultivo):
    nombre = _normalizar(cultivo)
    for ficha in CULTIVOS.values():
        if any(alias in nombre for alias in ficha["alias"]):
            return {
                "cultivo": ficha["nombre"],
                "etapas_relevantes": ficha["etapas"],
                "interpretacion_contextual": ficha["factores"],
                "verificaciones_en_campo": ficha["verificar"],
                "fuentes_metodologicas_generales": FUENTES_GENERALES,
                "alcance": "Ficha introductoria orientativa. No valida enfermedades, plagas ni dosis.",
            }
    return {
        "cultivo": str(cultivo or "No especificado")[:120],
        "alcance": "No hay ficha revisada para este cultivo. Solicita edad, variedad, etapa y fotografías antes de interpretar anomalías.",
        "fuentes_metodologicas_generales": FUENTES_GENERALES,
    }
