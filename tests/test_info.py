import json
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

    def test_partial_probe_limits_interval_to_first_100_seconds(self):
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
        self.assertIn("-count_packets", command)
        self.assertIn("-read_intervals", command)
        self.assertIn("%+100", command)

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

        self.assertIn("Protection:     AACS", report)
        self.assertIn("Extras:         BD-Java", report)
        self.assertIn("First 100 seconds only", report)
        self.assertIn("MPEG-4 AVC Video", report)
        self.assertIn("32682 kbps", report)
        self.assertIn("English", report)
        self.assertIn("28.636 kbps", report)
        self.assertIn("FILES:", report)
        self.assertIn("00001.M2TS", report)


if __name__ == "__main__":
    import unittest

    unittest.main()
