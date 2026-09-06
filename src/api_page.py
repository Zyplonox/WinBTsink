"""
api_page.py – the remote-control page served by api_server.py
==============================================================
A single self-contained HTML document: no external assets, so the API
server can hand it out as one response and it works offline.

It lives in its own module because its long HTML/JavaScript lines would
otherwise dominate the server code; line-length linting is switched off
for this file in pyproject.toml.
"""

from __future__ import annotations

INDEX_HTML = """<!doctype html><meta charset="utf-8"><title>BT-AudioSink</title>
<style>body{font-family:system-ui;background:#111827;color:#e5e7eb;margin:2em}
.card{background:#1f2937;border-radius:8px;padding:1em;margin:.5em 0}button{margin:.2em}
input[type=range]{width:200px}</style>
<h2>BT-AudioSink</h2><div id="s">loading…</div>
<script>
async function api(p,b){const r=await fetch('/api'+p,{method:b?'POST':'GET',headers:{'content-type':'application/json'},body:b?JSON.stringify(b):undefined});return r.json();}
async function render(){const s=await api('/status');let h=`<p>state: <b>${s.state}</b> · pairing ${s.pairing_allowed?'on':'off'}
 <button onclick="api('/pairing',{allowed:${!s.pairing_allowed}}).then(render)">toggle</button>
 · master <input type=range min=0 max=200 value=${Math.round(s.master_volume*100)} onchange="api('/volume',{percent:+this.value})"></p>`;
for(const d of s.devices){h+=`<div class=card><b>${d.name||d.addr}</b> <small>${d.addr}</small> ${d.streaming?'▶ '+d.codec:''} ${d.playback}<br>
<button onclick="api('/devices/${d.addr}/player',{action:'prev'})">⏮</button>
<button onclick="api('/devices/${d.addr}/player',{action:'${d.playback==='playing'?'pause':'play'}'}).then(render)">${d.playback==='playing'?'⏸':'▶'}</button>
<button onclick="api('/devices/${d.addr}/player',{action:'next'})">⏭</button>
<button onclick="api('/devices/${d.addr}/mute',{muted:${!d.muted}}).then(render)">${d.muted?'🔇':'🔊'}</button>
<input type=range min=0 max=100 value=${Math.round(d.volume*100)} onchange="api('/devices/${d.addr}/volume',{percent:+this.value})">
<button onclick="api('/devices/${d.addr}/disconnect').then(render)">✕</button></div>`;}
for(const r of s.remembered){if(!s.devices.some(d=>d.addr===r.addr))h+=`<div class=card>${r.name||r.addr} <small>${r.addr}</small>
 <button onclick="api('/devices/${r.addr}/connect').then(render)">connect</button></div>`;}
document.getElementById('s').innerHTML=h;}
render();setInterval(render,3000);
</script>"""
