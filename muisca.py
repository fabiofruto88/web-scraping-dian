import requests
from bs4 import BeautifulSoup
import json
import time
import re
import unicodedata
import argparse
import sys

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

BASE_URL = "https://muisca.dian.gov.co/WebArancel"


def _collapse_ws(texto: str) -> str:
    return re.sub(r"\s+", " ", (texto or "").strip())


def _normalize_key(texto: str) -> str:
    """Minúsculas + sin tildes para comparaciones robustas."""
    texto = (texto or "").strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return _collapse_ws(texto)


_RE_FECHA_CORTA = re.compile(r"^\d{2}-[a-zA-Z]{3}-\d{4}$")

_HEADER_KEYWORDS = {
    "concepto",
    "tarifa",
    "formula",
    "desde",
    "hasta",
    "acuerdo",
    "pais",
    "documento",
    "descripcion",
    "descripción",
    "termino",
    "tasa",
    "gravamen",
    "indice",
    "índice",
    "requisito",
}

# Todos los popups con su componente, para importaciones (reg=1) y exportaciones (reg=2)
POPUPS_IMPORTACION = {
    "gravamen":        (f"{BASE_URL}/DefGravamenPopUp.faces",              3),
    "iva":             (f"{BASE_URL}/DefIvaPopUp.faces",                   4),
    "tlc":             (f"{BASE_URL}/DefTarifasAcuerdoPopUp.faces",        6),
    "regimen_comercio":(f"{BASE_URL}/DefRegimenComercioPopUp.faces",       9),
    "indice_alfabetico":(f"{BASE_URL}/DefIndiceAlfabeticoMedidaPopUp.faces",15),
    "documentos":      (f"{BASE_URL}/DefDocumentosPopUp.faces",           10),
    "descripciones":   (f"{BASE_URL}/DefDescripcionesMinimasPopUp.faces", 29),
}

POPUPS_EXPORTACION = {
    "indice_alfabetico_expo":(f"{BASE_URL}/DefIndiceAlfabeticoMedidaPopUp.faces",15),
    "documentos_expo":       (f"{BASE_URL}/DefDocumentosPopUp.faces",           10),
    "descripciones_expo":    (f"{BASE_URL}/DefDescripcionesMinimasPopUp.faces", 29),
}


