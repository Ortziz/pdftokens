# Conversor de PDF a Markdown ligero para IA

Convierte PDFs en un `.md` (y, si hace falta, unas pocas imágenes) para pasárselos
a Claude gastando muchos menos tokens. Todo es local: no usa ninguna IA ni API.

## Instalación (Windows)

1. Python 3.9 o superior.
2. Tesseract OCR (solo si quieres OCR): instalador de UB Mannheim,
   https://github.com/UB-Mannheim/tesseract/wiki
   En el instalador, en "Additional language data", marca **Spanish** y **Basque** (si lo necesitas).
3. En la carpeta del programa:

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Sin Tesseract el programa funciona igual; solo que las capturas se quedan como imagen
y las páginas escaneadas no se leen.

## Uso

```powershell
# un PDF
python conversor.py apuntes.pdf

# tus ~10 PDFs de golpe: un solo .md y pocas imágenes
python conversor.py C:\Apuntes\ -o C:\Apuntes\para_claude --unir --laminas

# documentos en euskera / español con capturas
python conversor.py apuntes.pdf --ocr-lang eus+spa+eng
```

Al terminar muestra un informe con una **estimación** de tokens antes y después.
Sube a Claude el `.md` y las imágenes `F01…`/`L1…` (si las hay). En el texto aparecen
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

## Opciones útiles

| Opción | Para qué |
|---|---|
| `--imagenes auto` | Pasa las capturas de texto a texto por OCR: ahorra tokens pero **puede introducir errores**; no usar si importa cada carácter |
| `--sin-imagenes` (o `--imagenes omitir`) | No genera ninguna imagen: solo texto. Donde había una figura queda `[imagen omitida]` (más el texto de sus etiquetas si era un esquema). Las páginas escaneadas se siguen leyendo por OCR |
| `--unir` | Un solo `.md` con todos los PDFs |
| `--laminas` | Agrupa las imágenes en pocas láminas (menos archivos que subir) |
| `--paginas "1-10,15"` | Solo esas páginas |
| `--sin-paginas` | Quita las marcas `[p.N]` |
| `--max-lado 800` | Imágenes más pequeñas (cuestan menos) |
| `--tesseract RUTA` | Si Tesseract no está en el PATH |

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