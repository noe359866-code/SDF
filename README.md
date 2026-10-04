# SDF — Extractor y clasificador de canales de TV

Herramienta de línea de comandos para leer listas M3U/M3U8 locales o fuentes públicas, normalizar
los canales, eliminar duplicados y exportarlos a JSON, CSV o Supabase.

## Qué hace

- **Límite por defecto de 20 canales únicos (`--limit 20`)**: selecciona hasta 20 canales variados,
  priorizados por calidad de metadatos (logo, país, idioma, categoría, HTTPS/HLS) y distribuidos
  entre distintas categorías. Usa `--limit 0` si deseas exportar sin límite.
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

Lista las fuentes y sus claves con:

```sh
python sdf_tv_channels.py --list-sources
```

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
```

`--check-streams` añade `stream_check` a cada objeto JSON; en CSV añade `stream_status`,
`stream_http_status`, `stream_content_type`, `stream_final_url` y `stream_detail`. También imprime
un resumen por estado. Usa `--timeout` para limitar cada petición, `--workers` para ajustar el
paralelismo y `--max-checks` para acotar la cantidad máxima de pruebas cuando se busca completar
el cupo de `--limit`.

Estados habituales: `http_ok` (respuesta HTTP satisfactoria; en M3U se reconoce `#EXTM3U`),
`invalid_playlist`, `restricted` (por ejemplo, HTTP 401/403), `http_error`, `unreachable` y
`unsupported` (protocolo distinto de HTTP(S)). Si un servidor rechaza la cabecera `Range` (por
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
canales, por defecto `20`, y el modo de activación) y también se ejecuta automáticamente cada
5 horas (`0 */5 * * *` UTC). En cada ejecución verifica candidatos por lotes y agrega hasta
20 canales únicos y activos (`is_active = true`) sin repetir.

Configura estos secretos en **Settings → Secrets and variables → Actions**:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (o `SUPABASE_SECRET_KEY`)
