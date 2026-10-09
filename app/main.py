"""CarWitness (车证): find vehicle events across street cameras, back them with
Cosmos descriptions + YOLO detections, and write bilingual incident reports and
AV-simulation scene cards with W&B serverless inference.

Stdlib only, so the pod starts instantly from python:3.12-slim.

Env:
  VSS_URL, VSS_USERNAME, VSS_PASSWORD      team VSS backend (from the k8s Secret)
  WANDB_API_KEY, WANDB_TEAM, WANDB_PROJECT W&B serverless inference
  LLM_MODEL                                optional; otherwise picked from /v1/models
  PORT                                     default 8080
"""

import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")
WANDB_API_KEY = os.environ.get("WANDB_API_KEY", "")
WANDB_TEAM = os.environ.get("WANDB_TEAM", "")
WANDB_PROJECT = os.environ.get("WANDB_PROJECT", "")
LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.inference.wandb.ai/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
HERE = os.path.dirname(os.path.abspath(__file__))

# Closed event vocabulary: the LLM may only conclude these, each backed by a clip.
EVENT_TYPES = {
    "pedestrian-conflict": "Pedestrian close to / in front of a moving vehicle",
    "cyclist-conflict": "Cyclist close to a moving vehicle",
    "near-contact": "Two vehicles very close or nearly touching",
    "double-parked": "Vehicle stopped or double-parked in a travel lane",
    "curb-stop": "Vehicle stopping or parked at the curb",
    "cut-in": "Vehicle cutting in / abrupt lane change",
    "hard-brake": "Vehicle braking hard or stopping suddenly",
    "blocked-crosswalk": "Vehicle blocking a crosswalk or bike lane",
    "vehicle-sighting": "Target vehicle seen (matches the description)",
    "no-event": "Nothing notable",
}

# Event feed: rules evaluated against the archive (Clearcam-style alerts).
FEED_RULES = [
    {"id": "ped", "name_en": "Pedestrian in front of a moving vehicle", "name_zh": "行人在行驶车辆前方",
     "query": "pedestrian crossing in front of a moving vehicle", "event_type": "pedestrian-conflict"},
    {"id": "truck", "name_en": "Truck stopped at the curb", "name_zh": "卡车停靠路边",
     "query": "delivery truck parked at the curb", "event_type": "curb-stop"},
    {"id": "taxi", "name_en": "Taxi stopping for a passenger", "name_zh": "出租车停车接客",
     "query": "yellow taxi stopping to pick up a passenger", "event_type": "curb-stop"},
]
if os.environ.get("FEED_RULES_JSON"):
    FEED_RULES = json.loads(os.environ["FEED_RULES_JSON"])


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- HTTP helpers

