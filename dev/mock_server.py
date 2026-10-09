"""Local mock of the team VSS backend + an OpenAI-compatible LLM (W&B inference), on one port.

  python3 dev/mock_server.py            # :9100, LLM under /v1
  VSS_URL=http://127.0.0.1:9100 LLM_BASE_URL=http://127.0.0.1:9100/v1 python3 app/main.py

Test hooks:
  POST /mock/expire        invalidate every issued JWT (next VSS call gets 401 -> app must re-login)
  POST /mock/llm_down      toggle LLM failures (chat/completions -> 500)
  GET  /mock/stats         counters (logins, 401s, LLM calls by kind)
  The 4th authenticated VSS request also auto-expires the token once (EXPIRE_ONCE_AT).
  A query containing "LLMFAIL" makes chat/completions fail (plan_query fallback).
  A query containing "zebra" returns no hits. Sources with "_segment_003" have no detections (404).
Stdlib only.
"""

import json
import os
import random
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "9100"))
HERE = os.path.dirname(os.path.abspath(__file__))
MP4_PATH = os.environ.get("MOCK_MP4", os.path.join(HERE, "sample.mp4"))
EXPIRE_ONCE_AT = int(os.environ.get("MOCK_EXPIRE_ONCE_AT", "4"))
BUCKET = "s3://team-39-vss-chunks-segments/"

_lock = threading.Lock()
_state = {"tokens": set(), "n": 0, "auth_calls": 0, "expired_once": False, "llm_down": False,
          "stats": {"logins": 0, "unauthorized": 0, "llm": {}, "stream": 0, "stream_206": 0}}

if os.path.exists(MP4_PATH):
    with open(MP4_PATH, "rb") as f:
        MP4 = f.read()
else:  # not a playable video, but correct container signature + headers
    MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(range(256)) * 200


def seg_row(stamp, vid, chunk, seg, camera, location, desc, counts, uploaded):
    """One search row, shaped like the real VSS /api/v1/search results[] (verified live)."""
    orig = "s3://team-39-vss-chunks/team-39/%s_VID_%s_chunk_%04d.mp4" % (stamp, vid, chunk)
    source = "%ssegments/%s_VID_%s_chunk_%04d_segment_%03d_of_006.mp4" % (BUCKET, stamp, vid, chunk, seg)
    return {
        "filename": source.rsplit("/", 1)[-1], "source": source, "reasoning_content": desc, "is_public": True,
        "upload_timestamp": uploaded, "duration": 5, "segment_number": seg, "total_segments": 6,
        "segment_start_sec": (seg - 1) * 5, "segment_end_sec": seg * 5, "original_video": orig, "tags": [],
        "cosmos_model": "nvidia/Cosmos-Reason1-7B", "tokens_used": 912, "cached_prompt_tokens": 0,
        "camera_id": camera, "capture_type": "streets", "location": location,
        "perception_json": json.dumps({"model": "yolo11_coco", "object_counts": counts}),
        "object_classes": ",".join(sorted(counts)), "object_counts": json.dumps(counts),
        "max_detection_conf": 0.9287, "perception_ok": True, "perception_source": "yolo11_coco",
        "detection_sidecar_uri": None, "detection_frame_count": 150,
        "detection_count": sum(counts.values()) * 30, "extra_metadata": {},
    }


