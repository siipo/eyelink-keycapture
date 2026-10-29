"""EyeLink Key Capture.

Records EyeLink data while the participant works in another program (e.g. a
DICOM viewer). On each response key press it writes a KEYPRESS message to the
EDF, saves a native-resolution screenshot of the tracked screen and adds a
Data Viewer IMGLOAD message, so gaze can be overlaid on exactly what was on
screen. The case key starts a new trial (one case per trial).
"""

import configparser
import csv
import ctypes
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

# Must happen before pygame, mss or pylink touch the screen, otherwise Windows
# display scaling makes coordinates and screenshots disagree.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")

import mss
import mss.tools
import pygame
import pylink
from pynput import keyboard

APP_NAME = "EyeLink Key Capture"
QUIT_CONFIRM_SECONDS = 3.0


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- logging

class Log:
    def __init__(self):
        self.file = None

    def open(self, path):
        self.file = open(path, "a", encoding="utf-8")

    def __call__(self, text):
        line = f"{datetime.now():%H:%M:%S}  {text}"
        print(line, flush=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()


log = Log()


# ---------------------------------------------------------------- config

def parse_rgb(text):
    parts = [int(p) for p in text.split(",")]
    if len(parts) != 3:
        raise ValueError(f"colour must be R,G,B: {text!r}")
    return tuple(parts)


def load_config():
    path = os.path.join(app_dir(), "config.ini")
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if not cfg.read(path, encoding="utf-8-sig"):  # -sig: Notepad may add a BOM
        raise SystemExit(f"config.ini not found next to the program: {path}")
    out_folder = cfg.get("output", "folder", fallback="data")
    if not os.path.isabs(out_folder):
        out_folder = os.path.join(app_dir(), out_folder)
    return {
        "host_ip": cfg.get("tracker", "host_ip", fallback="100.1.1.1"),
        "dummy": cfg.getboolean("tracker", "dummy_mode", fallback=False),
        "cal_type": cfg.get("tracker", "calibration_type", fallback="HV9"),
        "monitor": cfg.get("display", "monitor", fallback="auto").strip().lower(),
        "width": cfg.getint("display", "width", fallback=4200),
        "height": cfg.getint("display", "height", fallback=2800),
        "cal_bg": parse_rgb(cfg.get("display", "cal_background", fallback="40,40,40")),
        "cal_fg": parse_rgb(cfg.get("display", "cal_foreground", fallback="255,255,255")),
        "response_key": cfg.get("keys", "response_key", fallback="1"),
        "case_key": cfg.get("keys", "case_key", fallback="2"),
        "recal_key": cfg.get("keys", "recalibrate_key", fallback="f9"),
        "quit_key": cfg.get("keys", "quit_key", fallback="f10"),
        "out_folder": out_folder,
        "png_level": cfg.getint("output", "png_compression", fallback=1),
    }


def pick_monitor(cfg):
    """Return the mss monitor dict for the tracked screen."""
    with mss.MSS() as sct:
        monitors = sct.monitors[1:]  # [0] is the virtual all-screens box
    for i, m in enumerate(monitors, 1):
        log(f"Monitor {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})"
            f"{'  primary' if m.get('is_primary') else ''}")
    if cfg["monitor"] == "auto":
        for m in monitors:
            if m["width"] == cfg["width"] and m["height"] == cfg["height"]:
                return m
        raise SystemExit(
            f"No monitor is {cfg['width']}x{cfg['height']}. Check that the screen is "
            "connected and Windows uses its native resolution, or set "
            "[display] monitor to a number in config.ini.")
    index = int(cfg["monitor"])
    if not 1 <= index <= len(monitors):
        raise SystemExit(f"[display] monitor = {index}, but only {len(monitors)} monitor(s) found.")
    m = monitors[index - 1]
    if (m["width"], m["height"]) != (cfg["width"], cfg["height"]):
        log(f"WARNING: monitor {index} is {m['width']}x{m['height']}, config says "
            f"{cfg['width']}x{cfg['height']}. Using the monitor's real size.")
    return m


# ---------------------------------------------------------------- keys

class KeySpec:
    """A key from config.ini, matched against pynput events."""

    def __init__(self, name):
        self.name = name.strip()
        self.char = None
        self.vks = set()
        self.special = None
        if len(self.name) == 1:
            self.char = self.name.lower()
            vk = ctypes.windll.user32.VkKeyScanW(self.name) & 0xFF
            if vk != 0xFF:
                self.vks.add(vk)
            if self.name.isdigit():
                self.vks.add(0x60 + int(self.name))  # numpad digit
        else:
            try:
                self.special = getattr(keyboard.Key, self.name.lower())
            except AttributeError:
                raise SystemExit(f"Unknown key name in config.ini: {name!r}")

    def matches(self, key):
        if self.special is not None:
            return key == self.special
        if isinstance(key, keyboard.KeyCode):
            if key.char is not None and key.char.lower() == self.char:
                return True
            return key.vk in self.vks
        return False


class KeyListener:
    """Global key hook. Puts (action, perf_counter time) on a queue."""

    def __init__(self, actions, events):
        self.actions = actions  # list of (action name, KeySpec)
        self.events = events
        self.down = set()
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)

    @staticmethod
    def _id(key):
        return key if not isinstance(key, keyboard.KeyCode) else (key.vk, key.char)

    def on_press(self, key):
        t = time.perf_counter()
        kid = self._id(key)
        if kid in self.down:
            return  # auto-repeat while held
        self.down.add(kid)
        for action, spec in self.actions:
            if spec.matches(key):
                self.events.put((action, t))
                break

    def on_release(self, key):
        self.down.discard(self._id(key))

    def start(self):
        self.listener.start()

    def stop(self):
        self.listener.stop()


