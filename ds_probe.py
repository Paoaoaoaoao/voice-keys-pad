"""DualSense 按键探针（CreateFile + ReadFile 版）。

这台机器的实况：Steam 的 ds5drv + Nefarius ViGEmBus 接管了手柄，所以
  - winmm（摇杆 API）枚举到 0 个设备
  - hidapi 枚举 32 个 HID 里没有 054C
  - Raw Input 能列出设备路径，但按 usage 0x01/0x05 注册后 10 秒收到 0 条报文
最后这条能走：拿 Raw Input 给出的接口路径直接 CreateFile（只读、共享），
ReadFile 阻塞收报告。不抢独占、不用关 Steam、不动驱动。

用法：双击桌面「手柄按键探针.bat」，依次按键，每键按住约 1 秒再松。
输出写进 E:\\AI\\TTS\\手柄按键映射.txt

自检：python ds_probe.py --selftest   （监听 12 秒，期间随手按几下 L1/R1）
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# 探针输出跟着脚本走，写死盘符会让别人克隆下来跑不了
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "手柄按键映射.txt")
TARGET = "VID_054C"

GENERIC_READ = 0x80000000
FILE_SHARE_RW = 0x00000003
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INFINITE = 0xFFFFFFFF

RIDI_DEVICENAME = 0x20000007
ERROR_IO_PENDING = 997

lock = threading.Lock()
latest = {}
recv_count = [0]
stop_flag = threading.Event()
handles = []
recording = [False]
window = []
conn = {"ok": False}      # 读通路当前是否连着
CURH = [None]             # 正在用的读句柄（shutdown 时要能关掉）
threads = []


class RIDL(ctypes.Structure):
    _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


def set_sigs():
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.restype = wintypes.BOOL
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.GetOverlappedResult.restype = wintypes.BOOL
    k32.CancelSynchronousIo.restype = wintypes.BOOL
    user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                              ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.UINT)]


set_sigs()


def target_paths():
    n = wintypes.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(n), ctypes.sizeof(RIDL))
    if n.value == 0:
        return []
    arr = (RIDL * n.value)()
    user32.GetRawInputDeviceList(arr, ctypes.byref(n), ctypes.sizeof(RIDL))
    out = []
    for d in arr:
        if d.dwType != 2:
            continue
        sz = wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICENAME, None,
                                      ctypes.byref(sz))
        buf = ctypes.create_unicode_buffer(max(1, sz.value))
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICENAME, buf,
                                      ctypes.byref(sz))
        if TARGET in (buf.value or "").upper():
            out.append(buf.value)
    return out


def open_read_handle():
    paths = target_paths()
    if not paths:
        return None
    h = k32.CreateFileW(paths[0], GENERIC_READ, FILE_SHARE_RW, None,
                        OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    return None if h in (0, -1, 2 ** 64 - 1) else h


def _read_pass(h):
    """读一轮。出错、或者连续 8 秒收不到任何报文（手柄睡眠/拔线/枚举抖动），
    就返回，让外层关掉句柄重开——旧版这里直接 return 掉线程，
    表现成"程序还在，但按什么都没反应"。"""
    ov = OVERLAPPED()
    ov.hEvent = k32.CreateEventW(None, True, False, None)
    buf = ctypes.create_string_buffer(512)
    got = wintypes.DWORD(0)
    last_data = time.time()
    while not stop_flag.is_set():
        ok = k32.ReadFile(h, buf, 512, ctypes.byref(got), ctypes.byref(ov))
        if not ok:
            err = k32.GetLastError()
            if err not in (ERROR_IO_PENDING, 0, 258):
                return
            if k32.WaitForSingleObject(ov.hEvent, 500) != 0:
                if time.time() - last_data > 8:
                    return
                continue
            if not k32.GetOverlappedResult(h, ctypes.byref(ov),
                                           ctypes.byref(got), False):
                if time.time() - last_data > 8:
                    return
                continue
        n = got.value
        if n >= 1:
            rep = buf.raw[:n]
            with lock:
                recv_count[0] += 1
                latest[rep[0]] = rep
                if recording[0] and len(window) < 3000:
                    window.append(rep)
            last_data = time.time()
            k32.ResetEvent(ov.hEvent)
        elif time.time() - last_data > 8:
            return


def _reader_thread():
    while not stop_flag.is_set():
        h = open_read_handle()
        if h is None:
            conn["ok"] = False
            time.sleep(1.5)
            continue
        conn["ok"] = True
        CURH[0] = h
        _read_pass(h)
        conn["ok"] = False
        k32.CancelIo(h)
        k32.CloseHandle(h)
        CURH[0] = None
        time.sleep(0.5)


def connected():
    return conn["ok"]


def open_all():
    """起读线程。找不到设备也照样起线程重试，不再直接失败退出。"""
    if not target_paths():
        print("  暂时找不到含 054C 的 HID 接口，会一直重试（插上手柄就自动接上）",
              flush=True)
    t = threading.Thread(target=_reader_thread, daemon=True)
    t.start()
    threads.append(t)
    return [t]


def snapshot():
    with lock:
        return dict(latest), recv_count[0]


# ---- 往手柄写（震动 / 灯条）。2026-10-11 实测：读句柄是 overlapped 的，
# 写要另开一个非 overlapped 句柄，HID 允许多次打开，互不影响。
# 实测能用的字节组合：[0]=0x02 [1]=0x0F [2]=0x07
# [3]=右马达 [4]=左马达；灯条要额外置 [2] 的 0x04 并写 [43]亮度 [45..47]RGB
WR = [None]
WREV = [None]
GENERIC_RW = 0x80000000 | 0x40000000


def open_writer():
    paths = target_paths()
    if not paths:
        return False
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(paths[0], GENERIC_RW, FILE_SHARE_RW, None,
                        OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if h in (0, -1, 2 ** 64 - 1):
        print(f"  写句柄打不开 err={k32.GetLastError()}", flush=True)
        return False
    WR[0] = h
    WREV[0] = k32.CreateEventW(None, True, False, None)
    return True


def write_report(payload, timeout_ms=300):
    """带超时的写。WriteFile 对 HID 是阻塞的，万一被 Steam/ds5drv 卡住，
    没有超时会把调用它的线程整个挂住（旧版就卡在主循环上）。"""
    if WR[0] is None and not open_writer():
        return False
    k32.WriteFile.restype = wintypes.BOOL
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    data = bytes(payload) + b"\x00" * (64 - len(payload))
    buf = (ctypes.c_ubyte * 64).from_buffer_copy(data)
    ov = OVERLAPPED()
    ov.hEvent = WREV[0]
    n = wintypes.DWORD(0)
    k32.ResetEvent(ov.hEvent)
    if k32.WriteFile(WR[0], buf, 64, ctypes.byref(n), ctypes.byref(ov)):
        return True
    err = k32.GetLastError()
    if err not in (ERROR_IO_PENDING, 0):
        return False
    if k32.WaitForSingleObject(ov.hEvent, timeout_ms) != 0:
        k32.CancelIo(WR[0])          # 超时：放弃这次，绝不死等
        k32.ResetEvent(ov.hEvent)
        _drop_writer()               # 句柄可能已经废了，下次重开
        return False
    if not k32.GetOverlappedResult(WR[0], ctypes.byref(ov),
                                   ctypes.byref(n), False):
        _drop_writer()
        return False
    return True


def _drop_writer():
    """写失败说明句柄可能随设备重新枚举失效了，关掉让下次重开。"""
    if WR[0]:
        try:
            k32.CancelIo(WR[0])
            k32.CloseHandle(WR[0])
        except Exception:
            pass
    WR[0] = None


def output(right=0, left=0, led=None, brightness=255):
    """只声明本次真正要改的部分。
    2026-10-11 踩过坑：曾经写 valid_flag1=0x07（同时使能 静音键LED/省电控制/灯条）
    却把 power_save_control 留成 0，之后手柄按键字节 8/9/10 就再也不上报了。
    所以这里默认只置"马达"这一项，要动灯条才额外置灯条位。"""
    b = bytearray(64)
    b[0] = 0x02
    b[1] = 0x03          # valid_flag0：只声明马达
    b[2] = 0x00          # valid_flag1：不碰静音灯/省电控制
    b[3] = right
    b[4] = left
    if led is not None:
        b[2] = 0x04      # 只开灯条控制
        b[43] = brightness
        b[45], b[46], b[47] = led
    return write_report(b)


def buzz(times=2, ms=150, level=200):
    ok = True
    for _ in range(times):
        ok &= output(right=level, left=level)
        time.sleep(ms / 1000)
        output()
        time.sleep(ms / 400)
    return ok


def flash(led=(255, 0, 0), times=2, ms=250):
    ok = True
    for _ in range(times):
        ok &= output(led=led)
        time.sleep(ms / 1000)
        output(led=(0, 0, 0))
        time.sleep(ms / 400)
    return ok


def report():
    """给别的程序用：当前最新的 0x01 报文（bytes），没有就 None。"""
    with lock:
        return latest.get(0x01)


def bit(r, index, b):
    return bool(r[index] >> b & 1) if r is not None and len(r) > index else False


def shutdown():
    stop_flag.set()
    for h in [CURH[0], WR[0]] + [x[0] for x in handles]:
        try:
            if h:
                k32.CancelIo(h)
                k32.CloseHandle(h)
        except Exception:
            pass
    CURH[0] = None
    WR[0] = None


SEQ = [
    ("L1", "左上肩键"), ("R1", "右上肩键"),
    ("L2", "左扳机，慢慢按到底再松"), ("R2", "右扳机，慢慢按到底再松"),
    ("十字上", "方向键上"), ("十字下", "方向键下"),
    ("十字左", "方向键左"), ("十字右", "方向键右"),
    ("方块", "□"), ("叉", "✕"), ("圈", "○"), ("三角", "△"),
    ("触摸板", "按下触摸板"), ("静音键", "触摸板下面那颗 mic 键"),
    ("PS键", "中间索尼 logo"), ("OPTIONS", "右边小键"),
    ("CREATE", "左边小键"),
    ("左摇杆", "推一下再回中"), ("右摇杆", "推一下再回中"),
]


def selftest(seconds=12):
    open_all()
    _, n0 = snapshot()
    print(f"监听 {seconds} 秒 —— 现在随手反复按几下 L1 和 R1 ...", flush=True)
    time.sleep(seconds)
    reports, n1 = snapshot()
    print(f"收到报文数: {n1 - n0}")
    for rid, rep in sorted(reports.items()):
        print(f"  report 0x{rid:02X} 长度 {len(rep)}: "
              + " ".join(f"{b:02X}" for b in rep[:14]) + " ...")
    shutdown()
    return 0 if (n1 - n0) > 0 else 2


def capture(seconds):
    with lock:
        window.clear()
        recording[0] = True
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.05)
    with lock:
        recording[0] = False
        return list(window)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return selftest(12)
    log = open(LOG, "w", encoding="utf-8")

    def w(s):
        print(s, flush=True)
        log.write(s + "\n")
        log.flush()

    if not open_all():
        return 1

    w("\n先花 3 秒记一下「谁都不按时也会抖」的字节，那些是陀螺仪/计数器，要排除...")
    idle = capture(3.0)
    jitter = set()
    if idle:
        for i in range(len(idle[0])):
            if len({r[i] for r in idle}) > 1:
                jitter.add(i)
    w(f"  采到 {len(idle)} 条，排除自行抖动的字节 {len(jitter)} 个："
      + (",".join(str(i) for i in sorted(jitter)) or "无"))

    w("\n" + "=" * 66)
    w("依次按键。回车后进入 3 秒检测窗，请按慢一点（按住约 1 秒再松）")
    w("=" * 66)
    for name, hint in SEQ:
        input(f"\n>>> 【{name}】（{hint}）按回车... ")
        reps = capture(3.0)
        if not reps:
            w(f"  {name}: 没收到报文")
            continue
        base = reps[0]
        rows = []
        for i in range(len(base)):
            if i in jitter:
                continue
            vals = {r[i] for r in reps}
            if len(vals) == 1 and base[i] in vals:
                continue
            lo, hi = min(vals), max(vals)
            bits = [f"bit{j}" for j in range(8)
                    if (base[i] ^ hi) >> j & 1 or (base[i] ^ lo) >> j & 1]
            rows.append((i, base[i], lo, hi, bits))
        if not rows:
            w(f"  {name}: 3 秒内无变化（按慢一点，或这颗键不在这路报文里）")
        for i, b, lo, hi, bits in rows[:18]:
            w(f"  {name}: 字节{i:>3}  静息=0x{b:02X}  范围=0x{lo:02X}~0x{hi:02X}"
              f"  [{','.join(bits)}]")
        if len(rows) > 18:
            w(f"  {name}: …另有 {len(rows)-18} 个字节")
        for i, b, lo, hi, bits in rows:
            log.write(f"    FULL {name} 字节{i} 静息0x{b:02X} "
                      f"范围0x{lo:02X}~0x{hi:02X} {bits}\n")
    w("\n探针结束。" + LOG + " 里就是每颗键对应的字节和位，照它填 PAD_BITS。")
    shutdown()
    input("按回车关闭")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_flag.set()
