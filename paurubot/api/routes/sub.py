"""Branded subscription endpoint /s/<token> (SVC-08).

Client apps keep receiving the panel's subscription body with the usage headers they
understand, so the panel address never reaches customers. Browsers get an app-like page:
usage ring, the config list with per-config copy and QR, the share sheet and import
buttons, in light/dark and RTL/LTR. The old stylesheet stays for the /buy guest pages.
"""

from __future__ import annotations

import logging
import re
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from paurubot import kv
from paurubot.api.deps import DB
from paurubot.bot.views import qr_base64, qr_png
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import days_left, jdate, size
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Panel, Service, ServiceStatus, Shop
from paurubot.i18n import Texts, texts_for
from paurubot.panels import PanelError, driver_for
from paurubot.panels.registry import delivery_of
from paurubot.provisioning import subscription
from paurubot.provisioning.subscription import ALLOWED_SCHEMES, BLOCKED_SCHEMES

log = logging.getLogger(__name__)
router = APIRouter(tags=["subscription"])

RATE_LIMIT = 30
RATE_WINDOW = 60
NO_STORE = {"Cache-Control": "no-store"}

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_FONT_FILES: dict[str, tuple[str, str]] = {
    "font400.ttf": ("Vazirmatn-Regular.ttf", "font/ttf"),
    "font700.ttf": ("Vazirmatn-Bold.ttf", "font/ttf"),
    "font900.ttf": ("Vazirmatn-Black.ttf", "font/ttf"),
}
_fonts: dict[str, bytes] = {}

APP_PLATFORMS: dict[str, tuple[str, ...]] = {
    "hiddify": ("sub_page_android", "sub_page_ios", "sub_page_desktop"),
    "v2rayng": ("sub_page_android",),
    "happ": ("sub_page_android", "sub_page_ios"),
    "streisand": ("sub_page_ios",),
}

_PROTOCOLS: dict[str, str] = {
    "vless": "VLESS",
    "vmess": "VMess",
    "trojan": "Trojan",
    "ss": "Shadowsocks",
    "ssr": "ShadowsocksR",
    "hysteria": "Hysteria",
    "hysteria2": "Hysteria2",
    "hy2": "Hysteria2",
    "tuic": "TUIC",
    "wg": "WireGuard",
    "wireguard": "WireGuard",
    "socks5": "SOCKS5",
    "socks": "SOCKS",
    "http": "HTTP",
    "https": "HTTPS",
    "snell": "Snell",
    "juicity": "Juicity",
    "anytls": "AnyTLS",
}

CHIPS: dict[ServiceStatus, str] = {
    ServiceStatus.ACTIVE: "good",
    ServiceStatus.LIMITED: "warn",
    ServiceStatus.EXPIRED: "bad",
    ServiceStatus.DISABLED: "bad",
}

# ---------------------------------------------------------------------------
# Legacy assets: the guest purchase pages (/buy) still call these two.
# ---------------------------------------------------------------------------