def http_json(method, url, body=None, headers=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    # Cloudflare (W&B inference) rejects the default Python-urllib User-Agent with 403.
    h = {"Accept": "application/json", "User-Agent": "CarWitness/1.0 (+https://github.com/Stephen2077/carwitness)"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # Keep the status code but surface the server's error body (e.g. FastAPI 422 detail).
        try:
            e.msg = "%s: %s" % (e.msg, e.read()[:300].decode("utf-8", "replace"))
        except Exception:
            pass
        raise
    return json.loads(raw) if raw else {}


# ----------------------------------------------------------------------------- VSS client

class VSS:
    def __init__(self):
        self._token = None
        self._lock = threading.Lock()

    def token(self, refresh=False):
        with self._lock:
            if self._token and not refresh:
                return self._token
            r = http_json("POST", VSS_URL + "/api/v1/auth/login",
                          {"username": VSS_USERNAME, "password": VSS_PASSWORD}, timeout=30)
            self._token = r["access_token"]
            return self._token

    def call(self, method, path, body=None, timeout=120):
        for attempt in (0, 1):
            try:
                return http_json(method, VSS_URL + path, body,
                                 {"Authorization": "Bearer " + self.token(refresh=attempt == 1)},
                                 timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:
                    continue
                raise

    def search(self, query, top_k=10, min_similarity=0.3, metadata_filters=None):
        body = {"query": query, "top_k": top_k, "llm_top_n": 3, "min_similarity": min_similarity}
        if metadata_filters:
            body["metadata_filters"] = metadata_filters
        return self.call("POST", "/api/v1/search", body)

    def metadata(self, source):
        return self.call("GET", "/api/v1/videos/metadata?source=" + urllib.parse.quote(source, safe=""))

    def detections(self, source):
        try:
            return self.call("GET", "/api/v1/videos/detections?source=" + urllib.parse.quote(source, safe=""))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def stream_request(self, source, range_header=None):
        url = (VSS_URL + "/api/v1/videos/stream?source=" + urllib.parse.quote(source, safe="")
               + "&token=" + urllib.parse.quote(self.token()))
        h = {"User-Agent": "CarWitness/1.0"}
        if range_header:
            h["Range"] = range_header
        return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60)


vss = VSS()


# ----------------------------------------------------------------------------- result normalization

def pick(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def num(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def normalize_hit(r):
    """Map a VSS search row to the fields the UI needs; tolerant of key variants."""
    md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
    src = pick(r, "source", "segment_source", "preview_source", "s3_uri")
    start = num(pick(r, "best_match_start_sec", "segment_start_sec", "start_sec", "start_time", "start"))
    end = num(pick(r, "best_match_end_sec", "segment_end_sec", "end_sec", "end_time", "end"))
    tags = pick(r, "tags", default=[]) or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    classes = pick(r, "object_classes", default="")
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.split(",") if c.strip()]
    return {
        "source": src,
        "original_video": pick(r, "original_video", default=pick(md, "original_video")),
        "score": num(pick(r, "similarity_score", "score", "similarity")),
        "start": start,
        "end": end,
        "description": pick(r, "reasoning_content", "reasoning", "description", "caption", default=""),
        "camera_id": pick(r, "camera_id", default=pick(md, "camera_id")),
        "capture_type": pick(r, "capture_type", default=pick(md, "capture_type")),
        "location": pick(r, "location", default=pick(md, "location")),
        "timestamp": pick(r, "upload_timestamp", "timestamp", "capture_time", "created_at",
                          default=pick(md, "timestamp")),
        "tags": tags if isinstance(tags, list) else [],
        "object_classes": classes if isinstance(classes, list) else [],
        "objects": parse_counts(r.get("object_counts")) or object_counts_from_text(tags),
    }


def parse_counts(x):
    """VSS returns object_counts as a JSON string ('{"car": 2}'); accept a dict too."""
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            return {}
    if not isinstance(x, dict):
        return {}
    out = {}
    for k, v in x.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def object_counts_from_text(tags):
    """The VSS UI shows YOLO counts as tags like 'car 4'. Parse those if present."""
    out = {}
    for t in tags if isinstance(tags, list) else []:
        m = re.match(r"^\s*([a-z ]+?)\s+(\d+)\s*$", str(t))
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def summarize_detections(det):
    """Collapse a YOLO sidecar into {class: max count per frame} + a few sample boxes."""
    if not det:
        return {"counts": {}, "frames": 0, "boxes": [], "video_shape": None}
    frames = det.get("frames") if isinstance(det, dict) else None
    if frames is None and isinstance(det, dict):
        frames = det.get("detections") or det.get("results") or []
    if isinstance(det, list):
        frames = det
    frames = [f for f in frames or [] if isinstance(f, dict)]
    # Sample boxes from frames spread across the clip (~6 frames), not just the first ones.
    step = max(1, len(frames) // 6)
    counts, boxes = {}, []
    for fi, f in enumerate(frames):
        objs = f.get("detections") or f.get("objects") or f.get("boxes") or []
        if "label" in f and not objs:
            objs = [f]
        per, taken = {}, 0
        for o in objs:
            if not isinstance(o, dict):
                continue
            label = o.get("label") or o.get("class") or o.get("class_name") or o.get("name")
            if not label:
                continue
            per[label] = per.get(label, 0) + 1
            if fi % step == 0 and taken < 3 and len(boxes) < 40:
                taken += 1
                boxes.append({
                    "label": label,
                    "conf": num(pick(o, "confidence", "score", "conf")),
                    "bbox": o.get("bbox") or o.get("box") or o.get("xyxy"),
                    "t": num(pick(f, "time_sec", "timestamp", "time", "t")),
                })
        for k, v in per.items():
            counts[k] = max(counts.get(k, 0), v)
    if isinstance(det, dict) and parse_counts(det.get("object_counts")):
        counts = parse_counts(det.get("object_counts"))  # sidecar's own summary wins
    shape = det.get("video_shape") if isinstance(det, dict) else None
    return {"counts": counts, "frames": len(frames), "boxes": boxes, "video_shape": shape}


# ----------------------------------------------------------------------------- LLM (W&B inference)

_model_lock = threading.Lock()
PREFERRED_MODELS = ["Qwen3-235B-A22B-Instruct", "Llama-3.3-70B", "DeepSeek-V3", "gpt-oss-120b",
                    "Llama-4-Maverick", "Qwen", "Llama"]


def llm_model():
    global LLM_MODEL
    with _model_lock:
        if LLM_MODEL:
            return LLM_MODEL
        ids = [m.get("id") for m in http_json("GET", LLM_BASE + "/models", headers=llm_headers(), timeout=20).get("data", [])]
        log("W&B models:", ids)
        for pref in PREFERRED_MODELS:
            for i in ids:
                if i and pref.lower() in i.lower():
                    LLM_MODEL = i
                    return i
        if not ids:
            raise RuntimeError("no models listed at " + LLM_BASE + "/models")
        LLM_MODEL = ids[0]
        return LLM_MODEL


def llm_headers():
    h = {"Authorization": "Bearer " + WANDB_API_KEY}
    if WANDB_TEAM and WANDB_PROJECT:
        h["OpenAI-Project"] = WANDB_TEAM + "/" + WANDB_PROJECT
    return h


def _llm_json(system, user, max_tokens=1800):
    body = {
        "model": llm_model(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    for attempt in (0, 1):
        r = http_json("POST", LLM_BASE + "/chat/completions", body, llm_headers(), timeout=120)
        text = r["choices"][0]["message"]["content"] or ""
        try:
            return parse_json_loose(text)
        except ValueError as e:
            # Models occasionally emit slightly broken JSON; one retry at temperature 0 usually fixes it.
            if attempt:
                raise
            log("LLM JSON parse failed, retrying:", e)
            body["temperature"] = 0


# Trace LLM calls in W&B Weave when the package is installed (optional).
try:
    import weave  # noqa: E402
    if WANDB_API_KEY and WANDB_PROJECT:
        weave.init((WANDB_TEAM + "/" if WANDB_TEAM else "") + WANDB_PROJECT)
        llm_json = weave.op(name="carwitness_llm")(_llm_json)
        log("Weave tracing enabled")
    else:
        llm_json = _llm_json
except Exception:  # weave missing or init failed: run without tracing
    llm_json = _llm_json


def parse_json_loose(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("LLM did not return JSON: " + text[:200])
    return json.loads(text[start:end + 1])


# ----------------------------------------------------------------------------- core features

PLAN_SYSTEM = """You turn a user's request (Chinese or English) about vehicles on street cameras into a video search.
Return ONLY JSON: {"query_en": "...", "event_type": "...", "intent_zh": "...", "intent_en": "..."}
- query_en: a short, concrete English description of what would be VISIBLE in the clip (objects, colors, actions, positions), suitable for semantic search over captions. No plate numbers, no names.
- event_type: one of %s
- intent_zh / intent_en: one short sentence restating what the user is looking for.""" % list(EVENT_TYPES)


def plan_query(text):
    try:
        p = llm_json(PLAN_SYSTEM, text, max_tokens=300)
        if p.get("event_type") not in EVENT_TYPES:
            p["event_type"] = "vehicle-sighting"
        if not p.get("query_en"):
            p["query_en"] = text
        p["llm"] = True
        return p
    except Exception as e:
        log("plan_query fallback:", e)
        return {"query_en": text, "event_type": "vehicle-sighting", "intent_zh": text, "intent_en": text, "llm": False}


MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.25"))
CLIP_SLICE_BYTES = 2 * 1024 * 1024


def hits_from(raw, keep=3):
    """Normalize VSS rows; VSS doesn't enforce min_similarity, so drop weak hits here
    (but keep the top `keep` if everything is weak). Falls back to chunk_results."""
    rows = raw.get("results") or raw.get("chunk_results") or []
    hits = [h for h in (normalize_hit(r) for r in rows if isinstance(r, dict)) if h["source"]]
    hits.sort(key=lambda h: -(h["score"] or 0))
    good = [h for h in hits if (h["score"] or 0) >= MIN_SCORE]
    return good if good else hits[:keep]


def do_search(text, top_k=10, min_similarity=0.3):
    t0 = time.time()
    plan = plan_query(text)
    raw = vss.search(plan["query_en"], top_k=top_k, min_similarity=min_similarity)
    hits = hits_from(raw)
    # Attach the matched chunk window (jump-to-moment) where available.
    chunks = {c.get("original_video"): c for c in raw.get("chunk_results") or [] if c.get("original_video")}
    for h in hits:
        c = chunks.get(h["original_video"])
        if c and h["start"] is None:
            h["start"], h["end"] = num(c.get("best_match_start_sec")), num(c.get("best_match_end_sec"))
    syn = raw.get("llm_synthesis") or {}
    return {
        "plan": plan,
        "hits": hits,
        "synthesis": syn.get("response") if isinstance(syn, dict) else None,
        "cameras": sorted({h["camera_id"] for h in hits if h["camera_id"]}),
        "elapsed_sec": round(time.time() - t0, 2),
    }


REPORT_SYSTEM = """You are CarWitness, a careful video-evidence analyst for street-camera footage.
You receive evidence clips (id, Cosmos video description, YOLO object counts, time window).
Write an incident report about the user's question. Rules:
- event_type MUST be one of: %s
- Every event MUST cite at least one evidence id from the input. Never invent clips.
- Only state what the descriptions/detections support. Use hedged wording ("appears to", "疑似").
- Never read license plates or identify people.
Return ONLY JSON:
{"title_en": "...", "title_zh": "...",
 "events": [{"event_type": "...", "evidence": ["E1"], "confidence": "high|medium|low",
             "summary_en": "...", "summary_zh": "...", "risk": "high|medium|low"}],
 "overall_en": "2-3 sentences", "overall_zh": "2-3 句",
 "recommended_action_en": "...", "recommended_action_zh": "..."}""" % list(EVENT_TYPES)


def gather_evidence(hits, limit=4):
    ev = []
    for i, h in enumerate(hits[:limit]):
        det = None
        try:
            det = summarize_detections(vss.detections(h["source"]))
        except Exception as e:
            log("detections failed:", e)
        counts = (det or {}).get("counts") or h.get("objects") or {}
        ev.append({
            "id": "E%d" % (i + 1),
            "source": h["source"],
            "camera_id": h.get("camera_id"),
            "location": h.get("location"),
            "time_window_sec": [h.get("start"), h.get("end")],
            "timestamp": h.get("timestamp"),
            "description": (h.get("description") or "")[:1500],
            "yolo_counts": counts,
            "yolo_boxes": (det or {}).get("boxes", [])[:12],
        })
    return ev


def do_report(question, hits):
    ev = gather_evidence(hits)
    if not ev:
        return {"error": "no evidence"}
    prompt = json.dumps({"question": question,
                         "evidence": [{k: v for k, v in e.items() if k != "yolo_boxes"} for e in ev]},
                        ensure_ascii=False)
    rep = llm_json(REPORT_SYSTEM, prompt, max_tokens=2000)
    # Guardrail: drop any event that is off-vocabulary or cites no real evidence.
    valid_ids = {e["id"] for e in ev}
    kept, dropped = [], 0
    events = rep.get("events") if isinstance(rep.get("events"), list) else []
    for e in events:
        if not isinstance(e, dict):
            dropped += 1
            continue
        raw_cites = e.get("evidence") or []
        if not isinstance(raw_cites, list):
            raw_cites = [raw_cites]
        # Accept "E1", "e1", 1, "1" -> "E1"; anything else is unsupported.
        norm = [("E" + str(c)) if str(c).strip().isdigit() else str(c).strip().upper() for c in raw_cites]
        cites = [c for c in dict.fromkeys(norm) if c in valid_ids]
        if e.get("event_type") in EVENT_TYPES and cites:
            e["evidence"] = cites
            kept.append(e)
        else:
            dropped += 1
    rep["events"] = kept
    rep["dropped_unsupported"] = dropped
    rep["evidence"] = ev
    rep["model"] = LLM_MODEL
    rep["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rep


SCENE_SYSTEM = """You convert one real street-camera clip into an autonomous-vehicle simulation corner-case scenario card.
Use only what the description and detections support; mark unknowns as "unknown".
Return ONLY JSON:
{"scenario_id": "...", "title": "...", "event_type": "one of %s",
 "ego": {"viewpoint": "fixed street camera|dashcam|unknown", "notes": "..."},
 "environment": {"time_of_day": "...", "lighting": "...", "weather": "...", "road_layout": "...", "traffic_control": "..."},
 "actors": [{"id": "A1", "type": "car|suv|truck|bus|bicycle|pedestrian|motorcycle|other", "color": "...",
             "initial_position": "...", "maneuver": "...", "speed": "stopped|slow|moderate|fast|unknown"}],
 "interaction": {"description": "...", "min_gap_estimate": "...", "conflict_point": "..."},
 "trigger": "...", "expected_av_behavior": "...", "variations_to_simulate": ["...", "..."],
 "criticality": "high|medium|low"}""" % list(EVENT_TYPES)


def do_scene_card(hit):
    ev = gather_evidence([hit], limit=1)[0]
    card = llm_json(SCENE_SYSTEM, json.dumps({k: v for k, v in ev.items() if k != "id"}, ensure_ascii=False),
                    max_tokens=1500)
    card["source_clip"] = {"s3_uri": ev["source"], "time_window_sec": ev["time_window_sec"],
                           "camera_id": ev["camera_id"], "timestamp": ev["timestamp"]}
    card["evidence"] = {"cosmos_description": ev["description"], "yolo_counts": ev["yolo_counts"],
                        "yolo_boxes": ev["yolo_boxes"]}
    card["generator"] = {"app": "CarWitness", "model": LLM_MODEL,
                         "pipeline": "VAST DataEngine + NVIDIA Cosmos3-Reason/Embed1 + YOLO11"}
    return card


_feed = {"updated": None, "rules": [], "error": None}
_feed_lock = threading.Lock()


def refresh_feed():
    out = []
    for rule in FEED_RULES:
        try:
            raw = vss.search(rule["query"], top_k=4, min_similarity=0.3)
            hits = hits_from(raw, keep=2)
        except Exception as e:
            log("feed rule failed", rule.get("id"), e)
            hits = []
        out.append(dict(rule, hits=hits))
    with _feed_lock:
        _feed.update(updated=time.strftime("%H:%M:%S"), rules=out, error=None)


def feed_loop():
    while True:
        try:
            refresh_feed()
        except Exception as e:
            _feed["error"] = str(e)
            log("feed error", e)
        time.sleep(300)


# ----------------------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(self.command, self.path.split("?")[0], *args[1:2])

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _route(self):
        u = urllib.parse.urlparse(self.path)
        # Ingress strips /app, but tolerate it if it ever arrives un-stripped.
        path = u.path[4:] if u.path.startswith("/app/") else ("/" if u.path == "/app" else u.path)
        return path, urllib.parse.parse_qs(u.query)

    def do_GET(self):
        path, q = self._route()
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, {"ok": True})
            if path == "/api/feed":
                with _feed_lock:
                    return self._send(200, dict(_feed))
            if path == "/api/config":
                return self._send(200, {"event_types": EVENT_TYPES, "model": LLM_MODEL or None,
                                        "rules": [{k: r.get(k) for k in ("id", "name_en", "name_zh", "query")} for r in FEED_RULES]})
            if path in ("/api/detections", "/api/clip") and not q.get("source"):
                return self._send(400, {"error": "missing ?source="})
            if path == "/api/detections":
                return self._send(200, summarize_detections(vss.detections(q["source"][0])))
            if path == "/api/clip":
                return self._proxy_clip(q["source"][0])
            return self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(502, {"error": str(e)})

    def do_POST(self):
        path, _ = self._route()
        try:
            try:
                b = self._body()
            except ValueError:
                return self._send(400, {"error": "invalid JSON body"})
            need = {"/api/search": "query", "/api/report": "hits", "/api/scenecard": "hit"}.get(path)
            if need and not b.get(need):
                return self._send(400, {"error": "missing field: " + need})
            if path == "/api/search":
                return self._send(200, do_search(b["query"], top_k=int(b.get("top_k", 10)),
                                                 min_similarity=float(b.get("min_similarity", 0.3))))
            if path == "/api/report":
                return self._send(200, do_report(b.get("question", ""), b["hits"]))
            if path == "/api/scenecard":
                return self._send(200, do_scene_card(b["hit"]))
            if path == "/api/feed/refresh":
                refresh_feed()
                with _feed_lock:
                    return self._send(200, dict(_feed))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(502, {"error": str(e)})

    def _proxy_clip(self, source):
        """Range-capable proxy so the browser never sees the VSS JWT."""
        rng = self.headers.get("Range")
        # Serve open-ended ranges ("bytes=N-") in slices: the outer proxies buffer whole
        # responses, so a full 5–10 MB segment per request stalls the first frame.
        m = re.match(r"^bytes=(\d+)-$", rng or "")
        if m:
            first = int(m.group(1))
            rng = "bytes=%d-%d" % (first, first + CLIP_SLICE_BYTES - 1)
        try:
            try:
                up = vss.stream_request(source, rng)
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):
                    raise
                vss.token(refresh=True)
                up = vss.stream_request(source, rng)
        except urllib.error.HTTPError as e:
            # Pass 404/416 etc. through instead of a generic 502.
            return self._send(e.code, {"error": "clip upstream HTTP %d" % e.code})
        with up:
            self.send_response(up.status)
            ctype = up.headers.get("Content-Type") or ""
            # VSS/S3 serves segments as binary/octet-stream; browsers need video/mp4 to play them.
            if not ctype.startswith("video/"):
                ctype = "video/mp4"
            self.send_header("Content-Type", ctype)
            for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if up.headers.get(h):
                    self.send_header(h, up.headers[h])
            if not up.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            if not up.headers.get("Content-Length"):
                # No length: we can only delimit the body by closing the connection.
                self.send_header("Connection", "close")
                self.close_connection = True
            try:
                self.end_headers()
                while True:
                    chunk = up.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

def main():
    missing = [k for k in ("VSS_URL", "VSS_USERNAME", "VSS_PASSWORD", "WANDB_API_KEY") if not os.environ.get(k)]
    if missing:
        log("WARNING missing env:", missing)
    threading.Thread(target=feed_loop, daemon=True).start()
    log("CarWitness listening on :%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
