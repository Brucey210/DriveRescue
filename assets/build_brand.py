"""Draws the Drive Rescue logo and splash screens.

    python3 assets/build_brand.py

The mark is a lifebuoy that is also a disk platter: a ring with a spindle hole. SVG files are written
first and everything else (PNG for the boot screens, the wallpaper) is rendered from them with a
headless browser, so there is one source for the artwork. Needs Playwright with Chromium.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "brand")
TEAL, TEAL_DARK, AMBER = "#12968F", "#0A5C58", "#E8A63A"

# The mark on a rounded tile. 128 x 128 units.
MARK_BODY = f"""
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#17A9A1"/><stop offset="1" stop-color="#0A6560"/></linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="30" fill="url(#tile)"/>
  <circle cx="64" cy="64" r="33" fill="none" stroke="#FFFFFF" stroke-width="24"/>
  <circle cx="64" cy="64" r="33" fill="none" stroke="{AMBER}" stroke-width="24" stroke-dasharray="25.9 25.9" transform="rotate(22.5 64 64)"/>
  <circle cx="64" cy="64" r="21" fill="{TEAL_DARK}"/>
  <circle cx="64" cy="64" r="6" fill="#FFFFFF"/>
  <circle cx="64" cy="64" r="2.2" fill="{TEAL_DARK}"/>
"""


def svg(w, h, body, view=None):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{view or f"0 0 {w} {h}"}">'
            f'{body}</svg>\n')


def mark_svg():
    return svg(128, 128, MARK_BODY)


def lockup_svg(dark_text):
    ink = "#E9F2F2" if dark_text else "#14212B"
    body = (f'<g>{MARK_BODY}</g>'
            f'<text x="148" y="82" font-family="Inter, system-ui, Segoe UI, sans-serif" font-size="54" font-weight="700" fill="{ink}" letter-spacing="-0.5">Drive Rescue</text>')
    return svg(520, 128, body)


def splash_svg(w, h, starting=True):
    s = h / 1080.0  # everything is drawn for 1080 high and scaled
    cx, cy = w / 2, h / 2
    body = f"""
  <defs>
    <radialGradient id="bg" cx="50%" cy="42%" r="75%"><stop offset="0" stop-color="#0F4A47"/><stop offset="0.55" stop-color="#082B2A"/><stop offset="1" stop-color="#051716"/></radialGradient>
    <radialGradient id="glow" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#2FD0C6" stop-opacity="0.28"/><stop offset="1" stop-color="#2FD0C6" stop-opacity="0"/></radialGradient>
  </defs>
  <rect width="{w}" height="{h}" fill="url(#bg)"/>
  <circle cx="{cx}" cy="{cy - 70 * s}" r="{330 * s}" fill="url(#glow)"/>
  <g transform="translate({cx - 130 * s} {cy - 330 * s}) scale({260 * s / 128})">{MARK_BODY}</g>
  <text x="{cx}" y="{cy + 10 * s}" text-anchor="middle" font-family="Inter, system-ui, Segoe UI, sans-serif" font-size="{92 * s}" font-weight="700" fill="#F2F8F8" letter-spacing="{-1.5 * s}">Drive Rescue</text>
  <text x="{cx}" y="{cy + 80 * s}" text-anchor="middle" font-family="Inter, system-ui, Segoe UI, sans-serif" font-size="{34 * s}" font-weight="400" fill="#9CC9C6">Rescue your files. Clone your drive.</text>
"""
    if starting:
        body += (f'  <text x="{cx}" y="{h - 90 * s}" text-anchor="middle" font-family="Inter, system-ui, Segoe UI, sans-serif" '
                 f'font-size="{28 * s}" font-weight="500" fill="#6FA9A5" letter-spacing="{4 * s}">STARTING…</text>\n')
    return svg(w, h, body)


def main():
    os.makedirs(OUT, exist_ok=True)
    files = {
        "mark.svg": mark_svg(),
        "logo-on-light.svg": lockup_svg(False),
        "logo-on-dark.svg": lockup_svg(True),
        "splash-1920x1080.svg": splash_svg(1920, 1080),
        "splash-1024x768.svg": splash_svg(1024, 768),
        "splash-640x480.svg": splash_svg(640, 480),
        "wallpaper-1920x1080.svg": splash_svg(1920, 1080, starting=False),
    }
    for name, text in files.items():
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed, so only the SVG files were written.")
        return
    renders = [("mark.svg", "mark-512.png", 512, 512, True), ("mark.svg", "mark-256.png", 256, 256, True),
               ("mark.svg", "mark-64.png", 64, 64, True), ("mark.svg", "mark-32.png", 32, 32, True),
               ("logo-on-light.svg", "logo-on-light.png", 1040, 256, True), ("logo-on-dark.svg", "logo-on-dark.png", 1040, 256, True),
               ("splash-1920x1080.svg", "splash-1920x1080.png", 1920, 1080, False),
               ("splash-1024x768.svg", "splash-1024x768.png", 1024, 768, False),
               ("splash-640x480.svg", "splash-640x480.png", 640, 480, False),
               ("wallpaper-1920x1080.svg", "wallpaper-1920x1080.png", 1920, 1080, False)]
    with sync_playwright() as p:
        b = p.chromium.launch()
        for src, dst, w, h, transparent in renders:
            page = b.new_page(viewport={"width": w, "height": h})
            raw = open(os.path.join(OUT, src), encoding="utf-8").read()
            # scale the SVG to fill the page
            raw = raw.replace("<svg ", f'<svg preserveAspectRatio="xMidYMid meet" style="width:{w}px;height:{h}px;display:block" ', 1)
            page.set_content(f'<html><body style="margin:0;background:transparent">{raw}</body></html>')
            page.wait_for_timeout(150)
            page.screenshot(path=os.path.join(OUT, dst), omit_background=transparent)
            page.close()
        b.close()
    print("wrote", ", ".join(sorted(os.listdir(OUT))))


if __name__ == "__main__":
    sys.exit(main())
