# -*- coding: utf-8 -*-
"""GeoIP resolver - online APIs + local binary DB + Redis cache + proxy-pool rotation.

Local offline DB: data/ipdb.bin (DB-IP Country Lite, binary format).
Loads on startup, updates monthly. 二分查找, ~1μs per lookup.
Cache: Redis DB1 geo:<ip> + proxy:<ip:port> country/location.
API requests rotate through proxy pool to avoid rate limits.
"""

import json, os, time, threading, random, struct, socket
import redis
import urllib.request
import urllib.parse
import urllib.error

REDIS_HOST = os.environ.get("REDIS_HOST", "proxy-redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
_r = None

def _redis():
    global _r
    if _r is None:
        _r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=1, decode_responses=True)
    return _r

GEO_TTL = 7 * 24 * 3600
FILL_INTERVAL = 600
BATCH_SIZE = 45
CN_FILL_INTERVAL = 60   # CN IPs: check every 1 min for faster city resolution
CN_BATCH_SIZE = 500      # Larger batch for CN since we need province+city
HTTP_TIMEOUT = 12
LOCAL_DB_PATH = os.environ.get("IPDB_PATH", os.path.join(os.path.dirname(__file__), "data", "ipdb.bin"))
LOCAL_DB_UPDATE_URL = "https://download.db-ip.com/free/dbip-country-lite-{year}-{month:02d}.csv.gz"
LOCAL_DB_UPDATE_INTERVAL = 30 * 24 * 3600  # 每月更新

COUNTRY_CODE = {
    "CN": "中国", "US": "美国", "JP": "日本", "KR": "韩国", "GB": "英国",
    "DE": "德国", "FR": "法国", "CA": "加拿大", "AU": "澳大利亚", "RU": "俄罗斯",
    "IN": "印度", "BR": "巴西", "ID": "印尼", "NG": "尼日利亚", "ZA": "南非",
    "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "VN": "越南", "PH": "菲律宾",
    "MM": "缅甸", "KH": "柬埔寨", "LA": "老挝", "BD": "孟加拉国", "PK": "巴基斯坦",
    "IR": "伊朗", "TR": "土耳其", "SA": "沙特阿拉伯", "AE": "阿联酋",
    "EG": "埃及", "KE": "肯尼亚", "TZ": "坦桑尼亚", "UG": "乌干达",
    "GH": "加纳", "CI": "科特迪瓦", "SN": "塞内加尔", "CM": "喀麦隆",
    "AO": "安哥拉", "ZM": "赞比亚", "ZW": "津巴布韦", "DZ": "阿尔及利亚",
    "MA": "摩洛哥", "TN": "突尼斯", "MX": "墨西哥", "AR": "阿根廷",
    "CO": "哥伦比亚", "VE": "委内瑞拉", "CL": "智利", "PE": "秘鲁",
    "EC": "厄瓜多尔", "BO": "玻利维亚", "PY": "巴拉圭", "UY": "乌拉圭",
    "CR": "哥斯达黎加", "IT": "意大利", "ES": "西班牙", "NL": "荷兰",
    "BE": "比利时", "SE": "瑞典", "CH": "瑞士", "PL": "波兰",
    "UA": "乌克兰", "CZ": "捷克", "RO": "罗马尼亚", "HU": "匈牙利",
    "AT": "奥地利", "PT": "葡萄牙", "GR": "希腊", "IE": "爱尔兰",
    "DK": "丹麦", "FI": "芬兰", "NO": "挪威", "IL": "以色列",
    "HK": "香港", "TW": "台湾", "MO": "澳门",
    "MU": "毛里求斯", "AZ": "阿塞拜疆",
}

