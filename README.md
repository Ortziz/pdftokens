# Conversor de PDF a Markdown ligero para IA

Convierte PDFs en un `.md` (y, si hace falta, unas pocas imágenes) para pasárselos
a Claude gastando muchos menos tokens. Todo es local: no usa ninguna IA ni API.

## Instalación (Windows)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python conversor.py --instalar-tesseract
```

`pip` no puede instalar Tesseract porque es un programa aparte, no un paquete de Python.
Por eso existe `--instalar-tesseract`: lo instala con `winget` (Windows puede pedir permiso
de administrador) y descarga los idiomas inglés, español y euskera a la carpeta `tessdata`
junto al programa. Si no tienes `winget`, instálalo a mano desde
https://github.com/UB-Mannheim/tesseract/wiki y vuelve a ejecutar el comando para bajar los idiomas.

Tesseract es opcional: sin él todo funciona igual, salvo que las páginas escaneadas
no se leen (se dejan como imagen). En el modo por defecto las capturas ya se quedan como imagen.

## Uso

```powershell
# un PDF
python conversor.py apuntes.pdf

# tus ~10 PDFs de golpe: un solo .md y pocas imágenes
python conversor.py C:\Apuntes\ --unir --laminas

# documentos en euskera / español
python conversor.py apuntes.pdf --ocr-lang eus+spa+eng
```

Cada conversión crea su propia carpeta dentro de `salida_ia` (cámbiala con `-o`):

```text
salida_ia/
└── c_20261118_NombreDelPdfSinEspacios/     c_AAAAMMDD_nombre
    ├── NombreDelPdfSinEspacios.md
    └── F01_p2.png, F02_p4.png ...          (si hay imágenes)
```

- El nombre es `c_` + fecha (sin hora) + nombre del PDF sin espacios.
- Si repites la conversión el mismo día, la carpeta nueva termina en `_2`, `_3`... (no pisa la anterior).
- Una carpeta con varios PDFs sin `--unir` crea una subcarpeta por PDF; con `--unir` crea una sola
  (`c_AAAAMMDD_documentos`, cambia el nombre con `--nombre-unido`).

Al terminar muestra un informe con una **estimación** de tokens antes y después.
Sube a Claude el `.md` y las imágenes de esa carpeta (si las hay). En el texto aparecen
como `[IMG F01]`.

## Qué hace con cada cosa

| Contenido | Resultado |
|---|---|
| Texto | Markdown (títulos, listas, código con sangría) |
| Cabeceras, pies, nº de página repetidos | Se eliminan |
| Palabras partidas por guion a final de línea | Se unen |
| Tablas con bordes | Tabla Markdown |
| Capturas de texto (terminal, código) | Imagen (por defecto, sin errores). Con `--imagenes auto`: OCR → bloque ```` ```ocr ```` |
| Esquemas, gráficos, fotos | Imagen recortada y reducida + etiquetas de texto del esquema |
| Páginas escaneadas | OCR de la página entera |
| Logotipos repetidos | Se descartan |

## Opciones

### Qué convertir y dónde dejarlo

| Opción | Qué hace |
|---|---|
| `pdfs` (sin guion) | Uno o varios PDFs, o una carpeta (convierte todos los `.pdf` que haya dentro) |
| `-o CARPETA` | Carpeta donde se crean las subcarpetas de salida `c_AAAAMMDD_nombre`. Por defecto `salida_ia` |
| `--paginas "1-10,15"` | Solo esas páginas. Con una carpeta, se aplica igual a cada PDF; las páginas que no existan se ignoran y el informe avisa |
| `--unir` | Un solo `.md` y una sola carpeta para todos los PDFs. Los títulos de cada PDF bajan un nivel, van tras `# Documento: nombre`, y las figuras se numeran seguidas (F01, F02…) |
| `--nombre-unido NOMBRE` | Solo con `--unir`: sustituye `documentos` en el nombre de la carpeta y del `.md` |

### Imágenes (lo que más tokens cuesta)

