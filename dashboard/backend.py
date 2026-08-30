#!/usr/bin/env python3
"""backend — pure API, port 5051. Redis-backed proxy listing with lightweight indexes."""
import json, os, time, urllib.parse, hashlib, threading
from functools import lru_cache
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import redis, sys

sys.path.insert(0, "/app")
import geo

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

HOST = os.environ.get("REDIS_HOST", "proxy-redis")
_pool0 = redis.ConnectionPool(host=HOST, port=6379, db=0, decode_responses=True,
                               socket_timeout=5, socket_connect_timeout=3)
_pool1 = redis.ConnectionPool(host=HOST, port=6379, db=1, decode_responses=True,
                               socket_timeout=5, socket_connect_timeout=3)
r0 = redis.Redis(connection_pool=_pool0)
r1 = redis.Redis(connection_pool=_pool1)
PER_PAGE = 50
MAX_LIMIT = 200
CACHE_TTL = 60
INDEX_TTL = 900
BAD_IPS = {"0.0.0.0", "127.0.0.1", "localhost", "::1"}
_jhao_cache = {"t": 0, "d": {}}
_index_lock = threading.Lock()
_index_building = False
_stats_cache = {"t": 0, "d": None}
_stats_lock = threading.Lock()

COUNTRY_NAME = {
    "CN": "中国", "HK": "香港", "TW": "台湾", "MO": "澳门", "US": "美国",
    "JP": "日本", "KR": "韩国", "SG": "新加坡", "TH": "泰国", "ID": "印尼",
    "IN": "印度", "VN": "越南", "MY": "马来西亚", "PH": "菲律宾", "GB": "英国",
    "DE": "德国", "FR": "法国", "RU": "俄罗斯", "BR": "巴西", "CA": "加拿大",
    "IT": "意大利", "ES": "西班牙", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士",
    "AU": "澳大利亚", "NZ": "新西兰", "PK": "巴基斯坦", "BD": "孟加拉国",
    "UA": "乌克兰", "PL": "波兰", "CZ": "捷克", "AR": "阿根廷", "MX": "墨西哥",
    "CL": "智利", "ZA": "南非", "EG": "埃及", "NG": "尼日利亚", "KE": "肯尼亚",
    "SA": "沙特阿拉伯", "AE": "阿联酋", "TR": "土耳其", "IL": "以色列",
    "FI": "芬兰", "NO": "挪威", "DK": "丹麦", "IE": "爱尔兰", "AT": "奥地利",
    "BE": "比利时", "CO": "哥伦比亚", "IR": "伊朗", "KH": "柬埔寨",
    "EC": "厄瓜多尔", "RO": "罗马尼亚", "KZ": "哈萨克斯坦", "PE": "秘鲁",
    "BZ": "伯利兹", "CW": "库拉索", "EE": "爱沙尼亚", "LT": "立陶宛",
    "VE": "委内瑞拉", "BG": "保加利亚", "BO": "玻利维亚", "SY": "叙利亚",
    "VG": "英属维京", "HN": "洪都拉斯", "PY": "巴拉圭", "IQ": "伊拉克",
    "RS": "塞尔维亚", "ZW": "津巴布韦", "GT": "危地马拉", "DO": "多米尼加",
    "SC": "塞舌尔", "CR": "哥斯达黎加", "IM": "马恩岛", "PA": "巴拿马",
    "MN": "蒙古", "LV": "拉脱维亚", "CY": "塞浦路斯",
    "LY": "利比亚", "NP": "尼泊尔", "OM": "阿曼", "HU": "匈牙利",
    "GE": "格鲁吉亚", "PS": "巴勒斯坦", "BA": "波黑", "SR": "苏里南",
    "HR": "克罗地亚", "AO": "安哥拉", "LA": "老挝", "TT": "特立尼达",
    "AF": "阿富汗", "YE": "也门", "LU": "卢森堡", "XK": "科索沃",
    "RW": "卢旺达", "BI": "布隆迪", "SS": "南苏丹", "UG": "乌干达",
    "MC": "摩纳哥", "MZ": "莫桑比克", "GQ": "赤道几内亚", "CD": "刚果金",
    "HT": "海地", "SV": "萨尔瓦多", "GU": "关岛", "MF": "法属圣马丁",
    "MM": "缅甸", "GR": "希腊", "MT": "马耳他", "NA": "纳米比亚",
    "TZ": "坦桑尼亚", "BY": "白俄罗斯", "TM": "土库曼斯坦", "SI": "斯洛文尼亚",
    "SK": "斯洛伐克", "MD": "摩尔多瓦", "AL": "阿尔巴尼亚", "PR": "波多黎各",
    "GH": "加纳", "AZ": "阿塞拜疆", "UZ": "乌兹别克斯坦", "UY": "乌拉圭",
    "NI": "尼加拉瓜", "PT": "葡萄牙", "LB": "黎巴嫩", "SN": "塞内加尔",
    "AM": "亚美尼亚", "BS": "巴哈马",
    "LK": "斯里兰卡", "CM": "喀麦隆", "BF": "布基纳法索", "KG": "吉尔吉斯斯坦",
    "MU": "毛里求斯",
    # ── 2026-06-30 补全 ──
    "BH": "巴林", "QA": "卡塔尔", "ME": "黑山", "BW": "博茨瓦纳",
    "MV": "马尔代夫", "MW": "马拉维", "LS": "莱索托", "MK": "北马其顿",
    "JM": "牙买加", "IS": "冰岛", "SO": "索马里", "BJ": "贝宁",
    "GN": "几内亚", "BT": "不丹", "MG": "马达加斯加", "SD": "苏丹",
    "TG": "多哥", "SZ": "斯威士兰", "TL": "东帝汶", "WS": "萨摩亚",
    "JO": "约旦", "AQ": "南极洲", "CG": "刚果布", "TJ": "塔吉克斯坦",
    "TN": "突尼斯", "MA": "摩洛哥", "CI": "科特迪瓦", "ZM": "赞比亚",
    "TC": "特克斯和凯科斯", "YT": "马约特", "BB": "巴巴多斯", "DJ": "吉布提",
    "TD": "乍得", "SL": "塞拉利昂", "KW": "科威特", "LR": "利比里亚",
    "GD": "格林纳达", "VU": "瓦努阿图", "VI": "美属维京", "GA": "加蓬",
    "BN": "文莱", "LC": "圣卢西亚", "AD": "安道尔",
}
COUNTRY_ALIAS = {v: k for k, v in COUNTRY_NAME.items()}
COUNTRY_ALIAS.update({
    "CHINA": "CN", "中国": "CN", "UNITED STATES": "US", "USA": "US",
    "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB",
    "NETHERLANDS": "NL", "GERMANY": "DE", "FRANCE": "FR", "CANADA": "CA",
    "RUSSIA": "RU", "INDIA": "IN", "INDONESIA": "ID", "BRAZIL": "BR",
    "SINGAPORE": "SG", "JAPAN": "JP", "SOUTH KOREA": "KR", "KOREA": "KR",
    "THAILAND": "TH", "VIET NAM": "VN", "VIETNAM": "VN", "PHILIPPINES": "PH",
    "HONG KONG": "HK", "TAIWAN": "TW", "MACAO": "MO", "MACAU": "MO",
    "SPAIN": "ES", "ITALY": "IT", "POLAND": "PL", "SWEDEN": "SE", "FINLAND": "FI",
    "AUSTRALIA": "AU", "MEXICO": "MX", "COLOMBIA": "CO", "ARGENTINA": "AR",
    "BANGLADESH": "BD", "MALAYSIA": "MY", "TÜRKIYE": "TR", "TURKEY": "TR",
    "IRAN": "IR", "UKRAINE": "UA", "SOUTH AFRICA": "ZA", "SEYCHELLES": "SC",
    "KAZAKHSTAN": "KZ", "UNITED ARAB EMIRATES": "AE", "IRELAND": "IE",
    "DENMARK": "DK", "AUSTRIA": "AT", "PERU": "PE", "ECUADOR": "EC",
    "CHILE": "CL", "CAMBODIA": "KH", "ROMANIA": "RO", "COSTA RICA": "CR",
    "LATVIA": "LV", "CYPRUS": "CY", "MONGOLIA": "MN", "EGYPT": "EG",
    "KENYA": "KE", "PANAMA": "PA", "ISLE OF MAN": "IM", "DOMINICAN REPUBLIC": "DO",
    "BELIZE": "BZ", "CURACAO": "CW", "ESTONIA": "EE", "LITHUANIA": "LT",
    "VENEZUELA": "VE", "BOLIVIA": "BO", "SYRIA": "SY",
    "BRITISH VIRGIN ISLANDS": "VG", "HONDURAS": "HN", "PARAGUAY": "PY",
    "IRAQ": "IQ", "SERBIA": "RS", "ZIMBABWE": "ZW", "GUATEMALA": "GT",
    "BULGARIA": "BG", "PAKISTAN": "PK", "ISRAEL": "IL",
    "SAUDI ARABIA": "SA", "NIGERIA": "NG", "NORWAY": "NO",
    "NEW ZEALAND": "NZ", "SWITZERLAND": "CH", "BELGIUM": "BE",
    "CZECHIA": "CZ", "CROATIA": "HR",
    "NEPAL": "NP", "LIBYA": "LY", "OMAN": "OM", "HUNGARY": "HU",
    "MAURITIUS": "MU", "AZERBAIJAN": "AZ", "GHANA": "GH",
})

