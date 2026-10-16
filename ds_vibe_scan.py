"""客观判断"到底震没震"：靠手柄自己的加速度计。

震动时 IMU 一定有高频抖动，所以不用问人：
  1) 先采 1.5 秒静息，算出 IMU 区（字节 12-31）相邻采样的平均波动 = 基线
  2) 对每个候选 valid_flag0 发一次满强度马达报告，发的同时采 0.8 秒，算同一指标
  3) 波动明显高于基线的那些 flag0，就是真的在震

用法：python ds_vibe_scan.py   （手柄平放桌面，别拿在手里）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ds_probe as pad  # noqa: E402

IMU = range(12, 32)
CANDS = [0x01, 0x02, 0x03, 0x07, 0x0B, 0x0F]


def activity(seconds):
    """返回 IMU 区相邻采样的平均绝对差（越大=越晃）。"""
    samples = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        r = pad.report()
        if r:
            samples.append([r[i] for i in IMU])
        time.sleep(0.003)
    if len(samples) < 4:
        return 0.0, len(samples)
    tot = 0.0
    n = 0
    for a, b in zip(samples, samples[1:]):
        for i in range(len(a)):
            d = abs(a[i] - b[i])
            tot += min(d, 256 - d)
            n += 1
    return (tot / n if n else 0.0), len(samples)


def send(vf0, right=255, left=255):
    b = bytearray(64)
    b[0] = 0x02
    b[1] = vf0
    b[2] = 0x00
    b[3] = right
    b[4] = left
    return pad.write_report(b)


def main():
    if not pad.open_all() or not pad.open_writer():
        print("打不开手柄")
        return 1
    print("先静息 1.5 秒（别碰手柄）...", flush=True)
    time.sleep(0.5)
    base, n = activity(1.5)
    send(0x03, 0, 0)
    print(f"  基线波动 = {base:.3f}（{n} 个采样）\n", flush=True)
    print(f"{'valid_flag0':>12} {'发报告':>7} {'波动':>9} {'倍数':>7}  判定")
    hits = []
    for vf0 in CANDS:
        time.sleep(0.6)
        send(vf0)
        act, _ = activity(0.8)
        send(0x03, 0, 0)
        ratio = (act / base) if base > 1e-6 else 0.0
        verdict = "在震" if ratio >= 3.0 and act > 0.5 else "没动"
        if verdict == "在震":
            hits.append(vf0)
        print(f"{'0x%02X' % vf0:>12} {'已发':>7} {act:9.3f} {ratio:6.1f}x  {verdict}",
              flush=True)
        time.sleep(0.6)
    print(f"\n能震的 valid_flag0: {[hex(v) for v in hits] or '一个都没有'}")
    if hits:
        print(f"建议用 0x{hits[0]:02X}（第一个能震的），"
              "我再确认它不会把按键上报弄没")
    else:
        print("说明问题不在 flag0：可能是这台机器上 ds5drv 把输出报告吞了，"
              "震动这条路要换方案")
    pad.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
