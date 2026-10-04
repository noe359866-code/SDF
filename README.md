# SDF — Extractor y clasificador de canales de TV

Herramienta de línea de comandos para leer listas M3U/M3U8 locales o fuentes públicas, normalizar
los canales, eliminar duplicados y exportarlos a JSON, CSV o Supabase.

## Qué hace

- **Límite por defecto de 20 canales únicos (`--limit 20`)**: selecciona hasta 20 canales variados,
  priorizados por calidad de metadatos (logo, país, idioma, categoría, HTTPS/HLS) y distribuidos
  entre distintas categorías. Usa `--limit 0` si deseas exportar sin límite.
- **Cupo repartible entre fuentes (`--max-per-source`)**: con 11 fuentes configuradas, limita
  cuántos canales aporta cada una (por ejemplo `--max-per-source 8`) para que ninguna lista
  acapare el resultado. `0` (valor por defecto) no aplica límite.
- **Sin canales repetidos**: deduplica por identidad canónica de canal (normalizando sufijos como
  `HD`, `FHD`, `4K`, `1080p`, `720p`, `[Geo-blocked]`, `En Vivo`, `Señal 2`, prefijos de país y
  numeración), `slug`, `tvg-id`, URL del stream y URL final tras redirecciones HTTP. Además, al
  sincronizar con Supabase consulta primero los canales existentes para no repetir los ya guardados.
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

### Cómo se resuelven los reproductores

Las tres últimas fuentes no publican el `.m3u8` en la página del canal: lo cargan con
JavaScript. Para cada página de canal el extractor:

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
paralelismo y `--max-checks` para acotar la cantidad máxima de pruebas cuando se busca completar
el cupo de `--limit`.

Estados habituales: `http_ok` (respuesta HTTP satisfactoria; en M3U se reconoce `#EXTM3U`),
`invalid_playlist`, `restricted` (por ejemplo, HTTP 401/403), `http_error`, `unreachable`,
`not_live` (embed de YouTube que no declara emisión en vivo) y `unsupported` (protocolo
distinto de HTTP(S)). Si un servidor rechaza la cabecera `Range` (por
ejemplo con HTTP 416), se reintenta automáticamente sin `Range` leyendo únicamente los primeros
4 KiB.

## Clasificación

Categorías principales: `sports`, `news`, `movies`, `series`, `kids`, `documentary`, `music`,
`business`, `culture`, `travel`, `cooking`, `religious`, `weather`, `general` y `other`.

La clasificación es heurística. Se conserva `unknown` cuando no hay señales suficientes para idioma
o país; la nacionalidad de una cadena o canal no se usa automáticamente como idioma.

## Desarrollo y pruebas

Requiere Python 3.11 o posterior. Las pruebas unitarias se ejecutan sin acceder a Internet:

```sh
python -m unittest discover -s tests -v
```

## GitHub Actions: sincronización automática

El workflow `.github/workflows/sync-tv-channels.yml` permite ejecutarse manualmente desde
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
alguna fuente responda muy lento y `--max-per-source 8` para repartir el cupo entre las 11
fuentes configuradas (ajústalo o quítalo si prefieres que una sola lista llene los 20 canales).

Configura estos secretos en **Settings → Secrets and variables → Actions**:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (o `SUPABASE_SECRET_KEY`)

Si los secretos no están configurados, el workflow **no falla**: avisa de que se omite la
sincronización y sigue exportando y guardando `data/tv_channels.json`.
