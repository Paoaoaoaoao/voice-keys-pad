"""手柄口述主程序：两个模式，键位全部由 口述按键.txt 决定（改完自动生效）。

跑法：双击桌面「口述手柄.bat」。

模式（按 L1+R1 同时按切换，切换时会响一声并打印当前模式）：
  [平台] 默认。完全不碰麦克风，只按你的键表发键盘码，让平台自己的语音转文字干活。
  [本地] 程序开麦：按住=录，松开=本地 SenseVoice 转写并把字打进焦点窗口。

空闲时只有一个线程在读手柄按键报文，不开录音流。手柄被 Steam 抢一下、或你拔了
重插，读线程会自己重新接上（控制台会打一行「手柄接上了，可以按」）。
重复双击 bat 不会跑出第二份：后开的那个会提示一句就退出。
[本地] 模式下只有按住那颗键的几秒才开麦（Windows 右下角麦克风图标亮/灭可核对），
音频只在内存里，转写完就丢，默认不落盘。
开麦失败、转写报错都会把原因打在窗口里并记进 口述历史.txt，不会默默吞掉。

键表写法、可用键名、支持的组合键，全部写在同目录 口述按键.txt 的注释里。
阈值 ON=180/OFF=40、削掉开头 200ms、电平低于 0.008 提示"太轻" —— 这几个是经验值，
不是硬件规格，觉得不合适就改 ptt_pad.py 顶部那几个常量。
"""
import ctypes
import gc
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import wave
from ctypes import wintypes

import numpy as np
import sounddevice as sd

import ds_probe as pad

BASE = os.path.dirname(os.path.abspath(__file__))
SR_ASR = 16000      # SenseVoice 要 16k，手柄那边是 48k


def find_conf():
    r"""键表放在哪都能找到：桌面「口述手柄」文件夹 > 桌面根 > 脚本同目录。
    路径写死过一次，搬个文件夹就找不到键表，所以改成按候选顺序找。"""
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    cands = [os.path.join(desk, "口述手柄", "口述按键.txt"),
             os.path.join(desk, "口述按键.txt"),
             os.path.join(BASE, "口述按键.txt")]
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0]


# 键表放在桌面上，和「口述手柄.bat」放在一起，改的人一眼找得到
CONF = find_conf()
HIST = os.path.join(BASE, "口述历史.txt")
TARGETS = os.path.join(BASE, "口述目标.txt")

PTT_ON = 180
TRIM_MS = 200
RATE = 48000
LOW_LEVEL = 0.008
MODE_COMBO = ("L1", "R1")
# 组合键节奏：先按第一个键，隔 COMBO_STEP_MS 再按第二个……全部按下后停
# COMBO_HOLD_MS 再倒序抬起。这两个数字是按使用手感定的，可调。
COMBO_STEP_MS = 100
COMBO_HOLD_MS = 20
# 按住连发（tap 键）：按下先立刻发一次，按住满 REPEAT_DELAY 秒后每
# REPEAT_EVERY 秒补发一次。这两个数字同样能在键表 [手感] 段改。
# 注：一次 tap 本身要花 20ms（组合键还要加 100ms×键数），所以连发间隔
#     再小也发不到那么快，实际会被 tap 的耗时托底。
REPEAT_DELAY = 0.4
REPEAT_EVERY = 0.033

user32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

ERROR_ALREADY_EXISTS = 183
_MUTEX = [None]


def already_running():
    """同一个键表跑两份 = 每个键发两遍、两路抢同一支麦。
    用局部命名对象当单实例锁；建锁本身失败时不拦正常启动。"""
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    h = k32.CreateMutexW(None, False, "Local\\VoiceKeysPad-DualSense")
    _MUTEX[0] = h
    return bool(h) and h not in (0, 2 ** 64 - 1) and \
        k32.GetLastError() == ERROR_ALREADY_EXISTS

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK = {}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[_c] = ord(_c)
for _i, _d in enumerate("1234567890"):
    VK[_d] = 0x31 + _i
for _n in range(1, 25):
    VK[f"F{_n}"] = 0x70 + _n - 1
