from playwright.sync_api import sync_playwright
import threading

GLOBAL_BROWSER = None
GLOBAL_PW = None

def init_browser():
    global GLOBAL_BROWSER, GLOBAL_PW
    GLOBAL_PW = sync_playwright().start()
    GLOBAL_BROWSER = GLOBAL_PW.chromium.launch_persistent_context(
        user_data_dir="./test_profile",
        headless=True
    )

def use_browser():
    try:
        p = GLOBAL_BROWSER.new_page()
        p.goto("https://example.com")
        print("Success:", p.title())
    except Exception as e:
        print("Failed:", e)

init_browser()
t = threading.Thread(target=use_browser)
t.start()
t.join()
