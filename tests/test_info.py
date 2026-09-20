import json
import struct
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import remote_bluray as app


class FakeImage:
    volume_id = "Example Blu-ray"
    remote = SimpleNamespace(size=123456789)

    def find(self, path):
        if path in {"/AACS", "/BDMV/JAR"}:
            return object()
        if path == "/BDMV/STREAM/00001.m2ts":
            return SimpleNamespace(size=987654321)
        raise FileNotFoundError(path)


class InfoTests(TestCase):
    def setUp(self):
        self.playlist = app.Playlist(
            name="00800.MPLS",
            items=(app.PlaylistItem("00001", "M2TS", 0, 90000),),
            size_bytes=987654321,
            unique_size_bytes=987654321,
        )

    def test_partial_probe_uses_short_low_traffic_default_window(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [], "format": {}}),
            stderr="",
        )
        with patch.object(app, "get_executable", return_value="ffprobe"), patch.object(
            app.subprocess, "run", return_value=completed
        ) as run:
            payload = app.probe_media_info(["-i", "virtual.m2ts"], "partial")

        self.assertEqual(payload["streams"], [])
        command = run.call_args.args[0]
        self.assertIn("-read_intervals", command)
        self.assertIn("%+10", command)

    def test_packet_bitrates_use_the_scan_window(self):
        media_info = {"streams": [{"index": 0, "codec_type": "video"}]}

        app.apply_packet_bitrates(media_info, {0: 1_000_000}, 200, "partial")

        self.assertEqual(app.format_bitrate_value(media_info["streams"][0]["bit_rate"]), "800 kbps")

    def test_partial_probe_accepts_custom_duration(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [], "format": {}}),
            stderr="",
        )
        with patch.object(app, "get_executable", return_value="ffprobe"), patch.object(
            app.subprocess, "run", return_value=completed
        ) as run:
            app.probe_media_info(["-i", "virtual.m2ts"], "partial", 300)

        command = run.call_args.args[0]
        self.assertIn("%+300", command)

    def test_report_contains_bdinfo_style_stream_sections(self):
        media_info = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24000/1001",
                    "display_aspect_ratio": "16:9",
                    "profile": "High",
                    "level": 41,
                    "bit_rate": "32682000",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "dts",
                    "profile": "DTS-HD MA",
                    "channel_layout": "7.1",
                    "sample_rate": "48000",
                    "bits_per_sample": 24,
                    "bit_rate": "4017000",
                    "tags": {"language": "eng"},
                },
                {
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "bit_rate": "28636",
                    "tags": {"language": "eng"},
                },
            ]
        }

        report = app.format_info_report(FakeImage(), self.playlist, media_info, "partial")

        self.assertIn("DISC INFO:\n", report)
        self.assertIn("Protection:     AACS", report)
        self.assertIn("Extras:         BD-Java", report)
        self.assertIn(f"BDInfo:         remote-bluray {app.__version__} (ffprobe)", report)
        self.assertIn("Middle sample: 10 seconds at 0s", report)
        self.assertIn("MPEG-4 AVC Video", report)
        self.assertIn("32682 kbps", report)
        self.assertIn("English", report)
        self.assertIn("28.636 kbps", report)
        self.assertIn("FILES:", report)
        self.assertIn("00001.M2TS", report)
        self.assertNotIn("-" * 100, report)
        self.assertNotIn("-----                            -------", report)
        self.assertIn("VIDEO:\n\nCodec", report)
        self.assertIn("\n\n---\n\nMPEG-4 AVC Video", report)
        self.assertIn("AUDIO:\n\nCodec", report)

    def test_cs0_volume_labels_ignore_fixed_field_padding(self):
        self.assertEqual(
            app.decode_cs0(b"\x08GERMANY_YEAR_ZERO\x00\x00\x00\x12"),
            "GERMANY_YEAR_ZERO",
        )

    def test_random_screenshot_times_are_seeded_and_bounded(self):
        first = app.random_screenshot_times(300, 5, seed=1948, start_seconds=60)
        second = app.random_screenshot_times(300, 5, seed=1948, start_seconds=60)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 5)
        self.assertTrue(all(60 <= value < 300 for value in first))

    def test_screenshot_skip_must_leave_a_time_window(self):
        with self.assertRaisesRegex(ValueError, "Increase --scan-duration"):
            app.random_screenshot_times(300, 1, start_seconds=300)

    def test_info_parser_accepts_screenshot_options(self):
        args = app.build_parser().parse_args(
            [
                "info",
                "source.iso",
                "--scan",
                "partial",
                "--scan-duration",
                "00:05:00",
                "--screenshots",
                "3",
                "--screenshot-dir",
                "shots",
                "--seed",
                "1948",
                "--screenshot-subtitle",
                "none",
                "--screenshot-skip-start",
                "00:02:00",
            ]
        )

        self.assertEqual(args.screenshot_count, 3)
        self.assertEqual(args.screenshot_dir, "shots")
        self.assertEqual(args.seed, 1948)
        self.assertEqual(args.screenshot_subtitle, "none")
        self.assertEqual(app.parse_duration(args.scan_duration), 300)
        self.assertEqual(app.parse_duration(args.screenshot_skip_start), 120)

    def test_info_uses_1m_range_chunks_by_default(self):
        args = app.build_parser().parse_args(["info", "source.iso"])

        self.assertEqual(args.range_size, 1 * 1024 * 1024)

    def test_emby_json_format_includes_stream_metadata_and_chapters(self):
        playlist = app.Playlist(
            name="00001.mpls",
            items=(app.PlaylistItem("00001", "M2TS", 0, 4_500_000),),
            size_bytes=987654321,
            unique_size_bytes=987654321,
            chapters=(
                app.PlaylistChapter(index=0, time_seconds=0, mark_type=1),
                app.PlaylistChapter(index=1, time_seconds=300, mark_type=1),
            ),
        )
        media_info = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                    "profile": "Main 10",
                    "level": 153,
                    "pix_fmt": "yuv420p10le",
                    "color_transfer": "smpte2084",
                    "color_primaries": "bt2020",
                    "color_space": "bt2020nc",
                    "bit_rate": "35000000",
                    "disposition": {"default": 1},
                    "side_data_list": [
                        {
                            "side_data_type": "DOVI configuration record",
                            "dv_profile": 8,
                            "bl_signal_compatibility_id": 1,
                        }
                    ],
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "profile": "TrueHD",
                    "channels": 8,
                    "channel_layout": "7.1",
                    "sample_rate": "48000",
                    "bits_per_sample": 24,
                    "bit_rate": "4500000",
                    "tags": {"language": "eng", "title": "English Atmos"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "chi", "title": "简体中文"},
                    "disposition": {"forced": 1},
                },
            ]
        }

        payload = app.format_emby_json(FakeImage(), playlist, media_info, "partial", 100)

        self.assertEqual(len(payload), 1)
        source = payload[0]["MediaSourceInfo"]
        self.assertEqual(source["Container"], "bluray")
        self.assertEqual(source["RunTimeTicks"], 1_000_000_000)
        self.assertEqual(source["Bitrate"], 79_012_346)
        video, audio, subtitle = source["MediaStreams"]
        self.assertEqual(video["ExtendedVideoType"], "DolbyVision")
        self.assertEqual(video["ExtendedVideoSubType"], "DoviProfile81")
        self.assertEqual(video["VideoRange"], "DolbyVision")
        self.assertEqual(video["Protocol"], "File")
        self.assertNotIn("DisplayLanguage", video)
        self.assertEqual(audio["Title"], "English Atmos")
        self.assertTrue(audio["IsDefault"])
        self.assertEqual(subtitle["Codec"], "PGSSUB")
        self.assertTrue(subtitle["IsForced"])
        self.assertEqual(subtitle["DisplayTitle"], "Chinese (PGSSUB)")
        self.assertEqual(subtitle["SubtitleLocationType"], "InternalStream")
        self.assertEqual(payload[0]["Chapters"][1]["StartPositionTicks"], 3_000_000_000)
        self.assertEqual(payload[0]["RemoteBluray"]["ScanDurationSeconds"], 100)

    def test_info_parser_and_printer_support_emby_json(self):
        args = app.build_parser().parse_args(
            ["info", "source.iso", "--format", "emby-json"]
        )
        result = {"emby_json": [{"MediaSourceInfo": {}}], "screenshots": []}
        output = StringIO()

        with redirect_stdout(output):
            app.print_info_result(args, result)

        self.assertEqual(json.loads(output.getvalue()), result["emby_json"])

    def test_mpls_play_marks_are_translated_to_playlist_chapters(self):
        data = bytearray(64)
        mark_pos = 8
        struct.pack_into(">I", data, mark_pos, 30)
        struct.pack_into(">H", data, mark_pos + 4, 2)
        data[mark_pos + 7] = 1
        struct.pack_into(">H", data, mark_pos + 8, 0)
        struct.pack_into(">I", data, mark_pos + 10, 45_000)
        data[mark_pos + 21] = 1
        struct.pack_into(">H", data, mark_pos + 22, 1)
        struct.pack_into(">I", data, mark_pos + 24, 22_500)
        items = [
            app.PlaylistItem("00001", "M2TS", 0, 90_000),
            app.PlaylistItem("00002", "M2TS", 0, 90_000),
        ]

        chapters = app.parse_mpls_chapters(data, mark_pos, items)

        self.assertEqual(
            [(chapter.index, chapter.time_seconds) for chapter in chapters],
            [(0, 1.0), (1, 2.5)],
        )

    def test_source_to_url_unwraps_markdown_link(self):
        url = "https://example.test/movie.iso"

        self.assertEqual(app.source_to_url(f"[movie]({url})"), url)
        escaped_url = "https://example.test/movie\\(1989\\).iso"
        self.assertEqual(
            app.source_to_url(f"[movie]({escaped_url})"),
            "https://example.test/movie(1989).iso",
        )

    def test_private_sources_bypass_environment_proxy_by_default(self):
        self.assertTrue(app._default_no_proxy("http://10.40.161.250:9530/file.iso"))
        self.assertTrue(app._default_no_proxy("http://127.0.0.1:9530/file.iso"))
        self.assertFalse(app._default_no_proxy("https://example.com/file.iso"))

    def test_remote_options_allow_explicit_proxy_override(self):
        direct = app.build_parser().parse_args(
            ["extract-video", "source.iso", "-o", "out.mkv", "--no-proxy"]
        )
        proxied = app.build_parser().parse_args(
            ["extract-video", "source.iso", "-o", "out.mkv", "--use-proxy"]
        )
        self.assertTrue(direct.no_proxy)
        self.assertFalse(proxied.no_proxy)

    def test_nested_release_directory_resolves_virtual_bdmv_paths(self):
        image = app.RemoteUdfImage.__new__(app.RemoteUdfImage)
        image.bdmv_root = "/My Release/BDMV"

        self.assertEqual(image._resolve_bdmv_path("/BDMV"), "/My Release/BDMV")
        self.assertEqual(
            image._resolve_bdmv_path("/BDMV/PLAYLIST/00001.mpls"),
            "/My Release/BDMV/PLAYLIST/00001.mpls",
        )
        self.assertEqual(image._resolve_bdmv_path("/"), "/")

    def test_playlist_candidates_skip_playlists_with_missing_m2ts(self):
        valid = app.Playlist(
            name="00800.mpls",
            items=(app.PlaylistItem("00000", "M2TS", 0, 90_000),),
        )
        missing = app.Playlist(
            name="01628.mpls",
            items=(app.PlaylistItem("00676", "M2TS", 0, 90_000),),
        )

        class CandidateImage:
            verbose = False
            _playlist_cache = None

            def list_dir(self, path):
                if path == "/BDMV/STREAM":
                    return [
                        {"name": "00000.m2ts", "directory": False},
                    ]
                if path == "/BDMV/PLAYLIST":
                    return [
                        {"name": "01628.mpls", "directory": False},
                        {"name": "00800.mpls", "directory": False},
                    ]
                raise AssertionError(path)

            def find(self, path):
                if path.endswith("01628.mpls") or path.endswith("00800.mpls"):
                    return SimpleNamespace(read_all=lambda: b"playlist")
                if path.endswith("00676.m2ts"):
                    raise FileNotFoundError(path)
                if path.endswith("00000.m2ts"):
                    return SimpleNamespace(size=1234, is_complete=True)
                raise AssertionError(path)

        with patch.object(app, "parse_mpls", side_effect=[missing, valid]):
            playlists = app.RemoteUdfImage.playlist_candidates(CandidateImage())

        self.assertEqual([playlist.name for playlist in playlists], ["00800.mpls"])
        self.assertEqual(playlists[0].size_bytes, 1234)

    def test_playlist_pid_filter_removes_unselected_clip_streams(self):
        playlist = app.Playlist(
            "00800.mpls",
            (app.PlaylistItem("00293", "M2TS", 0, 45000),),
            stream_metadata=(
                ("video", "", 36),
                ("audio", "eng", 131),
                ("subtitle", "eng", 144),
                ("subtitle", "zho", 144),
            ),
            stream_pid_metadata=(
                ("video", "", 36, 0x1011),
                ("audio", "eng", 131, 0x1100),
                ("subtitle", "eng", 144, 0x12A0),
                ("subtitle", "zho", 144, 0x12A1),
            ),
        )
        streams = [
            {"index": 0, "id": "0x1011", "codec_type": "video"},
            {"index": 1, "id": "0x1015", "codec_type": "video"},
            {"index": 2, "id": "0x1100", "codec_type": "audio"},
            {"index": 3, "id": "0x12a0", "codec_type": "subtitle"},
            {"index": 4, "id": "0x12a1", "codec_type": "subtitle"},
            {"index": 5, "id": "0x12a2", "codec_type": "subtitle"},
        ]

        result = app.playlist_streams(playlist, streams)

        self.assertEqual([stream["id"] for stream in result], ["0x1011", "0x1100", "0x12a0", "0x12a1"])
        self.assertEqual(result[2]["tags"]["language"], "eng")
        self.assertEqual(result[3]["tags"]["language"], "zho")

    def test_subtitle_language_inference_is_conservative(self):
        self.assertEqual(app.infer_language_from_text("第一行中文字幕\n第二行"), "zho")
        self.assertEqual(app.infer_language_from_text("これは日本語の字幕です"), "jpn")
        self.assertEqual(app.infer_language_from_name("movie.zh-Hans.srt"), "zho")
        self.assertEqual(app.infer_language_from_text("bonjour le monde ceci est une phrase"), "")

    def test_attached_repeated_tail_keeps_declared_timeline(self):
        feature = app.PlaylistItem("00304", "M2TS", 0, 3_767_000)
        loop = app.PlaylistItem("00295", "M2TS", 0, 3_767_000)
        source = (feature,) + (loop,) * 300

        normalized, note = app.normalize_playlist_items(source)
        playlist = app.Playlist(
            "00021.mpls",
            normalized,
            size_bytes=33_850_398_720,
            unique_size_bytes=224_925_696,
            source_items=source,
            normalization_note=note,
        )

        self.assertEqual(playlist.items, source)
        self.assertEqual(playlist.clip_ids[0], "00304")
        self.assertEqual(playlist.clip_ids.count("00295"), 300)
        self.assertAlmostEqual(playlist.duration_seconds, 83.711111 * 301, places=4)
        self.assertEqual(playlist.main_selection_duration, playlist.duration_seconds)
        self.assertIn("retained attached repeated clip 00295.m2ts x300", note or "")

    def test_pure_loop_is_reduced_and_still_marked_as_looping(self):
        item = app.PlaylistItem("00295", "M2TS", 0, 450_000)
        source = (item,) * 300
        normalized, note = app.normalize_playlist_items(source)
        playlist = app.Playlist("00023.mpls", normalized, source_items=source, normalization_note=note)

        self.assertEqual(len(playlist.items), 1)
        self.assertEqual(playlist.looping_period, 1)
        self.assertTrue(playlist.is_looping)

    def test_main_selection_uses_attached_playlist_timeline(self):
        feature = app.PlaylistItem("00304", "M2TS", 0, 3_767_000)
        loop = app.PlaylistItem("00295", "M2TS", 0, 3_767_000)
        source = (feature,) + (loop,) * 300
        normalized, note = app.normalize_playlist_items(source)
        attached = app.Playlist(
            "00021.mpls", normalized, source_items=source, normalization_note=note
        )
        bonus = app.Playlist(
            "00801.mpls", (app.PlaylistItem("00306", "M2TS", 0, 50_000_000),)
        )

        self.assertEqual(app.main_playlist([attached, bonus]).name, "00021.mpls")

    def test_feature_mode_ignores_attached_loop_when_excluding_main(self):
        feature = app.PlaylistItem("00304", "M2TS", 0, 3_767_000)
        loop = app.PlaylistItem("00295", "M2TS", 0, 3_767_000)
        source = (feature,) + (loop,) * 300
        normalized, note = app.normalize_playlist_items(source)
        attached = app.Playlist(
            "00021.mpls", normalized, source_items=source, normalization_note=note
        )
        actual_feature = app.Playlist(
            "00001.mpls", (app.PlaylistItem("00327", "M2TS", 0, 4_500_000),), size_bytes=20_000
        )
        near_duplicate = app.Playlist(
            "00801.mpls", (app.PlaylistItem("00326", "M2TS", 0, 4_500_010),), size_bytes=10_000
        )

        self.assertEqual(
            app.feature_reference_playlist([attached, actual_feature, near_duplicate]).name,
            "00001.mpls",
        )

    def test_physical_extent_beyond_eof_is_detected_without_reading_media(self):
        class Remote:
            size = 1_000

        class Image:
            remote = Remote()

            @staticmethod
            def partition_base(_partition):
                return 0

        file = app.UdfFile(
            Image(),
            "/BDMV/STREAM/00002.m2ts",
            {"length": 2_048, "ads": [{"kind": 0, "partition": 0, "lba": 0, "length": 2_048}]},
            0,
        )
        self.assertFalse(file.is_complete)

    def test_chooses_first_chinese_subtitle_stream(self):
        streams = [
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "chi"}},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "zho"}},
        ]

        selected = app.choose_chinese_subtitle_stream(self.playlist, streams, "auto")

        self.assertEqual(selected, (1, "Chinese"))

    def test_bdshare_post_contains_tmdb_report_screenshots_and_ed2k(self):
        post = app.build_bdshare_post(
            "◎译　　名　德意志零年",
            "https://image.tmdb.org/t/p/original/poster.jpg",
            "DISC INFO\n\nVIDEO:\n\nCodec\n-----",
            ["https://haildceu1.github.io/picx-images-hosting/shot.jpg"],
            "ed2k://|file|movie.iso|123|HASH|/",
        )

        self.assertTrue(post.startswith("[free][img]https://image.tmdb.org/t/p/original/poster.jpg[/img]"))
        self.assertIn("◎译　　名　德意志零年", post)
        self.assertIn("[code]\nDISC INFO\n\nVIDEO:", post)
        self.assertIn("[img]https://haildceu1.github.io/picx-images-hosting/shot.jpg[/img]", post)
        self.assertIn("[hide][code]\ned2k://|file|movie.iso|123|HASH|/\n[/code]", post)
        self.assertTrue(post.endswith("[/hide]"))

    def test_ed2k_hash_uses_decoded_remote_filename_and_size(self):
        image = SimpleNamespace(
            url="https://example.test/d/abc/%E7%94%B5%E5%BD%B1%202024.iso?download=1",
            remote=SimpleNamespace(size=44942753792),
        )
        args = SimpleNamespace(ed2k_link=None, ed2k_hash="ABCDEF")

        self.assertEqual(
            app.build_ed2k_link(image, args),
            "ed2k://|file|电影 2024.iso|44942753792|ABCDEF|/",
        )

        query_image = SimpleNamespace(
            url="https://example.test/d/opaque.iso?/%E7%8B%99%E5%87%BB%E7%94%B5%E8%AF%9D%E4%BA%AD%20%282003%29.iso",
            remote=SimpleNamespace(size=123),
        )
        self.assertEqual(
            app.build_ed2k_link(query_image, args),
            "ed2k://|file|狙击电话亭 (2003).iso|123|ABCDEF|/",
        )

    def test_md4_and_small_ed2k_hash_match_known_vectors(self):
        self.assertEqual(
            app.md4_digest(b"").hex(),
            "31d6cfe0d16ae931b73c59d7e0c089c0",
        )
        self.assertEqual(
            app.md4_digest(b"a").hex(),
            "bde52cb31de33e46245e05fbdbd6fb24",
        )

    def test_calculate_ed2k_hash_streams_remote_bytes(self):
        payload = b"abc"
        remote = SimpleNamespace(
            size=len(payload),
            read_range=lambda offset, size: payload[offset : offset + size],
        )

        self.assertEqual(
            app.calculate_ed2k_hash(SimpleNamespace(remote=remote)),
            app.md4_digest(payload).hex(),
        )
        self.assertEqual(
            app.ed2k_hash_from_parts([app.md4_digest(b"a")], 1),
            "bde52cb31de33e46245e05fbdbd6fb24",
        )

    def test_calculate_ed2k_hash_uses_parallel_complete_file_ranges(self):
        payload = b"abcdefghij"
        remote = SimpleNamespace(
            size=len(payload),
            fetch_range=lambda offset, size: payload[offset : offset + size],
        )
        with patch.object(app, "ED2K_PART_SIZE", 3):
            expected = app.ed2k_hash_from_parts(
                [app.md4_digest(payload[index : index + 3]) for index in (0, 3, 6, 9)],
                len(payload),
            )
            actual = app.calculate_ed2k_hash(SimpleNamespace(remote=remote), workers=3)
        self.assertEqual(actual, expected)

    def test_bdshare_parser_has_defaults_for_integrated_workflow(self):
        args = app.build_parser().parse_args(["bdshare", "source.iso", "--tmdb-id", "8016"])

        self.assertEqual(args.scan, "partial")
        self.assertEqual(args.scan_duration, "300")
        self.assertEqual(args.screenshot_count, 3)
        self.assertEqual(args.ed2k_workers, 2)
        self.assertEqual(args.tmdb_type, "movie")
        self.assertEqual(args.screenshot_subtitle, "auto")

        auto_args = app.build_parser().parse_args(
            ["bdshare", "source.iso", "--tmdb-id", "8016", "--ed2k-auto"]
        )
        self.assertTrue(auto_args.ed2k_auto)


if __name__ == "__main__":
    import unittest

    unittest.main()