VK.update({
    "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09, "ESC": 0x1B, "SPACE": 0x20,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "INSERT": 0x2D, "HOME": 0x24,
    "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "PRTSC": 0x2C, "PRINTSCREEN": 0x2C, "SCROLLLOCK": 0x91, "PAUSE": 0x13,
    "APPS": 0x5D, "VOLMUTE": 0xAD, "VOLDOWN": 0xAE, "VOLUP": 0xAF,
    "PLAYPAUSE": 0xB3, "MINUS": 0xBD, "EQUALS": 0xBB, "COMMA": 0xBC,
    "PERIOD": 0xBE, "SLASH": 0xBF, "SEMICOLON": 0xBA, "QUOTE": 0xDE,
    "LBRACKET": 0xDB, "RBRACKET": 0xDD, "BACKSLASH": 0xDC, "GRAVE": 0xC0,
    "CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WIN": 0x5B, "META": 0x5B,
    "LCTRL": 0xA2, "RCTRL": 0xA3, "LALT": 0xA4, "RALT": 0xA5,
    "LSHIFT": 0xA0, "RSHIFT": 0xA1, "LWIN": 0x5B, "RWIN": 0x5C,
})
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
            0x5B, 0x5C, 0xA3, 0xA5, 0x2C, 0x91, 0x92, 0x93, 0x94}

REV = {v: k for k, v in VK.items()}
for _p, _pref in (("CTRL", "CTRL"), ("ALT", "ALT"), ("SHIFT", "SHIFT"),
                  ("WIN", "WIN"), ("ENTER", "ENTER"), ("DELETE", "DEL")):
    REV[VK[_p]] = _pref


def label(keys):
    return "+".join(REV.get(v, f"0x{v:02X}") for v in keys)

PAD_BITS = {
    "L1": (9, 0), "R1": (9, 1), "L3": (9, 2), "R2": (9, 3),
    "CREATE": (9, 4), "OPTIONS": (9, 5), "R3": (9, 6),
    "□": (8, 4), "✕": (8, 5), "○": (8, 6), "△": (8, 7),
    "PS": (10, 0), "TOUCH": (10, 1), "MUTE": (10, 2),
}
PAD_ALIAS = {"叉": "✕", "圈": "○", "三角": "△", "方块": "□", "触摸板": "TOUCH",
             "静音键": "MUTE", "静音": "MUTE", "PS键": "PS", "分享": "CREATE",
             "选项": "OPTIONS",
             "CROSS": "✕", "X": "✕", "CIRCLE": "○", "O": "○",
             "SQUARE": "□", "TRIANGLE": "△", "TRI": "△",
             "DPAD_UP": "UP", "DPAD_DOWN": "DOWN",
             "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT"}
DPAD_DIRS = {0: ("UP",), 1: ("UP", "RIGHT"), 2: ("RIGHT",), 3: ("DOWN", "RIGHT"),
             4: ("DOWN",), 5: ("DOWN", "LEFT"), 6: ("LEFT",), 7: ("UP", "LEFT")}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


if ctypes.sizeof(INPUT) != 40:
    raise SystemExit(f"INPUT 结构体是 {ctypes.sizeof(INPUT)} 字节，x64 必须是 40，"
                     "否则 SendInput 直接拒绝，什么都发不出去")

user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
_sendfail = [0]


def _send(ev):
    n = user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INPUT))
    if n != 1:
        _sendfail[0] += 1
        if _sendfail[0] in (1, 50):
            print(f"!! SendInput 被拒绝(返回{n}, err={k32.GetLastError()})："
                  "目标窗口若以管理员权限运行，普通权限的程序是发不进键的",
                  flush=True)


def key_event(vk=0, up=False, scan=0, unicode=False):
    flags = (KEYEVENTF_UNICODE if unicode else 0) | (KEYEVENTF_KEYUP if up else 0)
    if vk in EXTENDED and not unicode:
        flags |= KEYEVENTF_EXTENDEDKEY
    _send(INPUT(type=INPUT_KEYBOARD,
                u=INPUT._U(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags))))


def _key_ev(vk, up):
    key_event(vk=vk, up=up)