# Reverse mapping for normalizing country codes
_COUNTRY_REVERSE = {v: k for k, v in COUNTRY_CODE.items()}
_COUNTRY_REVERSE.update({
    "CHINA": "CN", "UNITED STATES": "US", "USA": "US",
    "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB",
    "NETHERLANDS": "NL", "GERMANY": "DE", "FRANCE": "FR", "CANADA": "CA",
    "RUSSIA": "RU", "INDIA": "IN", "INDONESIA": "ID", "BRAZIL": "BR",
    "SINGAPORE": "SG", "JAPAN": "JP", "SOUTH KOREA": "KR", "KOREA": "KR",
    "THAILAND": "TH", "VIET NAM": "VN", "VIETNAM": "VN", "PHILIPPINES": "PH",
    "HONG KONG": "HK", "TAIWAN": "TW", "MACAO": "MO", "MACAU": "MO",
    "SPAIN": "ES", "ITALY": "IT", "POLAND": "PL", "SWEDEN": "SE", "FINLAND": "FI",
    "AUSTRALIA": "AU", "MEXICO": "MX", "COLOMBIA": "CO", "ARGENTINA": "AR",
    "BANGLADESH": "BD", "MALAYSIA": "MY", "TURKIYE": "TR", "TURKEY": "TR",
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

# ═══════════ Local Binary IP Database (offline, ~1μs lookup) ═══════════

_local_db = {"entries": [], "loaded": False, "lock": threading.Lock()}


def _load_local_db(path=None):
    """Load binary IP range database into memory. 3.5MB for 355K entries."""
    path = path or LOCAL_DB_PATH
    with _local_db["lock"]:
        if _local_db["loaded"]:
            return True
        if not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                count = struct.unpack("<I", f.read(4))[0]
                entries = []
                for _ in range(count):
                    data = f.read(10)
                    if len(data) < 10:
                        break
                    start_int = struct.unpack("<I", data[0:4])[0]
                    end_int = struct.unpack("<I", data[4:8])[0]
                    country = data[8:10].decode("ascii").rstrip("\x00")
                    entries.append((start_int, end_int, country))
            entries.sort(key=lambda x: x[0])
            _local_db["entries"] = entries
            _local_db["loaded"] = True
            return True
        except Exception:
            return False


def _local_lookup(ip_str):
    """Binary search for country code. Returns 'ZZ' if not found."""
    if not _local_db["loaded"]:
        _load_local_db()
    with _local_db["lock"]:
        entries = _local_db["entries"]
        if not entries:
            return "ZZ"
        try:
            target = struct.unpack("!I", socket.inet_aton(ip_str))[0]
        except Exception:
            return "ZZ"
        lo, hi = 0, len(entries) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s, e, c = entries[mid]
            if target < s:
                hi = mid - 1
            elif target > e:
                lo = mid + 1
            else:
                return c
    return "ZZ"


def _update_local_db():
    """Download latest DB-IP Country Lite CSV and rebuild binary DB."""
    import gzip, datetime
    now = datetime.datetime.utcnow()
    url = LOCAL_DB_UPDATE_URL.format(year=now.year, month=now.month)
    csv_gz_path = "/tmp/dbip-latest.csv.gz"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "proxy-dashboard/geo-updater"})
        raw = urllib.request.urlopen(req, timeout=60).read()
        with open(csv_gz_path, "wb") as f:
            f.write(raw)
    except Exception:
        return False
    entries = []
    try:
        with gzip.open(csv_gz_path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) < 3 or ":" in parts[0]:
                    continue
                try:
                    start_int = struct.unpack("!I", socket.inet_aton(parts[0]))[0]
                    end_int = struct.unpack("!I", socket.inet_aton(parts[1]))[0]
                except Exception:
                    continue
                entries.append((start_int, end_int, parts[2]))
    except Exception:
        return False
    if len(entries) < 100000:
        return False
    entries.sort(key=lambda x: x[0])
    tmp_path = LOCAL_DB_PATH + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(struct.pack("<I", len(entries)))
            for start_int, end_int, country in entries:
                country_bytes = country.encode("ascii")[:2].ljust(2, b"\x00")
                f.write(struct.pack("<II", start_int, end_int))
                f.write(country_bytes)
        os.rename(tmp_path, LOCAL_DB_PATH)
        with _local_db["lock"]:
            _local_db["entries"] = entries
            _local_db["loaded"] = True
        return True
    except Exception:
        return False


def _local_db_updater_loop():
    """Background thread: update local DB monthly."""
    while True:
        try:
            _update_local_db()
        except Exception:
            pass
        time.sleep(LOCAL_DB_UPDATE_INTERVAL)


# ═══════════ API resolver + Redis cache ═══════════

_fill_thread = None
_fill_running = False
_proxy_cache = {"t": 0, "d": []}


