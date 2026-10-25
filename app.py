"""网页抓取工具：输入 URL，抓取 HTML，去除 script/style 后展示；可选接入 DeepSeek 进行智能数据提取。"""

import csv
import io
import json
import logging
import os
import random
import re
import time
import urllib.parse

from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request, send_file
from openai import OpenAI
from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# 日志配置：抓取失败、页间休眠等信息输出到控制台
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 加载 .env 文件中的环境变量（DEEPSEEK_API_KEY）
load_dotenv()

app = Flask(__name__)

# ===== DeepSeek 配置（Key 从环境变量 / .env 文件读取）=====
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# 发给大模型的 HTML 最大字符数，过长则截断，避免超出上下文
# 方案一：DeepSeek 支持很长的上下文，这里放大到 50000，避免常规页面被截断
MAX_HTML_CHARS = 50000

# 单页模板：输入框 + 按钮 + 结果展示区
PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <title>网页抓取工具</title>
    <style>
        body { font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 960px; margin: 40px auto; padding: 0 20px; color: #24292f; }
        h1 { font-size: 26px; }
        h2 { font-size: 18px; margin-top: 28px; }
        label { display: block; margin: 14px 0 4px; font-weight: 600; }
        input[type=text], input[type=number], textarea { width: 100%; box-sizing: border-box; padding: 10px; font-size: 15px; border: 1px solid #d0d7de; border-radius: 6px; font-family: inherit; }
        textarea { resize: vertical; }
        button { margin-top: 16px; padding: 10px 28px; font-size: 15px; cursor: pointer; background: #1f6feb; color: #fff; border: none; border-radius: 6px; }
        button:hover { background: #1158c7; }
        .error { color: #d1242f; }
        .note { color: #0969da; }
        pre { background: #f6f8fa; border: 1px solid #ddd; border-radius: 6px; padding: 15px;
              white-space: pre-wrap; word-break: break-all; max-height: 600px; overflow: auto; }
        table { border-collapse: collapse; width: 100%; margin-top: 8px; background: #fff; }
        th, td { border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; vertical-align: top; word-break: break-word; }
        th { background: #f6f8fa; font-weight: 600; }
        tr:nth-child(even) td { background: #fafbfc; }
    </style>
</head>
<body>
    <h1>网页抓取工具</h1>
    <form method="post">
        <label for="url">目标网页 URL</label>
        <input type="text" id="url" name="url" placeholder="请输入目标网页 URL，例如 https://example.com" value="{{ url }}">

        <label for="requirement">数据提取需求（可选，填写后调用 DeepSeek 智能提取）</label>
        <textarea id="requirement" name="requirement" rows="3" placeholder="例如：提取网页里的所有文章标题和对应的链接">{{ requirement }}</textarea>

        <label for="selector">CSS 选择器（可选，填写后只把命中的节点发给大模型，避免截断）</label>
        <input type="text" id="selector" name="selector" placeholder="例如：ol.grid_view（豆瓣电影列表）" value="{{ selector }}">

        <label for="max_pages">最大抓取页数（用于分页爬取，默认 1 页）</label>
        <input type="number" id="max_pages" name="max_pages" min="1" step="1" value="{{ max_pages }}" placeholder="默认 1 页">

        <button type="submit">开始抓取</button>
    </form>

    {% if error %}<p class="error">{{ error }}</p>{% endif %}

    {% if extract_error %}<p class="error">{{ extract_error }}</p>{% endif %}

    {% if selector_note %}<p class="note">{{ selector_note }}</p>{% endif %}

    {% if crawl_note %}<p class="note">{{ crawl_note }}</p>{% endif %}

    {% if table_headers %}
        <h2>提取结果（{{ table_rows|length }} 条）</h2>
        <button type="button" onclick="exportCsv()" style="margin: 0 0 12px;">导出 CSV</button>
        <table id="result-table">
            <thead>
                <tr>{% for h in table_headers %}<th>{{ h }}</th>{% endfor %}</tr>
            </thead>
            <tbody>
                {% for row in table_rows %}
                <tr>{% for cell in row %}<td>{{ cell }}</td>{% endfor %}</tr>
                {% endfor %}
            </tbody>
        </table>
    {% endif %}

    {% if extract_raw %}
        <h2>大模型原始返回（JSON 解析失败）</h2>
        <pre>{{ extract_raw }}</pre>
    {% endif %}

    {% if html %}
        <h2>清理后的 HTML（{{ length }} 字符）</h2>
        <pre>{{ html }}</pre>
    {% endif %}

    <script>
    // 将当前结果表格以 JSON 发给后端 /export 生成 CSV 并下载
    function exportCsv() {
        const table = document.getElementById('result-table');
        if (!table) return;

        const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim());
        const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr =>
            Array.from(tr.querySelectorAll('td')).map(td => td.textContent)
        );

        fetch('/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ headers: headers, rows: rows, filename: '提取结果.csv' })
        })
        .then(resp => {
            if (!resp.ok) {
                return resp.json().then(j => { throw new Error(j.error || ('导出失败：' + resp.status)); });
            }
            // 从 Content-Disposition 解析后端返回的文件名（兼容 filename*=UTF-8'' 与 filename=）
            const disp = resp.headers.get('Content-Disposition') || '';
            const m = disp.match(/filename\\*=UTF-8''([^;]+)/i) || disp.match(/filename="?([^";]+)"?/i);
            const filename = m ? decodeURIComponent(m[1]) : '提取结果.csv';
            return resp.blob().then(blob => ({ blob: blob, filename: filename }));
        })
        .then(({ blob, filename }) => {
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        })
        .catch(err => alert(err.message));
    }
    </script>
</body>
</html>
"""

# 带上 User-Agent，模拟浏览器访问
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _scroll_to_load_all(page, max_rounds=30):
    """反复滚到底部，直到页面高度不再增长，以触发无限滚动/下拉加载；最后滚回顶部。"""
    last_height = page.evaluate("document.body.scrollHeight")
    for _ in range(max_rounds):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height <= last_height:
            break
        last_height = new_height
    # 滚回顶部，确保视口附近的懒加载元素也都触发
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


# 通用登录态：若存在导出的 storage_state（见 save_storage_state.py），加载它应对需要登录的站点
STORAGE_STATE_PATH = "storage_state.json"
FETCH_RETRIES = 2          # 页面导航/执行上下文异常时的重试次数
FETCH_RETRY_DELAY = 3      # 重试前等待秒数


def fetch_html(url):
    """用 Playwright 打开页面，等待动态内容（含滚动加载）渲染完成后返回完整 HTML。

    对「执行上下文被销毁」等导航异常（站点风控强制重定向）等待后重试，并加载登录态。
    """
    last_error = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for attempt in range(FETCH_RETRIES + 1):
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    viewport={"width": 1366, "height": 768},
                    locale="zh-CN",
                    storage_state=(
                        STORAGE_STATE_PATH if os.path.exists(STORAGE_STATE_PATH) else None
                    ),
                )
                page = context.new_page()
                try:
                    # 先等 DOM 基本就绪，再滚动触发懒加载，最后尽量等网络空闲
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    _scroll_to_load_all(page)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except PlaywrightTimeoutError:
                        # 部分页面存在常驻连接（如长轮询、埋点），网络永不空闲，回退为固定等待
                        page.wait_for_timeout(2000)
                    return page.content()
                except PlaywrightTimeoutError:
                    # 超时交给上层处理，不做无谓重试
                    raise
                except PlaywrightError as e:
                    last_error = e
                    app.logger.warning(
                        "页面加载异常，%.1f 秒后重试（第 %d/%d 次）：%s",
                        FETCH_RETRY_DELAY, attempt + 1, FETCH_RETRIES, e,
                    )
                    context.close()
                    if attempt < FETCH_RETRIES:
                        time.sleep(FETCH_RETRY_DELAY)
            raise last_error
        finally:
            browser.close()


def build_table(data, ordered_headers=None):
    """把解析出的 JSON 数组规范化为表格的表头和行，单元格内容统一转成字符串。

    ordered_headers 指定时按该顺序固定列（缺失的列用空串填充）；
    否则按数据中键的出现顺序自动推导。
    """
    if ordered_headers is not None:
        headers = list(ordered_headers)
    else:
        headers = []
        for item in data:
            if isinstance(item, dict):
                for key in item:
                    if key not in headers:
                        headers.append(key)

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    if headers:
        rows = [
            [cell(item.get(h, "") if isinstance(item, dict) else "") for h in headers]
            for item in data
        ]
        return headers, rows

    # 全部是标量时，退化为单列表格
    return ["值"], [[cell(item)] for item in data]


PAGE_PARAM_NAMES = ("page", "p", "pn", "pagenum")
OFFSET_PARAM_NAMES = ("start", "offset")
# 偏移量分页每页条目数（豆瓣 Top250 等为 25）；若目标站每页条数不同，改这里即可
OFFSET_PAGE_SIZE = 25
# 已知使用偏移量分页的站点（host -> 每页条目数）：即使 URL 没带 start 也能正确翻页
OFFSET_HOSTS = {
    "movie.douban.com": 25,
}


def _is_offset_url(url):
    """URL 是否应走偏移量分页：显式带 start/offset 参数，或属于已知偏移量站点。"""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    return (
        any(k in query for k in OFFSET_PARAM_NAMES)
        or parts.netloc.lower() in OFFSET_HOSTS
    )


def paginate_url(url, n):
    """生成第 n 页的地址，兼容两类分页规律：

    1. 偏移量式：URL 含 start / offset，或属于 OFFSET_HOSTS 中的站点，值为 (n-1) * 每页条数；
    2. 页码式：page / p / pn / pagenum 参数，值为 n；都没有则追加 ?page=n。
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    host = parts.netloc.lower()

    offset_key = next((k for k in OFFSET_PARAM_NAMES if k in query), None)
    if offset_key is not None:
        query[offset_key] = [str((n - 1) * OFFSET_PAGE_SIZE)]
    elif host in OFFSET_HOSTS:
        # 已知偏移量站点但 URL 没带 start：自动补 start（豆瓣 Top250 用 start=0/25/50/75）
        query["start"] = [str((n - 1) * OFFSET_HOSTS[host])]
    else:
        page_key = next((k for k in PAGE_PARAM_NAMES if k in query), "page")
        query[page_key] = [str(n)]

    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def resolve_page_url(url, n):
    """计算抓取循环中第 n 页的 URL。

    - 第 1 页：保持用户输入原样；仅当是已知偏移量站点且未带 start 时补 start=0；
    - 后续页：交给 paginate_url 处理。
    """
    if n > 1:
        return paginate_url(url, n)
    parts = urllib.parse.urlsplit(url)
    has_offset = any(
        k in urllib.parse.parse_qs(parts.query) for k in OFFSET_PARAM_NAMES
    )
    if parts.netloc.lower() in OFFSET_HOSTS and not has_offset:
        return paginate_url(url, 1)  # 自动补上 start=0
    return url


# ===== 提取结果的统一 Schema =====
# 表格最终固定展示这些列（顺序即表格列顺序）。若业务字段变化，改这里和 KEY_MAPPING 即可。
SCHEMA_KEYS = ["名字", "链接", "评分"]
SCHEMA_KEYS_STR = "、".join(SCHEMA_KEYS)

# 大模型可能返回的英文键名 -> 中文标准键名，用于后端兜底统一
KEY_MAPPING = {
    "name": "名字",
    "title": "名字",
    "link": "链接",
    "url": "链接",
    "href": "链接",
    "rating": "评分",
    "score": "评分",
}


def normalize_items(data):
    """把大模型返回的条目统一到 SCHEMA_KEYS：英文键映射为中文、丢弃多余键。

    保证多页合并后表格列固定，不会因键名语言不一致而出现重复 / 错位列。
    """
    normalized = []
    for item in data:
        if not isinstance(item, dict):
            app.logger.warning("提取结果中存在非对象条目，已跳过：%r", item)
            continue
        row = {}
        for key, value in item.items():
            canonical = KEY_MAPPING.get(key, key)
            if canonical not in SCHEMA_KEYS:
                continue
            row.setdefault(canonical, value)
        if row:
            normalized.append(row)
    return normalized


def dedupe_items(items, seen):
    """按「链接」（无链接则用「名字」）去重，返回未出现过的条目并更新 seen。

    防止分页翻页失效（每页都返回同一批数据）时把重复内容堆进结果。
    """
    fresh = []
    for item in items:
        key = item.get("链接") or item.get("名字")
        if key and key in seen:
            app.logger.info("去重：跳过重复条目 %r", key)
            continue
        if key:
            seen.add(key)
        fresh.append(item)
    return fresh


def extract_json_with_llm(html, requirement):
    """调用 DeepSeek 从清理后的 HTML 中提取数据，返回 JSON 数组。

    返回 (data, extract_raw, extract_error)：
    - data：解析出的 JSON 数组（list），失败时为空列表；
    - extract_raw：JSON 解析失败时的原始返回文本，否则为 ""；
    - extract_error：调用失败（如未配置 KEY）时的错误信息，否则为 ""。
    """
    if not DEEPSEEK_API_KEY:
        return [], "", "未找到 DEEPSEEK_API_KEY，请在 .env 文件中设置"

    html_for_prompt = html
    if len(html_for_prompt) > MAX_HTML_CHARS:
        html_for_prompt = html_for_prompt[:MAX_HTML_CHARS] + "\n...（内容过长已截断）"

    prompt = (
        "以下是一段清理后的网页 HTML 内容：\n"
        f"{html_for_prompt}\n\n"
        "用户的数据提取需求：\n"
        f"{requirement}\n\n"
        "请根据上述需求从 HTML 中提取数据。"
        f"键名必须且只能使用：{SCHEMA_KEYS_STR}，严禁使用其他语言或其它键名。"
        "必须只返回严格的 JSON 数组格式，不要任何多余的解释和 markdown 标记。"
    )

    try:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个数据提取助手，只输出 JSON 数组。"
                        f"必须且只能使用以下 JSON 键名：{SCHEMA_KEYS_STR}。"
                        "严禁使用任何其他语言的键名（如 name、link、rating、title），"
                        "严禁增加额外的键，每个对象的键名必须完全一致。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return [], "", f"调用大模型失败：{e}"

    # 去除可能包裹的 markdown 代码块标记，便于解析
    cleaned = re.sub(r"^```(?:json)?\s*", "", content)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except Exception:
        return [], content, ""

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [], content, ""

    # 统一键名：英文键映射为中文、丢弃多余键，保证多页合并后列一致
    data = normalize_items(data)
    return data, "", ""


@app.route("/", methods=["GET", "POST"])
def index():
    url, requirement, selector, error, html = "", "", "", "", ""
    length = 0
    max_pages = 1
    selector_note = ""
    crawl_note = ""
    table_headers, table_rows, extract_raw, extract_error = [], [], "", ""

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        requirement = request.form.get("requirement", "").strip()
        selector = request.form.get("selector", "").strip()
        try:
            max_pages = int(request.form.get("max_pages", "1").strip())
        except ValueError:
            max_pages = 1
        max_pages = max(1, max_pages)

        if not url:
            error = "请输入 URL"
        else:
            all_data = []       # 各页提取出的 JSON 条目，最终合并
            seen_links = set()  # 已见过的「链接 / 名字」，用于跨页去重
            html_parts = []     # 各页清理后的 HTML
            pages_ok = 0
            pages_failed = 0

            for page_num in range(1, max_pages + 1):
                page_url = resolve_page_url(url, page_num)

                # 抓取当前页
                raw_html = None
                try:
                    raw_html = fetch_html(page_url)
                except PlaywrightTimeoutError as e:
                    pages_failed += 1
                    app.logger.warning("第 %s 页加载超时：%s", page_num, e)
                    if page_num == 1:
                        error = f"页面加载超时：{e}"
                except PlaywrightError as e:
                    pages_failed += 1
                    app.logger.warning("第 %s 页抓取失败：%s", page_num, e)
                    if page_num == 1:
                        error = f"抓取失败：{e}"

                if raw_html is not None:
                    soup = BeautifulSoup(raw_html, "html.parser")
                    # 去除 script / style / 注释，减少体积
                    for tag in soup(["script", "style"]):
                        tag.decompose()
                    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
                        comment.extract()
                    page_html = soup.prettify()
                    html_parts.append(page_html)
                    pages_ok += 1

                    # 填写了提取需求时才调用大模型
                    if requirement:
                        llm_html = page_html
                        if selector:
                            node = soup.select_one(selector)
                            if node is not None:
                                llm_html = str(node)
                                if page_num == 1:
                                    selector_note = (
                                        f"已按选择器「{selector}」定位到目标节点，"
                                        f"发送 {len(llm_html)} 字符（原 HTML {len(page_html)} 字符）"
                                    )
                            elif page_num == 1:
                                selector_note = f"未找到选择器「{selector}」，已回退为发送完整 HTML"

                        data, raw, err = extract_json_with_llm(llm_html, requirement)
                        if err:
                            # 某一页提取失败：跳过该页数据，记录日志，不影响其它页
                            app.logger.warning("第 %s 页提取失败：%s", page_num, err)
                            extract_error = err
                        elif raw:
                            app.logger.warning("第 %s 页 JSON 解析失败", page_num)
                            extract_raw = raw
                        else:
                            all_data.extend(dedupe_items(data, seen_links))

                # 页间随机休眠，模拟真人阅读停顿，防反爬（最后一页不再等待）
                if page_num < max_pages:
                    delay = random.uniform(3, 6)
                    app.logger.info("第 %s 页处理完成，随机休眠 %.2f 秒", page_num, delay)
                    time.sleep(delay)

            html = "\n\n".join(html_parts)
            length = len(html)

            if max_pages > 1:
                crawl_note = f"分页抓取完成：共 {max_pages} 页，成功 {pages_ok} 页，失败 {pages_failed} 页"
            if requirement and all_data:
                table_headers, table_rows = build_table(all_data, ordered_headers=SCHEMA_KEYS)

    return render_template_string(
        PAGE,
        url=url,
        requirement=requirement,
        selector=selector,
        max_pages=max_pages,
        error=error,
        html=html,
        length=length,
        selector_note=selector_note,
        crawl_note=crawl_note,
        table_headers=table_headers,
        table_rows=table_rows,
        extract_raw=extract_raw,
        extract_error=extract_error,
    )


@app.route("/export", methods=["POST"])
def export_csv():
    """接收前端表格数据（JSON），生成 CSV 并作为附件返回。

    前端把表头 + 行数据以 JSON POST 到这里，本路由用 utf-8-sig 编码写出 CSV，
    并通过 send_file 设置 Content-Disposition 附件响应头，浏览器直接下载。
    """
    payload = request.get_json(silent=True)
    if not payload or "headers" not in payload or "rows" not in payload:
        return jsonify({"error": "缺少表格数据"}), 400

    headers = payload.get("headers", [])
    rows = payload.get("rows", [])
    download_name = payload.get("filename", "提取结果.csv")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        # 统一转成字符串，空值写为空串，避免非文本类型写入报错
        writer.writerow(["" if cell is None else str(cell) for cell in row])

    # utf-8-sig 会在文件头写入 BOM，Excel 打开中文不乱码
    bio = io.BytesIO()
    bio.write(buf.getvalue().encode("utf-8-sig"))
    bio.seek(0)

    return send_file(
        bio,
        mimetype="text/csv",
        as_attachment=True,
        download_name=download_name,
    )


if __name__ == "__main__":
    app.run(debug=True)