def combo_down(keys):
    for i, vk in enumerate(keys):
        if i:
            time.sleep(COMBO_STEP_MS / 1000)
        _key_ev(vk, False)


def combo_up(keys):
    for vk in reversed(keys):
        _key_ev(vk, True)


def tap(keys):
    combo_down(keys)
    time.sleep(COMBO_HOLD_MS / 1000)
    combo_up(keys)


def send_chars(s):
    for ch in s:
        key_event(scan=ord(ch), unicode=True)
        key_event(scan=ord(ch), unicode=True, up=True)


def foreground_title():
    h = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, buf, 256)
    return buf.value or ""


k32.GetConsoleWindow.restype = wintypes.HWND


def focus_is_own_console():
    """SendInput 只发给前台窗口。焦点还在这个命令行窗口时，发出去等于白发。"""
    return bool(user32.GetForegroundWindow()) and \
        user32.GetForegroundWindow() == k32.GetConsoleWindow()


def hhmmss():
    return time.strftime("%H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000)


def log_line(s):
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(time.strftime("%m-%d %H:%M:%S ") + s + "\n")


KEY_ALIAS = {"DEL": "DELETE", "RET": "ENTER", "BS": "BACKSPACE", "PGUP": "PAGEUP",
             "PGDN": "PAGEDOWN", "ESCAPE": "ESC", "PSC": "PRTSC",
             "SCRLK": "SCROLLLOCK", "BREAK": "PAUSE", "SUPER": "WIN",
             "CMD": "WIN", "OPTION": "ALT", "OPT": "ALT"}


def parse_combo(text):
    keys = []
    for part in text.upper().replace(" ", "").split("+"):
        if not part:
            continue
        part = KEY_ALIAS.get(part, part)
        if part not in VK:
            raise ValueError(f"不认识的键名 {part}（可用：字母/数字/F1-F24/"
                             f"ENTER TAB ESC SPACE BACKSPACE DELETE HOME END "
                             f"PGUP PGDN 方向键 和修饰键 CTRL ALT SHIFT WIN）")
        keys.append(VK[part])
    if not keys:
        raise ValueError("空组合键")
    return keys


def parse_action(text):
    text = text.strip()
    low = text.lower()
    if low == "dict":
        return ("dict", None)
    if low == "off":
        return ("off", None)
    if ":" not in text:
        raise ValueError(f"动作要写成 tap:xxx / once:xxx / hold:xxx / dict / off，"
                         f"收到 {text}")
    kind, _, arg = text.partition(":")
    kind = kind.strip().lower()
    if kind not in ("tap", "once", "hold"):
        raise ValueError(f"未知动作 {kind}")
    return (kind, parse_combo(arg))


def read_conf_text():
    """键表按 utf-8 / gbk 都试一遍，读不出返回 None（调用方负责报原因）。"""
    if not os.path.exists(CONF):
        return None
    data = open(CONF, "rb").read()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def section_kv(want):
    """取键表里某个设置段（[通知] / [手感]）的 键=值，热重载一样生效。"""
    text = read_conf_text()
    out, sect = {}, None
    if text is None:
        return out
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if line.startswith("[") and line.endswith("]"):
            sect = line[1:-1]
            continue
        if sect != want or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


YES = ("是", "开", "要", "on", "1", "true", "yes")


def load_feel():
    """[手感] 段：tap 键按住连发的节奏。数字是默认值，在键表里就能改。"""
    kv = section_kv("手感")
    cfg = {"连发": kv.get("连发", "是") in YES,
           "启动": REPEAT_DELAY, "间隔": REPEAT_EVERY}
    for k, who in (("连发启动", "启动"), ("连发间隔", "间隔"),
                   ("启动", "启动"), ("间隔", "间隔")):
        if k in kv:
            try:
                cfg[who] = max(0.01, float(kv[k]))
            except ValueError:
                print(f"  [手感] {k} = {kv[k]} 不是数字，"
                      f"仍用 {cfg[who]}", flush=True)
    return cfg


def load_conf():
    """返回 {'平台': {键:(动作,键码)}, '本地': {...}}，出错的那行打印并跳过。"""
    out = {"平台": {}, "本地": {}}
    if not os.path.exists(CONF):
        print("!! 找不到键表：" + CONF, flush=True)
        print("   没有它程序一个键都不会发。桌面「口述手柄.bat」旁边应该有"
              " 口述按键.txt", flush=True)
        return out
    text = read_conf_text()
    if text is None:
        print("!! 键表编码读不了，请用记事本另存为 -> 编码选 UTF-8", flush=True)
        return out
    sect = None
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            sect = line[1:-1]
            out.setdefault(sect, {})
            continue
        if sect is None or "=" not in line:
            continue
        if sect not in ("平台", "本地"):
            continue          # [通知]/[手感] 是给别的读取函数用的
        name, _, val = line.partition("=")
        name = name.strip()
        key = PAD_ALIAS.get(name.upper() if name.isascii() else name,
                            name.upper() if name.isascii() else name)
        if key not in PAD_BITS and key not in ("UP", "DOWN", "LEFT", "RIGHT"):
            print(f"  键表第{ln}行：不认的手柄键「{name}」，已跳过", flush=True)
            continue
        try:
            out[sect][key] = parse_action(val)
        except ValueError as e:
            print(f"  键表第{ln}行：{e}", flush=True)
    return out


WPN = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows",
                   "Notifications", "wpndatabase.db")


