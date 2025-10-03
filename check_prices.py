# check_prices.py — V3: price mode + availability mode (sold-out watcher)
import json, re, os, time, random
from pathlib import Path
from typing import Dict, List, Tuple
import requests
from playwright.sync_api import sync_playwright

STATE = Path("prices.json")

# ------------- Utils -------------
def load_state() -> Dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}

def save_state(d: Dict):
    STATE.write_text(json.dumps(d, indent=2, ensure_ascii=False))

def notify_telegram(msg: str):
    tok = os.getenv("TELEGRAM_BOT_TOKEN"); chat = os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("Telegram no configurado.")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": msg, "disable_web_page_preview": True},
                      timeout=25).raise_for_status()
    except Exception as e:
        print("Error Telegram:", e)

def parse_money(s: str) -> float | None:
    n = re.sub(r"[^\d.,]", "", s).replace(",", "")
    try: return round(float(n), 2)
    except: return None

MONEY = r"(?:MXN|MX\$|\$)\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?"
PRICE_RE = re.compile(MONEY, re.I)

# nombres de sección típicos
SEC_IMPL = re.compile(
    r"^(GENERAL(?:\s+[A-Z0-9]+)?|GRADA(?:\s+(?:ORIENTE|PONIENTE|NORTE|SUR|[A-Z]))|VIP|PLATEA|PREFERENTE|CANCHA|PALCO|BUTACA)\b",
    re.I
)
SEC_EXPL = re.compile(r"^(?:SECTION|SECCIÓN)\s+([A-ZÁÉÍÓÚÜÑ0-9\-\/ ]{1,40})$", re.I)

AVAIL_NO_PATTERNS = re.compile(
    r"(?:\b0\s+Sin resultados\b|No hay boletos disponibles|Sin boletos|Sold out|Agotado)",
    re.I
)

def normalize_section(s: str) -> str:
    s = re.sub(r"^\s*(SECTION|SECCIÓN)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()

def extract_sections_from_text(text_visible: str) -> Dict[str, float]:
    lines = [ln.strip() for ln in text_visible.splitlines() if ln.strip()]
    res: Dict[str, float] = {}
    for i, raw in enumerate(lines):
        m = SEC_EXPL.match(raw)
        if m:
            sec = normalize_section(m.group(0))
            price = None
            for j in range(1, 5):
                if i + j >= len(lines): break
                mp = PRICE_RE.search(lines[i + j]); 
                if mp:
                    price = parse_money(mp.group(0)); break
            if price is not None: res[sec] = min(price, res.get(sec, price))
    for i, raw in enumerate(lines):
        if not SEC_IMPL.search(raw): 
            continue
        sec = normalize_section(raw)
        price = None
        mp0 = PRICE_RE.search(raw)
        if mp0: price = parse_money(mp0.group(0))
        if price is None:
            for j in range(1, 5):
                if i + j >= len(lines): break
                mp = PRICE_RE.search(lines[i + j])
                if mp: price = parse_money(mp.group(0)); break
        if price is not None: res[sec] = min(price, res.get(sec, price))
    return res

def slugify(url: str) -> str:
    import re
    base = re.sub(r"[^a-zA-Z0-9]+", "-", url.split("/")[-1]).strip("-")
    return base or "event"

# ------------- Playwright -------------
def fetch_page_text(url: str) -> Tuple[str, str]:
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            ".pw-session",
            headless=True,  # primera vez pon False para login/cookies
            locale="es-MX",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            extra_http_headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"},
        )
        page = ctx.new_page()
        # espera aleatoria corta para humanizar
        time.sleep(random.uniform(1.5, 4.0))
        page.goto(url, wait_until="networkidle", timeout=120_000)
        # cookies (best effort)
        for sel in ['button:has-text("Aceptar")','button:has-text("Accept")','text=/Aceptar todas|Accept all/i']:
            try: page.locator(sel).first.click(timeout=1500); break
            except: pass
        # pestaña "Lowest Price"
        for sel in ['text=Lowest Price','text=Más barato','text=Precio más bajo']:
            try: page.locator(sel).first.click(timeout=1200); break
            except: pass
        # scroll para hidratar
        page.wait_for_timeout(1200)
        for _ in range(10):
            try: page.mouse.wheel(0, 1600)
            except: pass
            page.wait_for_timeout(250)
        body = ""
        try: body = page.locator("body").inner_text(timeout=4000)
        except: pass
        html = page.content()
        # evidencia opcional
        base = slugify(url)
        try: page.screenshot(path=f"debug_{base}.png", full_page=True)
        except: pass
        Path(f"debug_{base}.html").write_text(html, encoding="utf-8")
        Path(f"debug_{base}_text.txt").write_text(body, encoding="utf-8")
        ctx.close()
        return body, html

