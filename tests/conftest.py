"""Shared pytest fixtures for the File Health test suite.

Every audio fixture generates synthetic audio via ffmpeg's own `lavfi`
test sources rather than depending on real music files: `assets/` is
gitignored and holds real copyrighted tracks from the user's own
library, never meant to be committed or relied on by a portable test
suite (a MusicBrainz plugin-registry reviewer running this suite has no
access to that directory).

This file also builds a lightweight Qt/Picard test harness for
picard-integration specs (UI-*): an offscreen QApplication, and
duck-typed FakeFile/FakeMetadata/FakePluginApi stand-ins for Picard's
own File/Metadata/PluginApi. Picard's own File/Metadata/PluginApi
classes need a live Tagger/Config/PluginManifest context this suite
deliberately avoids (see Picard's own test/picardtestcase.py MockTagger
for the same tradeoff in Picard's own test suite) — these fakes
implement exactly the surface __init__.py's plugin functions actually
read or write, nothing more.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analysis  # noqa: E402


@pytest.fixture(scope="session")
def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        pytest.skip("ffmpeg not found on PATH")
    return path


@pytest.fixture(scope="session")
def ffprobe_path(ffmpeg_path: str) -> str:
    return analysis._find_ffprobe(ffmpeg_path)


def _run_ffmpeg(ffmpeg_path: str, args: list[str]) -> None:
    proc = subprocess.run([ffmpeg_path, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                           capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg fixture generation failed: {proc.stderr}")


@pytest.fixture()
def clean_wav(tmp_path: Path, ffmpeg_path: str) -> str:
    """A short, clean sine-wave WAV — no clipping, no phase issues, no
    corruption. The baseline "nothing wrong" fixture most tests compare
    an actual defect fixture against.
    """
    out = tmp_path / "clean.wav"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-ac", "2", str(out),
    ])
    return str(out)


@pytest.fixture()
def clipped_wav(tmp_path: Path, ffmpeg_path: str) -> str:
    """A hard-clipped WAV — same sine source boosted well past full scale
    with no limiter, so encoding to 16-bit PCM naturally saturates
    (hard-clips) at full scale — which astats' own Flat factor reliably
    detects as a run of identical extreme sample values.
    """
    out = tmp_path / "clipped.wav"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-af", "volume=50",
        "-ac", "2", "-sample_fmt", "s16", str(out),
    ])
    return str(out)


@pytest.fixture()
def phase_inverted_wav(tmp_path: Path, ffmpeg_path: str) -> str:
    """A stereo WAV whose right channel is the exact inverse of the
    left — a textbook out-of-phase fixture aphasemeter's own phasing=1
    mode reliably classifies.
    """
    out = tmp_path / "phase_inverted.wav"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-af", "pan=stereo|c0=c0|c1=-1*c0",
        str(out),
    ])
    return str(out)


@pytest.fixture()
def mono_wav(tmp_path: Path, ffmpeg_path: str) -> str:
    """A genuinely mono (single-channel) WAV — used to confirm the
    phase-check branch is skipped rather than misfiring on mono content.
    """
    out = tmp_path / "mono.wav"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-ac", "1", str(out),
    ])
    return str(out)


@pytest.fixture()
def hires_wav(tmp_path: Path, ffmpeg_path: str) -> str:
    """A genuine hi-res-rate (96kHz) WAV with full-spectrum white-noise
    content — used to confirm the fake-hi-res branch actually runs when
    the sample rate crosses FAKE_HIRES_MIN_SAMPLE_RATE_HZ. White noise
    (not a sine) so real content genuinely exists above the hi-res
    check frequency, unlike a fake-hi-res file.
    """
    out = tmp_path / "hires.wav"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "anoisesrc=duration=3:color=white:sample_rate=96000",
        "-ac", "2", str(out),
    ])
    return str(out)


@pytest.fixture()
def clean_music_wav(tmp_path: Path, ffmpeg_path: str) -> str:
    """A realistic "nothing wrong" Track Health fixture — unlike
    `clean_wav`'s pure sine (which reads as heavily compressed/DR0 to a
    real dynamic-range meter, since a constant tone has almost no crest
    factor), this is tremolo-modulated pink noise: full-spectrum content
    with genuine loud/quiet variation, so it doesn't trip the spectral
    cutoff or DR14 checks the way a synthetic tone would.
    """
    out = tmp_path / "clean_music.wav"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "anoisesrc=duration=6:color=pink:sample_rate=44100",
        "-af", "volume=0.4,tremolo=f=0.3:d=0.95",
        "-ac", "2", str(out),
    ])
    return str(out)


@pytest.fixture()
def lowpassed_mp3(tmp_path: Path, ffmpeg_path: str) -> str:
    """An MP3 whose source was aggressively lowpassed before encoding —
    a stand-in for "converted from a lossy file", triggering the
    spectral-cutoff check.
    """
    out = tmp_path / "lowpassed.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-af", "lowpass=f=12000",
        "-b:a", "192k", str(out),
    ])
    return str(out)


@pytest.fixture()
def flac_clean(tmp_path: Path, ffmpeg_path: str) -> str:
    """A clean lossless FLAC — used for File Health's Excellent-tier path."""
    out = tmp_path / "clean.flac"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-ac", "2", str(out),
    ])
    return str(out)