def resolve(ip, update_proxy_hashes=False):
    # 1. Local binary DB (fastest, offline)
    cc = _local_lookup(ip)
    if cc != "ZZ":
        # CN IP: always do online lookup for city detail
        if cc == "CN":
            data = _query_one(ip, prefer_cn=True)
            if data:
                _store(ip, data)
                loc = _format_location(data)
                if loc and loc != COUNTRY_CODE.get(cc, cc):
                    if update_proxy_hashes:
                        _sync_location_to_proxies(ip, loc)
                    return loc
            _enqueue_ip(ip)
            country = COUNTRY_CODE.get(cc, cc)
            return country
        # Non-CN: check Redis cache for richer location
        data = _cached(ip)
        if data and not data.get("_placeholder"):
            loc = _format_location(data)
            if loc and loc != COUNTRY_CODE.get(cc, cc):
                return loc
        country = COUNTRY_CODE.get(cc, cc)
        return country

    # 2. Redis cache
    data = _cached(ip)
    if data and not data.get("_placeholder"):
        return _format_location(data)

    # 3. Enqueue for online API lookup
    _enqueue_ip(ip)
    return "..."


def _sync_location_to_proxies(ip, location):
    try:
        for k in _redis().scan_iter(f"proxy:{ip}:*", count=100):
            _redis().hset(k, "location", location)
    except Exception:
        pass


def resolve_region(ip):
    # 1. Local binary DB
    cc = _local_lookup(ip)
    if cc != "ZZ":
        return cc

    # 2. Redis cache
    data = _cached(ip)
    if data and not data.get("_placeholder"):
        code = data.get("countryCode", "")
        if code and code != "?" and len(code) == 2 and code.isascii() and code.isalpha():
            return code
        if code and code != "?":
            resolved = _COUNTRY_REVERSE.get(code.upper())
            if resolved and len(resolved) == 2:
                return resolved

    # 3. Enqueue for future resolution
    _enqueue_ip(ip)
    return "?"


def resolve_and_store(ip, proxy_key=None, force=False):
    """Resolve one IP now and write geo cache + optional proxy hash fields."""
    if not force:
        data = _cached(ip)
        if data and not data.get("_placeholder"):
            # For CN IPs cached without city detail, force online to get province+city
            if data.get("countryCode") == "CN" and not data.get("city") and not data.get("regionName"):
                pass  # fall through to online query below
            else:
                if proxy_key:
                    _write_proxy_geo(proxy_key, data)
                return data
        # Check local DB
        cc = _local_lookup(ip)
        if cc != "ZZ":
            # For CN IPs, do online lookup now to get province+city (spec: no cache short-circuit)
            if cc == "CN":
                online = _query_one(ip)
                if online:
                    _store(ip, online)
                    if proxy_key:
                        _write_proxy_geo(proxy_key, online)
                    return online
                # fallback: store placeholder with country-only data, enqueue for retry
                _enqueue_ip(ip)
            data = {"status": "success", "country": COUNTRY_CODE.get(cc, cc),
                    "countryCode": cc, "regionName": "", "city": "",
                    "query": ip, "source": "local-db",
                    "geo_updated_at": 0 if cc == "CN" else int(time.time())}
            _store(ip, data)
            if proxy_key:
                _write_proxy_geo(proxy_key, data)
            return data
    is_cn = _local_lookup(ip) == "CN"
    data = _query_one(ip, prefer_cn=is_cn)
    if data:
        # If CN IP got English-only result, try again with Chinese API later
        if is_cn and not _has_chinese_location(data):
            _enqueue_ip(ip)
        _store(ip, data)
        if proxy_key:
            _write_proxy_geo(proxy_key, data)
    return data


def inject_geo(ip, country_code, country_name="", region="", city=""):
    cc = (country_code or "").upper()
    data = {
        "status": "success", "country": country_name or COUNTRY_CODE.get(cc, cc),
        "countryCode": cc, "regionName": region or "", "city": city or "",
        "query": ip, "source": "injected", "geo_updated_at": int(time.time()),
    }
    _store(ip, data)


