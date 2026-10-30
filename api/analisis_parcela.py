
"""Análisis geoespacial de NDVI/NDMI para DELTA."""

import json
import hashlib
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import geometry_mask
from rasterio.warp import (
    calculate_default_transform,
    reproject,
    Resampling,
    transform_geom,
)

VERSION_ANALISIS = 2
NODATA = -9999.0
COBERTURA_MINIMA = 0.50

ESCALAS = {
    "ndvi": [
        (-1.0, "#704f35"),
        (0.0, "#bea570"),
        (0.2, "#e5d178"),
        (0.4, "#b9cf5e"),
        (0.6, "#66aa4b"),
        (0.8, "#247d39"),
        (1.0, "#0c512c"),
    ],
    "ndmi": [
        (-1.0, "#c30019"),
        (-0.2, "#ff6900"),
        (0.0, "#ffeb00"),
        (0.2, "#2ad719"),
        (0.4, "#00c8eb"),
        (0.6, "#0f23c8"),
        (1.0, "#07166b"),
    ],
}


def normalizar_poligono(perimetro):
    if isinstance(perimetro, str):
        perimetro = json.loads(perimetro)

    if not isinstance(perimetro, dict) or perimetro.get("type") != "Polygon":
        raise ValueError("El perímetro debe ser un GeoJSON Polygon")

    anillos = perimetro.get("coordinates")

    if not isinstance(anillos, list) or not anillos or len(anillos[0]) < 4:
        raise ValueError("El perímetro debe contener al menos tres vértices")

    return perimetro


def firma_perimetro(perimetro):
    serializado = json.dumps(
        normalizar_poligono(perimetro),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    return hashlib.sha256(
        serializado.encode("utf-8")
    ).hexdigest()


def _mascara_poligono(geojson, crs, transform, shape):
    geom = transform_geom("EPSG:4326", crs, geojson)

    return geometry_mask(
        [geom],
        out_shape=shape,
        transform=transform,
        invert=True,
        all_touched=False,
    )


def _colorear(matriz, mascara, indice):
    paradas = ESCALAS[indice]
    posiciones = [valor for valor, _ in paradas]

    canales = np.array([
        tuple(int(color[j:j + 2], 16) for j in (1, 3, 5))
        for _, color in paradas
    ], dtype=np.uint8)

    rgba = np.zeros(
        (*matriz.shape, 4),
        dtype=np.uint8,
    )

    for canal in range(3):
        rgba[:, :, canal] = np.interp(
            np.clip(matriz, -1, 1),
            posiciones,
            canales[:, canal],
        ).astype(np.uint8)

    rgba[:, :, 3] = np.where(
        mascara, 255, 0
    ).astype(np.uint8)

    return rgba


def procesar_indice(
    tif_origen,
    perimetro,
    indice,
    png_destino,
    tif_destino,
):
    indice = indice.lower()

    if indice not in ESCALAS:
        raise ValueError("Índice no compatible")

    perimetro = normalizar_poligono(perimetro)

    with rasterio.open(tif_origen) as fuente:
        if fuente.count != 1 or fuente.crs is None:
            raise ValueError(
                "GeoTIFF sin georreferencia o con múltiples bandas"
            )

        original = fuente.read(1).astype(np.float32)

        geometria = _mascara_poligono(
            perimetro,
            fuente.crs,
            fuente.transform,
            original.shape,
        )

        if not geometria.any():
            raise ValueError(
                "El perímetro no cubre píxeles del raster"
            )

        validos = (
            geometria
            & np.isfinite(original)
            & (original >= -1)
            & (original <= 1)
        )

        if fuente.nodata is not None and np.isfinite(fuente.nodata):
            validos &= original != fuente.nodata

        cantidad = int(validos.sum())
        cobertura = cantidad / int(geometria.sum())

        if cantidad == 0 or cobertura < COBERTURA_MINIMA:
            raise ValueError(
                f"Cobertura insuficiente en la parcela ({cobertura:.0%}); "
                "se necesita otra escena menos nublada"
            )

        promedio = float(
            original[validos].mean(dtype=np.float64)
        )

        recortado = np.where(
            validos, original, NODATA
        ).astype(np.float32)

        perfil = fuente.profile.copy()

        perfil.update(
            driver="GTiff",
            count=1,
            dtype="float32",
            nodata=NODATA,
            compress="deflate",
        )

        Path(tif_destino).parent.mkdir(
            parents=True, exist_ok=True
        )

        with rasterio.open(tif_destino, "w", **perfil) as salida:
            salida.write(recortado, 1)

        transf, ancho, alto = calculate_default_transform(
            fuente.crs,
            "EPSG:4326",
            fuente.width,
            fuente.height,
            *fuente.bounds,
        )

        imagen = np.full(
            (alto, ancho),
            NODATA,
            dtype=np.float32,
        )

        reproject(
            source=recortado,
            destination=imagen,
            src_transform=fuente.transform,
            src_crs=fuente.crs,
            src_nodata=NODATA,
            dst_transform=transf,
            dst_crs="EPSG:4326",
            dst_nodata=NODATA,
            resampling=Resampling.nearest,
        )

    visibles = (
        np.isfinite(imagen)
        & (imagen >= -1)
        & (imagen <= 1)
    )

    if not visibles.any():
        raise ValueError(
            "La reproyección no produjo píxeles visibles"
        )

    Image.fromarray(
        _colorear(imagen, visibles, indice),
        "RGBA",
    ).save(png_destino)

    oeste = transf.c
    norte = transf.f
    este = oeste + transf.a * ancho
    sur = norte + transf.e * alto

    return {
        "promedio": round(promedio, 6),
        "pixeles_validos": cantidad,
        "cobertura_parcela": round(cobertura, 4),
        "bounds": [
            [round(sur, 8), round(oeste, 8)],
            [round(norte, 8), round(este, 8)],
        ],
        "escala": [
            {"valor": valor, "color": color}
            for valor, color in ESCALAS[indice]
        ],
        "escala_tipo": "fija_absoluta",
        "version_analisis": VERSION_ANALISIS,
        "perimetro_recortado": True,
        "perimetro_hash": firma_perimetro(perimetro),
    }