def _flag_emoji(code):
    """Return flag emoji for ISO country code."""
    if not code or code == "?" or len(code) != 2:
        return "🏳"
    a = code.upper()
    try:
        return chr(0x1F1E6 + ord(a[0]) - 65) + chr(0x1F1E6 + ord(a[1]) - 65)
    except Exception:
        return "🏳"

def normalize_country(country):
    c = (country or "").strip()
    if not c or c in ("unknown", "?", "0"): return ""
    up = c.upper()
    if len(up) == 2 and up.isascii() and up.isalpha(): return up
    return COUNTRY_ALIAS.get(c) or COUNTRY_ALIAS.get(up) or up

def jhao_map():
    now = time.time()
    if now - _jhao_cache["t"] < CACHE_TTL:
        return _jhao_cache["d"]
    out = {}
    try:
        for f, v in r0.hscan_iter("use_proxy"):
            try: out[f] = json.loads(v)
            except Exception: pass
    except Exception:
        out = {}
    _jhao_cache["d"] = out; _jhao_cache["t"] = now
    return out

def parse_member(member):
    try:
        ip, port = member.rsplit(":", 1)
        if ip in BAD_IPS or not port.isdigit():
            return None, None
        port_num = int(port)
        if not (1 <= port_num <= 65535):
            return None, None
        return ip, port
    except Exception:
        return None, None

