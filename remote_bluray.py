"""Read a remote Blu-ray ISO through HTTP Range requests.

The tool parses the UDF metadata partition in Python and exposes selected UDF
files through a local HTTP server.  ffprobe/ffmpeg can then consume a virtual
BDMV/STREAM/*.m2ts URL without downloading the whole ISO first.
"""

from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import quote, unquote, urlsplit

import requests


__version__ = "0.10.0"
BLOCK_SIZE = 2048
ED2K_PART_SIZE = 9500 * 1024
DEFAULT_RANGE_SIZE = 8 * 1024 * 1024
DEFAULT_WORKERS = 2
DEFAULT_PREFETCH = 2
MAX_RETRIES = 4


def md4_digest(data: bytes) -> bytes:
    """Return an MD4 digest without requiring an optional crypto package."""
    message = bytearray(data)
    bit_length = len(message) * 8
    message.append(0x80)
    message.extend(b"\x00" * ((56 - len(message) % 64) % 64))
    message.extend(struct.pack("<Q", bit_length))

    def rotate(value: int, amount: int) -> int:
        return ((value << amount) | (value >> (32 - amount))) & 0xFFFFFFFF

    def round_one(x: int, y: int, z: int) -> int:
        return (x & y) | (~x & z)

    def round_two(x: int, y: int, z: int) -> int:
        return (x & y) | (x & z) | (y & z)

    def round_three(x: int, y: int, z: int) -> int:
        return x ^ y ^ z

    a0, b0, c0, d0 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
    for offset in range(0, len(message), 64):
        words = struct.unpack_from("<16I", message, offset)
        a, b, c, d = a0, b0, c0, d0
        for function, indexes, shifts, constant in (
            (round_one, range(16), (3, 7, 11, 19), 0),
            (round_two, (0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15), (3, 5, 9, 13), 0x5A827999),
            (round_three, (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15), (3, 9, 11, 15), 0x6ED9EBA1),
        ):
            for index, word_index in enumerate(indexes):
                value = (a + function(b, c, d) + words[word_index] + constant) & 0xFFFFFFFF
                a = rotate(value, shifts[index % 4])
                a, b, c, d = d, a, b, c
        a0 = (a0 + a) & 0xFFFFFFFF
        b0 = (b0 + b) & 0xFFFFFFFF
        c0 = (c0 + c) & 0xFFFFFFFF
        d0 = (d0 + d) & 0xFFFFFFFF
    return struct.pack("<4I", a0, b0, c0, d0)


def ed2k_hash_from_parts(part_hashes: list[bytes], total_size: int) -> str:
    if total_size <= ED2K_PART_SIZE:
        digest = part_hashes[0] if part_hashes else md4_digest(b"")
    else:
        digest = md4_digest(b"".join(part_hashes))
    return digest.hex()


def calculate_ed2k_hash(image: RemoteUdfImage, progress_file=None) -> str:
    """Calculate the ED2K hash by streaming the complete remote file."""
    part_hashes: list[bytes] = []
    offset = 0
    total_size = image.remote.size
    while offset < total_size:
        part_size = min(ED2K_PART_SIZE, total_size - offset)
        data = image.remote.read_range(offset, part_size)
        if len(data) != part_size:
            raise IOError(
                f"Short read while calculating ED2K hash at {offset}: "
                f"{len(data)} bytes, expected {part_size}"
            )
        part_hashes.append(md4_digest(data))
        offset += part_size
        if progress_file is not None:
            print(
                f"ED2K: hashed {offset:,}/{total_size:,} bytes "
                f"({offset / total_size:.1%})",
                file=progress_file,
                flush=True,
            )
    return ed2k_hash_from_parts(part_hashes, total_size)


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def descriptor_tag(block: bytes) -> int:
    if len(block) < 16:
        return -1
    checksum = sum(block[index] for index in range(16) if index != 4) & 0xFF
    return u16(block, 0) if checksum == block[4] else -1