def _format_location(data):
    cc = (data.get("countryCode") or "").upper()
    if cc in ("HK", "TW", "MO"):
        return COUNTRY_CODE.get(cc, data.get("country", "?"))
    if cc == "CN":
        prov = data.get("regionName", "") or ""
        city = data.get("city", "") or ""
        # filter junk words in city
        junk = ("阿里", "腾讯", "百度", "华为", "电信", "联通", "移动", "云")
        if city and any(j in city for j in junk):
            city = ""
        # filter non-city locality names (bridges, roads, towns, etc.)
        locality_junk = ("桥", "路", "镇", "村", "街", "广场", "小区", "园区", "大厦", "花园", "中心")
        if city and any(w in city for w in locality_junk):
            city = ""
        # filter English names
        if city:
            has_cjk = any('\u4e00' <= c <= '\u9fff' for c in city)
            if not has_cjk and any(c.isascii() and c.isalpha() for c in city):
                city = ""
        if prov:
            has_cjk = any('\u4e00' <= c <= '\u9fff' for c in prov)
            if not has_cjk and any(c.isascii() and c.isalpha() for c in prov):
                prov = ""  # filter English province names like "Zhejiang"
        # Clean up municipality names
        if prov.endswith("市") and not city:
            prov = prov.rstrip("市")
        # Clean up city "市" suffix (e.g. 广州市→广州)
        if city and city.endswith("市") and len(city) > 2:
            city = city.rstrip("市")
        # deduplicate: 北京市+北京→北京, 上海+上海→上海
        if prov and city:
            prov_clean = prov.rstrip("市省自治区")
            city_clean = city.rstrip("市")
            if prov_clean == city_clean or prov == city:
                return prov_clean if prov.endswith("市") else prov
        if prov and city:
            return prov + city
        if prov or city:
            return prov or city
        # No usable Chinese location found: purge stale cache and re-enqueue
        ip_addr = data.get("query", "")
        if ip_addr:
            try:
                _redis().delete(f"geo:{ip_addr}")
            except Exception:
                pass
            _enqueue_ip(ip_addr)
        return "中国"
    return COUNTRY_CODE.get(cc, data.get("country", "?"))


def _cached(ip):
    if not ip or ip in ("0.0.0.0", "127.0.0.1"):
        return None
    try:
        raw = _redis().get(f"geo:{ip}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _store(ip, data):
    if not ip or data is None:
        return
    data["geo_updated_at"] = int(time.time())
    _redis().setex(f"geo:{ip}", GEO_TTL, json.dumps(data, ensure_ascii=False))


def _write_proxy_geo(proxy_key, data):
    try:
        cc = (data.get("countryCode") or "").upper()
        if not cc or cc == "?":
            ip = data.get("query", "")
            if ip:
                lcc = _local_lookup(ip)
                if lcc != "ZZ":
                    cc = lcc
        if not cc or cc == "?":
            return
        if len(cc) != 2 or not (cc.isascii() and cc.isalpha()):
            resolved = _COUNTRY_REVERSE.get(cc)
            if resolved and len(resolved) == 2:
                cc = resolved
            else:
                return
        loc = _format_location(data)
        _redis().hset(f"proxy:{proxy_key}", mapping={
            "country": cc,
            "location": loc,
            "geo_updated_at": str(int(time.time())),
            "geo_source": data.get("source", "ip-api"),
        })
    except Exception:
        pass


def _enqueue_ip(ip):
    try:
        if ip and ip not in ("0.0.0.0", "127.0.0.1"):
            _redis().sadd("geo:queue", ip)
    except Exception:
        pass


def _load_proxy_pool():
    now = time.time()
    if _proxy_cache["d"] and now - _proxy_cache["t"] < 300:
        return _proxy_cache["d"]
    proxies = []
    try:
        # 取分数最高的一批，优先 HTTP。免费代理不稳定，多备一些。
        for m in _redis().zrevrange("proxies:pool", 0, 499):
            if m.startswith(("0.0.0.0:", "127.0.0.1:")):
                continue
            hd = _redis().hgetall(f"proxy:{m}")
            proto = (hd.get("protocol") or "http").lower()
            if proto in ("http", "https", "?"):
                proxies.append(m)
    except Exception:
        proxies = []
    random.shuffle(proxies)
    _proxy_cache["d"] = proxies
    _proxy_cache["t"] = now
    return proxies


def _open_url(req, timeout=HTTP_TIMEOUT):
    # 先直连；遇到 403/429/限流或失败，再用池子代理轮换。
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429, 503):
            return None
    except Exception:
        pass
    for pxy in _load_proxy_pool()[:20]:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                "http": f"http://{pxy}", "https": f"http://{pxy}"
            }))
            return opener.open(req, timeout=timeout).read()
        except Exception:
            continue
    return None


