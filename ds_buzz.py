"""DualSense 震动/灯条 测试器。

2026-10-11 的教训：之前这里发的是 valid_flag0=0x0F / valid_flag1=0x07，
把"静音键LED使能 + 省电控制使能 + 灯条使能"三位一起打开，却把
power_save_control 留成 0 —— 发完之后手柄按键字节 8/9/10 就再也不上报了。
所以这里统一改走 ds_probe.output()：只声明本次真正要改的那一项。

用法：双击桌面「手柄震动测试.bat」。跑完它会让你再按几下键，
用来确认"震完按键还在不在"——这一条比震不震更重要。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ds_probe as pad  # noqa: E402

VARIANTS = [
    ("1 只震右马达（小马达）", dict(right=255, left=0), 1.4),
    ("2 只震左马达（大马达）", dict(right=0, left=255), 1.4),
    ("3 两个一起，中等强度", dict(right=120, left=120), 1.4),
    ("4 灯条红色、亮度拉满", dict(led=(255, 0, 0), brightness=255), 2.2),
    ("5 灯条灭", dict(led=(0, 0, 0), brightness=32), 0.8),
]


def main():
    if not pad.open_all():
        return 1
    if not pad.open_writer():
        print("写句柄打不开，测不了")
        return 2
    print("把柄拿好，能看到顶部灯条。")
    input("准备好按回车开始（自动跑两轮，不怕走神）...")
    for rnd in (1, 2):
        print(f"\n===== 第 {rnd} 轮 =====", flush=True)
        for i, (name, kw, hold) in enumerate(VARIANTS, 1):
            ok = pad.output(**kw)
            print(f"  [{i}/5] {name}  -> {'已发出' if ok else '写失败/超时'}",
                  flush=True)
            time.sleep(hold)
            pad.output()
            time.sleep(0.6)
    print("\n最后确认按键上报还活着：回车后进入 8 秒窗口，"
          "**在这 8 秒里狂按** L1 / R1 / ✕（不用等松手，脚本取的是期间的极值）",
          flush=True)
    input("按回车开始 8 秒... ")
    t0 = time.time()
    seen = set()
    got = False
    while time.time() - t0 < 8:
        r = pad.report()
        if r is not None and len(r) > 10:
            got = True
            if r[8] != 0x08 or r[9] or r[10]:
                seen.add((r[8], r[9], r[10]))
        time.sleep(0.005)
    if not got:
        print("  连报文都读不到 -> 读通路断了，重插手柄再看")
    elif seen:
        print(f"  按键上报正常：窗口里抓到 {len(seen)} 种按键状态，"
              f"例 {[tuple(hex(v) for v in s) for s in list(seen)[:3]]}")
    else:
        print("  !! 报文在流但按键位全程没动 -> 这次写入确实把按键上报弄没了")
    pad.shutdown()
    input("\n按回车关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