# ------------- Availability check -------------
def check_availability(url: str) -> Tuple[bool, Dict[str,float]]:
    body, html = fetch_page_text(url)
    # 1) heurística negativa: textos de "sin resultados"
    if AVAIL_NO_PATTERNS.search(body) or AVAIL_NO_PATTERNS.search(html):
        sections = extract_sections_from_text(body)  # por si hay algo raro
        available = bool(sections)  # si a pesar del texto hay secciones, gana secciones
        return available, sections
    # 2) si hay secciones o precios visibles, lo consideramos disponible
    sections = extract_sections_from_text(body)
    if sections: 
        return True, sections
    # 3) como último recurso: ¿hay un precio suelto visible?
    if re.search(MONEY, body, re.I): 
        return True, {}  # disponible pero sin mapear secciones
    return False, {}

# ------------- Main -------------
def parse_urls(file: Path) -> List[Tuple[str, str]]:
    lines = [ln.strip() for ln in file.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    parsed = []
    for ln in lines:
        if ln.lower().startswith("availability:"):
            parsed.append(("availability", ln.split(":",1)[1].strip()))
        else:
            parsed.append(("price", ln))
    return parsed

def main():
    urls = parse_urls(Path("urls.txt"))
    if not urls:
        print("urls.txt vacío"); return
    state = load_state()
    reports = []

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone()
    ts = now.strftime("%Y-%m-%d %H:%M")

    for mode, url in urls:
        print(f"\n=== Revisando ({mode}) {url} ===")
        per_url = state.get(url, {})
        if mode == "availability":
            available, sections = check_availability(url)
            before = bool(per_url.get("available", False))
            state[url] = {"mode": "availability", "available": available, "sections": sections}
            if available != before:
                if available:
                    lines = ["🎫 ¡DISPONIBLES! ({} )".format(ts), url]
                    if sections:
                        snap = "\n".join([f"• {s}: {p:.2f}" for s,p in sorted(sections.items())])
                        lines.append("Secciones detectadas:\n" + snap)
                    reports.append("\n".join(lines))
                else:
                    reports.append(f"❌ Volvió a agotarse ({ts})\n{url}")
            else:
                status = "Disponible" if available else "Agotado"
                reports.append(f"ℹ️ Sin cambios ({ts}) — {status}\n{url}")

        else:  # price mode (tu lógica actual resumida)
            body, _ = fetch_page_text(url)
            sections = extract_sections_from_text(body)
            old = per_url.get("sections", {})
            ups, downs, others = [], [], []
            allsecs = sorted(set(old.keys()) | set(sections.keys()))
            for sec in allsecs:
                ov, nv = old.get(sec), sections.get(sec)
                if ov is None and nv is not None: others.append(f"• {sec}: nuevo {nv:.2f}")
                elif ov is not None and nv is None: others.append(f"• {sec}: sin disponibilidad (antes {ov:.2f})")
                elif ov is not None and nv is not None:
                    if nv > ov: ups.append(f"• {sec}: {ov:.2f} → {nv:.2f} (+{nv-ov:.2f})")
                    elif nv < ov: downs.append(f"• {sec}: {ov:.2f} → {nv:.2f} (−{ov-nv:.2f})")
            state[url] = {"mode":"price","sections":sections, "available": bool(sections)}
            header = f"🎟️ Cambios detectados ({ts})\n{url}\n"
            lines = []
            if ups:   lines.append("⬆️ Subidas:\n" + "\n".join(ups))
            if downs: lines.append("⬇️ Bajas:\n" + "\n".join(downs))
            if others:lines.append("ℹ️ Novedades:\n" + "\n".join(others))
            if not (ups or downs or others):
                lines.append("✅ Sin cambios respecto al registro previo.")
            snap = "\n".join([f"• {s}: {p:.2f}" for s,p in sorted(sections.items())]) or "Sin precios detectados"
            lines.append("Precios actuales:\n" + snap)
            reports.append(header + "\n\n".join(lines))

    save_state(state)
    notify_telegram("\n\n".join(reports))
    print("\n\n".join(reports))

if __name__ == "__main__":
    main()
