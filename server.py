"""市场资料自助查询后端 —— 直连乐享知识库MCP服务"""
import json
import os
import re
import traceback
import ssl
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.error

from flask import Flask, request, jsonify, send_from_directory, redirect, make_response

app = Flask(__name__, static_folder=".")

# CORS 支持 —— 允许本地开发时从任何端口访问
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp

# ====== 共享配置（唯一真源：shared_config.json） ======
def _load_shared_config():
    """从 shared_config.json 加载共享规则，server.py 和 cloud.html 共用同一份。
    密钥从环境变量 LEXIANG_AUTH_TOKEN 读取，代码里不写死。"""
    config_path = Path(__file__).parent / "shared_config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 编译年级正则
    grade_patterns = {}
    for item in cfg["grade_patterns"]:
        grade_patterns[item["label"]] = re.compile(item["pattern"])

    return {
        "lexiang_mcp_url": cfg["lexiang_mcp_url"],
        "space_id": cfg["space_id"],
        "subject_abbr": cfg["subject_abbr"],
        "grade_patterns": grade_patterns,
        "answer_suffix_pattern": re.compile(cfg["answer_suffix_pattern"]),
        "number_prefix_pattern": cfg["number_prefix_pattern"],
        "copy_redline_patterns": cfg.get("copy_redline_patterns", []),
    }

SHARED = _load_shared_config()
LEXIANG_MCP_URL = SHARED["lexiang_mcp_url"]
LEXIANG_AUTH = os.environ.get("LEXIANG_AUTH_TOKEN", "")
SPACE_ID = SHARED["space_id"]
SUBJECT_ABBR = SHARED["subject_abbr"]
GRADE_PATTERNS = SHARED["grade_patterns"]
ANSWER_SUFFIX_PATTERN = SHARED["answer_suffix_pattern"]
NUMBER_PREFIX_RE = re.compile(SHARED["number_prefix_pattern"])

def extract_grade_from_name(name):
    """从文件名提取年级信息，返回 六年级/七年级/八年级/None"""
    for grade_label, pattern in GRADE_PATTERNS.items():
        if pattern.search(name):
            return grade_label
    return None


# ====== 三条门禁 ======

def is_answer_file(name):
    """判断文件是否为答案文件（文件名含"解析"或"答案"字样）"""
    return bool(ANSWER_SUFFIX_PATTERN.search(name))


def extract_number_prefix(name):
    """提取文件名的编号前缀，如 【SX】【六年级】【25-612】
    题目和答案的前缀完全相同，按前缀配对即可。"""
    m = NUMBER_PREFIX_RE.search(name)
    if m:
        return m.group(1)
    return None


def pair_files(file_list):
    """按编号前缀完全相同配对：提取【学科】【年级】【编号】前缀，前缀相同的材料+答案自动配对。"""
    answers = []
    materials = []
    for f in file_list:
        if is_answer_file(f.get("name", "")):
            answers.append(f)
        else:
            materials.append(f)

    paired = []
    used_answer_indices = set()

    for mat in materials:
        mat_prefix = extract_number_prefix(mat.get("name", ""))
        matched = None

        if mat_prefix:
            for i, ans in enumerate(answers):
                if i in used_answer_indices:
                    continue
                ans_prefix = extract_number_prefix(ans.get("name", ""))
                if ans_prefix and ans_prefix == mat_prefix:
                    matched = ans
                    used_answer_indices.add(i)
                    break

        paired.append({
            "material": mat,
            "answer": matched,
        })

    # 未配对的答案作为独立条目
    for i, ans in enumerate(answers):
        if i not in used_answer_indices:
            paired.append({
                "material": ans,
                "answer": None,
                "is_standalone_answer": True,
            })

    return paired


