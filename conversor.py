#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
conversor.py - Convierte PDFs en Markdown ligero para pasárselos a una IA
gastando el mínimo de tokens.

Qué hace:
  * Extrae el texto como Markdown (títulos, listas, código, tablas).
  * Quita cabeceras, pies y números de página repetidos; une líneas partidas.
  * Capturas de texto (terminal, código, mensajes)  -> OCR -> texto (0 imágenes).
  * Esquemas / gráficos / fotos                      -> imagen recortada y reducida.
  * Páginas escaneadas                               -> OCR de la página entera.
  * Opcional: junta varios PDFs en un solo .md y las imágenes en pocas láminas.
  * Al final muestra una estimación de tokens antes/después.

Todo es local. No usa ninguna IA ni API.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import pytesseract
except ImportError:  # el programa funciona sin OCR
    pytesseract = None

# ----------------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------------
DPI_FIGURA = 200            # resolución a la que se recortan las figuras
TOKENS_MAX_IMAGEN = 1600    # Claude escala las imágenes grandes hasta ~1.600 tokens
CHARS_POR_TOKEN = 3.3       # estimación conservadora para español/euskera/código

MONO = re.compile(r"(mono|courier|consolas|menlo|lucida\s*console|typewriter|code)", re.I)
BOLD = re.compile(r"(bold|black|heavy|semibold|demi)", re.I)
VIÑETA = re.compile(r"^\s*([•●▪■◦○·\-–—*\uf0b7\uf0a7\uf0d8])\s+")
NUMERADA = re.compile(r"^\s*(\d{1,3}[.)])\s+")
NUM_PAGINA = re.compile(r"^(p[aá]g(ina)?\.?\s*)?\d{1,4}(\s*(/|de|of)\s*\d{1,4})?$", re.I)
REEMPLAZOS = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
              "\u00a0": " ", "\u200b": "", "\u00ad": ""}


def tok_texto(s):
    return int(len(s) / CHARS_POR_TOKEN) + 1


def tok_imagen(w, h):
    return min(TOKENS_MAX_IMAGEN, int(w * h / 750))


CID = re.compile(r"\(cid:\d+\)")


def limpiar(s):
    for a, b in REEMPLAZOS.items():
        s = s.replace(a, b)
    # glifos sin mapa Unicode: al principio de línea suelen ser viñetas
    s = re.sub(r"^\s*\(cid:\d+\)\s*", "• ", s)
    s = CID.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


# ----------------------------------------------------------------------------
# Geometría
# ----------------------------------------------------------------------------
def centro_dentro(b, caja, margen=0.0):
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return (caja[0] - margen <= cx <= caja[2] + margen and
            caja[1] - margen <= cy <= caja[3] + margen)


def interseca(a, b, m=0.0):
    return not (a[2] + m < b[0] or b[2] + m < a[0] or a[3] + m < b[1] or b[3] + m < a[1])


def union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def fusionar_cajas(items, margen):
    """items: lista de dicts con 'caja'. Fusiona los que se tocan; suma contadores."""
    items = [dict(i) for i in items]
    cambiado = True
    while cambiado:
        cambiado = False
        out = []
        while items:
            a = items.pop()
            i = 0
            while i < len(items):
                if interseca(a["caja"], items[i]["caja"], margen):
                    b = items.pop(i)
                    a["caja"] = union(a["caja"], b["caja"])
                    for k in a:
                        if k != "caja" and isinstance(a[k], (int, bool)):
                            a[k] = a[k] + b[k] if not isinstance(a[k], bool) else (a[k] or b[k])
                    cambiado = True
                    i = 0
                else:
                    i += 1
            out.append(a)
        items = out
    return items


# ----------------------------------------------------------------------------
# OCR (Tesseract)
# ----------------------------------------------------------------------------
TESSDATA_LOCAL = Path(__file__).resolve().parent / "tessdata"   # idiomas descargados por --instalar-tesseract
IDIOMAS_DESCARGA = ("eng", "spa", "eus")


def buscar_tesseract():
    """Ruta de tesseract(.exe) si está instalado (PATH o ubicaciones típicas de Windows)."""
    en_path = shutil.which("tesseract")
    if en_path:
        return en_path
    cands = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
             r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
    for c in cands:
        if Path(c).exists():
            return c
    return None


def instalar_tesseract():
    """Instala Tesseract (winget en Windows) y descarga los idiomas a ./tessdata."""
    print("== Instalación de Tesseract ==")
    exe = buscar_tesseract()
    if exe:
        print(f"Tesseract ya está instalado: {exe}")
    elif sys.platform == "win32":
        if shutil.which("winget"):
            print("Instalando con winget (Windows puede pedirte permiso de administrador)...")
            r = subprocess.run(["winget", "install", "-e", "--id", "UB-Mannheim.TesseractOCR",
                                "--accept-source-agreements", "--accept-package-agreements"])
            exe = buscar_tesseract()
            if not exe:
                print(f"\nNo se pudo instalar automáticamente (código {r.returncode}).")
        else:
            print("No encuentro 'winget' en este equipo.")
        if not exe:
            print("Instálalo a mano: https://github.com/UB-Mannheim/tesseract/wiki")
            print("Después vuelve a ejecutar:  python conversor.py --instalar-tesseract")
            return 1
    else:
        print("Instala Tesseract con tu gestor de paquetes (apt install tesseract-ocr / brew install tesseract)"
              " y vuelve a ejecutar este comando para descargar los idiomas.")
    TESSDATA_LOCAL.mkdir(exist_ok=True)
    fallos = 0
    for l in IDIOMAS_DESCARGA:
        dest = TESSDATA_LOCAL / f"{l}.traineddata"
        if dest.exists() and dest.stat().st_size > 500_000:
            print(f"Idioma {l}: ya descargado")
            continue
        urls = (f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{l}.traineddata",
                f"https://github.com/tesseract-ocr/tessdata_fast/raw/main/{l}.traineddata")
        tmp = dest.with_suffix(".part")
        error = None
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f)
                if tmp.stat().st_size < 500_000:
                    raise OSError("descarga incompleta")
                tmp.replace(dest)
                error = None
                print(f"Idioma {l}: descargado")
                break
            except Exception as e:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
                error = e
        if error is not None:
            fallos += 1
            print(f"Idioma {l}: ERROR ({error.__class__.__name__}: {error})")
    ocr = OCR("+".join(IDIOMAS_DESCARGA))
    if ocr.ok and not ocr.faltan and not fallos:
        print(f"\nListo. Tesseract funciona con: {', '.join(IDIOMAS_DESCARGA)}")
        return 0
    if fallos:
        print(f"\nINCOMPLETO: no se pudieron descargar {fallos} idioma(s). Comprueba tu conexión/proxy "
              "y vuelve a ejecutar este comando, o copia los .traineddata a la carpeta 'tessdata' "
              "(https://github.com/tesseract-ocr/tessdata_fast).")
    if not ocr.ok:
        print(f"Tesseract aún no funciona: {ocr.motivo}")
    elif ocr.faltan:
        print(f"Faltan idiomas: {', '.join(ocr.faltan)}")
    return 1


