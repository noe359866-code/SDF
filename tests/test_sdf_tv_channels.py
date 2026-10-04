import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from sdf_tv_channels import (
    DEFAULT_SOURCES,
    Channel,
    SourceConfig,
    StreamCheck,
    _dedupe,
    _fetch_source,
    check_stream,
    check_streams,
    classify_channel,
    parse_m3u,
    read_playlist,
    write_output,
)


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

    def test_country_from_standard_tvg_id_suffix(self):
        self.assertEqual(classify_channel("CNN", attrs={"tvg-id": "CNN.us"})[2], "US")

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


if __name__ == "__main__":
    unittest.main()
