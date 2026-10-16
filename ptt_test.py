"""口述输入实测：列出所有能录的麦 -> 选号 -> 回车 -> 倒计时 -> 录 18 秒 -> 立刻出 SenseVoice 文字。

用法：双击桌面「口述测试.bat」，照着提示按回车。
每次都由你按回车才开始录，不会出现"不知道你已经开始了"。
结果追加到  E:\\AI\\TTS\\口述测试\\结果.txt

录音走 sounddevice（WASAPI 优先，WDM-KS 兜底），比 ffmpeg dshow 看得全：
dshow 抓不到电脑自带的 Realtek 麦孔，你要把拆下来的麦克风头插电脑 MIC 孔就只能靠它。

看这几个数就够：
  信噪比    越大越好；>25dB 才算可用
  峰值因数  >15 基本说明录进去的是撞击/爆音，不是连续人声
  左/右     两路各转一遍，万一只有一个才是真麦
"""
import os
import re
import shutil
import subprocess
import sys
import time
import wave

import numpy as np
import sounddevice as sd

FF = shutil.which("ffmpeg")


def ffmpeg_or_stop():
    if not FF:
        sys.exit("没找到 ffmpeg（PATH 里没有）。这个脚本只用它来拆/合声道，"
                 "装了再跑：winget install ffmpeg")


HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "口述测试")
LOG = os.path.join(BASE, "结果.txt")
TAG = re.compile(r"<\|[^<>|]*\|>")

SKIP_KEYS = ("映射器", "主声音捕获", "立体声混音", "线路输入",
             "Steam Streaming", "NVIDIA Virtual", "Virtual Audio", "CABLE")
API_RANK = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2,
            "Windows WDM-KS": 3}

台词 = (
    "今天游戏圈有八条消息，卡普空把 RE 引擎改成了 AI 生成，"
    "PS5 被指悄悄改款，GT 赛车七的十月更新定在十五号。"
)

_model = None


def api_name(dev):
    return sd.query_hostapis(dev["hostapi"])["name"]


def list_mics():
    """返回 [(显示名, 设备索引), ...]，同名端点只留优先级最高的那个 host API。"""
    best = {}
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        name = d["name"]
        if any(k in name for k in SKIP_KEYS):
            continue
        api = api_name(d)
        rank = API_RANK.get(api, 9)
        if name not in best or rank < best[name][2]:
            best[name] = (name, i, rank, api)
    ordered = sorted(best.values(), key=lambda t: (t[2], t[0]))
    return [(f"{t[0]}   [{t[3]}]", t[1]) for t in ordered]


def asr(wav_path):
    global _model
    if _model is None:
        print("  正在加载 SenseVoice（第一次约 15 秒）...", flush=True)
        import torch
        from funasr import AutoModel
        _model = AutoModel(
            model="iic/SenseVoiceSmall", hub="ms", trust_remote_code=True,
            disable_update=True,
            device="cuda:0" if torch.cuda.is_available() else "cpu")
        print("  加载完成", flush=True)
    res = _model.generate(input=wav_path, language="zh", emoji=False)
    return " / ".join(TAG.sub("", r.get("text", "")).strip() for r in res) or "(空)"


def record(dev_index, seconds, dst):
    d = sd.query_devices(dev_index)
    rate = int(d["default_samplerate"]) or 48000
    ch = max(1, int(d["max_input_channels"]))
    try:
        buf = sd.rec(int(seconds * rate), samplerate=rate, channels=ch,
                     dtype="float32", device=dev_index)
        sd.wait()
    except Exception as e:
        print(f"  录音失败: {type(e).__name__}: {e}")
        return None
    x = np.asarray(buf)
    two = x if x.ndim == 2 else np.repeat(x[:, None], 2, axis=1)
    if two.shape[1] > 2:
        two = two[:, :2]
    with wave.open(dst, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(two, -1, 1) * 32767).astype("<i2").tobytes())
    return two.astype(np.float32), rate, dst


def stats(x, rate):
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    n = max(1, int(rate * 0.5))
    bins = [float(np.sqrt(np.mean(s * s))) for s in
            (x[i:i + n] for i in range(0, len(x) - 1, n))]
    bins = [b for b in bins if b > 0]
    floor = min(bins) if bins else 0.0
    crest = (peak / rms) if rms > 1e-9 else 0.0
    snr = (20 * np.log10(rms / floor)) if floor > 1e-9 and rms > floor else 0.0
    return rms, peak, floor, crest, snr, bins


