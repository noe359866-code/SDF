import csv
import email.message
import gzip
import json
import os
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from sdf_tv_channels import (
    DEFAULT_MAX_CHANNELS,
    DEFAULT_SOURCES,
    Channel,
    LinkParser,
    SourceConfig,
    StreamCheck,
    _canonical_channel_key,
    _dedupe,
    _fetch_source,
    _fetch_text,
    channel_slug,
    check_stream,
    check_streams,
    classify_channel,
    clean_channel_name,
    dedupe_unique_channels,
    fetch_existing_supabase_channels,
    main,
    parse_m3u,
    read_playlist,
    select_channels_with_checks,
    sync_to_supabase,
    write_output,
)


class Headers(email.message.Message):
    """Minimal stand-in for ``http.client.HTTPMessage``."""

    def __init__(self, values=None):
        super().__init__()
        for key, value in (values or {}).items():
            self[key] = value


class FakeResponse:
    def __init__(self, body, status=200, content_type="application/vnd.apple.mpegurl",
                 url="https://example.com/live.m3u8"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def geturl(self):
        return self.url


class PlaylistParserTests(unittest.TestCase):
    def test_extracts_attributes_and_name_with_comma(self):
        text = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="news.es" tvg-name="Noticias" '
            'tvg-logo="https://example.com/logo.png" group-title="News",'
            'Noticias, Internacional\n'
            'https://example.com/live.m3u8\n'
        )
        channels = parse_m3u(text, source="Test")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Noticias, Internacional")
        self.assertEqual(channels[0].tvg_id, "news.es")
        self.assertEqual(channels[0].category, "news")
        self.assertEqual(channels[0].country, "ES")
        self.assertEqual(channels[0].source, "Test")

    def test_reads_language_and_country_codes(self):
        self.assertEqual(
            classify_channel(
                "Canal ejemplo",
                attrs={"tvg-language": "es-MX", "tvg-country": "MEX"},
            ),
            ("other", "es", "MX"),
        )

    def test_language_classification_avoids_short_substring_false_positive(self):
        self.assertEqual(classify_channel("ESPN"), ("sports", "unknown", "unknown"))
        # A Spanish-language brand/group is still a useful language signal.
        self.assertEqual(classify_channel("ESPN Deportes", "Deportes")[:2], ("sports", "es"))

    def test_country_from_standard_tvg_id_suffix_and_prefix(self):
        self.assertEqual(classify_channel("CNN", attrs={"tvg-id": "CNN.us"})[2], "US")
        self.assertEqual(classify_channel("CNN", attrs={"tvg-id": "CNN.us@HD"})[2], "US")
        self.assertEqual(classify_channel("[MX] Azteca Uno")[2], "MX")

    def test_supports_more_language_codes(self):
        self.assertEqual(classify_channel("Canal", attrs={"tvg-language": "ru-RU"})[1], "ru")
        self.assertEqual(classify_channel("Canal", attrs={"tvg-language": "zho"})[1], "zh")

    def test_category_matching_uses_word_boundaries(self):
        self.assertEqual(classify_channel("Television TV")[0], "other")
        self.assertEqual(classify_channel("Drama TV")[0], "series")

    def test_skips_invalid_and_orphan_urls(self):
        self.assertEqual(
            parse_m3u("#EXTM3U\nhttps://example.com/orphan.m3u8\n#EXTINF:-1,Bad\nnot-a-url\n"),
            [],
        )

    def test_deduplicates_exact_entries(self):
        text = (
            "#EXTM3U\n#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n"
            "#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n"
        )
        self.assertEqual(len(parse_m3u(text)), 1)

    def test_keeps_alternate_sources(self):
        text = (
            "#EXTM3U\n#EXTINF:-1,News TV\nhttps://example.com/a.m3u8\n"
            "#EXTINF:-1,News TV\nhttps://example.com/b.m3u8\n"
        )
        self.assertEqual(len(parse_m3u(text)), 2)

    def test_empty_playlist(self):
        self.assertEqual(parse_m3u("#EXTM3U\n"), [])

    def test_resolves_relative_urls_in_remote_playlists(self):
        channels = parse_m3u(
            "#EXTM3U\n#EXTINF:-1,Canal\n../streams/live.m3u8\n",
            base_url="https://example.com/lists/tv.m3u",
        )
        self.assertEqual(channels[0].url, "https://example.com/streams/live.m3u8")

    def test_supports_extgrp_group_override(self):
        channels = parse_m3u(
            "#EXTM3U\n#EXTINF:-1,Noticias 24\n#EXTGRP:Noticias\n"
            "https://example.com/news.m3u8\n"
        )
        self.assertEqual(channels[0].group, "Noticias")
        self.assertEqual(channels[0].category, "news")

    def test_does_not_parse_hls_segments_as_tv_channels(self):
        text = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
            "#EXTINF:6.0,\nsegment-1.ts\n#EXT-X-ENDLIST\n"
        )
        self.assertEqual(parse_m3u(text), [])

    def test_resolves_relative_local_playlist_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = Path(temp_dir) / "channels.m3u"
            playlist.write_text("#EXTM3U\n#EXTINF:-1,Canal\nstreams/live.ts\n", encoding="utf-8")
            channels = read_playlist(playlist)
            expected = (Path(temp_dir) / "streams" / "live.ts").resolve().as_uri()
            self.assertEqual(channels[0].url, expected)

    def test_url_dedupe_preserves_case_sensitive_paths(self):
        channels = [
            Channel("Canal", "http://EXAMPLE.com:80/Live.m3u8", source="Uno"),
            Channel("Canal", "http://example.com/Live.m3u8", source="Dos"),
            Channel("Canal", "http://example.com/live.m3u8", source="Tres"),
        ]
        unique = _dedupe(channels)
        self.assertEqual(len(unique), 2)
        self.assertIn("Uno", unique[0].source)
        self.assertIn("Dos", unique[0].source)

    def test_configured_sources(self):
        urls = {s.url for s in DEFAULT_SOURCES}
        self.assertEqual(
            urls,
            {
                "https://iptv.bbyte.app/jellyfin/live.m3u",
                "https://www.cxtvenvivo.com/",
                "https://www.tdtchannels.com/lists/tv.m3u8",
                "https://m3u.cl/lista/top.m3u",
                "https://m3u.cl/lista/LATAM.m3u",
                "https://iptv-org.github.io/iptv/index.m3u",
                "https://teleonline.org/",
                "https://teleonline.github.io/listas/tv.m3u8",
            },
        )

    def test_remote_source_resolves_playlist_relative_to_its_url(self):
        source = SourceConfig("remote", "Remote", "https://example.com/lists/tv.m3u")
        text = "#EXTM3U\n#EXTINF:-1,Canal\n../streams/live.m3u8\n"
        with patch("sdf_tv_channels._fetch_text", return_value=text):
            channels, errors = _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(errors, [])
        self.assertEqual(channels[0].url, "https://example.com/streams/live.m3u8")

    def test_zero_max_pages_does_not_fetch_site_pages(self):
        source = SourceConfig("site", "Site", "https://example.com/", "site",
                              page_prefixes=("/tv/",))
        with patch(
            "sdf_tv_channels._fetch_text", return_value='<a href="/tv/one">One</a>'
        ) as fetch:
            channels, errors = _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(channels, [])
        self.assertEqual(errors, [])
        fetch.assert_called_once_with(source.url, 1)

    def test_link_parser_preserves_anchor_label(self):
        parser = LinkParser()
        parser.feed('<a href="/canal/antena-3">Antena 3</a>')
        parser.close()
        self.assertEqual(parser.links, [("/canal/antena-3", "Antena 3")])


