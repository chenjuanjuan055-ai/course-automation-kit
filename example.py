# -*- coding: utf-8 -*-
"""example.py — 怎么把 playback_probe 接进自己的自动化脚本。

场景: 一个"等视频播完"的循环。难点在于**怎么知道它还在播** ——
      很多播放器的进度条只在控制器弹出时才在界面树里，读不到不等于停了。

这个示例演示三层判据融合的写法。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playback_probe import PlaybackProbe      # noqa: E402

# ---- 按你的环境改这三个 ----
ADB = r'F:\MuMuPlayer\nx_main\adb.exe'
DEVICE = '127.0.0.1:7555'
PACKAGE = 'com.example.app'

# 播放器控件 id（用 uiautomatorviewer 或 dump 界面树看）
ID_START = 'start'                    # 播放键（存在 = 停着）
ID_PROGRESS = 'bottom_seek_progress'  # 进度百分比（控制器弹出时才有）
ID_REPLAY = 'replay_text'             # 重播按钮（出现 = 播完了）

STALL_SECONDS = 90      # 音频也停了、进度也不动，多久算真卡住
MAX_WAIT = 5400         # 单节最长等 90 分钟


def adb_tap(adb: str, dev: str, x: int, y: int) -> bool:
    """adb 注入点击：实测比 uiautomator2 的 click 快 3~4 倍。"""
    r = subprocess.run([adb, '-s', dev, 'shell', 'input', 'tap',
                        str(x), str(y)], capture_output=True)
    return r.returncode == 0


def watch_until_done(d, probe: PlaybackProbe, log=print) -> str:
    """等当前视频播完。

    返回 'done'(看到重播) / 'stuck'(真卡住) / 'timeout'

    关键点:
      - 音频在播时**不要去点** UI 上的播放键 —— 那是控制器残留的静态图,
        点下去反而把正在播的视频按停
      - 音频在播时把 sleep 放长(10s), 省 CPU 也让模拟器更稳
    """
    t0 = time.time()
    last_sign = time.time()      # 最后一次看到"确实在动"的时刻
    last_clock = None
    last_pct = None
    best_pct = None
    best_t = 0.0
    clicks = 0

    while time.time() - t0 < MAX_WAIT:
        waited = time.time() - t0

        # ---- 读完 UI（如果拿得到句柄）----
        info = probe.probe(d)
        audio = info['playing']
        pct = info['pct']
        has_start = info['has_start_button']

        # ---- 播完了吗 ----
        if info['at_eos']:
            log('  检测到播放器 atEOS → 本节看完')
            return 'done'

        # ---- 时钟推进 = 最硬的证据 ----
        if info['clock'] >= 0:
            if last_clock is not None and info['clock'] > last_clock:
                last_sign = time.time()
                best_t = waited
            last_clock = info['clock']
        if audio:
            last_sign = time.time()
            best_t = waited          # 进度读数没变, 但视频确实在走

        # ---- 进度读数（有就看）----
        if pct is not None:
            if best_pct is None or pct > best_pct + 0.5:
                best_pct, best_t = pct, waited
            if last_pct is not None and pct < last_pct - 20 and last_pct > 40:
                log(f'  进度从{last_pct:.0f}%跳回{pct:.0f}% → 播完自动重播')
                return 'done'
            last_pct = pct

        # ---- 停着就点一下 ----
        if has_start and not audio:
            # UI 说停、音频也说停 —— 是真停了
            clicks += 1
            if clicks > 8:
                log('  点了几次都没恢复播放，放弃本节')
                return 'timeout'
            log(f'  视频停住了（进度{pct}），点一下[播放](第{clicks}次)')
            # 点播放键：优先按 id 找坐标
            try:
                el = d(resourceId=f'{PACKAGE}:id/{ID_START}')
                if el.exists(timeout=0.5):
                    b = el.info.get('bounds') or {}
                    cx = (b.get('left', 0) + b.get('right', 0)) // 2
                    cy = (b.get('top', 0) + b.get('bottom', 0)) // 2
                    adb_tap(ADB, DEVICE, cx, cy)
            except Exception as e:
                log(f'  点播放失败: {type(e).__name__}')
            best_t = waited
            time.sleep(1.5)
            continue

        # ---- 真卡住了吗（音频停了 + 进度也不动）----
        if not audio and waited - best_t >= STALL_SECONDS:
            log(f'  音频停了、进度也不动 {STALL_SECONDS}s → 认定卡住')
            return 'stuck'

        # ---- 省电: 在播就少查 ----
        time.sleep(10 if audio else 3)

    log('  视频等待超时')
    return 'timeout'


def main():
    import uiautomator2 as u2
    d = u2.connect(DEVICE)
    probe = PlaybackProbe(ADB, DEVICE, PACKAGE)

    print('=== 当前状态 ===')
    print(probe.probe(d))
    print()

    print('=== 等它播完 ===')
    r = watch_until_done(d, probe)
    print(f'\n结果: {r}')


if __name__ == '__main__':
    main()