PAGE_CSS = """
:root{--bg:#f3f5f9;--card:#fff;--ink:#16202e;--muted:#5d6b7e;--line:#dfe4ec;--accent:#2563eb;
--accent-ink:#fff;--good:#15803d;--warn:#b45309;--bad:#b91c1c;--bar:#e6eaf1}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--card:#18202b;--ink:#e8edf4;--muted:#9aa7b8;
--line:#2a3442;--accent:#5b8def;--accent-ink:#0b1220;--bar:#2a3442}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 Vazirmatn,Tahoma,system-ui,sans-serif}
main{max-width:460px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:15px;font-weight:600;color:var(--muted);margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.name{font-size:20px;font-weight:700;margin:0 0 4px;word-break:break-all}
.status{font-size:14px;color:var(--muted)}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}
.facts div{font-size:13px;color:var(--muted)}
.facts b{display:block;font-size:16px;color:var(--ink)}
.bar{display:block;width:100%;height:8px;margin-top:14px;border:0;border-radius:4px;background:var(--bar);
appearance:none;-webkit-appearance:none;color:var(--accent);overflow:hidden}
.bar::-webkit-progress-bar{background:var(--bar);border-radius:4px}
.bar::-webkit-progress-value{background:currentColor;border-radius:4px}
.bar::-moz-progress-bar{background:currentColor;border-radius:4px}
.bar.hi{color:var(--warn)}.bar.full{color:var(--bad)}
.qr{display:block;width:220px;max-width:100%;margin:4px auto 14px;border-radius:10px;background:#fff;padding:8px}
.link{direction:ltr;text-align:left;font:13px/1.5 ui-monospace,monospace;background:var(--bg);
border:1px solid var(--line);border-radius:10px;padding:10px;word-break:break-all;margin-bottom:12px}
.btn{display:block;width:100%;text-align:center;padding:12px;border-radius:10px;border:0;font:inherit;
font-weight:600;text-decoration:none;cursor:pointer;margin-top:8px}
.primary{background:var(--accent);color:var(--accent-ink)}
.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.apps{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.apps .btn{margin:0}
h2{font-size:15px;margin:0 0 10px}
details summary{cursor:pointer;color:var(--muted);font-size:14px}
details .link{margin:8px 0 0}
.note{font-size:13px;color:var(--muted);text-align:center}
.opt{display:flex;gap:10px;align-items:flex-start;padding:12px;border:1px solid var(--line);border-radius:10px;
margin-bottom:8px;cursor:pointer}
.opt input{margin-top:5px;accent-color:var(--accent)}
.opt small{display:block;color:var(--muted);font-size:13px}
.opt:has(input:checked){border-color:var(--accent)}
.price{display:block;font-weight:700;margin-top:2px}
.err{background:var(--card);border:1px solid var(--bad);color:var(--bad);border-radius:10px;padding:10px 12px;
font-weight:600}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""

PAGE_JS = """
document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
var v=b.getAttribute('data-copy');var done=b.getAttribute('data-done');var label=b.textContent;
function ok(){b.textContent=done;setTimeout(function(){b.textContent=label},1600)}
if(navigator.clipboard){navigator.clipboard.writeText(v).then(ok)}else{var t=document.createElement('textarea');
t.value=v;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();ok()}})});
"""

# ---------------------------------------------------------------------------
# The subscription page assets.
# ---------------------------------------------------------------------------

SUB_CSS = """
@font-face{font-family:'Vazirmatn';src:url('/s/_/font400.ttf') format('truetype');font-weight:400;
font-style:normal;font-display:swap}
@font-face{font-family:'Vazirmatn';src:url('/s/_/font700.ttf') format('truetype');font-weight:500 700;
font-style:normal;font-display:swap}
@font-face{font-family:'Vazirmatn';src:url('/s/_/font900.ttf') format('truetype');font-weight:800 900;
font-style:normal;font-display:swap}
:root{--bg:#eceaff;--ink:#17143f;--mute:#6d6a94;--card:rgba(255,255,255,.72);--line:rgba(100,80,230,.18);
--a:#6a4cff;--b:#0fb2e0;--c:#ff8a3d;--d:#ff5f6d;--ok:#10b981;--glow:rgba(106,76,255,.35);
box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#07081c;
--ink:#ecebff;--mute:#9391c2;--card:rgba(20,19,56,.66);--line:rgba(150,130,255,.2);--a:#8d74ff;
--b:#33d4ff;--glow:rgba(141,116,255,.4)}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#07081c;--ink:#ecebff;--mute:#9391c2;
--card:rgba(20,19,56,.66);--line:rgba(150,130,255,.2);--a:#8d74ff;--b:#33d4ff;--glow:rgba(141,116,255,.4)}
:root[data-theme="light"]{color-scheme:light}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font-family:'Vazirmatn',Tahoma,system-ui,sans-serif;
min-height:100vh;overflow-x:hidden}
#defs{position:absolute;width:0;height:0;overflow:hidden}
.bg{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.bg i{position:absolute;border-radius:50%;filter:blur(80px);animation:dr 20s ease-in-out infinite alternate}
.bg i:nth-child(1){width:60vmax;height:60vmax;background:var(--a);opacity:.4;top:-25vmax;right:-15vmax}
.bg i:nth-child(2){width:50vmax;height:50vmax;background:var(--b);opacity:.26;bottom:-25vmax;left:-15vmax;
animation-delay:-7s}
@keyframes dr{to{transform:translate(7vmax,5vmax) scale(1.15)}}
.wrap{position:relative;z-index:1;max-width:560px;margin:0 auto;padding:12px 14px 120px}
.top{display:flex;align-items:center;gap:12px;padding:8px 4px 16px}
.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,var(--a),var(--b));
display:grid;place-items:center;box-shadow:0 8px 24px -6px var(--glow);flex:none}
.top h1{margin:0;font-size:1.05rem;font-weight:800;flex:1;line-height:1.3;min-width:0}
.top h1 small{display:block;font-weight:400;color:var(--mute);font-size:.74rem}
.ib{width:40px;height:40px;border-radius:13px;border:1px solid var(--line);background:var(--card);
color:var(--ink);cursor:pointer;display:grid;place-items:center;font-size:1rem;flex:none}
.chip{display:inline-flex;align-items:center;gap:8px;font-size:.8rem;color:var(--ok);
background:rgba(16,185,129,.13);padding:6px 12px;border-radius:99px;font-weight:500}
.chip.warn{color:var(--c);background:rgba(255,138,61,.14)}
.chip.warn .dot{background:var(--c)}
.chip.warn .dot::after{background:var(--c)}
.chip.bad{color:var(--d);background:rgba(255,95,109,.14)}
.chip.bad .dot{background:var(--d)}
.chip.bad .dot::after{background:var(--d)}
.chip.mut{color:var(--mute);background:var(--line)}
.chip.mut .dot{background:var(--mute)}
.chip.mut .dot::after{background:var(--mute)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);position:relative}
.dot::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--ok);
animation:pg 1.8s ease-out infinite}
@keyframes pg{to{transform:scale(3.3);opacity:0}}
.page{display:none;gap:12px;grid-template-columns:1fr 1fr}.page.on{display:grid}
.page>*{opacity:0;transform:translateY(16px);animation:up .6s cubic-bezier(.2,.8,.2,1) forwards;
animation-delay:calc(var(--i,0)*60ms)}
.page>*:nth-child(1){--i:0}.page>*:nth-child(2){--i:1}.page>*:nth-child(3){--i:2}
.page>*:nth-child(4){--i:3}.page>*:nth-child(5){--i:4}.page>*:nth-child(6){--i:5}
.page>*:nth-child(7){--i:6}.page>*:nth-child(8){--i:7}
@keyframes up{to{opacity:1;transform:none}}
.full{grid-column:1/-1}
.card{--mx:50%;--my:50%;background:var(--card);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
border:1px solid var(--line);border-radius:24px;padding:18px;position:relative;overflow:hidden}
.card::before{content:"";position:absolute;inset:0;background:radial-gradient(220px circle at var(--mx)
var(--my),var(--glow),transparent 70%);opacity:0;transition:opacity .3s;pointer-events:none}
.card:hover::before{opacity:.5}
h2{margin:0 0 14px;font-size:1rem;font-weight:800}
.hero{background:linear-gradient(135deg,rgba(106,76,255,.42),var(--card) 70%);padding-bottom:84px}
.hero .id{display:flex;gap:12px;align-items:center;min-width:0}
.av{width:54px;height:54px;border-radius:18px;background:linear-gradient(135deg,var(--c),var(--a));
display:grid;place-items:center;font-weight:800;font-size:1.4rem;color:#fff;flex:none}
.hero b{font-size:1.2rem;word-break:break-word}
.hero small{display:block;color:var(--mute);direction:ltr;text-align:right;font-size:.82rem;
word-break:break-all}
.hero .chip{position:absolute;top:18px;inset-inline-end:18px}
.hero canvas{position:absolute;inset-inline:0;bottom:0;width:100%;height:80px}
.tile small{color:var(--mute);font-size:.78rem;display:block}
.tile .v{font-size:1.6rem;font-weight:800;direction:ltr;text-align:right;margin-top:4px;
font-variant-numeric:tabular-nums}
.tile .v u{text-decoration:none;font-size:.85rem;color:var(--mute);font-weight:500;margin-inline-start:4px}
.ic{width:36px;height:36px;border-radius:12px;display:grid;place-items:center;margin-bottom:10px;
background:rgba(141,116,255,.16);color:var(--a)}
.ringrow{display:flex;align-items:center;gap:18px}
.ring{position:relative;width:116px;height:116px;flex:none;display:grid;place-items:center}
.ring svg{position:absolute;inset:0;transform:rotate(-90deg)}
.ring circle{fill:none;stroke-width:9}
.t{stroke:var(--line)}
.p{stroke:url(#g);stroke-linecap:round;stroke-dasharray:314;stroke-dashoffset:314;
filter:drop-shadow(0 0 6px var(--a))}
.ring .rot{position:absolute;inset:-6px;border-radius:50%;background:conic-gradient(transparent 70%,
var(--b) 95%,transparent);-webkit-mask:radial-gradient(circle,transparent 66%,#000 67% 69%,transparent 70%);
mask:radial-gradient(circle,transparent 66%,#000 67% 69%,transparent 70%);animation:rt 4s linear infinite}
@keyframes rt{to{transform:rotate(360deg)}}
.ring b{font-size:1.5rem}
.ringrow p{margin:0;color:var(--mute);font-size:.86rem;line-height:1.9}
.ringrow p strong{color:var(--ink)}
.search{width:100%;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:16px;
padding:13px 16px;font:500 .92rem 'Vazirmatn',Tahoma,sans-serif;outline:none}
.search:focus{border-color:var(--a);box-shadow:0 0 0 3px var(--glow)}
.list{padding:4px 0}
.it{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--line);
transition:background .2s}.it:last-child{border:0}.it:hover{background:rgba(141,116,255,.08)}
.it .ib2{width:44px;height:44px;flex:none;border-radius:14px;display:grid;place-items:center;
border:1px solid var(--line);background:rgba(141,116,255,.14);color:var(--a)}
.it.g .ib2{background:rgba(16,185,129,.14);color:var(--ok)}
.it.y .ib2{background:rgba(255,138,61,.14);color:var(--c)}
.it.n .ib2{color:var(--mute);background:transparent;border:0}
.it .tx{flex:1;min-width:0}
.it .tx b{display:block;font-size:.9rem;font-weight:800;line-height:1.5;word-break:break-word}
.it .tx span{display:block;color:var(--mute);font-size:.74rem;direction:ltr;text-align:right;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.it .ac{display:flex;gap:6px;flex:none}
.sb{width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:var(--card);
color:var(--ink);cursor:pointer;display:grid;place-items:center;transition:transform .15s,border-color .2s}
.sb:hover{border-color:var(--a)}.sb:active{transform:scale(.88)}
.it.hide{display:none}
.cnt{color:var(--mute);font-size:.8rem;margin:8px 16px 6px}
.share{display:grid;gap:16px;justify-items:center;text-align:center}
.qr{position:relative;padding:14px;background:#fff;border-radius:20px;box-shadow:0 0 0 2px var(--a),
0 16px 44px -12px var(--a);overflow:hidden}
.qr::after{content:"";position:absolute;left:0;right:0;height:44px;top:-44px;
background:linear-gradient(transparent,rgba(15,178,224,.55));border-bottom:2px solid var(--b);
animation:sc 3s ease-in-out infinite}
@keyframes sc{0%{top:-44px}55%,100%{top:100%}}
.qr img{display:block;width:190px;height:190px}
.lnk{width:100%;direction:ltr;text-align:left;font:.74rem ui-monospace,Menlo,monospace;word-break:break-all;
background:var(--card);border:1px dashed var(--line);border-radius:14px;padding:12px;color:var(--mute)}
.btns{display:grid;grid-template-columns:1.4fr 1fr;gap:10px;width:100%}
.btn{border:1px solid var(--line);border-radius:15px;padding:14px 8px;font:800 .92rem 'Vazirmatn',Tahoma,
sans-serif;color:var(--ink);background:var(--card);cursor:pointer;position:relative;overflow:hidden;
text-decoration:none}
.btn:active{transform:scale(.97)}
.btn.pr{border:0;color:#fff;background:linear-gradient(110deg,var(--a),var(--b),var(--a));
background-size:220% 100%;animation:fl 5s linear infinite;box-shadow:0 10px 26px -10px var(--a)}
@keyframes fl{to{background-position:-220% 0}}
.btn .r{position:absolute;border-radius:50%;background:rgba(255,255,255,.5);transform:scale(0);
animation:rp .6s ease-out forwards;pointer-events:none}
@keyframes rp{to{transform:scale(4);opacity:0}}
.apps{display:grid;gap:12px}
.app{display:flex;align-items:center;gap:14px;text-decoration:none;color:var(--ink);padding:16px;
border-radius:22px;border:1px solid var(--line);background:var(--card);transition:transform .2s,
border-color .2s}.app:hover{border-color:var(--a)}
.app:active{transform:scale(.97)}
.app .ai{width:50px;height:50px;border-radius:16px;display:grid;place-items:center;font-weight:800;
font-size:1.2rem;color:#fff;box-shadow:0 8px 20px -8px var(--glow);flex:none}
.ai.p1{background:linear-gradient(135deg,#3b2cff,#8d74ff)}
.ai.p2{background:linear-gradient(135deg,#0fb2e0,#4f6bff)}
.ai.p3{background:linear-gradient(135deg,#34a0ff,#6ae0ff)}
.ai.p4{background:linear-gradient(135deg,#9d3cff,#ff5fa0)}
.ai.p5{background:linear-gradient(135deg,#ff8a3d,#ffb23d)}
.ai.p6{background:linear-gradient(135deg,#10b981,#33d4ff)}
.app b{display:block}
.app small{color:var(--mute);font-size:.76rem}
.app .go{margin-inline-start:auto;color:var(--mute);font-size:1.4rem}
.steps{padding:0;margin:0;display:grid;gap:12px;counter-reset:s}
.steps li{list-style:none;display:flex;gap:12px;align-items:center;font-size:.88rem;counter-increment:s}
.steps li::before{content:counter(s);width:28px;height:28px;flex:none;border-radius:50%;
display:grid;place-items:center;background:linear-gradient(135deg,var(--a),var(--b));color:#fff;
font-weight:800;font-size:.8rem}
.sup{display:inline-flex;align-items:center;gap:6px;justify-content:center;color:var(--mute);
font-size:.82rem;text-decoration:none}
.sup:hover{color:var(--a)}
.nav{position:fixed;z-index:5;left:50%;transform:translateX(-50%);
bottom:calc(14px + env(safe-area-inset-bottom,0px));display:flex;gap:4px;padding:6px;border-radius:24px;
background:var(--card);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
border:1px solid var(--line);box-shadow:0 14px 40px -12px var(--glow)}
.nav a{color:var(--mute);text-decoration:none;font-size:.7rem;display:flex;flex-direction:column;
align-items:center;gap:3px;padding:8px 15px;border-radius:18px;transition:.3s}
.nav a.on{background:linear-gradient(135deg,var(--a),var(--b));color:#fff}
.sheet{position:fixed;inset:0;z-index:20;display:none;align-items:flex-end;justify-content:center;
background:rgba(5,5,25,.55);backdrop-filter:blur(4px)}.sheet.on{display:flex}
.sheet .pn{width:100%;max-width:560px;background:var(--bg);border:1px solid var(--line);
border-radius:28px 28px 0 0;padding:22px 20px calc(24px + env(safe-area-inset-bottom,0px));
animation:sl .4s cubic-bezier(.2,.9,.3,1) both;text-align:center;display:grid;gap:14px;
justify-items:center}
@keyframes sl{from{transform:translateY(100%)}}
.sheet h3{margin:0;font-size:.95rem}
.sheet p{margin:0;color:var(--mute);font-size:.8rem}
.sheet .btn{width:100%}
.toast{position:fixed;z-index:30;left:50%;top:calc(16px + env(safe-area-inset-top,0px));
transform:translate(-50%,-90px);background:var(--ok);color:#fff;padding:12px 20px;border-radius:99px;
font-weight:500;font-size:.88rem;transition:.4s cubic-bezier(.2,.9,.3,1.3);pointer-events:none;
white-space:nowrap}.toast.on{transform:translate(-50%,0)}
canvas#fx{position:fixed;inset:0;z-index:40;pointer-events:none}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
:focus-visible{outline:2px solid var(--a);outline-offset:2px}
"""

SUB_JS = r"""
(function(){
"use strict";
var $=function(s){return document.querySelector(s)},
$$=function(s,r){return[].slice.call((r||document).querySelectorAll(s))};
var I={db:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
cal:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 11h18"/>',
copy:'<rect x="9" y="9" width="11" height="11" rx="3"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
app:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
qr:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3z"/>',
home:'<path d="M3 11 12 4l9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
link:'<path d="M10 14a4 4 0 0 0 5.6 0l3-3a4 4 0 0 0-5.6-5.6l-1 1M14 10a4 4 0 0 0-5.6 0l-3 3a4 4 0 0 0 5.6 5.6l1-1"/>',
layers:'<path d="m12 3 9 5-9 5-9-5zM3 13l9 5 9-5"/>',
info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
user:'<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/>',
key:'<circle cx="8" cy="14" r="4"/><path d="M10.8 11.2 20 2M16.8 4.4l2.8 2.8M13.8 7.4l2.8 2.8"/>'};
function ic(n,s){return '<svg width="'+(s||20)+'" height="'+(s||20)+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+(I[n]||"")+'</svg>'}
$$('[data-i]').forEach(function(e){e.innerHTML=ic(e.dataset.i)});
// theme: follows the system until the visitor picks light or dark
var root=document.documentElement,LS="sub-theme",th=$("#th");
try{var saved=localStorage.getItem(LS);if(saved==="dark"||saved==="light")root.setAttribute("data-theme",saved)}catch(e){}
if(th)th.onclick=function(){var sys=matchMedia("(prefers-color-scheme:dark)").matches;
var dark=root.getAttribute("data-theme")==="dark"||(!root.getAttribute("data-theme")&&sys);
var to=dark?"light":"dark";root.setAttribute("data-theme",to);try{localStorage.setItem(LS,to)}catch(e){}};
$$(".card").forEach(function(card){card.addEventListener("pointermove",function(e){
var r=card.getBoundingClientRect();card.style.setProperty("--mx",(e.clientX-r.left)+"px");
card.style.setProperty("--my",(e.clientY-r.top)+"px")})});
// toast + copy + confetti + ripple
var ts=$("#ts"),tt;
function toast(m,x,y){if(ts){ts.textContent=m;ts.classList.add("on");clearTimeout(tt);
tt=setTimeout(function(){ts.classList.remove("on")},2000)}if(x!=null)burst(x,y)}
function cp(s,msg,e){function ok(){toast(msg,e&&e.clientX,e&&e.clientY)}
function fb(){var a=document.createElement("textarea");a.value=s;document.body.appendChild(a);a.select();
try{document.execCommand("copy")}catch(x){}a.remove();ok()}
if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(s).then(ok,fb);else fb()}
function rip(b,e){var r=b.getBoundingClientRect(),s=document.createElement("span"),
z=Math.max(r.width,r.height)/4;s.className="r";
s.style.cssText="width:"+z+"px;height:"+z+"px;left:"+(e.clientX-r.left-z/2)+"px;top:"+(e.clientY-r.top-z/2)+"px";
b.appendChild(s);setTimeout(function(){s.remove()},650)}
$$(".btn").forEach(function(b){b.addEventListener("click",function(e){if(e.isTrusted)rip(b,e)})});
var sh=$("#sh");
function sheet(t,p,i){var h=$("#sht"),pp=$("#shp"),img=$("#qr2");
if(h)h.textContent=t;if(pp)pp.textContent=p;
if(img)img.src=new URL("qr.png?i="+encodeURIComponent(i),location.href).href;
if(sh){sh.classList.add("on");sh.setAttribute("aria-hidden","false")}}
function sheetOff(){if(!sh)return;sh.classList.remove("on");sh.setAttribute("aria-hidden","true");
var img=$("#qr2");if(img)img.removeAttribute("src")}
if(sh){var shx=$("#shx");if(shx)shx.onclick=sheetOff;
sh.addEventListener("click",function(e){if(e.target===sh)sheetOff()});
document.addEventListener("keydown",function(e){if(e.key==="Escape")sheetOff()})}
var lst=$("#lst"),items=lst?$$("#lst [data-q]"):[],sq=$("#sq");
if(sq&&lst)sq.oninput=function(){var q=this.value.toLowerCase();
items.forEach(function(r){r.classList.toggle("hide",r.dataset.q.indexOf(q)<0)})};
if(lst)lst.addEventListener("click",function(e){var b=e.target.closest("button");if(!b)return;
if(b.dataset.all){cp(b.dataset.all,b.dataset.m,e);return}
if(b.dataset.u){cp(b.dataset.u,b.dataset.m,e);return}
if(b.dataset.qr!=null&&b.dataset.qr!=="")sheet(b.dataset.t,b.dataset.p,b.dataset.qr)});
var lnk=$("#lnk"),cpb=$("#cp"),dl=$("#dl"),qr=$("#qr");
if(cpb&&lnk)cpb.onclick=function(e){cp(lnk.textContent.trim(),cpb.dataset.m,e)};
if(dl&&qr)dl.onclick=function(){var a=document.createElement("a");a.href=qr.src;
a.download="subscription-qr.png";document.body.appendChild(a);a.click();a.remove()};
// numbers count up once; the ring follows with the same easing
var rp=$("#rp"),ringRun=false;
function count(){if(ringRun)return;ringRun=true;var els=$$("[data-n]"),go=+rp.dataset.p||0;
if(matchMedia("(prefers-reduced-motion:reduce)").matches){
els.forEach(function(n){n.textContent=n.dataset.n});
if(rp)rp.style.strokeDashoffset=314-314*go/100;return}
var t0=performance.now();
(function f(t){var k=Math.min((t-t0)/1400,1),z=1-Math.pow(1-k,3);
els.forEach(function(n){n.textContent=Math.round(+n.dataset.n*z)});
if(rp)rp.style.strokeDashoffset=314-314*go/100*z;
if(k<1)requestAnimationFrame(f)})(t0)}
// page router: #home / #configs / #share / #apps
var T={};
$$(".nav a").forEach(function(a){var k=(a.getAttribute("href")||"#home").slice(1);T[k]=a.textContent.trim()});
function show(){var p=(location.hash||"#home").slice(1);if(!T[p])p="home";
$$(".page").forEach(function(s){s.classList.toggle("on",s.id===p)});
$$(".nav a").forEach(function(a){a.classList.toggle("on",a.getAttribute("href")==="#"+p)});
var ttl=$("#ttl");if(ttl&&ttl.firstChild&&T[p])ttl.firstChild.nodeValue=T[p];
scrollTo(0,0);count()}
addEventListener("hashchange",show);show();
// wave under the hero
var wv=$("#wv"),w=wv?wv.getContext("2d"):null,ph=0,dpr=Math.min(devicePixelRatio||1,2),
red=matchMedia("(prefers-reduced-motion:reduce)").matches;
function wr(){if(!wv)return;var r=wv.getBoundingClientRect();
if(r.width){wv.width=r.width*dpr;wv.height=r.height*dpr}}
if(wv){wr();addEventListener("resize",wr);
(function wave(){var W=wv.width,H=wv.height;
if(!W||!H||wv.offsetParent===null){wr();requestAnimationFrame(wave);return}
w.clearRect(0,0,W,H);
[[.5,'#8d74ff',1],[.35,'#33d4ff',1.6],[.25,'#ff8a3d',2.3]].forEach(function(ln,i){
w.beginPath();for(var x=0;x<=W;x+=4){var y=H*.55+Math.sin(x/(70*dpr)*ln[2]+ph*ln[2]+i)*H*.3*ln[0]
+Math.sin(x/(30*dpr)+ph*2)*H*.05;x?w.lineTo(x,y):w.moveTo(x,y)}
w.strokeStyle=ln[1];w.lineWidth=2.2*dpr;w.shadowColor=ln[1];w.shadowBlur=10*dpr;w.stroke()});
if(!red)ph+=.025;requestAnimationFrame(wave)})()}
// confetti
var cvr=$("#fx"),c=cvr?cvr.getContext("2d"):null,B=[];
function rsz(){if(cvr){cvr.width=innerWidth;cvr.height=innerHeight}}rsz();addEventListener("resize",rsz);
function burst(x,y){if(!c)return;for(var i=0;i<28;i++){var a=Math.random()*6.28,s=Math.random()*5+2;
B.push({x:x,y:y,vx:Math.cos(a)*s,vy:Math.sin(a)*s-3,l:1,h:['#33d4ff','#8d74ff','#ff8a3d'][i%3]})}}
(function lp(){if(c){c.clearRect(0,0,cvr.width,cvr.height);
B=B.filter(function(b){b.x+=b.vx;b.y+=b.vy;b.vy+=.16;b.l-=.02;c.globalAlpha=Math.max(b.l,0);
c.fillStyle=b.h;c.beginPath();c.arc(b.x,b.y,3,0,6.28);c.fill();return b.l>0})}
requestAnimationFrame(lp)})()
})();
"""


@router.get("/s/_/page.css", include_in_schema=False)
async def page_css() -> Response:
    return Response(PAGE_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/s/_/page.js", include_in_schema=False)
async def page_js() -> Response:
    return Response(
        PAGE_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/s/_/sub.css", include_in_schema=False)
async def sub_css() -> Response:
    return Response(SUB_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/s/_/sub.js", include_in_schema=False)
async def sub_js() -> Response:
    return Response(
        SUB_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/s/_/{font}", include_in_schema=False)
async def font_file(font: str) -> Response:
    info = _FONT_FILES.get(font)
    if info is None:
        return _not_found()
    data = _fonts.get(font)
    if data is None:
        try:
            data = (_FONT_DIR / info[0]).read_bytes()
        except OSError:
            return _not_found()
        _fonts[font] = data
    return Response(
        data, media_type=info[1], headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )


def _not_found() -> Response:
    return Response("not found", status_code=404, media_type="text/plain")


async def _resolve(db: DB, token: str) -> tuple[Service, Shop, Panel] | None:
    service = await db.scalar(select(Service).where(Service.sub_token == token))
    if service is None or service.status == ServiceStatus.DELETED:
        return None
    shop = await db.get(Shop, service.shop_id)
    panel = await db.get(Panel, service.panel_id)
    if shop is None or panel is None:
        return None
    return service, shop, panel


def _link(service: Service, request: Request) -> str:
    return subscription.branded_url(service) or f"{str(request.base_url).rstrip('/')}/s/{service.sub_token}"


async def _links(panel: Panel, service: Service) -> list[str]:
    try:
        return await (await driver_for(panel)).fetch_links(service.subscription_url)
    except PanelError:
        return []


@router.get("/s/{token}", include_in_schema=False)
async def subscription_endpoint(token: str, request: Request, db: DB) -> Response:
    if not subscription.TOKEN_RE.match(token):
        return _not_found()
    allowed, _ = await kv.hit(f"sub:{token}", RATE_LIMIT, RATE_WINDOW)
    if not allowed:
        return Response("slow down", status_code=429, media_type="text/plain", headers={"Retry-After": "60"})
    found = await _resolve(db, token)
    if found is None:
        return _not_found()
    service, shop, panel = found
    if panel.national_mode:
        return Response(
            "National-network mode: use the replacement configuration in the bot.",
            status_code=503,
            media_type="text/plain",
            headers={"Cache-Control": "no-store", "Retry-After": "300"},
        )
    if request.query_params.get("download") != "1" and subscription.wants_page(
        request.headers.get("user-agent", ""), request.headers.get("accept", "")
    ):
        return await _page(db, request, service, shop, panel)
    try:
        body = await (await driver_for(panel)).fetch_subscription(
            service.subscription_url, request.headers.get("user-agent") or "v2rayNG/1.8.0"
        )
    except PanelError:
        body = None
    if body is None:
        return Response("subscription unavailable", status_code=502, media_type="text/plain")
    # Which app imported it, for the customer's own view (F-11); throttled, committed by DB.
    subscription.client_seen(service, subscription.client_app(request.headers.get("user-agent")), utcnow())
    headers = dict(body.headers)
    headers["profile-title"] = subscription.profile_title(shop.title or "PauruBot")
    headers.setdefault("profile-update-interval", "12")
    headers["Cache-Control"] = "no-store"
    return Response(body.content, media_type=body.content_type.split(";")[0] or "text/plain", headers=headers)


@router.get("/s/{token}/qr.png", include_in_schema=False)
async def subscription_qr(token: str, request: Request, db: DB) -> Response:
    """One QR per request: no params for the subscription link, ?i=<n> for config n."""
    if not subscription.TOKEN_RE.match(token):
        return _not_found()
    allowed, _ = await kv.hit(f"sub:{token}", RATE_LIMIT, RATE_WINDOW)
    if not allowed:
        return Response("slow down", status_code=429, media_type="text/plain", headers={"Retry-After": "60"})
    found = await _resolve(db, token)
    if found is None:
        return _not_found()
    service, _, panel = found
    item: str = _link(service, request)
    if "i" in request.query_params:
        try:
            index = int(request.query_params.get("i", ""))
        except ValueError:
            return _not_found()
        if index >= 0:
            rows = await _links(panel, service)
            if index >= len(rows) or not _is_config(rows[index]):
                return _not_found()
            item = rows[index]
    png = qr_png(item)
    if png is None:
        return _not_found()
    return Response(png, media_type="image/png", headers=NO_STORE)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _is_config(item: str) -> bool:
    if not ALLOWED_SCHEMES.match(item):
        return False
    return item.split(":", 1)[0].lower() not in BLOCKED_SCHEMES


def _usage(service: Service) -> tuple[int, str, str, str]:
    """(percent, used text, total text, left text); total/left stay empty when unlimited."""
    if service.data_limit:
        percent = min(100, int(service.used_bytes * 100 / service.data_limit))
        used, total = size(service.used_bytes), size(service.data_limit)
        left = size(max(0, service.data_limit - service.used_bytes))
    else:
        percent, used, total, left = 0, size(service.used_bytes), "", ""
    return percent, used, total, left


def _expiry(t: Texts, service: Service) -> tuple[str, int | None]:
    if service.expire_at is None:
        text = t("sub_page_on_hold") if service.status == ServiceStatus.ON_HOLD else t("unlimited")
        return text, None
    return jdate(service.expire_at), max(0, days_left(service.expire_at) or 0)


# ---------------------------------------------------------------------------
# Markup builders (no inline styles anywhere: the CSP forbids them)
# ---------------------------------------------------------------------------

LOGO = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" '
    'stroke-linejoin="round"><path d="M12 2.5 4 6v6c0 4.5 3.2 8 8 9.5 4.8-1.5 8-5 8-9.5V6l-8-3.5Z"/>'
    '<path d="m12.8 7-3.3 5h3l-.8 5 3.3-5h-3Z" fill="#fff"/></svg>'
)

SHEET = (
    '<div class="sheet" id="sh" role="dialog" aria-modal="true" aria-hidden="true"><div class="pn">'
    '<h3 id="sht"></h3><div class="qr"><img id="qr2" alt=""></div><p id="shp"></p>'
    '<button type="button" class="btn" id="shx">{close}</button></div></div>'
)

NAV = (
    '<nav class="nav"><a href="#home" class="on"><span data-i="home"></span>{home}</a>'
    '<a href="#configs"><span data-i="globe"></span>{configs}</a>'
    '<a href="#share"><span data-i="qr"></span>{share}</a>'
    '<a href="#apps"><span data-i="app"></span>{apps}</a></nav>'
)


def _tile(icon: str, label: str, number: str, unit: str, value: str) -> str:
    """A stat tile: counts up when the value is a plain number, plain text otherwise."""
    head = f'<span data-n="{escape(number)}">0</span>' if number else f"<span>{escape(value)}</span>"
    tail = f"<u>{escape(unit)}</u>" if number and unit else ""
    return (
        f'<div class="card tile"><div class="ic" data-i="{icon}"></div>'
        f"<small>{escape(label)}</small>"
        f'<div class="v">{head}{tail}</div></div>'
    )


def _hero(t: Texts, service: Service, shop: Shop) -> str:
    name = escape(str(service.label or service.panel_username))
    token = escape(service.sub_token or "")
    letter = escape((shop.title or "PauruBot")[0].upper())
    # status_* keys lead with an emoji glyph; this page draws its own dot.
    status = re.sub(r"^[^\w\u0600-\u06FF]+", "", t(f"status_{service.status.value}"))
    tone = CHIPS.get(service.status, "mut")
    return (
        '<div class="card hero full"><div class="id">'
        f'<div class="av">{letter}</div><div><b>{name}</b><small>{token}</small></div></div>'
        f'<span class="chip {tone}"><span class="dot"></span>{escape(status)}</span>'
        '<canvas id="wv"></canvas></div>'
    )


def _ring(t: Texts, service: Service) -> str:
    percent, used, total, left = _usage(service)
    expiry, _days = _expiry(t, service)
    # LTR values sit inside RTL lines: each value gets its own <bdi> so "4.2 GB"
    # keeps its order; the connecting words come from the locale text.
    used_box, total_box = f"<bdi>{escape(used)}</bdi>", f"<bdi>{escape(total)}</bdi>"
    if total:
        first = (
            escape(t("sub_page_used_of", used="\u0001", total="\u0002"))
            .replace("\u0001", used_box)
            .replace("\u0002", total_box)
        )
        second = f"<bdi>{escape(left)}</bdi>"
    else:
        first = used_box
        second = escape(t("unlimited"))
    lines = (
        f"<strong>{escape(t('sub_page_volume'))}: {first}</strong>"
        f"<br>{escape(t('sub_page_remaining'))}: {second}"
        f"<br>{escape(t('sub_page_expiry'))}: {escape(expiry)}"
    )
    return (
        '<div class="card full"><div class="ringrow"><div class="ring"><span class="rot"></span>'
        '<svg viewBox="0 0 116 116" aria-hidden="true"><circle class="t" cx="58" cy="58" r="50"/>'
        f'<circle class="p" id="rp" cx="58" cy="58" r="50" data-p="{percent}"/></svg>'
        f'<b><span data-n="{percent}">0</span>%</b></div>'
        f"<p>{lines}</p></div></div>"
    )


def _initial(t: Texts, shop: Shop, service: Service) -> str:
    """The home page body: hero, two stat tiles and the usage ring."""
    _expiry_text, days = _expiry(t, service)
    _percent, _used, total, _left = _usage(service)
    if total:
        head, _sep, unit = total.partition(" ")
        volume_tile = (
            _tile("db", t("sub_page_total_volume"), head, unit, total)
            if head.isdigit()
            else _tile("db", t("sub_page_total_volume"), "", "", total)
        )
    else:
        volume_tile = _tile("db", t("sub_page_total_volume"), "", "", t("unlimited"))
    days_tile = (
        _tile("cal", t("sub_page_days_left"), str(days), t("sub_page_unit_days"), "")
        if days is not None
        else _tile("cal", t("sub_page_days_left"), "", "", "-")
    )
    return (
        '<section class="page on" id="home">'
        + _hero(t, service, shop)
        + volume_tile
        + days_tile
        + _ring(t, service)
        + "</section>"
    )


def _acts(uid: str, msg: str, index: str, title: str, note: str, t: Texts) -> str:
    copy_btn = (
        f'<button type="button" class="sb" data-u="{escape(uid)}" data-m="{escape(msg)}" '
        f'aria-label="{escape(t("sub_page_btn_copy"))}"><span data-i="copy"></span></button>'
        if uid
        else ""
    )
    return (
        '<div class="ac">'
        + copy_btn
        + f'<button type="button" class="sb" data-qr="{escape(index)}" data-t="{escape(title)}" '
        + f'data-p="{escape(note)}" aria-label="QR"><span data-i="qr"></span></button></div>'
    )


def _rows(t: Texts, link: str, raw: list[str]) -> str:
    """The configs list: panel notices first, the link row, copy-all, then one row per config."""
    uris = [item for item in raw if _is_config(item)]
    parts = [
        f'<div class="it n" data-q="{escape(item.lower())}">'
        '<div class="ib2" data-i="info"></div><div class="tx"><b>' + escape(item) + "</b></div></div>"
        for item in raw
        if not _is_config(item)
    ]
    parts.append(
        '<div class="it g"><div class="ib2" data-i="link"></div><div class="tx"><b>'
        f"{escape(t('sub_page_link_row'))}</b><span>{escape(link)}</span></div>"
        + _acts(link, t("sub_page_t_link"), "-1", t("sub_page_link_row"), t("sub_page_sheet_p_link"), t)
        + "</div>"
    )
    if uris:
        parts.append(
            '<div class="it y"><div class="ib2" data-i="layers"></div><div class="tx"><b>'
            f"{escape(t('sub_page_all_row'))}</b><span>"
            f"{escape(t('sub_page_items', count=len(uris)))}</span></div>"
            '<div class="ac"><button type="button" class="sb" data-all="'
            + escape(chr(10).join(uris))
            + f'" data-m="{escape(t("sub_page_t_all"))}" aria-label="{escape(t("sub_page_btn_copy"))}">'
            '<span data-i="copy"></span></button></div></div>'
        )
    if not raw:
        parts.append(f'<p class="cnt">{escape(t("sub_page_no_links"))}</p>')
    for index, item in enumerate(raw):
        if _is_config(item):
            scheme = item.split(":", 1)[0].lower()
            proto = _PROTOCOLS.get(scheme, scheme.upper())
            head = item.partition("://")[2].partition("#")[0]
            meta = f"{proto} · {scheme}://{head}" if head else proto
            title: str = t("sub_page_config_n", n=index + 1)
            if "#" in item:
                remark = unquote(item.rsplit("#", 1)[1]).strip()
                title = remark or title
            parts.append(
                f'<div class="it" data-q="{escape(title.lower())}">'
                '<div class="ib2" data-i="globe"></div><div class="tx"><b>'
                + escape(title)
                + "</b><span>"
                + escape(meta)
                + "</span></div>"
                + _acts(item, t("sub_page_t_one"), str(index), title, t("sub_page_sheet_p_one"), t)
                + "</div>"
            )
    return "".join(parts)


def _share(t: Texts, link: str) -> str:
    qr = qr_base64(link)
    image = f'<img id="qr" src="data:image/png;base64,{qr}" alt="{escape(t("qr_caption"))}">' if qr else ""
    return (
        '<section class="page" id="share"><div class="card full"><div class="share">'
        f'<h2>{escape(t("sub_page_share_title"))}</h2><div class="qr">{image}</div>'
        f'<div class="lnk" id="lnk">{escape(link)}</div>'
        '<div class="btns">'
        f'<button type="button" class="btn pr" id="cp" data-m="{escape(t("sub_page_t_link"))}">'
        f"{escape(t('sub_page_copy'))}</button>"
        f'<button type="button" class="btn" id="dl">{escape(t("sub_page_download_qr"))}</button>'
        "</div></div></div></section>"
    )


def _apps(t: Texts, link: str, cfg: dict[str, Any], title: str, support: str, chevron: str) -> str:
    piles = []
    for i, app in enumerate(subscription.app_buttons(cfg, link, title)):
        name = str(app.get("name") or "")
        letter = escape(name[:1].upper() or "?")
        small = ""
        keys = APP_PLATFORMS.get(name.strip().lower())
        if keys:
            small = f"<small>{escape(' · '.join(t(key) for key in keys))}</small>"
        piles.append(
            f'<a class="app" href="{escape(app["url"])}">'
            f'<span class="ai p{i % 6 + 1}">{letter}</span>'
            f"<span><b>{escape(name)}</b>{small}</span>"
            f'<span class="go">{chevron}</span></a>'
        )
    steps = [escape(step.strip()) for step in t("sub_page_steps").split("|") if step.strip()][:6]
    guide = "".join(f"<li>{step}</li>" for step in steps)
    help_link = (
        f'<a class="sup" href="https://t.me/{escape(support)}">{escape(t("sub_page_support"))}</a>'
        if support
        else ""
    )
    return (
        '<section class="page" id="apps">'
        f'<div class="apps full">{"".join(piles)}</div>'
        '<div class="card full">'
        f'<h2>{escape(t("sub_page_guide"))}</h2><ol class="steps">{guide}</ol>{help_link}</div></section>'
    )


def _configs_page(t: Texts, link: str, raw: list[str]) -> str:
    return (
        '<section class="page" id="configs">'
        '<input class="search full" id="sq" type="search" autocomplete="off" '
        f'placeholder="{escape(t("sub_page_search"))}">'
        f'<div class="card full list" id="lst">{_rows(t, link, raw)}</div></section>'
    )


def _special_access(t: Texts, service: Service, kind: str, link: str, rows: list[str]) -> str:
    """The access card for WireGuard (QR + download) and login/account panels."""
    if kind == "wireguard":
        row = next((item for item in rows if _is_config(item)), rows[0] if rows else "")
        if not row:
            return f'<div class="card full"><p class="status">{escape(t("wireguard_unavailable"))}</p></div>'
        qr = qr_base64(row)
        image = (
            f'<div class="qr"><img src="data:image/png;base64,{qr}" alt="{escape(t("qr_caption"))}"></div>'
            if qr
            else ""
        )
        return (
            f'<div class="card full share">{image}'
            f'<a class="btn pr" href="{escape(link)}?download=1">{escape(t("sub_page_download_conf"))}</a></div>'
        )
    password = escape(str((service.panel_data or {}).get("password") or "-"))
    return (
        '<div class="card full">'
        '<div class="it"><div class="ib2" data-i="user"></div><div class="tx"><b>'
        + escape(t("sub_page_username"))
        + "</b><span>"
        + escape(service.panel_username)
        + '</span></div></div><div class="it"><div class="ib2" data-i="key"></div><div class="tx"><b>'
        + escape(t("sub_page_password"))
        + "</b><span>"
        + password
        + "</span></div></div></div>"
    )


def _document(t: Texts, title: str, heading: str, tag: str, pages: str, nav: str = "") -> str:
    """The shared frame: background, header, pages, nav, sheet, toast."""
    sheet = SHEET.replace("{close}", escape(t("sub_page_close"))) if nav else ""
    direction = "rtl" if t.locale == "fa" else "ltr"
    head = (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title>"
        '<meta name="theme-color" media="(prefers-color-scheme:light)" content="#eceaff">'
        '<meta name="theme-color" media="(prefers-color-scheme:dark)" content="#07081c">'
        '<link rel="stylesheet" href="/s/_/sub.css">'
    )
    deco = (
        '<div class="bg"><i></i><i></i></div><canvas id="fx"></canvas>'
        '<svg id="defs" width="0" height="0" aria-hidden="true"><defs>'
        '<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#8d74ff"/><stop offset="1" stop-color="#33d4ff"/>'
        "</linearGradient></defs></svg>"
    )
    header = (
        '<header class="top"><div class="logo">'
        + LOGO
        + '</div><h1 id="ttl">'
        + escape(heading)
        + "<small>"
        + escape(tag)
        + "</small></h1>"
        + '<button type="button" class="ib" id="th" aria-label="'
        + escape(t("sub_page_toggle_theme"))
        + '">◐</button></header>'
    )
    return (
        "<!doctype html>\n"
        f'<html lang="{escape(t.locale)}" dir="{direction}">\n'
        f"<head>{head}</head>\n"
        f"<body>\n{deco}\n"
        f'<div class="wrap">{header}{pages}</div>\n'
        f"{nav}{sheet}"
        '<div class="toast" id="ts" role="status"></div>\n'
        '<script src="/s/_/sub.js"></script>\n</body>\n</html>'
    )


async def _page(db: DB, request: Request, service: Service, shop: Shop, panel: Panel) -> Response:
    customer = await db.get(Customer, service.customer_id)
    t = await texts_for(db, shop.id, (customer.locale if customer else None) or shop.default_locale)
    cfg = await shop_settings.load(db, shop.id)
    link = _link(service, request)
    title = shop.title or "PauruBot"
    tag = t("sub_page_tag")
    chevron = "‹" if t.locale == "fa" else "›"
    kind = delivery_of(panel.kind)
    if kind != "subscription":
        rows = await _links(panel, service) if kind == "wireguard" else []
        access = _special_access(t, service, kind, link, rows)
        pages = _initial(t, shop, service) + access
        return HTMLResponse(_document(t, title, title, tag, pages), headers=NO_STORE)
    nav_items = (
        NAV.replace("{home}", escape(t("sub_page_tab_home")))
        .replace("{configs}", escape(t("sub_page_tab_configs")))
        .replace("{share}", escape(t("sub_page_tab_share")))
        .replace("{apps}", escape(t("sub_page_tab_apps")))
    )
    rows = await _links(panel, service)
    pages = (
        _initial(t, shop, service)
        + _configs_page(t, link, rows)
        + _share(t, link)
        + _apps(
            t,
            link,
            cfg,
            title,
            str(cfg.get("support_username") or "").lstrip("@"),
            chevron,
        )
    )
    return HTMLResponse(
        _document(t, title, t("sub_page_tab_home"), tag, pages, nav=nav_items), headers=NO_STORE
    )