def load_notify():
    """读键表里的 [通知] 段（和键位同一个文件，改完一样自动生效）。"""
    kv = section_kv("通知")
    cfg = {"启用": kv.get("启用", "是") in YES, "关键词": [],
           "灯": kv.get("灯", "是") in YES, "间隔": 1.0}
    cfg["关键词"] = [x.strip() for x in
                     kv.get("关键词", "").replace("，", ",").split(",")
                     if x.strip()]
    if "间隔" in kv:
        try:
            cfg["间隔"] = max(0.3, float(kv["间隔"]))
        except ValueError:
            pass
    return cfg


_wpn_err = [None]
_wpn_warned = [False]


def wpn_query(sql, args=()):
    """只读打开系统通知库；读不到就返回 None（不等于没有通知）。
    第一次失败要把真实原因打出来——不然"通知不震"永远查不出为什么。"""
    try:
        c = sqlite3.connect(f"file:{WPN}?mode=ro", uri=True, timeout=1)
        try:
            rows = c.execute(sql, args).fetchall()
        finally:
            c.close()
        if _wpn_err[0] is not None:
            _wpn_err[0] = None
            print("  通知库又能读了，监视恢复", flush=True)
        return rows
    except Exception as e:
        _wpn_err[0] = f"{type(e).__name__}: {e}"
        if not _wpn_warned[0]:
            _wpn_warned[0] = True
            print(f"\n!! 通知库读不到，监视暂时没反应：{_wpn_err[0]}", flush=True)
            print(f"   {WPN}", flush=True)
        return None


def max_arrival():
    rows = wpn_query("SELECT MAX(ArrivalTime) FROM Notification")
    return rows[0][0] if rows and rows[0][0] else 0


def fetch_new(since):
    rows = wpn_query("SELECT ArrivalTime, Payload FROM Notification "
                     "WHERE ArrivalTime > ? ORDER BY ArrivalTime ASC LIMIT 8",
                     (since,))
    if not rows:
        return []
    out = []
    for t, payload in rows:
        xml = payload.decode("utf-8", "replace") if isinstance(payload, bytes) \
            else str(payload or "")
        texts = re.findall(r"<text[^>]*>(.*?)</text>", xml, re.S)
        out.append((t, " / ".join(x.strip() for x in texts)))
    return out


_model = None
TAG = re.compile(r"<\|[^<>|]*\|>")


def load_model():
    global _model
    if _model is not None:
        return
    print("加载 SenseVoice（约 10-20 秒，只这一次）...", flush=True)
    _load_err[0] = None
    import torch
    from funasr import AutoModel
    _model = AutoModel(model="iic/SenseVoiceSmall", hub="ms",
                       trust_remote_code=True, disable_update=True,
                       disable_pbar=True,
                       device="cuda:0" if torch.cuda.is_available() else "cpu")
    print("模型就绪。", flush=True)


_loading = [False]
_load_err = [None]


