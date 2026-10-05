# SDF — Extractor y clasificador de canales de TV

Herramienta de línea de comandos para leer listas M3U/M3U8 locales o fuentes públicas, normalizar
los canales, eliminar duplicados y exportarlos a JSON, CSV o Supabase.

## Qué hace

- **Límite por defecto de 20 canales únicos (`--limit 20`)**: selecciona hasta 20 canales variados,
  priorizados por calidad de metadatos (logo, país, idioma, categoría, HTTPS/HLS) y distribuidos
  entre distintas categorías. Usa `--limit 0` si deseas exportar sin límite.
- **Cupo repartible entre fuentes (`--max-per-source`)**: con 22 fuentes configuradas, limita
  cuántos canales aporta cada una (por ejemplo `--max-per-source 8`) para que ninguna lista
  acapare el resultado. `0` (valor por defecto) no aplica límite.
- **Sin canales repetidos**: deduplica por identidad canónica de canal (normalizando sufijos de
  calidad como `HD`, `FHD`, `4K`, `1080p`, `720p`, `[Geo-blocked]`, `En Vivo`, `Señal 2`, prefijos
  de país y numeración), `slug`, `tvg-id`, URL del stream y URL final tras redirecciones HTTP.
  Conserva ediciones regionales distintas como `LATAM`, `Norte`, `Sur` e `International` para no
  confundirlas con duplicados. Además, al sincronizar con Supabase consulta primero los canales
  existentes para no repetir los ya guardados.
- **Comprobación progresiva y rápida (`--check-streams`)**: cuando se usa junto con `--limit`,
  comprueba los candidatos por lotes con soporte de streams alternativos (fallback) y se detiene en
  cuanto reúne los 20 canales activos (`http_ok`), evitando tardar minutos en listas masivas.
- Lee playlists con nombres que incluyen comas, metadatos `tvg-*`, `#EXTGRP` y URLs relativas.
- Resuelve rutas relativas de listas locales y remotas; no confunde segmentos de una playlist HLS
  con canales de TV.
- Clasifica categoría, idioma y país usando primero los metadatos explícitos y como alternativa
  nombres, grupos, prefijos (`[MX]`, `ES |`) y sufijos en `tvg-id` (por ejemplo, `cnn.us`).

## Fuentes configuradas

- BByte Jellyfin — https://iptv.bbyte.app/jellyfin/live.m3u
- CXTv — https://www.cxtvenvivo.com/
- TDTChannels — https://www.tdtchannels.com/lists/tv.m3u8
- m3u.cl Top — https://m3u.cl/lista/top.m3u
- m3u.cl LATAM — https://m3u.cl/lista/LATAM.m3u
- IPTV-org — https://iptv-org.github.io/iptv/index.m3u
- Teleonline — https://teleonline.org/; también detecta su playlist pública M3U8.
- Teleonline M3U — https://teleonline.github.io/listas/tv.m3u8 (lista directa, sin scraping).
- Teleonline TV — https://www.teleonline.tv/ (WordPress con reproductor propio).
- TV en Vivo — https://www.tvenvivo.org/ (wrappers PHP `/live/core.php?canal=…`).
- TV Libre Online — https://tvlibreonline.st/ (wrapper `/html/fl/` + `cv.json`).
- TV Garden — https://tvgarden.world/; descubre páginas globales de canal mediante su sitemap.
- Samsung TV Plus — https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/samsungtvplus_all.m3u
- LG Channels US — https://www.apsattv.com/uslg.m3u (país predeterminado: US cuando la lista no lo especifica).
- Tubi — https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/tubi_all.m3u
- Plex — https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/plex_all.m3u
- Vizio WatchFree — https://www.apsattv.com/vizio.m3u
- Xiaomi — https://www.apsattv.com/xiaomi.m3u
- Rakuten TV UK — https://www.apsattv.com/rakuten_uk.m3u; alternativa solicitada:
  https://www.apsattv.com/rakutentv-uk.m3u.