class OCR:
    def __init__(self, idiomas, ruta=None, activo=True):
        self.ok = False
        self.motivo = ""
        self.lang = idiomas
        self.faltan = []
        if not activo:
            self.motivo = "OCR desactivado (--sin-ocr)"
            return
        if pytesseract is None:
            self.motivo = "falta el paquete pytesseract (pip install pytesseract)"
            return
        if ruta:
            pytesseract.pytesseract.tesseract_cmd = ruta
        else:
            exe = buscar_tesseract()
            if exe:
                pytesseract.pytesseract.tesseract_cmd = exe
        if any(TESSDATA_LOCAL.glob("*.traineddata")):
            os.environ["TESSDATA_PREFIX"] = str(TESSDATA_LOCAL)
        try:
            disponibles = set(pytesseract.get_languages(config=""))
        except Exception as e:  # noqa: BLE001
            self.motivo = "no encuentro Tesseract. Instálalo con:  python conversor.py --instalar-tesseract"
            return
        pedidos = [l for l in idiomas.split("+") if l]
        ok = [l for l in pedidos if l in disponibles]
        self.faltan = [l for l in pedidos if l not in disponibles]
        if not ok:
            self.motivo = (f"idiomas de Tesseract no instalados: {', '.join(self.faltan)}. "
                           "Instálalos con:  python conversor.py --instalar-tesseract")
            return
        self.lang = "+".join(ok)
        self.ok = True

    def palabras(self, img, psm):
        # Canal máximo (en vez de luminosidad): conserva texto azul/verde de terminales.
        g = Image.fromarray(np.asarray(img.convert("RGB")).max(axis=2))
        if np.asarray(g).mean() < 115:       # fondo oscuro (terminales): invertir
            g = ImageOps.invert(g)
        g = ImageOps.autocontrast(g, cutoff=1)
        w, h = g.size
        f = 3 if max(w, h) < 900 else (2 if max(w, h) < 1600 else 1)
        if f > 1:
            g = g.resize((w * f, h * f), Image.LANCZOS)
        pad = 30                              # sin margen, Tesseract pierde el 1.er carácter
        g = ImageOps.expand(g, border=pad, fill=255)
        d = pytesseract.image_to_data(g, lang=self.lang, config=f"--psm {psm}",
                                      output_type=pytesseract.Output.DICT)
        res = []
        for i, t in enumerate(d["text"]):
            t = t.strip()
            if not t:
                continue
            try:
                c = float(d["conf"][i])
            except (TypeError, ValueError):
                continue
            if c < 0:
                continue
            res.append(dict(t=t, c=c, x=(d["left"][i] - pad) / f, y=(d["top"][i] - pad) / f,
                            w=d["width"][i] / f, h=d["height"][i] / f,
                            blk=d["block_num"][i], par=d["par_num"][i], lin=d["line_num"][i]))
        return res, (w, h)


def lineas_ocr(palabras, conf_min_linea=-1):  # sin filtro: la confianza de Tesseract no es fiable por línea
    """Agrupa palabras en líneas (en orden de Tesseract) y descarta líneas basura."""
    lineas = {}
    for p in palabras:
        lineas.setdefault((p["blk"], p["par"], p["lin"]), []).append(p)
    out = []
    for k, ws in lineas.items():
        conf = sum(w["c"] for w in ws) / len(ws)
        if conf < conf_min_linea:
            continue
        out.append((k, " ".join(w["t"] for w in ws), min(w["x"] for w in ws)))
    return out


def analizar_captura(palabras, dim):
    """Devuelve (es_captura_de_texto, confianza, texto)."""
    if not palabras:
        return False, 0.0, ""
    W, H = dim
    conf = sum(p["c"] for p in palabras) / len(palabras)
    cobertura = sum(p["w"] * p["h"] for p in palabras) / max(1, W * H)
    ls = lineas_ocr(palabras)
    if not ls:
        return False, conf, ""
    xs = [x for _, _, x in ls]
    alineadas = 0.0
    if len(xs) >= 2:
        x0 = min(xs)
        alineadas = sum(1 for x in xs if x - x0 < 0.04 * W) / len(xs)
    es_texto = (len(palabras) >= 2 and
                (cobertura >= 0.08 or (len(ls) >= 2 and alineadas >= 0.6)))
    return es_texto, conf, "\n".join(t for _, t, _ in ls)


