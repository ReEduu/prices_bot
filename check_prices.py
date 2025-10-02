# check_prices.py — V2.1 (debug + artefactos + robustez carga)
import json, re, os, sys, time, math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
from playwright.sync_api import sync_playwright

STATE = Path("prices.json")
DEBUG = os.getenv("DEBUG", "0") == "1"

# ---------- Estado ----------
def load_state() -> Dict[str, Dict[str, float]]:
    return json.loads(STATE.read_text()) if STATE.exists() else {}

def save_state(d: Dict[str, Dict[str, float]]):
    STATE.write_text(json.dumps(d, indent=2, ensure_ascii=False))

# ---------- Telegram ----------
def notify_telegram(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram no configurado (faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=data, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"Error enviando Telegram: {e}")

# ---------- Utilidades ----------
MONEY = r"(?:MXN|MX\$|\$)\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?"

def parse_money_to_float(s: str) -> Optional[float]:
    n = re.sub(r"[^\d.,]", "", s).replace(",", "")
    try:
        return round(float(n), 2)
    except:
        return None

def normalize_section_name(s: str) -> str:
    s = re.sub(r"^\s*Section\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()

def slugify(url: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", url.split("/")[-1]).strip("-")
    return base or "event"

# ---------- Extracción ----------
def extract_sections_and_prices_from_html(html_text: str) -> Dict[str, float]:
    results: Dict[str, float] = {}
    parts = re.split(r"(?i)(?=Section\s+)", html_text)
    for part in parts:
        msec = re.search(r"(?i)Section\s+([A-Za-zÁÉÍÓÚÜÑ0-9\s\-\/]+?)\b", part)
        if not msec:
            continue
        section = normalize_section_name(msec.group(1))
        mp = re.search(MONEY, part)
        if not mp:
            continue
        price_val = parse_money_to_float(mp.group(0))
        if price_val is None:
            continue
        if section not in results or price_val < results[section]:
            results[section] = price_val
    return results

def fetch_sections_prices(pw, url: str) -> Dict[str, float]:
    browser = pw.chromium.launch()
    ctx = browser.new_context(
        locale="es-MX",
        user_agent=("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"),
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9"},
    )
    page = ctx.new_page()
    page.goto(url, wait_until="networkidle", timeout=120_000)

    # Intenta aceptar cookies si aparece
    for sel in [
        'button:has-text("Accept")', 'button:has-text("Aceptar")',
        'text=/Accept all|Aceptar todas/i'
    ]:
        try:
            page.locator(sel).first.click(timeout=2000)
            break
        except:
            pass

    # Asegurar pestaña Lowest Price
    for sel in ['text=Lowest Price', 'text=Más barato', 'text=Precio más bajo']:
        try:
            page.locator(sel).first.click(timeout=1500)
            break
        except:
            pass

    # Dar tiempo a hidratar y hacer scroll para cargar listas perezosas
    page.wait_for_timeout(2000)
    for _ in range(8):
        try:
            page.mouse.wheel(0, 2000)
        except:
            pass
        page.wait_for_timeout(500)

    html = page.content()

    # Artefactos debug
    if DEBUG:
        base = slugify(url)
        try:
            page.screenshot(path=f"debug_{base}.png", full_page=True)
        except:
            pass
        Path(f"debug_{base}.html").write_text(html, encoding="utf-8")

    ctx.close(); browser.close()
    return extract_sections_and_prices_from_html(html)

# ---------- Diff ----------
def diff_prices(old: Dict[str, float], new: Dict[str, float]):
    ups, downs, others = [], [], []
    all_sections = sorted(set(old.keys()) | set(new.keys()))
    for sec in all_sections:
        ov = old.get(sec)
        nv = new.get(sec)
        if ov is None and nv is not None:
            others.append(f"• {sec}: nuevo {nv:.2f}")
        elif ov is not None and nv is None:
            others.append(f"• {sec}: sin disponibilidad (antes {ov:.2f})")
        elif ov is not None and nv is not None:
            if nv > ov:
                ups.append(f"• {sec}: {ov:.2f} → {nv:.2f} (+{nv-ov:.2f})")
            elif nv < ov:
                downs.append(f"• {sec}: {ov:.2f} → {nv:.2f} (−{ov-nv:.2f})")
    return ups, downs, others

# ---------- Main ----------
def main():
    urls = [u.strip() for u in Path("urls.txt").read_text().splitlines() if u.strip()]
    if not urls:
        print("urls.txt vacío"); sys.exit(0)

    prev_state = load_state()  # {url: {section: price}}
    new_state: Dict[str, Dict[str, float]] = dict(prev_state)
    daily_reports: List[str] = []

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%Y-%m-%d %H:%M")

    with sync_playwright() as pw:
        for url in urls:
            print(f"\n=== Revisando {url} ===")
            sections = fetch_sections_prices(pw, url)

            # LOG visible en consola SIEMPRE
            print(f"Secciones detectadas: {len(sections)}")
            for sec, price in sorted(sections.items()):
                print(f"  - {sec}: {price:.2f}")

            old_sections = prev_state.get(url, {})
            new_state[url] = sections

            ups, downs, others = diff_prices(old_sections, sections)

            header = f"🎟️ Cambios detectados ({date_str})\n{url}\n"
            lines = []

            if ups:   lines.append("⬆️ Subidas:\n" + "\n".join(ups))
            if downs: lines.append("⬇️ Bajas:\n" + "\n".join(downs))
            if others:lines.append("ℹ️ Novedades:\n" + "\n".join(others))

            # Siempre adjuntamos snapshot actual
            snapshot = "\n".join([f"• {sec}: {price:.2f}" for sec, price in sorted(sections.items())]) or "Sin precios detectados"
            if not (ups or downs or others):
                lines.append("✅ Sin cambios respecto al registro previo.")
            lines.append("Precios actuales:\n" + snapshot)

            report = header + ("\n\n".join(lines))
            daily_reports.append(report)

    save_state(new_state)
    # Notificación Telegram SIEMPRE
    notify_telegram("\n\n".join(daily_reports))
    print("\n\n" + "\n\n".join(daily_reports) + "\n")

if __name__ == "__main__":
    main()
