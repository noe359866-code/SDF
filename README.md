# SDF — TV Channel Extractor & Classifier

A lightweight Python 3.10+ CLI that parses **local, user-supplied M3U/M3U8 playlists**, extracts channel metadata, classifies channels, and exports JSON or CSV.

> Use only playlists and streams you are authorized to access. This tool parses playlist metadata only; it does not crawl third-party websites, bypass authentication/DRM, or probe stream availability.

## Features

- Extracts channel name, URL, group, `tvg-id`, `tvg-name`, and `tvg-logo`.
- Rule-based categories: sports, news, movies, series, kids, documentary, music, general, or other.
- Detects common language and country labels when metadata/name hints are available; unknown values remain explicit.
- Removes exact duplicate name/URL pairs but preserves alternate sources.
- Exports JSON or CSV, with category/language/country filters.
- Standard library only; no third-party dependencies.

## Usage

`python sdf_tv_channels.py ./playlist.m3u -o channels.json`

`python sdf_tv_channels.py ./playlist.m3u8 -o channels.csv`

`python sdf_tv_channels.py ./playlist.m3u --category sports -o sports.json`

`python sdf_tv_channels.py ./playlist.m3u --language es -o spanish.json`

`python sdf_tv_channels.py ./playlist.m3u --country MX -o mexico.csv --format csv`

Run tests with `python -m unittest discover -s tests -v`.

Classification is heuristic; playlist naming conventions vary, so review results before production use. Unsupported language/country values fall back to keyword detection or `unknown`.