- Rakuten TV France — https://www.apsattv.com/rakuten_fr.m3u; alternativa solicitada:
  https://www.apsattv.com/rakuten-fr.m3u.
- Movie Ark Brasil — https://www.apsattv.com/moviearkbr.m3u
- Cineverse — https://www.apsattv.com/cineverse.m3u

Las fuentes pueden reorganizar sus archivos; se conservan también las URLs solicitadas que aún no
responden (por ejemplo, Tubi puede devolver 404) para que el scraper las vuelva a intentar en
futuras ejecuciones. Las variantes oficiales de Rakuten en Apsattv usan guion bajo, mientras las
URLs solicitadas se configuran como respaldo.

### Cómo se resuelven los reproductores

Teleonline TV, TV en Vivo y TV Libre Online no publican el `.m3u8` en la página del canal:
lo cargan con JavaScript. Para cada página de canal el extractor:

1. Descarga la página y busca streams HLS/MPD, incluso dentro de JavaScript con las barras
   escapadas (`https:\/\/…`).
2. Extrae **embeds de YouTube** (`/embed/ID`, `/live/ID`) como canales con
   `stream_type: "youtube"`; se consideran activos solo si el embed declara una emisión en vivo.
3. Sigue hasta `max_depth` saltos los **iframes**, los wrappers (`/html/fl/?get=…`) y las
   URLs de reproductor que aparecen en el JS del propio sitio.
4. Resuelve los **JSON de configuración** (`/html/cv.json`) y reconstruye la URL del
   reproductor tal como la arma el navegador (`//host/cvatt.html?get=<token>`), conservando
   el token de la URL original.

Las opciones **(FL)** de TV Libre Online están geo-restringidas a Argentina, Uruguay y
Paraguay; fuera de esos países solo responderá la opción de YouTube. Igual que el resto de
fuentes, si algo falla se imprime un `WARNING` y se continúa con las demás.

### Descubrimiento de páginas y clasificación de país

TV Garden publica páginas con el patrón `/tv/{país}/{canal}` y un sitemap global. El scraper sigue
los sitemaps de forma acotada, selecciona páginas en rotación entre los países encontrados y extrae
streams, títulos y datos JSON cuando están disponibles; no necesita rastrear todo el sitio para
obtener cobertura internacional. El código de país de la ruta prevalece sobre menciones en el nombre
(como una edición “France 24” listada en el mercado de Estados Unidos).

Para M3U y feeds JSON se prefieren los metadatos explícitos (`tvg-country`, país del registro,
`channel-id`/`tvg-id` con sufijo regional); después se usan los códigos de país en la ruta y las
pistas regionales configuradas para una fuente. La inferencia por texto solo acepta formas de
etiqueta (por ejemplo, `ESPN Colombia`, `[ZA] Canal` o `Noticias de Sudáfrica`) y evita asignar un
país por una mención incidental como “Jordan Peterson”. Si no hay una señal suficiente, conserva
`unknown`. Las pistas de país de las listas regionales solo rellenan valores desconocidos, no
reemplazan un país que ya venga explícito.

### Tipos de stream

Cada canal exporta `stream_type`:

- `hls`: URL HLS/MPD reproducible (es el valor por defecto y el que ya se usaba).
- `youtube`: embed de YouTube en vivo (`https://www.youtube.com/embed/ID`).

En JSON y CSV aparece como `stream_type`; en M3U solo se añade el atributo
`stream-type="youtube"` cuando no es HLS, para no ensuciar las listas normales. En Supabase
se guarda en la columna `stream_type`.

Lista las fuentes y sus claves con:

```sh
python sdf_tv_channels.py --list-sources
```

Las fuentes son públicas y pueden caerse o cambiar de URL. Cuando una falla no se detiene la
ejecución: se imprime un `WARNING` con el motivo y se continúa con el resto. El resumen final
indica cuántos canales aportó cada fuente (`Canales encontrados por fuente: ...`), de modo que
si una lista deja de funcionar se ve de inmediato en el log del workflow.

## Uso