# ---------------------------------------------------------------- screenshots

class ScreenshotWriter:
    """Grabs on one thread and encodes PNGs on another, so a slow PNG never
    delays the grab for the next keypress."""

    def __init__(self, monitor, png_level):
        self.monitor = {k: monitor[k] for k in ("left", "top", "width", "height")}
        self.png_level = png_level
        self.grab_jobs = queue.Queue()
        self.encode_jobs = queue.Queue()
        self.grabber = threading.Thread(target=self._grab_loop, daemon=True)
        self.encoder = threading.Thread(target=self._encode_loop, daemon=True)

    def start(self):
        self.grabber.start()
        self.encoder.start()

    def capture(self, path):
        self.grab_jobs.put((path, time.perf_counter()))

    def _grab_loop(self):
        with mss.MSS() as sct:
            while True:
                job = self.grab_jobs.get()
                if job is None:
                    self.encode_jobs.put(None)
                    return
                path, t_req = job
                try:
                    shot = sct.grab(self.monitor)
                    delay_ms = (time.perf_counter() - t_req) * 1000
                    self.encode_jobs.put((path, shot, delay_ms))
                except Exception as e:
                    log(f"    ERROR grabbing screenshot {path}: {e}")

    def _encode_loop(self):
        while True:
            job = self.encode_jobs.get()
            if job is None:
                return
            path, shot, delay_ms = job
            try:
                mss.tools.to_png(shot.rgb, shot.size, level=self.png_level, output=path)
                log(f"    saved {os.path.basename(path)} (captured {delay_ms:.0f} ms after request)")
            except Exception as e:
                log(f"    ERROR saving screenshot {path}: {e}")

    def finish(self):
        if self.grabber.is_alive():
            self.grab_jobs.put(None)
            self.grabber.join()
            self.encoder.join()


# ---------------------------------------------------------------- calibration graphics

