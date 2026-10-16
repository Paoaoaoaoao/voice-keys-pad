"""写入隔离排查：到底是"哪一步"让手柄不再上报按键。

背景：这台机器上 Steam 的 ds5drv 内核驱动接管了手柄（winmm 和 hidapi 都看不见设备，
只有 raw input 路径能开）。实测规律：只要往手柄写过东西，按键字节 8/9/10 就冻住不动，
换字节组合没用。所以要定位到底是哪一步造成的。

四步，每步之后都让你按几下键，脚本自己统计那 6 秒里按键位有没有动过：
  0 基线（还没做任何写操作）
  1 只开写句柄，一个字节都不写
  2 写一条灯条报告（不震马达）
  3 写一条马达报告（震一下）
哪一步之后"按键=无"，元凶就是那一步。

用法：双击桌面「手柄写入排查.bat」，跟着提示走。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ds_probe as pad  # noqa: E402


def check_buttons(step_name, seconds=6):
    """让你在这几秒里狂按，统计按键位有没有动过。"""
    print(f"\n--- {step_name}：接下来 {seconds} 秒请狂按 L1 / R1 / ✕ ---", flush=True)
    t0 = time.time()
    seen = set()
    got_any = False
    while time.time() - t0 < seconds:
        r = pad.report()
        if r is not None and len(r) > 10:
            got_any = True
            if r[8] != 0x08 or r[9] != 0 or r[10] != 0:
                seen.add((r[8], r[9], r[10]))
        time.sleep(0.005)
    if not got_any:
        print(f"    {step_name}: 连报文都没有 -> 读通路已断", flush=True)
    elif seen:
        print(f"    {step_name}: 按键上报正常，抓到 {len(seen)} 种按键状态",
              flush=True)
    else:
        print(f"    {step_name}: !! 报文在流，但按键位 8/9/10 全程没动 -> 已断",
              flush=True)
    return bool(seen)


def main():
    if not pad.open_all():
        return 1
    print("手柄读通路已开。这一步不做任何写操作。")
    input("按回车开始第 0 步（基线）...")
    ok0 = check_buttons("第0步 基线")
    if not ok0:
        print("\n基线就是坏的，先重插手柄再来。")
        pad.shutdown()
        input("按回车关闭")
        return 2

    input("\n按回车做第 1 步：只打开写句柄，不写任何字节...")
    wok = pad.open_writer()
    print(f"    写句柄打开: {'成功' if wok else '失败'}")
    time.sleep(1.0)
    ok1 = check_buttons("第1步 只开句柄没写")

    if ok1:
        input("\n按回车做第 2 步：写一条只动灯条的报告（灯应该变红）...")
        pad.output(led=(255, 0, 0), brightness=255)
        time.sleep(1.0)
        ok2 = check_buttons("第2步 写过灯条")
        if ok2:
            input("\n按回车做第 3 步：写一条马达报告（应该会震）...")
            pad.output(right=200, left=200)
            time.sleep(0.4)
            pad.output()
            ok3 = check_buttons("第3步 写过马达")
        else:
            ok3 = False
    else:
        ok2 = ok3 = False

    print("\n" + "=" * 56)
    print(f"  第0步 基线          : {'正常' if ok0 else '已断'}")
    print(f"  第1步 只开写句柄    : {'正常' if ok1 else '已断'}")
    print(f"  第2步 写过灯条      : {'正常' if ok2 else '已断'}")
    print(f"  第3步 写过马达      : {'正常' if ok3 else '已断'}")
    first_bad = {0: "基线本身（先重插手柄）", 1: "开写句柄这个动作",
                 2: "灯条报告", 3: "马达报告", None: "全部通过，写入是安全的"}
    bad = next((i for i, v in enumerate([ok0, ok1, ok2, ok3]) if not v), None)
    print(f"\n  结论：元凶 = {first_bad[bad]}")
    print("  这四行结果决定震动功能留不留：全正常就留，某步异常就改那一格的写法。")
    pad.shutdown()
    input("\n按回车关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