def decode_cs0(data: bytes) -> str:
    """Decode the OSTA CS0 compressed Unicode used by UDF names."""
    data = bytes(data).rstrip(b"\x00")
    if not data:
        return ""
    compression = data[0]
    payload = data[1:]
    if compression == 8:
        decoded = payload.decode("latin-1", "replace")
        return decoded.split("\x00", 1)[0].rstrip()
    if compression == 16:
        payload = payload[: len(payload) // 2 * 2]
        decoded = payload.decode("utf-16-be", "replace")
        return decoded.split("\x00", 1)[0].rstrip()
    decoded = payload.decode("latin-1", "replace")
    return decoded.split("\x00", 1)[0].rstrip()


def decode_extent_ad(data: bytes, offset: int) -> dict:
    value = u32(data, offset)
    return {
        "kind": value >> 30,
        "length": value & 0x3FFFFFFF,
        "lba": u32(data, offset + 4),
    }


def decode_long_ad(data: bytes, offset: int, implicit_partition: int | None = None) -> dict:
    value = u32(data, offset)
    return {
        "kind": value >> 30,
        "length": value & 0x3FFFFFFF,
        "lba": u32(data, offset + 4),
        "partition": u16(data, offset + 8) if implicit_partition is None else implicit_partition,
    }


def decode_file_entry(data: bytes, partition: int) -> dict:
    """Decode the UDF File Entry or Extended File Entry allocation descriptors."""
    tag = descriptor_tag(data)
    if tag not in (261, 266):
        raise RuntimeError(f"Expected UDF File Entry, got descriptor tag {tag}")
    if tag == 261:
        ea_offset, ad_base = 168, 176
    else:
        ea_offset, ad_base = 208, 216
    l_ea = u32(data, ea_offset)
    l_ad = u32(data, ea_offset + 4)
    # Allocation descriptors follow the Extended Attributes area.  Extended
    # File Entries commonly carry 24 bytes of attributes on Blu-ray/UDF
    # images, so using the fixed base offset would read attribute bytes as an
    # allocation descriptor location.
    ad_offset = ad_base + l_ea
    flags = u16(data, 34)
    ad_type = flags & 7
    entry = {
        "tag": tag,
        "file_type": data[27],
        "flags": flags,
        "length": u64(data, 56),
        "l_ea": l_ea,
        "l_ad": l_ad,
        "ad_type": ad_type,
        "inline": ad_type == 3,
        "ads": [],
    }
    if entry["inline"]:
        entry["inline_data"] = data[ad_offset : ad_offset + l_ad]
        return entry
    if ad_type == 0:
        ad_size = 8
    elif ad_type == 1:
        ad_size = 16
    elif ad_type == 2:
        ad_size = 20
    else:
        raise RuntimeError(f"Unsupported UDF ICB allocation descriptor type {ad_type}")
    for offset in range(ad_offset, ad_offset + l_ad, ad_size):
        if ad_type == 0:
            value = u32(data, offset)
            entry["ads"].append({
                "kind": value >> 30,
                "length": value & 0x3FFFFFFF,
                "lba": u32(data, offset + 4),
                "partition": partition,
            })
        elif ad_type == 1:
            entry["ads"].append(decode_long_ad(data, offset))
        else:
            value = u32(data, offset)
            entry["ads"].append({
                "kind": value >> 30,
                "length": value & 0x3FFFFFFF,
                "lba": u32(data, offset + 12),
                "partition": u16(data, offset + 16),
            })
    return entry


def decode_directory_entries(data: bytes) -> list[dict]:
    entries = []
    cursor = 0
    while cursor + 38 <= len(data):
        if descriptor_tag(data[cursor : cursor + BLOCK_SIZE]) != 257:
            break
        name_length = data[cursor + 19]
        implementation_length = u16(data, cursor + 36)
        used = 4 * ((38 + implementation_length + name_length + 3) // 4)
        if used < 40 or cursor + used > len(data):
            break
        name_start = cursor + 38 + implementation_length
        name = decode_cs0(data[name_start : name_start + name_length])
        characteristic = data[cursor + 18]
        if name and not (characteristic & 0x08):
            entries.append({
                "name": name,
                "directory": bool(characteristic & 0x02),
                "characteristic": characteristic,
                "icb": decode_long_ad(data, cursor + 20),
            })
        cursor += used
    return entries


def be16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


@dataclass(frozen=True)
class PlaylistItem:
    """One primary clip segment referenced by a Blu-ray MPLS file."""

    clip_id: str
    codec_id: str
    in_time: int
    out_time: int

    @property
    def duration_seconds(self) -> float:
        return max(0, self.out_time - self.in_time) / 45000.0


@dataclass(frozen=True)
class Playlist:
    name: str
    items: tuple[PlaylistItem, ...]
    size_bytes: int = 0
    unique_size_bytes: int = 0
    audio_stream_count: int = -1
    stream_metadata: tuple[tuple[str, str, int], ...] = ()

    @property
    def duration_seconds(self) -> float:
        return sum(item.duration_seconds for item in self.items)

    @property
    def clip_ids(self) -> tuple[str, ...]:
        return tuple(item.clip_id for item in self.items)

    @property
    def total_bitrate_mbps(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return self.size_bytes * 8 / self.duration_seconds / 1_000_000

    @property
    def looping_period(self) -> int | None:
        """Return the repeated play-item cycle length, if this is a loop."""
        item_count = len(self.items)
        if item_count < 3:
            return None

        # A menu playlist can repeat one item or a short sequence of items
        # many times.  Require at least three cycles and near-perfect
        # periodicity so ordinary multi-segment feature playlists are not
        # discarded merely because they reuse a clip.
        for period in range(1, item_count // 3 + 1):
            cycle_count = item_count / period
            if cycle_count < 3:
                continue
            matches = sum(
                self.items[index] == self.items[index % period]
                for index in range(item_count)
            )
            if matches / item_count < 0.95:
                continue
            cycle_duration = sum(item.duration_seconds for item in self.items[:period])
            if cycle_duration > 0 and self.duration_seconds / cycle_duration >= 3:
                return period
        return None

    @property
    def is_looping(self) -> bool:
        return self.looping_period is not None


def parse_mpls_stream(
    data: bytes,
    cursor: int,
    end: int,
    stream_type: str,
) -> tuple[tuple[str, str, int] | None, int]:
    """Read one MPLS STN stream entry and return its language metadata."""
    if cursor >= end:
        return None, end
    descriptor_length = data[cursor]
    descriptor_end = cursor + 1 + descriptor_length
    if descriptor_length < 1 or descriptor_end > end:
        return None, end

    # The first descriptor contains stream type/PID information.  The second
    # descriptor contains the coding type and, for audio/subtitle streams, the
    # three-letter ISO 639 language code.
    if descriptor_end >= end:
        return None, end
    coding_length = data[descriptor_end]
    coding_start = descriptor_end + 1
    coding_end = coding_start + coding_length
    if coding_length < 1 or coding_end > end:
        return None, end

    coding_type = data[coding_start]
    language = ""
    if coding_type in {
        0x03,
        0x04,
        0x80,
        0x81,
        0x82,
        0x83,
        0x84,
        0x85,
        0x86,
        0xA1,
        0xA2,
    }:
        language_start = coding_start + 2
    elif coding_type in {0x90, 0x91}:
        language_start = coding_start + 1
    elif coding_type == 0x92:
        language_start = coding_start + 2
    else:
        language_start = -1

    if language_start >= 0 and language_start + 3 <= coding_end:
        language = data[language_start : language_start + 3].decode(
            "ascii", "replace"
        ).strip("\x00 ")

    return (stream_type, language, coding_type), coding_end


def parse_stn_stream_metadata(
    data: bytes,
    stn_start: int,
    item_end: int,
) -> list[tuple[str, str, int]]:
    """Parse primary video/audio/PG language entries from an MPLS STN block."""
    if stn_start + 16 > item_end:
        return []
    stn_length = be16(data, stn_start)
    stn_end = min(item_end, stn_start + 2 + stn_length)
    if stn_end < stn_start + 16:
        return []

    num_video = data[stn_start + 4]
    num_audio = data[stn_start + 5]
    num_pg = data[stn_start + 6]
    num_pip_pg = data[stn_start + 10]
    cursor = stn_start + 16
    metadata: list[tuple[str, str, int]] = []

    for stream_type, count in (
        ("video", num_video),
        ("audio", num_audio),
        ("subtitle", num_pg + num_pip_pg),
    ):
        for _ in range(count):
            stream, cursor = parse_mpls_stream(data, cursor, stn_end, stream_type)
            if stream is None:
                return metadata
            metadata.append(stream)

    return metadata


def parse_mpls(data: bytes, name: str = "") -> Playlist:
    """Parse the primary play items from a Blu-ray MPLS playlist.

    The fields used here follow libbluray's mpls_parse implementation.  The
    parser intentionally focuses on the primary timeline.  It also keeps the
    primary stream language metadata from the MPLS STN block so ``list`` can
    describe tracks without downloading or fully decoding every M2TS.
    """
    if len(data) < 20 or data[:4] != b"MPLS":
        raise RuntimeError(f"Invalid MPLS header: {name or '<unnamed>'}")

    list_pos = be32(data, 8)
    if list_pos + 10 > len(data):
        raise RuntimeError(f"MPLS playlist section is outside the file: {name}")

    playlist_length = be32(data, list_pos)
    playlist_end = list_pos + 4 + playlist_length
    if playlist_end > len(data):
        raise RuntimeError(f"MPLS playlist section is truncated: {name}")

    item_count = be16(data, list_pos + 6)
    cursor = list_pos + 10
    items: list[PlaylistItem] = []
    audio_counts: list[int] = []
    stream_metadata: list[tuple[str, str, int]] = []
    stream_metadata_counts: Counter[tuple[str, str, int]] = Counter()
    for index in range(item_count):
        if cursor + 2 > playlist_end:
            raise RuntimeError(f"MPLS play item {index} is truncated: {name}")
        item_length = be16(data, cursor)
        item_start = cursor + 2
        item_end = item_start + item_length
        if item_length < 20 or item_end > playlist_end or item_end > len(data):
            raise RuntimeError(f"Invalid MPLS play item {index}: {name}")

        clip_id = data[item_start : item_start + 5].decode("ascii", "replace")
        codec_id = data[item_start + 5 : item_start + 9].decode("ascii", "replace")
        in_time = be32(data, item_start + 12)
        out_time = be32(data, item_start + 16)
        if out_time < in_time:
            raise RuntimeError(f"MPLS play item {index} has a negative duration: {name}")
        items.append(PlaylistItem(clip_id, codec_id, in_time, out_time))

        # The STN block starts after the fixed play-item fields and any
        # additional angle clip descriptors.  Its fifth byte is the primary
        # audio stream count.  It lets audio batch extraction skip video-only
        # feature clips without probing each remote M2TS first.
        stn_start = item_start + 32
        packed_flags = be16(data, item_start + 9)
        is_multi_angle = bool(packed_flags & 0x0010)
        if is_multi_angle:
            angle_count = max(1, data[item_start + 32])
            stn_start = item_start + 34 + (angle_count - 1) * 10
        if stn_start + 6 <= item_end:
            audio_counts.append(data[stn_start + 5])
            item_metadata = parse_stn_stream_metadata(data, stn_start, item_end)
            item_counts = Counter(item_metadata)
            for metadata, count in item_counts.items():
                additional = count - stream_metadata_counts[metadata]
                if additional > 0:
                    stream_metadata.extend([metadata] * additional)
                    stream_metadata_counts[metadata] = count
        cursor = item_end

    audio_stream_count = max(audio_counts) if audio_counts else -1
    return Playlist(
        name=name,
        items=tuple(items),
        audio_stream_count=audio_stream_count,
        stream_metadata=tuple(stream_metadata),
    )


def source_to_url(source: str | Path) -> str:
    """Read a .strm file or accept an HTTP(S) ISO URL directly."""
    value = str(source).strip()
    if value.lower().startswith(("http://", "https://")):
        return value

    source_path = Path(value)
    if not source_path.is_file():
        raise FileNotFoundError(
            f"Source is neither an HTTP(S) URL nor an existing .strm file: {value}"
        )
    url = source_path.read_text(encoding="utf-8-sig").strip()
    if not url:
        raise RuntimeError(f"Empty STRM file: {source_path}")
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError(f"STRM does not contain an HTTP(S) URL: {source_path}")
    return url


class RemoteRangeReader:
    def __init__(
        self,
        url: str,
        verbose: bool = False,
        workers: int = DEFAULT_WORKERS,
        prefetch: int = DEFAULT_PREFETCH,
        range_size: int = DEFAULT_RANGE_SIZE,
    ):
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if prefetch < 0:
            raise ValueError("prefetch must be non-negative")
        if range_size < BLOCK_SIZE or range_size % BLOCK_SIZE:
            raise ValueError(f"range_size must be a multiple of {BLOCK_SIZE} bytes")

        self.verbose = verbose
        self.workers = workers
        self.prefetch = prefetch
        self.range_size = range_size
        self.lock = threading.RLock()
        self._thread_local = threading.local()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._inflight = {}
        self._executor = None

        session = requests.Session()
        response = session.get(
            url,
            headers={"Range": "bytes=0-0"},
            allow_redirects=True,
            timeout=30,
        )
        try:
            response.raise_for_status()
            if response.status_code != 206:
                raise RuntimeError(f"Remote server does not support Range: HTTP {response.status_code}")
            content_range = response.headers.get("Content-Range", "")
            if "/" not in content_range:
                raise RuntimeError("Remote response has no Content-Range header")
            self.size = int(content_range.rsplit("/", 1)[1])
            self.final_url = response.url
        finally:
            response.close()
        self._base_cookies = session.cookies.copy()
        session.close()

        # Keep the default memory footprint bounded even when range-size is
        # increased.  The current chunk plus the prefetch window remain
        # available whenever the configured size fits within this cap.
        cache_memory_cap = 256 * 1024 * 1024
        memory_limit = max(1, cache_memory_cap // self.range_size)
        self.cache_limit = max(1, min(workers + prefetch + 2, memory_limit))
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="remote-bluray-range",
        )
        if verbose:
            print(f"ISO size: {self.size:,} bytes")
            print(f"Final URL: {self.final_url[:120]}...")
            print(
                f"Range reader: workers={self.workers}, "
                f"prefetch={self.prefetch}, range-size={self.range_size:,} bytes"
            )

    def _session_for_thread(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.cookies.update(self._base_cookies)
            self._thread_local.session = session
        return session

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            return min(8.0, 0.5 * (2**attempt))

    def _fetch(self, start: int, end: int) -> bytes:
        expected = end - start + 1
        session = self._session_for_thread()
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            response = None
            try:
                response = session.get(
                    self.final_url,
                    headers={"Range": f"bytes={start}-{end}"},
                    timeout=60,
                )
                if response.status_code == 206:
                    data = response.content
                    if len(data) != expected:
                        raise IOError(
                            f"Range returned {len(data):,} bytes; expected {expected:,}"
                        )
                    if self.verbose:
                        print(f"Remote Range: {start}-{end} -> {len(data):,} bytes")
                    return data

                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable:
                    raise RuntimeError(f"Range request failed: HTTP {response.status_code}")
                last_error = IOError(f"Range request failed: HTTP {response.status_code}")
                if attempt >= MAX_RETRIES:
                    raise last_error
                delay = self._retry_delay(response, attempt)
                if self.verbose:
                    print(
                        f"Range {start}-{end} got HTTP {response.status_code}; "
                        f"retrying in {delay:.1f}s"
                    )
                time.sleep(delay)
            except (requests.RequestException, IOError) as error:
                last_error = error
                if attempt >= MAX_RETRIES:
                    raise IOError(f"Range request failed for {start}-{end}: {error}") from error
                delay = min(8.0, 0.5 * (2**attempt))
                if self.verbose:
                    print(f"Range {start}-{end} failed; retrying in {delay:.1f}s: {error}")
                time.sleep(delay)
            finally:
                if response is not None:
                    response.close()
        raise IOError(f"Range request failed for {start}-{end}: {last_error}")

    def _fetch_chunk(self, index: int) -> bytes:
        start = index * self.range_size
        if start >= self.size:
            return b""
        end = min(start + self.range_size, self.size) - 1
        return self._fetch(start, end)

    def _complete_chunk(self, index: int, future) -> None:
        try:
            data = future.result()
        except Exception:
            with self.lock:
                if self._inflight.get(index) is future:
                    self._inflight.pop(index, None)
            return
        with self.lock:
            if self._inflight.get(index) is future:
                self._inflight.pop(index, None)
            if data:
                self._cache[index] = data
                self._cache.move_to_end(index)
                while len(self._cache) > self.cache_limit:
                    self._cache.popitem(last=False)

    def _ensure_chunk(self, index: int) -> None:
        if index * self.range_size >= self.size:
            return
        with self.lock:
            if index in self._cache or index in self._inflight:
                return
            future = self._executor.submit(self._fetch_chunk, index)
            self._inflight[index] = future
            future.add_done_callback(lambda completed: self._complete_chunk(index, completed))

    def _get_chunk(self, index: int) -> bytes:
        with self.lock:
            data = self._cache.get(index)
            if data is not None:
                self._cache.move_to_end(index)
                return data
            future = self._inflight.get(index)
            if future is None:
                future = self._executor.submit(self._fetch_chunk, index)
                self._inflight[index] = future
                future.add_done_callback(lambda completed: self._complete_chunk(index, completed))
        return future.result()

    def _schedule_prefetch(self, index: int) -> None:
        for next_index in range(index + 1, index + 1 + self.prefetch):
            self._ensure_chunk(next_index)

    def read_range(self, start: int, size: int) -> bytes:
        if size <= 0 or start >= self.size:
            return b""
        start = max(0, start)
        end = min(start + size, self.size)
        output = bytearray()
        while start < end:
            index = start // self.range_size
            data = self._get_chunk(index)
            if not data:
                break
            chunk_start = index * self.range_size
            offset = start - chunk_start
            count = min(end - start, len(data) - offset)
            if count <= 0:
                raise IOError(f"Invalid cached Range chunk at index {index}")
            output.extend(data[offset : offset + count])
            start += count
            self._schedule_prefetch(index)
        return bytes(output)

    def read_block(self, lba: int) -> bytes:
        data = self.read_range(lba * BLOCK_SIZE, BLOCK_SIZE)
        if len(data) != BLOCK_SIZE:
            raise IOError(f"Short read at LBA {lba}: {len(data)} bytes")
        return data


@dataclass
class UdfFile:
    image: "RemoteUdfImage"
    path: str
    entry: dict
    partition: int

    @property
    def size(self) -> int:
        return self.entry["length"]

    def _locate(self, offset: int) -> tuple[dict, int]:
        if offset < 0 or offset >= self.size:
            raise ValueError(f"Offset outside {self.path}: {offset}")
        remaining = offset
        for ad in self.entry["ads"]:
            if remaining < ad["length"]:
                return ad, remaining
            remaining -= ad["length"]
        raise IOError(f"No allocation descriptor covers offset {offset} in {self.path}")

    def read_at(self, offset: int, size: int) -> bytes:
        if offset >= self.size or size <= 0:
            return b""
        size = min(size, self.size - offset)
        output = bytearray()
        while size:
            ad, within = self._locate(offset)
            available = min(size, ad["length"] - within)
            if ad["kind"] == 0:
                absolute = self.image.partition_base(ad["partition"]) + ad["lba"] * BLOCK_SIZE + within
                output.extend(self.image.remote.read_range(absolute, available))
            elif ad["kind"] in (1, 2):
                output.extend(b"\x00" * available)
            else:
                raise IOError(f"Unsupported allocation extent type {ad['kind']} in {self.path}")
            offset += available
            size -= available
        return bytes(output)

    def read_all(self) -> bytes:
        output = bytearray()
        offset = 0
        while offset < self.size:
            chunk = self.read_at(
                offset,
                min(self.image.remote.range_size, self.size - offset),
            )
            if not chunk:
                break
            output.extend(chunk)
            offset += len(chunk)
        return bytes(output)


class RemoteUdfImage:
    def __init__(
        self,
        source: str | Path,
        verbose: bool = False,
        workers: int = DEFAULT_WORKERS,
        prefetch: int = DEFAULT_PREFETCH,
        range_size: int = DEFAULT_RANGE_SIZE,
    ):
        self.source = str(source)
        self.url = source_to_url(source)
        self.verbose = verbose
        self.remote = RemoteRangeReader(
            self.url,
            verbose=verbose,
            workers=workers,
            prefetch=prefetch,
            range_size=range_size,
        )
        self.partition_starts: dict[int, int] = {}
        self.partition_lengths: dict[int, int] = {}
        self.metadata_partition = None
        self.root_icb = None
        self._parse_volume()

    def partition_base(self, partition: int) -> int:
        if partition not in self.partition_starts:
            raise RuntimeError(f"Unknown UDF partition reference: {partition}")
        return self.partition_starts[partition] * BLOCK_SIZE

    def _parse_volume(self):
        total_blocks = self.remote.size // BLOCK_SIZE
        anchor = None
        for lba in (256, total_blocks - 1, total_blocks - 257):
            block = self.remote.read_block(lba)
            if descriptor_tag(block) == 2:
                anchor = block
                break
        if anchor is None:
            raise RuntimeError("No UDF Anchor Volume Descriptor Pointer found")

        main_vds = decode_extent_ad(anchor, 16)
        descriptors = {}
        for lba in range(main_vds["lba"], main_vds["lba"] + (main_vds["length"] + BLOCK_SIZE - 1) // BLOCK_SIZE):
            block = self.remote.read_block(lba)
            tag = descriptor_tag(block)
            descriptors.setdefault(tag, block)
            if tag == 8:
                break
        if 5 not in descriptors or 6 not in descriptors:
            raise RuntimeError("UDF volume descriptor sequence is incomplete")

        partition_descriptor = descriptors[5]
        logical_volume = descriptors[6]
        physical_partition = u16(partition_descriptor, 22)
        physical_start = u32(partition_descriptor, 188)
        physical_length = u32(partition_descriptor, 192)
        self.partition_starts[physical_partition] = physical_start
        self.partition_lengths[physical_partition] = physical_length
        self.volume_id = decode_cs0(logical_volume[84:212])

        map_length = u32(logical_volume, 264)
        map_count = u32(logical_volume, 268)
        maps = logical_volume[440 : 440 + map_length]
        cursor = 0
        metadata_map = None
        for _ in range(map_count):
            if cursor + 2 > len(maps):
                break
            map_type = maps[cursor]
            item_length = maps[cursor + 1]
            if item_length < 2 or cursor + item_length > len(maps):
                raise RuntimeError("Invalid UDF partition map")
            if map_type == 1:
                logical_partition = cursor // 1
                self.partition_starts[logical_partition] = physical_start
                self.partition_lengths[logical_partition] = physical_length
            elif map_type == 2 and item_length == 64:
                metadata_map = {
                    "partition": u16(maps, cursor + 38),
                    "lba": u32(maps, cursor + 40),
                    "mirror_lba": u32(maps, cursor + 44),
                    "index": len(self.partition_starts),
                }
            cursor += item_length

        # The logical partition number is the partition-map index.  Blu-ray
        # metadata maps are normally map 0=physical and map 1=metadata.
        self.partition_starts[0] = physical_start
        self.partition_lengths[0] = physical_length
        if metadata_map:
            metadata_entry_block = self.remote.read_block(physical_start + metadata_map["lba"])
            metadata_entry = decode_file_entry(metadata_entry_block, physical_partition)
            if not metadata_entry["ads"]:
                raise RuntimeError("UDF metadata file has no allocation descriptor")
            metadata_base = physical_start + metadata_entry["ads"][0]["lba"]
            metadata_partition = 1
            self.partition_starts[metadata_partition] = metadata_base
            self.partition_lengths[metadata_partition] = metadata_entry["length"] // BLOCK_SIZE
            self.metadata_partition = metadata_partition

        fsd_location = decode_long_ad(logical_volume, 248)
        fsd = self._read_descriptor(fsd_location)
        if descriptor_tag(fsd) != 256:
            raise RuntimeError(f"UDF File Set Descriptor not found, got tag {descriptor_tag(fsd)}")
        self.root_icb = decode_long_ad(fsd, 400)
        if self.verbose:
            print(f"UDF volume: {self.volume_id}")
            print(f"UDF partitions: {self.partition_starts}")
            print(f"Root ICB: {self.root_icb}")

    def _read_descriptor(self, extent: dict) -> bytes:
        absolute = self.partition_base(extent["partition"]) + extent["lba"] * BLOCK_SIZE
        return self.remote.read_range(absolute, BLOCK_SIZE)

    def _entry_from_icb(self, icb: dict) -> dict:
        block = self.remote.read_block(self.partition_starts[icb["partition"]] + icb["lba"])
        return decode_file_entry(block, icb["partition"])

    def _directory_from_entry(self, entry: dict, partition: int) -> list[dict]:
        udf_file = UdfFile(self, "<directory>", entry, partition)
        return decode_directory_entries(udf_file.read_all())

    def _find_in_directory(self, entries: Iterable[dict], name: str) -> dict:
        folded = name.casefold()
        for entry in entries:
            if entry["name"].casefold() == folded:
                return entry
        raise FileNotFoundError(name)

    def find(self, path: str) -> UdfFile:
        components = [part for part in path.replace("\\", "/").split("/") if part]
        current_entry = {"directory": True, "icb": self.root_icb}
        current_partition = self.root_icb["partition"]
        current_path = ""
        for component in components:
            directory_entry = self._entry_from_icb(current_entry["icb"])
            if directory_entry["file_type"] != 4:
                raise NotADirectoryError(current_path or "/")
            entries = self._directory_from_entry(directory_entry, current_partition)
            current_entry = self._find_in_directory(entries, component)
            current_partition = current_entry["icb"]["partition"]
            current_path = f"{current_path}/{current_entry['name']}"
        entry = self._entry_from_icb(current_entry["icb"])
        return UdfFile(self, path or "/", entry, current_partition)

    def list_dir(self, path: str) -> list[dict]:
        directory = self.find(path)
        if directory.entry["file_type"] != 4:
            raise NotADirectoryError(path)
        return self._directory_from_entry(directory.entry, directory.partition)

    def stream_candidates(self) -> list[tuple[str, UdfFile]]:
        candidates = []
        for item in self.list_dir("/BDMV/STREAM"):
            if item["directory"] or not item["name"].lower().endswith((".m2ts", ".mts")):
                continue
            path = f"/BDMV/STREAM/{item['name']}"
            candidates.append((item["name"], self.find(path)))
        return sorted(candidates, key=lambda value: value[1].size, reverse=True)

    def playlist_candidates(self) -> list[Playlist]:
        playlists = []
        for item in self.list_dir("/BDMV/PLAYLIST"):
            name = item["name"]
            if item["directory"] or not name.lower().endswith(".mpls"):
                continue
            path = f"/BDMV/PLAYLIST/{name}"
            playlist = parse_mpls(self.find(path).read_all(), name=name)
            clip_sizes = {
                item.clip_id: self.find(f"/BDMV/STREAM/{item.clip_id}.m2ts").size
                for item in playlist.items
            }
            size_bytes = sum(clip_sizes.get(item.clip_id, 0) for item in playlist.items)
            playlists.append(
                Playlist(
                    name=playlist.name,
                    items=playlist.items,
                    size_bytes=size_bytes,
                    unique_size_bytes=sum(clip_sizes.values()),
                    audio_stream_count=playlist.audio_stream_count,
                    stream_metadata=playlist.stream_metadata,
                )
            )
        return sorted(playlists, key=lambda playlist: playlist.name.casefold())


class VirtualFileServer:
    def __init__(self, image: RemoteUdfImage, verbose: bool = False):
        self.image = image
        self.verbose = verbose
        self.server = None

    def __enter__(self):
        image = self.image
        verbose = self.verbose

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format_string, *args):
                if verbose:
                    print(f"[local-http] {self.address_string()} {format_string % args}")

            def _file(self):
                path = unquote(urlsplit(self.path).path)
                if not path.startswith("/"):
                    path = "/" + path
                return image.find(path)

            def _send(self, include_body: bool):
                try:
                    remote_file = self._file()
                except (FileNotFoundError, NotADirectoryError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                except Exception as error:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
                    return

                start = 0
                end = remote_file.size - 1
                range_header = self.headers.get("Range")
                if range_header:
                    if not range_header.startswith("bytes=") or "," in range_header:
                        self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        return
                    spec = range_header[6:]
                    start_text, separator, end_text = spec.partition("-")
                    try:
                        if start_text:
                            start = int(start_text)
                            end = int(end_text) if end_text else remote_file.size - 1
                        else:
                            length = int(end_text)
                            start = max(0, remote_file.size - length)
                        if start < 0 or start >= remote_file.size or end < start:
                            raise ValueError
                        end = min(end, remote_file.size - 1)
                    except ValueError:
                        self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        return

                length = end - start + 1
                status = HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK
                self.send_response(status)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if range_header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{remote_file.size}")
                self.send_header("Connection", "close")
                self.end_headers()
                if not include_body:
                    return
                position = start
                while position <= end:
                    chunk_size = min(1024 * 1024, end - position + 1)
                    chunk = remote_file.read_at(position, chunk_size)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                        return
                    position += len(chunk)

            def do_HEAD(self):
                self._send(False)

            def do_GET(self):
                self._send(True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        if self.server is None:
            raise RuntimeError("Virtual server is not running")
        return f"http://127.0.0.1:{self.server.server_port}"

    def file_url(self, path: str) -> str:
        return self.url + "/" + path.lstrip("/")

    def __exit__(self, exc_type, exc, traceback):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)


def get_executable(name: str) -> str:
    environment_name = {
        "ffmpeg": "REMOTE_BLURAY_FFMPEG",
        "ffprobe": "REMOTE_BLURAY_FFPROBE",
    }.get(name)
    if environment_name:
        configured = os.environ.get(environment_name)
        if configured and Path(configured).is_file():
            return configured

    path = shutil.which(name)
    if path:
        return path
    candidates = {
        "ffmpeg": r"C:\ffmpeg\bin\ffmpeg.exe",
        "ffprobe": r"C:\ffmpeg\bin\ffprobe.exe",
    }
    candidate = candidates.get(name)
    if candidate and Path(candidate).exists():
        return candidate
    raise FileNotFoundError(f"Executable not found: {name}")


def choose_stream(image: RemoteUdfImage, requested: str | None) -> tuple[str, UdfFile]:
    candidates = image.stream_candidates()
    if not candidates:
        raise RuntimeError("No M2TS files found in /BDMV/STREAM")
    if requested:
        requested = requested.lower()
        for name, file in candidates:
            if name.lower() == requested:
                return name, file
        raise FileNotFoundError(f"M2TS not found: {requested}")
    return candidates[0]


def normalize_playlist_name(requested: str) -> str:
    value = requested.strip()
    if value.isdigit():
        return f"{int(value):05d}.mpls"
    if not value.lower().endswith(".mpls"):
        value += ".mpls"
    return value


def choose_playlist(image: RemoteUdfImage, requested: str | None) -> Playlist:
    playlists = image.playlist_candidates()
    if not playlists:
        raise RuntimeError("No valid .mpls playlists found in /BDMV/PLAYLIST")
    if requested:
        wanted = normalize_playlist_name(requested).casefold()
        for playlist in playlists:
            if playlist.name.casefold() == wanted:
                return playlist
        raise FileNotFoundError(f"MPLS playlist not found: {requested}")
    return max(playlists, key=lambda playlist: playlist.duration_seconds)


def format_duration(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds_int, milliseconds = divmod(remainder, 1000)
    return f"{hours}:{minutes:02d}:{seconds_int:02d}.{milliseconds:03d}"


def parse_duration(value: str) -> float:
    """Parse seconds or H:M:S(.ms) for the feature-list threshold."""
    text = str(value).strip()
    if not text:
        raise ValueError("Duration cannot be empty")
    if ":" not in text:
        seconds = float(text)
    else:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid duration: {value}")
        try:
            numbers = [float(part) for part in parts]
        except ValueError as error:
            raise ValueError(f"Invalid duration: {value}") from error
        if len(numbers) == 2:
            minutes, seconds_part = numbers
            hours = 0.0
        else:
            hours, minutes, seconds_part = numbers
        if minutes < 0 or seconds_part < 0 or seconds_part >= 60 or minutes >= 60:
            raise ValueError(f"Invalid duration: {value}")
        seconds = hours * 3600 + minutes * 60 + seconds_part
    if seconds < 0:
        raise ValueError(f"Duration must be non-negative: {value}")
    return seconds


def ffmpeg_seconds(timestamp: int) -> str:
    return f"{timestamp / 45000.0:.6f}"


def main_playlist(playlists: list[Playlist]) -> Playlist:
    if not playlists:
        raise RuntimeError("No valid .mpls playlists found in /BDMV/PLAYLIST")
    # Count each referenced M2TS once for main detection.  Some discs contain
    # fake/looping playlists that repeat one short clip hundreds of times;
    # counting every repetition would incorrectly make those playlists the
    # largest title.
    return max(
        playlists,
        key=lambda playlist: (
            playlist.unique_size_bytes,
            playlist.size_bytes,
            playlist.duration_seconds,
        ),
    )


def select_playlists(
    image: RemoteUdfImage,
    requested: list[str] | None,
    mode: str | None,
    min_duration: str | None,
) -> list[Playlist]:
    """Resolve explicit playlist names or the main/feature automatic modes."""
    playlists = image.playlist_candidates()
    if not playlists:
        raise RuntimeError("No valid .mpls playlists found in /BDMV/PLAYLIST")

    if requested:
        by_name = {playlist.name.casefold(): playlist for playlist in playlists}
        selected = []
        seen = set()
        for value in requested:
            name = normalize_playlist_name(value).casefold()
            if name not in by_name:
                raise FileNotFoundError(f"MPLS playlist not found: {value}")
            if name not in seen:
                selected.append(by_name[name])
                seen.add(name)
        return selected

    if mode == "main":
        return [main_playlist(playlists)]

    if mode == "feat":
        if min_duration is None:
            raise ValueError("feat mode requires --min-duration")
        threshold = parse_duration(min_duration)
        main = main_playlist(playlists)
        selected = []
        for playlist in playlists:
            if playlist.name.casefold() == main.name.casefold():
                continue
            if playlist.is_looping:
                print(
                    f"Skip: {playlist.name} appears to be a looping playlist "
                    f"(period={playlist.looping_period}, "
                    f"{len(playlist.items)} play item(s))"
                )
                continue
            if (
                playlist.duration_seconds >= threshold
                and playlist.duration_seconds <= main.duration_seconds
            ):
                selected.append(playlist)
        return sorted(selected, key=lambda playlist: (-playlist.size_bytes, playlist.name.casefold()))

    raise ValueError("Specify --playlist, --mode main, or --mode feat")


def playlist_summary(playlist: Playlist) -> str:
    display_name = playlist.name.rsplit(".", 1)[0] + ".MPLS"
    return (
        f"[{display_name}]\n"
        f"  Length: {format_duration(playlist.duration_seconds)}  |  "
        f"Size: {playlist.size_bytes:,} bytes  |  "
        f"Bitrate: {playlist.total_bitrate_mbps:.2f} Mbps"
    )


@contextmanager
def virtual_input(
    image: RemoteUdfImage,
    server: VirtualFileServer,
    stream_name: str | None,
    playlist_name: str | None,
) -> Iterator[tuple[list[str], str]]:
    """Yield ffmpeg input arguments for either one M2TS or one MPLS timeline."""
    if playlist_name:
        playlist = choose_playlist(image, playlist_name)
        if not playlist.items:
            raise RuntimeError(f"Playlist has no primary play items: {playlist.name}")

        with tempfile.TemporaryDirectory(prefix="remote_bluray_") as temp_dir:
            concat_path = Path(temp_dir) / f"{Path(playlist.name).stem}.ffconcat"
            lines = ["ffconcat version 1.0"]
            for item in playlist.items:
                stream_path = f"/BDMV/STREAM/{item.clip_id}.m2ts"
                clip = image.find(stream_path)
                # The virtual URL is local and contains only ASCII path parts.
                lines.append(f"file '{server.file_url(stream_path)}'")
                if item.in_time:
                    lines.append(f"inpoint {ffmpeg_seconds(item.in_time)}")
                if item.out_time:
                    lines.append(f"outpoint {ffmpeg_seconds(item.out_time)}")
                if clip.size <= 0:
                    raise RuntimeError(f"Empty M2TS clip referenced by {playlist.name}: {item.clip_id}")
            concat_path.write_text("\n".join(lines) + "\n", encoding="ascii")
            label = (
                f"Playlist: {playlist.name} "
                f"({len(playlist.items)} play item(s), {format_duration(playlist.duration_seconds)})"
            )
            yield (
                [
                    "-protocol_whitelist",
                    "file,http,https,tcp,tls",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_path),
                ],
                label,
            )
        return

    name, file = choose_stream(image, stream_name)
    path = f"/BDMV/STREAM/{name}"
    yield ["-i", server.file_url(path)], f"Stream: {name} ({file.size:,} bytes)"


def output_paths(output_name: str, playlists: list[Playlist], kind: str) -> list[Path]:
    """Build non-overlapping output paths for one or many playlist jobs."""
    output = Path(output_name)
    default_suffix = ".mka" if kind == "audio" else ".mkv"
    known_suffixes = {".mka", ".mkv", ".m2ts", ".ts", ".mp4"}
    if output.suffix.casefold() in known_suffixes:
        if len(playlists) == 1:
            return [output]
        directory = output.parent
        prefix = output.stem + "-"
        suffix = output.suffix
    else:
        directory = output
        prefix = ""
        suffix = default_suffix
    return [
        directory / f"{prefix}{Path(playlist.name).stem}{suffix}"
        for playlist in playlists
    ]


def input_has_audio(input_args: list[str]) -> bool:
    return bool(input_audio_codecs(input_args))


def input_audio_codecs(input_args: list[str]) -> set[str]:
    command = [
        get_executable("ffprobe"),
        "-hide_banner",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
    ] + input_args
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed while checking audio codecs")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe returned invalid JSON while checking audio codecs") from error
    return {
        str(stream.get("codec_name", "")).casefold()
        for stream in payload.get("streams", [])
        if stream.get("codec_name")
    }


def probe_stream_metadata(input_args: list[str]) -> list[dict]:
    command = [
        get_executable("ffprobe"),
        "-hide_banner",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,profile:stream_tags=language",
        "-of",
        "json",
    ] + input_args
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed while reading stream metadata")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe returned invalid JSON while reading stream metadata") from error
    return list(payload.get("streams", []))


INFO_PARTIAL_SECONDS = 100.0
DEFAULT_SCREENSHOT_SKIP_START_SECONDS = 60.0


def probe_media_info(
    input_args: list[str],
    scan_mode: str = "full",
    partial_seconds: float = INFO_PARTIAL_SECONDS,
) -> dict:
    """Probe a playlist timeline for the detailed ``info`` report.

    The packet pass in :func:`probe_packet_stats` supplies measured bitrates.
    ``-read_intervals`` keeps the metadata probe bounded in partial mode.
    """
    command = [
        get_executable("ffprobe"),
        "-hide_banner",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
    ]
    if scan_mode == "partial":
        if partial_seconds <= 0:
            raise ValueError("partial scan duration must be greater than zero")
        command.extend(["-read_intervals", f"%+{partial_seconds:g}"])
    elif scan_mode != "full":
        raise ValueError(f"Unknown info scan mode: {scan_mode}")
    command.extend(input_args)

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed while reading info")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe returned invalid JSON while reading info") from error
    return payload


def probe_packet_stats(
    input_args: list[str],
    scan_mode: str = "full",
    partial_seconds: float = INFO_PARTIAL_SECONDS,
) -> dict[int, int]:
    """Sum demuxed packet bytes by stream without buffering full output.

    Blu-ray MPEG-TS streams often omit ``bit_rate`` in their stream metadata,
    especially video and PGS subtitles.  Compact packet output lets us derive
    an average bitrate while keeping the full-scan subprocess output bounded.
    """
    command = [
        get_executable("ffprobe"),
        "-hide_banner",
        "-v",
        "error",
        "-show_packets",
        "-show_entries",
        "packet=stream_index,size",
        "-of",
        "compact=p=1:nk=0",
    ]
    if scan_mode == "partial":
        if partial_seconds <= 0:
            raise ValueError("partial scan duration must be greater than zero")
        command.extend(["-read_intervals", f"%+{partial_seconds:g}"])
    elif scan_mode != "full":
        raise ValueError(f"Unknown info scan mode: {scan_mode}")
    command.extend(input_args)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    packet_sizes: dict[int, int] = {}
    assert process.stdout is not None
    for line in process.stdout:
        if not line.startswith("packet|"):
            continue
        fields = {}
        for field in line.rstrip("\r\n").split("|")[1:]:
            key, separator, value = field.partition("=")
            if separator:
                fields[key] = value
        try:
            stream_index = int(fields["stream_index"])
            packet_size = int(fields["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if packet_size > 0:
            packet_sizes[stream_index] = packet_sizes.get(stream_index, 0) + packet_size

    process.stdout.close()
    stderr = process.stderr.read() if process.stderr is not None else ""
    returncode = process.wait()
    if returncode:
        raise RuntimeError(stderr.strip() or "ffprobe failed while measuring packet bitrates")
    return packet_sizes


LANGUAGE_NAMES = {
    "chi": "Chinese",
    "zho": "Chinese",
    "cmn": "Chinese",
    "eng": "English",
    "en": "English",
    "fra": "French",
    "fre": "French",
    "fr": "French",
    "jpn": "Japanese",
    "jap": "Japanese",
    "deu": "German",
    "ger": "German",
    "spa": "Spanish",
    "ita": "Italian",
    "kor": "Korean",
    "rus": "Russian",
}

CHINESE_LANGUAGE_CODES = {
    "chi",
    "zho",
    "cmn",
    "zh",
    "cn",
    "chs",
    "cht",
    "chinese",
}


def display_language(value: str | None) -> str:
    if not value:
        return "-"
    normalized = value.strip().casefold()
    return LANGUAGE_NAMES.get(normalized, value)


def display_codec(stream: dict) -> str:
    codec = str(stream.get("codec_name", "unknown")).casefold()
    profile = str(stream.get("profile", "")).casefold()
    if codec == "hevc":
        return "MPEG-H HEVC Video"
    if codec == "h264":
        return "AVC Video"
    if codec == "vc1":
        return "VC-1 Video"
    if codec == "mpeg2video":
        return "MPEG-2 Video"
    if codec == "dts":
        if "master" in profile or "ma" in profile:
            return "DTS-HD Master Audio"
        if "high resolution" in profile or "hi_res" in profile:
            return "DTS-HD High Resolution Audio"
        return "DTS Audio"
    if codec == "ac3":
        return "Dolby Digital Audio"
    if codec == "eac3":
        return "Dolby Digital Plus Audio"
    if codec == "truehd":
        return "Dolby TrueHD Audio"
    if codec == "pcm_bluray":
        return "Blu-ray LPCM Audio"
    if codec == "aac":
        return "AAC Audio"
    if codec == "hdmv_pgs_subtitle":
        return "Presentation Graphics"
    if codec == "dvd_subtitle":
        return "DVD Subtitles"
    if codec == "subrip":
        return "SubRip"
    return codec.replace("_", " ").upper()


def info_display_codec(stream: dict) -> str:
    """Use the longer codec names commonly found in BDInfo reports."""
    codec = str(stream.get("codec_name", "unknown")).casefold()
    if codec == "h264":
        return "MPEG-4 AVC Video"
    if codec == "hevc":
        return "MPEG-H HEVC Video"
    return display_codec(stream)


def stream_language(stream: dict) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("language") or tags.get("LANGUAGE")


def numeric_value(value) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def stream_bitrate(stream: dict) -> float | None:
    """Return a stream bitrate in bits per second when ffprobe provides one."""
    bitrate = numeric_value(stream.get("bit_rate"))
    if bitrate:
        return bitrate

    # Some demuxers expose the measured bitrate as a BPS tag instead of the
    # regular stream field.  Prefer the plain tag, then language-qualified
    # variants such as BPS-eng.
    tags = stream.get("tags") or {}
    for key, value in tags.items():
        if str(key).casefold() == "bps" or str(key).casefold().startswith("bps-"):
            bitrate = numeric_value(value)
            if bitrate:
                return bitrate
    return None


def apply_packet_bitrates(
    media_info: dict,
    packet_sizes: dict[int, int],
    duration_seconds: float,
    scan_mode: str,
    partial_seconds: float = INFO_PARTIAL_SECONDS,
) -> dict:
    """Add measured average bitrates to the ffprobe stream dictionaries."""
    if duration_seconds <= 0:
        return media_info
    measured_duration = (
        min(partial_seconds, duration_seconds)
        if scan_mode == "partial"
        else duration_seconds
    )
    if measured_duration <= 0:
        return media_info

    for stream in media_info.get("streams", []):
        try:
            stream_index = int(stream.get("index"))
        except (TypeError, ValueError):
            continue
        byte_count = packet_sizes.get(stream_index, 0)
        if byte_count > 0:
            stream["bit_rate"] = str(byte_count * 8 / measured_duration)
    return media_info


def format_bitrate_value(value: float | None) -> str:
    bitrate = numeric_value(value)
    if not bitrate:
        return "-"
    text = f"{bitrate / 1000:.3f}".rstrip("0").rstrip(".")
    return f"{text} kbps"


def rational_float(value) -> float | None:
    if value is None:
        return None
    try:
        text = str(value)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            if int(denominator) == 0:
                return None
            return float(Fraction(int(numerator), int(denominator)))
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def format_frame_rate(value) -> str | None:
    frame_rate = rational_float(value)
    if not frame_rate or frame_rate <= 0:
        return None
    return f"{frame_rate:.3f}".rstrip("0").rstrip(".") + " fps"


def display_aspect_ratio(stream: dict) -> str | None:
    value = stream.get("display_aspect_ratio")
    if value and str(value).upper() not in {"N/A", "0:0"}:
        return str(value)

    width = numeric_value(stream.get("width"))
    height = numeric_value(stream.get("height"))
    if not width or not height:
        return None
    ratio = Fraction(int(width), int(height)).limit_denominator(100)
    return f"{ratio.numerator}:{ratio.denominator}"


def format_level(value) -> str | None:
    level = numeric_value(value)
    if not level:
        return None
    if level >= 10 and float(level).is_integer():
        return f"{level / 10:.1f}".rstrip("0").rstrip(".")
    return str(value)


def format_video_description(stream: dict) -> str:
    values: list[str] = []
    height = numeric_value(stream.get("height"))
    if height:
        height_text = str(int(height)) if height.is_integer() else str(height)
        values.append(f"{height_text}p")

    frame_rate = format_frame_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
    if frame_rate:
        values.append(frame_rate)
    aspect_ratio = display_aspect_ratio(stream)
    if aspect_ratio:
        values.append(aspect_ratio)

    profile = str(stream.get("profile") or "").strip()
    level = format_level(stream.get("level"))
    if profile:
        profile_text = profile if profile.casefold().endswith("profile") else f"{profile} Profile"
        if level:
            profile_text += f" {level}"
        values.append(profile_text)
    elif level:
        values.append(f"Level {level}")

    return " / ".join(values) or "-"


def display_channel_layout(stream: dict) -> str | None:
    layout = str(stream.get("channel_layout") or "").strip()
    if layout and layout.casefold() not in {"unknown", "(none)"}:
        return layout
    channels = numeric_value(stream.get("channels"))
    if not channels:
        return None
    return {
        1: "1.0",
        2: "2.0",
        6: "5.1",
        8: "7.1",
    }.get(int(channels), f"{int(channels)} ch")


def format_audio_description(stream: dict) -> str:
    values: list[str] = []
    channel_layout = display_channel_layout(stream)
    if channel_layout:
        values.append(channel_layout)

    sample_rate = numeric_value(stream.get("sample_rate"))
    if sample_rate:
        sample_text = f"{sample_rate / 1000:.3f}".rstrip("0").rstrip(".")
        values.append(f"{sample_text} kHz")

    bits = numeric_value(stream.get("bits_per_sample") or stream.get("bits_per_raw_sample"))
    if bits:
        values.append(f"{int(bits)}-bit")

    return " / ".join(values) or "-"


def format_subtitle_description(stream: dict) -> str:
    tags = stream.get("tags") or {}
    title = tags.get("title") or tags.get("TITLE")
    return str(title) if title else "-"


def stream_signature(stream: dict) -> tuple[str, str, str, str]:
    tags = stream.get("tags") or {}
    language = str(tags.get("language") or tags.get("LANGUAGE") or "").casefold()
    return (
        str(stream.get("codec_type", "")).casefold(),
        str(stream.get("codec_name", "")).casefold(),
        str(stream.get("profile", "")).casefold(),
        language,
    )


def add_playlist_languages(playlist: Playlist, streams: list[dict]) -> list[dict]:
    """Fill missing ffprobe language tags from the MPLS stream table."""
    languages: dict[str, list[str]] = {"video": [], "audio": [], "subtitle": []}
    for stream_type, language, _coding_type in playlist.stream_metadata:
        languages.setdefault(stream_type, []).append(language)

    positions: Counter[str] = Counter()
    enriched = []
    for stream in streams:
        stream_type = str(stream.get("codec_type", "")).casefold()
        position = positions[stream_type]
        positions[stream_type] += 1
        tags = stream.get("tags") or {}
        existing = tags.get("language") or tags.get("LANGUAGE")
        language_values = languages.get(stream_type, [])
        if existing or position >= len(language_values) or not language_values[position]:
            enriched.append(stream)
            continue
        enriched_stream = dict(stream)
        enriched_tags = dict(tags)
        enriched_tags["language"] = language_values[position]
        enriched_stream["tags"] = enriched_tags
        enriched.append(enriched_stream)
    return enriched


def playlist_m2ts_names(playlist: Playlist) -> list[tuple[str, int]]:
    counts = Counter(playlist.clip_ids)
    return [(f"{clip_id}.m2ts", counts[clip_id]) for clip_id in dict.fromkeys(playlist.clip_ids)]


def playlist_stream_metadata(
    playlist: Playlist,
    server: VirtualFileServer,
    cache: dict[str, list[dict]],
) -> list[dict]:
    """Probe the first clip and add stream types found only in later clips."""
    merged: list[dict] = []
    for clip_id, _ in playlist_m2ts_names(playlist):
        clip_name = clip_id.rsplit(".", 1)[0]
        if clip_name not in cache:
            path = f"/BDMV/STREAM/{clip_name}.m2ts"
            cache[clip_name] = probe_stream_metadata(["-i", server.file_url(path)])
        streams = add_playlist_languages(playlist, cache[clip_name])
        if not merged:
            merged.extend(streams)
            continue
        available = Counter(stream_signature(stream) for stream in merged)
        for stream in streams:
            signature = stream_signature(stream)
            if available[signature]:
                available[signature] -= 1
            else:
                merged.append(stream)
    return merged


def info_table_header(columns: list[tuple[str, int]]) -> list[str]:
    header = " ".join(f"{name:<{width}}" for name, width in columns).rstrip()
    divider = " ".join(
        f"{'-' * len(name):<{width}}" for name, width in columns
    ).rstrip()
    return [header, divider]


def info_stream_rows(streams: list[dict], stream_type: str) -> list[str]:
    selected = [stream for stream in streams if stream.get("codec_type") == stream_type]
    if stream_type == "video":
        columns = [("Codec", 32), ("Bitrate", 15), ("Description", 1)]
    else:
        columns = [
            ("Codec", 32),
            ("Language", 16),
            ("Bitrate", 15),
            ("Description", 1),
        ]
    lines = info_table_header(columns)
    if not selected:
        lines.append("(none)")
        return lines

    for stream in selected:
        codec = info_display_codec(stream)
        language = display_language(stream_language(stream))
        bitrate = format_bitrate_value(stream_bitrate(stream))
        if stream_type == "video":
            description = format_video_description(stream)
        elif stream_type == "audio":
            description = format_audio_description(stream)
        else:
            description = format_subtitle_description(stream)
        if stream_type == "video":
            lines.append(f"{codec:<32} {bitrate:<15} {description}")
        else:
            lines.append(f"{codec:<32} {language:<16} {bitrate:<15} {description}")
    return lines


def udf_path_exists(image: RemoteUdfImage, path: str) -> bool:
    try:
        image.find(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def playlist_file_rows(
    image: RemoteUdfImage,
    playlist: Playlist,
) -> list[str]:
    """Format one row per unique M2TS referenced by the playlist."""
    rows = []
    seen: set[str] = set()
    for item in playlist.items:
        clip_id = item.clip_id.upper()
        if clip_id in seen:
            continue
        seen.add(clip_id)
        name = f"{clip_id}.M2TS"
        clip = image.find(f"/BDMV/STREAM/{item.clip_id}.m2ts")
        duration = item.duration_seconds
        bitrate = "-"
        if duration > 0 and clip.size > 0:
            bitrate = f"{clip.size * 8 / duration / 1000:,.0f}"
        rows.append(
            f"{name:<16} {format_duration(item.in_time / 45000.0):<21} "
            f"{format_duration(duration):<16} {clip.size:>20,} {bitrate:>14}"
        )
    return rows


def format_info_report(
    image: RemoteUdfImage,
    playlist: Playlist,
    media_info: dict,
    scan_mode: str,
    partial_seconds: float = INFO_PARTIAL_SECONDS,
) -> str:
    label = (image.volume_id or "-").strip() or "-"
    protection = "AACS" if udf_path_exists(image, "/AACS") else "None detected"
    extras = "BD-Java" if (
        udf_path_exists(image, "/BDMV/JAR")
        or udf_path_exists(image, "/BDMV/BACKUP/JAR")
    ) else "None detected"
    scan_label = (
        "Complete file"
        if scan_mode == "full"
        else f"First {partial_seconds:g} seconds only"
    )
    streams = add_playlist_languages(playlist, list(media_info.get("streams", [])))

    lines = [
        "DISC INFO",
        "",
        f"Disc Title:     {label}",
        f"Disc Label:     {label}",
        f"Disc Size:      {image.remote.size:,} bytes",
        f"Protection:     {protection}",
        f"Extras:         {extras}",
        f"Scanner:        remote-bluray {__version__} (ffprobe)",
        f"Scan:           {scan_label}",
        "",
        "PLAYLIST REPORT:",
        "",
        f"Name:                   {playlist.name.upper()}",
        f"Length:                 {format_duration(playlist.duration_seconds)} (h:m:s.ms)",
        f"Size:                   {playlist.size_bytes:,} bytes",
        f"Total Bitrate:          {playlist.total_bitrate_mbps:.2f} Mbps",
        "",
        "VIDEO:",
        "",
    ]
    lines.extend(info_stream_rows(streams, "video"))
    lines.extend(["", "AUDIO:", ""])
    lines.extend(info_stream_rows(streams, "audio"))
    lines.extend(["", "SUBTITLES:", ""])
    lines.extend(info_stream_rows(streams, "subtitle"))
    lines.extend(
        [
            "",
            "FILES:",
            "",
        ]
    )
    lines.extend(
        info_table_header(
            [
                ("Name", 16),
                ("Time In", 21),
                ("Length", 16),
                ("Size", 20),
                ("Total Bitrate", 14),
            ]
        )
    )
    lines.extend(playlist_file_rows(image, playlist))
    return "\n".join(lines)


def choose_chinese_subtitle_stream(
    playlist: Playlist,
    streams: list[dict],
    mode: str,
) -> tuple[int, str] | None:
    """Return the first Chinese subtitle's type-relative stream index."""
    if mode == "none":
        return None
    if mode != "auto":
        raise ValueError(f"Unknown screenshot subtitle mode: {mode}")

    enriched_streams = add_playlist_languages(playlist, streams)
    subtitle_index = 0
    for stream in enriched_streams:
        if stream.get("codec_type") != "subtitle":
            continue
        language = stream_language(stream)
        normalized = str(language or "").strip().casefold()
        if normalized in CHINESE_LANGUAGE_CODES or display_language(language) == "Chinese":
            return subtitle_index, display_language(language)
        subtitle_index += 1
    return None


def random_screenshot_times(
    duration_seconds: float,
    count: int,
    seed: int | None = None,
    start_seconds: float = 0.0,
) -> list[float]:
    """Choose sorted random timestamps from a playlist timeline."""
    if count < 0:
        raise ValueError("screenshot count must be non-negative")
    if count == 0:
        return []
    if duration_seconds <= 0:
        raise ValueError("cannot take screenshots from an empty timeline")
    if start_seconds < 0:
        raise ValueError("screenshot start time must be non-negative")

    # Leave a small margin at the end so a random frame is less likely to be
    # an end-of-file/black frame while still supporting very short clips.
    upper_bound = max(0.0, duration_seconds - 0.5)
    if upper_bound > 0 and start_seconds >= upper_bound:
        raise ValueError(
            f"no screenshot time remains after skipping {start_seconds:g}s; "
            f"the available timeline ends at {upper_bound:g}s. "
            "Increase --scan-duration or lower --screenshot-skip-start."
        )
    generator = random.Random(seed) if seed is not None else random.SystemRandom()
    return sorted(generator.uniform(start_seconds, upper_bound) for _ in range(count))


def screenshot_timestamp(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds_int, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}-{minutes:02d}-{seconds_int:02d}.{milliseconds:03d}"


def save_random_screenshots(
    input_args: list[str],
    playlist: Playlist,
    duration_seconds: float,
    count: int,
    output_directory: str | Path,
    seed: int | None = None,
    subtitle_index: int | None = None,
    start_seconds: float = 0.0,
) -> list[Path]:
    """Save random JPEG frames from the already-built virtual playlist input."""
    times = random_screenshot_times(duration_seconds, count, seed, start_seconds)
    if not times:
        return []

    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    playlist_stem = Path(playlist.name).stem
    for index, seconds in enumerate(times, start=1):
        output = directory / (
            f"{playlist_stem}-random-{index:02d}-{screenshot_timestamp(seconds)}.jpg"
        )
        command = [
            get_executable("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
        ] + input_args
        # Seek after opening the virtual concat input.  Input seeking can land
        # on a non-IDR HEVC frame, which produces block corruption on UHD/DV
        # playlists streamed through the local HTTP server.
        command.extend(["-ss", f"{seconds:.3f}"])
        if subtitle_index is None:
            command.extend(["-map", "0:v:0", "-vf", "format=yuv420p"])
        else:
            command.extend(
                [
                    "-filter_complex",
                    f"[0:v:0][0:s:{subtitle_index}]overlay=shortest=1,format=yuv420p[v]",
                    "-map",
                    "[v]",
                ]
            )
        command.extend([
            "-an",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output),
        ])
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode:
            raise RuntimeError(
                result.stderr.strip()
                or f"ffmpeg failed while creating screenshot at {seconds:.3f}s"
            )
        if not output.is_file():
            raise RuntimeError(f"ffmpeg did not create screenshot: {output}")
        outputs.append(output)
    return outputs


def format_labeled_values(label: str, values: list[str], width: int = 100) -> list[str]:
    text = ", ".join(values) if values else "-"
    prefix = f"  {label}: "
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=prefix,
        subsequent_indent=" " * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    ) or [prefix.rstrip()]


def format_stream_group(title: str, streams: list[dict], stream_type: str) -> list[str]:
    selected = [stream for stream in streams if stream.get("codec_type") == stream_type]
    grouped: OrderedDict[tuple[str, str], int] = OrderedDict()
    for stream in selected:
        codec = display_codec(stream)
        tags = stream.get("tags") or {}
        language = tags.get("language") or tags.get("LANGUAGE")
        key = (codec, display_language(language))
        grouped[key] = grouped.get(key, 0) + 1

    values = []
    for (codec, language), count in grouped.items():
        value = codec if language == "-" else f"{codec} ({language})"
        if count > 1:
            value += f" x{count}"
        values.append(value)
    return format_labeled_values(title, values)


def format_playlist_details(
    playlist: Playlist,
    server: VirtualFileServer,
    cache: dict[str, list[dict]],
) -> str:
    streams = playlist_stream_metadata(playlist, server, cache)
    m2ts = []
    for name, count in playlist_m2ts_names(playlist):
        suffix = f" x{count}" if count > 1 else ""
        m2ts.append(f"{name}{suffix}")
    lines = [playlist_summary(playlist)]
    lines.extend(format_labeled_values("M2TS", m2ts))
    lines.extend(format_stream_group("Video", streams, "video"))
    lines.extend(format_stream_group("Audio", streams, "audio"))
    lines.extend(format_stream_group("Subtitles", streams, "subtitle"))
    return "\n".join(lines)


def image_from_args(args) -> RemoteUdfImage:
    return RemoteUdfImage(
        args.source,
        verbose=args.verbose,
        workers=args.workers,
        prefetch=args.prefetch,
        range_size=args.range_size,
    )


def command_list(args):
    image = image_from_args(args)
    playlists = image.playlist_candidates()
    print(f"Playlists: {len(playlists)} (excluding .mpls.backup)\n")
    with VirtualFileServer(image, verbose=args.verbose) as server:
        stream_cache: dict[str, list[dict]] = {}
        for index, playlist in enumerate(playlists):
            if index:
                print()
            print(format_playlist_details(playlist, server, stream_cache), flush=True)


def collect_info_result(args, image: RemoteUdfImage, progress_file=None) -> dict:
    playlists = select_playlists(image, None, args.mode, None)
    if len(playlists) != 1:
        raise RuntimeError("info expects exactly one playlist")
    playlist = playlists[0]
    partial_seconds = INFO_PARTIAL_SECONDS
    if args.scan == "partial":
        partial_seconds = parse_duration(args.scan_duration)
        if partial_seconds <= 0:
            raise ValueError("partial scan duration must be greater than zero")
    scan_label = (
        "complete main playlist"
        if args.scan == "full"
        else f"first {partial_seconds:g} seconds of the main playlist"
    )
    screenshot_outputs: list[Path] = []
    screenshot_duration = (
        playlist.duration_seconds
        if args.scan == "full"
        else min(partial_seconds, playlist.duration_seconds)
    )
    screenshot_skip_start = 0.0
    if args.screenshot_count:
        screenshot_skip_start = parse_duration(args.screenshot_skip_start)

    with VirtualFileServer(image, verbose=args.verbose) as server:
        with virtual_input(image, server, None, playlist.name) as selected:
            input_args, _label = selected
            print(f"Scanning {playlist.name} ({scan_label})...", file=progress_file, flush=True)
            media_info = probe_media_info(input_args, args.scan, partial_seconds)
            packet_sizes = probe_packet_stats(input_args, args.scan, partial_seconds)
            apply_packet_bitrates(
                media_info,
                packet_sizes,
                playlist.duration_seconds,
                args.scan,
                partial_seconds,
            )
            screenshot_subtitle = choose_chinese_subtitle_stream(
                playlist,
                list(media_info.get("streams", [])),
                args.screenshot_subtitle,
            )
            if args.screenshot_count:
                try:
                    screenshot_outputs = save_random_screenshots(
                        input_args,
                        playlist,
                        screenshot_duration,
                        args.screenshot_count,
                        args.screenshot_dir,
                        args.seed,
                        screenshot_subtitle[0] if screenshot_subtitle else None,
                        screenshot_skip_start,
                    )
                except ValueError as error:
                    raise SystemExit(f"Error: {error}") from error
    return {
        "playlist": playlist,
        "report": format_info_report(image, playlist, media_info, args.scan, partial_seconds),
        "screenshots": screenshot_outputs,
        "screenshot_subtitle": screenshot_subtitle,
        "screenshot_skip_start": screenshot_skip_start,
    }


def print_info_result(args, result: dict) -> None:
    print(result["report"], flush=True)
    screenshot_outputs = result["screenshots"]
    if not screenshot_outputs:
        return
    screenshot_subtitle = result["screenshot_subtitle"]
    print("\nSCREENSHOTS:", flush=True)
    if screenshot_subtitle:
        print(
            f"  Subtitle: {screenshot_subtitle[1]} "
            f"(subtitle stream {screenshot_subtitle[0] + 1})",
            flush=True,
        )
    elif args.screenshot_subtitle == "none":
        print("  Subtitle: disabled", flush=True)
    else:
        print("  Subtitle: no Chinese subtitle found", flush=True)
    print(f"  Skip start: {result['screenshot_skip_start']:g} seconds", flush=True)
    for output in screenshot_outputs:
        print(f"  {output}", flush=True)


def command_info(args):
    image = image_from_args(args)
    result = collect_info_result(args, image)
    print_info_result(args, result)


def load_tmdb_module(script_path: str | Path):
    script_path = Path(script_path).expanduser()
    if not script_path.is_file():
        raise FileNotFoundError(f"TMDB script not found: {script_path}")
    module_spec = importlib.util.spec_from_file_location("remote_bluray_tmdb_info", script_path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"Unable to load TMDB script: {script_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    for name in ("fetch_tmdb_data", "generate_bbcode"):
        if not callable(getattr(module, name, None)):
            raise RuntimeError(f"TMDB script must define {name}(): {script_path}")
    return module


def tmdb_api_key(module, configured_key: str | None) -> str:
    api_key = configured_key or os.environ.get("TMDB_API_KEY")
    if not api_key:
        api_key = getattr(module, "DEFAULT_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "No TMDB API key. Use --tmdb-api-key or set TMDB_API_KEY."
        )
    return api_key


def source_filename(image: RemoteUdfImage) -> str:
    path = unquote(urlsplit(image.url).path).rstrip("/")
    filename = path.rsplit("/", 1)[-1]
    return filename or "remote-bluray.iso"


def build_ed2k_link(image: RemoteUdfImage, args, progress_file=None) -> str:
    if args.ed2k_link:
        return args.ed2k_link.strip()
    if args.ed2k_hash:
        return (
            f"ed2k://|file|{source_filename(image)}|{image.remote.size}|"
            f"{args.ed2k_hash.strip()}|/"
        )
    if getattr(args, "ed2k_auto", False):
        return (
            f"ed2k://|file|{source_filename(image)}|{image.remote.size}|"
            f"{calculate_ed2k_hash(image, progress_file)}|/"
        )
    return "[待补充 ED2K 链接]"


def run_git(repo: Path, *git_args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *git_args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git {' '.join(git_args)} failed in {repo}: {detail}")
    return completed.stdout.strip()


def upload_images_to_picx(image_paths: list[Path], repo_path: str | Path) -> list[str]:
    """Upload generated screenshots to both PicX branches and return GitHub Pages URLs."""
    if not image_paths:
        return []
    repo = Path(repo_path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"PicX repository is not a Git checkout: {repo}")
    if run_git(repo, "status", "--porcelain"):
        raise RuntimeError(f"PicX repository has local changes; commit or move them first: {repo}")

    destinations: list[tuple[Path, str]] = []
    for image_path in image_paths:
        source = Path(image_path)
        if not source.is_file():
            raise FileNotFoundError(f"Screenshot not found: {source}")
        digest = hashlib.sha1(source.read_bytes()).hexdigest()[:10]
        filename = f"{source.stem}.{digest}{source.suffix.lower()}"
        destinations.append((source, filename))

    original_branch = run_git(repo, "branch", "--show-current")
    if not original_branch:
        raise RuntimeError(f"PicX repository is in a detached HEAD state: {repo}")

    run_git(repo, "fetch", "origin")
    for branch in ("master", "gh-pages"):
        if not run_git(repo, "show-ref", "--verify", f"refs/remotes/origin/{branch}", check=False):
            raise RuntimeError(f"PicX repository has no origin/{branch} branch: {repo}")

    try:
        for branch in ("master", "gh-pages"):
            local_branch = run_git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
            if local_branch:
                run_git(repo, "checkout", branch)
            else:
                run_git(repo, "checkout", "-b", branch, f"origin/{branch}")
            run_git(repo, "pull", "--ff-only", "origin", branch)

            names_to_add: list[str] = []
            for source, filename in destinations:
                target = repo / filename
                if target.exists():
                    if target.read_bytes() != source.read_bytes():
                        raise RuntimeError(f"PicX filename collision with different content: {target}")
                else:
                    shutil.copy2(source, target)
                    names_to_add.append(filename)
            if names_to_add:
                run_git(repo, "add", "--", *names_to_add)
                if run_git(repo, "diff", "--cached", "--name-only"):
                    run_git(repo, "commit", "-m", "Upload bdshare screenshots")
                    run_git(repo, "push", "origin", branch)
    finally:
        run_git(repo, "checkout", original_branch)

    return [
        f"https://haildceu1.github.io/picx-images-hosting/{quote(filename)}"
        for _source, filename in destinations
    ]


def build_bdshare_post(
    tmdb_bbcode: str,
    poster_url: str,
    info_report: str,
    screenshot_urls: list[str],
    ed2k_link: str,
) -> str:
    lines = [f"[free][img]{poster_url}[/img]", "", tmdb_bbcode.rstrip(), "", "[code]"]
    lines.extend(info_report.rstrip().splitlines())
    lines.extend(["[/code]"])
    for screenshot_url in screenshot_urls:
        lines.extend(["", f"[img]{screenshot_url}[/img]"])
    lines.extend(["[/free]", "", "[hide][code]", ed2k_link, "[/code]", "[/hide]"])
    return "\n".join(lines)


def command_bdshare(args):
    image = image_from_args(args)
    tmdb_module = load_tmdb_module(args.tmdb_script)
    api_key = tmdb_api_key(tmdb_module, args.tmdb_api_key)
    tmdb_data = tmdb_module.fetch_tmdb_data(args.tmdb_id, args.tmdb_type, api_key)
    if not isinstance(tmdb_data, dict) or tmdb_data.get("error"):
        error = tmdb_data.get("error", "TMDB returned no data") if isinstance(tmdb_data, dict) else str(tmdb_data)
        raise RuntimeError(f"TMDB lookup failed: {error}")
    poster_url = str(tmdb_data.get("poster_url", "")).strip()
    if not poster_url:
        raise RuntimeError("TMDB lookup returned no poster URL")

    result = collect_info_result(args, image, progress_file=sys.stderr)
    screenshot_urls = upload_images_to_picx(result["screenshots"], args.picx_repo)
    tmdb_bbcode = tmdb_module.generate_bbcode(tmdb_data)
    post = build_bdshare_post(
        tmdb_bbcode,
        poster_url,
        result["report"],
        screenshot_urls,
        build_ed2k_link(image, args, progress_file=sys.stderr),
    )
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(post + "\n", encoding="utf-8")
    print(post)


def command_probe(args):
    image = image_from_args(args)
    with VirtualFileServer(image, verbose=args.verbose) as server:
        with virtual_input(image, server, args.stream, args.playlist) as selected:
            input_args, label = selected
            command = [
                get_executable("ffprobe"),
                "-hide_banner",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
            ] + input_args
            print(f"\n{label}")
            print("Running:", " ".join(command))
            result = subprocess.run(command, check=False)
            if result.returncode:
                raise SystemExit(result.returncode)


def command_extract_audio(args):
    image = image_from_args(args)
    if args.playlist or args.mode:
        playlists = select_playlists(image, args.playlist, args.mode, args.min_duration)
        if not playlists:
            raise RuntimeError("No feature playlists meet the --min-duration threshold")
        outputs = output_paths(args.output, playlists, "audio")
    else:
        playlists = []
        outputs = [Path(args.output)]

    with VirtualFileServer(image, verbose=args.verbose) as server:
        if playlists:
            jobs = zip(playlists, outputs)
        else:
            jobs = [(None, outputs[0])]
        for playlist, output in jobs:
            playlist_name = playlist.name if playlist else None
            with virtual_input(image, server, args.stream, playlist_name) as selected:
                input_args, label = selected
                has_audio = playlist is None or playlist.audio_stream_count != 0
                if playlist and playlist.audio_stream_count < 0:
                    has_audio = input_has_audio(input_args)
                if playlist and not has_audio:
                    print(f"\nSkip: {playlist.name} does not contain an audio stream")
                    continue
                audio_codecs = input_audio_codecs(input_args)
                audio_codec = "pcm_s24le" if "pcm_bluray" in audio_codecs else "copy"
                if audio_codec == "pcm_s24le":
                    print("Audio: pcm_bluray -> pcm_s24le (lossless Matroska-compatible conversion)")
                output.parent.mkdir(parents=True, exist_ok=True)
                command = [
                    get_executable("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                ] + input_args + [
                    "-map",
                    args.map,
                    "-c:a",
                    audio_codec,
                ]
                if args.duration:
                    command.extend(["-t", args.duration])
                command.extend(["-y", str(output)])
                print(f"\n{label}")
                print("Running:", " ".join(command))
                result = subprocess.run(command, check=False)
                if result.returncode:
                    raise SystemExit(result.returncode)
                print(f"Output: {output}")


def command_extract_video(args):
    image = image_from_args(args)
    if args.playlist or args.mode:
        playlists = select_playlists(image, args.playlist, args.mode, args.min_duration)
        if not playlists:
            raise RuntimeError("No feature playlists meet the --min-duration threshold")
        outputs = output_paths(args.output, playlists, "video")
    else:
        playlists = []
        outputs = [Path(args.output)]

    with VirtualFileServer(image, verbose=args.verbose) as server:
        if playlists:
            jobs = zip(playlists, outputs)
        else:
            jobs = [(None, outputs[0])]
        for playlist, output in jobs:
            output.parent.mkdir(parents=True, exist_ok=True)
            playlist_name = playlist.name if playlist else None
            with virtual_input(image, server, args.stream, playlist_name) as selected:
                input_args, label = selected
                audio_codecs = input_audio_codecs(input_args)
                if "pcm_bluray" in audio_codecs:
                    codec_args = ["-c", "copy", "-c:a", "pcm_s24le"]
                else:
                    codec_args = ["-c", "copy"]
                if "pcm_bluray" in audio_codecs:
                    print("Audio: pcm_bluray -> pcm_s24le (lossless Matroska-compatible conversion)")
                command = [
                    get_executable("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                ] + input_args + [
                    "-map",
                    args.map,
                ] + codec_args
                if args.duration:
                    command.extend(["-t", args.duration])
                command.extend(["-y", str(output)])
                print(f"\n{label}")
                print("Running:", " ".join(command))
                result = subprocess.run(command, check=False)
                if result.returncode:
                    raise SystemExit(result.returncode)
                print(f"Output: {output}")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_range_size(value: str) -> int:
    text = str(value).strip().lower()
    units = {
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
    }
    multiplier = 1
    number = text
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            multiplier = units[suffix]
            break
    try:
        parsed = int(float(number) * multiplier)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use a byte count such as 8388608 or 8M") from error
    if parsed < BLOCK_SIZE or parsed % BLOCK_SIZE:
        raise argparse.ArgumentTypeError(
            f"must be at least {BLOCK_SIZE} bytes and a multiple of {BLOCK_SIZE}"
        )
    return parsed


def add_remote_options(parser, suppress_defaults: bool = False) -> None:
    default_workers = argparse.SUPPRESS if suppress_defaults else DEFAULT_WORKERS
    default_prefetch = argparse.SUPPRESS if suppress_defaults else DEFAULT_PREFETCH
    default_range_size = argparse.SUPPRESS if suppress_defaults else DEFAULT_RANGE_SIZE
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=default_workers,
        help=f"parallel Range downloads (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--prefetch",
        type=nonnegative_int,
        default=default_prefetch,
        help=f"number of future Range chunks to prefetch (default: {DEFAULT_PREFETCH})",
    )
    parser.add_argument(
        "--range-size",
        type=parse_range_size,
        default=default_range_size,
        metavar="SIZE",
        help="HTTP Range chunk size, e.g. 4M, 8M, or 8388608",
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"remote-bluray {__version__}")
    parser.add_argument("--verbose", action="store_true", help="show local HTTP and remote Range activity")
    add_remote_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list Blu-ray streams and playlists")
    add_remote_options(list_parser, suppress_defaults=True)
    list_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    list_parser.set_defaults(func=command_list)

    info_parser = subparsers.add_parser(
        "info",
        help="report BDInfo-style encoding details for the main playlist",
    )
    add_remote_options(info_parser, suppress_defaults=True)
    info_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    info_parser.add_argument(
        "--mode",
        choices=("main",),
        default="main",
        help="playlist selection mode (currently only main is supported)",
    )
    info_parser.add_argument(
        "--scan",
        choices=("full", "partial"),
        default="full",
        help="scan the complete main playlist or a partial interval (default: full)",
    )
    info_parser.add_argument(
        "--scan-duration",
        default=str(int(INFO_PARTIAL_SECONDS)),
        help="partial scan duration in seconds or H:M:S (default: 100 seconds)",
    )
    info_parser.add_argument(
        "--screenshots",
        "--screenshot-count",
        dest="screenshot_count",
        type=nonnegative_int,
        default=0,
        metavar="N",
        help="save N random JPEG screenshots (default: disabled)",
    )
    info_parser.add_argument(
        "--screenshot-dir",
        default="info-screenshots",
        metavar="DIR",
        help="directory for random screenshots (default: info-screenshots)",
    )
    info_parser.add_argument(
        "--seed",
        type=int,
        help="optional random seed for reproducible screenshot timestamps",
    )
    info_parser.add_argument(
        "--screenshot-subtitle",
        choices=("auto", "none"),
        default="auto",
        help="burn the first Chinese subtitle into screenshots, or disable it (default: auto)",
    )
    info_parser.add_argument(
        "--screenshot-skip-start",
        default=str(int(DEFAULT_SCREENSHOT_SKIP_START_SECONDS)),
        help="skip this much of the beginning before choosing screenshots, in seconds or H:M:S (default: 60 seconds)",
    )
    info_parser.set_defaults(func=command_info)

    bdshare_parser = subparsers.add_parser(
        "bdshare",
        help="generate a complete BDShare BBCode post from TMDB, disc info, and screenshots",
    )
    add_remote_options(bdshare_parser, suppress_defaults=True)
    bdshare_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    bdshare_parser.add_argument(
        "--mode",
        choices=("main",),
        default="main",
        help="playlist selection mode (currently only main is supported)",
    )
    bdshare_parser.add_argument(
        "--scan",
        choices=("full", "partial"),
        default="partial",
        help="scan the complete main playlist or a partial interval (default: partial)",
    )
    bdshare_parser.add_argument(
        "--scan-duration",
        default="300",
        help="partial scan duration in seconds or H:M:S (default: 300 seconds)",
    )
    bdshare_parser.add_argument("--tmdb-id", required=True, help="TMDB movie or TV ID")
    bdshare_parser.add_argument(
        "--tmdb-type",
        choices=("movie", "tv"),
        default="movie",
        help="TMDB item type (default: movie)",
    )
    bdshare_parser.add_argument(
        "--tmdb-script",
        default=r"D:\Academic\tmdb_info.py",
        help=r"path to the local TMDB helper script (default: D:\Academic\tmdb_info.py)",
    )
    bdshare_parser.add_argument(
        "--tmdb-api-key",
        help="TMDB API key; otherwise TMDB_API_KEY or the helper script default is used",
    )
    bdshare_parser.add_argument(
        "--picx-repo",
        default=str(Path(__file__).resolve().parent / "picx-images-hosting"),
        metavar="DIR",
        help="local checkout of picx-images-hosting (default: ./picx-images-hosting)",
    )
    bdshare_parser.add_argument(
        "--screenshots",
        "--screenshot-count",
        dest="screenshot_count",
        type=nonnegative_int,
        default=3,
        metavar="N",
        help="save and upload N random JPEG screenshots (default: 3)",
    )
    bdshare_parser.add_argument(
        "--screenshot-dir",
        default="bdshare-screenshots",
        metavar="DIR",
        help="directory for generated screenshots (default: bdshare-screenshots)",
    )
    bdshare_parser.add_argument(
        "--seed",
        type=int,
        help="optional random seed for reproducible screenshot timestamps",
    )
    bdshare_parser.add_argument(
        "--screenshot-subtitle",
        choices=("auto", "none"),
        default="auto",
        help="burn the first Chinese subtitle into screenshots, or disable it (default: auto)",
    )
    bdshare_parser.add_argument(
        "--screenshot-skip-start",
        default=str(int(DEFAULT_SCREENSHOT_SKIP_START_SECONDS)),
        help="skip this much of the beginning before choosing screenshots, in seconds or H:M:S (default: 60 seconds)",
    )
    ed2k_group = bdshare_parser.add_mutually_exclusive_group()
    ed2k_group.add_argument("--ed2k-link", help="complete ED2K link to place in the hidden section")
    ed2k_group.add_argument(
        "--ed2k-hash",
        help="ED2K file hash; the filename and remote ISO size are filled automatically",
    )
    ed2k_group.add_argument(
        "--ed2k-auto",
        action="store_true",
        help="read the complete remote ISO and calculate its real ED2K hash",
    )
    bdshare_parser.add_argument(
        "-o",
        "--output",
        help="also save the generated BBCode post to a UTF-8 text file",
    )
    bdshare_parser.set_defaults(func=command_bdshare)

    probe_parser = subparsers.add_parser("probe", help="probe a virtual M2TS or MPLS timeline with ffprobe")
    add_remote_options(probe_parser, suppress_defaults=True)
    probe_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    probe_selection = probe_parser.add_mutually_exclusive_group()
    probe_selection.add_argument("--stream", help="M2TS filename; defaults to the largest stream")
    probe_selection.add_argument("--playlist", help="MPLS filename/number, e.g. 00000.mpls or 0")
    probe_parser.set_defaults(func=command_probe)

    extract_parser = subparsers.add_parser("extract-audio", help="copy one audio stream to an MKA file")
    add_remote_options(extract_parser, suppress_defaults=True)
    extract_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    extract_parser.add_argument("-o", "--output", required=True, help="output audio filename or directory; directories receive playlist-based .mka names")
    extract_selection = extract_parser.add_mutually_exclusive_group()
    extract_selection.add_argument("--stream", help="M2TS filename; defaults to the largest stream")
    extract_selection.add_argument(
        "--playlist",
        nargs="+",
        help="one or more MPLS filenames/numbers, e.g. 00000.mpls 00001.mpls",
    )
    extract_selection.add_argument("--mode", choices=("main", "feat"), help="automatic playlist selection mode")
    extract_parser.add_argument(
        "--min-duration",
        help="feat mode minimum playlist duration, in seconds or H:M:S(.ms)",
    )
    extract_parser.add_argument("--map", default="0:a:0", help="ffmpeg stream map, e.g. 0:a:0")
    extract_parser.add_argument("--duration", help="optional short duration for a smoke test, e.g. 1")
    extract_parser.set_defaults(func=command_extract_audio)

    video_parser = subparsers.add_parser(
        "extract-video",
        help="copy the complete selected M2TS or MPLS timeline (video and associated streams)",
    )
    add_remote_options(video_parser, suppress_defaults=True)
    video_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    video_parser.add_argument("-o", "--output", required=True, help="output video filename or directory; directories receive playlist-based .mkv names")
    video_selection = video_parser.add_mutually_exclusive_group()
    video_selection.add_argument("--stream", help="M2TS filename; defaults to the largest stream")
    video_selection.add_argument(
        "--playlist",
        nargs="+",
        help="one or more MPLS filenames/numbers, e.g. 00000.mpls 00001.mpls",
    )
    video_selection.add_argument("--mode", choices=("main", "feat"), help="automatic playlist selection mode")
    video_parser.add_argument(
        "--min-duration",
        help="feat mode minimum playlist duration, in seconds or H:M:S(.ms)",
    )
    video_parser.add_argument("--map", default="0", help="ffmpeg stream map; defaults to all streams")
    video_parser.add_argument("--duration", help="optional short duration for a smoke test, e.g. 1")
    video_parser.set_defaults(func=command_extract_video)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.verbose = args.verbose or getattr(args, "verbose", False)
    args.func(args)


if __name__ == "__main__":
    main()
