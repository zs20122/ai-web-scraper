"""导出网站登录态（Cookie / storage_state），供 app.py 在需要登录的站点使用。

用法：
1. 运行（可指定目标站点 URL，不指定则用 example.com）：
       python save_storage_state.py https://www.example.com
2. 在弹出的有头浏览器里手动登录目标网站；
3. 登录成功后回到终端按回车；
4. 脚本把登录态保存为 storage_state.json，之后 app.py 抓取时会自动加载。

注：登录态一般有有效期，失效后重新运行本脚本刷新即可。
"""
import sys

from playwright.sync_api import sync_playwright

OUTPUT = "storage_state.json"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://www.example.com/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        user_agent=UA,
        viewport={"width": 1366, "height": 768},
        locale="zh-CN",
    )
    page = context.new_page()
    page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
    print(f">>> 请在浏览器中完成登录（{TARGET}）。登录成功后回到本终端按回车继续……")
    input()
    context.storage_state(path=OUTPUT)
    print(f">>> 登录态已保存到 {OUTPUT}，之后抓取该站点会自动加载。")
    browser.close()