@pytest.fixture()
def low_bitrate_mp3(tmp_path: Path, ffmpeg_path: str) -> str:
    """A clean-content MP3 encoded well below the published MP3
    transparency threshold (192kbps) — used for File Health's clean-file
    OK-tier path.
    """
    out = tmp_path / "low_bitrate.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-b:a", "96k", str(out),
    ])
    return str(out)


@pytest.fixture()
def high_bitrate_mp3(tmp_path: Path, ffmpeg_path: str) -> str:
    """A clean-content MP3 encoded at/above the published MP3
    transparency threshold (192kbps) — used for File Health's clean-file
    Good-tier path.
    """
    out = tmp_path / "high_bitrate.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-b:a", "256k", str(out),
    ])
    return str(out)


@pytest.fixture()
def corrupt_mp3_bit_reservoir(tmp_path: Path, ffmpeg_path: str) -> str:
    """An MP3 with an engineered bit-reservoir corruption — flips bytes
    inside the compressed frame data of an otherwise-valid MP3 so
    ffmpeg's own decoder logs a `bits_left` diagnostic.

    Which exact byte offset triggers the diagnostic is sensitive to the
    specific encode's own frame/granule boundaries (confirmed
    empirically: a fixed fractional offset that worked for one
    encoder-generated fixture did not for another). Rather than pin one
    offset that could silently stop working against a different ffmpeg
    build, this scans a spread of candidate offsets against the
    detector itself and uses the first one that actually fires —
    self-verifying at fixture-creation time rather than assumed.
    """
    src = tmp_path / "source_for_corruption.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-b:a", "192k", str(src),
    ])
    original = src.read_bytes()
    out = tmp_path / "corrupt.mp3"
    flip_width = 24
    for offset in range(200, len(original) - 200, 200):
        candidate = bytearray(original)
        for i in range(offset, offset + flip_width):
            candidate[i] ^= 0xFF
        out.write_bytes(bytes(candidate))
        if analysis.CORRUPTION_BITS_LEFT_PATTERN.search(
            analysis._run_merged_analysis(ffmpeg_path, str(out), False, False, 170.0, 44100)[1]
        ):
            return str(out)
    pytest.fail("no byte-flip offset triggered a bits_left corruption signature for this fixture")


@pytest.fixture()
def truncated_mp3(tmp_path: Path, ffmpeg_path: str) -> str:
    """An MP3 whose Xing header still declares the original (larger)
    audio-stream byte count, but whose on-disk data has been chopped
    short afterward — a genuine Xing-header/actual-size mismatch.
    """
    src = tmp_path / "source_for_truncation.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=44100",
        "-b:a", "192k", str(src),
    ])
    data = src.read_bytes()
    out = tmp_path / "truncated.mp3"
    out.write_bytes(data[: len(data) // 2])
    return str(out)


@pytest.fixture()
def undecodable_file(tmp_path: Path) -> str:
    """A file with an audio extension but no genuine audio content at
    all — ffmpeg can't decode it under any circumstance, the baseline
    "structurally impossible to measure" fixture for File Health's
    Unplayable / Track Health's decode-failure terminal state.
    """
    out = tmp_path / "not_audio.mp3"
    out.write_bytes(b"this is not an mp3 file, just plain text padding" * 20)
    return str(out)


@pytest.fixture()
def malformed_tag_mp3(tmp_path: Path, ffmpeg_path: str) -> str:
    """An MP3 with a valid audio stream but a corrupted ID3v2 header —
    the version byte is set to an unsupported value (0xFF), which
    mutagen's strict version dispatch rejects outright while ffmpeg's
    own lenient container/tag reading and audio decode are unaffected.
    """
    from mutagen import id3  # noqa: F401  (imported for clarity of intent)

    src = tmp_path / "source_for_tag.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-metadata", "title=Test", "-b:a", "192k", str(src),
    ])
    data = bytearray(src.read_bytes())
    assert data[:3] == b"ID3", "expected a leading ID3v2 header to corrupt"
    data[3] = 0xFF  # ID3v2.255 — not a real version, mutagen raises ID3UnsupportedVersionError
    out = tmp_path / "malformed_tag.mp3"
    out.write_bytes(bytes(data))
    return str(out)


