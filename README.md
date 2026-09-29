# 课程平台自动化工具箱

把「课程类 APP 的自动化」做快、做准的一套方法论和可复用工具。

不是某个平台的成品脚本 —— 是**怎么把它做快**的实测数据、踩过的坑、能直接抄的工具。

---

## 两条路线，先选清楚

发现一个课程平台后，第一件事不是写脚本，是**反编译**。

国内的自研培训平台，绝大多数在服务端**信任客户端上报**。反编译一下往往能直接找到进度上报 API：

```java
// 典型：完成判定只看客户端上报的一个数字
state = (duration - maxProgress < 2500) ? "Y" : "N";
```

于是有两条路：

| | A. 离线直送 | B. 真播放 + 挤掉白等 |
|---|---|---|
| **速度** | 单节 0.5 秒 | 上限 = 视频时长 |
| **上报内容** | 假的 | 真的 |
| **风险** | 平台一查就是作弊证据 | 最小 |
| **适用** | 可牺牲的账号 | **在意账号安全时选这个** |

**这个仓库主要讲 B。** 因为「快」不该建立在「骗」上面 —— 而且 B 能优化的空间比大多数人想的大得多。

---

## B 路线的五个优化点（全部有实测数据）

### 1. 播放检测 —— 最容易误判的地方

**很多播放器的进度条控件只在控制器弹出时才出现在界面树里。**

控制器一隐藏，读进度就返回 `None`，或者读到上次的残留快照。程序于是误判成「视频卡住了」，白等几分钟。

**但视频一直在播。**

```bash
# 系统播放器状态，不受 UI 影响
adb shell dumpsys media.player | grep -E "packageName: <pkg>|state\("
# 输出: state(5)   ← 5 = STARTED

# 音频兜底
adb shell dumpsys audio | grep "type:android.media.MediaPlayer"
# 输出: state:started

# 最硬的证据 —— 播放器时钟在推进
adb shell dumpsys media.player | grep "MediaClock"
# 每 5 秒涨约 130 条
```

**三层判据融合，任一命中就算在播：**

```python
def is_playing(device, pkg):
    """dumpsys media.player 的 state(5) → 最准
    dumpsys audio 的 state:started    → 兜底"""
    out = adb_shell(device, 'dumpsys', 'media.player')
    if f'packageName: {pkg}' in out and re.search(r'state\(5\)', out):
        return True
    out = adb_shell(device, 'dumpsys', 'audio')
    return any('type:android.media.MediaPlayer' in l and 'state:started' in l
               for l in out.splitlines())
```

完整实现见 [`playback_probe.py`](playback_probe.py)。

> ⚠️ **音频说「在播」时，不要去点 UI 上那个 `[播放]` 键。**
> 那是控制器残留的静态图，点下去反而会把正在播的视频按停。

### 2. 进课时主动点播

实测多数课程 APP 打开课时是**待播放**状态，**不会自动开播**。

旧写法靠「给应用 25 秒自己开播」—— 白等 25 秒。

```python
def kickstart_play(d, pkg, tries=6):
    for _ in range(tries):
        x = dump(d)
        if is_lesson_complete(x):
            return
        if has_id(x, 'start'):            # 有播放键 = 停着
            fast_click(d(resourceId=f'{pkg}:id/start'))
            return
        if parse_video_time(x) is not None:
            return                         # 已经在播
        time.sleep(0.3)
```

### 3. 点击速度 —— u2 比 adb 慢 3.6 倍

一台 MuMu 模拟器上的实测：

| 操作 | 耗时 |
|---|---|
| `d.dump_hierarchy()` | **47ms** |
| `d(resourceId).exists(0)` | 26ms |
| `d.app_current()` | **138ms** |
| `adb shell input tap` | **36ms** |
| `d.click(x,y)` | **128ms** |
| `adb shell input swipe` | **344ms** |

