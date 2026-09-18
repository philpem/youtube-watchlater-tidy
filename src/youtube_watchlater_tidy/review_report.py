from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .recovery import archive_links
from .reports import latest_snapshot_id

REVIEW_FORMAT = "youtube-watchlater-tidy-review-decisions-v1"
REVIEW_ACTIONS = {"keep", "review", "archive", "delete"}
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}


@dataclass(frozen=True)
class ReviewImportResult:
    snapshot_id: int
    requested: int
    changed: int
    unchanged: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _thumbnail(value: str | None) -> str | None:
    if not value:
        return None
    try:
        rows = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    candidates: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        width = row.get("width") if isinstance(row.get("width"), (int, float)) else 0
        height = row.get("height") if isinstance(row.get("height"), (int, float)) else 0
        candidates.append((int(width * height), row["url"]))
    return max(candidates, default=(0, ""))[1] or None


def review_rows(
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)

    rows = conn.execute(
        """
        SELECT e.position, e.video_id, e.title AS original_title,
               e.channel_id AS original_channel_id, e.channel AS original_channel,
               e.uploader AS original_uploader, e.uploader_id AS original_uploader_id,
               e.duration AS original_duration, e.view_count AS original_view_count,
               e.availability AS original_availability, e.thumbnails_json,
               m.title AS metadata_title, m.source AS metadata_source,
               m.channel_id AS metadata_channel_id, m.channel AS metadata_channel,
               m.uploader AS metadata_uploader, m.uploader_id AS metadata_uploader_id,
               m.duration AS metadata_duration, m.view_count AS metadata_view_count,
               m.upload_date AS metadata_upload_date, m.availability AS metadata_availability,
               a.has_video AS archive_has_video, a.raw_json AS archive_raw_json,
               da.preferred_title AS dearrow_title,
               d.action AS current_action, d.destination_playlist AS current_destination,
               d.source AS current_source, d.reason AS current_reason,
               lc.run_id AS llm_run_id, lc.action AS llm_action, lc.topic AS llm_topic,
               lc.content_type AS llm_content_type, lc.timeliness AS llm_timeliness,
               lc.quality AS llm_quality, lc.confidence AS llm_confidence,
               lc.reason AS llm_reason, lc.existing_playlist AS llm_existing_playlist,
               lc.new_queue_proposal AS llm_new_queue_proposal,
               lc.destination_confidence AS llm_destination_confidence,
               lc.destination_reason AS llm_destination_reason,
               lc.needs_description AS llm_needs_description,
               lc.needs_transcript AS llm_needs_transcript,
               la.run_id AS annotation_run_id,
               la.primary_category AS annotation_primary_category,
               la.subject AS annotation_subject,
               la.tags_json AS annotation_tags_json,
               la.content_type AS annotation_content_type,
               la.confidence AS annotation_confidence
        FROM snapshot_entries AS e
        LEFT JOIN preferred_metadata AS m ON m.video_id = e.video_id
        LEFT JOIN archive_lookups AS a
          ON a.id = (
              SELECT a2.id
              FROM archive_lookups AS a2
              WHERE a2.video_id = e.video_id AND a2.status = 'found'
              ORDER BY a2.id DESC
              LIMIT 1
          )
        LEFT JOIN preferred_dearrow AS da ON da.video_id = e.video_id
        LEFT JOIN current_decisions AS d
          ON d.snapshot_id = e.snapshot_id AND d.video_id = e.video_id
        LEFT JOIN llm_classifications AS lc
          ON lc.id = (
              SELECT c2.id
              FROM llm_classifications AS c2
              JOIN llm_classification_runs AS r2 ON r2.id = c2.run_id
              WHERE c2.video_id = e.video_id
                AND r2.snapshot_id = e.snapshot_id
                AND r2.status = 'complete'
              ORDER BY c2.id DESC
              LIMIT 1
          )
        LEFT JOIN llm_annotations AS la
          ON la.id = (
              SELECT a2.id
              FROM llm_annotations AS a2
              JOIN llm_annotation_runs AS ar2 ON ar2.id = a2.run_id
              WHERE a2.video_id = e.video_id
                AND ar2.snapshot_id = e.snapshot_id
                AND ar2.status = 'complete'
              ORDER BY a2.id DESC
              LIMIT 1
          )
        WHERE e.snapshot_id = ?
        ORDER BY e.position
        """,
        (snapshot_id,),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        original_title = str(row["original_title"] or "")
        metadata_title = row["metadata_title"]
        recovered_title = None
        if (
            original_title.casefold() in UNAVAILABLE_TITLES
            and isinstance(metadata_title, str)
            and metadata_title.strip()
        ):
            recovered_title = metadata_title

        channel = next(
            (
                str(value)
                for value in (
                    row["original_channel"],
                    row["original_uploader"],
                    row["metadata_channel"],
                    row["metadata_uploader"],
                )
                if value
            ),
            None,
        )
        channel_id = next(
            (
                str(value)
                for value in (
                    row["original_channel_id"],
                    row["original_uploader_id"],
                    row["metadata_channel_id"],
                    row["metadata_uploader_id"],
                )
                if value
            ),
            None,
        )
        duration = (
            row["original_duration"]
            if row["original_duration"] is not None
            else row["metadata_duration"]
        )
        views = (
            row["original_view_count"]
            if row["original_view_count"] is not None
            else row["metadata_view_count"]
        )
        availability = row["original_availability"] or row["metadata_availability"]

        recovered_video_links: list[dict[str, Any]] = []
        if row["archive_has_video"] and row["archive_raw_json"]:
            try:
                archive_raw = json.loads(row["archive_raw_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                archive_raw = {}
            if isinstance(archive_raw, dict):
                for link in archive_links(archive_raw):
                    if "video" not in link.contains.casefold():
                        continue
                    recovered_video_links.append(
                        {
                            "service": link.service,
                            "title": link.title,
                            "url": link.url,
                            "note": link.note,
                            "maybe_paywalled": link.maybe_paywalled,
                        }
                    )

        current = None
        if row["current_action"] not in (None, "clear"):
            current = {
                "action": row["current_action"],
                "destination_playlist": row["current_destination"],
                "source": row["current_source"],
                "reason": row["current_reason"],
            }

        llm = None
        if row["llm_run_id"] is not None:
            llm = {
                "run_id": int(row["llm_run_id"]),
                "action": row["llm_action"],
                "topic": row["llm_topic"],
                "content_type": row["llm_content_type"],
                "timeliness": row["llm_timeliness"],
                "quality": row["llm_quality"],
                "confidence": row["llm_confidence"],
                "reason": row["llm_reason"],
                "existing_playlist": row["llm_existing_playlist"],
                "new_queue_proposal": row["llm_new_queue_proposal"],
                "destination_confidence": row["llm_destination_confidence"],
                "destination_reason": row["llm_destination_reason"],
                "needs_description": bool(row["llm_needs_description"]),
                "needs_transcript": bool(row["llm_needs_transcript"]),
            }

        annotation = None
        if row["annotation_run_id"] is not None:
            try:
                annotation_tags = json.loads(row["annotation_tags_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                annotation_tags = []
            if not isinstance(annotation_tags, list):
                annotation_tags = []
            annotation = {
                "run_id": int(row["annotation_run_id"]),
                "primary_category": row["annotation_primary_category"],
                "subject": row["annotation_subject"],
                "tags": [str(tag) for tag in annotation_tags if isinstance(tag, str)],
                "content_type": row["annotation_content_type"],
                "confidence": row["annotation_confidence"],
            }

        result.append(
            {
                "position": int(row["position"]),
                "video_id": str(row["video_id"]),
                "url": f"https://www.youtube.com/watch?v={row['video_id']}",
                "original_title": original_title,
                "recovered_title": recovered_title,
                "metadata_source": row["metadata_source"],
                "recovered_video_links": recovered_video_links,
                "dearrow_title": row["dearrow_title"],
                "channel": channel,
                "channel_id": channel_id,
                "duration": float(duration) if duration is not None else None,
                "views": int(views) if views is not None else None,
                "upload_date": row["metadata_upload_date"],
                "availability": availability,
                "thumbnail": _thumbnail(row["thumbnails_json"]),
                "current_decision": current,
                "llm": llm,
                "annotation": annotation,
            }
        )
    return snapshot_id, result


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_review_html(snapshot_id: int, rows: list[dict[str, Any]]) -> str:
    data = _json_for_script({"snapshot_id": snapshot_id, "rows": rows})
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watch Later review — snapshot {snapshot_id}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ margin: 1rem; }}
header {{ background: Canvas; padding: .5rem 0; }}
.controls {{ display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }}
.pagination {{ display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; margin: .5rem 0; }}
.pagination button:disabled {{ opacity: .5; }}
input, select, button {{ font: inherit; padding: .35rem; }}
#summary {{ margin: .5rem 0; font-size: .9rem; }}
.facets {{ display: grid; gap: .55rem; margin: .7rem 0 1rem; padding: .65rem; border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); border-radius: .4rem; }}
.facet-row {{ display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }}
.facet-label {{ min-width: 7rem; font-weight: 600; }}
.facet-chip {{ border: 1px solid color-mix(in srgb, CanvasText 30%, transparent); border-radius: 999px; background: Canvas; cursor: pointer; padding: .2rem .5rem; }}
.facet-chip.active {{ font-weight: 700; outline: 2px solid color-mix(in srgb, CanvasText 45%, transparent); }}
.semantic-category {{ font-weight: 700; margin-bottom: .15rem; }}
.semantic-subject {{ margin-bottom: .25rem; }}
.semantic-tags {{ display: flex; flex-wrap: wrap; gap: .25rem; margin-bottom: .4rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
th, td {{ border-bottom: 1px solid color-mix(in srgb, CanvasText 25%, transparent); padding: .4rem; vertical-align: top; }}
th {{ position: sticky; top: 0; z-index: 1; background: Canvas; text-align: left; }}
.thumb {{ width: 120px; max-height: 80px; object-fit: contain; }}
.title {{ min-width: 20rem; }}
.reason {{ max-width: 28rem; white-space: normal; }}
.small {{ font-size: .78rem; opacity: .8; }}
.action {{ font-weight: 600; }}
.override-note {{ width: 15rem; }}
footer {{ margin-top: 1rem; font-size: .8rem; opacity: .8; }}
</style>
</head>
<body>
<header>
<h1>Watch Later review — snapshot {snapshot_id}</h1>
<div class="controls">
<label>Search <input id="search" type="search"></label>
<label>Current <select id="currentFilter"><option value="">all</option><option value="unresolved">unresolved</option><option>keep</option><option>review</option><option>archive</option><option>delete</option><option>move</option></select></label>
<label>LLM <select id="llmFilter"><option value="">all</option><option value="none">none</option><option>keep</option><option>review</option><option>archive</option><option>delete</option><option>move</option></select></label>
<label>Topic <select id="topicFilter"><option value="">all</option></select></label>
<label>Max confidence <input id="confidence" type="number" min="0" max="1" step="0.05" placeholder="1.0"></label>
<label>Sort <select id="sort"><option value="position">position ↑</option><option value="position-desc">position ↓</option><option value="confidence">confidence ↑</option><option value="confidence-desc">confidence ↓</option><option value="views-desc">views ↓</option></select></label>
<button id="exportButton">Export explicit overrides</button>
</div>
<div class="pagination">
<label>Rows/page <select id="pageSize"><option>50</option><option selected>100</option><option>250</option><option>500</option></select></label>
<button id="firstPage" type="button">« First</button>
<button id="previousPage" type="button">‹ Previous</button>
<span id="pageStatus" aria-live="polite"></span>
<button id="nextPage" type="button">Next ›</button>
<button id="lastPage" type="button">Last »</button>
</div>
<div id="summary"></div>
<section id="facets" class="facets">
<div class="facet-row"><span class="facet-label">Categories</span><div id="categoryFacets" class="facet-row"></div></div>
<div class="facet-row"><span class="facet-label">Top tags</span><div id="tagFacets" class="facet-row"></div></div>
<div class="facet-row"><button id="clearSemanticFilters" type="button">Clear semantic filters</button><span id="semanticStatus" class="small"></span></div>
</section>
</header>
<table>
<thead><tr><th>Pos</th><th>Video</th><th>Title / metadata</th><th>Current decision</th><th>Semantic annotation / LLM suggestion</th><th>Human override</th></tr></thead>
<tbody id="rows"></tbody>
</table>
<footer>Exported overrides are imported later as append-only decisions with source <code>human-review-report</code>. This page does not write SQLite or YouTube directly.</footer>
<script>
const DATA={data};
const state = new Map();
const selectedCategories = new Set();
const selectedTags = new Set();
let currentPage = 1;
const searchEl = document.getElementById('search');
const currentFilterEl = document.getElementById('currentFilter');
const llmFilterEl = document.getElementById('llmFilter');
const topicFilterEl = document.getElementById('topicFilter');
const confidenceEl = document.getElementById('confidence');
const sortEl = document.getElementById('sort');
const pageSizeEl = document.getElementById('pageSize');
const firstPageEl = document.getElementById('firstPage');
const previousPageEl = document.getElementById('previousPage');
const nextPageEl = document.getElementById('nextPage');
const lastPageEl = document.getElementById('lastPage');
const pageStatusEl = document.getElementById('pageStatus');
const rowsEl = document.getElementById('rows');
const summaryEl = document.getElementById('summary');
const categoryFacetsEl = document.getElementById('categoryFacets');
const tagFacetsEl = document.getElementById('tagFacets');
const clearSemanticFiltersEl = document.getElementById('clearSemanticFilters');
const semanticStatusEl = document.getElementById('semanticStatus');
const exportButtonEl = document.getElementById('exportButton');
const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const dur=s=>s==null?'-':`${{Math.floor(s/60)}}:${{String(Math.round(s%60)).padStart(2,'0')}}`;
const num=n=>n==null?'-':Number(n).toLocaleString();
const confidence=r=>r.llm?.confidence ?? null;
function populateTopics() {{
  [...new Set(DATA.rows.map(r=>r.llm?.topic).filter(Boolean))].sort().forEach(t=>{{
    const o=document.createElement('option'); o.value=t; o.textContent=t; topicFilterEl.append(o);
  }});
}}
function matchesBase(r) {{
  const q=searchEl.value.trim().toLowerCase();
  const annotation=r.annotation;
  if(q && ![
    r.video_id,r.original_title,r.recovered_title,r.dearrow_title,r.channel,
    ...(r.recovered_video_links||[]).flatMap(x=>[x.service,x.title]),
    r.llm?.topic,r.llm?.reason,
    annotation?.primary_category,annotation?.subject,annotation?.content_type,
    ...(annotation?.tags||[])
  ].filter(Boolean).join(' ').toLowerCase().includes(q)) return false;
  const currentAction=r.current_decision?.action ?? 'unresolved';
  if(currentFilterEl.value && currentAction!==currentFilterEl.value) return false;
  const llmAction=r.llm?.action ?? 'none';
  if(llmFilterEl.value && llmAction!==llmFilterEl.value) return false;
  if(topicFilterEl.value && r.llm?.topic!==topicFilterEl.value) return false;
  if(confidenceEl.value!=='' && (confidence(r)==null || confidence(r)>Number(confidenceEl.value))) return false;
  return true;
}}
function matchesSemantic(r) {{
  if(selectedCategories.size && !selectedCategories.has(r.annotation?.primary_category)) return false;
  if(selectedTags.size && !(r.annotation?.tags||[]).some(tag=>selectedTags.has(tag))) return false;
  return true;
}}
function matches(r) {{
  return matchesBase(r) && matchesSemantic(r);
}}
function countValues(values) {{
  const counts=new Map();
  values.filter(Boolean).forEach(value=>counts.set(value,(counts.get(value)||0)+1));
  return [...counts.entries()].sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0]));
}}
function toggleFacet(set,value) {{
  if(set.has(value)) set.delete(value); else set.add(value);
  currentPage=1;
  render();
}}
function bindFacetButtons(root) {{
  root.querySelectorAll('.category-chip').forEach(button=>button.addEventListener('click',()=>toggleFacet(selectedCategories,button.dataset.category)));
  root.querySelectorAll('.tag-chip').forEach(button=>button.addEventListener('click',()=>toggleFacet(selectedTags,button.dataset.tag)));
}}
function renderFacets() {{
  const population=DATA.rows.filter(matchesBase);
  const categories=countValues(population.map(r=>r.annotation?.primary_category));
  const tags=countValues(population.flatMap(r=>r.annotation?.tags||[])).slice(0,30);
  categoryFacetsEl.innerHTML=categories.length
    ? categories.map(([name,count])=>'<button type="button" class="facet-chip category-chip '+(selectedCategories.has(name)?'active':'')+'" data-category="'+esc(name)+'">'+esc(name)+' <span class="small">'+count+'</span></button>').join('')
    : '<span class="small">no semantic categories</span>';
  tagFacetsEl.innerHTML=tags.length
    ? tags.map(([tag,count])=>'<button type="button" class="facet-chip tag-chip '+(selectedTags.has(tag)?'active':'')+'" data-tag="'+esc(tag)+'">#'+esc(tag)+' <span class="small">'+count+'</span></button>').join('')
    : '<span class="small">no semantic tags</span>';
  bindFacetButtons(categoryFacetsEl);
  bindFacetButtons(tagFacetsEl);
  clearSemanticFiltersEl.disabled=selectedCategories.size===0 && selectedTags.size===0;
  const parts=[];
  if(selectedCategories.size) parts.push(selectedCategories.size+' categor'+(selectedCategories.size===1?'y':'ies')+' selected');
  if(selectedTags.size) parts.push(selectedTags.size+' tag(s) selected');
  semanticStatusEl.textContent=parts.join(' · ');
}}
function sorted(visible) {{
  const mode=sortEl.value;
  return [...visible].sort((a,b)=>{{
    if(mode==='position-desc') return b.position-a.position;
    if(mode==='confidence') return (confidence(a)??2)-(confidence(b)??2);
    if(mode==='confidence-desc') return (confidence(b)??-1)-(confidence(a)??-1);
    if(mode==='views-desc') return (b.views??-1)-(a.views??-1);
    return a.position-b.position;
  }});
}}
function semanticHtml(annotation) {{
  if(!annotation) return '<div class="small">no semantic annotation</div>';
  const category='<button type="button" class="facet-chip category-chip '+(selectedCategories.has(annotation.primary_category)?'active':'')+'" data-category="'+esc(annotation.primary_category)+'">'+esc(annotation.primary_category)+'</button>';
  const tags=(annotation.tags||[]).map(tag=>'<button type="button" class="facet-chip tag-chip '+(selectedTags.has(tag)?'active':'')+'" data-tag="'+esc(tag)+'">#'+esc(tag)+'</button>').join('');
  return '<div class="semantic-category">'+category+' · '+Math.round((annotation.confidence??0)*100)+'%</div>'
    +'<div class="semantic-subject">'+esc(annotation.subject)+'</div>'
    +'<div class="semantic-tags">'+tags+'</div>'
    +'<div class="small">'+esc(annotation.content_type)+' · annotation run '+annotation.run_id+'</div>';
}}
function suggestionHtml(llm) {{
  if(!llm) return '<div class="small">no stored LLM suggestion</div>';
  const destination=llm.existing_playlist?' → '+esc(llm.existing_playlist):llm.new_queue_proposal?' → '+esc(llm.new_queue_proposal):'';
  return '<div class="action">LLM suggestion: '+esc(llm.action)+' · '+Math.round((llm.confidence??0)*100)+'%</div>'
    +'<div>'+esc(llm.topic)+' · '+esc(llm.content_type)+' · '+esc(llm.timeliness)+'</div>'
    +'<div class="reason">'+esc(llm.reason)+'</div>'
    +'<div class="small">classification run '+llm.run_id+destination
    +(llm.needs_description?' · needs description':'')
    +(llm.needs_transcript?' · needs transcript':'')+'</div>';
}}
function rowHtml(r) {{
  const current=r.current_decision;
  const llm=r.llm;
  const annotation=r.annotation;
  const s=state.get(r.video_id) || {{action:'',note:''}};
  const thumb=r.thumbnail?`<a href="${{esc(r.url)}}" target="_blank"><img class="thumb" loading="lazy" referrerpolicy="no-referrer" src="${{esc(r.thumbnail)}}"></a>`:'';
  const recovered=r.recovered_title?`<div><b>Recovered:</b> ${{esc(r.recovered_title)}} <span class="small">(${{esc(r.metadata_source)}})</span></div>`:'';
  const recoveredVideos=(r.recovered_video_links||[]).map(x=>`<a href="${{esc(x.url)}}" target="_blank" rel="noopener noreferrer">${{esc(x.service)}}${{x.title&&x.title!=='archived resource'?' — '+esc(x.title):''}}</a>${{x.maybe_paywalled?' <span class="small">(may require access)</span>':''}}`).join(' · ');
  const recoveredVideo=recoveredVideos?`<div><b>Recovered video:</b> ${{recoveredVideos}}</div>`:'';
  const dearrow=r.dearrow_title?`<div><b>DeArrow:</b> ${{esc(r.dearrow_title)}}</div>`:'';
  const cur=current?`<div class="action">${{esc(current.action)}}${{current.destination_playlist?' → '+esc(current.destination_playlist):''}}</div><div class="small">${{esc(current.source)}}${{current.reason?' — '+esc(current.reason):''}}</div>`:'<span class="small">unresolved</span>';
  const lm=semanticHtml(annotation)+suggestionHtml(llm);
  return `<tr data-id="${{esc(r.video_id)}}"><td>${{r.position}}</td><td>${{thumb}}<div><a href="${{esc(r.url)}}" target="_blank">${{esc(r.video_id)}}</a></div></td><td class="title"><b>${{esc(r.original_title)}}</b>${{recovered}}${{recoveredVideo}}${{dearrow}}<div>${{esc(r.channel||'-')}}</div><div class="small">${{dur(r.duration)}} · ${{num(r.views)}} views · ${{esc(r.upload_date||'-')}} · ${{esc(r.availability||'-')}}</div></td><td>${{cur}}</td><td>${{lm}}</td><td><select class="override-action"><option value="">no override</option>${{['keep','review','archive','delete'].map(a=>`<option value="${{a}}" ${{s.action===a?'selected':''}}>${{a}}</option>`).join('')}}</select><br><input class="override-note" placeholder="optional note" value="${{esc(s.note)}}"></td></tr>`;
}}
function render() {{
  renderFacets();
  const visible=sorted(DATA.rows.filter(matches));
  const pageSize=Number(pageSizeEl.value);
  const pageCount=Math.max(1,Math.ceil(visible.length/pageSize));
  currentPage=Math.min(Math.max(1,currentPage),pageCount);
  const start=(currentPage-1)*pageSize;
  const pageRows=visible.slice(start,start+pageSize);
  rowsEl.innerHTML=pageRows.map(rowHtml).join('');
  bindFacetButtons(rowsEl);
  rowsEl.querySelectorAll('tr').forEach(tr=>{{
    const id=tr.dataset.id;
    tr.querySelector('.override-action').addEventListener('change',e=>{{
      const x=state.get(id)||{{action:'',note:''}}; x.action=e.target.value; state.set(id,x); renderSummary(visible.length,start,pageRows.length,pageCount);
    }});
    tr.querySelector('.override-note').addEventListener('input',e=>{{
      const x=state.get(id)||{{action:'',note:''}}; x.note=e.target.value; state.set(id,x);
    }});
  }});
  renderSummary(visible.length,start,pageRows.length,pageCount);
}}
function renderSummary(visibleCount,start,pageCountOnPage,pageCount) {{
  const overrideCount=[...state.values()].filter(x=>x.action).length;
  const first=visibleCount===0?0:start+1;
  const last=Math.min(start+pageCountOnPage,visibleCount);
  summaryEl.textContent=`Showing ${{first}}–${{last}} of ${{visibleCount}} matching / ${{DATA.rows.length}} total videos; ${{overrideCount}} explicit override(s)`;
  pageStatusEl.textContent=`Page ${{currentPage}} / ${{pageCount}}`;
  firstPageEl.disabled=previousPageEl.disabled=currentPage<=1;
  nextPageEl.disabled=lastPageEl.disabled=currentPage>=pageCount;
}}
function exportOverrides() {{
  const decisions=[];
  for(const r of DATA.rows) {{
    const x=state.get(r.video_id);
    if(x?.action) decisions.push({{video_id:r.video_id,action:x.action,note:x.note||null}});
  }}
  const payload={{format:'{REVIEW_FORMAT}',snapshot_id:DATA.snapshot_id,created_at:new Date().toISOString(),decisions}};
  const blob=new Blob([JSON.stringify(payload,null,2)+'\\n'],{{type:'application/json'}});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=`watchlater-review-${{DATA.snapshot_id}}.json`;
  a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}}
[searchEl,currentFilterEl,llmFilterEl,topicFilterEl,confidenceEl,sortEl,pageSizeEl].forEach(el=>el.addEventListener('input',()=>{{currentPage=1; render();}}));
clearSemanticFiltersEl.addEventListener('click',()=>{{
  selectedCategories.clear();
  selectedTags.clear();
  currentPage=1;
  render();
}});
firstPageEl.addEventListener('click',()=>{{currentPage=1; render();}});
previousPageEl.addEventListener('click',()=>{{currentPage-=1; render();}});
nextPageEl.addEventListener('click',()=>{{currentPage+=1; render();}});
lastPageEl.addEventListener('click',()=>{{
  const visibleCount=DATA.rows.filter(matches).length;
  currentPage=Math.max(1,Math.ceil(visibleCount/Number(pageSizeEl.value)));
  render();
}});
exportButtonEl.addEventListener('click',exportOverrides);
populateTopics();
render();
</script>
</body></html>
"""


def write_review_report(
    conn: sqlite3.Connection,
    output: str | Path,
    snapshot_id: int | None = None,
) -> tuple[int, int]:
    snapshot_id, rows = review_rows(conn, snapshot_id)
    Path(output).write_text(render_review_html(snapshot_id, rows), encoding="utf-8")
    return snapshot_id, len(rows)


def load_review_decisions(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != REVIEW_FORMAT:
        raise ValueError(f"review decision file must use format {REVIEW_FORMAT!r}")
    if not isinstance(value.get("snapshot_id"), int):
        raise ValueError("review decision file has no integer snapshot_id")
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("review decision file decisions must be an array")
    seen: set[str] = set()
    for index, row in enumerate(decisions):
        if not isinstance(row, dict):
            raise ValueError(f"decisions[{index}] must be an object")
        if set(row) - {"video_id", "action", "note"}:
            raise ValueError(f"decisions[{index}] contains unexpected fields")
        video_id = row.get("video_id")
        action = row.get("action")
        note = row.get("note")
        if not isinstance(video_id, str) or not video_id:
            raise ValueError(f"decisions[{index}].video_id must be a non-empty string")
        if video_id in seen:
            raise ValueError(f"duplicate video_id {video_id!r} in review decision file")
        seen.add(video_id)
        if action not in REVIEW_ACTIONS:
            raise ValueError(
                f"decisions[{index}].action must be one of {', '.join(sorted(REVIEW_ACTIONS))}"
            )
        if note is not None and not isinstance(note, str):
            raise ValueError(f"decisions[{index}].note must be a string or null")
    return value


def apply_review_decisions(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
) -> ReviewImportResult:
    snapshot_id = int(payload["snapshot_id"])
    snapshot = conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError(f"snapshot {snapshot_id} does not exist")

    decisions = payload["decisions"]
    present = {
        str(row["video_id"])
        for row in conn.execute(
            "SELECT video_id FROM snapshot_entries WHERE snapshot_id = ?", (snapshot_id,)
        )
    }
    missing = [row["video_id"] for row in decisions if row["video_id"] not in present]
    if missing:
        raise ValueError(
            f"review decision file contains video id(s) not in snapshot {snapshot_id}: "
            + ", ".join(missing)
        )

    changed = unchanged = 0
    planned: list[tuple[dict[str, Any], int | None]] = []
    for row in decisions:
        current = conn.execute(
            """
            SELECT id, action, reason, source
            FROM current_decisions
            WHERE snapshot_id = ? AND video_id = ?
            """,
            (snapshot_id, row["video_id"]),
        ).fetchone()
        note = row.get("note") or None
        if (
            current is not None
            and current["action"] == row["action"]
            and (current["reason"] or None) == note
            and current["source"] == "human-review-report"
        ):
            unchanged += 1
            continue
        changed += 1
        planned.append((row, int(current["id"]) if current is not None else None))

    if not dry_run and planned:
        with conn:
            for row, supersedes in planned:
                conn.execute(
                    """
                    INSERT INTO decision_events (
                        snapshot_id, video_id, action, destination_playlist,
                        source, rule_json, reason, created_at, supersedes_id
                    ) VALUES (?, ?, ?, NULL, 'human-review-report', NULL, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        row["video_id"],
                        row["action"],
                        row.get("note") or None,
                        _utc_now(),
                        supersedes,
                    ),
                )

    return ReviewImportResult(
        snapshot_id=snapshot_id,
        requested=len(decisions),
        changed=changed,
        unchanged=unchanged,
    )