class DedupeAndLimitTests(unittest.TestCase):
    def test_clean_channel_name_and_canonical_key_normalize_variants(self):
        self.assertEqual(
            clean_channel_name("101. [MX] Ver Azteca Uno [1080p] [Geo-blocked] En Vivo - CXTv"),
            "Azteca Uno",
        )
        self.assertEqual(_canonical_channel_key("Antena 3 HD"), "antena-3")
        self.assertEqual(_canonical_channel_key("Antena 3 (1080p) Señal 2"), "antena-3")
        self.assertEqual(_canonical_channel_key("Antena 3 En Vivo"), "antena-3")
        self.assertEqual(_canonical_channel_key("Discovery Channel"), "discovery")
        self.assertEqual(_canonical_channel_key("Canal Discovery Latam"), "discovery")
        self.assertEqual(_canonical_channel_key("Canal 24 Horas"), "24-horas")
        self.assertEqual(_canonical_channel_key("Canal 13 HD"), "canal-13")
        self.assertEqual(channel_slug("Antena 3 [720p]"), "antena-3")

    def test_m3u_export_and_logo_sanitization(self):
        channels = parse_m3u(
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="a3.es" tvg-logo="N/A" group-title="General",Antena 3\n'
            'https://example.com/a3.m3u8\n'
            '#EXTINF:-1 tvg-id="la1.es" tvg-logo="/logos/la1.png" group-title="General",La 1\n'
            'https://example.com/la1.m3u8\n',
            base_url="https://example.com/lists/tv.m3u8",
        )
        self.assertEqual(channels[0].logo, "")
        self.assertEqual(channels[1].logo, "https://example.com/logos/la1.png")
        with tempfile.TemporaryDirectory() as temp_dir:
            m3u_path = Path(temp_dir) / "exported.m3u8"
            write_output(channels, m3u_path, "m3u")
            exported_text = m3u_path.read_text(encoding="utf-8")
            reparsed = parse_m3u(exported_text)
            self.assertEqual(len(reparsed), 2)
            self.assertEqual(reparsed[1].logo, "https://example.com/logos/la1.png")

    def test_dedupe_unique_channels_prevents_repeated_names_tvg_ids_and_urls(self):
        channels = [
            Channel("CNN", "http://example.com/cnn-low.m3u8", category="news"),
            Channel(
                "CNN HD [1080p]",
                "https://example.com/cnn-hd.m3u8",
                tvg_id="CNN.us",
                logo="https://example.com/cnn.png",
                country="US",
                language="en",
                category="news",
            ),
            Channel("CNN En Vivo", "https://example.com/cnn-mirror.m3u8", category="news"),
            Channel("Cable News Network", "https://example.com/cnn-other.m3u8", tvg_id="CNN.us", category="news"),
            Channel("ESPN Deportes", "https://example.com/espn.m3u8", category="sports", language="es"),
        ]
        unique = dedupe_unique_channels(channels, limit=20)
        self.assertEqual(len(unique), 2)
        names = {c.name for c in unique}
        self.assertEqual(names, {"CNN HD", "ESPN Deportes"})
        cnn = next(c for c in unique if "CNN" in c.name)
        self.assertEqual(cnn.url, "https://example.com/cnn-hd.m3u8")
        self.assertEqual(cnn.logo, "https://example.com/cnn.png")

    def test_limits_to_20_unique_channels_by_default(self):
        channels = [
            Channel(f"Canal Único {i}", f"https://example.com/stream-{i}.m3u8", category="news")
            for i in range(1, 35)
        ]
        # Add duplicates of each channel with HD/mirror suffixes
        channels.extend(
            Channel(f"Canal Único {i} HD [1080p]", f"https://mirror.example.com/stream-{i}.m3u8", category="news")
            for i in range(1, 35)
        )
        selected = dedupe_unique_channels(channels, limit=DEFAULT_MAX_CHANNELS)
        self.assertEqual(len(selected), 20)
        slugs = [channel_slug(c.name) for c in selected]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_select_channels_with_checks_stops_early_and_uses_fallback_stream(self):
        channels = [
            Channel("Canal Uno", "https://example.com/uno-dead.m3u8", category="news"),
            Channel("Canal Uno HD", "https://example.com/uno-live.m3u8", category="news"),
            Channel("Canal Dos", "https://example.com/dos-live.m3u8", category="sports"),
            Channel("Canal Tres", "https://example.com/tres-live.m3u8", category="movies"),
        ]

        def fake_check(url, timeout=10):
            if "dead" in url:
                return StreamCheck("unreachable", detail="offline")
            return StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", url)

        with patch("sdf_tv_channels.check_stream", side_effect=fake_check):
            selected, checks = select_channels_with_checks(
                channels, limit=2, timeout=1, workers=2, only_http_ok=True,
            )
        self.assertEqual(len(selected), 2)
        selected_slugs = {_canonical_channel_key(c.name) for c in selected}
        self.assertEqual(len(selected_slugs), 2)
        for ch in selected:
            self.assertEqual(checks[ch.url].status, "http_ok")

    def test_sync_to_supabase_adds_at_most_20_non_repeating_channels(self):
        channels = []
        checks = {}
        for i in range(1, 30):
            url = f"https://example.com/ch-{i}.m3u8"
            mirror_url = f"https://mirror.example.com/ch-{i}.m3u8"
            channels.append(Channel(f"Canal {i}", url, country="MX", category="general"))
            channels.append(Channel(f"Canal {i} [1080p]", mirror_url, country="MX", category="general"))
            checks[url] = StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", url)
            checks[mirror_url] = StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", mirror_url)

        captured_payload = []

        def fake_urlopen(req, timeout=30):
            captured_payload.extend(json.loads(req.data.decode("utf-8")))
            return FakeResponse(b"", status=201)

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            synced = sync_to_supabase(
                channels,
                "https://project.supabase.co",
                "secret-key",
                stream_checks=checks,
                activation_mode="automatic",
            )

        self.assertEqual(synced, 20)
        self.assertEqual(len(captured_payload), 20)
        slugs = [row["slug"] for row in captured_payload]
        urls = [row["stream_url"] for row in captured_payload]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(row["is_active"] for row in captured_payload))

    def test_sync_to_supabase_skips_existing_channels_when_new_ones_available(self):
        channels = [
            Channel("Canal Existente", "https://example.com/existing.m3u8", country="ES"),
            Channel("Canal Nuevo", "https://example.com/new.m3u8", country="ES"),
        ]
        existing_keys = ({"canal-existente"}, {"https://example.com/existing.m3u8"}, {"canal-existente"})
        captured_payload = []

        def fake_urlopen(req, timeout=30):
            captured_payload.extend(json.loads(req.data.decode("utf-8")))
            return FakeResponse(b"", status=201)

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            synced = sync_to_supabase(
                channels,
                "https://project.supabase.co",
                "secret-key",
                activation_mode="manual",
                existing_keys=existing_keys,
            )

        self.assertEqual(synced, 1)
        self.assertEqual(captured_payload[0]["slug"], "canal-nuevo")

    def test_fetch_existing_supabase_channels_parses_response(self):
        body = json.dumps([
            {"slug": "antena-3", "stream_url": "https://example.com/a3.m3u8", "name": "Antena 3 HD"},
        ]).encode("utf-8")
        with patch("sdf_tv_channels.urlopen", return_value=FakeResponse(body, status=200, content_type="application/json")):
            slugs, urls, names = fetch_existing_supabase_channels("https://project.supabase.co", "secret")
        self.assertIn("antena-3", slugs)
        self.assertIn("https://example.com/a3.m3u8", urls)
        self.assertIn("antena-3", names)

    def test_cli_main_exports_20_unique_channels_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = Path(temp_dir) / "input.m3u"
            output = Path(temp_dir) / "out.json"
            lines = ["#EXTM3U"]
            for i in range(1, 28):
                lines.append(f'#EXTINF:-1 group-title="News",Canal {i}')
                lines.append(f"https://example.com/live-{i}.m3u8")
                lines.append(f'#EXTINF:-1 group-title="News",Canal {i} HD [1080p]')
                lines.append(f"https://mirror.example.com/live-{i}.m3u8")
            playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")

            rc = main([str(playlist), "-o", str(output)])
            self.assertEqual(rc, 0)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(exported), 20)
            slugs = {channel_slug(item["name"]) for item in exported}
            self.assertEqual(len(slugs), 20)