**结论：**
- `d.click()` 比 `adb input tap` 慢 **3.6 倍** —— 换掉，一晚能省几十秒
- **但 `dump` 只要 47ms —— 不要为 dump 做优化**，真正的浪费在别处

```python
def fast_click(el, adb, device):
    """优先 adb 注入(36ms)，退回 u2(128ms)。
    adb 还有个额外好处：弹窗吞 u2 点击时它仍能点到。"""
    pt = el_center(el)
    r = subprocess.run([adb, '-s', device, 'shell', 'input', 'tap',
                        str(pt[0]), str(pt[1])], capture_output=True)
    if r.returncode == 0:
        return True
    return el.click()
```

### 4. 等待策略 —— 别用固定 sleep

```python
# 慢：每轮固定等 1 秒，而检测只要 47ms → 白等 950ms
while time.time() < deadline:
    time.sleep(1.0)
    st = page_state(d)

# 快：密集轮询 → 发现变化只慢 25ms
while time.time() < deadline:
    time.sleep(0.05)
    st = page_state(d)
```

**滑完之后用「等稳定」而不是固定 sleep：**

```python
def wait_still(d, timeout=0.8, gap=0.15):
    """连读两次界面一模一样 = 停住了。
    timeout 给 0.8 而不是 1.2：实测滑完内容 200~300ms 就稳定，
    真要慢的页面，这里也是「一变就继续等」，不会漏。"""
    deadline = time.time() + timeout
    prev = dump(d)
    while time.time() < deadline:
        time.sleep(gap)
        now = dump(d)
        if now and now == prev:
            return now
        prev = now
    return prev
```

实测效果：`gap=0.3 → 0.15` 让 `wait_still` 从 **385ms 降到 239ms**。

### 5. 点击落错行 —— 三个必须堵的窟窿

**症状：想开课时 A，实际打开了课时 B。** 不报错，静默点错，白学一节。

| 窟窿 | 原因 | 修法 |
|---|---|---|
| 文字匹配不到 | `d(text=x)` 是**整串字面**匹配，顿号/空格差异就失效 | 归一化后比对 |
| 点到屏幕外 | 屏幕外元素的坐标，adb 照样发 tap —— **静默失效** | 校验中心点在可视区 |
| 点错不检查 | 拿到实际打开的标题只打日志，不比对 | 比对，不一致就退回重开 |

```python
def _find_row_on_screen(d, txt, key):
    """只在当前这一屏找，屏幕外的节点不返回"""
    for n in ET.fromstring(dump(d)).iter('node'):
        t = (n.get('text') or '').strip()
        if _norm(t) != key:                  # 归一化匹配
            continue
        x1, y1, x2, y2 = parse_bounds(n)
        cy = (y1 + y2) // 2
        if not (ry(120) < cy < ry(1840)):    # 必须真的在可视区内
            continue
        return ((x1 + x2) // 2, cy)
    return None


def _same_lesson(a, b):
    """课时标题比对：归一化 → 前缀 → 编号 → 前 8 字"""
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    short, long = sorted([na, nb], key=len)
    if len(short) >= 6 and long.startswith(short):
        return True
    if _lesson_no(a) and _lesson_no(a) == _lesson_no(b):
        return True          # 编号比：避免 1.1.2 撞上 1.1.20
    return na[:8] == nb[:8]
```

**点开后必须核对：**

```python
cur = get_lesson_title(d)
if cur and not _same_lesson(cur, want):
    log(f'想开[{want}]但开的是[{cur}] → 退回重开')
    back_to_detail(d)
    continue                 # 不要照学下去
```

---

## 翻列表：到底就停

**症状：找不到目标时一直往下滑** —— 在模拟器上看就是「一直在刷新」。

```python
prev = None
for _ in range(max_round):
    cards = parse_cards(dump(d))
    if target in cards:
        return target
    key = tuple(cards)
    if key and key == prev:      # 一模一样 = 到底了
        return None
    prev = key
    scroll_down(d)
```

