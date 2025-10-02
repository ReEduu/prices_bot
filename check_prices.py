# check_prices.py — V2.2 forense: screenshot + html + body_text + trace
import json, re, os, sys, time, math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
from playwright.sync_api import sync_playwright

STATE = Path("prices.json")
DEBUG = True  # fuerza debug SIEMPRE por ahora

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
        print("Telegram no configurado.")
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

# ---------- Extracciones ----------
# Reconoce nombres de sección típicos (puedes añadir más)
SECTION_PREFIXES = [
    r"GENERAL(?:\s+[A-Z0-9]+)?",
    r"GRADA(?:\s+(?:ORIENTE|PONIENTE|NORTE|SUR|A|B|C))?",
    r"VIP", r"PLATEA", r"PREFERENTE", r"CANCHA", r"PALCO", r"BUTACA",
]

MONEY = r"(?:MXN|MX\$|\$)\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?"

def extract_from_text_blocks(text: str) -> Dict[str, float]:
    """
    Solo trabaja con TEXTO VISIBLE (body.inner_text y tarjetas),
    nada de HTML/atributos. Empareja líneas de sección y líneas con precio.
    """
    results: Dict[str, float] = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Normaliza tildes para búsquedas robustas (opcional)
    norm = [re.sub(r"\s+", " ", ln).upper() for ln in lines]

    # 1) Caso explícito: líneas que empiezan con 'SECTION ' o 'SECCIÓN '
    for i, ln in enumerate(norm):
        msec = re.match(r"^(SECTION|SECCIÓN)\s+([A-Z0-9ÁÉÍÓÚÜÑ\-/ ]{1,40})$", ln, flags=re.IGNORECASE)
        if msec:
            sec = msec.group(2).strip()
            # busca precio en siguientes 1-4 líneas visibles
            price_val = None
            for j in range(1, 5):
                if i + j >= len(norm):
                    break
                mp = re.search(MONEY, lines[i + j], flags=re.IGNORECASE)
                if mp:
                    price_val = parse_money_to_float(mp.group(0))
                    if price_val is not None:
                        break
            if price_val is not None:
                sec = normalize_section_name(sec)
                if sec not in results or price_val < results[sec]:
                    results[sec] = price_val

    # 2) Caso implícito: líneas que parecen un nombre de sección sin la palabra "Section/Sección"
    sec_regex = re.compile(rf"^({'|'.join(SECTION_PREFIXES)})\b", flags=re.IGNORECASE)
    for i, ln in enumerate(norm):
        if sec_regex.search(ln):
            sec = normalize_section_name(lines[i])
            # busca precio cercano (misma línea o siguientes 1-4)
            price_val = None
            # misma línea
            mp0 = re.search(MONEY, lines[i], flags=re.IGNORECASE)
            if mp0:
                price_val = parse_money_to_float(mp0.group(0))
            # siguientes líneas
            if price_val is None:
                for j in range(1, 5):
                    if i + j >= len(norm):
                        break
                    mp = re.search(MONEY, lines[i + j], flags=re.IGNORECASE)
                    if mp:
                        price_val = parse_money_to_float(mp.group(0))
                        if price_val is not None:
                            break
            if price_val is not None:
                if sec not in results or price_val < results[sec]:
                    results[sec] = price_val

    return results

def extract_from_html(html: str) -> Dict[str, float]:
    """
    Filtro súper restrictivo para evitar capturar atributos aria-*.
    Solo usa HTML como ÚLTIMO recurso:
    - Requiere >Section ...< en texto (entre etiquetas).
    """
    results: Dict[str, float] = {}

    # Busca '>' Section ... '<' para asegurar que es texto visible dentro de un nodo
    for m in re.finditer(r">([^<]*\b(Section|Sección)\s+[A-Za-zÁÉÍÓÚÜÑ0-9\-/ ]{1,40}[^<]*)<", html, flags=re.IGNORECASE):
        chunk = m.group(1)
        msec = re.search(r"(?i)(Section|Sección)\s+([A-Za-zÁÉÍÓÚÜÑ0-9\-/ ]{1,40})", chunk)
        if not msec:
            continue
        sec = normalize_section_name(msec.group(2))
        # precio cercano en el mismo fragmento (o poco después en el HTML)
        window = html[m.start(): m.end() + 400]  # ventanita corta para buscar precio
        mp = re.search(MONEY, window, flags=re.IGNORECASE)
        if not mp:
            continue
        price_val = parse_money_to_float(mp.group(0))
        if price_val is None:
            continue
        if sec not in results or price_val < results[sec]:
            results[sec] = price_val

    return results