class StreamCheckTests(unittest.TestCase):
    def test_recognizes_hls_and_reads_only_a_small_range(self):
        response = FakeResponse(b"\xef\xbb\xbf#EXTM3U\n#EXT-X-VERSION:3\n", status=206)
        with patch("sdf_tv_channels.urlopen", return_value=response) as open_url:
            result = check_stream("https://example.com/live.m3u8?token=abc")
        self.assertEqual(result.status, "http_ok")
        self.assertEqual(result.http_status, 206)
        self.assertIn("Playlist header detected", result.detail)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Range"), "bytes=0-4095")

    def test_retries_without_range_on_http_416(self):
        err_416 = HTTPError("https://example.com/live.m3u8", 416, "Range Not Satisfiable", {}, None)
        ok_response = FakeResponse(b"#EXTM3U\n#EXT-X-VERSION:3\n", status=200)
        with patch("sdf_tv_channels.urlopen", side_effect=[err_416, ok_response]) as open_url:
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "http_ok")
        self.assertEqual(open_url.call_count, 2)
        retry_request = open_url.call_args_list[1].args[0]
        self.assertIsNone(retry_request.get_header("Range"))

    def test_flags_html_returned_for_m3u_url(self):
        response = FakeResponse(b"<html>sign in</html>", status=200)
        with patch("sdf_tv_channels.urlopen", return_value=response):
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "invalid_playlist")

    def test_reports_restricted_http_status_without_bypassing_it(self):
        error = HTTPError("https://example.com/live.m3u8", 403, "Forbidden", {}, None)
        with patch("sdf_tv_channels.urlopen", side_effect=error):
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "restricted")
        self.assertEqual(result.http_status, 403)

    def test_reports_network_errors_and_unsupported_schemes(self):
        with patch("sdf_tv_channels.urlopen", side_effect=URLError("offline")):
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "unreachable")
        self.assertEqual(check_stream("rtmp://example.com/live").status, "unsupported")

    def test_check_results_are_embedded_in_json_and_csv_exports(self):
        channel = Channel("Canal", "https://example.com/live.m3u8")
        checks = {channel.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl")}
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "channels.json"
            csv_path = Path(temp_dir) / "channels.csv"
            write_output([channel], json_path, "json", checks)
            write_output([channel], csv_path, "csv", checks)
            json_row = json.loads(json_path.read_text(encoding="utf-8"))[0]
            self.assertEqual(json_row["stream_check"]["status"], "http_ok")
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_row = next(csv.DictReader(handle))
            self.assertEqual(csv_row["stream_status"], "http_ok")
            self.assertEqual(csv_row["stream_http_status"], "200")

    def test_non_http_streams_are_returned_as_unsupported(self):
        channel = Channel("UDP Canal", "udp://239.0.0.1:1234")
        self.assertEqual(check_streams([channel])[channel.url].status, "unsupported")


class FetchTextTests(unittest.TestCase):
    """Regression tests for the HTTP download layer (compression, size, retries)."""

    class Response:
        def __init__(self, body, encoding="", charset="utf-8"):
            self.body = body
            self.headers = Headers({"Content-Encoding": encoding})
            self._charset = charset

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit=-1):
            return self.body if limit < 0 else self.body[:limit]

        def get_content_charset(self):
            return self._charset

    def test_decodes_gzip_responses(self):
        body = gzip.compress("#EXTM3U\n#EXTINF:-1,Canal\nhttps://example.com/live.m3u8\n".encode())
        with patch("sdf_tv_channels.urlopen", return_value=self.Response(body, "gzip")):
            text = _fetch_text("https://example.com/list.m3u", timeout=1, retries=0)
        self.assertIn("#EXTM3U", text)
        self.assertEqual(len(parse_m3u(text)), 1)

    def test_decodes_deflate_responses(self):
        body = zlib.compress("#EXTM3U\n#EXTINF:-1,Canal\nhttps://example.com/live.m3u8\n".encode())
        with patch("sdf_tv_channels.urlopen", return_value=self.Response(body, "deflate")):
            text = _fetch_text("https://example.com/list.m3u", timeout=1, retries=0)
        self.assertIn("#EXTM3U", text)

    def test_truncates_oversized_responses_instead_of_failing(self):
        body = b"#EXTM3U\n" + b"x" * 5000
        with patch("sdf_tv_channels.urlopen", return_value=self.Response(body)):
            text = _fetch_text("https://example.com/list.m3u", timeout=1,
                               max_bytes=1024, retries=0)
        self.assertEqual(len(text), 1024)

    def test_retries_transient_statuses(self):
        error = HTTPError("https://example.com/l.m3u", 503, "Busy", {}, None)
        body = b"#EXTM3U\n"
        with patch("sdf_tv_channels.urlopen", side_effect=[error, self.Response(body)]) as open_url:
            text = _fetch_text("https://example.com/l.m3u", timeout=1, retries=2)
        self.assertIn("#EXTM3U", text)
        self.assertEqual(open_url.call_count, 2)

    def test_raises_after_retries_are_exhausted(self):
        error = HTTPError("https://example.com/l.m3u", 404, "Not Found", {}, None)
        with patch("sdf_tv_channels.urlopen", side_effect=error):
            with self.assertRaises(HTTPError):
                _fetch_text("https://example.com/l.m3u", timeout=1, retries=1)

    def test_deadline_marks_pending_checks_as_timeout(self):
        def slow_check(url, timeout=10):
            time.sleep(0.05)
            return StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", url)

        channels = [Channel(f"Canal {i}", f"https://example.com/{i}.m3u8") for i in range(5)]
        with patch("sdf_tv_channels.check_stream", side_effect=slow_check):
            results = check_streams(channels, timeout=1, workers=5,
                                    deadline=time.monotonic() + 0.01)
        self.assertTrue(results)
        self.assertTrue(any(check.status == "timeout" for check in results.values()))