def safe_float(v, default=0.0):
    try:
        if v in (None, "", "None"): return default
        return float(v)
    except Exception:
        return default

def grade_for_delay(delay):
    if delay is None or delay <= 0: return "?"
    if delay < 500: return "s"
    if delay < 1000: return "a"
    if delay < 3000: return "b"
    if delay < 5000: return "c"
    return "d"

def geo_from_hash_or_cache(ip, hd):
    country = (hd.get("country") or hd.get("region") or "").strip()
    location = (hd.get("location") or hd.get("region_label") or hd.get("city") or "").strip()
    country = normalize_country(country)
    if country:
        country_name = COUNTRY_NAME.get(country, country)
        if not location or location in ("unknown", "?", "0", country_name):
            # Location is just country name — try geo cache for city detail
            try:
                lo = geo_cached(ip)[1]
                if lo and lo not in ("?", "unknown", country_name):
                    location = lo
            except Exception:
                pass
        if not location or location in ("unknown", "?", "0"):
            location = country_name
        return country, location
    try:
        cr, lo = geo_cached(ip)
        return cr or "?", lo or "?"
    except Exception:
        return "?", "?"

def location_display(country_code, loc, ip=None):
    """Return best display location string, trying geo cache for CN city detail."""
    cn = COUNTRY_NAME.get(country_code, country_code)
    if not loc or loc in ("unknown", "?", "0", cn):
        if ip:
            try:
                lo = geo_cached(ip)[1]
                if lo and lo not in ("?", "unknown", cn):
                    return lo
            except Exception:
                pass
        return cn
    # Filter English-only names for CN proxies
    if country_code == "CN":
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in loc)
        if not has_cjk and any(c.isascii() and c.isalpha() for c in loc):
            return cn
    return loc