def fetch_sections_prices(pw, url: str) -> Dict[str, float]:
    user_data_dir = ".pw-session"

    ctx = pw.chromium.launch_persistent_context(
        user_data_dir,
        headless=True,   # <— pon False la primera vez para ver el navegador
        locale="es-MX",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
    )


    page = ctx.new_page()
    page.goto(url, wait_until="networkidle", timeout=120_000)

    # Cookies
    for sel in [
        'button:has-text("Aceptar")', 'button:has-text("Accept")',
        'text=/Aceptar todas|Accept all/i'
    ]:
        try:
            page.locator(sel).first.click(timeout=2000)
            break
        except:
            pass

    # Pestaña Lowest Price
    for sel in ['text=Lowest Price', 'text=Más barato', 'text=Precio más bajo']:
        try:
            page.locator(sel).first.click(timeout=1500)
            break
        except:
            pass

    # Scroll para hidratar (varias veces por si hay virtualización)
    page.wait_for_timeout(1500)
    for _ in range(12):
        try:
            page.mouse.wheel(0, 1800)
        except:
            pass
        page.wait_for_timeout(300)

    # 1) Texto visible de todo el body
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=4000)
    except:
        pass

    # 2) HTML completo
    html = page.content()

    # 3) Locators de tarjetas visibles (fallback)
    card_texts = []
    try:
        # Graba los <li> o tarjetas que contengan 'Section' (en mobile suele estar así)
        cards = page.locator("li:has-text('Section')").all()
        if not cards:
            cards = page.locator(":text('Section')").all()
        for el in cards:
            try:
                t = el.inner_text(timeout=0)
                if t:
                    card_texts.append(t)
            except:
                pass
    except:
        pass

    # --- Artefactos ---
    base = slugify(url)
    try:
        page.screenshot(path=f"debug_{base}.png", full_page=True)
    except:
        pass
    Path(f"debug_{base}.html").write_text(html, encoding="utf-8")
    Path(f"debug_{base}_text.txt").write_text(body_text + "\n\n" + "\n\n---CARDS---\n\n" + "\n\n".join(card_texts), encoding="utf-8")
    try:
        ctx.tracing.stop(path=f"trace_{base}.zip")
    except:
        pass

    # --- Parseo combinado ---
    results = {}
    # a) por el texto visible
    a = extract_from_text_blocks(body_text + "\n\n" + "\n\n".join(card_texts))
    results.update(a)
    # b) por el HTML directo
    b = extract_from_html(html)
    for k, v in b.items():
        if k not in results or v < results[k]:
            results[k] = v

    ctx.close(); 
    return results

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

    prev_state = load_state()
    new_state: Dict[str, Dict[str, float]] = dict(prev_state)
    daily_reports: List[str] = []

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%Y-%m-%d %H:%M")

    with sync_playwright() as pw:
        for url in urls:
            print(f"\n=== Revisando {url} ===")
            sections = fetch_sections_prices(pw, url)

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
            snapshot = "\n".join([f"• {sec}: {price:.2f}" for sec, price in sorted(sections.items())]) or "Sin precios detectados"
            if not (ups or downs or others):
                lines.append("✅ Sin cambios respecto al registro previo.")
            lines.append("Precios actuales:\n" + snapshot)
            daily_reports.append(header + ("\n\n".join(lines)))

    save_state(new_state)
    notify_telegram("\n\n".join(daily_reports))
    print("\n\n" + "\n\n".join(daily_reports) + "\n")

if __name__ == "__main__":
    main()