def search_answer_for_material(material_name, subject, grade):
    """按编号前缀定向搜索答案文件。题目和答案前缀完全相同。"""
    prefix = extract_number_prefix(material_name)
    if not prefix:
        return None

    print(f"  [补搜答案] prefix={prefix}")

    result = call_mcp("search_kb_search", {
        "keyword": prefix,
        "limit": 10,
        "space_id": SPACE_ID,
        "title_only": True,
        "type": "file",
    })
    if not result.get("ok"):
        return None

    docs = result["data"].get("docs", result["data"].get("data", {}).get("docs", []))
    for d in docs:
        name = clean_html(d.get("title", d.get("original_title", "")))
        if not is_answer_file(name):
            continue

        # 年级门禁
        ans_grade = extract_grade_from_name(name)
        if ans_grade is None:
            ans_grade = "六年级"
        if ans_grade != grade:
            continue

        # 前缀完全相同 → 配对成功
        ans_prefix = extract_number_prefix(name)
        if ans_prefix and ans_prefix == prefix:
            tid = d.get("target_id", d.get("id", ""))
            date_str = ""
            ts_raw = d.get("updated_at", "")
            if ts_raw:
                try:
                    ts_int = int(ts_raw)
                    date_str = datetime.fromtimestamp(ts_int).strftime("%Y年%m月%d日").lstrip("0").replace("年0", "年").replace("月0", "月")
                except Exception:
                    pass
            print(f"    [补搜答案] ✅ 前缀匹配: {name[:50]}")
            return {
                "id": tid,
                "name": name,
                "date": date_str or "未知",
                "source": "定向答案搜索",
            }

    print(f"    [补搜答案] ❌ 未找到同前缀答案")
    return None



# ====== 搜索相关 ======

def name_contains_keyword(name, keyword):
    """检查文件名是否匹配用户输入的关键字（基于文件命名，不是内容）"""
    if not name or not keyword:
        return True
    # 直接包含 → 匹配
    if keyword in name:
        return True
    # 用户输入长词组（如"计算题"），文件名是短词（如"计算"）
    # 逐步缩短关键字试匹配
    for i in range(len(keyword) - 1, 0, -1):
        if keyword[:i] in name:
            return True
    return False