@pytest.fixture()
def corrupt_artwork_mp3(tmp_path: Path, ffmpeg_path: str, ffprobe_path: str) -> str:
    """An MP3 with a genuine attached-pic (APIC) stream whose JPEG data
    is truncated to just past its header — ffprobe still reports the
    video/attached-pic stream as present, but ffmpeg's own image
    decoder can't decode a usable first frame from it.
    """
    from mutagen.id3 import ID3

    cover = tmp_path / "cover.jpg"
    _run_ffmpeg(ffmpeg_path, ["-f", "lavfi", "-i", "color=c=red:s=16x16", "-frames:v", "1", str(cover)])
    src = tmp_path / "source_for_artwork.mp3"
    _run_ffmpeg(ffmpeg_path, [
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-i", str(cover), "-map", "0:a", "-map", "1:v",
        "-c:a", "libmp3lame", "-b:a", "192k", "-c:v", "copy",
        "-id3v2_version", "3", "-metadata:s:v", "comment=Cover (front)",
        str(src),
    ])
    out = tmp_path / "corrupt_artwork.mp3"
    shutil.copy(src, out)
    tags = ID3(str(out))
    apic_key = next(k for k in tags.keys() if k.startswith("APIC"))
    tags[apic_key].data = tags[apic_key].data[:20]
    tags.save(str(out), v2_version=3)
    return str(out)


# --- Qt/Picard plugin test harness (picard-integration specs) ---

@pytest.fixture(scope="session")
def qapp():
    """Session-scoped offscreen QApplication — every Qt widget the
    plugin constructs (dialogs, options page, tree items, delegates)
    needs one alive, but only one may ever exist per process.
    """
    from PyQt6 import QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(scope="session")
