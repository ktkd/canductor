"""E2E: web UI check through the Selenium grid (docker selenium/standalone-chrome).

Prereqs:
  * the web service is running:
      canductor web --host 0.0.0.0 --dev hs=/dev/ttyUSB0 --dev ms=/dev/ttyUSB1 --port 8111
      (or for the editor: canductor-editor --scenarios <dir> --host 0.0.0.0 --port 8111)
  * the grid container:
      docker run -p 4444:4444 --add-host=host.docker.internal:host-gateway           -e SE_CB=false selenium/standalone-chrome:153.0
      (the browser runs inside the container, so the page URL must be reachable from there)

Optional overrides (env):
  SE_HOST      selenium grid base url   (default http://localhost:4444)
  PAGE_URL     URL the browser opens    (default PAGE_URL)
  LOCAL_URL    URL for direct HTTP calls from the host (default LOCAL_URL)

Run:  python3 tests/e2e_ui.py
"""
"""E2E: canductor web UI (core + session console) via Selenium grid."""
import json
import os
import sys
import time
from typing import Callable
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException


def console_log(driver: webdriver.Remote) -> list:
    try:
        return driver.execute_script("return window.__console || []")
    except WebDriverException:
        return []


PAGE_URL = os.environ.get("PAGE_URL", "http://host.docker.internal:8111")
LOCAL_URL = os.environ.get("LOCAL_URL", "http://127.0.0.1:8111")


