"""
屏幕感知与陪伴（S2 / S3 / S4）。

三件套各管一段，串起来就是一条线：

    monitor.ScreenMonitor      前台轮询 + 截屏 OCR → 场景（恐怖/愤怒/喜庆/幽默）
            │  scene_changed(scene)
            ▼
    companion.ScreenCompanion  场景 → 宠物浮窗主动冒话（四态语句池，不弹卡）

    monitor.ScreenMonitor
            │  foreground_changed(proc) / title_changed(kind)
            ▼
    focus_timer.ScreenFocusTimer  前台是网课/创作/工作类 → 自动进番茄钟（只内存）

    浏览器那条走 browser.classify_title：标题判成 focus（网页版网课、在线创作）
    就进番茄钟；判成 watch（在追剧）就留给上面那条刷剧四态，不抢。

几条贯穿全程的约定：
- **只存内存**：截到的画面、OCR 出来的字、场景与计时状态，一律不落 pet.db、不写盘；
- **标题只在一处破例**：默认不读窗口标题（沿用 monitor/foreground.py 的老约定，标题里
  可能有文档名、网址）。唯一的例外是浏览器前台——为了把"网页版网课"和"在看剧"分开，
  读一次标题判个类，判完只留 focus / watch 这个标签，标题本身不存、不落盘；
  标题判成网课时画面识别整个跳过。总闸在 screen.read_browser_title，关掉即回到不读标题。
- **模型的边界（这条改过口径，说清楚）**：OCR 那条线（S2/S3/S4）**至今一个模型都不调**，
  全是本地关键词与固定语句。加了画面情绪（mood_cv / mood_fuse）之后，情况变成
  「`screen.mood != "off"` 时会加载一个本地模型」——中文 CLIP 的 ONNX 量化版，
  约 132MB，放在 data/models/ 下（已 gitignore，仓库不带权重），**完全离线**，
  画面一个字节不出本机。想回到"一点模型都不碰"，`screen.mood = "off"` 即可，
  行为与加这一层之前逐字相同。云端档（mood="cloud"）另有硬闸：没授权绝不传画面。

    monitor.ScreenMonitor ──┬─ OCR 腿（认字幕字面）  ─┐
                            └─ CV 腿（认画面样子）   ─┴→ mood_fuse 融合 → scene
- **腔调隔离**：冒的话来自固定语句池（宠物本体的腔），不经过角色卡系统，
  玄机先生那套命理腔不会串进来。

开关都在配置的 screen 段（core/config.py 的 ScreenSection）。
"""
