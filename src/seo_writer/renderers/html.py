# ruff: noqa: E501
"""Self-contained semantic HTML reports with no external dependencies."""

from __future__ import annotations

import json
from html import escape
from typing import Any


def _e(value: Any) -> str:
    return escape(str(value or ""), quote=True)


def _safe_items(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        if item.get("status") == "confidential" or item.get("publishability") == "confidential":
            continue
        statement = _e(item.get("statement") or item.get("title") or item.get("label"))
        status = _e(item.get("status", "unknown")).replace("_", " ").title()
        rows.append(
            f'<article class="record"><div><h3>{statement}</h3>'
            f"<p>{_e(item.get('description', ''))}</p></div>"
            f'<span class="status">{status}</span></article>'
        )
    return "".join(rows) or '<p class="empty">No publishable records are available.</p>'


def render_report(
    context: dict[str, Any],
    sections: list[dict[str, Any]],
    *,
    review_seed: dict[str, Any] | None = None,
) -> str:
    """Render byte-stable HTML. Callers supply only trusted renderer fragments in ``html``."""
    title = _e(context.get("title", "Review report"))
    seed = json.dumps(review_seed or {}, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    section_html = []
    for index, section in enumerate(sections, 1):
        heading = _e(section.get("heading", "Section"))
        body = section.get("html")
        if body is None:
            body = _safe_items(section.get("items", []))
        section_html.append(
            f'<section id="section-{index}" aria-labelledby="heading-{index}">'
            f'<h2 id="heading-{index}">{heading}</h2>{body}</section>'
        )
    nav = "".join(
        f'<a href="#section-{index}">{_e(section.get("heading", "Section"))}</a>'
        for index, section in enumerate(sections, 1)
    )
    css = """
:root{color-scheme:light;--ink:#101b2d;--muted:#536174;--paper:#f7f7f3;--surface:#fff;--line:#cdd5df;--accent:#1268d3;--soft:#e7effa;--radius:12px}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--accent)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.shell{width:min(1180px,calc(100% - 48px));margin:auto}.masthead{padding:38px 0 26px;border-bottom:1px solid var(--line)}.mast-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);gap:32px;align-items:end}.kicker{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}h1{font-size:clamp(34px,5vw,58px);line-height:1.02;letter-spacing:-.045em;margin:8px 0 12px;max-width:14ch}h2{font-size:24px;letter-spacing:-.02em;margin:0 0 20px}h3{font-size:16px;margin:0 0 5px}.meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.meta div{border-left:2px solid var(--accent);padding-left:10px}.meta dt{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.meta dd{margin:2px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}.stage-nav{display:flex;gap:8px;overflow:auto;padding:14px 0}.stage-nav a{white-space:nowrap;text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:7px 12px;background:var(--surface);color:var(--ink)}main{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:36px;padding:22px 0 60px}section{padding:28px 0;border-bottom:1px solid var(--line)}.content{min-width:0}.review-summary{position:sticky;top:18px;align-self:start;background:var(--ink);color:#f4f7fb;border-radius:var(--radius);padding:20px}.review-summary button{width:100%;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:700;padding:11px 14px;cursor:pointer}.review-summary button:active{transform:translateY(1px)}.review-summary p{color:#c7d1df}.record{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;padding:16px 0;border-bottom:1px solid var(--line)}.status{background:var(--soft);color:#174f91;border-radius:999px;padding:4px 9px;font-size:12px;height:max-content}.empty{color:var(--muted);font-style:italic}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--surface)}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px}.heat{min-width:64px;text-align:center}.heat-0{background:#f1f3f5}.heat-1{background:#d9e8fb}.heat-2{background:#9bc4f2}.heat-3{background:#3986dd;color:#071526}.chart{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px;margin:14px 0}.chart svg{display:block;width:100%;height:auto}.evidence details{background:var(--surface);border-radius:var(--radius);padding:12px 14px;margin:8px 0}.field{display:grid;gap:6px;margin:14px 0}.field label{font-weight:700}.field input,.field textarea,.field select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid #7a8798;border-radius:8px;padding:10px}.field small{color:var(--muted)}footer{border-top:1px solid var(--line);padding:20px 0 40px;color:var(--muted);font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}@media(max-width:800px){.mast-grid,main{grid-template-columns:1fr}.review-summary{position:static;order:-1}.meta{grid-template-columns:1fr 1fr}}@media (max-width: 600px){.shell{width:min(100% - 28px,1180px)}.masthead{padding-top:24px}.meta{grid-template-columns:1fr}h1{font-size:36px}.record{grid-template-columns:1fr}.stage-nav{margin-inline:-14px;padding-inline:14px}}@media print{body{background:#fff;color:#111;font-size:11pt}.shell{width:100%}.stage-nav,.review-summary button{display:none}main{display:block}.review-summary{position:static;background:#fff;color:#111;border:1px solid #777;break-inside:avoid}.chart,section,.record{break-inside:avoid}a{color:#111;text-decoration:none}footer{font-size:8pt}}
html,body{max-width:100%;overflow-x:hidden}.table-wrap{max-width:100%;min-width:0;overflow-x:auto}.chart{min-width:0;overflow:hidden}.evidence a{overflow-wrap:anywhere;word-break:break-word}
""".strip()
    js = """
const seed=JSON.parse(document.getElementById('review-seed').textContent||'{}');
document.getElementById('download-review')?.addEventListener('click',()=>{
 const payload={...seed,reviewed_at:new Date().toISOString()};
 document.querySelectorAll('[data-review-field]').forEach(el=>{if(payload.profile)payload.profile[el.dataset.reviewField]=el.value.includes('\\n')?el.value.split('\\n').map(v=>v.trim()).filter(Boolean):el.value;else payload[el.dataset.reviewField]=el.value});
 const decisions=[];document.querySelectorAll('[data-decision]').forEach(row=>{const item={};row.querySelectorAll('[data-key]').forEach(el=>item[el.dataset.key]=el.value);decisions.push(item)});if(decisions.length)payload.decisions=decisions;
 const blob=new Blob([JSON.stringify(payload,null,2)+'\\n'],{type:'application/json'});
 const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=(seed.review_type||'review')+'-review.json';link.click();URL.revokeObjectURL(link.href);
});
""".strip()
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{css}</style></head><body>"
        '<header class="masthead"><div class="shell mast-grid"><div>'
        '<div class="kicker">SEO Writer analytical review</div>'
        f"<h1>{title}</h1><p>{_e(context.get('status', 'Review ready'))}</p></div>"
        '<dl class="meta">'
        f"<div><dt>Brand</dt><dd>{_e(context.get('brand'))}</dd></div>"
        f"<div><dt>Workspace</dt><dd>{_e(context.get('workspace'))}</dd></div>"
        f"<div><dt>Run</dt><dd>{_e(context.get('run_id') or 'Not applicable')}</dd></div>"
        f"<div><dt>Revision</dt><dd>{_e(context.get('revision', 1))}</dd></div></dl></div></header>"
        f'<nav class="shell stage-nav" aria-label="Review stages">{nav}</nav>'
        '<main class="shell"><div class="content">'
        + "".join(section_html)
        + '</div><aside class="review-summary"><h2>Review summary</h2>'
        "<p>Core content remains readable without JavaScript. Use the button to download structured feedback.</p>"
        '<button id="download-review" type="button">Download review JSON</button>'
        "<noscript><p>JavaScript is disabled. The report remains readable, but JSON download is unavailable.</p></noscript>"
        "</aside></main>"
        f'<footer class="shell">Input hash: {_e(context.get("input_hash"))}<br>Generator: seo-writer | Rules: {_e(context.get("rules_version", "current"))}</footer>'
        f'<script id="review-seed" type="application/json">{seed}</script><script>{js}</script></body></html>\n'
    )