```sh
# Leer una lista local (por defecto exporta hasta 20 canales únicos sin repetir)
python sdf_tv_channels.py ./playlist.m3u -o channels.json

# Descargar fuentes configuradas y seleccionar 20 canales únicos verificados
python sdf_tv_channels.py --all-sources --check-streams --limit 20 -o data/tv_channels.json

# Elegir varias fuentes
python sdf_tv_channels.py --source tdtchannels --source teleonline -o data/tv_channels.json

# Filtrar antes de exportar
python sdf_tv_channels.py --all-sources --category sports -o data/sports.json
python sdf_tv_channels.py --all-sources --language es -o data/spanish.json
python sdf_tv_channels.py --all-sources --country MX -o data/mexico.csv --format csv

# Exportar todos los canales sin límite de 20
python sdf_tv_channels.py ./playlist.m3u --limit 0 -o todos.json

# Sincronización manual con Supabase (hasta 20 canales únicos)
export SUPABASE_URL="https://tu-proyecto.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="tu-secret-key"
python sdf_tv_channels.py --all-sources --sync-supabase --limit 20

# Sincronización automática: solo agrega y activa hasta 20 streams únicos que responden HTTP OK
python sdf_tv_channels.py --all-sources --check-streams --sync-supabase --activation-mode automatic --limit 20

# Repartir el cupo entre fuentes (ninguna lista aporta más de 8 canales)
python sdf_tv_channels.py --all-sources --check-streams --limit 20 --max-per-source 8
```

### Búsqueda de canales específicos

Nueva función de **búsqueda** integrada en el CLI y en GitHub Actions. Permite encontrar canales por texto libre, insensible a mayúsculas y acentos, con soporte para múltiples campos y expresiones regulares.

**CLI — opciones nuevas:**

- `--search TEXT` — texto a buscar. Acepta comas como **OR** y espacios como **AND** dentro de cada alternativa.
  - `--search "espn"` → cualquier canal que contenga `espn`.
  - `--search "espn colombia"` → contiene `espn` **Y** `colombia` (en cualquier orden).
  - `--search "cnn, bbc"` → contiene `cnn` **O** `bbc`.
  - Insensible a acentos: `--search "futbol"` encuentra `Fútbol`.
- `--search-fields LISTA` — campos donde buscar (separados por comas). Por defecto `name,group,tvg_name,tvg_id,category`. Valores válidos: `name, group, tvg_id, tvg_name, category, country, language, source, source_url, url, logo, slug`.
- `--regex` / `--search-regex` — interpreta `--search` como expresión regular (también plegada, insensible a acentos).

Se combina en **AND** con los filtros ya existentes (`--category`, `--country`, `--language`, `--source`, `--all-sources`, `--check-streams`).

```sh
# Buscar ESPN en todas las fuentes
python sdf_tv_channels.py --all-sources --search "ESPN" -o espn.json

# Buscar sin acentos y filtrando por país
python sdf_tv_channels.py --all-sources --search "futbol" --country MX -o futbol-mx.json

# OR lógico: CNN o BBC
python sdf_tv_channels.py --all-sources --search "cnn, bbc" -o noticias.json

# AND lógico en cualquier orden
python sdf_tv_channels.py --all-sources --search "espn colombia" -o espn-co.json

# Buscar solo en el slug o el país
python sdf_tv_channels.py --all-sources --search "US" --search-fields country -o usa.json

# Expresión regular (insensible a acentos)
python sdf_tv_channels.py --all-sources --search "espn.*colombia" --regex -o espn-co-regex.json

# Combinar con verificación de streams y límite
python sdf_tv_channels.py --all-sources --search "fox sports" --check-streams --limit 10 -o fox-verificados.json

# Desde código Python
from sdf_tv_channels import search_channels, filter_channels
coinciden = search_channels(canales, "espn, fox sports", fields=("name","group"))
filtrados = filter_channels(canales, query="cnn", category="news", country="US")
```