# Fake archive of segments: (keywords, row)
CLIPS = [
    (["suv", "white", "intersection", "pedestrian", "crosswalk", "moving vehicle", "行人", "路口"], seg_row(
        "20261008_070827", "20261006_174811_003", 25, 2, "nyc_streets_cam-1", "new_york",
        "A white SUV travels north through the signalized intersection. As it turns right, two pedestrians are "
        "mid-crosswalk; the SUV appears to slow sharply and stops roughly one car length from them before "
        "continuing. Overcast morning, wet pavement.",
        {"car": 4, "person": 3, "traffic light": 2}, "2026-10-08T06:57:28.491908")),
    (["pedestrian", "crosswalk", "crossing", "moving vehicle", "taxi", "行人"], seg_row(
        "20261008_071402", "20261006_175302_001", 7, 1, "nyc_streets_cam-2", "new_york",
        "Several pedestrians cross the avenue in the marked crosswalk while a yellow taxi waits at the stop line. "
        "One pedestrian crosses against the signal in front of a slowly moving sedan, which brakes.",
        {"person": 15, "car": 2, "truck": 2}, "2026-10-08T07:01:10.120044")),
    (["cyclist", "bike", "bicycle", "close", "car", "骑车"], seg_row(
        "20261008_073015", "20261006_181500_002", 12, 4, "nyc_bike_gopro-1", "new_york",
        "Seen from a cyclist's helmet camera: a dark sedan drifts toward the painted bike lane while overtaking "
        "and passes within an estimated half metre; the rider swerves slightly left.",
        {"bicycle": 1, "car": 5, "bus": 1}, "2026-10-08T07:12:44.008311")),
    (["delivery", "truck", "double-parked", "double parked", "stopped", "travel lane", "bike lane", "卡车", "挡路", "车道"], seg_row(
        "20261008_075944", "20261006_183020_004", 31, 3, "nyc_streets_cam-1", "new_york",
        "A white box delivery truck with hazard lights on is stopped in the right travel lane, partially blocking "
        "the bike lane. Cars merge left around it; a cyclist goes around the truck into traffic.",
        {"truck": 1, "car": 7, "bicycle": 1}, "2026-10-08T07:20:02.771200")),
    (["taxi", "curb", "pick up", "passenger", "stopping"], seg_row(
        "20261008_081120", "20261006_190011_001", 4, 5, "sf_streets_cam-3", "san_francisco",
        "A yellow taxi pulls to the curb near the bus stop and a passenger with a suitcase gets in. A city bus "
        "behind it waits briefly before changing lanes.",
        {"car": 3, "bus": 1, "person": 2}, "2026-10-08T07:31:55.300100")),
]


def score(query, kws):
    q = query.lower()
    hit = sum(1 for k in kws if k in q)
    return 0.24 + 0.05 * hit + random.uniform(0, 0.02) if hit else random.uniform(0.12, 0.22)


def do_search(body):
    query = body.get("query") or ""
    top_k = int(body.get("top_k", 10))
    if int(body.get("llm_top_n", 1)) < 1:
        raise ValueError("llm_top_n must be >= 1")
    # Like the real backend, min_similarity is NOT applied strictly: 0.2x rows come back too.
    if "zebra" in query.lower():
        scored = []
    else:
        scored = sorted(((round(score(query, k), 4), row) for k, row in CLIPS), key=lambda x: -x[0])[:top_k]
    results, chunks = [], []
    for s, row in scored:
        results.append(dict(row, similarity_score=s))
        chunks.append({k: row[k] for k in ("original_video", "reasoning_content", "is_public", "upload_timestamp",
                                           "tags", "camera_id", "capture_type", "location", "cosmos_model",
                                           "tokens_used", "cached_prompt_tokens")})
        chunks[-1].update(filename=row["original_video"].rsplit("/", 1)[-1], chunk_duration_sec=30,
                          total_segments=6, similarity_score=s, best_segment_number=row["segment_number"],
                          best_match_start_sec=row["segment_start_sec"], best_match_end_sec=row["segment_end_sec"],
                          preview_source=row["source"], matched_segment_count=2, query=query,
                          timeline=[], stream_id=None, chunk_index=0, stream_chunk_total=1)
    return {"results": results, "chunk_results": chunks, "total": len(results), "chunk_total": len(chunks),
            "query": query, "embedding_time_ms": 41.2, "search_time_ms": 88.7, "permission_filtered": 0,
            "llm_synthesis": {"response": "Found %d matching segment(s) for '%s'." % (len(results), query),
                              "model": "mock-vss-llm"},
            "sql_query": "SELECT * FROM segments ORDER BY cosine_distance(embedding, :q) LIMIT %d" % top_k}


def detections(source):
    """Shaped like the real /api/v1/videos/detections sidecar (verified live)."""
    rnd = random.Random(source)
    labels = ["car", "car", "person", "truck", "bicycle", "traffic light"]
    frames, counts = [], {}
    for i in range(0, 150, 5):  # 30 sampled frames of a 5 s 30 fps segment
        dets, per = [], {}
        for _ in range(rnd.randint(2, 6)):
            x1, y1 = rnd.randint(0, 1700), rnd.randint(300, 900)
            lab = rnd.choice(labels)
            per[lab] = per.get(lab, 0) + 1
            dets.append({"label": lab, "confidence": round(rnd.uniform(0.35, 0.95), 4),
                         "bbox": [x1, y1, min(1919, x1 + rnd.randint(60, 400)), min(1079, y1 + rnd.randint(40, 200))]})
        for k, v in per.items():
            counts[k] = max(counts.get(k, 0), v)
        frames.append({"frame_index": i, "time_sec": round(i / 30.0, 3), "shape": [1080, 1920], "detections": dets})
    return {"source": "yolo11_coco", "segment_source": source, "video_shape": [1080, 1920], "fps": 30,
            "frame_count": 150, "detection_count": sum(len(f["detections"]) for f in frames),
            "object_classes": ",".join(sorted(counts)), "object_counts": counts,
            "max_detection_conf": max(d["confidence"] for f in frames for d in f["detections"]), "frames": frames}