def obtener_perfil(codigo):
    url_form = f"{BASE_URL}/DefConsultaGeneralNomenclaturas.faces"
    r = session.get(url_form, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    view_el = soup.find("input", {"name": "com.sun.faces.VIEW"})
    if not view_el or not view_el.get("value"):
        raise RuntimeError("No se encontró el token com.sun.faces.VIEW en el formulario")
    view_token = view_el["value"]

    formulario = {
        "vistaSalidaLugarEmbarque:formConsultaNomenclaturas:codNomenclatura": codigo,
        "vistaSalidaLugarEmbarque:formConsultaNomenclaturas": "vistaSalidaLugarEmbarque:formConsultaNomenclaturas",
        "com.sun.faces.VIEW": view_token,
        "vistaSalidaLugarEmbarque:formConsultaNomenclaturas:btConsultarNomenclatura.x": "10",
        "vistaSalidaLugarEmbarque:formConsultaNomenclaturas:btConsultarNomenclatura.y": "10",
    }

    r2 = session.post(
        f"{BASE_URL}/DefResultadoConsNomenclaturas.faces",
        data=formulario, timeout=45,
        headers={"Referer": url_form}
    )
    r2.raise_for_status()
    soup2 = BeautifulSoup(r2.text, "lxml")

    # ── Descripción: buscar todas las celdas con clase tdNorm_43
    # y tomar la que tenga más texto (es la descripción real, no el título)
    descripcion = None
    celdas_desc = soup2.find_all("td", class_="tdNorm_43")
    mejor = ""
    for celda in celdas_desc:
        # Usar separador para evitar que se pegue texto entre nodos.
        texto = _collapse_ws(celda.get_text(" ", strip=True))
        if len(texto) > len(mejor):
            mejor = texto
    if mejor:
        descripcion = mejor

    # ── Unidad física: buscar dentro de tablas con clase 'consulta'
    # la fila cuya primera celda diga exactamente 'Unidad física'
    unidad = None
    for tabla in soup2.find_all("table", class_="consulta"):
        for fila in tabla.find_all("tr"):
            celdas = fila.find_all("td")
            if len(celdas) >= 2:
                primera = _normalize_key(celdas[0].get_text(" ", strip=True))
                if "unidad" in primera and "fisica" in primera:
                    unidad = _collapse_ws(celdas[1].get_text(" ", strip=True))
                    break
        if unidad:
            break

    # ── Parámetros internos que MUISCA mete en campos ocultos ──
    def campo(nombre_parcial):
        el = soup2.find("input", {"name": lambda n: n and nombre_parcial in n})
        return el["value"] if el else None

    params = {
        "nomenclatura":    campo("strNomenclatura"),
        "codNomenclatura": campo("codNomenclatura"),
        "fechaConsulta":   campo("strFechaConsulta"),
    }

    print(f"  ID interno  : {params['nomenclatura']}")
    print(f"  Código      : {params['codNomenclatura']}")
    print(f"  Fecha       : {params['fechaConsulta']}")
    print(f"  Descripción : {str(descripcion)[:80]}...")
    print(f"  Unidad      : {unidad}")

    return descripcion, unidad, params


def consultar_popup(url, componente, params, regimen=1):
    r = session.get(url, params={
        "nomenclatura":                params["nomenclatura"],
        "codNomenclatura":             params["codNomenclatura"],
        "componente":                  str(componente),
        "regimen":                     str(regimen),
        "fechaConsulta":               params["fechaConsulta"],
        "modoPresentacionSeleccionBO": "dialogo",
    }, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    def _is_navigation_row(tr) -> bool:
        """Filas de paginación/filtros suelen tener <select> o inputs de texto/submit.
        No debe excluir filas de datos que traen botones (ej. <input type="image">).
        """
        if tr.find("select"):
            return True
        inputs = tr.find_all("input")
        if not inputs:
            return False
        for inp in inputs:
            t = (inp.get("type") or "").strip().lower()
            if t in {"text", "hidden", "submit", "button", "password", "search"}:
                return True
        return False

    def _extract_row_texts(tr):
        tds = tr.find_all("td")
        if not tds:
            return []
        textos_local = []
        for td in tds:
            # Si la celda solo tiene una imagen, es la columna Leg → ignorar
            if td.find("img") and not td.get_text(strip=True):
                continue
            textos_local.append(_collapse_ws(td.get_text(" ", strip=True)))
        while textos_local and not textos_local[-1]:
            textos_local.pop()
        return textos_local

    # Buscar la tabla con datos reales — escoger la de mayor "densidad" de filas/columnas
    tabla_datos = None
    mejor_score = (-1, -1)
    for tabla in soup.find_all("table"):
        tbody = tabla.find("tbody")
        if not tbody:
            continue

        filas = []
        max_cols = 0
        for tr in tbody.find_all("tr"):
            # Saltar filas de navegación
            if _is_navigation_row(tr):
                continue
            textos = _extract_row_texts(tr)
            if not textos or not any(textos):
                continue
            filas.append(textos)
            max_cols = max(max_cols, len(textos))

        score = (len(filas), max_cols)
        if score > mejor_score:
            mejor_score = score
            tabla_datos = tabla

    if not tabla_datos:
        return []

    # Encabezados (soporta <thead> con <th> o <td>, y fallback heurístico)
    encabezados = []
    thead = tabla_datos.find("thead")
    if thead:
        hdr_cells = thead.find_all(["th", "td"])
        encabezados = [_collapse_ws(c.get_text(" ", strip=True)) for c in hdr_cells]
    else:
        ths = tabla_datos.find_all("th")
        if ths:
            encabezados = [_collapse_ws(th.get_text(" ", strip=True)) for th in ths]

    encabezados = [e for e in encabezados if e and e != "Leg"]

    tbody = tabla_datos.find("tbody")
    if not tbody:
        return []

    # Tomar filas de datos ya filtradas
    filas_texto = []
    for tr in tbody.find_all("tr"):
        if _is_navigation_row(tr):
            continue
        textos = _extract_row_texts(tr)
        if not textos or not any(textos):
            continue

        # Filtrar filas basura típicas (p.ej. una fila suelta con solo una fecha o "...")
        non_empty = [t for t in textos if t]
        if len(non_empty) == 1:
            solo = non_empty[0]
            if solo == "..." or _RE_FECHA_CORTA.match(solo):
                continue

        filas_texto.append(textos)

    if not filas_texto:
        return []

    # Heurística de encabezados si no vinieron en <th>/<thead>
    if not encabezados:
        primera = filas_texto[0]
        norm_cells = [_normalize_key(c) for c in primera]
        looks_like_header = any(
            any(k in cell for k in _HEADER_KEYWORDS) for cell in norm_cells
        )
        if looks_like_header:
            encabezados = primera
            filas_texto = filas_texto[1:]
        else:
            encabezados = [f"col_{i+1}" for i in range(len(primera))]

    # Encabezados limpios (quitar vacíos)
    encabezados = [e for e in encabezados if str(e).strip()]

    resultado = []
    for textos in filas_texto:

        # ── Estrategia flexible de mapeo ──
        if len(textos) == len(encabezados):
            # Caso ideal: coinciden exactamente
            fila_dict = dict(zip(encabezados, textos))

        elif len(textos) < len(encabezados):
            # Menos celdas que encabezados (ej: régimen de comercio)
            # Rellenar con vacío al final
            textos_ext = textos + [""] * (len(encabezados) - len(textos))
            fila_dict = dict(zip(encabezados, textos_ext))

        else:
            # Más celdas que encabezados → tomar solo las primeras
            fila_dict = dict(zip(encabezados, textos[:len(encabezados)]))

        # Quitar claves vacías
        fila_dict = {k: v for k, v in fila_dict.items() if k.strip()}
        resultado.append(fila_dict)

    return resultado

def scrape_completo(codigo):
    print(f"\n{'='*55}")
    print(f"  SCRAPING: {codigo}")
    print(f"{'='*55}")

    descripcion, unidad, params = obtener_perfil(codigo)

    if not params["nomenclatura"]:
        print("ERROR: no se obtuvo ID interno.")
        return

    time.sleep(1)

    # ── Consultar importaciones ──
    print("\n  [IMPORTACIONES]")
    impo = {}
    for nombre, (url, componente) in POPUPS_IMPORTACION.items():
        print(f"    → {nombre}...", end=" ", flush=True)
        datos = consultar_popup(url, componente, params, regimen=1)
        impo[nombre] = datos
        print(f"{len(datos)} registros")
        time.sleep(1.2)

    # ── Consultar exportaciones ──
    print("\n  [EXPORTACIONES]")
    expo = {}
    for nombre, (url, componente) in POPUPS_EXPORTACION.items():
        print(f"    → {nombre}...", end=" ", flush=True)
        datos = consultar_popup(url, componente, params, regimen=2)
        expo[nombre] = datos
        print(f"{len(datos)} registros")
        time.sleep(1.2)

    # ── JSON final limpio y completo ──
    resultado = {
        "codigo":      codigo,
        "descripcion": descripcion,
        "unidad":      unidad,
        "importaciones": {
            "gravamen":         impo.get("gravamen", []),
            "iva":              impo.get("iva", []),
            "tlc":              impo.get("tlc", []),
            "regimen_comercio": impo.get("regimen_comercio", []),
            "indice_alfabetico":impo.get("indice_alfabetico", []),
            "documentos":       impo.get("documentos", []),
            "descripciones":    impo.get("descripciones", []),
        },
        "exportaciones": {
            "indice_alfabetico":expo.get("indice_alfabetico_expo", []),
            "documentos":       expo.get("documentos_expo", []),
            "descripciones":    expo.get("descripciones_expo", []),
        }
    }

    nombre_archivo = f"subpartida_{codigo}.json"
    with open(nombre_archivo, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*55}")
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    print(f"\n✓ Guardado en: {nombre_archivo}")


def _main():
    parser = argparse.ArgumentParser(description="Scraper MUISCA DIAN (WebArancel)")
    parser.add_argument(
        "codigos",
        nargs="*",
        help="Uno o más códigos arancelarios de 10 dígitos (ej: 6403510000)",
    )
    parser.add_argument(
        "--dump-popups",
        default="",
        help=(
            "Lista separada por coma de popups a dumpear (ej: documentos,descripciones). "
            "Genera archivos HTML para diagnóstico."
        ),
    )
    parser.add_argument(
        "--dump-dir",
        default="dumps",
        help="Carpeta de salida para HTML (por defecto: dumps)",
    )
    args = parser.parse_args()

    codigos = list(args.codigos or [])
    if not codigos:
        # Uso simple sin parámetros: pedir al usuario si hay consola interactiva.
        if sys.stdin.isatty():
            entrada = input("Código(s) arancelario(s) 10 dígitos (separados por coma): ").strip()
            if entrada:
                codigos = [c.strip() for c in entrada.split(",") if c.strip()]

    # Fallback seguro (no interactivo)
    if not codigos:
        codigos = ["6403510000"]

    dump_keys = [k.strip() for k in (args.dump_popups or "").split(",") if k.strip()]
    dump_keys_set = set(dump_keys)

    for codigo in codigos:
        if not dump_keys_set:
            scrape_completo(codigo)
            continue

        # Modo diagnóstico: primero obtener params y luego dumpear HTML crudo
        print(f"\n{'='*55}")
        print(f"  DUMP: {codigo}")
        print(f"{'='*55}")
        _, _, params = obtener_perfil(codigo)
        if not params.get("nomenclatura"):
            print("ERROR: no se obtuvo ID interno.")
            continue

        import os
        os.makedirs(args.dump_dir, exist_ok=True)

        # Construir un mapa de popups por nombre
        all_popups = {}
        all_popups.update(POPUPS_IMPORTACION)
        all_popups.update(POPUPS_EXPORTACION)

        for nombre, (url, componente) in all_popups.items():
            # Permitir pedir por alias base (documentos/descripciones/indice_alfabetico)
            base = nombre.replace("_expo", "")
            if nombre not in dump_keys_set and base not in dump_keys_set:
                continue

            for regimen in (1, 2):
                # Respetar qué popups aplican realmente
                if regimen == 2 and nombre in POPUPS_IMPORTACION and nombre not in (
                    "indice_alfabetico",
                    "documentos",
                    "descripciones",
                ):
                    continue
                if regimen == 1 and nombre in POPUPS_EXPORTACION:
                    continue

                r = session.get(
                    url,
                    params={
                        "nomenclatura": params["nomenclatura"],
                        "codNomenclatura": params["codNomenclatura"],
                        "componente": str(componente),
                        "regimen": str(regimen),
                        "fechaConsulta": params["fechaConsulta"],
                        "modoPresentacionSeleccionBO": "dialogo",
                    },
                    timeout=30,
                )
                r.raise_for_status()
                out = os.path.join(args.dump_dir, f"{codigo}_{nombre}_reg{regimen}.html")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"  ✓ dump {nombre} reg{regimen} -> {out}")


if __name__ == "__main__":
    _main()