def parrafos_ocr(palabras):
    """Texto de una página escaneada: reflujo de líneas dentro de cada párrafo."""
    bloques, orden = {}, []
    for k, texto, _ in lineas_ocr(palabras):
        kk = (k[0], k[1])
        if kk not in bloques:
            bloques[kk] = []
            orden.append(kk)
        bloques[kk].append(texto)
    out = []
    for kk in orden:
        t = ""
        for l in bloques[kk]:
            if t.endswith("-") and len(t) > 1 and t[-2].isalpha() and l[:1].islower():
                t = t[:-1] + l
            else:
                t = (t + " " + l).strip()
        out.append(t)
    return "\n\n".join(out)


# ----------------------------------------------------------------------------
# Imágenes
# ----------------------------------------------------------------------------
def renderizar(doc, idx, dpi):
    return doc[idx].render(scale=dpi / 72).to_pil().convert("RGB")


def recortar_blanco(img, pad=6):
    a = np.asarray(img.convert("L"))
    borde = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    if borde.mean() < 200:      # fondo oscuro: no recortar
        return img
    ys, xs = np.where(a < 240)
    if len(xs) == 0:
        return img
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + 1 + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + 1 + pad)
    return img.crop((x0, y0, x1, y1))


def reducir(img, max_lado, escala_max=1.0):
    lado = max(img.size)
    f = min(1.0, max_lado / lado, escala_max)
    # evitar que Claude tenga que reescalar: máx ~1,2 megapíxeles
    mp = img.width * img.height * f * f
    if mp > TOKENS_MAX_IMAGEN * 750:
        f *= (TOKENS_MAX_IMAGEN * 750 / mp) ** 0.5
    if f < 1.0:
        img = img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))), Image.LANCZOS)
    return img


def huella(img):
    p = img.convert("L").resize((24, 24), Image.BILINEAR)
    return hashlib.md5(np.asarray(p).tobytes()).hexdigest()


def fuente(tam):
    try:
        return ImageFont.load_default(size=tam)
    except TypeError:
        return ImageFont.load_default()


def hacer_laminas(figuras, max_alto=1500):
    """figuras: lista de (id, pagina, PIL). Apila varias por lámina con etiqueta."""
    etiqueta_h, hueco = 22, 8
    ancho = max(f[2].width for f in figuras)
    ancho = max(ancho, 200)
    # empaquetado por filas: las imágenes pequeñas van una junto a otra
    hojas, filas, alto = [], [], 0
    fila, x = [], 0
    for f in figuras:
        w = max(f[2].width, 110)
        if fila and x + w > ancho:
            filas.append(fila)
            fila, x = [], 0
        fila.append(f)
        x += w + hueco
    if fila:
        filas.append(fila)
    lote = []
    for fila in filas:
        h = max(f[2].height for f in fila) + etiqueta_h + hueco
        if lote and alto + h > max_alto:
            hojas.append(lote)
            lote, alto = [], 0
        lote.append(fila)
        alto += h
    if lote:
        hojas.append(lote)
    salida = []
    fnt = fuente(15)
    for lote in hojas:
        total = sum(max(f[2].height for f in fila) + etiqueta_h + hueco for fila in lote)
        wmax = max(sum(max(f[2].width, 110) + hueco for f in fila) - hueco for fila in lote)
        hoja = Image.new("RGB", (wmax, total), "white")
        d = ImageDraw.Draw(hoja)
        y = 0
        for fila in lote:
            x = 0
            for fid, pag, im in fila:
                w = max(im.width, 110)
                d.rectangle([x, y, x + w, y + etiqueta_h - 2], fill=(225, 225, 225))
                d.text((x + 4, y + 2), f"{fid}  (p.{pag})", fill="black", font=fnt)
                hoja.paste(im, (x, y + etiqueta_h))
                x += w + hueco
            y += max(f[2].height for f in fila) + etiqueta_h + hueco
        salida.append((hoja, [f[0] for fila in lote for f in fila]))
    return salida