**Uso programático:** las funciones `search_channels(channels, query, fields, use_regex)`, `channel_matches_query(channel, query, ...)` y `filter_channels(channels, query, category, language, country, source, ...)` también están disponibles al importar `sdf_tv_channels`.

**GitHub Actions — workflow dedicado:**

El workflow `.github/workflows/search-channels.yml` expone la misma búsqueda desde la pestaña **Actions → Buscar canales específicos → Run workflow**.

Inputs del workflow:

| Input | Descripción | Por defecto |
| --- | --- | --- |
| `query` | Texto a buscar (requerido) | `ESPN` |
| `search_fields` | Campos donde buscar | `name,group,tvg_name,tvg_id,category` |
| `use_regex` | ¿Interpretar como regex? | `false` |
| `category` / `country` / `language` | Filtros adicionales | *(vacío = todos)* |
| `source` | Fuente específica o `all` | `all` |
| `limit` | Máximo de canales | `20` |
| `format` | `json` / `csv` / `m3u` | `json` |
| `check_streams` | Verificar HTTP antes de exportar | `false` |

Cada ejecución:

1. Descarga las fuentes con los filtros indicados (`--search`, `--category`, etc.).
2. Imprime en el log y en el **Summary** cuántos canales coinciden y los primeros resultados.
3. Sube el resultado como artefacto `search-results-<run_number>` (retención 7 días) en el formato elegido.

El workflow respeta la misma deduplicación y normalización que el extractor principal y no escribe en Supabase; úsalo para localizar rápidamente un canal concreto antes de sincronizarlo.

### Errores frecuentes

| Situación | Comportamiento actual |
| --- | --- |
| `Process completed with exit code 2` | Solo se produce por un argumento inválido (por ejemplo, un archivo de entrada inexistente). Antes también salía con código 2 cuando faltaban los secretos de Supabase o la sincronización fallaba; ahora esos casos **avisa y continúa**, así que el archivo de canales se exporta igual. |
| Faltan `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Imprime `WARNING: --sync-supabase ignorado…`, exporta el archivo y termina con código 0. |
| Supabase responde 401/404/500 o no existe la tabla | Imprime `ERROR: No se pudo sincronizar con Supabase…` y termina con código 0. Usa `--fail-on-sync-error` si prefieres que el trabajo falle en ese caso. |
| La ejecución se corta por tiempo | Usa `--deadline <segundos>` para limitar la descarga de fuentes y la comprobación de streams; al agotarse se exporta lo ya verificado. |

`--check-streams` añade `stream_check` a cada objeto JSON; en CSV añade `stream_status`,
`stream_http_status`, `stream_content_type`, `stream_final_url` y `stream_detail`. También imprime
un resumen por estado. Usa `--timeout` para limitar cada petición, `--workers` para ajustar el
paralelismo y `--max-checks` para fijar un tope estricto de URLs comprobadas cuando se busca
completar el cupo de `--limit`; si el tope es menor que el cupo, puede exportarse una lista más
corta. `--max-per-source` también se respeta en esta selección progresiva.

Estados habituales: `http_ok` (respuesta HTTP satisfactoria; en M3U se reconoce `#EXTM3U`),
`invalid_playlist`, `restricted` (por ejemplo, HTTP 401/403), `http_error`, `unreachable`,
`not_live` (embed de YouTube que no declara emisión en vivo) y `unsupported` (protocolo
distinto de HTTP(S)). Si un servidor rechaza la cabecera `Range` (por
ejemplo con HTTP 416), se reintenta automáticamente sin `Range` leyendo únicamente los primeros
4 KiB.

## Clasificación

Categorías principales: `sports`, `news`, `movies`, `series`, `kids`, `documentary`, `music`,
`business`, `culture`, `travel`, `cooking`, `religious`, `weather`, `general` y `other`.

