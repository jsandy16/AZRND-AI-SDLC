"""The ST-3 v3 F look (0.8.0): a light page with a blue accent, the teal brand in the sidebar, the dark console.

Everything visual that Streamlit's own widgets cannot express lives here: keyed containers (`st.container(key="…")`, which
Streamlit renders with the class `st-key-…`) get their look from these rules. If a future Streamlit renames its classes the
app still works; it just looks plainer."""
from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

STATIC = Path(__file__).parent / "static"
LOGO = STATIC / "pkf-logo.svg"

BLUE, INK, MUTED, LINE = "#2457D6", "#1F2430", "#6B7280", "rgba(31,36,48,.14)"
TEAL, PURPLE = "#0F7C6C", "#5B2DD6"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'JetBrains Mono', monospace"

BASE_CSS = f"""
<style>
.block-container {{ padding-top: 1.2rem !important; padding-bottom: 1.5rem !important; max-width: 100% !important;
                   padding-left: 1.6rem !important; padding-right: 1.6rem !important; }}
/* Streamlit's own header would cover the top bar: keep it out of the way (its ⋮ menu stays at the far right) */
[data-testid="stHeader"] {{ background: transparent !important; height: 2.4rem !important; pointer-events: none; }}
[data-testid="stHeader"] * {{ pointer-events: auto; }}
.block-container {{ padding-top: 2.6rem !important; }}
.st-key-pkf-top {{ padding-right: 2.6rem; flex-wrap: nowrap !important; }}
.st-key-pkf-top > div {{ min-width: 0; }}
[class*="st-key-sc-pop-"] button {{ max-width: 24rem; }}
[class*="st-key-sc-pop-"] button p {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
@media (max-width: 1500px) {{ [class*="st-key-sc-pop-"] button {{ max-width: 15rem; }} }}
/* files of this stage: one row that scrolls sideways, like the rail */
.st-key-ws-files {{ overflow-x: auto !important; flex-wrap: nowrap !important; padding-bottom: 6px; }}
.st-key-ws-files > div {{ flex: 0 0 auto !important; }}
/* sidebar: the light grey panel of the design */
[data-testid="stSidebar"] {{ background: #F4F6F9 !important; border-right: 1px solid {LINE}; }}
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{ padding-top: .6rem; }}
.pkf-brand {{ display:flex; align-items:center; gap:10px; margin: 0 0 .6rem; }}
.pkf-brand .logo {{ width:38px; height:38px; border-radius:10px; background:{TEAL}; display:flex; align-items:center; justify-content:center; flex:none; }}
.pkf-brand b {{ font-size: 1.05rem; display:block; line-height:1.2; }}
.pkf-brand small {{ color:{MUTED}; font-size:.74rem; }}
.pkf-side-h {{ font-size:.7rem; font-weight:700; letter-spacing:.08em; color:{MUTED}; margin:.5rem 0 .2rem .1rem; }}
[class*="st-key-sb-pipe-"] button {{ justify-content:flex-start !important; padding:.2rem .5rem !important; min-height:2rem !important; }}
[class*="st-key-sb-pipe-"] button p {{ font-family:{MONO}; font-size:.76rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.st-key-sb-jobs, .st-key-sb-assistant {{ background:#fff; border-radius:10px; }}
.st-key-sb-assistant {{ border-color:#D9CCFF !important; background:#FBFAFF !important; }}
.st-key-sb-open button {{ background:{PURPLE} !important; border-color:{PURPLE} !important; color:#fff !important; }}
/* top bar */
.st-key-pkf-top {{ gap:.6rem !important; align-items:center !important; padding-bottom:.6rem; border-bottom:1px solid {LINE}; margin-bottom:.4rem; }}
.pkf-crumb {{ color:{MUTED}; font-size:.9rem; }} .pkf-crumb b {{ color:{INK}; font-family:{MONO}; font-size:.95rem; }}
.pkf-chip {{ display:inline-flex; align-items:center; gap:.35rem; padding:.18rem .6rem; border-radius:.5rem; white-space:nowrap;
            font: 600 .78rem/1.35 system-ui, sans-serif; }}
.pkf-chip.amber {{ background:#FFF1DC; color:#7A4E00; }} .pkf-chip.green {{ background:#DDF3E6; color:#1E7A43; }}
.pkf-chip.red {{ background:#FFE4E4; color:#B42318; }} .pkf-chip.grey {{ background:#EEF0F4; color:#4B5160; }}
.pkf-chip.blue {{ background:#E7EEFF; color:{BLUE}; }} .pkf-chip.purple {{ background:#F4F0FF; color:{PURPLE}; }}
.pkf-chip.orange {{ background:#FFF1DC; color:#7A4E00; }} .pkf-chip.yellow {{ background:#FFF6D6; color:#7A5B00; }}
[class*="st-key-wfd-stop-"] button:not(:disabled) {{ color:#B42318 !important; border-color:#F3B4B4 !important; }}
/* stepper */
.pkf-bar {{ height:6px; border-radius:3px; background:#E7EAF0; overflow:hidden; }} .pkf-bar i {{ display:block; height:100%; }}
[class*="st-key-ws-stage-"] button {{ justify-content:flex-start !important; padding:0 !important; min-height:1.6rem !important; }}
[class*="st-key-ws-stage-"] button p {{ font-weight:600; font-size:.88rem; text-align:left; }}
.st-key-ws-stepper {{ padding-bottom:.7rem; border-bottom:1px solid {LINE}; }}
.st-key-ws-stepper [data-testid="stVerticalBlock"], [class*="st-key-ws-card-"], .st-key-ws-files [data-testid="stVerticalBlock"] {{ gap:.3rem !important; }}
/* stage header */
.pkf-eyebrow {{ font-size:.72rem; font-weight:600; letter-spacing:.08em; color:{MUTED}; }}
.pkf-h1 {{ font-size:1.75rem; font-weight:700; letter-spacing:-.01em; margin:.1rem 0 .2rem; line-height:1.2; }}
.pkf-lead {{ color:#4B5160; max-width:760px; font-size:.92rem; }}
/* substage rail: one row, scrolls sideways, scrollbar always visible */
.st-key-ws-rail {{ overflow-x: auto !important; flex-wrap: nowrap !important; padding-bottom: 6px; scrollbar-width: auto; scrollbar-color: #B4BBC8 #E9ECF1; }}
.st-key-ws-rail::-webkit-scrollbar {{ height: 12px; }}
.st-key-ws-rail::-webkit-scrollbar-track {{ background:#E9ECF1; border-radius:6px; }}
.st-key-ws-rail::-webkit-scrollbar-thumb {{ background:#B4BBC8; border-radius:6px; border:2px solid #E9ECF1; }}
[class*="st-key-ws-card-"] {{ flex: 0 0 auto !important; background:#fff; border-radius:10px !important; }}
[class*="st-key-ws-card-"].pkf-on, .st-key-ws-card-on {{ border:2px solid {BLUE} !important; }}
[class*="st-key-ws-pick-"] button {{ justify-content:flex-start !important; padding:0 !important; min-height:1.4rem !important; }}
[class*="st-key-ws-pick-"] button p {{ font-weight:600; font-size:.93rem; text-align:left; }}
.pkf-kind {{ font-family:{MONO}; font-size:.72rem; color:{MUTED}; }} .pkf-kind.on {{ color:{BLUE}; }}
.pkf-kind.fail {{ color:#B42318; }} .pkf-kind.wait {{ color:#9A6700; }} .pkf-kind.ok {{ color:#1E7A43; }}
.pkf-sub {{ font-size:.8rem; color:{MUTED}; }}
.pkf-dot {{ display:inline-block; width:.55rem; height:.55rem; border-radius:50%; margin-right:.4rem; vertical-align:.05rem; }}
.pkf-dot.red {{ background:#E5484D; }} .pkf-dot.orange {{ background:#E0A100; }} .pkf-dot.green {{ background:#21A353; }}
.pkf-dot.grey {{ background:#C9CED6; }} .pkf-dot.blue {{ background:{BLUE}; }}
.pkf-mono {{ font-family:{MONO}; font-size:.82rem; overflow-wrap:anywhere; }}
.pkf-muted {{ color:{MUTED}; font-size:.82rem; }}
.pkf-check {{ display:inline-flex; width:18px; height:18px; border-radius:5px; background:#DDF3E6; color:#1E8E4B; align-items:center;
             justify-content:center; font-size:.75rem; font-weight:700; margin-right:.55rem; vertical-align:-.25rem; }}
.pkf-ring {{ display:inline-block; width:18px; height:18px; border-radius:50%; border:2px solid #E0A100; box-sizing:border-box;
            margin-right:.55rem; vertical-align:-.3rem; }}
.pkf-ring.red {{ border-color:#E5484D; }}
.pkf-spin {{ display:inline-block; width:.85rem; height:.85rem; border-radius:50%; vertical-align:-.12rem; margin-right:.35rem;
            border:.15rem solid #C9D6F7; border-top-color:{BLUE}; animation:pkf-spin 1s linear infinite; }}
@keyframes pkf-spin {{ to {{ transform: rotate(360deg); }} }}
/* the Your turn card */
.st-key-ws-turn {{ border-color:{BLUE} !important; background:#FAFCFF; }}
/* console: dark panel */
.st-key-ws-console {{ background:#0F131C !important; border-radius:10px !important; border-color:#0F131C !important; padding:.4rem .4rem .2rem !important; }}
.st-key-ws-console p, .st-key-ws-console label, .st-key-ws-console [data-testid="stMarkdownContainer"] {{ color:#E6EAF2 !important; }}
.st-key-ws-console [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
.st-key-ws-console [data-testid="stTextInput"] input {{ background:#1B2030 !important; color:#E6EAF2 !important; border-color:#3A4150 !important; font-family:{MONO}; font-size:.8rem; }}
.st-key-ws-console [data-baseweb="select"] > div, .st-key-ws-console [data-baseweb="select"] div {{ background:#1B2030 !important; color:#E6EAF2 !important; border-color:#3A4150 !important; }}
.st-key-ws-console [data-baseweb="select"] svg {{ fill:#9AA3B5 !important; }}
.st-key-ws-console input::placeholder {{ color:#7E879A !important; }}
.st-key-ws-console button {{ background:#1B2030 !important; color:#D7DCE6 !important; border-color:#3A4150 !important; }}
.st-key-ws-console [data-testid="stSegmentedControl"] button[aria-checked="true"],
.st-key-ws-console [data-testid="stButtonGroup"] button[aria-checked="true"] {{ background:#1F2A3F !important; color:#8FB4FF !important; }}
.st-key-ws-console iframe {{ height: max(360px, calc(100vh - 32rem)) !important; }}
.st-key-ws-con-scroll {{ max-height: max(360px, calc(100vh - 32rem)); overflow-y:auto; background:#fff; border-radius:8px; padding:.4rem .8rem; }}
.st-key-ws-con-scroll p, .st-key-ws-con-scroll [data-testid="stMarkdownContainer"] {{ color:{INK} !important; }}
/* assistant */
.st-key-as-decide {{ border-color:#D9CCFF !important; }}
/* 0.8.2: the assistant's own tab — the assistant on top (~75 % of the window), Decisions and Ask below (~25 %) */
.st-key-as-main {{ flex: 0 0 auto !important; height: calc((100dvh - 7.6rem) * .75) !important; min-height: 300px;
                   overflow-y: auto !important; padding-right: .4rem; }}
.st-key-as-bottom {{ flex: 0 0 auto !important; height: calc((100dvh - 7.6rem) * .25) !important; min-height: 150px;
                     overflow-y: auto !important; border-top: 1px solid {LINE}; padding-top: .5rem; }}
@media (max-height: 560px) {{ .st-key-as-main, .st-key-as-bottom {{ height: auto !important; overflow-y: visible !important; }} }}
[class*="st-key-wfd-fix-go-"] button:not(:disabled), [class*="st-key-wfd-apply-"] button:not(:disabled) {{
    background:{PURPLE} !important; border-color:{PURPLE} !important; color:#fff !important; }}
.pkf-brief {{ font-family:{MONO}; font-size:.8rem; line-height:1.55; background:#F7F8FA; border:1px solid {LINE}; border-radius:8px;
             padding:.6rem .8rem; white-space:pre-wrap; }}
.pkf-brief b {{ display:inline-block; width:5.6rem; color:{MUTED}; font-weight:600; }}
.pkf-bubble-me {{ background:#EEF2FF; border-radius:12px; padding:.55rem .8rem; margin:.3rem 0 .3rem 18%; }}
.pkf-bubble-ai {{ background:#fff; border:1px solid {LINE}; border-radius:12px; padding:.6rem .85rem; margin:.3rem 18% .3rem 0; }}
.pkf-full-hide [data-testid="stSidebar"], .pkf-full-hide [data-testid="stSidebarCollapsedControl"] {{ display:none !important; }}
</style>
"""

FULL_CSS = """<style>[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display:none !important; }
.block-container { padding-bottom: .4rem !important; }</style>"""


def inject() -> None:
    """The page-wide CSS (every page, every run)."""
    st.html(BASE_CSS)


def full_screen() -> None:
    """The assistant full screen: the sidebar is hidden while it shows."""
    st.html(FULL_CSS)


def chip(text: str, color: str = "grey", *, dot: bool = False, title: str | None = None) -> str:
    """A small rounded label (HTML, for st.markdown(unsafe_allow_html=True))."""
    t = f' title="{html.escape(title, quote=True)}"' if title else ""
    d = "● " if dot else ""
    return f'<span class="pkf-chip {color}"{t}>{d}{html.escape(text)}</span>'


def initials(name: str) -> str:
    parts = [x for x in str(name or "").replace("_", " ").split() if x]
    return ("".join(x[0] for x in parts[:2]) or "?").upper()


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))