| Opción | Qué hace |
|---|---|
| `--imagenes mantener` (por defecto) | Esquemas, gráficos y capturas se guardan como imagen. Es lo más fiel; el modo seguro para código en capturas |
| `--imagenes auto` | Las capturas que parecen texto (terminal, código) pasan a texto por OCR y quedan sin imagen. Ahorra tokens pero **puede introducir errores**; no usar si importa cada carácter. Solo actúa sobre imágenes incrustadas, no sobre esquemas dibujados. Necesita Tesseract |
| `--imagenes omitir` o `--sin-imagenes` | No genera ninguna imagen: solo texto. Donde había una figura queda `[imagen omitida]` (más el texto de sus etiquetas si era un esquema). Las páginas escaneadas se siguen leyendo por OCR |
| `--laminas` | Agrupa las imágenes en láminas (`L1.png`, `L2.png`…) con etiquetas tipo `F03 (p.4)`. Menos archivos que subir, no menos tokens |
| `--max-lado 1000` | Lado largo máximo (px) de cada imagen. Una de 1000×1000 cuesta unos 1.300 tokens. Bajarlo ahorra, pero el texto dentro de las capturas puede dejar de leerse bien: con código, pruébalo antes |
| `--area-minima 0.004` | Ignora imágenes menores que esa fracción de la página (0,004 ≈ 45×45 puntos en un A4: descarta iconos y viñetas). Súbelo si salen imágenes basura, bájalo si se pierde una figura pequeña |

### OCR (Tesseract)

| Opción | Qué hace |
|---|---|
| `--sin-ocr` | Desactiva el OCR por completo. Las páginas escaneadas se dejan como imagen (o se omiten con `--sin-imagenes`); el informe avisa |
| `--ocr-lang eng+spa` | Idiomas del OCR con los códigos de Tesseract unidos por `+` (`eus` = euskera). Si falta alguno, avisa y usa los que haya |
| `--conf-ocr 60` | Solo con `--imagenes auto`: confianza mínima (0-100) para sustituir una captura por su texto; si no llega, se queda como imagen. Súbela (p. ej. 80) para ser más exigente |
| `--dpi-ocr 250` | Resolución a la que se leen las páginas escaneadas. Más alta (300) lee mejor la letra pequeña pero tarda más. No afecta a las capturas |
| `--tesseract RUTA` | Si Tesseract está en un sitio no habitual |
| `--instalar-tesseract` | Instala Tesseract (winget), descarga los idiomas eng, spa y eus y sale sin convertir nada |

### Detalles del texto

| Opción | Qué hace |
|---|---|
| `--sin-paginas` | Quita las marcas `[p.N]`. Ahorra muy pocos tokens, pero pierdes la referencia de página |
| `--sin-tablas` | No detecta tablas; su texto sale como líneas sueltas. Solo si alguna tabla sale desordenada |

### Combinación recomendada para varios PDFs con código

```powershell
python conversor.py C:\Apuntes\ --unir --laminas
```

Es el modo por defecto: el código que es texto sale exacto y las capturas se quedan como imagen.

## Limitaciones conocidas

- **OCR de capturas** (solo con `--imagenes auto`): puede equivocarse en símbolos y caracteres parecidos
  (`G`/`6`, `0`/`O`), pierde el texto resaltado con fondo de color y los
  caracteres de dibujo de árbol (`├─`). Por eso NO es el modo por defecto.
- **Código**: el que es texto real en fuente monoespaciada se copia exacto (comprobado línea a línea,
  con espacios y sangría). Si está en otra fuente, el informe avisa.
- **Documentos a dos columnas**: el orden de lectura puede mezclar columnas.
- **Fórmulas**: se extraen como texto plano, no como LaTeX.
- **Tablas sin bordes** (o con bordes poco habituales) pueden quedar como texto suelto.
- **Páginas escaneadas**: el OCR no reconstruye tablas ni títulos.
- Las cifras de tokens son estimaciones (±25 %); la cifra real la ves en Claude.
- Las imágenes cuestan tokens según sus **píxeles**, no su tamaño en disco: por eso
  se recortan y se reducen. `--laminas` reduce el número de archivos, no los tokens
  (puede añadir algo de espacio en blanco).