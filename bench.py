# -*- coding: utf-8 -*-
"""bench.py — 量出各操作的实测延迟，找到真正的瓶颈。

用法:
    python bench.py --adb <adb路径> --device <设备号>

为什么先做这个:
  优化前必须知道**慢在哪**。凭感觉优化通常错 —— 实测过才发现
  "读整屏 47ms（不慢）"而"u2 的点击 128ms（比 adb tap 慢 3.6 倍）"。

只读、只测量，不改任何东西。
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time


def _find_adb() -> str:
    for p in (r'F:\MuMuPlayer\nx_main\adb.exe',
              r'D:\MuMuPlayer\nx_main\adb.exe',
              r'C:\Users\xi\AppData\Local\Android\Sdk\platform-tools\adb.exe'):
        if os.path.exists(p):
            return p
    return 'adb'


def _devices(adb: str) -> list:
    p = subprocess.run([adb, 'devices'], capture_output=True)
    out = p.stdout.decode('utf-8', 'replace')
    return [l.split('\t')[0] for l in out.splitlines()[1:]
            if '\tdevice' in l]


def bench(label: str, fn, n: int = 15, warmup: int = 2) -> dict:
    """跑 fn 若干次，返回耗时统计（毫秒）。"""
    for _ in range(warmup):
        try:
            fn()
        except Exception:
            pass
    ts = []
    err = None
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
            break
        ts.append((time.perf_counter() - t0) * 1000)
    if not ts:
        print(f'  {label:34s}  失败: {err}')
        return {}
    ts.sort()
    r = {
        'n': len(ts),
        'avg': statistics.mean(ts),
        'p50': ts[len(ts) // 2],
        'p95': ts[min(len(ts) - 1, int(len(ts) * 0.95))],
        'min': ts[0],
    }
    print(f'  {label:34s}  avg {r["avg"]:7.1f}ms   p50 {r["p50"]:7.1f}   '
          f'p95 {r["p95"]:7.1f}   min {r["min"]:6.1f}')
    return r


def main():
    ap = argparse.ArgumentParser(description='操作延迟实测')
    ap.add_argument('--adb', default=_find_adb())
    ap.add_argument('--device', default=None)
    ap.add_argument('--package', '-p', default=None,
                    help='可选：指定包名测 resourceId 查找')
    ap.add_argument('--n', type=int, default=15, help='每项采样次数')
    args = ap.parse_args()

    dev = args.device
    if not dev:
        devs = _devices(args.adb)
        if not devs:
            raise SystemExit('没有在线设备。先把模拟器/手机连上。')
        dev = devs[0]
    print(f'adb={args.adb}\n设备={dev}\n')

    try:
        import uiautomator2 as u2
    except ImportError:
        print('没装 uiautomator2。只测原始 adb：\n')
        u2 = None

    r = {}
    if u2:
        d = u2.connect(dev)
        print('=== uiautomator2 ===')
        r['dump'] = bench('d.dump_hierarchy()', lambda: d.dump_hierarchy(),
                          n=args.n)
        if args.package:
            r['exists'] = bench(
                'd(resourceId=..).exists(0)',
                lambda: d(resourceId=f'{args.package}:id/content')
                .exists(timeout=0), n=args.n)
        r['app_current'] = bench('d.app_current()', lambda: d.app_current(),
                                 n=args.n)
        r['info'] = bench('d.info', lambda: d.info, n=args.n)
        r['window_size'] = bench('d.window_size()', lambda: d.window_size(),
                                 n=args.n)
        r['u2_click'] = bench('d.click(x, y)', lambda: d.click(540, 960),
                              n=max(5, args.n // 2))

    print('\n=== 原始 adb ===')
    r['adb_tap'] = bench(
        'adb shell input tap',
        lambda: subprocess.run([args.adb, '-s', dev, 'shell',
                                'input', 'tap', '540', '960'],
                               capture_output=True),
        n=max(5, args.n // 2))
    r['adb_swipe'] = bench(
        'adb shell input swipe',
        lambda: subprocess.run([args.adb, '-s', dev, 'shell',
                                'input', 'swipe',
                                '540', '1400', '540', '1000', '300'],
                               capture_output=True),
        n=max(5, args.n // 2))
    r['dumpsys_win'] = bench(
        'adb shell dumpsys window',
        lambda: subprocess.run([args.adb, '-s', dev, 'shell',
                                'dumpsys', 'window', 'windows'],
                               capture_output=True),
        n=max(5, args.n // 2))
    r['dumpsys_media'] = bench(
        'adb shell dumpsys media.player',
        lambda: subprocess.run([args.adb, '-s', dev, 'shell',
                                'dumpsys', 'media.player'],
                               capture_output=True),
        n=max(5, args.n // 2))
    r['screencap'] = bench(
        'adb exec-out screencap -p',
        lambda: subprocess.run([args.adb, '-s', dev, 'exec-out',
                                'screencap', '-p'],
                               capture_output=True),
        n=max(3, args.n // 4))

    # ---------------- 结论 ----------------
    print('\n=== 该怎么改 ===')
    dump = r.get('dump', {}).get('avg')
    u2c = r.get('u2_click', {}).get('avg')
    adbt = r.get('adb_tap', {}).get('avg')
    if dump:
        print(f'  读整屏 {dump:.0f}ms —— 快不快看跟谁比：')
        print(f'    拿它当"轮询间隔"的话，平均只慢 {dump/2:.0f}ms（比固定 sleep 1s 好 20 倍）')
        if dump < 100:
            print('    → dump 本身不慢，别为它做优化')
    if u2c and adbt:
        print(f'  点击: u2 {u2c:.0f}ms vs adb {adbt:.0f}ms '
              f'→ adb 快 {u2c/adbt:.1f} 倍')
        print(f'    挂机一晚点 500 次能省 {(u2c-adbt)*500/1000:.0f} 秒')
    if u2c is None and adbt:
        print(f'  点击走 adb: {adbt:.0f}ms')

    print('\n  最该改的三处（按收益排序）:')
    print('    1. 固定 sleep → 密集轮询    （每次省 ~950ms）')
    print('    2. u2 click → adb tap       （每次省 ~90ms）')
    print('    3. 播放检测看 dumpsys，别只看 UI （省掉误判的几分钟）')


if __name__ == '__main__':
    main()