def plugin(qapp):
    """Imports the plugin's __init__.py as a real package (module name
    "file_health_plugin", not the literal directory name — plugin
    directories commonly use characters, like this repo's own hyphen,
    that aren't valid in a dotted import path) so its `from . import
    analysis`-style relative imports resolve, without needing Picard's
    own plugin3 loader/manifest machinery this suite deliberately avoids.

    Pre-registers `file_health_plugin.analysis` as an alias for the
    already-imported top-level `analysis` module (this same conftest's
    own `import analysis`) rather than letting the relative import
    re-execute analysis.py under a second module identity — two
    separate module objects would mean two separate exception classes
    of the same name (e.g. `analysis.FfmpegNotFoundError` vs
    `plugin.analysis.FfmpegNotFoundError`), so a test's `except` clause
    (or the plugin's own) silently stops matching a monkeypatched
    exception raised via the "wrong" copy.
    """
    if "file_health_plugin" in sys.modules:
        return sys.modules["file_health_plugin"]
    repo_root = str(Path(__file__).resolve().parent.parent)
    spec = importlib.util.spec_from_file_location(
        "file_health_plugin", str(Path(repo_root) / "__init__.py"),
        submodule_search_locations=[repo_root],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["file_health_plugin"] = module
    sys.modules["file_health_plugin.analysis"] = analysis
    spec.loader.exec_module(module)
    return module


class FakeMetadata:
    """Minimal stand-in for picard.metadata.Metadata's dict-like API,
    replicating only the two operations __init__.py's functions actually
    use: `__getitem__` (values joined with "; ", or "" if unset — matches
    Metadata.__getitem__'s own documented contract) and `getall`.
    """

    def __init__(self, initial: dict[str, object] | None = None):
        self._values: dict[str, list[str]] = {}
        if initial:
            for key, value in initial.items():
                self[key] = value

    def __getitem__(self, name: str) -> str:
        return "; ".join(self._values.get(name, []))

    def __setitem__(self, name: str, value: object) -> None:
        if isinstance(value, list):
            self._values[name] = [str(v) for v in value]
        elif value:
            self._values[name] = [str(value)]
        else:
            self._values[name] = []

    def getall(self, name: str) -> list[str]:
        return list(self._values.get(name, []))


class FakeFile:
    """Duck-typed stand-in for picard.file.File — implements exactly the
    surface __init__.py's plugin functions read or write (`.metadata`,
    `.filename`, `.base_filename`, `.parent_item`, `.set_pending`/
    `.clear_pending`/`.update`, `.column`, `.ui_item`), without needing
    the real File's live Tagger/Config-backed bookkeeping.
    """

    def __init__(self, filename: str = "/music/test.mp3", metadata: dict[str, object] | None = None):
        self.filename = filename
        self.base_filename = os.path.basename(filename)
        self.metadata = FakeMetadata(metadata)
        self.parent_item = None
        self.ui_item = None
        self.pending = False
        self.updated = False

    def set_pending(self) -> None:
        self.pending = True

    def clear_pending(self, signal: bool = True) -> None:
        self.pending = False

    def update(self, signal: bool = True) -> None:
        self.updated = True

    def column(self, key: str) -> str:
        return self.metadata[key]


class FakePluginConfig(dict):
    def register_option(self, name: str, default: object) -> None:
        self.setdefault(name, default)


class FakePluginApi:
    """Duck-typed stand-in for picard.plugin3.api.PluginApi — records
    every registration call `enable()` makes, without needing a live
    PluginManifest/Tagger/module the real PluginApi's constructor
    requires.
    """

    def __init__(self):
        self.logger = MagicMock()
        self.plugin_config = FakePluginConfig()
        self.registered_script_variables: list[str] = []
        self.registered_file_actions: list[type] = []
        self.registered_cluster_actions: list[type] = []
        self.registered_track_actions: list[type] = []
        self.registered_tools_menu_actions: list[type] = []
        self.registered_options_pages: list[type] = []
        self.file_post_load_processors: list = []
        self.file_post_addition_processors: list = []
        self.file_post_removal_processors: list = []

    def register_script_variable(self, name, **kwargs) -> None:
        self.registered_script_variables.append(name)

    def register_file_action(self, action_cls) -> None:
        self.registered_file_actions.append(action_cls)

    def register_cluster_action(self, action_cls) -> None:
        self.registered_cluster_actions.append(action_cls)

    def register_track_action(self, action_cls) -> None:
        self.registered_track_actions.append(action_cls)

    def register_tools_menu_action(self, action_cls) -> None:
        self.registered_tools_menu_actions.append(action_cls)

    def register_options_page(self, page_cls) -> None:
        self.registered_options_pages.append(page_cls)

    def register_file_post_load_processor(self, func) -> None:
        self.file_post_load_processors.append(func)

    def register_file_post_addition_to_track_processor(self, func) -> None:
        self.file_post_addition_processors.append(func)

    def register_file_post_removal_from_track_processor(self, func) -> None:
        self.file_post_removal_processors.append(func)


@pytest.fixture()
def patch_tagger_instance(monkeypatch, plugin):
    """Patches `tagger_instance` in every module that bound its own copy
    at import time (mirrors Picard's own test/conftest.py fixture of the
    same name) — every caller in this plugin's own dependency chain
    (BaseAction.__init__, OptionsPage.__init__, run_task, and the plugin
    module itself) already did `from picard import tagger_instance`,
    so patching `picard.tagger_instance` alone would miss all of them.
    """
    tagger = MagicMock()
    tagger.window = MagicMock()

    def _patch(*extra_modules):
        modules = [
            plugin,
            "picard.ui.options",
            "picard.extension_points.item_actions",
            "picard.util.thread",
            *extra_modules,
        ]
        for module in modules:
            if isinstance(module, str):
                module = importlib.import_module(module)
            monkeypatch.setattr(module, "tagger_instance", lambda: tagger, raising=False)
        return tagger

    _patch.tagger = tagger
    return _patch


@pytest.fixture()
def synchronous_run_task(monkeypatch, plugin):
    """Replaces run_task (Picard's real QThreadPool + posted-event
    scheduler) with a synchronous stand-in that calls func() then
    next_func() immediately — tests this plugin's own scheduling/
    callback wiring (which func/next_func it hands to run_task, and
    whether the callback's own logic is correct) without reimplementing
    Qt's real thread pool and event-loop plumbing.
    """
    def _run_task_sync(func, next_func=None, priority=0, thread_pool=None, task_counter=None, traceback=True):
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 - mirrors Runnable.run's own catch-all
            if next_func is not None:
                next_func(error=exc)
        else:
            if next_func is not None:
                next_func(result=result)
    monkeypatch.setattr(plugin, "run_task", _run_task_sync)
    return _run_task_sync


@pytest.fixture(autouse=True)
def _reset_health_metadata_cache(plugin):
    """`plugin._health_metadata_cache` is module-level global state (see
    its own docstring) — reset around every test so one test's cached
    values can't leak into another's via a shared filename.
    """
    plugin._health_metadata_cache.clear()
    yield
    plugin._health_metadata_cache.clear()
