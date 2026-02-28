from __future__ import annotations

import os
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .screen import get_screen_pixel_rgb
from .config import AppConfig, MainColor, ShadeButton, default_config_path, load_config, save_config
from .image_processing import PixelGrid, load_and_resize_to_grid
from .overlay import Marker, MarkersOverlay, PointResult, PointSelectOverlay, RectResult, RectSelectOverlay, StatusOverlay
from .paint import PainterOptions, erase_canvas, paint_grid

# -*- coding: utf-8 -*-

ONE_TO_ONE_PRESET_NAME = "1:1"
SIXTEEN_NINE_PRESET_NAME = "16:9"
NINE_SIXTEEN_PRESET_NAME = "9:16"
TSHIRT_PRESET_NAME = "เสื้อยืด"

ONE_TO_ONE_PRECISIONS: dict[str, Tuple[int, int]] = {
    "เล็ก": (30, 30),
    "กลาง": (50, 50),
    "ใหญ่": (100, 100),
    "ใหญ่พิเศษ": (150, 150),
}

SIXTEEN_NINE_PRECISIONS: dict[str, Tuple[int, int]] = {
    "เล็ก": (30, 18),
    "กลาง": (50, 28),
    "ใหญ่": (100, 56),
    "ใหญ่พิเศษ": (150, 84),
}

# Portrait presets (9:16) — widths x heights provided by user
NINE_SIXTEEN_PRECISIONS: dict[str, Tuple[int, int]] = {
    "เล็ก": (18, 30),
    "กลาง": (28, 50),
    "ใหญ่": (56, 100),
    "ใหญ่พิเศษ": (84, 150),
}

TSHIRT_PARTS: dict[str, Tuple[int, int]] = {
    "ด้านหน้า": (64, 80),
    "ด้านหลัง": (64, 80),
    "แขนซ้าย": (64, 48),
    "แขนขวา": (64, 48),
}


def selection_key(preset: str, variant: Optional[str]) -> str:
    if preset in {ONE_TO_ONE_PRESET_NAME, SIXTEEN_NINE_PRESET_NAME}:
        precision = variant or "เล็ก"
        return f"{preset}::{precision}"
    if preset == TSHIRT_PRESET_NAME:
        return f"{preset}::{variant or 'ด้านหน้า'}"
    return preset


@dataclass
class LoadedImage:
    path: str
    grid: PixelGrid


@dataclass
class ClickCaptureResult:
    pos: Tuple[int, int]
    rgb: Tuple[int, int, int]


class WorkerSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    status = QtCore.Signal(str)
    verify_cell = QtCore.Signal(int, int)
    bucket_base = QtCore.Signal(str, int, int, int, int, int)
    finished = QtCore.Signal()
    error = QtCore.Signal(str)
    paused = QtCore.Signal(str)
    stopped = QtCore.Signal(str)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Beer-Studio | Painter For Heartopia")

        self.statusBar().showMessage("พร้อมใช้งาน")

        self._config_path = default_config_path()
        self._cfg = load_config(self._config_path)

        self._loaded: Optional[LoadedImage] = None
        self._canvas_rect: Optional[Tuple[int, int, int, int]] = None

        self._overlay: Optional[RectSelectOverlay] = None

        self._markers_overlay: Optional[MarkersOverlay] = None

        self._status_overlay: Optional[StatusOverlay] = None
        self._game_window_rect: Optional[Tuple[int, int, int, int]] = None

        self._esc_listener = None

        self._stop_flag = False
        self._stop_reason: Optional[str] = None  # "pause" | "stop" | None
        # Paint session state (used for pause/resume)
        self._paint_total: int = 0
        self._paint_done: set[tuple[int, int]] = set()
        self._paint_paused: bool = False
        self._paint_session_sig: Optional[tuple] = None
        self._paint_base_bucket_key: Optional[tuple[str, tuple[int, int]]] = None
        self._paint_base_bucket_rgb: Optional[tuple[int, int, int]] = None

        self._build_ui()
        self._apply_persisted_state()
        self._refresh_config_view()

    def _ensure_status_overlay(self) -> StatusOverlay:
        if self._status_overlay is None:
            self._status_overlay = StatusOverlay(title="Beer-Studio | Painter")
        return self._status_overlay

    def _capture_foreground_window_rect(self) -> Optional[Tuple[int, int, int, int]]:
        # Best-effort on Windows; used to anchor the in-game overlays.
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        except Exception:
            return None

    def _hide_status_overlay(self) -> None:
        try:
            if self._status_overlay is not None:
                self._status_overlay.stop()
        except Exception:
            pass
        # Status overlay contains replica canvas + cursors; nothing else to stop.

    def _on_worker_status(self, msg: str) -> None:
        if not bool(getattr(self._cfg, "status_overlay_enabled", True)):
            return
        ov = self._ensure_status_overlay()
        if self._game_window_rect is not None:
            ov.set_anchor_rect(self._game_window_rect)
        if not ov.isVisible():
            ov.start()
        ov.set_status(msg)

    def _on_worker_verify_cell(self, x: int, y: int) -> None:
        if not bool(getattr(self._cfg, "status_overlay_enabled", True)):
            return
        ov = self._ensure_status_overlay()
        if self._game_window_rect is not None:
            ov.set_anchor_rect(self._game_window_rect)
        if not ov.isVisible():
            ov.start()
        ov.set_verify_cursor(int(x), int(y))

    def _on_worker_progress(self, x: int, y: int) -> None:
        # IMPORTANT: connect worker signals to QObject methods (not lambdas)
        # so Qt can safely queue delivery onto the UI thread.
        total = int(self._paint_total) if int(self._paint_total) > 0 else 1
        self._on_progress(int(x), int(y), total)

    def _on_worker_bucket_base(self, main_name: str, sx: int, sy: int, r: int, g: int, b: int) -> None:
        # Remember the base bucket-fill shade so Resume can keep using region-fill.
        self._paint_base_bucket_key = (str(main_name), (int(sx), int(sy)))
        self._paint_base_bucket_rgb = (int(r), int(g), int(b))

    def _start_esc_listener(self) -> None:
        # Global hotkey so it works even when the game window is focused.
        try:
            from pynput import keyboard  # type: ignore
        except Exception:
            self.statusBar().showMessage("ปุ่มหยุด ESC ไม่พร้อมใช้งาน (pynput import failed)", 5000)
            return

        # Stop any previous listener.
        self._stop_esc_listener()

        def on_press(key):
            try:
                if key == keyboard.Key.esc:
                    # Pause painting immediately (worker thread checks should_stop).
                    self._stop_reason = "pause"
                    self._stop_flag = True

                    # Stop listening so we don't re-trigger.
                    try:
                        self._run_on_ui_thread(lambda: self.statusBar().showMessage("กำลังหยุดชั่วคราว…", 1500))
                    except Exception:
                        pass
                    return False  # stop listener
            except Exception:
                return None
            return None

        # On some Windows setups, suppress=True can fail (or require elevated privileges).
        # Prefer suppression to avoid ESC closing dialogs, but fall back if needed.
        try:
            self._esc_listener = keyboard.Listener(on_press=on_press, suppress=True)
            self._esc_listener.daemon = True
            self._esc_listener.start()
            self.statusBar().showMessage("ปุ่ม ESC พร้อมใช้งาน (ถูกยับยั้ง)", 2500)
        except Exception:
            try:
                self._esc_listener = keyboard.Listener(on_press=on_press, suppress=False)
                self._esc_listener.daemon = True
                self._esc_listener.start()
                self.statusBar().showMessage("ปุ่ม ESC พร้อมใช้งาน", 2500)
            except Exception:
                self._esc_listener = None
                self.statusBar().showMessage("ปุ่มหยุด ESC ไม่พร้อมใช้งาน (listener failed)", 5000)

    def _stop_esc_listener(self) -> None:
        try:
            if self._esc_listener is not None:
                self._esc_listener.stop()
        except Exception:
            pass
        finally:
            self._esc_listener = None

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs)

        tab_main = QtWidgets.QWidget()
        tab_main_layout = QtWidgets.QVBoxLayout(tab_main)

        tab_cfg = QtWidgets.QWidget()
        tab_cfg_layout = QtWidgets.QVBoxLayout(tab_cfg)

        tab_timing = QtWidgets.QWidget()
        tab_timing_layout = QtWidgets.QVBoxLayout(tab_timing)

        tabs.addTab(tab_main, "หลัก")
        tabs.addTab(tab_cfg, "การตั้งค่าปุ่มและสี")
        tabs.addTab(tab_timing, "จังหวะ / ความน่าเชื่อถือ")

        # Image load
        row1 = QtWidgets.QHBoxLayout()
        self.btn_load = QtWidgets.QPushButton("นำเข้าภาพ…")
        self.lbl_image = QtWidgets.QLabel("ยังไม่ได้โหลดภาพ")
        self.lbl_image.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        row1.addWidget(self.btn_load)
        row1.addWidget(self.lbl_image, 1)
        tab_main_layout.addLayout(row1)

        # Preset / Precision / Part
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Canvas preset:"))
        self.cbo_preset = QtWidgets.QComboBox()
        self.cbo_preset.addItems([ONE_TO_ONE_PRESET_NAME, SIXTEEN_NINE_PRESET_NAME, NINE_SIXTEEN_PRESET_NAME, TSHIRT_PRESET_NAME])
        row2.addWidget(self.cbo_preset, 1)

        self.lbl_precision = QtWidgets.QLabel("ความละเอียด:")
        self.cbo_precision = QtWidgets.QComboBox()
        self.cbo_precision.addItems(list(ONE_TO_ONE_PRECISIONS.keys()))
        row2.addWidget(self.lbl_precision)
        row2.addWidget(self.cbo_precision)

        self.lbl_part = QtWidgets.QLabel("ส่วน:")
        self.cbo_part = QtWidgets.QComboBox()
        self.cbo_part.addItems(list(TSHIRT_PARTS.keys()))
        row2.addWidget(self.lbl_part)
        row2.addWidget(self.cbo_part)

        self.btn_select_canvas = QtWidgets.QPushButton("เลือกพื้นที่ Canvas…")
        row2.addWidget(self.btn_select_canvas)
        tab_main_layout.addLayout(row2)

        self.lbl_canvas = QtWidgets.QLabel("Canvas: ยังไม่ได้เลือก")
        tab_main_layout.addWidget(self.lbl_canvas)

        self.lbl_global_buttons = QtWidgets.QLabel("ปุ่มพาเลต: ยังไม่ได้ตั้งค่า")
        self.lbl_global_buttons.setWordWrap(True)
        tab_main_layout.addWidget(self.lbl_global_buttons)

        # Config
        cfg_group = QtWidgets.QGroupBox("การตั้งค่าสี")
        cfg_layout = QtWidgets.QVBoxLayout(cfg_group)

        row_cfg1 = QtWidgets.QHBoxLayout()
        self.btn_set_shades_button = QtWidgets.QPushButton("ตั้งค่าปุ่มแผงสี")
        self.btn_set_back_button = QtWidgets.QPushButton("ตั้งค่าปุ่มย้อนกลับ")
        self.btn_show_main_overlay = QtWidgets.QPushButton("แสดงซ้อนสีหลัก")
        row_cfg1.addWidget(self.btn_set_shades_button)
        row_cfg1.addWidget(self.btn_set_back_button)
        row_cfg1.addWidget(self.btn_show_main_overlay)
        cfg_layout.addLayout(row_cfg1)

        row_cfg_tools = QtWidgets.QHBoxLayout()
        self.btn_set_paint_tool = QtWidgets.QPushButton("ตั้งค่าปุ่มเครื่องมือวาด")
        self.btn_set_bucket_tool = QtWidgets.QPushButton("ตั้งค่าปุ่มเครื่องมือถัง")
        row_cfg_tools.addWidget(self.btn_set_paint_tool)
        row_cfg_tools.addWidget(self.btn_set_bucket_tool)
        cfg_layout.addLayout(row_cfg_tools)

        row_cfg_tools2 = QtWidgets.QHBoxLayout()
        self.btn_set_eraser_tool = QtWidgets.QPushButton("ตั้งค่าปุ่มเครื่องมือลบ")
        self.btn_set_eraser_thick_up = QtWidgets.QPushButton("ตั้งค่าปุ่มเพิ่มความหนาลบ")
        row_cfg_tools2.addWidget(self.btn_set_eraser_tool)
        row_cfg_tools2.addWidget(self.btn_set_eraser_thick_up)
        cfg_layout.addLayout(row_cfg_tools2)

        row_cfg2 = QtWidgets.QHBoxLayout()
        self.btn_add_color = QtWidgets.QPushButton("ตั้งค่าสีใหม่…")
        self.btn_remove_color = QtWidgets.QPushButton("ลบที่เลือก")
        self.btn_fix_swap_rb = QtWidgets.QPushButton("แก้ไขสี: สลับ R/B")
        row_cfg2.addWidget(self.btn_add_color)
        row_cfg2.addWidget(self.btn_remove_color)
        row_cfg2.addWidget(self.btn_fix_swap_rb)
        cfg_layout.addLayout(row_cfg2)

        self.lst_colors = QtWidgets.QListWidget()
        cfg_layout.addWidget(self.lst_colors)

        self.lbl_cfg_hint = QtWidgets.QLabel(
            "เคล็ดลับ: ตั้งค่าภาพในเกมเป็น Windows Mode และอยู่กลางจอ"
        )
        self.lbl_cfg_hint.setWordWrap(True)
        cfg_layout.addWidget(self.lbl_cfg_hint)

        tab_cfg_layout.addWidget(cfg_group)

        tab_cfg_layout.addStretch(1)

        # Timing / reliability (tab)
        timing = QtWidgets.QGroupBox("จังหวะ / ความน่าเชื่อถือ")
        tlay = QtWidgets.QGridLayout(timing)

        def ms_spin(min_ms: int, max_ms: int, step_ms: int):
            s = QtWidgets.QSpinBox()
            s.setRange(min_ms, max_ms)
            s.setSingleStep(step_ms)
            s.setSuffix(" ms")
            return s

        self.spin_move = ms_spin(0, 500, 5)
        self.spin_down = ms_spin(0, 500, 5)
        self.spin_after = ms_spin(0, 2000, 10)
        self.spin_panel = ms_spin(0, 3000, 10)
        self.spin_shade = ms_spin(0, 2000, 10)
        self.spin_row = ms_spin(0, 5000, 10)

        self.chk_drag = QtWidgets.QCheckBox("จังหวะข้างเคียง (คลิกเร็ว)")
        self.spin_drag_step = ms_spin(0, 200, 1)
        self.spin_after_drag = ms_spin(0, 2000, 10)

        self.chk_verify = QtWidgets.QCheckBox("ตรวจสอบ (วาดซ้ำที่พลาด)")
        self.spin_verify_tol = QtWidgets.QSpinBox()
        self.spin_verify_tol.setRange(0, 255)
        self.spin_verify_tol.setSingleStep(1)
        self.spin_verify_tol.setSuffix(" tol")
        self.spin_verify_passes = QtWidgets.QSpinBox()
        self.spin_verify_passes.setRange(1, 50)
        self.spin_verify_passes.setSingleStep(1)
        self.spin_verify_passes.setSuffix(" passes")

        self.chk_verify_streaming = QtWidgets.QCheckBox("ตรวจสอบขณะวาด (หน่วงเวลา)")
        self.spin_verify_lag = QtWidgets.QSpinBox()
        self.spin_verify_lag.setRange(0, 500)
        self.spin_verify_lag.setSingleStep(1)
        self.spin_verify_lag.setSuffix(" cells")

        self.chk_verify_auto_recover = QtWidgets.QCheckBox("กู้คืนอัตโนมัติ (ข้ามการตรวจสอบที่ติด)")

        self.chk_status_overlay = QtWidgets.QCheckBox("แสดงสถานะในเกม")

        tlay.addWidget(QtWidgets.QLabel("ระยะเวลาเคลื่อนที่เมาส์:"), 0, 0)
        tlay.addWidget(self.spin_move, 0, 1)
        tlay.addWidget(QtWidgets.QLabel("กดค้างเมาส์:"), 1, 0)
        tlay.addWidget(self.spin_down, 1, 1)
        tlay.addWidget(QtWidgets.QLabel("หน่วงเวลาหลังคลิก:"), 2, 0)
        tlay.addWidget(self.spin_after, 2, 1)
        tlay.addWidget(QtWidgets.QLabel("หลังเปิดแผงสี:"), 0, 2)
        tlay.addWidget(self.spin_panel, 0, 3)
        tlay.addWidget(QtWidgets.QLabel("หลังเลือกสี:"), 1, 2)
        tlay.addWidget(self.spin_shade, 1, 3)
        tlay.addWidget(QtWidgets.QLabel("หน่วงเวลาแถว:"), 2, 2)
        tlay.addWidget(self.spin_row, 2, 3)

        tlay.addWidget(self.chk_drag, 3, 0, 1, 2)
        tlay.addWidget(QtWidgets.QLabel("หน่วงเวลาจังหวะ:"), 3, 2)
        tlay.addWidget(self.spin_drag_step, 3, 3)
        tlay.addWidget(QtWidgets.QLabel("หน่วงเวลาหลังจังหวะ:"), 4, 2)
        tlay.addWidget(self.spin_after_drag, 4, 3)

        tlay.addWidget(self.chk_verify, 4, 0, 1, 2)
        tlay.addWidget(QtWidgets.QLabel("ค่าความคลาดเคลื่อน:"), 5, 2)
        tlay.addWidget(self.spin_verify_tol, 5, 3)
        tlay.addWidget(QtWidgets.QLabel("จำนวนครั้งสูงสุด:"), 6, 2)
        tlay.addWidget(self.spin_verify_passes, 6, 3)

        tlay.addWidget(self.chk_verify_streaming, 5, 0, 1, 2)
        tlay.addWidget(QtWidgets.QLabel("หน่วงเวลาตรวจสอบ:"), 6, 0)
        tlay.addWidget(self.spin_verify_lag, 6, 1)

        tlay.addWidget(self.chk_verify_auto_recover, 7, 2, 1, 2)

        tlay.addWidget(self.chk_status_overlay, 8, 0, 1, 2)

        tab_timing_layout.addWidget(timing)

        tab_timing_layout.addStretch(1)

        # Paint (main tab)
        paint_group = QtWidgets.QGroupBox("วาด")
        paint_layout = QtWidgets.QVBoxLayout(paint_group)

        rowm = QtWidgets.QHBoxLayout()
        rowm.addWidget(QtWidgets.QLabel("วิธี:"))
        self.cbo_paint_mode = QtWidgets.QComboBox()
        self.cbo_paint_mode.addItems(["วาดตามแถว", "วาดตามสี"])
        rowm.addWidget(self.cbo_paint_mode)
        rowm.addStretch(1)
        paint_layout.addLayout(rowm)

        row_bucket = QtWidgets.QHBoxLayout()
        self.chk_bucket_fill = QtWidgets.QCheckBox("เติมถังสีที่ใช้มากที่สุดก่อน")
        self.spin_bucket_min = QtWidgets.QSpinBox()
        self.spin_bucket_min.setRange(0, 100000)
        self.spin_bucket_min.setSingleStep(10)
        self.spin_bucket_min.setSuffix(" ช่องต่ำสุด")
        row_bucket.addWidget(self.chk_bucket_fill)
        row_bucket.addStretch(1)
        row_bucket.addWidget(QtWidgets.QLabel("ค่าขั้นต่ำ:"))
        row_bucket.addWidget(self.spin_bucket_min)
        paint_layout.addLayout(row_bucket)

        row_bucket2 = QtWidgets.QHBoxLayout()
        self.chk_bucket_regions = QtWidgets.QCheckBox("เติมถังพื้นที่ใหญ่ (เส้นขอบก่อน)")
        self.spin_bucket_regions_min = QtWidgets.QSpinBox()
        self.spin_bucket_regions_min.setRange(0, 100000)
        self.spin_bucket_regions_min.setSingleStep(25)
        self.spin_bucket_regions_min.setSuffix(" ช่องต่ำสุด")
        row_bucket2.addWidget(self.chk_bucket_regions)
        row_bucket2.addStretch(1)
        row_bucket2.addWidget(QtWidgets.QLabel("ค่าขั้นต่ำ:"))
        row_bucket2.addWidget(self.spin_bucket_regions_min)
        paint_layout.addLayout(row_bucket2)

        rowp = QtWidgets.QHBoxLayout()
        self.btn_paint = QtWidgets.QPushButton("วาดตอนนี้")
        self.btn_resume = QtWidgets.QPushButton("ทำต่อ")
        self.btn_resume.setEnabled(False)
        self.btn_erase = QtWidgets.QPushButton("ลบ Canvas")
        self.btn_stop = QtWidgets.QPushButton("หยุด")
        self.btn_stop.setEnabled(False)
        rowp.addWidget(self.btn_paint)
        rowp.addWidget(self.btn_resume)
        rowp.addWidget(self.btn_erase)
        rowp.addWidget(self.btn_stop)
        paint_layout.addLayout(rowp)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        paint_layout.addWidget(self.progress)

        tab_main_layout.addWidget(paint_group)

        # Add link label at the bottom
        link_label = QtWidgets.QLabel()
        link_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        link_label.setText('<a href="https://beer-studio.com" style="color: #0078d4; text-decoration: underline;">By : Beer-Studio</a>')
        link_label.setOpenExternalLinks(False)  # We'll handle this manually
        link_label.linkActivated.connect(lambda: webbrowser.open("https://beer-studio.com"))
        link_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        tab_main_layout.addWidget(link_label)

        tab_main_layout.addStretch(1)

        # Wiring
        self.btn_load.clicked.connect(self._on_load)
        self.btn_select_canvas.clicked.connect(self._on_select_canvas)
        self.btn_set_shades_button.clicked.connect(lambda: self._capture_global_button("shades"))
        self.btn_set_back_button.clicked.connect(lambda: self._capture_global_button("back"))
        self.btn_show_main_overlay.clicked.connect(self._on_toggle_main_color_overlay)
        self.btn_set_paint_tool.clicked.connect(lambda: self._capture_global_button("paint_tool"))
        self.btn_set_bucket_tool.clicked.connect(lambda: self._capture_global_button("bucket_tool"))
        self.btn_set_eraser_tool.clicked.connect(lambda: self._capture_global_button("eraser_tool"))
        self.btn_set_eraser_thick_up.clicked.connect(lambda: self._capture_global_button("eraser_thick_up"))
        self.btn_add_color.clicked.connect(self._on_setup_new_color)
        self.btn_remove_color.clicked.connect(self._on_remove_selected_color)
        self.btn_fix_swap_rb.clicked.connect(self._on_fix_swap_rb)
        self.btn_paint.clicked.connect(self._on_paint)
        self.btn_resume.clicked.connect(self._on_resume)
        self.btn_erase.clicked.connect(self._on_erase)
        self.btn_stop.clicked.connect(self._on_stop)

        self.cbo_preset.currentTextChanged.connect(self._on_preset_changed)
        self.cbo_precision.currentTextChanged.connect(self._on_precision_changed)
        self.cbo_part.currentTextChanged.connect(self._on_part_changed)

        self.cbo_paint_mode.currentTextChanged.connect(self._on_paint_mode_changed)

        self.chk_bucket_fill.stateChanged.connect(lambda _v: self._on_bucket_fill_changed())
        self.spin_bucket_min.valueChanged.connect(lambda _v: self._on_bucket_fill_changed())
        self.chk_bucket_regions.stateChanged.connect(lambda _v: self._on_bucket_fill_changed())
        self.spin_bucket_regions_min.valueChanged.connect(lambda _v: self._on_bucket_fill_changed())

        self.spin_move.valueChanged.connect(self._on_timing_changed)
        self.spin_down.valueChanged.connect(self._on_timing_changed)
        self.spin_after.valueChanged.connect(self._on_timing_changed)
        self.spin_panel.valueChanged.connect(self._on_timing_changed)
        self.spin_shade.valueChanged.connect(self._on_timing_changed)
        self.spin_row.valueChanged.connect(self._on_timing_changed)
        self.chk_drag.stateChanged.connect(lambda _v: self._on_timing_changed(0))
        self.spin_drag_step.valueChanged.connect(self._on_timing_changed)
        self.spin_after_drag.valueChanged.connect(self._on_timing_changed)

        self.chk_verify.stateChanged.connect(lambda _v: self._on_verify_changed())
        self.spin_verify_tol.valueChanged.connect(lambda _v: self._on_verify_changed())
        self.spin_verify_passes.valueChanged.connect(lambda _v: self._on_verify_changed())
        self.chk_verify_streaming.stateChanged.connect(lambda _v: self._on_verify_changed())
        self.spin_verify_lag.valueChanged.connect(lambda _v: self._on_verify_changed())
        self.chk_verify_auto_recover.stateChanged.connect(lambda _v: self._on_verify_changed())

        self.chk_status_overlay.stateChanged.connect(lambda _v: self._on_status_overlay_changed())

    def _on_status_overlay_changed(self) -> None:
        self._cfg.status_overlay_enabled = bool(self.chk_status_overlay.isChecked())
        self._save_cfg()
        if not self._cfg.status_overlay_enabled:
            self._hide_status_overlay()

    def _on_toggle_main_color_overlay(self) -> None:
        # Toggle if already visible.
        try:
            if self._markers_overlay is not None and self._markers_overlay.isVisible():
                self._markers_overlay.hide()
                return
        except Exception:
            pass

        markers: list[Marker] = []
        for mc in getattr(self._cfg, "main_colors", []) or []:
            pos = getattr(mc, "pos", None)
            if not pos or tuple(pos) == (0, 0):
                continue
            rgb = getattr(mc, "rgb", (0, 200, 255))
            markers.append(Marker(label=str(getattr(mc, "name", "Color")), pos=(int(pos[0]), int(pos[1])), color=rgb))

        if not markers:
            QtWidgets.QMessageBox.information(
                self,
                "ไม่มีสีหลัก",
                "ยังไม่มีตำแหน่งปุ่มสีหลักที่บันทึกไว้\n\nใช้ 'ตั้งค่าสีใหม่…' ก่อน",
            )
            return

        self._markers_overlay = MarkersOverlay(
            markers=markers,
            title="ตำแหน่งปุ่มสีหลัก",
            duration_ms=15000,
        )
        self._markers_overlay.start()

    def _apply_persisted_state(self):
        # Restore preset
        if self._cfg.canvas_preset and self.cbo_preset.findText(self._cfg.canvas_preset) >= 0:
            self.cbo_preset.setCurrentText(self._cfg.canvas_preset)

        # Restore precision
        if self._cfg.canvas_preset == ONE_TO_ONE_PRESET_NAME:
            prec = getattr(self._cfg, "one_to_one_precision", None)
            if prec and self.cbo_precision.findText(prec) >= 0:
                self.cbo_precision.setCurrentText(prec)
        elif self._cfg.canvas_preset == SIXTEEN_NINE_PRESET_NAME:
            prec = getattr(self._cfg, "sixteen_nine_precision", None)
            if prec and self.cbo_precision.findText(prec) >= 0:
                self.cbo_precision.setCurrentText(prec)

        # Restore T-Shirt part
        if self._cfg.tshirt_part and self.cbo_part.findText(self._cfg.tshirt_part) >= 0:
            self.cbo_part.setCurrentText(self._cfg.tshirt_part)

        self._update_variant_ui_visibility()

        # Restore per-selection state (image + canvas)
        self._restore_selection_state()

        # Restore timing controls
        self._sync_timing_ui_from_cfg()

        # Restore paint mode
        self._sync_paint_mode_ui_from_cfg()

    def _sync_paint_mode_ui_from_cfg(self) -> None:
        # Block signals so we don't save during startup.
        self.cbo_paint_mode.blockSignals(True)
        try:
            pm = (getattr(self._cfg, "paint_mode", "row") or "row").strip().lower()
            if pm == "color":
                self.cbo_paint_mode.setCurrentText("วาดตามสี")
            else:
                self.cbo_paint_mode.setCurrentText("วาดตามแถว")
        finally:
            self.cbo_paint_mode.blockSignals(False)

    def _on_paint_mode_changed(self, _text: str) -> None:
        txt = self.cbo_paint_mode.currentText().strip().lower()
        self._cfg.paint_mode = "color" if "color" in txt else "row"
        self._save_cfg()

    def _sync_timing_ui_from_cfg(self):
        def to_ms(v: float) -> int:
            return int(round(max(0.0, float(v)) * 1000.0))

        # Block signals to avoid saving on startup repeatedly
        widgets = [
            self.spin_move,
            self.spin_down,
            self.spin_after,
            self.spin_panel,
            self.spin_shade,
            self.spin_row,
            self.spin_drag_step,
            self.spin_after_drag,
        ]
        for w in widgets:
            w.blockSignals(True)
        self.chk_drag.blockSignals(True)
        self.chk_verify.blockSignals(True)
        self.spin_verify_tol.blockSignals(True)
        self.spin_verify_passes.blockSignals(True)
        self.chk_verify_streaming.blockSignals(True)
        self.spin_verify_lag.blockSignals(True)
        self.chk_verify_auto_recover.blockSignals(True)
        self.chk_bucket_fill.blockSignals(True)
        self.spin_bucket_min.blockSignals(True)
        self.chk_bucket_regions.blockSignals(True)
        self.spin_bucket_regions_min.blockSignals(True)
        self.chk_status_overlay.blockSignals(True)

        self.spin_move.setValue(to_ms(self._cfg.move_duration_s))
        self.spin_down.setValue(to_ms(self._cfg.mouse_down_s))
        self.spin_after.setValue(to_ms(self._cfg.after_click_delay_s))
        self.spin_panel.setValue(to_ms(self._cfg.panel_open_delay_s))
        self.spin_shade.setValue(to_ms(self._cfg.shade_select_delay_s))
        self.spin_row.setValue(to_ms(self._cfg.row_delay_s))

        self.chk_drag.setChecked(bool(getattr(self._cfg, "enable_drag_strokes", False)))
        self.spin_drag_step.setValue(to_ms(getattr(self._cfg, "drag_step_duration_s", 0.01)))
        self.spin_after_drag.setValue(to_ms(getattr(self._cfg, "after_drag_delay_s", 0.02)))

        self.chk_verify.setChecked(bool(getattr(self._cfg, "verify_rows", True)))
        self.spin_verify_tol.setValue(int(getattr(self._cfg, "verify_tolerance", 35)))
        self.spin_verify_passes.setValue(int(getattr(self._cfg, "verify_max_passes", 10)))

        self.chk_verify_streaming.setChecked(bool(getattr(self._cfg, "verify_streaming_enabled", False)))
        self.spin_verify_lag.setValue(int(getattr(self._cfg, "verify_streaming_lag", 10)))
        self.chk_verify_auto_recover.setChecked(bool(getattr(self._cfg, "verify_auto_recover_loops", False)))

        self.chk_bucket_fill.setChecked(bool(getattr(self._cfg, "bucket_fill_enabled", False)))
        self.spin_bucket_min.setValue(int(getattr(self._cfg, "bucket_fill_min_cells", 50)))

        self.chk_bucket_regions.setChecked(bool(getattr(self._cfg, "bucket_fill_regions_enabled", False)))
        self.spin_bucket_regions_min.setValue(int(getattr(self._cfg, "bucket_fill_regions_min_cells", 200)))

        self.chk_status_overlay.setChecked(bool(getattr(self._cfg, "status_overlay_enabled", True)))

        for w in widgets:
            w.blockSignals(False)
        self.chk_drag.blockSignals(False)
        self.chk_verify.blockSignals(False)
        self.spin_verify_tol.blockSignals(False)
        self.spin_verify_passes.blockSignals(False)
        self.chk_verify_streaming.blockSignals(False)
        self.spin_verify_lag.blockSignals(False)
        self.chk_verify_auto_recover.blockSignals(False)
        self.chk_bucket_fill.blockSignals(False)
        self.spin_bucket_min.blockSignals(False)
        self.chk_bucket_regions.blockSignals(False)
        self.spin_bucket_regions_min.blockSignals(False)
        self.chk_status_overlay.blockSignals(False)

    def _on_timing_changed(self, _value: int):
        # Persist timing settings immediately
        def to_s(ms: int) -> float:
            return max(0.0, float(ms) / 1000.0)

        self._cfg.move_duration_s = to_s(self.spin_move.value())
        self._cfg.mouse_down_s = to_s(self.spin_down.value())
        self._cfg.after_click_delay_s = to_s(self.spin_after.value())
        self._cfg.panel_open_delay_s = to_s(self.spin_panel.value())
        self._cfg.shade_select_delay_s = to_s(self.spin_shade.value())
        self._cfg.row_delay_s = to_s(self.spin_row.value())

        self._cfg.enable_drag_strokes = bool(self.chk_drag.isChecked())
        self._cfg.drag_step_duration_s = to_s(self.spin_drag_step.value())
        self._cfg.after_drag_delay_s = to_s(self.spin_after_drag.value())
        self._save_cfg()

    def _on_verify_changed(self) -> None:
        self._cfg.verify_rows = bool(self.chk_verify.isChecked())
        self._cfg.verify_tolerance = int(self.spin_verify_tol.value())
        self._cfg.verify_max_passes = int(self.spin_verify_passes.value())
        self._cfg.verify_streaming_enabled = bool(self.chk_verify_streaming.isChecked())
        self._cfg.verify_streaming_lag = int(self.spin_verify_lag.value())
        self._cfg.verify_auto_recover_loops = bool(self.chk_verify_auto_recover.isChecked())
        self._save_cfg()

    def _on_bucket_fill_changed(self) -> None:
        self._cfg.bucket_fill_enabled = bool(self.chk_bucket_fill.isChecked())
        self._cfg.bucket_fill_min_cells = int(self.spin_bucket_min.value())
        self._cfg.bucket_fill_regions_enabled = bool(self.chk_bucket_regions.isChecked())
        self._cfg.bucket_fill_regions_min_cells = int(self.spin_bucket_regions_min.value())
        self._save_cfg()

    def _refresh_config_view(self):
        self.lst_colors.clear()
        for mc in self._cfg.main_colors:
            self.lst_colors.addItem(f"{mc.name}  ({len(mc.shades)} shades)")

        preset = self.cbo_preset.currentText()
        part_txt = ""
        if preset == ONE_TO_ONE_PRESET_NAME:
            part_txt = f" — {self.cbo_precision.currentText()}"
        elif preset == SIXTEEN_NINE_PRESET_NAME:
            part_txt = f" — {self.cbo_precision.currentText()}"
        elif preset == TSHIRT_PRESET_NAME:
            part_txt = f" — {self.cbo_part.currentText()}"

        if self._canvas_rect is None:
            self.lbl_canvas.setText(f"Canvas{part_txt}: ยังไม่ได้เลือก")
        else:
            x, y, w, h = self._canvas_rect
            self.lbl_canvas.setText(f"Canvas{part_txt}: x={x}, y={y}, w={w}, h={h}")

        sp = self._cfg.shades_panel_button_pos
        bp = self._cfg.back_button_pos
        pp = getattr(self._cfg, "paint_tool_button_pos", None)
        bk = getattr(self._cfg, "bucket_tool_button_pos", None)
        er = getattr(self._cfg, "eraser_tool_button_pos", None)
        eu = getattr(self._cfg, "eraser_thickness_up_button_pos", None)
        sp_txt = f"{sp}" if sp is not None else "(ยังไม่ได้ตั้งค่า)"
        bp_txt = f"{bp}" if bp is not None else "(ยังไม่ได้ตั้งค่า)"
        pp_txt = f"{pp}" if pp is not None else "(ยังไม่ได้ตั้งค่า)"
        bk_txt = f"{bk}" if bk is not None else "(ยังไม่ได้ตั้งค่า)"
        er_txt = f"{er}" if er is not None else "(ยังไม่ได้ตั้งค่า)"
        eu_txt = f"{eu}" if eu is not None else "(ยังไม่ได้ตั้งค่า)"
        self.lbl_global_buttons.setText(
            f"ปุ่มพาเลต — แผงสี: {sp_txt} | ย้อนกลับ: {bp_txt} | เครื่องมือวาด: {pp_txt} | ถัง: {bk_txt} | ลบ: {er_txt} | ลบ+: {eu_txt}"
        )

    def _save_cfg(self):
        save_config(self._config_path, self._cfg)

    def _run_on_ui_thread(self, fn) -> None:
        # QTimer.singleShot reliably queues work onto the Qt event loop.
        QtCore.QTimer.singleShot(0, fn)

    def _confirm_capture(self, label: str, res: ClickCaptureResult):
        self.statusBar().showMessage(f"จับภาพ {label} ที่ {res.pos} rgb={res.rgb}", 5000)
        QtWidgets.QMessageBox.information(
            self,
            "จับภาพแล้ว",
            f"จับภาพ {label} แล้ว\n\nตำแหน่ง: {res.pos}\nRGB: {res.rgb}",
        )

    def _capture_click_async(self, title: str, message: str, apply_capture):
        """Shows a prompt, then uses a fullscreen overlay to pick a point + sample RGB."""
        QtWidgets.QMessageBox.information(self, title, message)

        ov = PointSelectOverlay(instruction="คลิกที่ตำแหน่งบนหน้าจอ (ESC/คลิกขวาเพื่อยกเลิก)")
        self._point_overlay = ov  # keep alive

        def on_sel(p: PointResult):
            rgb = get_screen_pixel_rgb(int(p.x), int(p.y))
            res = ClickCaptureResult(pos=(int(p.x), int(p.y)), rgb=rgb)
            self._run_on_ui_thread(lambda: apply_capture(res))

        ov.pointSelected.connect(on_sel)
        ov.cancelled.connect(lambda: None)
        ov.start()

    def _selected_preset_wh(self) -> Tuple[int, int]:
        preset = self.cbo_preset.currentText()
        if preset == ONE_TO_ONE_PRESET_NAME:
            precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision or "เล็ก"
            return ONE_TO_ONE_PRECISIONS.get(precision, ONE_TO_ONE_PRECISIONS["เล็ก"])
        if preset == SIXTEEN_NINE_PRESET_NAME:
            precision = self.cbo_precision.currentText() or getattr(self._cfg, "sixteen_nine_precision", "เล็ก") or "เล็ก"
            return SIXTEEN_NINE_PRECISIONS.get(precision, SIXTEEN_NINE_PRECISIONS["เล็ก"])
        if preset == TSHIRT_PRESET_NAME:
            part = self.cbo_part.currentText() or self._cfg.tshirt_part or "ด้านหน้า"
            return TSHIRT_PARTS.get(part, TSHIRT_PARTS["ด้านหน้า"])
        return (30, 30)

    def _current_selection_key(self) -> str:
        preset = self.cbo_preset.currentText()
        if preset == ONE_TO_ONE_PRESET_NAME:
            precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision or "เล็ก"
            return selection_key(preset, precision)
        if preset == SIXTEEN_NINE_PRESET_NAME:
            precision = self.cbo_precision.currentText() or getattr(self._cfg, "sixteen_nine_precision", "เล็ก") or "เล็ก"
            return selection_key(preset, precision)
        if preset == TSHIRT_PRESET_NAME:
            part = self.cbo_part.currentText() if preset == TSHIRT_PRESET_NAME else None
            return selection_key(preset, part)
        return selection_key(preset, None)

    def _update_variant_ui_visibility(self) -> None:
        preset = self.cbo_preset.currentText()
        is_precision = preset in {ONE_TO_ONE_PRESET_NAME, SIXTEEN_NINE_PRESET_NAME}
        is_tshirt = preset == TSHIRT_PRESET_NAME

        self.lbl_precision.setVisible(is_precision)
        self.cbo_precision.setVisible(is_precision)
        self.lbl_part.setVisible(is_tshirt)
        self.cbo_part.setVisible(is_tshirt)

    def _restore_selection_state(self) -> None:
        sel_key = self._current_selection_key()

        rect = self._cfg.last_canvas_rect_by_key.get(sel_key)
        if rect is None and self._cfg.last_canvas_rect is not None:
            rect = tuple(self._cfg.last_canvas_rect)
        self._canvas_rect = tuple(rect) if rect is not None else None

        img_path = self._cfg.last_image_path_by_key.get(sel_key)
        if not img_path and self._cfg.last_image_path:
            img_path = self._cfg.last_image_path

        self._loaded = None
        if img_path:
            p = Path(img_path)
            if p.exists():
                try:
                    w, h = self._selected_preset_wh()
                    grid = load_and_resize_to_grid(str(p), w=w, h=h)
                    self._loaded = LoadedImage(path=str(p), grid=grid)
                    self.lbl_image.setText(f"Loaded: {p} ({w}x{h})")
                except Exception:
                    # If the image can't be loaded anymore, just ignore it.
                    self._loaded = None
                    self.lbl_image.setText("ยังไม่ได้โหลดภาพ")
            else:
                self.lbl_image.setText("ยังไม่ได้โหลดภาพ")
        else:
            self.lbl_image.setText("ยังไม่ได้โหลดภาพ")

        self._refresh_config_view()

    def _on_load(self):
        w, h = self._selected_preset_wh()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "เลือกรูปภาพ",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All Files (*.*)",
        )
        if not path:
            return
        try:
            grid = load_and_resize_to_grid(path, w=w, h=h)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "นำเข้าล้มเหลว", str(e))
            return

        self._loaded = LoadedImage(path=path, grid=grid)
        self.lbl_image.setText(f"โหลดแล้ว: {path} ({w}x{h})")

        sel_key = self._current_selection_key()
        self._cfg.last_image_path_by_key[sel_key] = path
        self._cfg.last_image_path = path
        self._cfg.canvas_preset = self.cbo_preset.currentText()
        if self.cbo_preset.currentText() == ONE_TO_ONE_PRESET_NAME:
            self._cfg.one_to_one_precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision
        if self.cbo_preset.currentText() == TSHIRT_PRESET_NAME:
            self._cfg.tshirt_part = self.cbo_part.currentText() or self._cfg.tshirt_part
        self._save_cfg()

    def _on_select_canvas(self):
        if self._loaded is None:
            QtWidgets.QMessageBox.information(self, "เลือกรูปภาพ", "นำเข้ารูปภาพก่อน")
            return

        # Build preview pixmap from the resized grid (matches the preset exactly)
        grid = self._loaded.grid
        qimg = QtGui.QImage(grid.w, grid.h, QtGui.QImage.Format.Format_RGB888)
        for y in range(grid.h):
            for x in range(grid.w):
                r, g, b = grid.get(x, y)
                qimg.setPixel(x, y, QtGui.qRgb(r, g, b))
        pix = QtGui.QPixmap.fromImage(qimg)

        self._overlay = RectSelectOverlay(preview_pixmap=pix)
        self._overlay.rectSelected.connect(self._on_canvas_rect_selected)
        self._overlay.cancelled.connect(lambda: None)
        self._overlay.start()

    def _on_canvas_rect_selected(self, r: RectResult):
        # Use selection as canvas rect (we'll refine snapping later)
        self._canvas_rect = (r.x, r.y, r.w, r.h)

        sel_key = self._current_selection_key()
        self._cfg.last_canvas_rect_by_key[sel_key] = self._canvas_rect
        self._cfg.last_canvas_rect = self._canvas_rect
        self._save_cfg()
        self._refresh_config_view()

        QtWidgets.QMessageBox.information(self, "เลือก Canvas แล้ว",
                f"บันทึกพื้นที่ Canvas แล้ว\n\nตำแหน่ง: ({r.x}, {r.y})\nขนาด: {r.w}x{r.h}",
            )

    def _capture_global_button(self, which: str):
        self._capture_click_async(
            "จับภาพ",
            "หลังจากปิดกล่องโต้ตอบนี้ คลิกที่ตำแหน่งปุ่มในเกม (คลิกจะไม่กดปุ่ม)",
            lambda res: self._apply_global_button_capture(which, res),
        )

    def _apply_global_button_capture(self, which: str, res: ClickCaptureResult):
        if which == "shades":
            self._cfg.shades_panel_button_pos = res.pos
            self._confirm_capture("shades-panel button", res)
        elif which == "back":
            self._cfg.back_button_pos = res.pos
            self._confirm_capture("back button", res)
        elif which == "paint_tool":
            self._cfg.paint_tool_button_pos = res.pos
            self._confirm_capture("paint tool button", res)
        elif which == "bucket_tool":
            self._cfg.bucket_tool_button_pos = res.pos
            self._confirm_capture("bucket tool button", res)
        elif which == "eraser_tool":
            self._cfg.eraser_tool_button_pos = res.pos
            self._confirm_capture("eraser tool button", res)
        elif which == "eraser_thick_up":
            self._cfg.eraser_thickness_up_button_pos = res.pos
            self._confirm_capture("eraser thickness + button", res)
        self._save_cfg()
        self._refresh_config_view()

    def _on_preset_changed(self, _text: str):
        self._cfg.canvas_preset = self.cbo_preset.currentText()
        self._update_variant_ui_visibility()
        if self.cbo_preset.currentText() == ONE_TO_ONE_PRESET_NAME:
            self._cfg.one_to_one_precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision
        if self.cbo_preset.currentText() == SIXTEEN_NINE_PRESET_NAME:
            self._cfg.sixteen_nine_precision = self.cbo_precision.currentText() or getattr(
                self._cfg, "sixteen_nine_precision", "เล็ก"
            )
        if self.cbo_preset.currentText() == TSHIRT_PRESET_NAME:
            self._cfg.tshirt_part = self.cbo_part.currentText() or self._cfg.tshirt_part
        self._save_cfg()
        self._restore_selection_state()

    def _on_precision_changed(self, _text: str):
        preset = self.cbo_preset.currentText()
        if preset == ONE_TO_ONE_PRESET_NAME:
            self._cfg.one_to_one_precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision
        elif preset == SIXTEEN_NINE_PRESET_NAME:
            self._cfg.sixteen_nine_precision = self.cbo_precision.currentText() or getattr(
                self._cfg, "sixteen_nine_precision", "เล็ก"
            )
        else:
            return
        self._save_cfg()
        self._restore_selection_state()

    def _on_part_changed(self, _text: str):
        if self.cbo_preset.currentText() != TSHIRT_PRESET_NAME:
            return
        self._cfg.tshirt_part = self.cbo_part.currentText() or self._cfg.tshirt_part
        self._save_cfg()
        self._restore_selection_state()

    def _on_setup_new_color(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "สีใหม่", "ชื่อสี:")
        if not ok or not name.strip():
            return
        name = name.strip()

        # Ensure global buttons exist (shades panel + back). Capture them as part of the wizard.
        self._wizard_ensure_globals_then_continue(name)

    def _wizard_ensure_globals_then_continue(self, color_name: str):
        if self._cfg.shades_panel_button_pos is None:
            self._capture_click_async(
                "ตั้งค่าสีใหม่",
                "ก่อนเพิ่มสี เราต้องการตำแหน่งปุ่มแผงสี\n\n"
                "หลังจากปิดกล่องโต้ตอบนี้ คลิกที่ปุ่มที่เปิดแผงสี",
                lambda res: self._wizard_set_global_then_continue(color_name, "shades", res),
            )
            return
        if self._cfg.back_button_pos is None:
            self._capture_click_async(
                "ตั้งค่าสีใหม่",
                "ก่อนเพิ่มสี เราต้องการตำแหน่งปุ่มย้อนกลับ\n\n"
                "หลังจากปิดกล่องโต้ตอบนี้ คลิกที่ปุ่มย้อนกลับ (กลับไปหน้าสีหลัก)",
                lambda res: self._wizard_set_global_then_continue(color_name, "back", res),
            )
            return

        self._wizard_capture_main_color(color_name)

    def _wizard_set_global_then_continue(self, color_name: str, which: str, res: ClickCaptureResult):
        if which == "shades":
            self._cfg.shades_panel_button_pos = res.pos
            self._confirm_capture("shades-panel button", res)
        elif which == "back":
            self._cfg.back_button_pos = res.pos
            self._confirm_capture("back button", res)
        self._save_cfg()
        self._refresh_config_view()
        # Continue capturing any remaining globals, then proceed.
        self._wizard_ensure_globals_then_continue(color_name)

    def _wizard_capture_main_color(self, name: str):
        self._capture_click_async(
            "ตั้งค่าสีใหม่",
            "ขั้นตอนที่ 1: คลิกปุ่มสีหลักในพาเลตหลัก",
            lambda res: self._wizard_after_main_capture(name, res),
        )

    def _wizard_after_main_capture(self, name: str, res: ClickCaptureResult):
        self._confirm_capture(f"main color '{name}'", res)
        main = MainColor(name=name, pos=res.pos, rgb=res.rgb, shades=[])
        self._cfg.main_colors.append(main)
        self._save_cfg()
        self._refresh_config_view()

        QtWidgets.QMessageBox.information(
            self,
            "ตั้งค่าสีใหม่",
            "ขั้นตอนที่ 2: เปิดแผงเฉดสีในเกม\n"
            "จากนั้นคลิกปุ่มเฉดสีแต่ละปุ่มทีละปุ่ม (คลิกซ้าย)\n"
            "เมื่อเสร็จแล้ว ให้คลิก 'เสร็จสิ้น'",
        )

        # Collect shade picks until user clicks Finish.
        # Important: keep this NON-MODAL so you can freely interact with the game.
        shades: list[ShadeButton] = []
        self._shade_capture_active = True

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"จับภาพสี — {name}")
        dlg.setModal(False)
        dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)

        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel(
            "คลิก 'จับภาพเฉดสีถัดไป' จากนั้นคลิกที่ตำแหน่งปุ่มเฉดสีในเกม\n"
            "ทำซ้ำจนกว่าจะเสร็จ จากนั้นคลิก 'เสร็จสิ้น'" 
        )
        lbl.setWordWrap(True)
        v.addWidget(lbl)

        lst = QtWidgets.QListWidget()
        v.addWidget(lst)

        row = QtWidgets.QHBoxLayout()
        btn_capture = QtWidgets.QPushButton("จับภาพเฉดสีถัดไป")
        btn_finish = QtWidgets.QPushButton("เสร็จสิ้น")
        row.addWidget(btn_capture)
        row.addWidget(btn_finish)
        v.addLayout(row)

        def add_shade_capture(res2: ClickCaptureResult):
            if not getattr(self, "_shade_capture_active", False):
                return
            shade_name = f"shade-{len(shades)+1}"
            sh = ShadeButton(name=shade_name, pos=res2.pos, rgb=res2.rgb)
            shades.append(sh)
            lst.addItem(f"{shade_name} @ {res2.pos} rgb={res2.rgb}")
            self.statusBar().showMessage(f"จับภาพ {shade_name} ที่ {res2.pos} rgb={res2.rgb}", 4000)

        def capture_one():
            if not getattr(self, "_shade_capture_active", False):
                return

            ov = PointSelectOverlay(instruction="Click the SHADE button location (ESC/right-click to cancel)")
            self._point_overlay = ov

            def on_sel(p: PointResult):
                rgb = get_screen_pixel_rgb(int(p.x), int(p.y))
                r = ClickCaptureResult(pos=(int(p.x), int(p.y)), rgb=rgb)
                self._run_on_ui_thread(lambda: add_shade_capture(r))

            def on_cancel():
                self.statusBar().showMessage("ยกเลิกการจับภาพสี", 3000)

            ov.pointSelected.connect(on_sel)
            ov.cancelled.connect(on_cancel)
            ov.start()

        def finish():
            self._shade_capture_active = False
            # Save shades into matching main color
            for mc in self._cfg.main_colors:
                if mc.name == name and mc.pos == main.pos:
                    mc.shades = shades
                    break
            self._save_cfg()
            self._refresh_config_view()
            dlg.close()

            QtWidgets.QMessageBox.information(
                self,
                "บันทึกสีแล้ว",
                f"บันทึก {len(shades)} สีสำหรับ '{name}'.",
            )

        def on_close(_event):
            # If user closes the window, stop capturing to avoid orphan overlays.
            self._shade_capture_active = False

        dlg.closeEvent = on_close  # type: ignore[method-assign]

        btn_capture.clicked.connect(capture_one)
        btn_finish.clicked.connect(finish)

        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_remove_selected_color(self):
        idx = self.lst_colors.currentRow()
        if idx < 0:
            return
        if idx >= len(self._cfg.main_colors):
            return
        name = self._cfg.main_colors[idx].name
        if (
            QtWidgets.QMessageBox.question(
                self,
                "ลบ",
                f"ลบสี '{name}' หรือไม่?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return
        self._cfg.main_colors.pop(idx)
        self._save_cfg()
        self._refresh_config_view()

    def _on_fix_swap_rb(self):
        if (
            QtWidgets.QMessageBox.question(
                self,
                "สลับช่อง R/B?",
                "หากสีพาเลตที่จับภาพมาดูผิด (เช่น เหลืองทำงานเหมือนน้ำเงิน)\n"
                "นี่อาจเป็นเพราะปัญหาการสลับช่องสี R/B\n"
                "นี่จะสลับช่องสีแดงและน้ำเงินสำหรับสีหลัก/สีรองทั้งหมดที่บันทึกไว้\n\n"
                "ดำเนินการต่อ?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        def swap(rgb):
            r, g, b = rgb
            return (b, g, r)

        for mc in self._cfg.main_colors:
            mc.rgb = swap(mc.rgb)
            for sh in mc.shades:
                sh.rgb = swap(sh.rgb)

        self._save_cfg()
        self._refresh_config_view()
        QtWidgets.QMessageBox.information(self, "เสร็จสิ้น", "สลับ R/B สำหรับสีที่บันทึกแล้ว")

    def _on_paint(self):
        if self._loaded is None:
            QtWidgets.QMessageBox.information(self, "ขาดหาย", "นำเข้ารูปภาพก่อน")
            return
        if self._canvas_rect is None:
            QtWidgets.QMessageBox.information(self, "ขาดหาย", "เลือกพื้นที่ Canvas ก่อน")
            return

        if (
            not self._cfg.main_colors
            or self._cfg.shades_panel_button_pos is None
            or self._cfg.back_button_pos is None
        ):
            QtWidgets.QMessageBox.information(
                self,
                "ขาดการตั้งค่า",
                "ตั้งค่าสีและปุ่มทั่วไปของคุณก่อน\n\n"
                "Required: at least one main color with shades, plus the shades-panel and back buttons.",
            )
            return

        # Safety prompt
        if (
            QtWidgets.QMessageBox.warning(
                self,
                "กำลังจะวาด",
                "นี่จะควบคุมเมาส์และคลิกในเกม\n"
                "ตรวจสอบให้แน่ใจว่าเกมถูกโฟกัสและพาเลต/ผืนผ้าใบมองเห็นได้\n\n"
                "PyAutoGUI failsafe: ย้ายเมาส์ไปมุมบนซ้ายเพื่อยกเลิก",
                QtWidgets.QMessageBox.StandardButton.Ok
                | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            != QtWidgets.QMessageBox.StandardButton.Ok
        ):
            return

        if not self._paint_countdown(seconds=3):
            return

        self._start_paint_worker(resume=False)

    def _on_erase(self) -> None:
        if self._canvas_rect is None:
            QtWidgets.QMessageBox.information(self, "ขาดหาย", "เลือกพื้นที่ Canvas ก่อน")
            return

        if self._cfg.eraser_tool_button_pos is None or self._cfg.eraser_thickness_up_button_pos is None:
            QtWidgets.QMessageBox.information(
                self,
                "ขาดการตั้งค่า",
                "จับภาพปุ่มเครื่องมือลบและปุ่มเพิ่มความหนาลบก่อน (แท็บการตั้งค่าสี)",
            )
            return

        if (
            QtWidgets.QMessageBox.warning(
                self,
                "กำลังจะลบ",
                "นี่จะควบคุมเมาส์และลบ Canvas ที่เลือกทั้งหมดในเกม\n"
                "ตรวจสอบให้แน่ใจว่าเกมถูกโฟกัสและ Canvas มองเห็นได้\n\n"
                "PyAutoGUI failsafe: ย้ายเมาส์ไปมุมบนซ้ายเพื่อยกเลิก",
                QtWidgets.QMessageBox.StandardButton.Ok
                | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            != QtWidgets.QMessageBox.StandardButton.Ok
        ):
            return

        if not self._paint_countdown(seconds=3):
            return

        # Erasing invalidates any paused paint session.
        self._reset_paint_session()
        self._start_erase_worker()

    def _on_erase_done(self) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._hide_status_overlay()
        self.statusBar().showMessage("ลบเสร็จสิ้น", 4000)

    def _on_erase_stopped(self, msg: str) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._hide_status_overlay()
        self.statusBar().showMessage(msg or "หยุดการลบ", 4000)

    def _on_erase_error(self, msg: str) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._hide_status_overlay()
        QtWidgets.QMessageBox.warning(self, "การลบล้มเหลว", f"การลบพบข้อผิดพลาด\n\nข้อผิดพลาด: {msg}")

    def _start_erase_worker(self) -> None:
        if self._canvas_rect is None:
            return

        self.btn_paint.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._stop_flag = False
        self._stop_reason = None

        # Capture the active (foreground) window as the game window for overlay anchoring.
        self._game_window_rect = self._capture_foreground_window_rect()
        self._start_esc_listener()

        if bool(getattr(self._cfg, "status_overlay_enabled", True)):
            try:
                ov = self._ensure_status_overlay()
                if self._game_window_rect is not None:
                    ov.set_anchor_rect(self._game_window_rect)
                if not ov.isVisible():
                    ov.start()
                ov.set_status("Starting erase…")
            except Exception:
                pass

        signals = WorkerSignals()
        qc = QtCore.Qt.ConnectionType.QueuedConnection
        signals.status.connect(self._on_worker_status, qc)
        signals.finished.connect(self._on_erase_done, qc)
        signals.error.connect(self._on_erase_error, qc)
        signals.stopped.connect(self._on_erase_stopped, qc)

        def work():
            try:
                opts = PainterOptions(
                    move_duration_s=self._cfg.move_duration_s,
                    mouse_down_s=self._cfg.mouse_down_s,
                    after_click_delay_s=self._cfg.after_click_delay_s,
                    panel_open_delay_s=self._cfg.panel_open_delay_s,
                    shade_select_delay_s=self._cfg.shade_select_delay_s,
                    row_delay_s=self._cfg.row_delay_s,
                    enable_drag_strokes=bool(getattr(self._cfg, "enable_drag_strokes", False)),
                    drag_step_duration_s=float(getattr(self._cfg, "drag_step_duration_s", 0.01)),
                    after_drag_delay_s=float(getattr(self._cfg, "after_drag_delay_s", 0.02)),
                )

                grid_w, grid_h = self._selected_preset_wh()

                def status_cb(msg: str) -> None:
                    try:
                        signals.status.emit(str(msg))
                    except Exception:
                        pass

                erase_canvas(
                    cfg=self._cfg,
                    canvas_rect=self._canvas_rect,
                    grid_w=int(grid_w),
                    grid_h=int(grid_h),
                    options=opts,
                    should_stop=lambda: self._stop_flag,
                    status_cb=status_cb,
                )

                if self._stop_flag:
                    signals.stopped.emit("Erase stopped")
                    return

                signals.finished.emit()
            except Exception as e:
                signals.error.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _paint_countdown(self, seconds: int = 3) -> bool:
        """Modal countdown before starting automation. Returns False if cancelled."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Starting")
        dlg.setModal(True)

        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel()
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        btn_cancel = QtWidgets.QPushButton("Cancel")
        v.addWidget(btn_cancel)

        remaining = {"n": max(0, int(seconds))}

        def update_text():
            n = remaining["n"]
            if n <= 0:
                lbl.setText("Starting now…")
            else:
                lbl.setText(
                    "Switch to the game window now.\n\n"
                    f"Starting in {n}…\n\n"
                    "Failsafe: move mouse to top-left to abort."
                )

        timer = QtCore.QTimer(dlg)

        def tick():
            remaining["n"] -= 1
            update_text()
            if remaining["n"] <= 0:
                timer.stop()
                dlg.accept()

        def cancel():
            timer.stop()
            dlg.reject()

        btn_cancel.clicked.connect(cancel)

        update_text()
        timer.timeout.connect(tick)
        timer.start(1000)

        return dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted

    def _on_stop(self):
        # Manual stop is a cancel.
        self._stop_reason = "stop"
        self._stop_flag = True
        self._stop_esc_listener()
    def _current_paint_session_sig(self) -> Optional[tuple]:
        if self._loaded is None or self._canvas_rect is None:
            return None
        return (
            self._loaded.path,
            self._loaded.grid.w,
            self._loaded.grid.h,
            tuple(self._canvas_rect),
            self._current_selection_key(),
            str(getattr(self._cfg, "paint_mode", "row")),
        )

    def _reset_paint_session(self) -> None:
        self._paint_total = 0
        self._paint_done.clear()
        self._paint_paused = False
        self._paint_session_sig = None
        self._paint_base_bucket_key = None
        self._paint_base_bucket_rgb = None
        self.btn_resume.setEnabled(False)

    def _on_progress(self, x: int, y: int, total: int):
        # Progress callbacks can arrive out of order (Paint-by-Color) and can
        # repeat due to verification repaints. Track unique completed cells.
        if self._loaded is None:
            return
        if total > 0:
            self._paint_total = int(total)
        key = (int(x), int(y))
        if key not in self._paint_done:
            self._paint_done.add(key)

        denom = max(1, int(self._paint_total) or int(total) or 1)
        pct = int((len(self._paint_done) / denom) * 100)
        self.progress.setValue(max(0, min(100, pct)))

        # Replica canvas progress (best-effort)
        if bool(getattr(self._cfg, "status_overlay_enabled", True)):
            try:
                ov = self._ensure_status_overlay()
                if self._game_window_rect is not None:
                    ov.set_anchor_rect(self._game_window_rect)
                if not ov.isVisible():
                    ov.start()
                ov.mark_painted(int(x), int(y))
            except Exception:
                pass

    def _on_paint_done(self):
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        self._stop_esc_listener()
        self._hide_status_overlay()
        self._reset_paint_session()

    def _on_paint_paused(self, msg: str) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(True)
        self.btn_erase.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._hide_status_overlay()
        self._paint_paused = True
        self.statusBar().showMessage(msg or "Paused", 4000)

    def _on_paint_stopped(self, msg: str) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._hide_status_overlay()
        self._reset_paint_session()
        self.statusBar().showMessage(msg or "Stopped", 4000)

    def _on_paint_error(self, msg: str):
        self.btn_paint.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_erase.setEnabled(True)
        self._stop_esc_listener()
        self._hide_status_overlay()

        # Keep the session state so the user can tweak settings and resume.
        self._paint_paused = True
        self.btn_resume.setEnabled(True)
        QtWidgets.QMessageBox.warning(
            self,
            "Paint paused",
            "Painting hit an error and has been paused.\n\n"
            "You can tweak timing/verification settings and press Resume to continue from the last completed step.\n\n"
            f"Error: {msg}",
        )

    def _start_paint_worker(self, resume: bool) -> None:
        if self._loaded is None or self._canvas_rect is None:
            return

        total = self._loaded.grid.w * self._loaded.grid.h
        if not resume:
            self._reset_paint_session()
            self._paint_total = total
            self._paint_session_sig = self._current_paint_session_sig()
        else:
            # Validate that the session hasn't changed.
            cur_sig = self._current_paint_session_sig()
            if self._paint_session_sig is None or cur_sig != self._paint_session_sig:
                QtWidgets.QMessageBox.information(
                    self,
                    "ไม่สามารถทำต่อได้",
                    "รูปภาพ/Canvas/การตั้งค่าเปลี่ยนแปลงตั้งแต่การรันครั้งล่าสุด\n\nกรุณาเริ่มการวาดใหม่แทน",
                )
                self._reset_paint_session()
                return

        self.btn_paint.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_erase.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._stop_flag = False
        self._stop_reason = None

        # Capture the active (foreground) window as the game window for overlay anchoring.
        # This runs after the countdown, so the user should have focused the game.
        self._game_window_rect = self._capture_foreground_window_rect()
        self._start_esc_listener()

        # Prepare the in-game status overlay (UI thread only).
        if bool(getattr(self._cfg, "status_overlay_enabled", True)):
            try:
                ov = self._ensure_status_overlay()
                if self._game_window_rect is not None:
                    ov.set_anchor_rect(self._game_window_rect)
                ov.set_grid(self._loaded.grid.w, self._loaded.grid.h, self._loaded.grid.pixels)
                if resume and self._paint_done:
                    for (xx, yy) in list(self._paint_done):
                        ov.mark_painted(int(xx), int(yy))
                if not ov.isVisible():
                    ov.start()
                ov.set_status("Starting…")
            except Exception:
                pass

        signals = WorkerSignals()
        qc = QtCore.Qt.ConnectionType.QueuedConnection
        signals.progress.connect(self._on_worker_progress, qc)
        signals.status.connect(self._on_worker_status, qc)
        signals.verify_cell.connect(self._on_worker_verify_cell, qc)
        signals.bucket_base.connect(self._on_worker_bucket_base, qc)
        signals.finished.connect(self._on_paint_done, qc)
        signals.error.connect(self._on_paint_error, qc)
        signals.paused.connect(self._on_paint_paused, qc)
        signals.stopped.connect(self._on_paint_stopped, qc)

        def work():
            try:
                opts = PainterOptions(
                    move_duration_s=self._cfg.move_duration_s,
                    mouse_down_s=self._cfg.mouse_down_s,
                    after_click_delay_s=self._cfg.after_click_delay_s,
                    panel_open_delay_s=self._cfg.panel_open_delay_s,
                    shade_select_delay_s=self._cfg.shade_select_delay_s,
                    row_delay_s=self._cfg.row_delay_s,
                    enable_drag_strokes=bool(getattr(self._cfg, "enable_drag_strokes", False)),
                    drag_step_duration_s=float(getattr(self._cfg, "drag_step_duration_s", 0.01)),
                    after_drag_delay_s=float(getattr(self._cfg, "after_drag_delay_s", 0.02)),
                )

                def get_pixel(x: int, y: int):
                    return self._loaded.grid.get(x, y)

                skip_fn = (lambda x, y: (int(x), int(y)) in self._paint_done) if resume else None

                def status_cb(msg: str) -> None:
                    try:
                        signals.status.emit(str(msg))
                    except Exception:
                        pass

                def verify_cb(pt: Optional[Tuple[int, int]]) -> None:
                    try:
                        if pt is None:
                            signals.verify_cell.emit(-1, -1)
                        else:
                            signals.verify_cell.emit(int(pt[0]), int(pt[1]))
                    except Exception:
                        pass

                def bucket_base_cb(main_name: str, sx: int, sy: int, r: int, g: int, b: int) -> None:
                    try:
                        signals.bucket_base.emit(str(main_name), int(sx), int(sy), int(r), int(g), int(b))
                    except Exception:
                        pass

                paint_grid(
                    cfg=self._cfg,
                    canvas_rect=self._canvas_rect,
                    grid_w=self._loaded.grid.w,
                    grid_h=self._loaded.grid.h,
                    get_pixel=get_pixel,
                    options=opts,
                    paint_mode=self._cfg.paint_mode,
                    skip=skip_fn,
                    allow_bucket_fill=(not resume),
                    allow_region_bucket_fill=(not resume) or (self._paint_base_bucket_key is not None),
                    resume_base_bucket_key=(
                        (self._paint_base_bucket_key[0], self._paint_base_bucket_key[1])
                        if resume and self._paint_base_bucket_key is not None
                        else None
                    ),
                    resume_base_bucket_rgb=(
                        self._paint_base_bucket_rgb if resume and self._paint_base_bucket_rgb is not None else None
                    ),
                    bucket_base_cb=bucket_base_cb,
                    progress_cb=lambda x, y: signals.progress.emit(x, y),
                    should_stop=lambda: self._stop_flag,
                    status_cb=status_cb,
                    verify_cb=verify_cb,
                )

                if self._stop_flag:
                    if self._stop_reason == "pause":
                        signals.paused.emit("Paused (ESC)")
                        return
                    if self._stop_reason == "stop":
                        signals.stopped.emit("Stopped")
                        return

                signals.finished.emit()
            except Exception as e:
                signals.error.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_resume(self) -> None:
        if not self._paint_paused:
            return
        if self._loaded is None or self._canvas_rect is None:
            return
        if not self._paint_done:
            return

        # Short countdown to refocus the game window.
        if not self._paint_countdown(seconds=2):
            return

        self._paint_paused = False
        self._start_paint_worker(resume=True)


def run():
    # Qt on Windows can emit a scary-but-harmless DPI awareness warning on some setups.
    # Suppress that specific category to keep console output clean.
    rules = os.environ.get("QT_LOGGING_RULES", "")
    if "qt.qpa.window=false" not in rules:
        os.environ["QT_LOGGING_RULES"] = (rules + (";" if rules else "") + "qt.qpa.window=false").strip(";")

    app = QtWidgets.QApplication([])
    w = MainWindow()
    w.resize(900, 650)
    w.show()
    app.exec()