def _query_batch(ips):
    if not ips:
        return []
    out = []
    # ip-api.com free tier limit: 100 IPs per batch request
    for i in range(0, len(ips), 100):
        chunk = ips[i:i + 100]
        payload = json.dumps([{"query": ip} for ip in chunk]).encode()
        req = urllib.request.Request(
            "http://ip-api.com/batch?lang=zh-CN&fields=status,message,country,countryCode,regionName,city,query",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "proxy-dashboard/geo"},
        )
        raw = _open_url(req)
        if not raw:
            continue
        try:
            arr = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            continue
        for item in arr:
            if item.get("status") == "success":
                item["source"] = "ip-api"
                out.append(item)
    return out


def _query_one(ip, prefer_cn=False):
    arr = _query_batch([ip])
    if arr:
        data = arr[0]
        cc = (data.get("countryCode") or "").upper()
        if not prefer_cn or cc != "CN" or _has_chinese_location(data):
            return data
    # For CN IPs, prioritize Chinese-language APIs over English ones
    if prefer_cn:
        for fn in (_query_ip9, _query_ipwhois, _query_freeipapi):
            data = fn(ip)
            if data and _has_chinese_location(data):
                return data
    for fn in (_query_ip9, _query_ipwhois, _query_freeipapi, _query_ipapico, _query_ipinfo, _query_ipsb):
        data = fn(ip)
        if data:
            return data
    return None


def _has_chinese_location(data):
    rn = data.get("regionName", "") or ""
    cy = data.get("city", "") or ""
    for s in (rn, cy):
        if s and any(0x4e00 <= ord(c) <= 0x9fff for c in s):
            return True
    return False


def _json_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "proxy-dashboard/geo"})
    raw = _open_url(req, timeout=timeout)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return None


def _normalize(ip, country_code, country, region, city, source):
    cc = (country_code or "").upper()
    if not cc:
        return None
    # Normalize non-ISO country codes (full names -> 2-char code)
    if len(cc) != 2 or not (cc.isascii() and cc.isalpha()):
        resolved = _COUNTRY_REVERSE.get(cc)
        if not resolved and country:
            resolved = _COUNTRY_REVERSE.get(country.upper())
        if resolved and len(resolved) == 2:
            cc = resolved
    return {
        "status": "success",
        "country": country or COUNTRY_CODE.get(cc, cc),
        "countryCode": cc,
        "regionName": region or "",
        "city": city or "",
        "query": ip,
        "source": source,
    }


def _query_ip9(ip):
    d = _json_get("https://ip9.com.cn/get?ip=" + urllib.parse.quote(ip), timeout=8)
    if not d or d.get("msg") == "Error":
        return None
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    return _normalize(
        ip,
        data.get("country_code") or data.get("countryCode") or data.get("code"),
        data.get("country") or data.get("country_name"),
        data.get("prov") or data.get("province") or data.get("region") or data.get("regionName"),
        data.get("city") or data.get("cityName"),
        "ip9",
    )


def _query_freeipapi(ip):
    d = _json_get("https://freeipapi.com/api/json/" + urllib.parse.quote(ip), timeout=8)
    if not d:
        return None
    return _normalize(
        ip,
        d.get("countryCode"),
        d.get("countryName"),
        d.get("regionName"),
        d.get("cityName"),
        "freeipapi",
    )


def _query_ipwhois(ip):
    d = _json_get("https://ipwhois.app/json/" + urllib.parse.quote(ip) + "?lang=zh-CN", timeout=8)
    if not d or d.get("success") is False:
        return None
    return _normalize(
        ip,
        d.get("country_code"),
        d.get("country"),
        d.get("region"),
        d.get("city"),
        "ipwhois",
    )


