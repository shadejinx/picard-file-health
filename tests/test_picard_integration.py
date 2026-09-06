"""Tests for docs/intent/picard-integration/picard-integration-specs.md.

Uses the Qt/Picard test harness in conftest.py (qapp, FakeFile,
FakeMetadata, FakePluginApi, patch_tagger_instance, synchronous_run_task)
rather than Picard's own File/Track/PluginApi, which need a live
Tagger/Config/PluginManifest context this suite deliberately avoids.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

import analysis
from conftest import FakeFile, FakePluginApi


# --- Columns ---

def test_evaluate_returns_a_sortable_tier_index_not_raw_text(plugin):
    """@spec UI-COL-001"""
    provider = plugin.FileHealthProvider()
    unplayable = FakeFile(metadata={'~health_file_tier': 'Unplayable'})
    bad = FakeFile(metadata={'~health_file_tier': 'Bad'})
    excellent = FakeFile(metadata={'~health_file_tier': 'Excellent'})
    assert int(provider.evaluate(unplayable)) < int(provider.evaluate(bad)) < int(provider.evaluate(excellent))


def test_tooltip_lists_itemized_issues_not_a_free_text_blob(plugin, qapp, patch_tagger_instance):
    """@spec UI-COL-001"""
    patch_tagger_instance()
    delegate = plugin.FileHealthColumnDelegate()
    info = {'tier': 'Bad', 'issues': ['Issue one', 'Issue two'], 'info': [], 'changed_since_scan': False}
    tooltip = delegate._format_tooltip(info)
    assert tooltip.count('<li>') == 2


def test_enable_reconciles_live_views_synchronously_without_a_timer(plugin, patch_tagger_instance, monkeypatch):
    """@spec UI-COL-002"""
    patch_tagger_instance()
    monkeypatch.setattr(
        plugin.QtCore.QTimer,
        'singleShot',
        lambda *a, **k: pytest.fail("enable() must reconcile columns synchronously, not via a deferred timer"),
    )
    calls = []
    monkeypatch.setattr(plugin, '_install_delegate_on_live_views', lambda: calls.append('install_delegate'))
    api = FakePluginApi()
    plugin.enable(api)
    # Reconciliation runs exactly once, synchronously, within enable() itself
    # (no QTimer.singleShot call above would have raised otherwise).
    assert calls == ['install_delegate']


def test_install_delegate_on_live_views_force_shows_both_health_columns(plugin, qapp, patch_tagger_instance):
    """@spec UI-COL-002"""
    patch_tagger_instance()

    class FakeHeader:
        def __init__(self):
            self.shown = {}

        def show_column(self, index, show):
            self.shown[index] = show

    class FakeWidget(plugin.QtWidgets.QWidget):
        def __init__(self, columns):
            super().__init__()
            self.columns = columns
            self._header = FakeHeader()

        def header(self):
            return self._header

    widget = FakeWidget([plugin._FILE_HEALTH_COLUMN, plugin._TRACK_HEALTH_COLUMN])
    app = plugin.QtWidgets.QApplication.instance()
    app.allWidgets = lambda: [widget]
    try:
        plugin._install_delegate_on_live_views()
    finally:
        del app.allWidgets
    assert widget._header.shown == {0: True, 1: True}


def test_icon_is_left_aligned_not_centered(plugin, qapp, patch_tagger_instance, monkeypatch):
    """@spec UI-COL-003"""
    patch_tagger_instance()
    delegate = plugin.FileHealthColumnDelegate()
    monkeypatch.setattr(
        delegate, '_get_info',
        lambda index: {'tier': 'OK', 'issues': [], 'info': [], 'changed_since_scan': False},
    )
    painted_rects = []

    class FakeIcon:
        def paint(self, painter, rect):
            painted_rects.append(rect)

    monkeypatch.setattr(plugin, 'match_icons', [FakeIcon() for _ in range(6)])
    pixmap = plugin.QtGui.QPixmap(300, 20)
    painter = plugin.QtGui.QPainter(pixmap)
    option = plugin.QtWidgets.QStyleOptionViewItem()
    option.rect = plugin.QtCore.QRect(10, 0, 280, 20)
    index = plugin.QtCore.QModelIndex()
    delegate.paint(painter, option, index)
    painter.end()
    assert len(painted_rects) == 1
    # Fixed left padding regardless of the column's own width — a centered
    # icon's x would instead move with (rect.width() - icon_size) // 2.
    assert painted_rects[0].x() == option.rect.x() + 4


def test_tooltip_html_escapes_issue_and_note_text(plugin, qapp, patch_tagger_instance):
    """@spec UI-COL-004"""
    patch_tagger_instance()
    delegate = plugin.FileHealthColumnDelegate()
    malicious = '<script>alert(1)</script> & "quotes"'
    info = {'tier': 'Bad', 'issues': [malicious], 'info': [malicious], 'changed_since_scan': False}
    tooltip = delegate._format_tooltip(info)
    assert '<script>' not in tooltip
    assert tooltip.count('&lt;script&gt;') == 2


def test_track_health_unplayable_gets_the_same_icon_level_as_file_health(plugin):
    """@spec UI-COL-005"""
    assert plugin.TRACK_TIER_ICON_LEVEL['Unplayable'] == plugin.FILE_TIER_ICON_LEVEL['Unplayable']


def test_track_health_evaluate_ranks_unplayable_as_worst(plugin):
    """@spec UI-COL-005"""
    provider = plugin.TrackHealthProvider()
    unplayable = FakeFile(metadata={'~health_track_tier': 'Unplayable'})
    bad = FakeFile(metadata={'~health_track_tier': 'Bad'})
    excellent = FakeFile(metadata={'~health_track_tier': 'Excellent'})
    # A real "Unplayable" scan result must resolve to a genuine rank in
    # TRACK_TIERS, not the same "-1" fallback used for a lookup miss
    # (an unrecognized string) — otherwise the ordering assertion below
    # can pass by coincidence without "Unplayable" ever being a member
    # of TRACK_TIERS at all.
    assert provider.evaluate(unplayable) != "-1"
    assert int(provider.evaluate(unplayable)) < int(provider.evaluate(bad)) < int(provider.evaluate(excellent))


def test_track_health_unplayable_is_distinguishable_from_not_yet_scanned(plugin):
    """@spec UI-COL-005"""
    provider = plugin.TrackHealthProvider()
    unplayable = FakeFile(metadata={'~health_track_tier': 'Unplayable'})
    not_yet_scanned = FakeFile(metadata={})
    assert provider.evaluate(unplayable) != provider.evaluate(not_yet_scanned)


# --- Actions ---

def test_file_health_action_registered_for_files_clusters_and_tracks(plugin, patch_tagger_instance):
    """@spec UI-ACTION-001"""
    patch_tagger_instance()
    api = FakePluginApi()
    plugin.enable(api)
    assert plugin.ScanFileHealthAction in api.registered_file_actions
    assert plugin.ScanFileHealthAction in api.registered_cluster_actions
    assert plugin.ScanFileHealthAction in api.registered_track_actions


def test_track_health_action_registered_only_for_matched_tracks(plugin, patch_tagger_instance):
    """@spec UI-ACTION-002"""
    patch_tagger_instance()
    api = FakePluginApi()
    plugin.enable(api)
    assert plugin.ScanTrackHealthAction in api.registered_track_actions
    assert plugin.ScanTrackHealthAction not in api.registered_file_actions
    assert plugin.ScanTrackHealthAction not in api.registered_cluster_actions


def test_scan_action_schedules_via_run_task_instead_of_running_inline(plugin, patch_tagger_instance, monkeypatch):
    """@spec UI-ACTION-003, UI-ACTION-004"""
    patch_tagger_instance()
    monkeypatch.setattr(plugin, 'iter_files_from_objects', lambda objs: objs)
    scheduled = []
    monkeypatch.setattr(plugin, 'run_task', lambda func, next_func=None, **kw: scheduled.append((func, next_func)))
    action = plugin.ScanFileHealthAction()
    action.api = FakePluginApi()
    action.api.plugin_config.register_option('ffmpeg_path', '')
    file = FakeFile()
    action.callback([file])
    # Nothing ran inline — the scan work is only scheduled, not executed,
    # by the time callback() returns.
    assert len(scheduled) == 1
    assert file.pending is True


def test_scan_error_clears_pending_and_names_the_failure_in_the_status_bar(plugin, patch_tagger_instance):
    """@spec UI-ACTION-005"""
    tagger = patch_tagger_instance()
    file = FakeFile()
    file.pending = True
    plugin._file_health_scan_finished(file, None, RuntimeError("ffmpeg vanished mid-scan"))
    assert file.pending is False
    tagger.window.set_statusbar_message.assert_called_once()
    _, kwargs = tagger.window.set_statusbar_message.call_args[0], tagger.window.set_statusbar_message.call_args[1]
    call_args = tagger.window.set_statusbar_message.call_args[0]
    assert "ffmpeg vanished mid-scan" in call_args[1]['error']


def test_scan_completion_after_file_removed_from_every_tree_does_not_raise(plugin, patch_tagger_instance):
    """@spec UI-ACTION-006"""
    patch_tagger_instance()
    file = FakeFile()
    file.parent_item = None
    file.ui_item = None
    result = {
        'file_tier': 'OK', 'file_flags': '', 'info': '', 'content_hash': 'abc123',
        'codec_name': 'mp3', 'profile': None, 'bitrate_kbps': 192.0,
        'sample_rate': 44100.0, 'channels': 2.0,
    }
    plugin._file_health_scan_finished(file, result, None)  # must not raise
    assert file.pending is False


# --- Options Page ---

def test_ffmpeg_path_is_validated_before_being_accepted(plugin, qapp, patch_tagger_instance, monkeypatch):
    """@spec UI-OPTIONS-001"""
    patch_tagger_instance()
    page = plugin.HealthOptionsPage()

    def not_found(path):
        raise analysis.FfmpegNotFoundError("not found at /bad/path")
    monkeypatch.setattr(plugin.analysis, 'find_ffmpeg', not_found)
    page.ffmpeg_path_edit.setText("/bad/path")
    assert "not found at /bad/path" in page.ffmpeg_status_label.text()

    monkeypatch.setattr(plugin.analysis, 'find_ffmpeg', lambda path: "/usr/bin/ffmpeg")
    monkeypatch.setattr(plugin.analysis, 'get_ffmpeg_version', lambda path: (1, 0, 0))
    page._refresh_ffmpeg_status()
    assert "too old" in page.ffmpeg_status_label.text().lower()


def test_options_page_exposes_all_six_sensitivity_sliders(plugin, qapp, patch_tagger_instance):
    """@spec UI-OPTIONS-002"""
    patch_tagger_instance()
    page = plugin.HealthOptionsPage()
    sliders = [
        page.clip_slider, page.true_peak_slider, page.spectral_silence_slider,
        page.phase_angle_slider, page.dr14_shift_slider, page.noise_floor_slider,
    ]
    assert len(sliders) == 6
    assert all(isinstance(s, plugin._SensitivitySlider) for s in sliders)


# --- Details Window ---

def test_check_cells_recompute_against_current_thresholds_not_a_stored_tier(plugin):
    """@spec UI-DETAILS-001"""
    file = FakeFile(metadata={'~health_clip_flat_factor': '5.0'})
    lenient_state, _ = plugin._check_cells(file, analysis.Thresholds(clip_flat_factor=90.0))['Clipping']
    strict_state, _ = plugin._check_cells(file, analysis.Thresholds(clip_flat_factor=1.0))['Clipping']
    assert lenient_state == 'pass'
    assert strict_state == 'fail'


def test_tied_files_get_no_bold_winner(plugin, qapp, patch_tagger_instance):
    """@spec UI-DETAILS-002"""
    patch_tagger_instance()
    panel = plugin.DetailsPanel(None, analysis.Thresholds())
    a = FakeFile("/a.mp3", {'~health_file_tier': 'Good', '~health_track_tier': 'Good'})
    b = FakeFile("/b.mp3", {'~health_file_tier': 'Good', '~health_track_tier': 'Good'})
    panel.add_group("Group", [a, b])
    header = panel.tree.topLevelItem(0)
    assert not header.child(0).font(0).bold()
    assert not header.child(1).font(0).bold()


def test_unambiguous_winner_gets_bolded(plugin, qapp, patch_tagger_instance):
    """@spec UI-DETAILS-002"""
    patch_tagger_instance()
    panel = plugin.DetailsPanel(None, analysis.Thresholds())
    winner = FakeFile("/a.mp3", {'~health_file_tier': 'Excellent', '~health_track_tier': 'Excellent'})
    loser = FakeFile("/b.mp3", {'~health_file_tier': 'OK', '~health_track_tier': 'OK'})
    panel.add_group("Group", [winner, loser])
    header = panel.tree.topLevelItem(0)
    assert header.child(0).font(0).bold()
    assert not header.child(1).font(0).bold()


# --- Spectrogram Viewer ---

def test_spectrogram_is_not_rendered_during_a_routine_scan(plugin, clean_wav, monkeypatch):
    """@spec UI-SPECTRO-001"""
    calls = []
    monkeypatch.setattr(plugin.analysis, 'generate_spectrogram', lambda *a, **kw: calls.append(a))
    plugin._scan_file_health_one(clean_wav, None)
    plugin._scan_track_health_one(clean_wav, None, analysis.Thresholds())
    assert calls == []


def _panel_with_selected_file(plugin, qapp):
    panel = plugin.DetailsPanel(None, analysis.Thresholds())
    file = FakeFile()
    panel.add_group("g", [file])
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0).child(0))
    return panel


def test_spectrogram_temp_file_deleted_after_a_successful_render(
    plugin, qapp, patch_tagger_instance, synchronous_run_task, monkeypatch,
):
    """@spec UI-SPECTRO-002"""
    patch_tagger_instance()
    panel = _panel_with_selected_file(plugin, qapp)
    captured = {}

    def fake_generate(filename, path, ffmpeg_path=None):
        captured['path'] = path
        with open(path, 'wb') as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
        return True
    monkeypatch.setattr(plugin.analysis, 'generate_spectrogram', fake_generate)
    panel._show_spectrogram()
    assert 'path' in captured
    assert not os.path.exists(captured['path'])


def test_spectrogram_temp_file_deleted_when_render_reports_failure(
    plugin, qapp, patch_tagger_instance, synchronous_run_task, monkeypatch,
):
    """@spec UI-SPECTRO-002"""
    patch_tagger_instance()
    panel = _panel_with_selected_file(plugin, qapp)
    captured = {}

    def fake_generate(filename, path, ffmpeg_path=None):
        captured['path'] = path
        return False
    monkeypatch.setattr(plugin.analysis, 'generate_spectrogram', fake_generate)
    panel._show_spectrogram()
    assert not os.path.exists(captured['path'])


def test_spectrogram_temp_file_deleted_on_unexpected_exception(
    plugin, qapp, patch_tagger_instance, synchronous_run_task, monkeypatch,
):
    """@spec UI-SPECTRO-002"""
    patch_tagger_instance()
    panel = _panel_with_selected_file(plugin, qapp)
    captured = {}

    def fake_generate(filename, path, ffmpeg_path=None):
        captured['path'] = path
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(plugin.analysis, 'generate_spectrogram', fake_generate)
    panel._show_spectrogram()  # synchronous_run_task routes the raised error to on_done
    assert not os.path.exists(captured['path'])


# --- Help Dialog ---

def test_help_dialog_takes_no_file_or_tag_argument(plugin, qapp, patch_tagger_instance):
    """@spec UI-HELP-001"""
    patch_tagger_instance()
    params = list(inspect.signature(plugin.HelpDialog.__init__).parameters)
    assert params == ['self', 'parent']
    dialog = plugin.HelpDialog()
    assert dialog is not None


def test_help_documents_that_resaving_triggers_the_changed_since_scan_indicator(plugin):
    """@spec UI-HELP-002"""
    all_text = " ".join(html for _, html in plugin.help_content.SECTIONS).lower()
    assert "changed since" in all_text
    assert "re-sav" in all_text


# --- Metadata Persistence ---

def _file_health_result(content_hash: str) -> dict[str, object]:
    return {
        'file_tier': 'Good', 'file_flags': 'issue a', 'info': '', 'content_hash': content_hash,
        'codec_name': 'mp3', 'profile': None, 'bitrate_kbps': 256.0,
        'sample_rate': 44100.0, 'channels': 2.0,
    }


def _track_health_result(has_mains_hum: bool | None = None) -> dict[str, object]:
    return {
        'track_tier': 'Bad' if has_mains_hum else 'OK', 'track_flags': '', 'info': '',
        'track_score': 0.4 if has_mains_hum else 0.2, 'content_hash': 'hash',
        'bandwidth_hz': None, 'noise_floor_db': None, 'dr14': None, 'stereo_coherence': None,
        'true_peak_dbtp': None, 'clipping_flat_factor': None, 'spectral_cutoff_db': None,
        'hires_cutoff_db': None, 'is_out_of_phase': None, 'is_mono_duplicated': None,
        'has_mains_hum': has_mains_hum, 'peak_db': None,
        'codec_name': 'mp3', 'profile': None, 'bitrate_kbps': 256.0,
        'sample_rate': 44100.0, 'channels': 2.0,
    }


def test_scan_result_is_persisted_onto_file_metadata_fields(plugin):
    """@spec UI-META-001"""
    file = FakeFile()
    plugin._file_health_scan_finished(file, _file_health_result('hash1'), None)
    assert file.metadata['~health_file_tier'] == 'Good'
    assert file.metadata['~health_file_content_hash'] == 'hash1'
    assert file.metadata['~health_codec_name'] == 'mp3'


def test_content_hash_change_flags_changed_since_scan(plugin):
    """@spec UI-META-002"""
    file = FakeFile()
    plugin._file_health_scan_finished(file, _file_health_result('hashA'), None)
    assert file.metadata['~health_file_changed_since_scan'] == ''

    plugin._file_health_scan_finished(file, _file_health_result('hashB'), None)
    assert file.metadata['~health_file_changed_since_scan'] == '1'


# --- Security Posture ---

def test_plugin_never_opens_a_network_connection(plugin):
    """@spec UI-SEC-001

    Static guard: nothing in __init__.py may import a networking
    module — the plugin's every measurement is a local ffmpeg/ffprobe
    invocation against a file already on disk.
    """
    import inspect
    source = inspect.getsource(plugin)
    for forbidden in ("import socket", "import urllib", "import requests", "import http.client", "import ftplib"):
        assert forbidden not in source, f"found forbidden import {forbidden!r} in __init__.py"


def test_readme_discloses_plugin_capabilities():
    """@spec UI-SEC-002"""
    readme = Path(__file__).resolve().parent.parent.joinpath('README.md').read_text(encoding='utf-8')
    lower = readme.lower()
    assert "no network" in lower or "network access" in lower
    assert "ffmpeg" in lower and "subprocess" in lower
    assert "temporary" in lower or "temp file" in lower


# --- Details Window ---

def test_details_window_matrix_includes_a_mains_hum_column(plugin):
    """@spec UI-DETAILS-003"""
    assert 'Mains Hum' in plugin._CHECK_COLUMNS


def test_mains_hum_cell_fails_when_detected_and_passes_when_not(plugin, patch_tagger_instance):
    """@spec UI-DETAILS-003"""
    patch_tagger_instance()
    hummy = FakeFile()
    plugin._track_health_scan_finished(hummy, _track_health_result(has_mains_hum=True), None)
    state, _ = plugin._check_cells(hummy, analysis.Thresholds())['Mains Hum']
    assert state == 'fail'

    clean = FakeFile()
    plugin._track_health_scan_finished(clean, _track_health_result(has_mains_hum=False), None)
    state, _ = plugin._check_cells(clean, analysis.Thresholds())['Mains Hum']
    assert state == 'pass'
