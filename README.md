# SDF — Extractor y clasificador de canales de TV

Herramienta de línea de comandos para leer listas M3U/M3U8 locales o fuentes públicas, normalizar
los canales y exportarlos a JSON o CSV.

## Qué hace

- Lee playlists con nombres que incluyen comas, metadatos `tvg-*`, `#EXTGRP` y URLs relativas.
- Resuelve rutas relativas de listas locales y remotas; no confunde segmentos de una playlist HLS
  con canales de TV.
- Clasifica categoría, idioma y país usando primero los metadatos explícitos. Como alternativa usa
  nombres/grupos y sufijos de país habituales en `tvg-id` (por ejemplo, `cnn.us`). Las coincidencias
  usan límites de palabra para reducir falsos positivos como inferir español porque un nombre
  contiene `ESPN`.
- Deduplica URLs sin tratar como iguales rutas que distinguen mayúsculas, y conserva metadatos y
  fuentes alternativos.
- Puede comprobar, de forma opcional, si una URL HTTP(S) responde y si un manifiesto M3U contiene
  una cabecera válida. La comprobación es acotada y concurrente.

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
# Leer una lista local
python sdf_tv_channels.py ./playlist.m3u -o channels.json

# Descargar todas las fuentes configuradas
python sdf_tv_channels.py --all-sources -o data/tv_channels.json

# Elegir varias fuentes
python sdf_tv_channels.py --source tdtchannels --source teleonline -o data/tv_channels.json

# Filtrar antes de exportar
python sdf_tv_channels.py --all-sources --category sports -o data/sports.json
python sdf_tv_channels.py --all-sources --language es -o data/spanish.json
python sdf_tv_channels.py --all-sources --country MX -o data/mexico.csv --format csv

# Añadir un diagnóstico de alcance HTTP al resultado
python sdf_tv_channels.py ./playlist.m3u --check-streams -o comprobados.csv

# Activación manual: importa y activa los canales encontrados
export SUPABASE_URL="https://tu-proyecto.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="tu-secret-key"
python sdf_tv_channels.py --all-sources --sync-supabase

# Activación automática: solo activa streams que responden correctamente
python sdf_tv_channels.py --all-sources --check-streams --sync-supabase --activation-mode automatic
```

`--check-streams` añade `stream_check` a cada objeto JSON; en CSV añade `stream_status`,
`stream_http_status`, `stream_content_type`, `stream_final_url` y `stream_detail`. También imprime
un resumen por estado. Usa `--timeout` para limitar cada petición y `--workers` para ajustar el
paralelismo.

Estados habituales: `http_ok` (respuesta HTTP satisfactoria; en M3U se reconoce `#EXTM3U`),
`invalid_playlist`, `restricted` (por ejemplo, HTTP 401/403), `http_error`, `unreachable` y
`unsupported` (protocolo distinto de HTTP(S)). La comprobación solicita como máximo los primeros
4 KiB y **no garantiza que el canal se pueda reproducir**: no verifica todos los segmentos, codecs,
audio, geobloqueos ni disponibilidad futura. No intenta saltarse autenticación, DRM, paywalls,
geo-bloqueos ni protecciones anti-bot.

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
**Actions → Sync TV channels to Supabase → Run workflow** y también se ejecuta automáticamente
cada 5 horas. La activación automática comprueba los streams y escribe `is_active = true`
únicamente para los que responden correctamente.

Configura estos secretos en **Settings → Secrets and variables → Actions**:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (o `SUPABASE_SECRET_KEY`)

La programación `0 */5 * * *` usa UTC y corre a las horas 00, 05, 10, 15 y 20 UTC.