# ── Spec APIs (ipapi.co, ipinfo.io, ip.sb) ──
def _query_ipapico(ip):
    d = _json_get("https://ipapi.co/" + urllib.parse.quote(ip) + "/json/", timeout=8)
    if not d or d.get("error"):
        return None
    return _normalize(
        ip,
        d.get("country_code") or d.get("country"),
        d.get("country_name"),
        d.get("region"),
        d.get("city"),
        "ipapico",
    )


def _query_ipinfo(ip):
    d = _json_get("https://ipinfo.io/" + urllib.parse.quote(ip) + "/json", timeout=8)
    if not d or "error" in d:
        return None
    cc = d.get("country", "")
    parts = (d.get("region") or "").split(",")
    region = parts[0].strip() if parts else ""
    city = d.get("city", "")
    return _normalize(ip, cc, cc, region, city, "ipinfo")


def _query_ipsb(ip):
    d = _json_get("https://api.ip.sb/geoip/" + urllib.parse.quote(ip), timeout=8)
    if not d or "error" in d:
        return None
    return _normalize(
        ip,
        d.get("country_code"),
        d.get("country"),
        d.get("region"),
        d.get("city"),
        "ipsb",
    )


def start_filler():
    global _fill_thread, _fill_running
    if _fill_running:
        return
    _fill_running = True
    _fill_thread = threading.Thread(target=_fill_loop, daemon=True)
    _fill_thread.start()


def _fill_loop():
    last_cn_fill = 0
    while _fill_running:
        now = time.time()
        try:
            if now - last_cn_fill >= CN_FILL_INTERVAL:
                _fill_cycle(cn_only=True)
                last_cn_fill = now
            else:
                _fill_cycle(cn_only=False)
        except Exception:
            pass
        time.sleep(30)  # Check every 30s which cycle to run


def _needs_refresh(ip):
    data = _cached(ip)
    if not data or data.get("_placeholder"):
        return True
    ts = int(data.get("geo_updated_at") or 0)
    return time.time() - ts > GEO_TTL


def _fill_cycle(cn_only=False):
    batch = CN_BATCH_SIZE if cn_only else BATCH_SIZE
    ips = []
    # 1. Queue first
    try:
        while len(ips) < batch:
            ip = _redis().spop("geo:queue")
            if not ip:
                break
            if _needs_refresh(ip):
                cc = _local_lookup(ip)
                if cn_only and cc != "CN":
                    _redis().sadd("geo:queue", ip)  # Put back for regular cycle
                    continue
                ips.append(ip)
    except Exception:
        pass
    # 2. Scan pool for stale/missing
    if len(ips) < batch:
        try:
            for m in _redis().zscan_iter("proxies:pool", count=1000):
                key = m[0]
                ip = key.rsplit(":", 1)[0] if ":" in key else key
                if ip in ("0.0.0.0", "127.0.0.1"):
                    continue
                if cn_only:
                    cc = _local_lookup(ip)
                    if cc != "CN":
                        continue
                if _needs_refresh(ip):
                    ips.append(ip)
                if len(ips) >= batch:
                    break
        except Exception:
            pass
    if not ips:
        return
    for item in _query_batch(ips):
        ip = item.get("query")
        if ip:
            _store(ip, item)
    # Unresolved placeholders — 1 hour TTL
    for ip in ips:
        if not _cached(ip):
            try:
                _redis().setex(f"geo:{ip}", 3600, json.dumps({"_placeholder": True, "query": ip, "geo_updated_at": int(time.time())}))
            except Exception:
                pass


def _init():
    _load_local_db()
    t = threading.Thread(target=_local_db_updater_loop, daemon=True)
    t.start()
    # Enqueue all distinct CN IPs for city-level geo lookup
    enqueue_cn_ips()
    start_filler()

def enqueue_cn_ips():
    """Enqueue all unique CN IPs from pool for online geo lookup."""
    seen = set()
    count = 0
    try:
        for m in _redis().zscan_iter("proxies:pool", count=2000):
            key = m[0]
            ip = key.rsplit(":", 1)[0] if ":" in key else key
            if ip in seen or ip in ("0.0.0.0", "127.0.0.1"):
                continue
            cc = _local_lookup(ip)
            if cc == "CN":
                seen.add(ip)
                _redis().sadd("geo:queue", ip)
                count += 1
    except Exception as e:
        pass
    if count:
        print(f"[geo] enqueued {count} unique CN IPs for city lookup")

_searcher = None
