"""Static HTML review page and the approved-list file it exports.

Previews stream from Splice's URL inside the browser only; nothing here
downloads or saves preview audio.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .budget import Group, get_group
from .models import Sample

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
BATCH_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _script_json(value) -> str:
    """JSON that is safe inside a <script> element."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _row(s: Sample) -> dict:
    return {
        "uuid": s.catalog_uuid,
        "name": s.basename,
        "pack": s.pack or s.name.split("/", 1)[0],
        "label": s.label or "",
        "bpm": s.bpm,
        "key": (s.key or "") + (" " + s.chord_type if s.key and s.chord_type else ""),
        "tags": ", ".join(label for label, _ in s.tags),
        "preview": s.preview_url,
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review __TITLE__</title>
<style>
  :root { --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea; --cur: #fff7d6; --off: #f2f2f2; --accent: #0a66c2; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #161617; --fg: #f2f2f2; --muted: #a1a1a6; --line: #333; --cur: #3a3320; --off: #222; --accent: #4ea1ff; }
  }
  body { background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif; margin: 0; }
  header { position: sticky; top: 0; background: var(--bg); border-bottom: 1px solid var(--line); padding: 10px 16px; z-index: 1; }
  header h1 { font-size: 16px; margin: 0 0 4px; }
  header p { margin: 0; color: var(--muted); }
  button { font: inherit; cursor: pointer; }
  #export { background: var(--accent); color: #fff; border: 0; border-radius: 6px; padding: 6px 12px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  th { color: var(--muted); font-weight: 500; }
  tr.current { background: var(--cur); }
  tr.off td:not(.ctl) { opacity: .45; text-decoration: line-through; }
  td.tags { color: var(--muted); font-size: 12px; max-width: 280px; }
  .err { color: #c62828; font-size: 12px; }
  kbd { border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <p><span id="count"></span> &middot; <kbd>j</kbd>/<kbd>k</kbd> next/prev &middot; <kbd>space</kbd> play/pause
     &middot; <kbd>x</kbd> toggle row &middot; <kbd>p</kbd> toggle pack
     &middot; <button id="export" type="button">Export approved</button></p>
</header>
<noscript>This page needs JavaScript.</noscript>
<table>
  <thead><tr><th class="ctl">OK</th><th>#</th><th>Preview</th><th>Name</th><th>Pack</th><th>Label</th><th>BPM</th><th>Key</th><th>Tags</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  'use strict';
  var data = JSON.parse(document.getElementById('data').textContent);
  var tbody = document.getElementById('rows');
  var rows = [];
  var cur = 0;

  function cell(tr, text, cls) {
    var td = document.createElement('td');
    if (cls) td.className = cls;
    if (text !== null && text !== undefined) td.textContent = String(text);
    tr.appendChild(td);
    return td;
  }

  data.rows.forEach(function (r, i) {
    var tr = document.createElement('tr');
    var ctl = cell(tr, null, 'ctl');
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = true;
    box.addEventListener('change', function () { refresh(); });
    ctl.appendChild(box);
    cell(tr, i + 1);
    var pv = cell(tr, null, 'ctl');
    var audio = document.createElement('audio');
    audio.preload = 'none';
    audio.controls = true;
    if (r.preview) audio.src = r.preview;
    var err = document.createElement('div');
    err.className = 'err';
    audio.addEventListener('error', function () { err.textContent = 'preview expired — re-run shortlist'; });
    if (!r.preview) err.textContent = 'no preview';
    pv.appendChild(audio);
    pv.appendChild(err);
    cell(tr, r.name);
    var packTd = cell(tr, r.pack + ' ');
    var packBtn = document.createElement('button');
    packBtn.type = 'button';
    packBtn.textContent = 'toggle pack';
    packBtn.addEventListener('click', function () { togglePack(i); });
    packTd.appendChild(packBtn);
    cell(tr, r.label);
    cell(tr, r.bpm === null ? '' : r.bpm);
    cell(tr, r.key);
    cell(tr, r.tags, 'tags');
    tr.addEventListener('click', function (e) { if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') select(i, false); });
    tbody.appendChild(tr);
    rows.push({ data: r, tr: tr, box: box, audio: audio });
  });

  function refresh() {
    var n = 0;
    rows.forEach(function (row) {
      row.tr.classList.toggle('off', !row.box.checked);
      if (row.box.checked) n++;
    });
    document.getElementById('count').textContent = n + ' of ' + rows.length + ' approved (' + n + ' credits)';
  }

  function playing() {
    return rows.some(function (row) { return !row.audio.paused; });
  }

  function select(i, keepPlaying) {
    if (!rows.length) return;
    var wasPlaying = keepPlaying && playing();
    rows.forEach(function (row) { row.audio.pause(); });
    rows[cur].tr.classList.remove('current');
    cur = Math.max(0, Math.min(rows.length - 1, i));
    rows[cur].tr.classList.add('current');
    rows[cur].tr.scrollIntoView({ block: 'nearest' });
    if (wasPlaying) play(rows[cur]);
  }

  function play(row) {
    try { row.audio.currentTime = 0; } catch (e) {}
    var p = row.audio.play();
    if (p && p.catch) p.catch(function () {});
  }

  function togglePack(i) {
    var pack = rows[i].data.pack;
    var value = !rows[i].box.checked;
    rows.forEach(function (row) { if (row.data.pack === pack) row.box.checked = value; });
    refresh();
  }

  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey || !rows.length) return;
    var row = rows[cur];
    if (e.key === 'j') { select(cur + 1, true); }
    else if (e.key === 'k') { select(cur - 1, true); }
    else if (e.key === ' ') { if (row.audio.paused) play(row); else row.audio.pause(); }
    else if (e.key === 'x') { row.box.checked = !row.box.checked; refresh(); }
    else if (e.key === 'p') { togglePack(cur); }
    else return;
    e.preventDefault();
  });

  document.getElementById('export').addEventListener('click', function () {
    var approved = rows.filter(function (row) { return row.box.checked; })
                       .map(function (row) { return row.data.uuid; });
    var body = JSON.stringify({ batch_id: data.batch_id, group: data.group, catalog_uuids: approved }, null, 2);
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([body], { type: 'application/json' }));
    a.download = 'approved-' + data.batch_id + '.json';
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });

  if (rows.length) rows[0].tr.classList.add('current');
  refresh();
})();
</script>
</body>
</html>
"""


def write_review_page(samples: list[Sample], group: Group, batch_id: str, out_dir: Path) -> Path:
    if not BATCH_RE.match(batch_id):
        raise ValueError(f"bad batch id {batch_id!r}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {"batch_id": batch_id, "group": group.id, "rows": [_row(s) for s in samples]}
    title = html.escape(f"{group.title} — batch {batch_id} ({len(samples)} sounds)")
    page = PAGE.replace("__TITLE__", title).replace("__DATA__", _script_json(data))
    path = out_dir / f"{batch_id}.html"
    path.write_text(page, encoding="utf-8")
    return path


def load_approved(path: Path) -> tuple[str, str, list[str]]:
    """Read an exported approved list: ``(batch_id, group_id, catalog_uuids)``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read approved file {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("approved file must be a JSON object")
    batch_id, group_id, uuids = data.get("batch_id"), data.get("group"), data.get("catalog_uuids")
    if not isinstance(batch_id, str) or not BATCH_RE.match(batch_id):
        raise ValueError("approved file: bad or missing batch_id")
    if not isinstance(group_id, str):
        raise ValueError("approved file: bad or missing group")
    try:
        get_group(group_id)
    except KeyError:
        raise ValueError(f"approved file: unknown group {group_id!r}") from None
    if not isinstance(uuids, list) or not all(isinstance(u, str) and UUID_RE.match(u) for u in uuids):
        raise ValueError("approved file: catalog_uuids must be a list of UUIDs")
    return batch_id, group_id, list(dict.fromkeys(uuids))
