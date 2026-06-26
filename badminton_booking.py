#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MuMu/ADB automation for XJTU badminton court booking.

The script automates normal UI operations only. It intentionally does not
solve or bypass slider CAPTCHA challenges; after submit it waits for the user
to complete the CAPTCHA manually.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - checked at runtime
    raise SystemExit("缺少 Pillow。请先安装 Pillow 后再运行。") from exc


APP_PACKAGE_DEFAULT = "com.supwisdom.xjtu"
WORKSPACE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = WORKSPACE / "booking_config.json"
REMOTE_UI_XML = "/sdcard/window.xml"


class BookingError(RuntimeError):
    """Expected runtime failure with a user-readable message."""


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    adb_path: str
    width: int
    height: int
    vm_name: str | None = None
    vm_index: str | None = None


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def printable_text(value: str) -> str:
    return "".join(ch if (ch.isprintable() and not 0xE000 <= ord(ch) <= 0xF8FF) else "?" for ch in value)


def run_process(args: list[str], timeout: float = 15.0, check: bool = True) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise BookingError(f"找不到可执行文件：{args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BookingError(f"命令超时：{' '.join(args)}") from exc

    result = CommandResult(args=args, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    if check and result.returncode != 0:
        details = result.stderr_text.strip() or result.stdout_text.strip()
        raise BookingError(f"命令执行失败：{' '.join(args)}\n{details}")
    return result


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BookingError(f"配置文件不存在：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BookingError(f"配置文件不是合法 JSON：{path}\n{exc}") from exc


def save_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_nested(config: dict[str, Any], path: Iterable[str], default: Any = None) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def parse_time_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        raise BookingError(f"时间段格式错误：{value}，应为 HH:MM-HH:MM")
    h1, m1, h2, m2 = map(int, match.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def format_minutes(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def normalize_time_range(value: str) -> str:
    start, end = parse_time_range(value)
    return f"{format_minutes(start)}-{format_minutes(end)}"


def validate_config(config: dict[str, Any]) -> None:
    booking = config.get("booking")
    if not isinstance(booking, dict):
        raise BookingError("配置缺少 booking 对象。")

    priorities = booking.get("priorities")
    if not isinstance(priorities, list) or not priorities:
        raise BookingError("booking.priorities 必须是非空列表。")

    default_venue = booking.get("venue")
    for index, candidate in enumerate(priorities, start=1):
        if not isinstance(candidate, dict):
            raise BookingError(f"第 {index} 个候选场次不是对象。")
        venue = candidate.get("venue", default_venue)
        court = candidate.get("court")
        times = candidate.get("times")
        if not venue:
            raise BookingError(f"第 {index} 个候选场次缺少 venue。")
        if not isinstance(court, str) or not court:
            raise BookingError(f"第 {index} 个候选场次缺少 court。")
        if not isinstance(times, list) or not (1 <= len(times) <= 2):
            raise BookingError(f"第 {index} 个候选场次 times 必须包含 1 到 2 个时间段。")

        parsed = [parse_time_range(str(item)) for item in times]
        parsed_sorted = sorted(parsed)
        if parsed != parsed_sorted:
            raise BookingError(f"第 {index} 个候选场次的时间段必须按时间顺序填写。")
        for left, right in zip(parsed_sorted, parsed_sorted[1:]):
            if left[1] != right[0]:
                raise BookingError(f"第 {index} 个候选场次的两个时间段不连续：{times}")

    target_date = booking.get("target_date")
    if target_date:
        try:
            datetime.strptime(str(target_date), "%Y-%m-%d")
        except ValueError as exc:
            raise BookingError("booking.target_date 必须是 YYYY-MM-DD 格式。") from exc

    schedule_time = get_nested(config, ["run", "schedule_time"], "08:40:00")
    try:
        parse_clock_time(str(schedule_time))
    except ValueError as exc:
        raise BookingError("run.schedule_time 必须是 HH:MM 或 HH:MM:SS 格式。") from exc


def parse_clock_time(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(value)
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(value)
    return hour, minute, second


def resolve_path(value: str | None, fallback: str) -> str:
    raw = value or fallback
    return str(Path(raw).expanduser())


def find_adb_path(config: dict[str, Any]) -> str:
    configured = get_nested(config, ["mumu", "adb_path"])
    candidates = [
        configured,
        "D:/software-install/MuMuPlayer/nx_device/12.0/shell/adb.exe",
        "D:/software-install/MuMuPlayer/nx_main/adb.exe",
        "adb",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == "adb" or Path(str(candidate)).exists():
            return str(candidate)
    raise BookingError("找不到 adb.exe。请在 booking_config.json 里设置 mumu.adb_path。")


def parse_mumu_info(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BookingError(f"MuMuManager info 输出不是合法 JSON：\n{stdout[:1000]}") from exc


def discover_mumu_device(config: dict[str, Any], adb_path: str) -> tuple[str, str | None, str | None]:
    explicit_serial = get_nested(config, ["mumu", "device_serial"])
    if explicit_serial:
        return str(explicit_serial), None, None

    manager_path = resolve_path(
        get_nested(config, ["mumu", "manager_path"]),
        "D:/software-install/MuMuPlayer/nx_main/MuMuManager.exe",
    )
    preferred_name = get_nested(config, ["mumu", "preferred_vm_name"], "模拟手机-2")
    preferred_index = str(get_nested(config, ["mumu", "preferred_vm_index"], "2"))

    if Path(manager_path).exists():
        result = run_process([manager_path, "info", "--vmindex", "all"], timeout=10)
        players = parse_mumu_info(result.stdout_text)
        running = [
            item
            for item in players.values()
            if isinstance(item, dict)
            and item.get("is_android_started")
            and item.get("is_process_started")
            and item.get("adb_host_ip")
            and item.get("adb_port")
        ]
        if running:
            selected = None
            for item in running:
                if str(item.get("index")) == preferred_index or item.get("name") == preferred_name:
                    selected = item
                    break
            selected = selected or running[0]
            serial = f"{selected['adb_host_ip']}:{selected['adb_port']}"
            run_process([adb_path, "connect", serial], timeout=10, check=False)
            return serial, str(selected.get("name") or ""), str(selected.get("index") or "")

    devices = adb_devices(adb_path)
    if not devices:
        raise BookingError("没有发现可用 ADB 设备。请确认 MuMu 已启动，并开启/允许 ADB 调试。")
    return devices[0], None, None


def adb_devices(adb_path: str) -> list[str]:
    result = run_process([adb_path, "devices"], timeout=10)
    devices: list[str] = []
    for line in result.stdout_text.splitlines():
        if "\tdevice" in line:
            devices.append(line.split("\t", 1)[0].strip())
    return devices


def adb(device: DeviceInfo, args: list[str], timeout: float = 15.0, check: bool = True) -> CommandResult:
    return run_process([device.adb_path, "-s", device.serial, *args], timeout=timeout, check=check)


def get_screen_size(adb_path: str, serial: str) -> tuple[int, int]:
    result = run_process([adb_path, "-s", serial, "shell", "wm", "size"], timeout=10)
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", result.stdout_text)
    if not match:
        raise BookingError(f"无法读取设备分辨率：\n{result.stdout_text}")
    return int(match.group(1)), int(match.group(2))


def connect_device(config: dict[str, Any]) -> DeviceInfo:
    adb_path = find_adb_path(config)
    serial, vm_name, vm_index = discover_mumu_device(config, adb_path)
    width, height = get_screen_size(adb_path, serial)
    return DeviceInfo(serial=serial, adb_path=adb_path, width=width, height=height, vm_name=vm_name, vm_index=vm_index)


def current_focus(device: DeviceInfo) -> str:
    result = adb(device, ["shell", "dumpsys", "window"], timeout=15, check=False)
    text = result.stdout_text
    match = re.search(r"mCurrentFocus=Window\{[^}]*\s+([^/\s}]+)/([^}\s]+)", text)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    match = re.search(r"mFocusedApp=.*?\s+([^/\s}]+)/([^}\s]+)", text)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return ""


def assert_app_foreground(device: DeviceInfo, config: dict[str, Any], strict: bool = True) -> None:
    package = get_nested(config, ["app", "package"], APP_PACKAGE_DEFAULT)
    focus = current_focus(device)
    if package not in focus:
        message = (
            f"当前前台不是移动交通大学 App（期望包名 {package}，当前 {focus or '未知'}）。\n"
            "请先在 MuMu 里打开 App，并进入“体育场馆预订服务”的关注场地首页。"
        )
        if strict:
            raise BookingError(message)
        log(message)


def screenshot_bytes(device: DeviceInfo) -> bytes:
    result = adb(device, ["exec-out", "screencap", "-p"], timeout=15)
    data = result.stdout
    if not data.startswith(b"\x89PNG"):
        raise BookingError("ADB 截图结果不是 PNG，可能设备连接异常。")
    return data


def screenshot_image(device: DeviceInfo) -> Image.Image:
    return Image.open(io.BytesIO(screenshot_bytes(device))).convert("RGB")


def dump_ui_xml(device: DeviceInfo) -> str:
    adb(device, ["shell", "uiautomator", "dump", REMOTE_UI_XML], timeout=15, check=False)
    result = adb(device, ["exec-out", "cat", REMOTE_UI_XML], timeout=15, check=False)
    return result.stdout_text


def ui_texts(device: DeviceInfo) -> list[str]:
    xml = dump_ui_xml(device)
    if not xml.strip().startswith("<?xml"):
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    values: list[str] = []
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            value = printable_text(node.attrib.get(attr, ""))
            if value:
                values.append(value)
    return values


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return None
    return tuple(map(int, match.groups()))  # type: ignore[return-value]


def find_text_bounds(device: DeviceInfo, target_text: str) -> tuple[int, int, int, int] | None:
    xml = dump_ui_xml(device)
    if not xml.strip().startswith("<?xml"):
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    exact: list[tuple[int, int, int, int]] = []
    partial: list[tuple[int, int, int, int]] = []
    for node in root.iter("node"):
        text = printable_text(node.attrib.get("text", ""))
        desc = printable_text(node.attrib.get("content-desc", ""))
        bounds = parse_bounds(node.attrib.get("bounds", ""))
        if not bounds:
            continue
        if text == target_text or desc == target_text:
            exact.append(bounds)
        elif target_text in text or target_text in desc:
            partial.append(bounds)
    if exact:
        return max(exact, key=lambda item: (item[2] - item[0]) * (item[3] - item[1]))
    if partial:
        return max(partial, key=lambda item: (item[2] - item[0]) * (item[3] - item[1]))
    return None


def find_text_center(device: DeviceInfo, target_text: str) -> tuple[int, int] | None:
    bounds = find_text_bounds(device, target_text)
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def tap_text(device: DeviceInfo, target_text: str, label: str | None = None) -> bool:
    center = find_text_center(device, target_text)
    if not center:
        return False
    x, y = center
    tap(device, x, y, label or target_text)
    return True


def scaled_point(config: dict[str, Any], device: DeviceInfo, point: dict[str, Any]) -> tuple[int, int]:
    layout = config["layout"]
    base_width = int(layout.get("base_width", device.width))
    base_height = int(layout.get("base_height", device.height))
    x = float(point["x"]) * device.width / base_width
    y = float(point["y"]) * device.height / base_height
    return round(x), round(y)


def sample_rgb(image: Image.Image, x: int, y: int, radius: int = 4) -> tuple[int, int, int]:
    width, height = image.size
    left = max(0, x - radius)
    right = min(width - 1, x + radius)
    top = max(0, y - radius)
    bottom = min(height - 1, y + radius)
    total = [0, 0, 0]
    count = 0
    for py in range(top, bottom + 1):
        for px in range(left, right + 1):
            r, g, b = image.getpixel((px, py))
            total[0] += r
            total[1] += g
            total[2] += b
            count += 1
    return total[0] // count, total[1] // count, total[2] // count


def color_matches(rgb: tuple[int, int, int], rule: dict[str, Any]) -> bool:
    r, g, b = rgb
    channels = {"r": r, "g": g, "b": b}
    for key, value in rule.items():
        if key.endswith("_min"):
            channel = key[0]
            if channels[channel] < int(value):
                return False
        elif key.endswith("_max"):
            channel = key[0]
            if channels[channel] > int(value):
                return False
    return True


def classify_cell(image: Image.Image, config: dict[str, Any], x: int, y: int) -> str:
    colors = config.get("colors", {})
    rgb = sample_rgb(image, x, y, radius=int(colors.get("sample_radius", 5)))
    if color_matches(rgb, colors.get("selected_orange", {})):
        return "selected"
    if color_matches(rgb, colors.get("available_green", {})):
        return "available"
    if color_matches(rgb, colors.get("disabled_gray", {})):
        return "disabled"
    return "unknown"


def is_grayish(rgb: tuple[int, int, int], low: int = 80, high: int = 190, tolerance: int = 35) -> bool:
    r, g, b = rgb
    return low <= r <= high and low <= g <= high and low <= b <= high and max(rgb) - min(rgb) <= tolerance


def tap(device: DeviceInfo, x: int, y: int, label: str = "") -> None:
    suffix = f"：{label}" if label else ""
    log(f"点击 {x},{y}{suffix}")
    adb(device, ["shell", "input", "tap", str(x), str(y)], timeout=10)


def wait_seconds(seconds: float, reason: str = "") -> None:
    if reason:
        log(reason)
    time.sleep(seconds)


def get_venue_point(config: dict[str, Any], venue: str) -> dict[str, Any]:
    venues = get_nested(config, ["layout", "venues"], {})
    if venue not in venues:
        raise BookingError(f"layout.venues 中没有场馆坐标：{venue}")
    return venues[venue]


def time_to_y(config: dict[str, Any], time_range: str) -> dict[str, Any]:
    time_centers = get_nested(config, ["layout", "table", "time_centers"], {})
    if time_range not in time_centers:
        raise BookingError(f"layout.table.time_centers 中没有时间段坐标：{time_range}")
    return {"x": 0, "y": time_centers[time_range]}


def court_to_x(config: dict[str, Any], court: str) -> dict[str, Any]:
    court_centers = get_nested(config, ["layout", "table", "court_centers"], {})
    if court not in court_centers:
        raise BookingError(f"layout.table.court_centers 中没有场地坐标：{court}")
    return {"x": court_centers[court], "y": 0}


def cell_point(
    config: dict[str, Any],
    device: DeviceInfo,
    court: str,
    time_range: str,
    prefer_ui_court_center: bool = False,
) -> tuple[int, int]:
    court_center = find_text_center(device, court) if prefer_ui_court_center else None
    x_raw = court_center[0] if court_center else court_to_x(config, court)["x"]
    y_raw = time_to_y(config, time_range)["y"]
    return scaled_point(config, device, {"x": x_raw, "y": y_raw})


def detect_page(device: DeviceInfo, config: dict[str, Any]) -> str:
    image = screenshot_image(device)
    table = get_nested(config, ["layout", "table"], {})
    courts = table.get("court_centers", {})
    times = table.get("time_centers", {})
    grid_hits = 0
    time_label_hits = 0
    for court in list(courts.keys())[:3]:
        for time_range in list(times.keys())[:4]:
            x, y = cell_point(config, device, court, time_range)
            if classify_cell(image, config, x, y) in {"available", "selected", "disabled"}:
                grid_hits += 1
    time_label_x = int(get_nested(config, ["layout", "table", "time_label_x"], 48))
    base_width = int(get_nested(config, ["layout", "base_width"], device.width))
    time_label_x = round(time_label_x * device.width / base_width)
    for time_range in list(times.keys())[:4]:
        _, y = cell_point(config, device, "场地1", time_range)
        if is_grayish(sample_rgb(image, time_label_x, y, radius=6), low=70, high=170, tolerance=45):
            time_label_hits += 1
    if (
        grid_hits >= int(get_nested(config, ["workflow", "grid_detection_min_hits"], 3))
        and time_label_hits >= int(get_nested(config, ["workflow", "time_label_detection_min_hits"], 2))
    ):
        return "booking_grid"

    notice_button = get_nested(config, ["layout", "notice_order_button"], {})
    if notice_button:
        x, y = scaled_point(config, device, notice_button)
        rgb = sample_rgb(image, x, y, radius=8)
        if color_matches(rgb, get_nested(config, ["colors", "button_blue"], {})):
            return "notice"

    texts = "".join(ui_texts(device))
    if "立即预订" in texts or "使用须知" in texts:
        return "notice"
    if any(keyword in texts for keyword in ("关注场地", "创新港", "羽毛球场")):
        return "service_home"
    return "unknown"


def open_booking_grid(device: DeviceInfo, config: dict[str, Any]) -> None:
    booking = config["booking"]
    venue = booking.get("venue")
    page = detect_page(device, config)
    log(f"当前页面识别结果：{page}")

    if page == "booking_grid":
        return
    if page == "notice":
        point = get_nested(config, ["layout", "notice_order_button"])
        x, y = scaled_point(config, device, point)
        tap(device, x, y, "立即预订")
        wait_seconds(1.5, "等待进入场次选择页")
        return

    if page == "unknown" and get_nested(config, ["workflow", "assume_unknown_page_is_service_home"], True):
        log("未能可靠识别首页；按配置假定当前在关注场地首页。")
    elif page != "service_home":
        raise BookingError("无法确认当前页面。请手动打开“体育场馆预订服务”的关注场地首页后重试。")

    if not tap_text(device, venue, venue):
        log(f"未能从页面文字定位场馆，改用备用坐标：{venue}")
        point = get_venue_point(config, venue)
        x, y = scaled_point(config, device, point)
        tap(device, x, y, venue)
    wait_seconds(1.5, "等待场馆页面打开")

    page = detect_page(device, config)
    log(f"点击场馆后的页面识别结果：{page}")
    if page == "notice":
        point = get_nested(config, ["layout", "notice_order_button"])
        x, y = scaled_point(config, device, point)
        tap(device, x, y, "立即预订")
        wait_seconds(1.5, "等待进入场次选择页")
    elif page != "booking_grid":
        raise BookingError("点击场馆后没有进入使用须知或场次页，请检查坐标并考虑运行 calibrate。")


def target_date_offset(config: dict[str, Any]) -> int:
    booking = config["booking"]
    if booking.get("target_date"):
        target = datetime.strptime(str(booking["target_date"]), "%Y-%m-%d").date()
        return (target - datetime.now().date()).days
    return int(booking.get("date_offset_days", 0))


def choose_date(device: DeviceInfo, config: dict[str, Any]) -> None:
    offset = target_date_offset(config)
    target = datetime.now().date() + timedelta(days=offset)
    target_text = target.strftime("%Y-%m-%d")
    if tap_text(device, target_text, f"选择日期 {target_text}"):
        wait_seconds(0.8, "等待日期切换")
        return

    date_cells = get_nested(config, ["layout", "date_cells"], {})
    if str(offset) not in date_cells:
        if offset == 0:
            log("使用当前已选日期。")
            return
        raise BookingError(f"配置中没有 date_offset_days={offset} 的日期坐标。请修改配置或运行 calibrate。")
    point = date_cells[str(offset)]
    x, y = scaled_point(config, device, point)
    tap(device, x, y, f"选择日期 offset={offset}")
    wait_seconds(0.8, "等待日期切换")


def clear_known_selections(device: DeviceInfo, config: dict[str, Any]) -> None:
    if not bool(get_nested(config, ["workflow", "clear_existing_selection"], False)):
        return
    image = screenshot_image(device)
    table = get_nested(config, ["layout", "table"], {})
    selected_points: list[tuple[int, int]] = []
    for court in table.get("court_centers", {}).keys():
        for time_range in table.get("time_centers", {}).keys():
            x, y = cell_point(config, device, court, time_range)
            if classify_cell(image, config, x, y) == "selected":
                selected_points.append((x, y))
    for x, y in selected_points:
        tap(device, x, y, "清除已有选择")
        time.sleep(0.15)
    if selected_points:
        wait_seconds(0.5, "已清除页面上的已选场次")


def clear_candidate_points(device: DeviceInfo, config: dict[str, Any], points: list[tuple[int, int]]) -> None:
    image = screenshot_image(device)
    for x, y in points:
        if classify_cell(image, config, x, y) == "selected":
            tap(device, x, y, "撤销本次候选选择")
            time.sleep(0.15)


def select_candidate(device: DeviceInfo, config: dict[str, Any], candidate: dict[str, Any]) -> bool:
    court = candidate["court"]
    times = [normalize_time_range(str(item)) for item in candidate["times"]]
    require_available = bool(get_nested(config, ["workflow", "require_available_color_before_click"], True))

    clear_known_selections(device, config)
    image = screenshot_image(device)
    states: list[str] = []
    points: list[tuple[int, int]] = []
    try:
        for time_range in times:
            x, y = cell_point(config, device, court, time_range, prefer_ui_court_center=True)
            state = classify_cell(image, config, x, y)
            states.append(state)
            points.append((x, y))
    except BookingError as exc:
        log(f"候选无法定位：{candidate.get('name', '') or court}，{exc}")
        return False

    if require_available and any(state != "available" for state in states):
        if all(state == "selected" for state in states) and order_button_active(device, config):
            log(f"候选已经处于选中状态：{candidate.get('name', '') or court} {times}")
            return True
        log(f"候选不可用：{candidate.get('name', '') or court} {times}，颜色状态={states}")
        return False

    for (x, y), time_range, state in zip(points, times, states):
        tap(device, x, y, f"{court} {time_range}")
        time.sleep(0.25)

    wait_seconds(0.6, "验证场次选择状态")
    verify_image = screenshot_image(device)
    selected_count = 0
    for x, y in points:
        if classify_cell(verify_image, config, x, y) == "selected":
            selected_count += 1

    if selected_count != len(points):
        log(f"候选点击后未全部选中：已选 {selected_count}/{len(points)}")
        clear_candidate_points(device, config, points)
        return False

    if not order_button_active(device, config):
        log("候选点击后底部按钮没有变为“我要下单”，继续尝试下一个候选。")
        clear_candidate_points(device, config, points)
        return False
    log(f"已选中候选：{candidate.get('name', '') or court} {times}")
    return True


def select_best_slot(device: DeviceInfo, config: dict[str, Any]) -> dict[str, Any]:
    booking = config["booking"]
    default_venue = booking.get("venue")
    for candidate in booking["priorities"]:
        if candidate.get("venue") and candidate.get("venue") != default_venue:
            log(f"忽略候选里的旧 venue={candidate.get('venue')}，按目标场馆 {default_venue} 尝试：{candidate.get('name', '')}")
        if select_candidate(device, config, candidate):
            return candidate
    raise BookingError("所有候选场次都不可用或未能成功选中。")


def order_button_active(device: DeviceInfo, config: dict[str, Any]) -> bool:
    point = get_nested(config, ["layout", "order_button"], {})
    if not point:
        return False
    image = screenshot_image(device)
    x, y = scaled_point(config, device, point)
    rgb = sample_rgb(image, x, y, radius=8)
    return color_matches(rgb, get_nested(config, ["colors", "button_orange"], {}))


def notify_user_for_captcha() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass
    log("如果出现滑块验证码，请现在在 MuMu 里手动拖动滑块。脚本只等待结果，不破解验证码。")


def wait_after_submit(device: DeviceInfo, config: dict[str, Any]) -> None:
    timeout = float(get_nested(config, ["run", "wait_after_submit_seconds"], 180))
    interval = float(get_nested(config, ["run", "poll_interval_seconds"], 2))
    deadline = time.time() + timeout
    success_keywords = tuple(get_nested(config, ["workflow", "success_keywords"], ["成功", "预约成功", "预订成功", "订单"]))
    while time.time() < deadline:
        texts = "".join(ui_texts(device))
        if texts and any(keyword in texts for keyword in success_keywords):
            log(f"检测到可能的成功页面文字：{texts[:120]}")
            return
        time.sleep(interval)
    log("等待超时。请查看 MuMu 中的实际页面确认是否预订成功。")


def submit_order(device: DeviceInfo, config: dict[str, Any], execute: bool) -> None:
    if not order_button_active(device, config):
        raise BookingError("未检测到可点击的“我要下单”状态，请检查场次是否已选中。")

    point = get_nested(config, ["layout", "order_button"])
    x, y = scaled_point(config, device, point)
    if not execute:
        log("dry-run：已到达可下单状态，不会点击“我要下单”。")
        return

    tap(device, x, y, "我要下单")
    wait_seconds(1.0, "等待验证码或结果页面")
    notify_user_for_captcha()
    wait_after_submit(device, config)


def run_once(config: dict[str, Any], execute: bool) -> None:
    validate_config(config)
    device = connect_device(config)
    log(f"已连接设备：{device.serial}，分辨率 {device.width}x{device.height}")
    log(f"目标场馆：{config['booking'].get('venue')}")
    assert_app_foreground(device, config, strict=True)
    open_booking_grid(device, config)
    if detect_page(device, config) != "booking_grid":
        raise BookingError("没有进入场次选择页，请检查 App 当前页面或坐标配置。")
    choose_date(device, config)
    selected = select_best_slot(device, config)
    submit_order(device, config, execute=execute)
    log(f"流程结束。候选：{selected.get('name', selected.get('court'))}")


def run_check(config: dict[str, Any]) -> None:
    validate_config(config)
    device = connect_device(config)
    log(f"Python：{sys.version.split()[0]}")
    log(f"目标场馆：{config['booking'].get('venue')}")
    log(f"ADB：{device.adb_path}")
    log(f"设备：{device.serial}，MuMu={device.vm_name or '-'}，index={device.vm_index or '-'}")
    log(f"分辨率：{device.width}x{device.height}")
    focus = current_focus(device)
    log(f"前台窗口：{focus or '未知'}")
    package = get_nested(config, ["app", "package"], APP_PACKAGE_DEFAULT)
    if package not in focus:
        log(f"提示：当前前台不是 {package}，运行预订前请先打开学校 App 的预订服务首页。")
    page = detect_page(device, config)
    log(f"页面识别：{page}")
    texts = ui_texts(device)
    if texts:
        log("可读取文字：" + " | ".join(texts[:12]))
    else:
        log("当前页面文字读取较少，这是 H5/WebView 页面常见情况。")


def next_schedule_time(clock: str) -> datetime:
    hour, minute, second = parse_clock_time(clock)
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_schedule(config: dict[str, Any], execute: bool, override_time: str | None = None) -> None:
    schedule_time = override_time or str(get_nested(config, ["run", "schedule_time"], "08:40:00"))
    target = next_schedule_time(schedule_time)
    seconds = (target - datetime.now()).total_seconds()
    log(f"定时执行时间：{target.strftime('%Y-%m-%d %H:%M:%S')}，等待 {math.ceil(seconds)} 秒")
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 30))
    run_once(config, execute=execute)


def prompt_point(name: str, current: dict[str, Any] | None) -> dict[str, Any] | None:
    current_text = f" 当前={current.get('x')},{current.get('y')}" if current else ""
    raw = input(f"{name} 坐标 x,y（留空保持{current_text}）：").strip()
    if not raw:
        return current
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", raw)
    if not match:
        raise BookingError("坐标格式应为 x,y，例如 450,1540")
    return {"x": int(match.group(1)), "y": int(match.group(2))}


def run_calibrate(config_path: Path, config: dict[str, Any]) -> None:
    device = connect_device(config)
    data = screenshot_bytes(device)
    out_path = WORKSPACE / "calibration_screen.png"
    out_path.write_bytes(data)
    log(f"已保存当前截图：{out_path}")
    log(f"设备分辨率：{device.width}x{device.height}。请按截图上的设备坐标填写。")

    layout = config.setdefault("layout", {})
    booking = config.setdefault("booking", {})
    venue = booking.get("venue", "创新港一号巨构羽毛球场")
    venues = layout.setdefault("venues", {})
    venues[venue] = prompt_point(f"场馆入口：{venue}", venues.get(venue)) or venues.get(venue)
    layout["notice_order_button"] = prompt_point("使用须知页“立即预订”按钮", layout.get("notice_order_button")) or layout.get("notice_order_button")
    layout["order_button"] = prompt_point("场次页底部“我要下单”按钮", layout.get("order_button")) or layout.get("order_button")

    date_cells = layout.setdefault("date_cells", {})
    for key in ("0", "1", "2"):
        date_cells[key] = prompt_point(f"日期 offset={key}", date_cells.get(key)) or date_cells.get(key)

    table = layout.setdefault("table", {})
    court_centers = table.setdefault("court_centers", {})
    for court in ("场地1", "场地2", "场地3"):
        current = {"x": court_centers.get(court, 0), "y": 0} if court in court_centers else None
        point = prompt_point(f"{court} 任意单元格中心 x 坐标", current)
        if point:
            court_centers[court] = point["x"]

    time_centers = table.setdefault("time_centers", {})
    for time_range in list(time_centers.keys()):
        current = {"x": 0, "y": time_centers[time_range]}
        point = prompt_point(f"{time_range} 行中心 y 坐标", current)
        if point:
            time_centers[time_range] = point["y"]

    save_config(config_path, config)
    log(f"已更新配置：{config_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="移动交通大学羽毛球场自动预订脚本")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="检查环境和当前页面")

    run_parser = subparsers.add_parser("run", help="执行预订流程")
    run_parser.add_argument("--mode", choices=("once", "schedule"), default="once", help="执行模式")
    run_parser.add_argument("--execute", action="store_true", help="正式点击“我要下单”")
    run_parser.add_argument("--time", help="覆盖配置中的定时时间，格式 HH:MM 或 HH:MM:SS")

    subparsers.add_parser("calibrate", help="保存截图并交互式校准关键坐标")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        log("未提供命令，默认执行 dry-run：run --mode once。不会点击“我要下单”。")
        argv = ["run", "--mode", "once"]
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    try:
        config = load_config(config_path)
        if args.command == "check":
            run_check(config)
        elif args.command == "calibrate":
            run_calibrate(config_path, config)
        elif args.command == "run":
            config_execute = bool(get_nested(config, ["run", "execute"], False))
            execute = bool(args.execute or config_execute)
            if args.mode == "once":
                run_once(config, execute=execute)
            else:
                run_schedule(config, execute=execute, override_time=args.time)
        else:  # pragma: no cover
            parser.error(f"未知命令：{args.command}")
        return 0
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except BookingError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