def write16(x, rate, dst):
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    out = dst.replace(".wav", "_16k.wav")
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", dst,
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out],
                   capture_output=True)
    return out


def one_take(mic, seconds):
    label, dev_index = mic
    stamp = time.strftime("%H%M%S")
    raw = os.path.join(BASE, f"take_{stamp}_raw.wav")
    print(f"\n用麦：{label}  (设备号 {dev_index})")
    print("念这句（念别的也行，但同一句才好横向比）：")
    print("  " + 台词)
    input("准备好了按回车 -> 3 秒后开始录（录完自动停）...")
    for i in (3, 2, 1):
        print(f"  {i}", flush=True)
        time.sleep(1)
    print(f"  开始录音 {seconds} 秒，请念...", flush=True)
    got = record(dev_index, seconds, raw)
    if got is None:
        return None
    two, rate, _ = got
    mix = two.mean(axis=1)
    print(f"\n  混音 rms={np.sqrt(np.mean(mix * mix)):.5f} "
          f"峰值={np.max(np.abs(mix)):.5f}")
    rows = []
    for name, x in (("左声道", two[:, 0]), ("右声道", two[:, 1]), ("混音", mix)):
        rms, peak, floor, crest, snr, bins = stats(x, rate)
        w1 = write16(x, rate, os.path.join(BASE, f"take_{stamp}_{name}.wav"))
        txt = asr(w1)
        print(f"  {name}: rms={rms:.5f} 底噪={floor:.5f} 信噪比={snr:4.1f}dB "
              f"峰值因数={crest:4.1f}  ->  {txt}", flush=True)
        rows.append((name, rms, floor, snr, crest, txt))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {label} "
                f"{seconds}s 原始文件={raw}\n")
        for name, rms, floor, snr, crest, txt in rows:
            f.write(f"{name}\trms={rms:.5f}\t底噪={floor:.5f}\t"
                    f"信噪比={snr:.1f}dB\t峰值因数={crest:.1f}\t{txt}\n")
    print(f"\n  已记入 {LOG}")
    return raw


def smoke(seconds=1.5):
    mics = list_mics()
    print("现在能录的麦：")
    for i, (name, idx) in enumerate(mics, 1):
        print(f"  [{i}] (sd{idx}) {name}")
    if not mics:
        print("SMOKE FAIL: 一个都没抓到")
        return 1
    got = record(mics[0][1], seconds, os.path.join(BASE, "_smoke.wav"))
    if got is None:
        print("SMOKE FAIL")
        return 1
    two, rate, path = got
    rms, peak, floor, crest, snr, bins = stats(two[:, 0], rate)
    print(f"SMOKE OK {mics[0][0]} rate={rate} rms={rms:.5f} peak={peak:.5f} "
          f"floor={floor:.5f} crest={crest:.1f} snr={snr:.1f}dB")
    print("bins: " + " ".join(f"{b:.4f}" for b in bins))
    os.remove(path)
    return 0


def main():
    os.makedirs(BASE, exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        return smoke()
    print("=" * 62)
    print("口述输入实测（每段录完立刻出识别文字，方便换姿势再来）")
    print("=" * 62)
    n = 0
    while True:
        mics = list_mics()
        if not mics:
            print("没抓到任何录音设备。检查插好了没，或按回车退出。")
            input()
            break
        print("\n当前能录的麦（每次重新扫描，新插的会自己出现）：")
        for i, (name, idx) in enumerate(mics, 1):
            print(f"  [{i}] {name}")
        k = input("选编号，直接回车=结束: ").strip()
        if not k.isdigit() or not (1 <= int(k) <= len(mics)):
            break
        one_take(mics[int(k) - 1], 18)
        n += 1
        if input("再录一段? (回车=是, n=结束) ").strip().lower() == "n":
            break
    print(f"\n完成 {n} 段。结果表在 {LOG}")
    print("跑完了看 结果.txt：信噪比大的那一路就是能用的麦。")
    input("按回车关闭本窗口")
    return 0


if __name__ == "__main__":
    sys.exit(main())