@lru_cache(maxsize=50000)
def geo_cached(ip):
    return geo.resolve_region(ip), geo.resolve(ip, update_proxy_hashes=True)

def build_proxy(member, hd, jm):
    ip, port = parse_member(member)
    if not ip: return None
    jd = jm.get(member, {})
    country, location = geo_from_hash_or_cache(ip, hd)
    region = normalize_country(country)
    if not region or region == "?": return None  # 无效国家，跳过
    delay = safe_float(hd.get("latency") or hd.get("delay"), 0.0)
    if delay > 0 and delay >= 500: return None  # 跳过已知慢代理，保留未验证的 (delay=0)
    proto = (hd.get("protocol") or "").lower().strip()
    if not proto or proto == "unknown": proto = "https" if jd.get("https") else "?"
    return {"ip": ip, "port": port, "protocol": proto, "delay": delay,
            "grade": grade_for_delay(delay), "region": region, "location": location_display(country, location, ip),
            "source": hd.get("source") or jd.get("source", "?"),
            "anon": hd.get("anonymous") or jd.get("anonymous", "?"),
            "last_check": hd.get("last_check", "?"), "is_china": country == "CN"}

def fetch_members(members):
    if not members: return []
    pipe = r1.pipeline(transaction=False)
    for m in members: pipe.hgetall(f"proxy:{m}")
    rows = pipe.execute(); jm = jhao_map(); out = []
    for m, hd in zip(members, rows):
        p = build_proxy(m, hd or {}, jm)
        if p: out.append(p)
    return out

def fetch_members_range(start, end):
    return fetch_members(r1.zrange("proxies:pool", start, end))

def fetch_page(page, limit):
    total = r1.zcard("proxies:pool")
    start = (page - 1) * limit
    out = []; cursor = start
    while len(out) < limit and cursor < total:
        batch_end = min(total - 1, cursor + max(limit * 3, 200) - 1)
        chunk = fetch_members_range(cursor, batch_end)
        out.extend(chunk); cursor = batch_end + 1
        if not chunk and batch_end >= total - 1: break
    return total, out[:limit]

def idx_key(kind, value): return f"idx:{kind}:{value}"

def index_ready():
    try:
        return r1.get("idx:ready") == "1"
    except Exception:
        return False

def ensure_index_async():
    global _index_building
    with _index_lock:
        if _index_building or index_ready(): return
        _index_building = True
    threading.Thread(target=build_indexes, daemon=True).start()

def build_indexes():
    global _index_building
    try:
        old = [k for k in r1.scan_iter("idx:country:*")] + [k for k in r1.scan_iter("idx:proto:*")] + [k for k in r1.scan_iter("idx:grade:*")]
        if old:
            for i in range(0, len(old), 500):
                r1.delete(*old[i:i+500])
        total = r1.zcard("proxies:pool"); batch = 1000
        for off in range(0, total, batch):
            members = r1.zrange("proxies:pool", off, off + batch - 1)
            pipe = r1.pipeline(transaction=False)
            for m in members: pipe.hgetall(f"proxy:{m}")
            rows = pipe.execute(); w = r1.pipeline(transaction=False); jm = jhao_map()
            for m, hd in zip(members, rows):
                p = build_proxy(m, hd or {}, jm)
                if not p: continue
                w.sadd(idx_key("country", p["region"]), m)
                w.sadd(idx_key("proto", p["protocol"]), m)
                w.sadd(idx_key("grade", p["grade"]), m)
            w.execute()
        for k in list(r1.scan_iter("idx:country:*")) + list(r1.scan_iter("idx:proto:*")) + list(r1.scan_iter("idx:grade:*")):
            r1.expire(k, INDEX_TTL)
        r1.set("idx:ready", "1", ex=INDEX_TTL)
    except Exception as e:
        try: r1.setex("idx:error", 300, str(e))
        except Exception: pass
    finally:
        _index_building = False

