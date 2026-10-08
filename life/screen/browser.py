"""
浏览器前台判类（S4 扩展）：标题一句话分成"上网课 / 在创作"还是"在看剧"。

为什么单开一条线：浏览器在内置分类表里归"其他"，`chrome.exe` 既可能是网课、
也可能是爱奇艺。光看进程名分不出来，只能看标题——这是整个 v2 里**唯一**读窗口
标题的地方（隐私破例，见 monitor/foreground.py 开头那段），所以规矩定死：

- **只分类，不留原文**：函数进的是标题串，出的是三个短标签之一，标题本身不存字段、不落盘；
- **只在浏览器前台调**：非浏览器进程一律不读标题；
- **不 OCR 浏览器内容**：标题判成"网课/创作"时，画面识别那条线整个跳过
  （既然人在上课，没有理由再去截人家屏幕）。

三个出口：

    TITLE_FOCUS  网页版网课 / 在线创作 → 该进番茄钟（被动工作态）
    TITLE_WATCH  剧集 / 电影 / 直播    → 仍走 S2 刷剧四态，不进番茄钟
    ""           认不出来的           → 两条都不占，交给原有的进程名判断

**两个都命中时按 FOCUS 算**：网课标题里带"第 3 讲"、剧集标题里带"第 3 集"，
字面很像；从打扰程度看，判成专注是安静地起个倒计时，判成刷剧是要开口说话，
误判成专注的代价小得多。
"""

from __future__ import annotations

import re

TITLE_FOCUS = "focus"
TITLE_WATCH = "watch"

# 浏览器进程名。跟 monitor/monitor.py 的 WATCH_PROCS 里那份浏览器清单是同一批，
# 但这里语义不同（那边问"值不值得 OCR"，这边问"要不要读标题"），所以各留一份。
BROWSERS: frozenset[str] = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "360se.exe", "360chrome.exe", "sogouexplorer.exe", "quarkpc.exe",
    "iexplore.exe", "vivaldi.exe", "chromium.exe", "launcher.exe",
})

# 网页版网课 / 在线课堂。宁可写全一点：漏一个就是"上课时宠物在旁边叨叨"。
#
# 收词的尺子只有一把：**这个词一出现，这页还可能不是在上课吗？** 通不过就不收。
# 下面 S1 补的这批平台专名，正是照这把尺子筛下来的——同一条子串规则（`word in title`）
# 一个字没动，加的全是"只可能指这一个平台"的名字。
#
# 被这把尺子挡在门外的（**故意不收，宁可漏判**）：
#   「得到」   —— "得到"是普通动词，标题里到处是（"如何得到…"）；
#   「乐享」   —— "乐享生活"这类页面遍地都是，收进来就是乱判；
#   「学院」   —— "中国科学院""某某学院"都会中招；
#   「教育」   —— 太泛，连"教育局通知"都算；
#   「有道」   —— 只收"有道精品课/有道课堂"，光"有道"会把有道云笔记/有道词典拉进来。
# 收词的收益是渐进的（漏一个只是少判一次），代价是每次误判都要宠物开口打扰，
# 所以这里一律往"少收"那侧偏。
COURSE_WORDS: tuple[str, ...] = (
    "学习通", "超星", "慕课", "mooc", "网课", "在线课堂", "网络课堂", "课堂",
    "课程", "学堂在线", "腾讯课堂", "网易云课堂", "网易公开课", "中国大学",
    "智慧树", "雨课堂", "钉钉", "腾讯会议", "classin", "coursera", "edx",
    "udemy", "khan academy", "可汗学院", "bilibili课堂", "哔哩哔哩课堂",
    "课件", "教学", "培训", "讲座", "第1讲", "第2讲", "第3讲",
    "公开课", "复习", "考试", "题库", "作业", "自习",
    # ---- S1 扩展 ①：小众网校 / 垂直课程站 ----
    # 这些名字里**没有**"课堂/课程/慕课/培训"这类泛词，靠上面那张表一个字都兜不到，
    # 所以才要一个一个点名收进来。反过来，名字里带泛词的（网易云课堂、慕课网、
    # 平安知鸟、酷学院）不用重复写——"课堂""慕课""培训"已经把它们的标题盖住了。
    "极客时间", "geekbang", "imooc",
    "网校", "沪江", "新东方", "学而思", "猿辅导", "三节课", "开课吧",
    # 拉勾/起点都只收全名：「拉勾」会把拉勾招聘拉进来，「起点」会把起点中文网拉进来
    "拉勾教育", "起点学院", "百度传课", "有道精品课", "有道课堂", "youdao",
    # 高校自有的那几家：超星/雨课堂/智慧树已在上面，这些是补的长尾
    "云班课", "学银在线", "智慧职教", "国家智慧教育", "好大学在线",
    # ---- S1 扩展 ②：境外小平台（coursera/edx/udemy/khan 上面已有，这里是长尾）----
    "udacity", "datacamp", "codecademy", "freecodecamp", "skillshare",
    "pluralsight", "futurelearn", "masterclass", "linkedin learning",
    # ---- S1 扩展 ③：公司 / 机构内训平台 ----
    # 单独的「企业微信」**不收**：它多数时候是同事聊天，不是上课；"企业微信课堂"
    # 那种组合已经被上面的「课堂」盖住了，所以这里不必也不该写它。
    "云学堂", "小鹅通", "知鸟", "酷学院", "umu", "moodle", "canvas lms", "腾讯乐享",
)

