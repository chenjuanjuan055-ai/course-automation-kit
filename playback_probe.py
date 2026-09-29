# -*- coding: utf-8 -*-
"""playback_probe.py — 判断视频是不是真的在播，不依赖 UI。

为什么需要这个:
  很多课程类 APP 用的播放器（jzvd / ExoPlayer 封装），进度条控件
  **只在控制器弹出时才出现在界面树里**。控制器一隐藏，读进度就返回 None
  或读到上次的残留快照 —— 于是程序误判"视频卡住了"，白等几分钟的超时。

  但视频一直在播。用系统播放器状态判断，完全不受 UI 影响。

三层判据（任一命中就算在播）:
  1. dumpsys media.player 的 state(5)   ← 最准，5 = STARTED
  2. dumpsys audio 的 state:started      ← 兜底
  3. UI 进度读数在涨                      ← 控制器弹出时才可用

用法:
    from playback_probe import PlaybackProbe
    probe = PlaybackProbe(adb_path, device_serial, package_name)
    if probe.is_playing():
        ...
    info = probe.probe(d)          # 一次性拿全部信息

只依赖 subprocess，不需要 uiautomator2（probe() 可选传入 d 来读 UI 层）。
"""
from __future__ import annotations

import os
import re
import subprocess
import time


class PlaybackProbe:
    """播放状态探活。"""

    def __init__(self, adb: str, device: str, package: str,
                 cache_ttl: float = 1.5, timeout: float = 10.0):
        """
        adb       adb 可执行文件全路径（模拟器自带的那个往往比 PATH 上的准）
        device    设备号，如 '127.0.0.1:7555' 或 'emulator-5554'
        package   目标 APP 包名，如 'com.example.app'
        cache_ttl dumpsys 不便宜（几十毫秒），这段时间内复用上次结果
        """
        self.adb = adb
        self.device = device
        self.package = package
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._cache = [0.0, False]      # [读取时间, 结果]

    # ---------------- 低层 ----------------
    def _shell(self, *args: str) -> str:
        try:
            p = subprocess.run(
                [self.adb, '-s', self.device, 'shell', *args],
                capture_output=True, timeout=self.timeout)
            return p.stdout.decode('utf-8', 'replace')
        except Exception:
            return ''

    # ---------------- 三层判据 ----------------
    def media_player_playing(self) -> bool:
        """dumpsys media.player 里这个包的播放器是不是 state(5)=STARTED。

        state(5) 是 Android MediaPlayer 的 STARTED 常量。
        这是最可靠的一层 —— 系统播放器层面，跟 UI 完全无关。
        """
        out = self._shell('dumpsys', 'media.player')
        if f'packageName: {self.package}' not in out:
            return False
        return bool(re.search(r'state\(5\)', out))

    def audio_playing(self) -> bool:
        """dumpsys audio 里 MediaPlayer 是不是 state:started。兜底用。"""
        out = self._shell('dumpsys', 'audio')
        for line in out.splitlines():
            if ('type:android.media.MediaPlayer' in line
                    and 'state:started' in line):
                return True
        return False

    def media_clock(self) -> int:
        """播放器时钟已处理的消息数。连着采样两次, 变大 = 真的在推进。

        这是最硬的证据 —— 播放器时钟都停了那就是真卡住。
        返回 -1 表示读不到。
        """
        out = self._shell('dumpsys', 'media.player')
        m = re.search(r'MediaClock:\s*(\d+)\s*messages', out)
        return int(m.group(1)) if m else -1

    def frames_dropped(self) -> int:
        """丢帧数。附带信息，用来判断播放质量。读不到返回 -1。"""
        out = self._shell('dumpsys', 'media.player')
        m = re.search(r'numFramesDropped\((\d+)\)', out)
        return int(m.group(1)) if m else -1

    def at_eos(self) -> bool:
        """是不是播到结尾了（atEOS(1)）。"""
        out = self._shell('dumpsys', 'media.player')
        return 'atEOS(1)' in out

    # ---------------- 综合 ----------------
    def is_playing(self, force: bool = False) -> bool:
        """视频在播吗。带缓存（media.player 优先，audio 兜底）。"""
        now = time.time()
        if not force and now - self._cache[0] < self.cache_ttl:
            return self._cache[1]
        playing = self.media_player_playing() or self.audio_playing()
        self._cache[0], self._cache[1] = now, playing
        return playing

    def probe(self, d=None, playing_id: str = 'start',
              progress_id: str = 'bottom_seek_progress') -> dict:
        """一次性摸清全部状态。

        d 可选：传了 uiautomator2 的句柄就额外读 UI 层（有没有播放键、
        进度读数是多少）。不传就只看系统层。
        """
        info = {
            'playing': self.is_playing(force=True),
            'media_player': self.media_player_playing(),
            'audio': self.audio_playing(),
            'clock': self.media_clock(),
            'frames_dropped': self.frames_dropped(),
            'at_eos': self.at_eos(),
            'has_start_button': None,     # None = 没读 UI
            'pct': None,
        }
        if d is not None:
            try:
                x = d.dump_hierarchy()
                info['has_start_button'] = (
                    f'{self.package}:id/{playing_id}"' in x)
                for m in re.finditer(r'<node ([^>]{0,400}?)/?>', x):
                    s = m.group(1)
                    if f'{self.package}:id/{progress_id}"' not in s:
                        continue
                    t = re.search(r'text="([^"]*)"', s)
                    if t and t.group(1).strip():
                        try:
                            info['pct'] = float(t.group(1))
                        except ValueError:
                            pass
            except Exception:
                pass
        return info

    def watch(self, d=None, poll: float = 5.0, rounds: int = 0,
              log=print) -> dict:
        """盯着播，每 poll 秒报一次。rounds=0 表示一直盯。"""
        i = 0
        last_clock = None
        while True:
            if rounds and i >= rounds:
                break
            info = self.probe(d)
            mark = ''
            if last_clock is not None and info['clock'] > last_clock:
                mark = f" 时钟+{info['clock'] - last_clock}"
            if info['clock'] >= 0:
                last_clock = info['clock']
            log(f"  {i*poll:>5.0f}s  在播={info['playing']:<5} "
                f"media={info['media_player']:<5} 丢帧={info['frames_dropped']:<4} "
                f"pct={info['pct']}{mark}")
            i += 1
            time.sleep(poll)
        return self.probe(d)