# ----------------------------------------------------------------------------- fake LLM

def llm_kind(system):
    s = system.lower()
    if "scenario card" in s or "simulation corner-case" in s:
        return "scene"
    if "incident report" in s:
        return "report"
    if "into a video search" in s:
        return "plan"
    return "other"


def llm_answer(kind, user):
    if kind == "plan":
        u = user.lower()
        et = ("cyclist-conflict" if re.search(r"cycl|bike|骑", u) else
              "double-parked" if re.search(r"truck|double|卡车|挡", u) else
              "pedestrian-conflict" if re.search(r"pedestrian|行人|crosswalk", u) else "vehicle-sighting")
        q = user if re.match(r"^[\x00-\x7f]+$", user) else "white SUV turning at intersection near pedestrians in crosswalk"
        return json.dumps({"query_en": q, "event_type": et, "intent_zh": "查找：" + user, "intent_en": "Find: " + q})
    if kind == "report":
        ids = [e.get("id") for e in json.loads(user).get("evidence", [])] or ["E1"]
        rep = {
            "title_en": "Near-miss between turning SUV and pedestrians", "title_zh": "转弯SUV与行人险些相撞",
            "events": [
                {"event_type": "pedestrian-conflict", "evidence": [ids[0]], "confidence": "medium",
                 "summary_en": "A white SUV appears to stop about one car length from pedestrians in the crosswalk.",
                 "summary_zh": "一辆白色SUV疑似在距人行横道行人约一个车身处停下。", "risk": "high"},
                {"event_type": "hard-brake", "evidence": ids[:2], "confidence": "low",
                 "summary_en": "The vehicle appears to brake hard before the crosswalk.",
                 "summary_zh": "车辆疑似在人行横道前急刹。", "risk": "medium"},
                # Deliberately unsupported: the guardrail must drop this one.
                {"event_type": "near-contact", "evidence": ["E9"], "confidence": "high",
                 "summary_en": "HALLUCINATED: two vehicles collide.", "summary_zh": "（幻觉）两车相撞。", "risk": "high"},
            ],
            "overall_en": "The footage appears to show a close pedestrian interaction at a turning intersection. "
                          "No contact is visible.",
            "overall_zh": "画面疑似显示转弯车辆与行人距离过近，未见接触。",
            "recommended_action_en": "Review signal timing for turning vehicles.",
            "recommended_action_zh": "建议复核转弯车辆信号配时。",
        }
        # Wrapped like a reasoning model would: think block + fenced JSON.
        return "<think>checking evidence ids</think>\nHere is the report:\n```json\n%s\n```" % json.dumps(rep, ensure_ascii=False)
    if kind == "scene":
        return json.dumps({
            "scenario_id": "CW-NYC-0001", "title": "Right-turning SUV yields late to crosswalk pedestrians",
            "event_type": "pedestrian-conflict",
            "ego": {"viewpoint": "fixed street camera", "notes": "Ego AV replays the SUV's path"},
            "environment": {"time_of_day": "morning", "lighting": "overcast daylight", "weather": "wet pavement",
                            "road_layout": "4-way signalized intersection", "traffic_control": "traffic light"},
            "actors": [{"id": "A1", "type": "suv", "color": "white", "initial_position": "southbound approach",
                        "maneuver": "right turn", "speed": "moderate"},
                       {"id": "A2", "type": "pedestrian", "color": "unknown", "initial_position": "crosswalk mid-point",
                        "maneuver": "crossing", "speed": "slow"}],
            "interaction": {"description": "SUV stops late before pedestrians", "min_gap_estimate": "~1 car length",
                            "conflict_point": "east crosswalk"},
            "trigger": "Pedestrians enter crosswalk as SUV begins turn",
            "expected_av_behavior": "Yield before the crosswalk; creep only once clear",
            "variations_to_simulate": ["pedestrian speed +50%", "night lighting", "occluding parked van"],
            "criticality": "high"})
    return "OK"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[mock]", self.command, self.path[:120], *args[1:2], flush=True)

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _authed(self, token=None):
        tok = token or (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)
        with _lock:
            _state["auth_calls"] += 1
            if not _state["expired_once"] and _state["auth_calls"] >= EXPIRE_ONCE_AT:
                _state["expired_once"] = True
                _state["tokens"].clear()
            ok = tok in _state["tokens"]
            if not ok:
                _state["stats"]["unauthorized"] += 1
        if not ok:
            self._send(401, {"detail": "Could not validate credentials"})
        return ok

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        if p == "/mock/stats":
            return self._send(200, dict(_state["stats"], tokens=len(_state["tokens"]), llm_down=_state["llm_down"]))
        if p == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": "meta-llama/Llama-3.1-8B-Instruct"},
                                                               {"id": "Qwen/Qwen3-235B-A22B-Instruct-2507"}]})
        if p == "/api/v1/videos/stream":
            if not self._authed(q.get("token")):
                return
            return self._stream()
        if p in ("/api/v1/videos/metadata", "/api/v1/videos/detections"):
            if not self._authed():
                return
            src = q.get("source", "")
            row = next((r for _, r in CLIPS if r["source"] == src), None)
            if p.endswith("metadata"):
                return self._send(200, row) if row else self._send(404, {"detail": "Video not found"})
            if "_segment_003" in src or not src:
                return self._send(404, {"detail": "No detections for this video"})
            return self._send(200, detections(src))
        return self._send(404, {"detail": "Not Found"})

    do_HEAD = do_GET

    def _stream(self):
        with _lock:
            _state["stats"]["stream"] += 1
        total = len(MP4)
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
            else:  # suffix range
                start, end = max(0, total - int(m.group(2))), total - 1
            end = min(end, total - 1)
            if start >= total or start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = MP4[start:end + 1]
            with _lock:
                _state["stats"]["stream_206"] += 1
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        else:
            data = MP4
            self.send_response(200)
        self.send_header("Content-Type", "binary/octet-stream")  # what the real S3-backed stream returns
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
        except ValueError:
            return self._send(422, {"detail": "invalid JSON"})
        if p == "/mock/expire":
            with _lock:
                _state["tokens"].clear()
            return self._send(200, {"expired": True})
        if p == "/mock/llm_down":
            _state["llm_down"] = not _state["llm_down"]
            return self._send(200, {"llm_down": _state["llm_down"]})
        if p == "/api/v1/auth/login":
            if not b.get("username") or not b.get("password"):
                return self._send(401, {"detail": "Incorrect username or password"})
            with _lock:
                _state["n"] += 1
                tok = "mock.jwt.%d" % _state["n"]
                _state["tokens"].add(tok)
                _state["stats"]["logins"] += 1
            return self._send(200, {"access_token": tok, "token_type": "bearer", "username": b["username"]})
        if p == "/api/v1/search":
            if not self._authed():
                return
            if "query" not in b:
                return self._send(422, {"detail": [{"loc": ["body", "query"], "msg": "field required"}]})
            try:
                return self._send(200, do_search(b))
            except ValueError as e:
                return self._send(422, {"detail": str(e)})
        if p == "/v1/chat/completions":
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                return self._send(401, {"error": {"message": "missing api key"}})
            msgs = b.get("messages") or []
            system = next((m["content"] for m in msgs if m.get("role") == "system"), "")
            user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
            kind = llm_kind(system)
            with _lock:
                _state["stats"]["llm"][kind] = _state["stats"]["llm"].get(kind, 0) + 1
            if _state["llm_down"] or "LLMFAIL" in user:
                return self._send(500, {"error": {"message": "mock LLM failure"}})
            return self._send(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "model": b.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": llm_answer(kind, user)}}],
                "usage": {"prompt_tokens": len(system + user) // 4, "completion_tokens": 200}})
        return self._send(404, {"detail": "Not Found"})


if __name__ == "__main__":
    print("[mock] VSS + LLM mock on :%d (mp4: %s, %d bytes)" % (PORT, MP4_PATH if os.path.exists(MP4_PATH) else "synthetic", len(MP4)), flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