# ----------------------------------------------------------------------------
# Detección de figuras vectoriales
# ----------------------------------------------------------------------------
def figuras_vectoriales(pg, W, H, excluir, area_min):
    prims = []
    for tipo, objs in (("curva", pg.curves), ("linea", pg.lines), ("rect", pg.rects)):
        for ob in objs:
            b = (ob["x0"], ob["top"], ob["x1"], ob["bottom"])
            w, h = b[2] - b[0], b[3] - b[1]
            if max(w, h) < 4:
                continue
            if any(centro_dentro(b, e) for e in excluir):
                continue
            t = tipo
            if t == "rect":
                if w * h > 0.5 * W * H:
                    continue                     # fondos de página
                if min(w, h) < 1.5:
                    t = "linea"
            prims.append(dict(caja=b, curva=int(t == "curva"),
                              linea=int(t == "linea"),
                              diag=int(t == "linea" and w > 3 and h > 3),
                              rect=int(t == "rect" and min(w, h) >= 4)))
    if not prims:
        return []
    if len(prims) > 4000:
        prims = prims[:4000]
    # agrupar por proximidad (barrido por x para no hacer O(n^2) completo)
    prims.sort(key=lambda p: p["caja"][0])
    padre = list(range(len(prims)))

    def raiz(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i
    M = 6
    for i, a in enumerate(prims):
        for j in range(i + 1, len(prims)):
            b = prims[j]
            if b["caja"][0] > a["caja"][2] + M:
                break
            if interseca(a["caja"], b["caja"], M):
                padre[raiz(j)] = raiz(i)
    grupos = {}
    for i, p in enumerate(prims):
        grupos.setdefault(raiz(i), []).append(p)
    figs = []
    for g in grupos.values():
        caja = g[0]["caja"]
        for p in g[1:]:
            caja = union(caja, p["caja"])
        nc, nl, nd, nr = (sum(p[k] for p in g) for k in ("curva", "linea", "diag", "rect"))
        es_fig = (nc >= 3 or (nr >= 3 and nl >= 2) or nd >= 3 or (nr >= 4 and nc >= 1))
        w, h = caja[2] - caja[0], caja[3] - caja[1]
        if es_fig and w >= 40 and h >= 30 and w * h >= area_min * W * H:
            figs.append(dict(caja=caja, curvas=nc, diag=nd))
    return figs


# ----------------------------------------------------------------------------
# Tablas
# ----------------------------------------------------------------------------
def tabla_md(filas):
    filas = [[re.sub(r"\s+", " ", (c or "")).strip() for c in f] for f in filas]
    filas = [f for f in filas if any(f)]
    if len(filas) < 2:
        return None
    n = max(len(f) for f in filas)
    filas = [f + [""] * (n - len(f)) for f in filas]
    # una celda de varias líneas que se partió en dos filas: fila con un único
    # fragmento y la primera celda vacía -> se une a la fila anterior
    fusion = []
    for f in filas:
        if fusion and not f[0] and sum(1 for c in f if c) == 1:
            j = next(i for i, c in enumerate(f) if c)
            fusion[-1][j] = (fusion[-1][j] + " " + f[j]).strip()
        else:
            fusion.append(f)
    filas = fusion
    vacias = [all(not f[j] for f in filas) for j in range(n)]
    filas = [[c for j, c in enumerate(f) if not vacias[j]] for f in filas]
    n = len(filas[0]) if filas else 0
    if n < 2:
        return None
    llenas = sum(1 for f in filas for c in f if c)
    relleno = llenas / (len(filas) * n)
    if relleno < 0.4:
        return None
    sep = "|" + "---|" * n
    cuerpo = ["| " + " | ".join(f) + " |" for f in filas]
    return "\n".join([cuerpo[0], sep] + cuerpo[1:]), relleno


def detectar_tablas(pg):
    """Word y otros programas dibujan bordes duplicados: se prueban varias tolerancias
    y se elige la que da tablas más limpias (menos celdas vacías)."""
    mejor, mejor_score = [], -1.0
    for tol in (8, 3):
        try:
            ts = pg.find_tables(table_settings={
                "vertical_strategy": "lines", "horizontal_strategy": "lines",
                "snap_tolerance": tol, "join_tolerance": tol, "intersection_tolerance": tol})
        except Exception:  # noqa: BLE001
            continue
        res = []
        for t in ts:
            r = tabla_md(t.extract())
            if r:
                res.append((t.bbox, r[0], r[1]))
        if not res:
            continue
        score = sum(x[2] for x in res) / len(res)
        if score > mejor_score + 1e-9:
            mejor, mejor_score = res, score
    return [(b, md) for b, md, _ in mejor]


# ----------------------------------------------------------------------------
# Análisis de página
# ----------------------------------------------------------------------------
def analizar_pagina(pg, doc, idx, o, ocr, aviso):
    W, H = float(pg.width), float(pg.height)
    lineas = pg.extract_text_lines(return_chars=True, strip=True)
    nchars = sum(len(l["text"].replace(" ", "")) for l in lineas)
    raw_text = " ".join(l["text"] for l in lineas)

    imgs = []
    for im in pg.images:
        b = (max(0, im["x0"]), max(0, im["top"]), min(W, im["x1"]), min(H, im["bottom"]))
        if b[2] > b[0] and b[3] > b[1]:
            imgs.append((b, im))
    cobertura = sum((b[2] - b[0]) * (b[3] - b[1]) for b, _ in imgs) / (W * H)

    res = dict(items=[], raw_tokens=tok_texto(raw_text), escaneada=False, ocr_paginas=0,
               caps_ocr=[], caps_conf=[])

    # --- página escaneada -----------------------------------------------------
    if nchars < 25 and cobertura > 0.5:
        res["escaneada"] = True
        if ocr.ok:
            pil = renderizar(doc, idx, o.dpi_ocr)
            pals, dim = ocr.palabras(pil, 3)
            texto = parrafos_ocr(pals)
            conf = sum(p["c"] for p in pals) / len(pals) if pals else 0
            if texto.strip():
                res["items"].append(dict(kind="bloque", top=0, md=limpiar_multilinea(texto)))
                res["ocr_paginas"] = 1
                res["conf_pagina"] = conf
                if conf < 70:
                    aviso(f"página {idx + 1}: OCR con poca confianza ({conf:.0f}%), revísala")
                return res
        if o.imagenes == "omitir":
            aviso(f"página {idx + 1}: escaneada y sin OCR disponible; NO tiene texto y se omite "
                  f"({ocr.motivo or 'sin OCR'})")
            return res
        # sin OCR: se conserva la página como imagen reducida
        pil = renderizar(doc, idx, 110)
        res["items"].append(dict(kind="fig", top=0, tipo="imagen", img=reducir(pil, o.max_lado),
                                 labels=[], raster=True, caja=(0, 0, W, H)))
        aviso(f"página {idx + 1}: escaneada y sin OCR disponible; se deja como imagen"
              + (f" ({ocr.motivo})" if ocr.motivo else ""))
        return res

    # --- tablas ----------------------------------------------------------------
    tablas = [] if o.sin_tablas else detectar_tablas(pg)
    for bbox, md in tablas:
        res["items"].append(dict(kind="bloque", top=bbox[1], md=md))

    # --- figuras ---------------------------------------------------------------
    candidatas = []
    for b, im in imgs:
        w, h = b[2] - b[0], b[3] - b[1]
        if w * h < o.area_minima * W * H or min(w, h) < 20:
            continue
        if w * h > 0.9 * W * H and nchars >= 25:
            continue                                    # fondo decorativo
        src = im.get("srcsize") or (None, None)
        esc_max = (src[0] / (w / 72 * DPI_FIGURA)) if src[0] else 1.0
        candidatas.append(dict(caja=b, raster=1, vector=0, esc=esc_max))
    for fv in figuras_vectoriales(pg, W, H, [t[0] for t in tablas], o.area_minima):
        caja = fv["caja"]
        if fv["curvas"] == 0 and fv["diag"] == 0:
            # solo rectángulos y rayas rectas: ¿es un recuadro de texto (aviso, nota)?
            dentro = [len(l["text"]) for l in lineas
                      if centro_dentro((l["x0"], l["top"], l["x1"], l["bottom"]), caja)]
            if len(dentro) >= 2 and sum(dentro) / len(dentro) > 30:
                continue          # es texto enmarcado: se queda como texto normal
        candidatas.append(dict(caja=caja, raster=0, vector=1, esc=1.0))
    figuras = fusionar_cajas(candidatas, 4)
    for f in figuras:
        f["esc"] = min(f["esc"], 1.0) if f["raster"] and not f["vector"] else 1.0

    pagina_img = renderizar(doc, idx, DPI_FIGURA) if figuras else None
    s = pagina_img.width / W if pagina_img else 1
    ocupadas = [(t[0], None) for t in tablas]
    cajas_fig = []
    for f in figuras:
        b = f["caja"]
        crop = pagina_img.crop((int(b[0] * s) - 2, int(b[1] * s) - 2,
                                int(b[2] * s) + 2, int(b[3] * s) + 2))
        item = dict(kind="fig", top=b[1], caja=b, raster=bool(f["raster"] and not f["vector"]),
                    labels=[], tipo="imagen", hash=None)
        if o.imagenes == "omitir":
            item["tipo"] = "omitir"
        else:
            hecho = False
            if item["raster"] and ocr.ok and o.imagenes == "auto":
                pals, dim = ocr.palabras(crop, 6)
                es_txt, conf, texto = analizar_captura(pals, dim)
                if es_txt and conf >= o.conf_ocr:
                    item.update(tipo="texto", texto=texto, conf=conf)
                    res["caps_ocr"].append(idx + 1)
                    res["caps_conf"].append(conf)
                    if conf < 75:
                        aviso(f"página {idx + 1}: captura leída por OCR con confianza {conf:.0f}% "
                              "(revisa símbolos, o usa --imagenes mantener)")
                    hecho = True
            if not hecho:
                item["hash"] = huella(crop)
                item["img"] = reducir(recortar_blanco(crop), o.max_lado, f["esc"])
        res["items"].append(item)
        cajas_fig.append(item)

    # --- líneas de texto (fuera de tablas y figuras) ---------------------------
    for l in lineas:
        b = (l["x0"], l["top"], l["x1"], l["bottom"])
        if any(centro_dentro(b, t[0]) for t in tablas):
            continue
        dentro_fig = None
        for it in cajas_fig:
            if centro_dentro(b, it["caja"], margen=10):
                dentro_fig = it
                break
        if dentro_fig is not None:
            if not dentro_fig["raster"]:
                dentro_fig["labels"].append(limpiar(l["text"]))
            continue
        chars = [c for c in l["chars"] if c["text"].strip()]
        if not chars:
            continue
        tam = Counter(round(c["size"], 1) for c in chars).most_common(1)[0][0]
        fuentes = [c["fontname"] for c in chars]
        mono = sum(bool(MONO.search(f)) for f in fuentes) > len(fuentes) / 2
        negrita = sum(bool(BOLD.search(f)) for f in fuentes) > len(fuentes) * 0.8
        res["items"].append(dict(kind="linea", top=l["top"], bottom=l["bottom"], x0=l["x0"],
                                 x1=l["x1"], text=limpiar(l["text"]) if not mono else
                                 texto_mono(l["chars"]),
                                 size=tam, bold=negrita, mono=mono, zona=_zona(l, H)))
    return res


def texto_mono(chars):
    """Código: reconstruye los espacios a partir de las posiciones de los caracteres
    (así se conservan los espacios internos y alineaciones, sin tocar los símbolos)."""
    cs = sorted((c for c in chars if c["text"].strip()), key=lambda c: c["x0"])
    if not cs:
        return ""
    anchos = sorted(c["x1"] - c["x0"] for c in cs)
    cw = anchos[len(anchos) // 2] or 5.0
    out, prev = [], None
    for c in cs:
        if prev is not None:
            hueco = c["x0"] - prev["x1"]
            if hueco > 0.4 * cw:
                out.append(" " * max(1, int(round(hueco / cw))))
        out.append(c["text"])
        prev = c
    return "".join(out)


def _zona(l, H):
    if l["top"] < 0.07 * H:
        return "cab"
    if l["bottom"] > 0.93 * H:
        return "pie"
    return ""


def limpiar_multilinea(t):
    return "\n".join(limpiar(x) if x.strip() else "" for x in t.split("\n"))


# ----------------------------------------------------------------------------
# Montaje del Markdown
# ----------------------------------------------------------------------------
def montar_pagina(items, ctx):
    bloques = []
    cur = None
    codigo = None

    def vaciar():
        nonlocal cur, codigo
        if cur:
            pref = "- " if cur["tipo"] == "li" else ""
            bloques.append(pref + cur["text"])
            cur = None
        if codigo:
            ls = codigo["ls"]
            base = min(x for x, _ in ls)
            cw = codigo["cw"]
            txt = []
            for x, t in ls:
                if t is None:
                    txt.append("")
                else:
                    txt.append(" " * min(40, int(round((x - base) / cw))) + t)
            bloques.append("```\n" + "\n".join(txt) + "\n```")
            codigo = None

    anterior = None
    for it in sorted(items, key=lambda i: (i["top"], i.get("x0", 0))):
        k = it["kind"]
        if k in ("bloque", "fig"):
            vaciar()
            if k == "bloque":
                bloques.append(it["md"])
            else:
                bloques.append(ctx["emitir_figura"](it))
            anterior = None
            continue
        txt = it["text"]
        if not txt.strip():
            continue
        if it["mono"]:
            if cur:
                vaciar()
            if not codigo:
                codigo = dict(ls=[], cw=5.0, cws=[])
            if anterior is not None and anterior["mono"] and it["top"] - anterior["top"] > 2.2 * it["size"]:
                codigo["ls"].append((it["x0"], None))
            codigo["ls"].append((it["x0"], txt))
            n = len(txt)
            if n > 0:
                codigo["cws"].append((it["x1"] - it["x0"]) / n)
                codigo["cw"] = max(1.0, sorted(codigo["cws"])[len(codigo["cws"]) // 2])
            anterior = it
            continue
        if codigo:
            vaciar()
        nivel = ctx["nivel"](it)
        if nivel:
            vaciar()
            bloques.append("#" * (nivel + ctx["desplaza"]) + " " + VIÑETA.sub("", txt))
            anterior = it
            continue
        m = VIÑETA.match(txt)
        mn = NUMERADA.match(txt) if not m else None
        if m or mn:
            vaciar()
            cur = dict(tipo="li", text=(txt[m.end():] if m else txt), top=it["top"], size=it["size"])
            anterior = it
            continue
        if cur and (it["top"] - cur["top"]) <= 1.5 * cur["size"]:
            t = cur["text"]
            if t.endswith("-") and len(t) > 1 and t[-2].isalpha() and txt[:1].islower():
                cur["text"] = t[:-1] + txt
            else:
                cur["text"] = t + " " + txt
            cur["top"] = it["top"]
        else:
            vaciar()
            cur = dict(tipo="p", text=txt, top=it["top"], size=it["size"])
        anterior = it
    vaciar()
    return bloques


# ----------------------------------------------------------------------------
# Conversión de un PDF
# ----------------------------------------------------------------------------
def parsear_paginas(spec, n):
    if not spec:
        return list(range(n))
    out = set()
    for parte in spec.split(","):
        parte = parte.strip()
        if "-" in parte:
            a, b = parte.split("-")
            out.update(range(int(a) - 1, min(n, int(b))))
        elif parte:
            out.add(int(parte) - 1)
    return sorted(p for p in out if 0 <= p < n)


def convertir_pdf(ruta, o, ocr, estado, desplaza):
    avisos = []
    aviso = avisos.append
    pdf = pdfplumber.open(str(ruta))
    doc = pdfium.PdfDocument(str(ruta))
    paginas = parsear_paginas(o.paginas, len(pdf.pages))
    if o.paginas:
        mayor = max((int(x) for x in re.findall(r"\d+", o.paginas)), default=0)
        if not paginas:
            aviso(f"ninguna de las páginas pedidas ({o.paginas}) existe: el PDF tiene "
                  f"{len(pdf.pages)} páginas y el resultado queda VACÍO")
        elif mayor > len(pdf.pages):
            aviso(f"has pedido hasta la página {mayor} pero el PDF tiene {len(pdf.pages)}; "
                  "se convierten solo las que existen")
    datos = []
    for n, idx in enumerate(paginas, 1):
        print(f"\r  {ruta.name}: página {n}/{len(paginas)}", end="", file=sys.stderr, flush=True)
        try:
            d = analizar_pagina(pdf.pages[idx], doc, idx, o, ocr, aviso)
        except Exception as e:  # noqa: BLE001
            aviso(f"página {idx + 1}: error al procesarla ({e.__class__.__name__}: {e})")
            d = dict(items=[], raw_tokens=0, escaneada=False, ocr_paginas=0, caps_ocr=[], caps_conf=[])
        d["num"] = idx + 1
        datos.append(d)
        pdf.pages[idx].flush_cache()
    print("", file=sys.stderr)
    pdf.close()

    n = len(datos)
    # cabeceras / pies repetidos
    cuenta = Counter()
    for d in datos:
        vistos = set()
        for it in d["items"]:
            if it["kind"] == "linea" and it["zona"]:
                vistos.add(re.sub(r"\d+", "#", it["text"].lower()).strip())
        cuenta.update(vistos)
    umbral = max(2, int(0.4 * n + 0.5))
    quitadas = 0
    for d in datos:
        nuevos = []
        for it in d["items"]:
            if it["kind"] == "linea" and it["zona"]:
                clave = re.sub(r"\d+", "#", it["text"].lower()).strip()
                if (n >= 3 and cuenta[clave] >= umbral) or NUM_PAGINA.match(it["text"].strip()):
                    quitadas += 1
                    continue
            nuevos.append(it)
        d["items"] = nuevos

    # figuras decoradas (logotipos repetidos) y duplicadas
    paginas_por_hash = {}
    for d in datos:
        for it in d["items"]:
            if it["kind"] == "fig" and it.get("hash"):
                paginas_por_hash.setdefault(it["hash"], set()).add(d["num"])
    decor = {h for h, ps in paginas_por_hash.items() if n >= 3 and len(ps) >= max(3, 0.4 * n)}
    for d in datos:
        d["items"] = [it for it in d["items"]
                      if not (it["kind"] == "fig" and it.get("hash") in decor)]

    # tamaños para títulos
    pesos = Counter()
    for d in datos:
        for it in d["items"]:
            if it["kind"] == "linea" and not it["mono"]:
                pesos[round(it["size"] * 2) / 2] += len(it["text"])
    cuerpo = pesos.most_common(1)[0][0] if pesos else 11.0
    grandes = sorted({round(s * 2) / 2 for s in pesos if s >= cuerpo * 1.12}, reverse=True)[:3]

    def nivel(it):
        t = it["text"]
        if len(t) > 120:
            return 0
        s = round(it["size"] * 2) / 2
        if s in grandes:
            return grandes.index(s) + 1
        if s >= cuerpo * 1.12:
            return len(grandes) or 1
        if it["bold"] and s >= cuerpo * 0.95 and len(t) <= 90 and not t.endswith((".", ",", ";", ":")):
            return min(4, len(grandes) + 1)
        return 0

    vistas = {}   # hash -> id (para figuras repetidas)

    def emitir_figura(it):
        etiquetas = " | ".join(x for x in it["labels"] if x)
        suf = f" Texto en la figura: {etiquetas}" if etiquetas else ""
        if it["tipo"] == "omitir":
            estado["omitidas"] = estado.get("omitidas", 0) + 1
            return ("[imagen omitida]" + suf)
        if it["tipo"] == "texto":
            return "```ocr\n" + limpiar_multilinea(it["texto"]) + "\n```"
        if it.get("hash") in vistas:
            return f"[IMG {vistas[it['hash']]}]" + suf
        estado["n_fig"] += 1
        fid = f"F{estado['n_fig']:02d}"
        vistas[it["hash"]] = fid
        estado["figuras"].append((fid, it["pagina"], it["img"]))
        return f"[IMG {fid}]" + suf

    ctx = dict(nivel=nivel, desplaza=desplaza, emitir_figura=emitir_figura)
    partes = []
    for d in datos:
        for it in d["items"]:
            it["pagina"] = d["num"]
        bl = montar_pagina(d["items"], ctx)
        if not bl:
            continue
        if o.paginas_marca:
            partes.append(f"[p.{d['num']}]")
        partes.extend(bl)
    md = "\n\n".join(partes)
    md = re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", md)       # listas sin líneas en blanco
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"

    # aviso: código que no se detectó como tal (fuente no monoespaciada)
    patron = re.compile(r"(^\s*(def |class |import |from \S+ import |#include|public |private |"
                        r"function |const |let |var |return\b|SELECT |INSERT |CREATE TABLE))|"
                        r"([;{}]\s*$)|(=>)|(\)\s*\{)")
    pag_actual, en_codigo, sospechosas = None, False, Counter()
    for linea in md.split("\n"):
        if linea.startswith("```"):
            en_codigo = not en_codigo
            continue
        m = re.match(r"^\[p\.(\d+)\]$", linea)
        if m:
            pag_actual = int(m.group(1))
        elif not en_codigo and patron.search(linea):
            sospechosas[pag_actual] += 1
    for p, c in sorted(sospechosas.items(), key=lambda x: (x[0] is None, x[0])):
        if c >= 2:
            aviso(f"página {p}: {c} líneas parecen código pero no se detectó como bloque de código "
                  "(fuente no monoespaciada); revísalo en el .md")

    stats = dict(paginas=n, raw_tokens=sum(d["raw_tokens"] for d in datos),
                 quitadas=quitadas, escaneadas=sum(1 for d in datos if d["escaneada"]),
                 ocr_paginas=sum(d["ocr_paginas"] for d in datos),
                 caps_ocr=sum(len(d["caps_ocr"]) for d in datos),
                 caps_conf=[c for d in datos for c in d["caps_conf"]],
                 decor=len(decor))
    return md, stats, avisos


# ----------------------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="PDF -> Markdown ligero para IA (todo local).")
    ap.add_argument("pdfs", nargs="*", help="PDFs o carpetas con PDFs")
    ap.add_argument("-o", "--salida", default="salida_ia",
                    help="carpeta donde se crea una subcarpeta c_AAAAMMDD_nombre por cada conversión")
    ap.add_argument("--instalar-tesseract", action="store_true",
                    help="instala Tesseract (winget) y descarga los idiomas eng, spa y eus; luego sale")
    ap.add_argument("--unir", action="store_true", help="un solo .md con todos los PDFs")
    ap.add_argument("--nombre-unido", default="documentos", help="nombre del .md unido")
    ap.add_argument("--laminas", action="store_true",
                    help="agrupa las imágenes en pocas láminas (menos archivos que subir)")
    ap.add_argument("--imagenes", choices=["mantener", "auto", "omitir"], default="mantener",
                    help="mantener (por defecto): las capturas se dejan como imagen, sin errores de OCR | "
                         "auto: las capturas de texto pasan a texto por OCR (menos tokens, PUEDE HABER ERRORES) | "
                         "omitir: sin imágenes")
    ap.add_argument("--ocr-lang", default="eng+spa",
                    help="idiomas de Tesseract (p. ej. spa+eng, eus+spa). Por defecto eng+spa")
    ap.add_argument("--sin-imagenes", dest="sin_imagenes", action="store_true",
                    help="atajo de --imagenes omitir: no genera ninguna imagen")
    ap.add_argument("--sin-ocr", action="store_true", help="desactiva el OCR")
    ap.add_argument("--tesseract", help="ruta a tesseract.exe si no está en el PATH")
    ap.add_argument("--conf-ocr", type=float, default=60.0,
                    help="confianza mínima (0-100) para sustituir una captura por su texto")
    ap.add_argument("--dpi-ocr", type=int, default=250, help="resolución para OCR de páginas escaneadas")
    ap.add_argument("--max-lado", type=int, default=1000, help="lado máximo (px) de las imágenes conservadas")
    ap.add_argument("--area-minima", type=float, default=0.004,
                    help="área mínima de una imagen (fracción de la página) para considerarla")
    ap.add_argument("--paginas", help='solo estas páginas, p. ej. "1-10,15"')
    ap.add_argument("--sin-paginas", action="store_true", help="no poner marcas [p.N]")
    ap.add_argument("--sin-tablas", action="store_true", help="no detectar tablas")
    o = ap.parse_args()
    o.paginas_marca = not o.sin_paginas
    if o.sin_imagenes:
        o.imagenes = "omitir"

    if o.instalar_tesseract:
        sys.exit(instalar_tesseract())
    if not o.pdfs:
        ap.error("indica al menos un PDF o una carpeta con PDFs (o usa --instalar-tesseract)")

    rutas = []
    for p in o.pdfs:
        pp = Path(p)
        rutas += sorted(pp.glob("*.pdf")) if pp.is_dir() else [pp]
    rutas = [r for r in rutas if r.exists()]
    if not rutas:
        sys.exit("No he encontrado ningún PDF.")

    padre = Path(o.salida)
    padre.mkdir(parents=True, exist_ok=True)
    ocr = OCR(o.ocr_lang, o.tesseract, activo=not o.sin_ocr)
    if not ocr.ok:
        print(f"AVISO: OCR no disponible: {ocr.motivo}", file=sys.stderr)
    elif ocr.faltan:
        print(f"AVISO: faltan idiomas de Tesseract: {', '.join(ocr.faltan)} (uso {ocr.lang}). "
              "Instálalos con:  python conversor.py --instalar-tesseract", file=sys.stderr)

    fecha = datetime.now().strftime("%Y%m%d")
    trabajos = [(rutas, o.nombre_unido)] if o.unir else [([r], r.stem) for r in rutas]
    for grupo, nombre in trabajos:
        procesar_trabajo(grupo, nombre, padre, fecha, o, ocr)


def nombre_limpio(nombre):
    """Nombre sin espacios ni caracteres no válidos en Windows."""
    return re.sub(r'[\s<>:"/\\|?*]+', "", nombre) or "documento"


def carpeta_nueva(padre, fecha, nombre):
    """c_AAAAMMDD_nombre; si ya existe (misma fecha y nombre) añade _2, _3..."""
    base = f"c_{fecha}_{nombre_limpio(nombre)}"
    ruta, n = padre / base, 1
    while ruta.exists():
        n += 1
        ruta = padre / f"{base}_{n}"
    ruta.mkdir(parents=True)
    return ruta


def procesar_trabajo(rutas, nombre, padre, fecha, o, ocr):
    """Convierte uno o varios PDFs (--unir) en UNA carpeta de salida propia."""
    estado = dict(n_fig=0, figuras=[])
    docs = []
    for r in rutas:
        try:
            md, st, av = convertir_pdf(r, o, ocr, estado, 1 if o.unir else 0)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR en {r.name}: {e.__class__.__name__}: {e}", file=sys.stderr)
            continue
        docs.append((r, md, st, av))
    if not docs:
        return
    salida = carpeta_nueva(padre, fecha, nombre)
    base = nombre_limpio(nombre)

    # guardar markdown
    if o.unir:
        trozos = [f"# Documento: {r.stem}\n\n{md}" for r, md, _, _ in docs]
        (salida / f"{base}.md").write_text("\n\n".join(trozos), encoding="utf-8")
    else:
        r, md = docs[0][0], docs[0][1]
        (salida / f"{base}.md").write_text(f"# {r.stem}\n\n{md}", encoding="utf-8")

    # guardar imágenes
    tokens_img = 0
    n_archivos_img = 0
    figs = estado["figuras"]
    if figs:
        if o.laminas:
            for k, (hoja, ids) in enumerate(hacer_laminas(figs), 1):
                hoja.save(salida / f"L{k}.png", optimize=True)
                tokens_img += tok_imagen(*hoja.size)
                n_archivos_img += 1
        else:
            for fid, pag, im in figs:
                im.save(salida / f"{fid}_p{pag}.png", optimize=True)
                tokens_img += tok_imagen(*im.size)
                n_archivos_img += 1

    # informe
    print(f"\n===== INFORME: {salida.name} (estimaciones, ±25%) =====")
    tot_orig = tot_txt = 0
    for r, md, st, av in docs:
        t = tok_texto(md)
        orig = st["raw_tokens"] + st["paginas"] * TOKENS_MAX_IMAGEN
        tot_orig += orig
        tot_txt += t
        print(f"\n{r.name}  ({st['paginas']} págs)")
        print(f"  texto resultante: ~{t:,} tokens")
        extra = []
        if st["quitadas"]:
            extra.append(f"{st['quitadas']} líneas de cabecera/pie/nº de página quitadas")
        if st["caps_ocr"]:
            conf = sum(st["caps_conf"]) / len(st["caps_conf"])
            extra.append(f"{st['caps_ocr']} capturas pasadas a texto por OCR (confianza media {conf:.0f}%)")
        if st["ocr_paginas"]:
            extra.append(f"{st['ocr_paginas']} páginas escaneadas leídas por OCR")
        if st["decor"]:
            extra.append(f"{st['decor']} imágenes repetidas descartadas (logotipos)")
        for e in extra:
            print(f"  - {e}")
        for a in av:
            print(f"  ! {a}")
    total_salida = tot_txt + tokens_img
    print("\n--- TOTAL ---")
    print(f"PDF original (texto + página como imagen): ~{tot_orig:,} tokens")
    n_fig = len(figs)
    print(f"Resultado: ~{tot_txt:,} (texto) + ~{tokens_img:,} ({n_fig} figuras en {n_archivos_img} "
          f"archivo{'s' if n_archivos_img != 1 else ''} de imagen) = ~{total_salida:,}")
    if tot_orig:
        print(f"Ahorro estimado: {100 * (1 - total_salida / tot_orig):.0f}%")
    if estado.get("omitidas"):
        print(f"\nAVISO: {estado['omitidas']} imágenes/figuras omitidas (marcadas como [imagen omitida]); "
              "su contenido visual no llega a la IA.")
    print(f"\nSalida en: {salida.resolve()}")
    if figs:
        print("Sube el .md junto con las imágenes de esa carpeta; en el texto aparecen como [IMG Fnn].")


if __name__ == "__main__":
    main()