# ---------------- 命令行 ----------------
def _find_adb() -> str:
    cands = [
        r'F:\MuMuPlayer\nx_main\adb.exe',
        r'D:\MuMuPlayer\nx_main\adb.exe',
        os.path.expanduser(r'~\AppData\Local\Android\Sdk\platform-tools\adb.exe'),
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    return 'adb'


def _list_devices(adb: str) -> list:
    p = subprocess.run([adb, 'devices'], capture_output=True)
    out = p.stdout.decode('utf-8', 'replace')
    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if '\tdevice' in line:
            devs.append(line.split('\t')[0])
    return devs


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='播放状态探活')
    ap.add_argument('--adb', default=_find_adb())
    ap.add_argument('--device', default=None, help='默认取第一个在线设备')
    ap.add_argument('--package', '-p', required=True, help='APP 包名')
    ap.add_argument('--poll', type=float, default=5.0)
    ap.add_argument('--rounds', type=int, default=12)
    args = ap.parse_args()

    dev = args.device
    if not dev:
        devs = _list_devices(args.adb)
        if not devs:
            raise SystemExit('没有在线设备。先把模拟器/手机连上。')
        dev = devs[0]
    print(f'adb={args.adb}\n设备={dev}\n包名={args.package}\n')

    probe = PlaybackProbe(args.adb, dev, args.package)
    print('=== 盯着看 ===')
    probe.watch(poll=args.poll, rounds=args.rounds)
    print('\n最后一帧:', probe.probe())