def filter_keys(filters):
    keys = []
    if filters["grade"]: keys.append(idx_key("grade", filters["grade"]))
    if filters["protocol"]: keys.append(idx_key("proto", filters["protocol"]))
    if filters["country"] and filters["country"] != "!CN": keys.append(idx_key("country", filters["country"]))
    return keys

def match_extra(px, f):
    if f["country"] == "!CN" and px["is_china"]: return False
    if f["delay"] is not None and px["delay"] > f["delay"]: return False
    if f["search"] and f["search"] not in px["ip"]: return False
    if f["location"] and f["location"] not in px["location"].lower(): return False
    return True

def fetch_filtered(page, limit, filters):
    ensure_index_async()
    total = r1.zcard("proxies:pool")
    need_start = (page - 1) * limit
    keys = filter_keys(filters)
    # Fast path: Redis set index for exact country/protocol/grade filters.
    if index_ready() and keys and not filters["search"] and not filters["location"] and filters["delay"] is None and filters["country"] != "!CN":
        if len(keys) == 1:
            filtered = r1.scard(keys[0]); members = r1.sscan_iter(keys[0], count=limit * 4)
        else:
            tmp = f"idx:tmp:{hash('|'.join(keys))}:{int(time.time())}"
            filtered = r1.sinterstore(tmp, keys); r1.expire(tmp, 20); members = r1.sscan_iter(tmp, count=limit * 4)
        page_members = []
        for i, m in enumerate(members):
            if i < need_start: continue
            page_members.append(m)
            if len(page_members) >= limit: break
        return total, filtered, fetch_members(page_members)
    # Bounded fallback: fill requested page without blocking the whole dashboard.
    page_items = []; matched = 0; scanned = 0; batch = 1000; max_scan = min(total, max(30000, (page + 1) * limit * 20))
    for off in range(0, max_scan, batch):
        members = r1.zrange("proxies:pool", off, min(total - 1, off + batch - 1))
        for p in fetch_members(members):
            if filters["grade"] and p["grade"] != filters["grade"]: continue
            if filters["protocol"] and p["protocol"] != filters["protocol"]: continue
            if filters["country"]:
                if filters["country"] == "!CN":
                    if p["is_china"]: continue
                elif p["region"] != filters["country"]: continue
            if not match_extra(p, filters): continue
            if matched >= need_start and len(page_items) < limit: page_items.append(p)
            matched += 1
        scanned += len(members)
        if len(page_items) >= limit and matched >= need_start + limit: break
    # Estimate pages conservatively while index warms; avoids 90s full scans.
    estimated = matched if scanned >= total else max(matched, int(matched * total / max(scanned, 1)))
    return total, estimated, page_items

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, d, code=200):
        b = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try: self.wfile.write(b)
        except BrokenPipeError: pass
    def do_GET(self):
        p = urllib.parse.urlparse(self.path); path = p.path.rstrip("/") or "/"; q = urllib.parse.parse_qs(p.query)
        if path == "/api/proxies":
            try: page = max(1, int(q.get("page", ["1"])[0]))
            except Exception: page = 1
            try: limit = min(max(1, int(q.get("limit", [str(PER_PAGE)])[0])), MAX_LIMIT)
            except Exception: limit = PER_PAGE
            try: delay_f = float(q.get("delay", [""])[0]) if q.get("delay", [""])[0] != "" else None
            except Exception: delay_f = None
            filters = {"grade": q.get("grade", [""])[0].lower(), "country": q.get("country", [""])[0].upper(),
                       "protocol": q.get("protocol", [""])[0].lower(), "search": q.get("search", [""])[0].lower(),
                       "location": q.get("location", [""])[0].lower(), "delay": delay_f}
            has_filter = any(v not in ("", None) for v in filters.values())
            if has_filter: total, filtered, proxies = fetch_filtered(page, limit, filters)
            else: total, proxies = fetch_page(page, limit); filtered = total
            sort_by = q.get("sort", [""])[0]; sort_asc = q.get("asc", ["1"])[0] != "0"
            if sort_by == "delay": proxies = sorted(proxies, key=lambda x: x.get("delay") or 999999, reverse=not sort_asc)
            elif sort_by == "grade": proxies = sorted(proxies, key=lambda x: {"s":0,"a":1,"b":2,"c":3,"d":4}.get(x.get("grade","d"),4), reverse=not sort_asc)
            body = {"total": total, "filtered": filtered, "page": page, "pages": max(1, -(-filtered // limit)), "limit": limit, "proxies": proxies, "index_ready": index_ready()}
            b = json.dumps(body, ensure_ascii=False).encode(); etag = hashlib.md5(b).hexdigest()
            if self.headers.get("If-None-Match") == etag: self.send_response(304); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("ETag", etag); self.send_header("Cache-Control", "max-age=5"); self.end_headers()
            try: self.wfile.write(b)
            except BrokenPipeError: pass
            return
        if path == "/api/stats":
            total = r1.zcard("proxies:pool"); now = time.time()
            # Double-check: quick check under lock, heavy compute outside
            need_recalc = False
            with _stats_lock:
                cached = _stats_cache["d"]
                if now - _stats_cache["t"] >= 30 or cached is None or cached.get("total") != total:
                    need_recalc = True
            if need_recalc and cached is None:
                cached = None
            if need_recalc:
                if index_ready():
                    try:
                        grades = {g: r1.scard(idx_key("grade", g)) for g in ("s", "a", "b", "c", "d")}
                        known_protos = ["http", "https", "socks4", "socks5"]
                        protos = {k: r1.scard(idx_key("proto", k)) for k in known_protos}
                        protos = {k: v for k, v in protos.items() if v > 0}
                        region_keys = list(r1.scan_iter("idx:country:*", count=100))
                        regions = {}
                        if region_keys:
                            pipe = r1.pipeline(transaction=False)
                            for k in region_keys: pipe.scard(k)
                            for k, v in zip(region_keys, pipe.execute()):
                                regions[k.split(":", 2)[2]] = v
                        china = regions.get("CN", 0)
                        sample = sum(grades.values())
                        cached = {"total": sample if sample else total, "sample": sample, "grades": grades, "protocols": protos,
                                  "china": china, "regions": dict(sorted(regions.items(), key=lambda x: -x[1])[:80]),
                                  "index_ready": True, "stats_cached_at": int(now)}
                        with _stats_lock:
                            _stats_cache["d"] = cached; _stats_cache["t"] = now
                    except Exception:
                        pass
                if cached is None:
                    ensure_index_async()
                    grades = {"s":0,"a":0,"b":0,"c":0,"d":0}; protos = {}; regions = {}; china = 0; seen = 0
                    batch = 5000
                    for off in range(0, total, batch):
                        members = r1.zrange("proxies:pool", off, min(total - 1, off + batch - 1))
                        pipe = r1.pipeline(transaction=False)
                        for m in members: pipe.hgetall(f"proxy:{m}")
                        for m, hd in zip(members, pipe.execute()):
                            if not parse_member(m)[0]: continue
                            seen += 1
                            delay = safe_float((hd or {}).get("latency") or (hd or {}).get("delay"), 0.0)
                            if delay <= 0 or delay >= 500: continue
                            g = grade_for_delay(delay); grades[g] = grades.get(g, 0) + 1
                            proto = ((hd or {}).get("protocol") or "?").lower().strip() or "?"
                            if proto == "unknown": proto = "?"
                            protos[proto] = protos.get(proto, 0) + 1
                            country = normalize_country(((hd or {}).get("country") or (hd or {}).get("region") or "").strip())
                            if not country or country == "?": continue
                            regions[country] = regions.get(country, 0) + 1
                            if country == "CN": china += 1
                    cached = {"total": seen, "sample": seen, "grades": grades, "protocols": protos,
                              "china": china, "regions": dict(sorted(regions.items(), key=lambda x: -x[1])[:80]),
                              "index_ready": index_ready(), "stats_cached_at": int(now)}
                    with _stats_lock:
                        _stats_cache["d"] = cached; _stats_cache["t"] = now
            self._json(cached)
            return
        if path == "/api/countries":
            countries = {}
            if index_ready():
                try:
                    region_keys = list(r1.scan_iter("idx:country:*", count=200))
                    if region_keys:
                        pipe = r1.pipeline(transaction=False)
                        for k in region_keys: pipe.scard(k)
                        for k, v in zip(region_keys, pipe.execute()):
                            code = k.split(":", 2)[2]
                            countries[code] = v
                except Exception:
                    countries = {}
            if not countries:
                total = r1.zcard("proxies:pool")
                batch = 5000
                for off in range(0, total, batch):
                    members = r1.zrange("proxies:pool", off, min(total - 1, off + batch - 1))
                    pipe = r1.pipeline(transaction=False)
                    for m in members: pipe.hgetall(f"proxy:{m}")
                    for m, hd in zip(members, pipe.execute()):
                        if not parse_member(m)[0]: continue
                        cc = normalize_country(((hd or {}).get("country") or (hd or {}).get("region") or "").strip())
                        if not cc or cc == "?": continue  # 跳过无效国家
                        countries[cc] = countries.get(cc, 0) + 1
            result = []
            for code, count in sorted(countries.items(), key=lambda x: -x[1]):
                name = COUNTRY_NAME.get(code, code)
                result.append({"code": code, "name": name, "count": count, "flag": _flag_emoji(code), "is_china": code == "CN"})
            self._json({"countries": result, "total_countries": len(result)})
            return
        if path.startswith("/api/country/"):
            country_code = path.split("/api/country/")[1].upper()
            if not country_code: self._json({"error":"missing country"}, 400); return
            try: page = max(1, int(q.get("page", ["1"])[0]))
            except Exception: page = 1
            try: limit = min(max(1, int(q.get("limit", [str(PER_PAGE)])[0])), MAX_LIMIT)
            except Exception: limit = PER_PAGE
            try: delay_f = float(q.get("delay", [""])[0]) if q.get("delay", [""])[0] != "" else None
            except Exception: delay_f = None
            sort_by = q.get("sort", ["delay"])[0]; sort_asc = q.get("asc", ["1"])[0] != "0"
            proto_f = q.get("protocol", [""])[0].lower()
            loc_f = q.get("location", [""])[0].lower()
            search_f = q.get("search", [""])[0].lower()

            total = r1.zcard("proxies:pool")
            matched_items = []
            total_matched = 0
            country_key = idx_key("country", country_code)

            # Fast path: use Redis set index for country
            if index_ready():
                try:
                    if r1.exists(country_key):
                        candidate_count = r1.scard(country_key)
                        # For small countries: scan all, sort, paginate
                        if candidate_count <= 1000:
                            all_members = []
                            # Exhaust sscan_iter cursor for completeness
                            cursor = 0
                            while True:
                                cursor, batch = r1.sscan(country_key, cursor, count=200)
                                all_members.extend(batch)
                                if cursor == 0:
                                    break
                            # Fetch and filter all
                            all_matched = []
                            for member in all_members:
                                meta = r1.hgetall(f"proxy:{member}")
                                if not meta: continue
                                ip, port = parse_member(member)
                                if not ip or not port: continue
                                proto = (meta.get("protocol") or "?").lower().strip()
                                if proto_f and proto != proto_f: continue
                                loc = (meta.get("location") or "").lower()
                                if loc_f and loc_f not in loc: continue
                                delay = safe_float(meta.get("latency") or meta.get("delay"), 0.0)
                                if delay_f is not None and delay > delay_f: continue
                                if search_f and search_f not in member: continue
                                all_matched.append({
                                    "ip": ip, "port": port,
                                    "protocol": proto, "delay": delay,
                                    "location": location_display(country_code, meta.get("location"), ip),
                                    "country": country_code, "last_check": meta.get("last_check", "?"),
                                    "source": meta.get("source", "?"),
                                })
                            if sort_by == "delay":
                                all_matched.sort(key=lambda x: x.get("delay") or 99999, reverse=not sort_asc)
                            total_matched = len(all_matched)
                            start = (page - 1) * limit
                            matched_items = all_matched[start:start + limit]
                        else:
                            # Large country: paginate from set (排序为近似结果)
                            start = (page - 1) * limit
                            members_batch = list(r1.sscan_iter(country_key, count=limit * 4))
                            page_collected = 0; total_matched = 0; matched_batch = []
                            for member in members_batch:
                                meta = r1.hgetall(f"proxy:{member}")
                                if not meta: continue
                                ip, port = parse_member(member)
                                if not ip or not port: continue
                                proto = (meta.get("protocol") or "?").lower().strip()
                                if proto_f and proto != proto_f: continue
                                loc = (meta.get("location") or "").lower()
                                if loc_f and loc_f not in loc: continue
                                delay = safe_float(meta.get("latency") or meta.get("delay"), 0.0)
                                if delay_f is not None and delay > delay_f: continue
                                if search_f and search_f not in member: continue
                                total_matched += 1
                                if total_matched > start and page_collected < limit:
                                    matched_batch.append({
                                        "ip": ip, "port": port,
                                        "protocol": proto, "delay": delay,
                                        "location": location_display(country_code, meta.get("location"), ip),
                                        "country": country_code, "last_check": meta.get("last_check", "?"),
                                        "source": meta.get("source", "?"),
                                    })
                                    page_collected += 1
                            if sort_by == "delay":
                                matched_batch.sort(key=lambda x: x.get("delay") or 99999, reverse=not sort_asc)
                            matched_items = matched_batch
                    else:
                        total_matched = 0
                except Exception:
                    pass  # fall through to scan

            # Fallback: scan (when index not ready or country key missing)
            if not matched_items and (not index_ready() or not r1.exists(country_key)):
                total_matched = 0
                scanned = 0; batch = 2000
                max_scan = min(total, 40000)
                start = (page - 1) * limit
                page_collected = 0
                for off in range(0, max_scan, batch):
                    members = r1.zrange("proxies:pool", off, min(total - 1, off + batch - 1))
                    pipe = r1.pipeline(transaction=False)
                    for m in members: pipe.hgetall(f"proxy:{m}")
                    for m, hd in zip(members, pipe.execute()):
                        scanned += 1
                        if not hd: continue
                        ip, port = parse_member(m)
                        if not ip or not port: continue
                        cc = normalize_country((hd.get("country") or hd.get("region") or "").strip()) or "?"
                        if cc != country_code: continue
                        proto = (hd.get("protocol") or "?").lower().strip()
                        if proto_f and proto != proto_f: continue
                        loc = (hd.get("location") or "").lower()
                        if loc_f and loc_f not in loc: continue
                        delay = safe_float(hd.get("latency") or hd.get("delay"), 0.0)
                        if delay_f is not None and delay > delay_f: continue
                        if search_f and search_f not in m: continue
                        total_matched += 1
                        if total_matched > start and page_collected < limit:
                            matched_items.append({
                                "ip": ip, "port": port,
                                "protocol": proto, "delay": delay,
                                "location": location_display(country_code, hd.get("location"), ip),
                                "country": country_code, "last_check": hd.get("last_check", "?"),
                                "source": hd.get("source", "?"),
                            })
                            page_collected += 1
                    if scanned >= max_scan: break
                if sort_by == "delay":
                    matched_items.sort(key=lambda x: x.get("delay") or 99999, reverse=not sort_asc)

            body = {
                "country": country_code, "country_name": COUNTRY_NAME.get(country_code, country_code),
                "total_matched": total_matched, "page": page,
                "pages": max(1, -(-total_matched // limit)),
                "limit": limit, "proxies": matched_items,
            }
            self._json(body)
            return
        self._json({"error":"not found"}, 404)

if __name__ == "__main__":
    print("API on :5051")
    try: geo._init()
    except Exception: pass
    ensure_index_async()
    ThreadingHTTPServer(("0.0.0.0", 5051), H).serve_forever()
