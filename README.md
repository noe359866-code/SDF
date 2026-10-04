# SDF — TV Channel Extractor & Classifier

Extractor y clasificador de canales de TV para SDF.

## Fuentes configuradas

- BByte Jellyfin — https://iptv.bbyte.app/jellyfin/live.m3u
- CXTv — https://www.cxtvenvivo.com/
- TDTChannels — https://www.tdtchannels.com/lists/tv.m3u8
- m3u.cl Top — https://m3u.cl/lista/top.m3u
- m3u.cl LATAM — https://m3u.cl/lista/LATAM.m3u
- IPTV-org — https://iptv-org.github.io/iptv/index.m3u
- Teleonline — https://teleonline.org/; también detecta su playlist pública M3U8.

## Uso

    python sdf_tv_channels.py --list-sources
    python sdf_tv_channels.py --all-sources -o data/tv_channels.json
    python sdf_tv_channels.py --source tdtchannels --source teleonline -o data/tv_channels.json
    python sdf_tv_channels.py --all-sources --category sports -o data/sports.json
    python sdf_tv_channels.py --all-sources --language es -o data/spanish.json
    python sdf_tv_channels.py --all-sources --country MX -o data/mexico.csv --format csv

También se mantiene el modo local:

    python sdf_tv_channels.py ./playlist.m3u -o channels.json

## Clasificación

Categorías principales: sports, news, movies, series, kids, documentary, music, business,
culture, travel, cooking, religious, weather, general y other.

El idioma y país se infieren de metadatos y del texto del canal; cuando no hay señales
suficientes se conserva unknown.

## Límites

Procesa contenido públicamente accesible. No intenta saltarse autenticación, DRM,
paywalls, geo-bloqueos ni protecciones anti-bot, y no prueba la disponibilidad de cada stream.
La clasificación es heurística.