def load_model_async():
    """后台加载，别挡住按键轮询（加载要 11 秒，主循环卡住会漏键）。
    失败时把原因打出来并记住，不再每按一次键就默默重试一遍 11 秒。"""
    if _model is not None or _loading[0] or _load_err[0]:
        return
    _loading[0] = True

    def worker():
        try:
            load_model()
        except Exception as e:
            _load_err[0] = f"{type(e).__name__}: {e}"
            print(f"\n!! 本地模型加载失败：{_load_err[0]}", flush=True)
            print("   [本地] 模式的 dict 键用不了；[平台] 模式不受影响。",
                  flush=True)
            log_line(f"[模型加载失败] {_load_err[0]}")
        finally:
            _loading[0] = False

    threading.Thread(target=worker, daemon=True).start()


def unload_model():
    """切回平台模式时调用：丢对象 -> 逼 GC -> 让 CUDA 把缓存显存还给系统。"""
    global _model
    if _model is None:
        return False
    _model = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    gc.collect()
    return True


_resample_poly = None


def to_16k(x):
    """48k -> 16k 走多相重采样。旧版用 np.linspace 取最近邻，那是抽点不是重采样：
    8k 以上的能量直接折回低频，听感不明显但 SenseVoice 的梅尔谱是糊的。"""
    global _resample_poly
    if _resample_poly is None:
        from scipy.signal import resample_poly
        _resample_poly = resample_poly
    g = math.gcd(RATE, SR_ASR)
    return _resample_poly(np.asarray(x, np.float32), SR_ASR // g, RATE // g)


def transcribe(x):
    if _model is None:
        load_model()
    pcm = to_16k(x)
    path = os.path.join(tempfile.gettempdir(), f"ptt_{os.getpid()}.wav")
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR_ASR)
        f.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    t0 = time.time()
    try:
        res = _model.generate(input=path, language="zh", emoji=False)
        txt = " / ".join(TAG.sub("", r.get("text", "")).strip()
                         for r in res).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return txt, time.time() - t0


def pad_state(r):
    """report -> {手柄键名: True/False}"""
    st = {}
    for name, (bi, bit) in PAD_BITS.items():
        st[name] = bool(r[bi] >> bit & 1)
    hat = r[8] & 0x0F
    dirs = DPAD_DIRS.get(hat, ()) if hat != 8 else ()
    for d in ("UP", "DOWN", "LEFT", "RIGHT"):
        st[d] = d in dirs
    if len(r) > 6 and r[6] >= PTT_ON:
        st["R2"] = True
    return st