class ExitCodeTests(unittest.TestCase):
    """The CLI must keep working (and keep the exported file) when Supabase fails."""

    def _playlist(self, temp_dir):
        playlist = Path(temp_dir) / "input.m3u"
        playlist.write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="News",Canal Uno\nhttps://example.com/uno.m3u8\n'
            '#EXTINF:-1 group-title="Sports",Canal Dos\nhttps://example.com/dos.m3u8\n',
            encoding="utf-8",
        )
        return playlist

    def test_missing_credentials_exports_file_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            env = {k: v for k, v in os.environ.items()
                   if k not in {"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                                "SUPABASE_SECRET_KEY", "SUPABASE_KEY"}}
            with patch.dict(os.environ, env, clear=True):
                rc = main([str(playlist), "--sync-supabase", "-o", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)

    def test_sync_failure_does_not_abort_the_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch.dict(os.environ,
                            {"SUPABASE_URL": "https://project.supabase.co",
                             "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True), \
                 patch("sdf_tv_channels.fetch_existing_supabase_channels",
                       return_value=(set(), set(), set())), \
                 patch("sdf_tv_channels.sync_to_supabase",
                       side_effect=RuntimeError("No se pudo sincronizar con Supabase: HTTP 401")):
                rc = main([str(playlist), "--sync-supabase", "-o", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)

    def test_sync_failure_can_still_fail_the_run_on_demand(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch.dict(os.environ,
                            {"SUPABASE_URL": "https://project.supabase.co",
                             "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True), \
                 patch("sdf_tv_channels.fetch_existing_supabase_channels",
                       return_value=(set(), set(), set())), \
                 patch("sdf_tv_channels.sync_to_supabase",
                       side_effect=RuntimeError("boom")):
                rc = main([str(playlist), "--sync-supabase", "--fail-on-sync-error",
                           "-o", str(output)])
            self.assertEqual(rc, 1)

    def test_automatic_activation_enables_stream_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch("sdf_tv_channels.check_stream",
                       return_value=StreamCheck("http_ok", 200,
                                                "application/vnd.apple.mpegurl")) as check:
                rc = main([str(playlist), "--activation-mode", "automatic",
                           "--limit", "5", "-o", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(check.call_count, 2)

    def test_missing_input_file_is_still_a_usage_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(SystemExit) as ctx:
                main([str(Path(temp_dir) / "nope.m3u"), "-o", str(Path(temp_dir) / "o.json")])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