class PygameCalDisplay(pylink.EyeLinkCustomDisplay):
    """Calibration, validation and camera image on the tracked monitor."""

    def __init__(self, tracker, monitor, bg, fg):
        super().__init__()
        self.tracker = tracker
        self.monitor = monitor
        self.bg = bg
        self.fg = fg
        self.size = (monitor["width"], monitor["height"])
        self.screen = None
        self.font = None
        self.palette = []
        self.img_lines = []
        self.img_size = (0, 0)
        self.img_title = ""
        self.image = None

    def open_window(self):
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{self.monitor['left']},{self.monitor['top']}"
        pygame.display.init()
        pygame.font.init()
        self.screen = pygame.display.set_mode(self.size, pygame.NOFRAME)
        pygame.display.set_caption(f"{APP_NAME} - calibration")
        pygame.mouse.set_visible(False)
        self._bring_to_front()
        self.font = pygame.font.SysFont("Arial", max(24, self.size[1] // 60))
        self.screen.fill(self.bg)
        pygame.display.flip()

    def _bring_to_front(self):
        # Topmost so the viewer cannot cover the targets; foreground so the
        # Display PC keyboard (Enter, C, V, Esc) reaches the calibration.
        user32 = ctypes.windll.user32
        hwnd = pygame.display.get_wm_info()["window"]
        HWND_TOPMOST = ctypes.c_void_p(-1)
        SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x2, 0x1, 0x40
        user32.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        # Windows only lets the foreground process hand over focus; briefly
        # attaching to the foreground thread's input works around that.
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        own_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(own_thread, fg_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.AttachThreadInput(own_thread, fg_thread, False)

    def close_window(self):
        pygame.display.quit()
        self.screen = None

    # pylink callbacks
    def setup_cal_display(self):
        self.clear_cal_display()

    def exit_cal_display(self):
        self.clear_cal_display()

    def record_abort_hide(self):
        pass

    def clear_cal_display(self):
        if self.screen is None:
            return
        self.screen.fill(self.bg)
        pygame.display.flip()

    def erase_cal_target(self):
        self.clear_cal_display()

    def draw_cal_target(self, x, y):
        outer = max(10, self.size[1] // 70)
        inner = max(3, outer // 4)
        self.screen.fill(self.bg)
        pygame.draw.circle(self.screen, self.fg, (int(x), int(y)), outer)
        pygame.draw.circle(self.screen, self.bg, (int(x), int(y)), inner)
        pygame.display.flip()

    def play_beep(self, beepid):
        pass

    def get_input_key(self):
        keys = []
        if self.screen is None:
            return keys  # pylink polls keys while recording; the window is closed then
        for ev in pygame.event.get():
            if ev.type != pygame.KEYDOWN:
                continue
            k = ev.key
            mapping = {
                pygame.K_RETURN: pylink.ENTER_KEY, pygame.K_KP_ENTER: pylink.ENTER_KEY,
                pygame.K_ESCAPE: pylink.ESC_KEY, pygame.K_UP: pylink.CURS_UP,
                pygame.K_DOWN: pylink.CURS_DOWN, pygame.K_LEFT: pylink.CURS_LEFT,
                pygame.K_RIGHT: pylink.CURS_RIGHT, pygame.K_PAGEUP: pylink.PAGE_UP,
                pygame.K_PAGEDOWN: pylink.PAGE_DOWN,
                pygame.K_F1: pylink.F1_KEY, pygame.K_F2: pylink.F2_KEY,
                pygame.K_F3: pylink.F3_KEY, pygame.K_F4: pylink.F4_KEY,
                pygame.K_F5: pylink.F5_KEY, pygame.K_F6: pylink.F6_KEY,
                pygame.K_F7: pylink.F7_KEY, pygame.K_F8: pylink.F8_KEY,
                pygame.K_F9: pylink.F9_KEY, pygame.K_F10: pylink.F10_KEY,
            }
            if k in mapping:
                code = mapping[k]
            elif ev.unicode and ord(ev.unicode) < 256:
                code = ord(ev.unicode)
            else:
                continue
            keys.append(pylink.KeyInput(code, ev.mod))
        return keys

    def get_mouse_state(self):
        if self.screen is None:
            return (0, 0), 0
        return pygame.mouse.get_pos(), int(pygame.mouse.get_pressed()[0])

    def alert_printf(self, msg):
        log(f"EyeLink: {msg}")

    # camera image (shown when the operator presses Enter during setup)
    def setup_image_display(self, width, height):
        self.img_size = (width, height)
        self.img_lines = []
        self.clear_cal_display()
        return 1

    def exit_image_display(self):
        self.clear_cal_display()

    def image_title(self, text):
        self.img_title = text

    def set_image_palette(self, r, g, b):
        self.palette = [bytes((r[i], g[i], b[i])) for i in range(len(r))]

    def draw_image_line(self, width, line, totlines, buff):
        if line == 1:
            self.img_lines = []
        pal = self.palette
        self.img_lines.append(b"".join(pal[px] for px in buff[:width]))
        if line == totlines:
            data = b"".join(self.img_lines)
            self.image = pygame.image.frombuffer(data, (width, totlines), "RGB").copy()
            self.__updateimgsize__(width, totlines)  # pylink reads these when drawing the cross hair
            self.draw_cross_hair()
            scale = min(self.size[0] // 2 // width, self.size[1] // 2 // totlines) or 1
            img = pygame.transform.scale(self.image, (width * scale, totlines * scale))
            rect = img.get_rect(center=(self.size[0] // 2, self.size[1] // 2))
            self.screen.fill(self.bg)
            self.screen.blit(img, rect)
            if self.img_title:
                title = self.font.render(self.img_title, True, self.fg)
                self.screen.blit(title, title.get_rect(midtop=(rect.centerx, rect.bottom + 20)))
            pygame.display.flip()

    def _colour(self, colorindex):
        return {
            pylink.CR_HAIR_COLOR: (255, 255, 255), pylink.PUPIL_HAIR_COLOR: (255, 255, 255),
            pylink.PUPIL_BOX_COLOR: (0, 255, 0), pylink.SEARCH_LIMIT_BOX_COLOR: (255, 0, 0),
            pylink.MOUSE_CURSOR_COLOR: (255, 0, 0),
        }.get(colorindex, (128, 128, 128))

    def draw_line(self, x1, y1, x2, y2, colorindex):
        if self.image is not None:
            pygame.draw.line(self.image, self._colour(colorindex), (x1, y1), (x2, y2))

    def draw_lozenge(self, x, y, width, height, colorindex):
        if self.image is not None:
            pygame.draw.rect(self.image, self._colour(colorindex), (x, y, width, height), 1,
                             border_radius=min(width, height) // 2)


# ---------------------------------------------------------------- session

def ask_participant_id():
    while True:
        pid = input("Participant / session ID: ").strip()
        if re.fullmatch(r"[A-Za-z0-9_]+", pid or ""):
            return pid
        print("Use letters, digits and _ only.")


def host_edf_name(pid):
    # The Host PC only accepts 8.3 file names.
    return pid[:8] + ".edf"


class Session:
    def __init__(self, cfg, monitor, pid):
        self.cfg = cfg
        self.monitor = monitor
        self.pid = pid
        self.width = monitor["width"]
        self.height = monitor["height"]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.folder = os.path.join(cfg["out_folder"], f"{pid}_{stamp}")
        self.image_folder = os.path.join(self.folder, "images")
        os.makedirs(self.image_folder, exist_ok=True)
        log.open(os.path.join(self.folder, "session.log"))
        self.local_edf = os.path.join(self.folder, f"{pid}_{stamp}.edf")
        self.host_edf = host_edf_name(pid)
        self.case = 0
        self.in_case = False
        self.keypress_count = 0
        self.case_keypresses = 0
        self.tracker = None
        self.display = None
        self.writer = ScreenshotWriter(monitor, cfg["png_level"])
        self.csv_file = open(os.path.join(self.folder, "keypresses.csv"), "w", newline="", encoding="utf-8")
        self.csv = csv.writer(self.csv_file)
        self.csv.writerow(["event", "case", "keypress", "case_keypress", "wall_clock",
                           "tracker_time_ms", "offset_ms", "image"])

    # -- tracker
    def connect(self):
        if self.cfg["dummy"]:
            log("Dummy mode: no tracker, gaze data is not recorded.")
            self.tracker = pylink.EyeLink(None)
        else:
            log(f"Connecting to EyeLink Host at {self.cfg['host_ip']} ...")
            try:
                self.tracker = pylink.EyeLink(self.cfg["host_ip"])
            except RuntimeError as e:
                raise SystemExit(f"Could not connect to the Host PC ({e}). Check the Ethernet "
                                 "cable, the IP in config.ini and that the Host software is running.")
        t = self.tracker
        t.openDataFile(self.host_edf)
        t.sendCommand(f"add_file_preamble_text 'Recorded by {APP_NAME}, participant {self.pid}'")
        t.setOfflineMode()
        pylink.pumpDelay(100)
        w, h = self.width, self.height
        t.sendCommand(f"screen_pixel_coords = 0 0 {w - 1} {h - 1}")
        t.sendMessage(f"DISPLAY_COORDS 0 0 {w - 1} {h - 1}")
        t.sendCommand(f"calibration_type = {self.cfg['cal_type']}")
        version = 0
        if not self.cfg["dummy"]:
            vstr = t.getTrackerVersionString()
            log(f"Tracker: {vstr}")
            try:
                version = int(float(vstr.split()[-1]))
            except ValueError:
                version = 0
        htarget = ",HTARGET" if version > 3 else ""
        t.sendCommand("file_event_filter = LEFT,RIGHT,FIXATION,SACCADE,BLINK,MESSAGE,BUTTON,INPUT")
        t.sendCommand(f"file_sample_data = LEFT,RIGHT,GAZE,HREF,RAW,AREA,GAZERES,BUTTON,STATUS,INPUT{htarget}")
        t.sendCommand("link_event_filter = LEFT,RIGHT,FIXATION,SACCADE,BLINK,BUTTON,FIXUPDATE,INPUT")
        t.sendCommand(f"link_sample_data = LEFT,RIGHT,GAZE,GAZERES,AREA,STATUS,INPUT{htarget}")
        t.sendCommand("button_function 5 'accept_target_fixation'")

        self.display = PygameCalDisplay(t, self.monitor, self.cfg["cal_bg"], self.cfg["cal_fg"])
        pylink.openGraphicsEx(self.display)
        self.writer.start()

    def calibrate(self):
        if self.cfg["dummy"]:
            log("Dummy mode: calibration skipped.")
            return
        log("Calibration screen open on the tracked monitor. Enter = camera image, "
            "C = calibrate, V = validate, Esc/O = back to recording.")
        self.display.open_window()
        try:
            self.tracker.doTrackerSetup()
        finally:
            self.display.close_window()
        log("Calibration finished.")

    def msg(self, text, offset_ms=0):
        # A leading integer is read by Data Viewer as "this happened N ms earlier".
        self.tracker.sendMessage(f"{offset_ms} {text}" if offset_ms > 0 else text)

    def tracker_time(self):
        if self.cfg["dummy"]:
            return ""
        try:
            return self.tracker.trackerTime()
        except Exception:
            return ""

    # -- cases (trials)
    def start_case(self, t_key):
        if self.in_case:
            self.end_case()
        self.case += 1
        self.case_keypresses = 0
        t = self.tracker
        t.sendMessage(f"TRIALID {self.case}")
        t.sendCommand(f"record_status_message 'Case {self.case}'")
        err = t.startRecording(1, 1, 1, 1)
        if err != 0 and not self.cfg["dummy"]:
            log(f"ERROR: startRecording failed ({err}).")
            return
        pylink.pumpDelay(50)
        self.in_case = True
        # No offset here: the case starts when recording starts, not at the key press.
        image = f"case{self.case:02d}_start.png"
        self.msg(f"CASE_START {self.case}")
        self.msg(f"!V IMGLOAD FILL images/{image}")
        self.msg(f"!V TRIAL_VAR case {self.case}")
        self.writer.capture(os.path.join(self.image_folder, image))
        self.csv.writerow(["case_start", self.case, "", "", datetime.now().isoformat(timespec="milliseconds"),
                           self.tracker_time(), 0, f"images/{image}"])
        self.csv_file.flush()
        log(f"CASE {self.case} started.")

    def end_case(self):
        if not self.in_case:
            return
        t = self.tracker
        self.msg(f"CASE_END {self.case}")
        pylink.pumpDelay(100)
        t.stopRecording()
        self.msg(f"!V TRIAL_VAR keypresses {self.case_keypresses}")
        t.sendMessage(f"TRIAL_RESULT {pylink.TRIAL_OK}")
        self.in_case = False
        log(f"CASE {self.case} ended ({self.case_keypresses} keypress(es)).")

    def keypress(self, t_key):
        if not self.in_case:
            log(f"Response key ignored: no case running. Press {self.cfg['case_key']} to start a case.")
            return
        self.keypress_count += 1
        self.case_keypresses += 1
        offset = round((time.perf_counter() - t_key) * 1000)
        image = f"case{self.case:02d}_kp{self.keypress_count:03d}.png"
        # Message first: the EDF timestamp is the reference, the screenshot follows.
        self.msg(f"KEYPRESS {self.keypress_count} case {self.case}", offset)
        self.msg(f"!V IMGLOAD FILL images/{image}", offset)
        self.writer.capture(os.path.join(self.image_folder, image))
        self.csv.writerow(["keypress", self.case, self.keypress_count, self.case_keypresses,
                           datetime.now().isoformat(timespec="milliseconds"),
                           self.tracker_time(), offset, f"images/{image}"])
        self.csv_file.flush()
        log(f"  KEYPRESS {self.keypress_count} (case {self.case}, #{self.case_keypresses})")

    # -- shutdown
    def close(self):
        self.end_case()
        self.writer.finish()
        self.csv_file.close()
        t = self.tracker
        if t is None:
            return
        t.setOfflineMode()
        pylink.pumpDelay(500)
        t.closeDataFile()
        if not self.cfg["dummy"]:
            log("Transferring EDF from the Host PC ...")
            try:
                t.receiveDataFile(self.host_edf, self.local_edf)
                log(f"EDF saved: {self.local_edf}")
            except RuntimeError as e:
                log(f"ERROR: EDF transfer failed ({e}). The file is still on the Host PC as "
                    f"{self.host_edf}; copy it into {self.folder} by hand.")
        t.close()
        pylink.closeGraphics()


# ---------------------------------------------------------------- main loop

def run():
    cfg = load_config()
    print(f"{APP_NAME}\n")
    monitor = pick_monitor(cfg)
    log(f"Tracked screen: {monitor['width']}x{monitor['height']} at ({monitor['left']},{monitor['top']})")
    pid = ask_participant_id()
    if len(pid) > 8:
        log(f"Note: the Host PC file is named {host_edf_name(pid)} (8 characters max); "
            "the saved EDF keeps the full ID.")

    session = Session(cfg, monitor, pid)
    log(f"Session folder: {session.folder}")
    session.connect()
    session.calibrate()

    events = queue.Queue()
    keys = KeyListener([
        ("keypress", KeySpec(cfg["response_key"])),
        ("case", KeySpec(cfg["case_key"])),
        ("recalibrate", KeySpec(cfg["recal_key"])),
        ("quit", KeySpec(cfg["quit_key"])),
    ], events)
    keys.start()

    log(f"Ready. Keys: {cfg['case_key']} = next case, {cfg['response_key']} = response "
        f"(screenshot), {cfg['recal_key']} = recalibrate, {cfg['quit_key']} twice = quit.")
    log(f"Bring the viewer to the front and press {cfg['case_key']} to start case 1.")

    quit_armed_at = None
    last_check = time.monotonic()
    try:
        while True:
            try:
                action, t_key = events.get(timeout=0.2)
            except queue.Empty:
                action = None
            now = time.monotonic()

            if action == "keypress":
                session.keypress(t_key)
            elif action == "case":
                session.start_case(t_key)
            elif action == "recalibrate":
                session.end_case()
                keys.stop()
                session.calibrate()
                keys = KeyListener(keys.actions, events)
                keys.start()
                log(f"Press {cfg['case_key']} to start the next case.")
            elif action == "quit":
                if quit_armed_at and now - quit_armed_at < QUIT_CONFIRM_SECONDS:
                    break
                quit_armed_at = now
                log(f"Press {cfg['quit_key']} again within {QUIT_CONFIRM_SECONDS:.0f} s to stop and save.")

            if session.in_case and not cfg["dummy"] and now - last_check > 1.0:
                last_check = now
                if session.tracker.isRecording() != pylink.TRIAL_OK:
                    log("WARNING: the tracker stopped recording (aborted on the Host PC?). "
                        f"Press {cfg['case_key']} to start a new case.")
                    session.in_case = False
                    session.tracker.sendMessage(f"TRIAL_RESULT {pylink.TRIAL_ERROR}")
    finally:
        keys.stop()
        session.close()
        log(f"Done. {session.case} case(s), {session.keypress_count} keypress(es). "
            f"Data in {session.folder}")


def main():
    try:
        run()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"\nERROR: {e.code}")
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
    if getattr(sys, "frozen", False):
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