class DictSession:
    def __init__(self):
        self.holder = []
        self.stream = None
        self.t0 = 0

    def start(self):
        """开麦失败（设备被别的程序独占、静音键刚关麦、驱动抽风）返回 False
        并说清原因，不再抛出去把主循环带崩。"""
        self.holder = []

        def cb(indata, frames, t, status):
            self.holder.append(indata[:, 0].copy())

        try:
            self.stream = sd.InputStream(samplerate=RATE, channels=2,
                                         dtype="float32", callback=cb)
            self.stream.start()
        except Exception as e:
            self.stream = None
            print(f"\n!! 麦克风打不开：{type(e).__name__}: {e}", flush=True)
            print("   常见原因：手柄静音键把麦关了 / 另一个程序正占着它。"
                  "按 L1+R1 回 [平台] 模式仍然能发键。", flush=True)
            log_line(f"[开麦失败] {type(e).__name__}: {e}")
            return False
        self.t0 = time.time()
        return True

    def level(self):
        if not self.holder:
            return 0.0
        x = np.concatenate(self.holder)[-RATE // 2:]
        return float(np.sqrt(np.mean(x * x)))

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    def finish(self):
        self.close()
        x = np.concatenate(self.holder) if self.holder else np.zeros(0, np.float32)
        self.holder = []
        cut = int(RATE * TRIM_MS / 1000)
        return x[cut:] if len(x) > cut else x


def run_dict_end(sess, safe, targets):
    x = sess.finish()
    dur = len(x) / RATE
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    if dur < 0.4:
        print(f"\r太短（{dur:.1f}s），丢掉" + " " * 40, flush=True)
        return
    try:
        txt, dt = transcribe(x)
    except Exception as e:
        print(f"\r!! 转写失败：{type(e).__name__}: {e}"
              "（这句丢了，程序还在，再按一次试试）" + " " * 12, flush=True)
        log_line(f"[转写失败] {type(e).__name__}: {e}")
        return
    if not txt:
        print(f"\r[{dur:.1f}s 没识别出字] 电平={rms:.5f}"
              + ("  太轻了，靠近点或大声点（或麦克风被静音键关了）"
                 if rms < LOW_LEVEL else "") + " " * 20, flush=True)
        return
    title = foreground_title()
    print(f"\r[{dur:.1f}s 转写{dt:.2f}s 电平{rms:.4f}] {txt}"
          + ("   ← 电平低于 %.3f，声音偏小/偏远了，容易吞字" % LOW_LEVEL
             if rms < LOW_LEVEL else ""), flush=True)
    if focus_is_own_console():
        user32.MessageBeep(0x40)
        print("  !! 焦点还在这个命令行窗口，字没处可打 -> 已存历史。"
              "先点一下你要输入的软件，再按手柄", flush=True)
        log_line(f"[焦点在命令行|未打字] {txt}")
        return
    if safe and not any(k in title.lower() for k in targets):
        user32.MessageBeep(0x40)
        print(f"  --safe 拦住：「{title[:40]}」不在允许列表，已存历史", flush=True)
        log_line(f"[未注入|{title[:40]}] {txt}")
    else:
        send_chars(txt)
        log_line(f"[已入|{title[:40]}] {txt}")
        print(f"  → 打到「{title[:40]}」", flush=True)


def asrttest():
    load_model()
    print("录 2 秒做自检（你可以随便说一句）...", flush=True)
    s = DictSession()
    s.start()
    time.sleep(2.0)
    x = s.finish()
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0
    txt, dt = transcribe(x)
    print(f"自检: {len(x)/RATE:.1f}s rms={rms:.5f} 耗时{dt:.2f}s -> {txt or '(空)'}")
    pad.shutdown()
    return 0


_fx_busy = [False]


def play_notify_fx(cfg):
    """震动+闪灯放后台线程做：同步跑要占 1.5 秒，会漏掉手柄的松开边沿。"""
    if _fx_busy[0]:
        return
    _fx_busy[0] = True

    def worker():
        try:
            pad.buzz()
            if cfg["灯"]:
                pad.flash()
        finally:
            _fx_busy[0] = False

    threading.Thread(target=worker, daemon=True).start()


def watch():
    """按键自检：你按什么它就打什么，一直跑到你关窗口。用来判断
    '是没读到按键' 还是 '读到了但代码没认出来'。"""
    pad.open_all()
    print("\n按键自检已启动。按任意键试试，同时按 L1+R1 应该看到 COMBO 那行。"
          "\n关掉这个窗口就结束。\n" + "=" * 56, flush=True)
    prev = frozenset()
    n0 = pad.snapshot()[1]
    while True:
        r = pad.report()
        if r is None:
            time.sleep(0.05)
            continue
        st = pad_state(r)
        on = frozenset(k for k, v in st.items() if v)
        if on != prev:
            dt = time.strftime("%H:%M:%S")
            if on:
                extra = "   <== COMBO：这就是切模式的条件" \
                    if all(k in on for k in MODE_COMBO) else ""
                print(f"  {dt} 按下={sorted(on)}{extra}", flush=True)
            else:
                print(f"  {dt} 全部松开", flush=True)
            prev = on
        time.sleep(0.01)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--asrttest":
        return asrttest()
    if len(sys.argv) > 1 and sys.argv[1] == "--watch":
        return watch()
    if already_running():
        print("已经有一个「口述手柄」在跑了，这一个直接退出。", flush=True)
        print("两份同时跑的话：每个键会发两遍，而且两路抢同一支麦克风。",
              flush=True)
        print("要重启用法：先在原窗口按 Ctrl+C 或关掉那个窗口，再双击 bat。",
              flush=True)
        return 1
    pad.open_all()
    safe = "--safe" in sys.argv
    targets = []
    if safe:
        if not os.path.exists(TARGETS):
            open(TARGETS, "w", encoding="utf-8").write("记事本\n")
        targets = [x.strip().lower() for x in open(TARGETS, encoding="utf-8")
                   if x.strip()]
    conf = load_conf()
    notify_cfg = load_notify()
    feel = load_feel()
    last_arrival = max_arrival()
    mode = "平台"
    t_conn = time.time()
    while time.time() - t_conn < 2.0 and not pad.connected():
        time.sleep(0.05)     # 读线程刚起，等一下再报有没接上，免得谎报
    last_conn = pad.connected()
    print("=" * 62)
    print(f"当前模式：[{mode}]   （同时按 L1+R1 切换）")
    print("键表文件：" + CONF + "  （改完 1 秒内自动生效，不用重启）")
    if not last_conn:
        print("手柄还没接上：插上 USB 就自动接上，不用重启程序", flush=True)
    if notify_cfg["启用"]:
        kw = "、".join(notify_cfg["关键词"]) or "任何通知"
        print(f"通知监视：开（{kw}）—— 系统弹通知时手柄会震一下"
              + ("，灯条闪红" if notify_cfg["灯"] else ""))
    else:
        print("通知监视：关（键表 [通知] 启用=否）")
    if safe:
        print("保守模式：只往这些窗口打字 -> " + ", ".join(targets))
    if feel["连发"]:
        print(f"按住连发：tap 键按住 {feel['启动']:.2f} 秒后每 "
              f"{feel['间隔'] * 1000:.0f} 毫秒补发一次"
              "（键表 [手感] 改，once: 的键不参与）")
    else:
        print("按住连发：关（键表 [手感] 连发=否），tap 键一次只发一下")
    print("=" * 62)

    prev = {}
    holds = {}
    reps = {}                 # 键名 -> [按下时刻, 上次补发时刻, 已发次数]
    dict_sess = None
    combo_seen = False
    last_mtime = 0.0
    try:
        last_mtime = os.stat(CONF).st_mtime   # 免得启动第一秒就误报"已重新加载"
    except OSError:
        pass
    last_check = 0.0
    last_notify = 0.0
    last_bar = 0.0

    while True:
        now = time.time()
        if pad.connected() != last_conn:
            last_conn = pad.connected()
            msg = "接上了，可以按" if last_conn else \
                "断了 —— 插紧或重插 USB 会自动接回，不用重启程序"
            print(f"\n>>> 手柄{msg}", flush=True)
        # 键表热重载和通知监视不能排在"收到手柄报文"后面：
        # 手柄一打盹或被 Steam 抢一下，report() 拿不到东西就会让整条链空转，
        # 表现成"通知不震、按键也没反应"两件事同时失灵。
        if now - last_check > 1.0:
            last_check = now
            try:
                m = os.stat(CONF).st_mtime
            except OSError:
                m = 0
            if m != last_mtime:
                last_mtime = m
                try:
                    conf = load_conf()
                    notify_cfg = load_notify()
                    feel = load_feel()
                    print("  键表已重新加载", flush=True)
                except Exception as e:
                    print(f"  键表读坏了，先用着上一份：{type(e).__name__}: {e}",
                          flush=True)

        if notify_cfg["启用"] and now - last_notify >= notify_cfg["间隔"]:
            last_notify = now
            if not last_arrival:
                # 启动时库没读到（被系统占着），先补一次基线，
                # 不然会把库里 100 多条历史当新通知连着震
                last_arrival = max_arrival()
                continue
            for t, txt in fetch_new(last_arrival):
                if t > last_arrival:
                    last_arrival = t
                if notify_cfg["关键词"] and \
                        not any(k in txt for k in notify_cfg["关键词"]):
                    continue
                print(f"\n通知来了：{txt[:70]}", flush=True)
                play_notify_fx(notify_cfg)

        r = pad.report()
        if r is None:
            time.sleep(0.05)
            continue
        st = pad_state(r)
        both = all(st.get(k) for k in MODE_COMBO)
        if both and not combo_seen:
            mode = "本地" if mode == "平台" else "平台"
            for name in list(holds):
                if holds[name][0] == "hold":
                    combo_up(holds[name][1])
            holds.clear()
            reps.clear()
            if dict_sess:
                half = bool(dict_sess.holder)
                dict_sess.close()
                dict_sess = None
                if half:
                    print("    有句话只录了一半，切模式已丢弃", flush=True)
            user32.MessageBeep(0x40 if mode == "本地" else 0x20)
            if mode == "本地":
                print("\n>>> 切到 [本地] 模式（程序开麦，按住=录 松开=转写落字）",
                      flush=True)
                if _model is None:
                    print("    后台加载模型中（约 11 秒），加载好会提示，"
                          "这期间按键照常响应", flush=True)
                    load_model_async()
                else:
                    print("    模型还在，直接可用", flush=True)
            else:
                print("\n>>> 切到 [平台] 模式（不碰麦克风，只发键）", flush=True)
                if unload_model():
                    print("    已卸载本地模型，显存和内存交还系统", flush=True)
                else:
                    print("    模型本来就没加载，没有要释放的东西", flush=True)
        combo_seen = both

        for name, (act, keys) in conf.get(mode, {}).items():
            down_now = st.get(name, False)
            was = prev.get(name, False)
            if act == "off":
                continue
            if act == "dict":
                if down_now and not was:
                    if _model is None:
                        if _load_err[0]:
                            print(f"\n本地模型先前加载失败（{_load_err[0]}），"
                                  "这句不录。[平台] 模式不受影响。", flush=True)
                        else:
                            print("\n模型还没就绪（正在加载或刚卸载），这句不录，"
                                  "等提示后再按", flush=True)
                            load_model_async()
                    else:
                        sess = DictSession()
                        if sess.start():
                            dict_sess = sess
                            print("\n● 收音中…（松开结束）  ", end="", flush=True)
                        else:
                            time.sleep(0.5)
                elif down_now and dict_sess and now - last_bar > 0.25:
                    last_bar = now
                    lv = dict_sess.level()
                    print(f"\r● {'#' * min(26, int(lv * 300)):<26s} {lv:.4f}"
                          f"  {now - dict_sess.t0:4.1f}s  ", end="", flush=True)
                elif not down_now and was and dict_sess:
                    print("\r" + " " * 56 + "\r", end="")
                    try:
                        run_dict_end(dict_sess, safe, targets)
                    except Exception as e:
                        print(f"\n!! 转写这步出意外：{type(e).__name__}: {e}"
                              "（这句丢了，程序继续）", flush=True)
                        log_line(f"[异常] {type(e).__name__}: {e}")
                    finally:
                        dict_sess.close()
                        dict_sess = None
                    time.sleep(0.35)
                continue
            if act in ("tap", "once"):
                if down_now and not was:
                    tap(keys)
                    reps[name] = [now, now, 1]
                    warn = "  ← 焦点在命令行，这一下白发" \
                        if focus_is_own_console() else ""
                    print(f"  {hhmmss()} {name} -> tap {label(keys)}  落点「"
                          f"{foreground_title()[:26]}」{warn}", flush=True)
                elif down_now and name in reps and act == "tap" and feel["连发"]:
                    r0, r1, cnt = reps[name]
                    if now - r0 >= feel["启动"] and now - r1 >= feel["间隔"]:
                        reps[name] = [r0, now, cnt + 1]
                        tap(keys)          # 连发过程不打字，免得刷屏；松开时汇总一行
                elif not down_now and name in reps:
                    cnt = reps.pop(name)[2]
                    if cnt > 1:
                        print(f"  {hhmmss()} {name} 连发 {cnt} 次后松开", flush=True)
            elif act == "hold":
                if down_now and not was:
                    combo_down(keys)
                    holds[name] = ("hold", keys)
                    print(f"  {hhmmss()} {name} -> 按住 {label(keys)}", flush=True)
                elif not down_now and was and name in holds:
                    combo_up(holds[name][1])
                    del holds[name]
                    print(f"  {hhmmss()} {name} -> 松开", flush=True)
        prev = st
        time.sleep(0.015)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    finally:
        pad.shutdown()