def main() -> None:
    opt = webdriver.ChromeOptions()
    driver = webdriver.Remote(os.environ.get("SE_HOST", "http://localhost:4444").rstrip("/"), options=opt)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        window.__dlgLog = []; window.__dlgConfirm = true; window.__dlgVal = undefined;
        window.alert = (m) => { window.__dlgLog.push(['alert', String(m)]); };
        window.confirm = (m) => { window.__dlgLog.push(['confirm', String(m)]); return !!window.__dlgConfirm; };
        window.prompt = (m, d) => { window.__dlgLog.push(['prompt', String(m), String(d == null ? '' : d)]); return (window.__dlgVal === undefined ? (d || '') : window.__dlgVal); };
        window.__console = [];
        const _fmt = (x) => { try { return typeof x === 'object' ? JSON.stringify(x) : String(x); } catch(_) { return String(x); } };
        for (const m of ['log','warn','error','info']) {
          const orig = console[m].bind(console);
          console[m] = (...a) => { try { window.__console.push([m, a.map(_fmt).join(' ')]) } catch(_) {} ; orig(...a); };
        }
        window.addEventListener('error', (e) => { try { window.__console.push(['uncaught', e.message + ' @ ' + (e.filename||'?') + ':' + e.lineno]); } catch(_) {} });
        window.addEventListener('unhandledrejection', (e) => { try { window.__console.push(['unhandledrejection', _fmt(e.reason)]); } catch(_) {} });
    """})
    results: list[tuple[str, str]] = []

    def check(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            results.append(("PASS", name))
        except Exception as e:
            results.append(("FAIL", f"{name} -- {type(e).__name__}: {e}"))

    driver.set_window_size(1600, 1000)

    # --- 1. page load -------------------------------------------------------
    driver.get(PAGE_URL + "/")

    def t():
        w = WebDriverWait(driver, 10)
        w.until(EC.presence_of_element_located((By.ID, "sess-badge")))
        badge = driver.find_element(By.ID, "sess-badge")
        if badge.text == "done":
            # hub persists the finished session; the UI offers a local reset
            # ("New session" — full re-render incl. the status badge)
            driver.find_element(By.ID, "b-new").click()
            w.until(EC.presence_of_element_located((By.ID, "b-startsess")))
            assert driver.find_element(By.ID, "sess-badge").text == "idle", "badge must follow the reset to idle"
        else:
            # 'idle' (fresh) or 'error' (failed session: the start form is shown
            # with the error message — no reset needed)
            if badge.text not in ("idle", "error"):
                raise AssertionError(f"badge is {badge.text!r}")
        w.until(EC.presence_of_element_located((By.ID, "b-startsess")))
        for a in ("live-hs", "live-ms"):
            if not driver.find_element(By.ID, a):
                raise AssertionError(f"no live panel {a}")
        # open button is visible whenever the device is not configured
        dev_state = driver.execute_script(
            "return await fetch('/devices').then(r => r.json());")
        for b in ("lopen-hs", "lopen-ms"):
            btn = driver.find_element(By.ID, b)
            a = b.rsplit("-", 1)[1]
            st = dev_state[a]["state"]
            shown = "show" in btn.get_attribute("class")
            if st in ("init", "error") and not shown:
                raise AssertionError(f"{b} hidden but device state = {st}")
    check("page loads: idle badge, HS+MS live panels, open buttons", t)

    # --- 2. live stats row appears (SSE stream connected) -------------------
    def t():
        w = WebDriverWait(driver, 10)
        w.until(EC.text_to_be_present_in_element((By.ID, "lstat-hs"), "frames"))
    check("SSE stream connected (live stats update)", t)

    # --- 3. scenario dropdown + steps render --------------------------------
    def t():
        w = WebDriverWait(driver, 10)
        sel = w.until(EC.presence_of_element_located((By.ID, "ss-name")))
        opts = [o.text for o in sel.find_elements(By.TAG_NAME, "option")]
        sel = w.until(EC.presence_of_element_located((By.ID, "ss-name")))
        opts = [o.text for o in sel.find_elements(By.TAG_NAME, "option")]
        if "bench_quick" not in opts or "mazda_swap" not in opts:
            raise AssertionError(f"scenario options: {opts}")
        driver.execute_script(
            "const s = document.getElementById('ss-name'); s.value = 'bench_quick';"
            " s.dispatchEvent(new Event('change', {bubbles: true}));")
        w2 = WebDriverWait(driver, 10)
        w2.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#steps-list .step"))
        n = len(driver.find_elements(By.CSS_SELECTOR, "#steps-list .step"))
        if n < 3:
            raise AssertionError(f"expected several bench steps, got {n}")
    check("scenario select renders step list (bench_quick)", t)

    # --- 4. start a session via UI; card appears; answer start --------------
    def t():
        btn = driver.find_element(By.ID, "b-startsess")
        btn.click()
        w = WebDriverWait(driver, 20)
        w.until(EC.presence_of_element_located((By.ID, "b-start")))   # the card
        if driver.find_element(By.ID, "sess-badge").text not in ("waiting — confirm", "waiting"):
            raise AssertionError("not waiting state")
        driver.find_element(By.ID, "f-coolant").send_keys("90")
        driver.find_element(By.ID, "f-air").send_keys("25")
        driver.find_element(By.ID, "f-comment").send_keys("selenium e2e")
        # regression: while waiting on the start button the 5s rsync re-paints the
        # panel; the in-flight card input must survive the repaint (no value wipe)
        time.sleep(6.5)
        c_val = driver.find_element(By.ID, "f-coolant").get_attribute("value")
        cm_val = driver.find_element(By.ID, "f-comment").get_attribute("value")
        if c_val != "90" or cm_val != "selenium e2e":
            raise AssertionError(f"waiting-card form wiped by re-render: coolant={c_val!r} comment={cm_val!r}")
        driver.find_element(By.ID, "b-start").click()
    check("start session -> card -> start test (input survives 5s rsync repaint)", t)

    # --- 5. device absent -> session ends in 'error' with a visible message -------
    def t():
        w = WebDriverWait(driver, 30)
        w.until(lambda d: d.find_element(By.ID, "sess-badge").text == "error")
        crit = driver.find_elements(By.CSS_SELECTOR, "#panel .critical")
        if not crit:
            raise AssertionError("no error message in the panel")
        import urllib.request, json as _j
        st = _j.loads(urllib.request.urlopen(LOCAL_URL + "/api/session/status").read())
        le = st.get("last_error") or ""
        if not le or ("CoreError" not in le and "bus" not in le):
            raise AssertionError(f"last_error={le!r} state={st.get('session_state')}")
        print("   [info] final state:", st.get("session_state"), "last_error:", le)
    check("worker fails cleanly: 'error' badge + panel message + hub last_error", t)

    # --- 6. logs section renders past sessions ------------------------------
    def t():
        w = WebDriverWait(driver, 10)
        w.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#logs-body details"))
        n = len(driver.find_elements(By.CSS_SELECTOR, "#logs-body details"))
        if not n:
            raise AssertionError("no session listings")
        print("   [info] sessions listed:", n)
    check("logs section renders", t)
    # the most recent session is open by default; expand the second one
    def t():
        dets = driver.find_elements(By.CSS_SELECTOR, "#logs-body details")
        if len(dets) < 2:
            return
        det = dets[1]
        if not det.get_attribute("open"):
            det.find_element(By.TAG_NAME, "summary").click()
        w = WebDriverWait(driver, 10)
        w.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#logs-body details[open] .log-table"))
    check("logs: second session expands with file table", t)

    # --- 7. live TX validation (invalid + valid inputs) ----------------------
    def t():
        driver.find_element(By.ID, "txid-hs").clear(); driver.find_element(By.ID, "txid-hs").send_keys("zz")
        driver.find_element(By.ID, "txsend-hs").click()
        w = WebDriverWait(driver, 15)
        w.until(lambda d: d.find_element(By.ID, "txf-hs").text.strip() != "")
        v1 = driver.find_element(By.ID, "txf-hs").text
        print("   [info] invalid-id fill:", v1)
        driver.find_element(By.ID, "txid-hs").clear(); driver.find_element(By.ID, "txid-hs").send_keys("0x123")
        driver.find_element(By.ID, "txdata-hs").clear(); driver.find_element(By.ID, "txdata-hs").send_keys("0102")
        driver.find_element(By.ID, "txsend-hs").click()
        w2 = WebDriverWait(driver, 15)
        w2.until(lambda d: d.find_element(By.ID, "txf-hs").text.startswith("TX "))
        txt = driver.find_element(By.ID, "txf-hs").text
        if "ok" not in txt and "error" not in txt:
            raise AssertionError(f"TX fill: {txt}")
        print("   [info] TX fill:", txt)
    check("TX one-shot send: id validation + send from init state", t)

    # --- 8. saved TX preset round-trip ---------------------------------------
    def t():
        driver.find_element(By.ID, "txid-hs").clear(); driver.find_element(By.ID, "txid-hs").send_keys("7f1")
        driver.find_element(By.ID, "txdata-hs").clear(); driver.find_element(By.ID, "txdata-hs").send_keys("aa")
        driver.find_element(By.ID, "txsave-hs").click()
        w = WebDriverWait(driver, 10)
        w.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#txp-hs [data-txs]"))
    check("TX presets: save appears in quick list", t)

    # --- 9. row-limit control persists to localStorage ------------------------
    def t():
        driver.execute_script(
            "const el = document.getElementById('lrlim-hs'); el.value = '250';"
            " el.dispatchEvent(new Event('change')); return el.value;")
        v = driver.execute_script("return localStorage.getItem('canductor.rows.hs')")
        if v != "250":
            raise AssertionError(f"localStorage rows = {v!r}")
        shown = driver.execute_script("return document.getElementById('lrlim-hs').value")
        if shown != "250":
            raise AssertionError(f"input shows {shown!r}")
    check("live row limit control -> localStorage", t)

    # --- 10. console health ---------------------------------------------------
    def t():
        logs = console_log(driver)
        errs = [l for l in logs if l[0] in ("error", "uncaught", "unhandledrejection")]
        if errs:
            raise AssertionError("browser errors: " + json.dumps(errs)[:600])
        print("   [info] console entries:", len(logs))
    check("no JS console errors", t)

    for status, name in results:
        print(f"{status:5s} {name}")
    failed = [r for r in results if r[0] == "FAIL"]
    driver.quit()
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()