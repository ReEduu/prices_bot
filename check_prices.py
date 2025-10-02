# check_prices.py  — V2 (por sección) + siempre notifica por Telegram
import json, re, os, sys, time, math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
from playwright.sync_api import sync_playwright

STATE = Path("prices.json")

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
    data = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
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
    # Limpia "Section " y dobles espacios; deja mayúsculas.
    s = re.sub(r"^\s*Section\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()

# ---------- Extracción por sección ----------
def extract_sections_and_prices_from_html(html_text: str) -> Dict[str, float]:
    """
    Estrategia robusta:
      1) Busca bloques que contengan 'Section <NOMBRE>' cerca de un precio 'MX$...'.
      2) Empareja sección -> primer precio encontrado en el bloque.
    """
    results: Dict[str, float] = {}

    # Heurística: split por 'Section ' para aislar tarjetas/entradas
    parts = re.split(r"(?i)(?=Section\s+)", html_text)
    for part in parts:
        # nombre de sección
        msec = re.search(r"(?i)Section\s+([A-Za-zÁÉÍÓÚÜÑ0-9\s\-\/]+?)\b", part)
        if not msec:
            continue
        section = normalize_section_name(msec.group(1))

        # precio más cercano
        mp = re.search(MONEY, part)
        if not mp:
            continue
        price_val = parse_money_to_float(mp.group(0))
        if price_val is None:
            continue

        # Guarda el menor precio visto para la sección (Lowest Price tab)
        if section not in results or price_val < results[section]:
            results[section] = price_val

    return results

def fetch_sections_prices(pw, url: str) -> Dict[str, float]:
    browser = pw.chromium.launch()
    ctx = browser.new_context(
        locale="es-MX",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
        ),
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9"}
    )
    page = ctx.new_page()
    page.goto(url, wait_until="load", timeout=120_000)
    time.sleep(6)  # deja hidratar la SPA

    # Captura HTML completo (incluye texto visible + JSON embebido)
    html = page.content()
    ctx.close(); browser.close()

    return extract_sections_and_prices_from_html(html)

# ---------- Diff ----------
def diff_prices(old: Dict[str, float], new: Dict[str, float]) -> Tuple[List[str], List[str], List[str]]:
    """Devuelve (subidas, bajadas, nuevas/eliminadas) como mensajes ya formateados."""
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
    now = datetime.now(timezone.utc).astimezone()  # hora local del runner
    date_str = now.strftime("%Y-%m-%d %H:%M")

    with sync_playwright() as pw:
        for url in urls:
            print(f"Revisando {url} …")
            sections = fetch_sections_prices(pw, url)
            old_sections = prev_state.get(url, {})
            new_state[url] = sections

            ups, downs, others = diff_prices(old_sections, sections)

            # Construye mensaje por URL (siempre)
            if ups or downs or others or not old_sections:
                header = f"🎟️ Cambios detectados ({date_str})\n{url}\n"
                lines = []
                if ups:   lines.append("⬆️ Subidas:\n" + "\n".join(ups))
                if downs: lines.append("⬇️ Bajas:\n" + "\n".join(downs))
                if others:lines.append("ℹ️ Novedades:\n" + "\n".join(others))
                if not (ups or downs or others) and not old_sections:
                    lines.append("Primer registro tomado. (No hay comparación previa)")
                report = header + ("\n\n".join(lines))
            else:
                # Sin cambios -> resume precios actuales
                header = f"✅ Sin cambios ({date_str})\n{url}\n"
                current = "\n".join([f"• {sec}: {price:.2f}" for sec, price in sorted(sections.items())]) or "Sin precios detectados"
                report = header + current

            daily_reports.append(report)

    save_state(new_state)

    # Telegram SIEMPRE (1 mensaje con todas las URLs)
    notify_telegram("\n\n".join(daily_reports))
    print("\n\n".join(daily_reports))

if __name__ == "__main__":
    main()