def call_mcp(tool_name, arguments):
    """直连乐享知识库MCP服务"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LEXIANG_MCP_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": LEXIANG_AUTH,
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    print(f"[call_mcp] tool={tool_name} payload_len={len(data)} url={LEXIANG_MCP_URL}")
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"[call_mcp] OK status={resp.status}")
            # 提取 MCP 返回的实际数据
            r = result.get("result", {})
            content = r.get("content", [])
            if content and isinstance(content, list):
                text = content[0].get("text", "{}")
                try:
                    parsed = json.loads(text) if isinstance(text, str) else text
                    return {"ok": True, "data": parsed}
                except json.JSONDecodeError:
                    return {"ok": True, "data": {"raw": text}}
            # 也检查 structuredContent
            sc = r.get("structuredContent")
            if sc:
                return {"ok": True, "data": sc}
            return {"ok": True, "data": r}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[call_mcp] HTTP ERROR {e.code}: headers={dict(e.headers)} body={err_body[:300]}")
        return {"ok": False, "error": f"HTTP {e.code}: {err_body[:200]}"}
    except urllib.error.URLError as e:
        print(f"[call_mcp] URL ERROR: {e.reason}")
        return {"ok": False, "error": f"连接失败: {e.reason}"}
    except Exception as e:
        print(f"[call_mcp] EXCEPTION: {type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)}


def clean_html(text):
    """移除HTML标签"""
    return re.sub(r'<[^>]+>', '', text)


def read_entry_content(entry_id):
    """读取乐享知识库中一份资料的正文内容，用于生成专属话术。
    调用 entry_describe_ai_parse_content 获取 AI 解析后的文本。
    如果失败，返回空字符串。"""
    try:
        result = call_mcp("entry_describe_ai_parse_content", {"entry_id": entry_id})
        if not result.get("ok"):
            print(f"  [读内容] entry_ai_parse 失败: {result.get('error','')[:80]}")
            return ""
        data = result["data"]
        # 尝试多种可能的返回结构
        content = (
            data.get("content")
            or data.get("text")
            or data.get("data", {}).get("content")
            or data.get("data", {}).get("text")
            or ""
        )
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        if content and len(content) > 50:
            print(f"  [读内容] 成功获取 {len(content)} 字符")
            return str(content)
        # entry_ai_parse 可能返回空，回退到普通 describe
        print(f"  [读内容] entry_ai_parse 内容太短({len(content)}字)，回退到 entry_describe_entry")
        result2 = call_mcp("entry_describe_entry", {"entry_id": entry_id})
        if result2.get("ok"):
            d2 = result2["data"]
            entry = d2.get("entry", d2.get("data", {}).get("entry", d2))
            description = entry.get("description", entry.get("content", ""))
            if description and len(description) > 30:
                return str(description)
        return ""
    except Exception as e:
        print(f"  [读内容] 异常: {e}")
        return ""


def generate_per_item_copy(material_name, content_text, subject, grade):
    """基于一份资料的具体内容，生成专属的家长群参考话术。
    话术要求：基于真实内容、贴合知识点、实用而不是假大空、几十字即可。"""
    grade_label = {"六年级": "小升初", "七年级": "初一", "八年级": "初二"}.get(grade, grade)

    # 如果没有内容，基于文件名生成降级话术
    if not content_text or len(content_text) < 30:
        return _generate_fallback_copy(material_name, subject, grade_label)

    # 清理内容，截取前面最有信息量的部分
    clean = re.sub(r'<[^>]+>', '', content_text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    # 取前800字用于分析（足够提取知识点了）
    snippet = clean[:800]

    # 从内容中提取知识点线索
    topics = _extract_topics_from_content(snippet, subject)

    # 根据提取的知识点生成话术
    return _compose_copy(topics, material_name, subject, grade_label)


def _extract_topics_from_content(text, subject):
    """从内容文本中提取关键知识点，返回 topic 列表。按学科过滤避免串位。"""
    topics = []
    text_lower = text.lower()

    # 通用检测（所有学科共用）
    if "期末" in text or "期中" in text:
        topics.append("期末/期中")
    if "真题" in text or "升学" in text or "小升初" in text:
        topics.append("真题演练")

    if subject == "数学":
        if "计算" in text or "口算" in text or "脱式" in text or "竖式" in text:
            topics.append("计算训练")
        if "分数" in text or "百分" in text or "小数" in text:
            topics.append("分数与小数")
        if "方程" in text or "解方程" in text or "未知数" in text:
            topics.append("方程解法")
        if "比例" in text or "正比例" in text or "反比例" in text:
            topics.append("比例应用")
        if "几何" in text or "面积" in text or "体积" in text or "周长" in text:
            topics.append("几何图形")
        if "圆" in text and ("面积" in text or "周长" in text or "圆柱" in text):
            topics.append("圆的面积周长")
        if "应用" in text and ("题" in text or "解决" in text):
            topics.append("应用题")
        if "统计" in text:
            topics.append("统计图表")
        if "单位" in text and "换算" in text:
            topics.append("单位换算")

    elif subject == "语文":
        if "阅读" in text and ("理解" in text or "分析" in text or "答题" in text):
            topics.append("阅读理解")
        if "文言文" in text or "古诗" in text or "古文" in text:
            topics.append("文言文/古诗词")
        if "作文" in text or "写作" in text or "书面表达" in text:
            topics.append("写作训练")
        if "拼音" in text or "字词" in text or "笔顺" in text:
            topics.append("基础知识")
        if "修辞" in text or "比喻" in text or "拟人" in text or "排比" in text:
            topics.append("修辞手法")
        if "说明文" in text:
            topics.append("说明文阅读")
        if "记叙文" in text:
            topics.append("记叙文阅读")
        if "议论文" in text:
            topics.append("议论文阅读")
        if "句子" in text and ("修改" in text or "病句" in text or "仿写" in text):
            topics.append("句子训练")

    elif subject == "英语":
        if "语法" in text or "时态" in text or "动词" in text or "名词" in text:
            topics.append("语法要点")
        if "单词" in text or "词汇" in text or "短语" in text:
            topics.append("词汇积累")
        if "句型" in text or "句式" in text:
            topics.append("句型练习")
        if "阅读" in text and "理解" in text:
            topics.append("阅读理解")
        if "完形" in text or "填空" in text:
            topics.append("完形填空")
        if "听力" in text:
            topics.append("听力训练")
        if "作文" in text or "写作" in text:
            topics.append("书面表达")

    elif subject == "科学":
        if "实验" in text or "探究" in text:
            topics.append("实验探究")
        if "观察" in text:
            topics.append("观察方法")
        if "能量" in text or "电" in text or "磁" in text or "力" in text:
            topics.append("物理知识")
        if "物质" in text or "溶解" in text or "变化" in text:
            topics.append("物质变化")
        if "生物" in text or "植物" in text or "动物" in text or "细胞" in text:
            topics.append("生物知识")
        if "地球" in text or "宇宙" in text or "星座" in text or "太阳" in text:
            topics.append("地球与宇宙")

    # 兜底：从文件名提取
    if not topics:
        # 这里 material_name 需要从外部传入，暂时先让调用方处理
        pass

    # 去重保留前3个
    seen = set()
    deduped = []
    for t in topics:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:3]


def _extract_from_filename(name, subject):
    """从文件名提取话题（兜底用），按学科过滤"""
    topics = []
    # 通用
    if "期末" in name: topics.append("期末复习")
    if "期中" in name: topics.append("期中复习")
    if "真题" in name: topics.append("真题训练")
    if "单元" in name: topics.append("单元练习")

    if subject == "数学":
        if "计算" in name: topics.append("计算训练")
        if "方程" in name: topics.append("方程解法")
        if "几何" in name: topics.append("几何图形")
        if "比例" in name: topics.append("比例应用")
        if "百分" in name or "分数" in name: topics.append("分数运算")
    elif subject == "语文":
        if "阅读" in name: topics.append("阅读理解")
        if "文言" in name or "古诗" in name: topics.append("文言文/古诗词")
        if "作文" in name: topics.append("写作练习")
        if "基础" in name or "拼音" in name: topics.append("基础知识")
    elif subject == "英语":
        if "语法" in name: topics.append("语法要点")
        if "单词" in name or "词汇" in name: topics.append("词汇积累")
        if "阅读" in name: topics.append("阅读理解")
        if "完形" in name: topics.append("完形填空")
    elif subject == "科学":
        if "实验" in name: topics.append("实验探究")
        if "知识点" in name: topics.append("知识要点")

    return topics[:2]


def _compose_copy(topics, material_name, subject, grade_label):
    """组装话术文本。核心原则：用大白话、讲清楚这份资料练什么、孩子能收获什么。
    话术渠道：微信私发给家长，不是群发。"""
    # 从文件名提取一个短描述
    short_name = re.sub(r'【[^】]*】', '', material_name).strip()
    short_name = re.sub(r'[（(][^)）]*[)）]', '', short_name)
    short_name = short_name.strip()
    if len(short_name) > 30:
        short_name = short_name[:30] + "…"
    if not short_name:
        short_name = f"{subject}专项"

    if not topics:
        return (
            f"家长您好！这份《{short_name}》是{grade_label}阶段{subject}的课后练习资料，"
            f"题目都是根据孩子这个阶段容易出问题的地方来出的，难度适中。"
            f"孩子做完之后，基本能把这个类型的题吃透。"
            f"有需要的话我把完整资料发您～"
        )

    # 用大白话描述这份资料
    topic_str = "、".join(topics)

    # 判断资料类型
    has_exam = any("期末" in t or "期中" in t or "真题" in t for t in topics)

    if has_exam:
        return (
            f"家长您好！这份{grade_label}{subject}资料主要是{topic_str}，"
            f"里面都是往年考过的类似题型，可以让孩子提前熟悉考试的出题思路和难度。"
            f"建议周末抽时间让孩子做一做，做完看答案自己批，错的题重点看看。"
            f"有需要的话我把完整版发您～"
        )
    else:
        return (
            f"家长您好！这份资料是{grade_label}{subject}的{topic_str}专项练习，"
            f"题目从基础到拔高都有，孩子可以先做基础部分，熟练了再做难的。"
            f"每天抽十几分钟做一两页，坚持一段时间，这个知识点基本就稳了。"
            f"有需要的话我发完整版给您～"
        )


def scan_copy_violations(copy_text):
    """话术红线硬扫描：检测话术是否含禁止内容（价格/名额/升学率）。
    使用 shared_config.json 中的 copy_redline_patterns 规则。
    返回：{"violations": [...], "clean": True/False}"""
    if not copy_text:
        return {"violations": [], "clean": True}

    violations = []
    for rule in SHARED["copy_redline_patterns"]:
        pattern = re.compile(rule["pattern"])
        match = pattern.search(copy_text)
        if match:
            violations.append({
                "rule": rule["name"],
                "matched": match.group(),
                "message": f"话术疑似含「{rule['name']}」信息（匹配: {match.group()}），请人工复核。"
            })

    return {
        "violations": violations,
        "clean": len(violations) == 0,
    }


def _generate_fallback_copy(material_name, subject, grade_label):
    """无法读取内容时的降级话术（基于文件名）"""
    display = re.sub(r'【[^】]*】', '', material_name).strip()
    display = re.sub(r'[（(][^)）]*[)）]', '', display).strip()
    if len(display) > 25:
        display = display[:25] + "…"
    if not display:
        display = f"{subject}练习"

    return (
        f"家长您好！这份《{display}》是{grade_label}阶段{subject}的练习资料，"
        f"题目经过了筛选，紧扣孩子在这个阶段需要掌握的内容。"
        f"平时做完学校作业，可以拿来练练手，巩固一下。"
        f"有需要的话我把资料发您～"
    )


def search_lexiang(keyword, subject):
    """纯标题搜索：基于文件命名匹配用户关键字。不再使用语义搜索读内容。"""
    files = []
    seen_ids = set()

    abbr = SUBJECT_ABBR.get(subject, "")
    query = f"{abbr} {keyword}" if abbr else keyword
    print(f"  [标题搜索] query={query}")

    result = call_mcp("search_kb_search", {
        "keyword": query,
        "limit": 10,
        "space_id": SPACE_ID,
        "title_only": True,
        "type": "file",
    })
    if not result.get("ok"):
        return files

    data = result["data"]
    docs = data.get("docs", data.get("data", {}).get("docs", []))
    for d in docs:
        tid = d.get("target_id", d.get("id", ""))
        tname = clean_html(d.get("title", d.get("original_title", "")))
        ts_raw = d.get("updated_at", "")
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            f = {
                "id": tid,
                "title": tname,
                "name": tname,
                "score": 0,
                "source": "标题搜索",
            }
            if ts_raw:
                try:
                    ts_int = int(ts_raw)
                    f["date_str"] = datetime.fromtimestamp(ts_int).strftime("%Y年%m月%d日").lstrip("0").replace("年0", "年").replace("月0", "月")
                except Exception:
                    f["date_str"] = ""
            files.append(f)

    # 再去掉无名称的条目
    files = [f for f in files if f.get("name", "").strip()]
    return files


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    """提供所有静态文件（HTML/CSS/JS等）"""
    try:
        return send_from_directory(".", filename)
    except Exception:
        return jsonify({"error": "文件不存在"}), 404


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/api/config", methods=["GET", "OPTIONS"])
def api_config():
    """供 cloud.html 获取共享规则配置（不含密钥）"""
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp
    return jsonify({
        "space_id": SPACE_ID,
        "subject_abbr": SUBJECT_ABBR,
        "grade_patterns": [
            {"label": k, "pattern": v.pattern}
            for k, v in GRADE_PATTERNS.items()
        ],
        "answer_suffix_pattern": ANSWER_SUFFIX_PATTERN.pattern,
        "number_prefix_pattern": SHARED["number_prefix_pattern"],
    })

# CORS 预检请求处理
@app.route("/api/<path:subpath>", methods=["OPTIONS"])
@app.route("/api/health", methods=["OPTIONS"])
def api_options(subpath=None):
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.route("/api/download/<entry_id>", methods=["GET", "OPTIONS"])
def api_download(entry_id):
    """获取文件下载链接并重定向"""
    # 处理 CORS 预检请求
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp
    try:
        # 1. 获取 entry 详情，拿到真实的文件 target_id
        detail = call_mcp("entry_describe_entry", {"entry_id": entry_id})
        if not detail.get("ok"):
            return jsonify({"error": "文件信息获取失败"}), 404

        ddata = detail["data"]
        entry = ddata.get("entry", ddata.get("data", {}).get("entry", ddata))
        file_id = entry.get("target_id", "")
        fname = entry.get("name", "资料")

        if not file_id:
            return jsonify({"error": "未找到文件ID"}), 404

        # 2. 获取临时下载链接
        dl = call_mcp("file_download_file", {"file_id": file_id, "expire_seconds": 600})
        if not dl.get("ok"):
            return jsonify({"error": "下载链接获取失败"}), 500

        dldata = dl["data"]
        url = dldata.get("url", dldata.get("data", {}).get("url", ""))
        if not url:
            return jsonify({"error": "下载地址为空"}), 500

        print(f"[download] {fname} → {url[:80]}...")
        return redirect(url)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"下载失败: {str(e)}"}), 500


@app.route("/api/search", methods=["POST", "OPTIONS"])
def api_search():
    try:
        # 处理 CORS 预检请求
        if request.method == "OPTIONS":
            resp = make_response("", 204)
            resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Max-Age"] = "86400"
            return resp

        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"error": "请求格式错误，请使用 JSON 格式发送数据"}), 400
        grade = data.get("grade", "").strip()
        subject = data.get("subject", "").strip()
        keyword = data.get("keyword", "").strip()

        if not grade or not subject or not keyword:
            return jsonify({"error": "请完整填写年级、学科和关键字"}), 400

        print(f"[search] grade={grade} subject={subject} keyword={keyword}")

        # 纯标题搜索（基于文件命名）
        files = search_lexiang(keyword, subject)
        print(f"[search] 标题搜索返回 {len(files)} 条")

        # 格式化 & 门禁过滤
        file_list = []
        target_abbr = SUBJECT_ABBR.get(subject, "")
        grade_mismatched = 0
        keyword_mismatched = 0
        for f in files[:15]:
            name = clean_html(f.get("name", f.get("title", "")))
            # 学科门禁：过滤掉学科缩写不匹配的条目
            if target_abbr and f"【{target_abbr}】" not in name:
                if re.search(r'【[A-Z]{2}】', name) and f"【{target_abbr}】" not in name:
                    continue
            # 年级门禁
            file_grade = extract_grade_from_name(name)
            if file_grade is None:
                file_grade = "六年级"
            if file_grade != grade:
                grade_mismatched += 1
                continue
            # ★ 文件名关键字匹配（基于文件命名，不是内容）
            if not name_contains_keyword(name, keyword):
                keyword_mismatched += 1
                continue
            file_list.append({
                "id": f.get("id", ""),
                "name": name,
                "date": f.get("date_str", "未知") or "未知",
                "source": f.get("source", ""),
            })

        print(f"[search] 门禁过滤: grade={grade_mismatched} kw_mismatch={keyword_mismatched} → 进入配对 {len(file_list)}")

        # 门禁一：后缀配对
        paired = pair_files(file_list)
        print(f"[search] 配对完成: {len(paired)} 组 (其中{sum(1 for p in paired if p.get('answer'))}组有答案)")

        # ★ 补搜答案：对未配对成功的材料，定向搜索答案文件
        missing_answer_count = 0
        for p in paired:
            if p.get("answer") is None and not p.get("is_standalone_answer"):
                mat_name = p["material"].get("name", "")
                ans = search_answer_for_material(mat_name, subject, grade)
                if ans:
                    p["answer"] = ans
                    missing_answer_count += 1
        if missing_answer_count > 0:
            print(f"[search] 补搜答案: 成功补配 {missing_answer_count} 组")

        # ===== 逐份读取内容并生成专属话术 =====
        print(f"[search] 开始为 {len(paired)} 组资料生成专属话术...")
        result_pairs = []
        for idx, p in enumerate(paired):
            mat = p["material"]
            mat_id = mat.get("id", "")
            mat_name = mat.get("name", "")

            # 读取资料内容
            content = ""
            if mat_id:
                content = read_entry_content(mat_id)
                if not content:
                    print(f"  [{idx+1}/{len(paired)}] 无法读取内容: {mat_name[:40]}")

            # 生成专属话术
            per_copy = generate_per_item_copy(mat_name, content, subject, grade)
            print(f"  [{idx+1}/{len(paired)}] 话术生成完成 ({len(per_copy)}字)")

            # 话术红线扫描（硬拦截：检测价格/名额/升学率等敏感词）
            scan_result = scan_copy_violations(per_copy)

            ans = p["answer"]
            pair_entry = {
                "material": {
                    "id": mat_id,
                    "name": mat_name,
                    "date": mat.get("date", "未知"),
                },
                "answer": None,
                "copy": per_copy,  # ★ 每份资料专属话术
                "copy_warnings": scan_result["violations"] if not scan_result["clean"] else [],  # 红线警告标记
            }
            if ans:
                pair_entry["answer"] = {
                    "id": ans.get("id", ""),
                    "name": ans.get("name", ""),
                    "date": ans.get("date", "未知"),
                }
            if p.get("is_standalone_answer"):
                pair_entry["is_standalone_answer"] = True
            result_pairs.append(pair_entry)

        return jsonify({
            "success": True,
            "grade": grade,
            "subject": subject,
            "keyword": keyword,
            "total": len(files),
            "grade_filtered": grade_mismatched,
            "keyword_mismatched": keyword_mismatched,
            "pairs": result_pairs,  # 三列结构：资料 + 答案 + 专属话术
            "files": [  # 兼容旧版：平铺列表
                p["material"] for p in paired if not p.get("is_standalone_answer")
            ],
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"服务器内部错误: {str(e)}"}), 500


@app.route("/api/updates", methods=["GET", "OPTIONS"])
def api_updates():
    """更新日志：近三个月新增的文件。只展示新增，不展示删除。"""
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    try:
        # 计算三个月前的时间戳（秒）
        three_months_ago = datetime.now() - timedelta(days=90)
        start_ts = str(int(three_months_ago.timestamp()))

        print(f"[updates] 查询 {three_months_ago.strftime('%Y-%m-%d')} 之后的新增文件")

        result = call_mcp("entry_list_latest_entries", {
            "space_id": SPACE_ID,
            "limit": 50,
            "filters": {
                "created_at": {
                    "start": start_ts,
                },
                "exclude_types": {
                    "entry_types": ["folder"],
                },
            },
        })

        if not result.get("ok"):
            return jsonify({"success": True, "updates": [], "error": result.get("error", "")})

        data = result["data"]
        entries = data.get("entries", data.get("data", {}).get("entries", data.get("docs", [])))

        updates = []
        for entry in entries:
            name = clean_html(entry.get("name", entry.get("title", "")))
            if not name:
                continue
            ts_raw = entry.get("created_at", "")
            date_str = ""
            if ts_raw:
                try:
                    ts_int = int(ts_raw)
                    date_str = datetime.fromtimestamp(ts_int).strftime("%Y年%m月%d日").lstrip("0").replace("年0", "年").replace("月0", "月")
                except Exception:
                    pass
            updates.append({
                "name": name,
                "date": date_str or "未知",
                "entry_id": entry.get("id", entry.get("entry_id", "")),
            })

        # 按创建时间倒序（最新的排前面）
        updates.sort(key=lambda x: x["date"], reverse=True)

        print(f"[updates] 找到 {len(updates)} 条新增记录")
        return jsonify({"success": True, "updates": updates[:30]})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"获取更新日志失败: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    print("市场资料自助查询服务启动...")
    print(f"   地址: http://localhost:{port}")
    print(f"   乐享MCP: {LEXIANG_MCP_URL}")
    print(f"   知识库: {SPACE_ID}")
    # 关键：使用 "0.0.0.0" 同时监听 IPv4+IPv6，解决 Windows 上 localhost→::1 连接失败问题
    # threaded=True 支持并发请求，避免 MCP 长耗时调用阻塞健康检查
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