# 在线创作 / 设计 / 剪辑类网页工具。
CREATIVE_WORDS: tuple[str, ...] = (
    "figma", "canva", "稿定", "创客贴", "剪映", "在线ps", "photopea",
    "草料", "processon", "语雀", "notion", "石墨", "飞书文档", "腾讯文档",
    "mastergo", "即时设计", "墨刀", "axure", "sketch", "在线编辑", "编辑器",
)

# 干活用的网页系统（v2-focus-web ①）。真机反馈："打开工作网页识别不了、时钟不走"——
# 上面两张表收的是"上课"和"创作工具"，一般的工作网页（办公套件 / 研发协作 / 后台看板）
# 一个词都兜不到，标题判成空串，于是不进口番茄钟。
#
# 尺子还是那一把：**这个词一出现，这页还可能是在看剧/刷手机吗？** 通不过就不收。
# 被挡在门外的（故意不收）：「项目」「需求」「管理」这类词太泛，新闻标题里到处是；
# 「工作」两个字本身同理。收的是"只可能指这一个系统/这一类页面"的名字。
#
# 沿用 S1 已经定过的那两条，不在这里偷偷收回来（test_screen 的收词纪律盯着这两条）：
#   「企业微信」——多数时候是同事聊天，不是"在工作页面上干活"；它的桌面端本来就在
#                BUILTIN 里归"工作"，前台是那个客户端照样进专注，网页版漏判不亏；
#   「有道云笔记 / 印象笔记 / evernote」——个人记事本，"记录，成为更好的自己"那种
#                页面跟工作无关，跟「有道」被否掉是同一个理由。
# 真在自己单位的企业微信/OA 页面上班干活的，设置页有「工作站点词」，自己加。
WORK_WORDS: tuple[str, ...] = (
    # ---- 办公套件 / 在线文档：写周报、改文档、开会都在这里 ----
    "飞书", "lark", "金山文档", "wps云", "wps office",
    "google 文档", "google 表格", "google 幻灯片", "google docs", "google sheets",
    "office 365", "office.com", "sharepoint", "onedrive", "outlook", "邮箱", "邮件",
    "在线表格", "在线文档", "共享文档", "云文档", "协作文档", "会议纪要",
    "幕布", "onenote", "腾讯乐享文档",
    # ---- 研发协作 / 项目管理：看板、工单、代码仓库 ----
    "github", "gitlab", "gitee", "bitbucket", "gerrit", "jenkins",
    "jira", "confluence", "禅道", "zentao", "teambition", "tapd", "云效",
    "coding.net", "stack overflow", "需求池", "迭代看板", "工单", "知识库", "wiki",
    # ---- 后台 / 数据：运营后台、数据看板 ----
    "后台管理", "管理后台", "运营后台", "管理系统", "工作台", "控制台",
    "数据看板", "数据大屏", "仪表盘", "dashboard", "订单管理", "库存管理",
    "客户管理", "crm", "erp", "oa系统", "考勤",
    # ---- 网页版会议 ----
    "zoom", "飞书会议", "wemeetapp", "webex", "teams",
)

# 剧集 / 电影 / 综艺 / 直播：这些留着走 S2，别抢成番茄钟。
WATCH_WORDS: tuple[str, ...] = (
    "全集", "在线观看", "在线播放", "免费观看", "电视剧", "动漫", "番剧",
    "剧场版", "电影", "综艺", "纪录片", "直播", "短视频", "爱奇艺", "优酷",
    "腾讯视频", "芒果tv", "哔哩哔哩", "bilibili", "netflix", "youtube",
)

# 「第 3 集」「第12话」这类编号：剧集的强信号，但网课的「第 3 讲」不算
_EPISODE_NUM = re.compile(r"第\s*[\d一二三四五六七八九十百千]+\s*[集话期季部]")


def is_browser(proc: str | None) -> bool:
    """这个进程是不是浏览器（只有它们才走标题那条破例）。"""
    return bool(proc) and proc.lower() in BROWSERS


def _hit(title: str, words: tuple[str, ...]) -> bool:
    return any(word in title for word in words)


def classify_title(title: str, extra_words: tuple[str, ...] = ()) -> str:
    """浏览器标题 → TITLE_FOCUS / TITLE_WATCH / 空串。

    只看标题这一句，不做任何记录——出参是短标签，进来那句标题不留在任何地方。

    `extra_words` 是用户自己在设置页标的工作站点词（`screen.focus_words`）：
    内置表收不全所有人用的系统，漏的那个不该只能等下一版。跟内置词同一条规则
    （大小写不敏感的子串），**只归到专注那一侧**——用户标的就是"这是我的工作页面"。
    """
    if not title:
        return ""
    lowered = title.lower()
    extra_words = tuple(word.lower() for word in extra_words if word)
    focus = (
        _hit(lowered, COURSE_WORDS)
        or _hit(lowered, CREATIVE_WORDS)
        or _hit(lowered, WORK_WORDS)
        or _hit(lowered, extra_words)
    )
    watch = _hit(lowered, WATCH_WORDS) or bool(_EPISODE_NUM.search(lowered))
    if focus:
        # 两个都命中时按专注算：安静起倒计时，比误开口说话代价小
        return TITLE_FOCUS
    if watch:
        return TITLE_WATCH
    return ""