La clasificación no está limitada a México ni a Latinoamérica: incluye los códigos ISO 3166-1 de
país (alpha-2 y alpha-3) y una tabla de nombres en inglés y español. Para idioma acepta códigos
ISO 639 de dos y tres letras —incluidos códigos regionales como `hau-NG` o `zh-Hant-TW`— y nombres
reconocidos en inglés y español; los códigos sin equivalente de dos letras se conservan en su
forma de tres letras. Algunas palabras de categoría también se reconocen en portugués, francés,
alemán, ruso, árabe, chino, japonés, coreano y otros idiomas. Estos datos vienen incluidos en
`data/iso_metadata.json`; no hace falta instalar dependencias externas.

La clasificación sigue siendo heurística. Se conserva `unknown` cuando no hay señales suficientes
para idioma o país; la nacionalidad de una cadena o canal no se usa automáticamente como idioma.

## Desarrollo y pruebas

Requiere Python 3.11 o posterior. Las pruebas unitarias se ejecutan sin acceder a Internet:

```sh
python -m unittest discover -s tests -v
```

## GitHub Actions

### 1. Sincronización automática — `.github/workflows/sync-tv-channels.yml`

El workflow permite ejecutarse manualmente desde
**Actions → Sync TV channels to Supabase → Run workflow** (con opción para elegir el límite de
canales, por defecto `20`, el modo de activación y si guardar el resultado en el repositorio) y
también se ejecuta automáticamente cada 5 horas (`0 */5 * * *` UTC). En cada ejecución verifica
candidatos por lotes y agrega hasta 20 canales únicos y activos (`is_active = true`) sin repetir.

Cada ejecución deja tres rastros del resultado:

1. `data/tv_channels.json` en el repositorio: se confirma automáticamente cuando la lista cambia
   (commits `chore: actualizar data/tv_channels.json [skip ci]`), así los canales quedan
   guardados y son consultables sin depender de los artefactos.
2. Un artefacto `tv-channels-<número>` con el mismo JSON, conservado 7 días.
3. Un resumen en la pestaña del job con los canales exportados, los sincronizados y el detalle
   por fuente.

El workflow usa `--deadline 420` para que nunca supere el `timeout-minutes: 15` del job aunque
alguna fuente responda muy lento y `--max-per-source 8` para repartir el cupo entre las 22
fuentes configuradas (ajústalo o quítalo si prefieres que una sola lista llene los 20 canales).

Configura estos secretos en **Settings → Secrets and variables → Actions**:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (o `SUPABASE_SECRET_KEY`)

Si los secretos no están configurados, el workflow **no falla**: avisa de que se omite la
sincronización y sigue exportando y guardando `data/tv_channels.json`.

### 2. Buscar canales específicos — `.github/workflows/search-channels.yml` ⭐ NUEVO

Workflow dedicado a **buscar canales en específico** sin necesidad de clonar el repo ni usar la terminal.

**Cómo usarlo:** en GitHub ve a **Actions → Buscar canales específicos → Run workflow** y rellena:

- **query** — texto a buscar (ej: `ESPN`, `Telefe`, `CNN`). Acepta `cnn, bbc` (OR) y `espn colombia` (AND).
- **search_fields** — dónde buscar: `name,group,tvg_name,tvg_id,category,country,language,source,slug...`
- **use_regex** — si activar modo expresión regular.
- **category / country / language** — filtros extra opcionales.
- **source** — una fuente concreta o `all` (las 22).
- **limit** / **format** / **check_streams** — como en el CLI.

Al ejecutarse, el workflow:

1. Descarga las fuentes con `python sdf_tv_channels.py --all-sources --search "…"` + filtros.
2. Deja el resumen en **Summary** (canales encontrados + tabla) y en los logs.
3. Sube el resultado como artefacto `search-results-<run_number>` (`json`/`csv`/`m3u`, 7 días).

Úsalo para localizar un canal antes de sincronizarlo, o para exportar solo una temática:

```sh
# Equivalente local de una ejecución del workflow:
python sdf_tv_channels.py --all-sources --search "Discovery" --category documentary -o docu.json
python sdf_tv_channels.py --all-sources --search "TNT Sports" --country AR -o tnt-ar.json
```