**另一个大坑：清单里分类写错。** 标了 A 分类、课实际在 B 分类时，
程序会把每个分类都从头翻一遍。

实测数据：**每轮白翻 32 秒，三个分类 64 秒 → 改成写对分类后 4 秒。快 16 倍。**

---

## 答题：先看接口有没有明文答案

**用正文匹配猜答案的正确率极低（实测 0%）。** 先看接口返回里有没有：

```
courseSectionTestList[].systemAnswerValue    ← 标准答案的 id 列表
courseSectionTestList[].systemAnswerAnalysis ← 解析
```

有就**预采一次落盘**，答题时按题干查表：

```python
bank.put_section(questions)          # 题干归一化后做 key
hit = bank.pick_from_bank(stem, ui_options)
```

采集时**每 10 节落一次盘** —— 几百节的采集被打断不该全丢。

> **注意：不是每门课都有题。** 实测同一平台内：一门课 69 节有 24 题，
> 另三门 94/80/56 节**一题没有**。采到 0 条不要慌，先抽查确认。

---

## 逆向流程（找上报 API）

### 1. 反编译
```bash
adb shell pm path <pkg>
adb pull <base.apk>
jadx -d apk_out base.apk --no-imports -j 8
```
无加固时 jadx 直接出 Java 源码。

### 2. 定位 API 层
找 `baseUrl`，重点看三处：
- baseUrl 常量
- **OkHttp Interceptor** → 全部请求头（**看有没有签名**）
- **Retrofit interface** → 全部端点

### 3. 提取全部端点
```python
pat = re.compile(
    r'@retrofit2\.http\.(POST|GET|PUT|DELETE)\("([^"]+)"\)\s*\n\s*[^\n]*?\s(\w+)\(')
```
按关键词分组：`progress|study|learn|record|section|learnedTime`

### 4. 挖完成判定
在播放 Activity 里找 `onPause()` / `onStop()` —— 上报几乎都在这。
找 `state = ... ? "Y" : "N"` 这类三元。

### 5. 拿 token
```bash
# root 模拟器：shared_prefs 直接读，通常明文
adb shell "su" <<< "cat /data/data/<pkg>/shared_prefs/user.xml"
```
> **坑：`adb shell su -c '...'` 的引号会被吞，必须用管道送命令。**

### 6. 先只读探测
先调只读接口（课程列表/详情）确认凭据有效，再动写接口。

---

## 常见平台缺陷

| 缺陷 | 表现 |
|---|---|
| 服务端信任客户端上报 | 进度由客户端给，无服务端校验 |
| 无请求签名 | 只有固定 header，无 HMAC/nonce |
| token 明文存储 | shared_prefs 直接读 |
| 答案明文下发 | `systemAnswerValue` 之类字段 |
| 限速字段未启用 | 接口返回 null 且代码里无引用 |

---

## 工具

| 文件 | 用途 |
|---|---|
| [`playback_probe.py`](playback_probe.py) | 播放状态探活（三层判据），不依赖 UI |
| [`bench.py`](bench.py) | 量出各操作的实测延迟，找到真正的瓶颈 |

---

## 实测收益总结

| 项 | 改前 | 改后 | 提升 |
|---|---|---|---|
| 点击 | 167ms | **62ms** | 63% |
| 滑完等稳定 | 385ms | **239ms** | 38% |
| 等页面切换 | 最多 1000ms | **50ms** | 95% |
| 找一门课 | 64 秒 | **4 秒** | 16× |
| 进课时 | 25 秒空等 | **6 秒** | — |
| 播放误判 | 白等 180 秒 | **30ms 确认** | — |

**单节固定开销：从上万毫秒压到 8 秒。**

---

## 免责声明

本仓库是**技术方法论** —— 关于 uiautomator2 / adb 的性能优化与 UI 自动化的准确性。
不针对任何特定平台，不含任何平台的实现细节或凭据。

使用者需自行确认其使用场景符合所在平台的服务条款与当地法律。

---

## License

MIT
