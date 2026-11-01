import requests
import time
import json
import os
import uuid
import threading
import random
import re
import html
import pyotp
import sys
import subprocess
import atexit
import signal
from collections import Counter 
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urljoin
from flask import Flask, Response
from requests.adapters import HTTPAdapter

# ==========================================================================
# ✏️ EDIT HERE  (code-level settings)
# --------------------------------------------------------------------------
# CUSTOM_API_BASE_URL : আপনার নিজের Base URL এখানে বসান (domain only, path ছাড়া)।
#                       উদাহরণ: "https://api.yourdomain.com"
#                       খালি ("") রাখলে Admin Panel / auto-detect ব্যবহার হবে।
# API_PATH            : API-র path prefix. ডিফল্ট "/api/v1"
#                       Final API Base = CUSTOM_API_BASE_URL + API_PATH
# ==========================================================================
CUSTOM_API_BASE_URL = ""
API_PATH = "/api/v1"

# Portable local database.  The complete database is kept in one JSON file,
# so it can be copied to another host and restored without an external service.
DATA_FILE = os.environ.get("BOT_DATA_FILE", "bot_data.json")
_local_db_lock = threading.RLock()

class _Increment:
    def __init__(self, amount):
        self.amount = amount

class _ServerTimestamp:
    pass

class _Snapshot:
    def __init__(self, doc_id, data=None):
        self.id = str(doc_id)
        self._data = data
        self.exists = data is not None
    def to_dict(self):
        return dict(self._data or {})

class _Document:
    def __init__(self, store, collection, doc_id):
        self.store, self.collection, self.doc_id = store, collection, str(doc_id)
    def get(self):
        with _local_db_lock:
            return _Snapshot(self.doc_id, self.store.get(self.collection, {}).get(self.doc_id))
    def set(self, data, merge=False):
        with _local_db_lock:
            bucket = self.store.setdefault(self.collection, {})
            current = bucket.get(self.doc_id, {}) if merge else {}
            bucket[self.doc_id] = _merge_local(current, data)
            _write_local_db()
    def update(self, data):
        return self.set(data, merge=True)

class _Query:
    def __init__(self, store, collection, items=None):
        self.store, self.collection = store, collection
        self.items = items
    def where(self, field, op, value):
        rows = self._rows()
        if op == ">" : rows = [(i, d) for i, d in rows if d.get(field, 0) > value]
        elif op == "==" : rows = [(i, d) for i, d in rows if d.get(field) == value]
        return _Query(self.store, self.collection, rows)
    def order_by(self, field, direction="ASCENDING"):
        rows = self._rows()
        rows.sort(key=lambda x: x[1].get(field, 0) or 0, reverse=direction == "DESCENDING")
        return _Query(self.store, self.collection, rows)
    def limit(self, count):
        return _Query(self.store, self.collection, self._rows()[:count])
    def _rows(self):
        if self.items is not None: return list(self.items)
        return list(self.store.get(self.collection, {}).items())
    def stream(self):
        return [_Snapshot(i, d) for i, d in self._rows()]
    def select(self, _fields):
        return self

class _Collection(_Query):
    def document(self, doc_id):
        return _Document(self.store, self.collection, doc_id)

class _Batch:
    def __init__(self, store):
        self.store, self.operations = store, []
    def update(self, doc, data):
        self.operations.append((doc, data))
    def commit(self):
        for doc, data in self.operations: doc.update(data)

def _merge_local(current, data):
    result = dict(current or {})
    for key, value in (data or {}).items():
        if isinstance(value, _Increment):
            result[key] = result.get(key, 0) + value.amount
        elif isinstance(value, _ServerTimestamp):
            result[key] = datetime.now().isoformat()
        else:
            result[key] = value
    return result

def _write_local_db():
    directory = os.path.dirname(os.path.abspath(DATA_FILE))
    os.makedirs(directory, exist_ok=True)
    temp_file = DATA_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as fp:
        json.dump(local_db_store, fp, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp_file, DATA_FILE)

def _load_local_db():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        print(f"⚠️ Could not read {DATA_FILE}: {exc}")
        return {}

local_db_store = _load_local_db()
DATA_FILE_EXISTED = os.path.isfile(DATA_FILE)

class _LocalDB:
    def collection(self, name):
        return _Collection(local_db_store, name)
    def batch(self):
        return _Batch(local_db_store)
    def export_bytes(self):
        with _local_db_lock:
            return json.dumps(local_db_store, ensure_ascii=False, indent=2).encode("utf-8")
    def import_bytes(self, raw):
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if not isinstance(parsed, dict):
            raise ValueError("Backup must contain a JSON object")
        with _local_db_lock:
            local_db_store.clear()
            local_db_store.update(parsed)
            _write_local_db()

db = _LocalDB()
# These two small helpers preserve the old counter/timestamp call sites while
# using only the local JSON database implementation above.
local_ops = type("LocalOperations", (), {
    "Increment": _Increment,
    "SERVER_TIMESTAMP": _ServerTimestamp()
})

# Config
IS_CHILD = os.environ.get("BOT_ROLE") == "child"
CHILD_BOT_ID = os.environ.get("CHILD_BOT_ID", "")
TOKEN = (os.environ.get("BOT_TOKEN") or "8746930600:AAGU97EkcUGPC1HrqrHfyHQ1QHtbFcP78XI").strip()
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN not set!")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}/"

OWNER_ID = int(os.environ.get("CHILD_ADMIN_ID", "7544406836")) if IS_CHILD else 8280457872
BOT_USERNAME = ""

# ==========================================
# Render Health-Check (Flask + Waitress)
# 🌟 The mini-app system has been fully removed - this Flask app is kept only
# to bind $PORT so Render's health check considers the service alive.
# ==========================================
health_check_web = Flask(__name__)

@health_check_web.route("/")
@health_check_web.route("/healthz")
@health_check_web.route("/api/healthz")
def _health_check():
    try:
        base = get_public_base()
    except Exception:
        base = ""
    return Response(f"API Working ✅\nBase URL: {base}{API_PATH}\n", mimetype="text/plain; charset=utf-8")

def start_health_check_server():
    if IS_CHILD:
        port = int(os.environ.get("CHILD_PORT", 18100))
        bind_host = "127.0.0.1"
    else:
        port = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT") or 10000)
        bind_host = "0.0.0.0"
    try:
        from waitress import serve
        serve(health_check_web, host=bind_host, port=port, threads=256, connection_limit=2000, channel_timeout=120)
    except ImportError:
        health_check_web.run(host=bind_host, port=port)

# ==========================================
# Country metadata (ISO -> name, calling code) + Premium Emoji Database
# ==========================================

COUNTRY_META = {
    "AC": ("Ascension Island", "247"), "AD": ("Andorra", "376"), "AE": ("United Arab Emirates", "971"),
    "AF": ("Afghanistan", "93"), "AG": ("Antigua and Barbuda", "1"), "AI": ("Anguilla", "1"),
    "AL": ("Albania", "355"), "AM": ("Armenia", "374"), "AO": ("Angola", "244"),
    "AR": ("Argentina", "54"), "AS": ("American Samoa", "1"), "AT": ("Austria", "43"),
    "AU": ("Australia", "61"), "AW": ("Aruba", "297"), "AX": ("Aland Islands", "358"),
    "AZ": ("Azerbaijan", "994"), "BA": ("Bosnia and Herzegovina", "387"), "BB": ("Barbados", "1"),
    "BD": ("Bangladesh", "880"), "BE": ("Belgium", "32"), "BF": ("Burkina Faso", "226"),
    "BG": ("Bulgaria", "359"), "BH": ("Bahrain", "973"), "BI": ("Burundi", "257"),
    "BJ": ("Benin", "229"), "BL": ("Saint Barthélemy", "590"), "BM": ("Bermuda", "1"),
    "BN": ("Brunei", "673"), "BO": ("Bolivia", "591"), "BQ": ("Caribbean Netherlands", "599"),
    "BR": ("Brazil", "55"), "BS": ("Bahamas", "1"), "BT": ("Bhutan", "975"),
    "BW": ("Botswana", "267"), "BY": ("Belarus", "375"), "BZ": ("Belize", "501"),
    "CA": ("Canada", "1"), "CC": ("Cocos Islands", "61"), "CD": ("DR Congo", "243"),
    "CF": ("Central African Republic", "236"), "CG": ("Congo", "242"), "CH": ("Switzerland", "41"),
    "CI": ("Cote d'Ivoire", "225"), "CK": ("Cook Islands", "682"), "CL": ("Chile", "56"),
    "CM": ("Cameroon", "237"), "CN": ("China", "86"), "CO": ("Colombia", "57"),
    "CR": ("Costa Rica", "506"), "CU": ("Cuba", "53"), "CV": ("Cabo Verde", "238"),
    "CW": ("Curaçao", "599"), "CX": ("Christmas Island", "61"), "CY": ("Cyprus", "357"),
    "CZ": ("Czechia", "420"), "DE": ("Germany", "49"), "DJ": ("Djibouti", "253"),
    "DK": ("Denmark", "45"), "DM": ("Dominica", "1"), "DO": ("Dominican Republic", "1"),
    "DZ": ("Algeria", "213"), "EC": ("Ecuador", "593"), "EE": ("Estonia", "372"),
    "EG": ("Egypt", "20"), "EH": ("Western Sahara", "212"), "ER": ("Eritrea", "291"),
    "ES": ("Spain", "34"), "ET": ("Ethiopia", "251"), "FI": ("Finland", "358"),
    "FJ": ("Fiji", "679"), "FK": ("Falkland Islands", "500"), "FM": ("Micronesia", "691"),
    "FO": ("Faroe Islands", "298"), "FR": ("France", "33"), "GA": ("Gabon", "241"),
    "GB": ("United Kingdom", "44"), "GD": ("Grenada", "1"), "GE": ("Georgia", "995"),
    "GF": ("French Guiana", "594"), "GG": ("Guernsey", "44"), "GH": ("Ghana", "233"),
    "GI": ("Gibraltar", "350"), "GL": ("Greenland", "299"), "GM": ("Gambia", "220"),
    "GN": ("Guinea", "224"), "GP": ("Guadeloupe", "590"), "GQ": ("Equatorial Guinea", "240"),
    "GR": ("Greece", "30"), "GT": ("Guatemala", "502"), "GU": ("Guam", "1"),
    "GW": ("Guinea-Bissau", "245"), "GY": ("Guyana", "592"), "HK": ("Hong Kong", "852"),
    "HN": ("Honduras", "504"), "HR": ("Croatia", "385"), "HT": ("Haiti", "509"),
    "HU": ("Hungary", "36"), "ID": ("Indonesia", "62"), "IE": ("Ireland", "353"),
    "IL": ("Israel", "972"), "IM": ("Isle of Man", "44"), "IN": ("India", "91"),
    "IO": ("British Indian Ocean Territory", "246"), "IQ": ("Iraq", "964"), "IR": ("Iran", "98"),
    "IS": ("Iceland", "354"), "IT": ("Italy", "39"), "JE": ("Jersey", "44"),
    "JM": ("Jamaica", "1"), "JO": ("Jordan", "962"), "JP": ("Japan", "81"),
    "KE": ("Kenya", "254"), "KG": ("Kyrgyzstan", "996"), "KH": ("Cambodia", "855"),
    "KI": ("Kiribati", "686"), "KM": ("Comoros", "269"), "KN": ("Saint Kitts and Nevis", "1"),
    "KP": ("North Korea", "850"), "KR": ("South Korea", "82"), "KW": ("Kuwait", "965"),
    "KY": ("Cayman Islands", "1"), "KZ": ("Kazakhstan", "7"), "LA": ("Laos", "856"),
    "LB": ("Lebanon", "961"), "LC": ("Saint Lucia", "1"), "LI": ("Liechtenstein", "423"),
    "LK": ("Sri Lanka", "94"), "LR": ("Liberia", "231"), "LS": ("Lesotho", "266"),
    "LT": ("Lithuania", "370"), "LU": ("Luxembourg", "352"), "LV": ("Latvia", "371"),
    "LY": ("Libya", "218"), "MA": ("Morocco", "212"), "MC": ("Monaco", "377"),
    "MD": ("Moldova", "373"), "ME": ("Montenegro", "382"), "MF": ("Saint Martin (French part)", "590"),
    "MG": ("Madagascar", "261"), "MH": ("Marshall Islands", "692"), "MK": ("North Macedonia", "389"),
    "ML": ("Mali", "223"), "MM": ("Myanmar", "95"), "MN": ("Mongolia", "976"),
    "MO": ("Macao", "853"), "MP": ("Northern Mariana Islands", "1"), "MQ": ("Martinique", "596"),
    "MR": ("Mauritania", "222"), "MS": ("Montserrat", "1"), "MT": ("Malta", "356"),
    "MU": ("Mauritius", "230"), "MV": ("Maldives", "960"), "MW": ("Malawi", "265"),
    "MX": ("Mexico", "52"), "MY": ("Malaysia", "60"), "MZ": ("Mozambique", "258"),
    "NA": ("Namibia", "264"), "NC": ("New Caledonia", "687"), "NE": ("Niger", "227"),
    "NF": ("Norfolk Island", "672"), "NG": ("Nigeria", "234"), "NI": ("Nicaragua", "505"),
    "NL": ("Netherlands", "31"), "NO": ("Norway", "47"), "NP": ("Nepal", "977"),
    "NR": ("Nauru", "674"), "NU": ("Niue", "683"), "NZ": ("New Zealand", "64"),
    "OM": ("Oman", "968"), "PA": ("Panama", "507"), "PE": ("Peru", "51"),
    "PF": ("French Polynesia", "689"), "PG": ("Papua New Guinea", "675"), "PH": ("Philippines", "63"),
    "PK": ("Pakistan", "92"), "PL": ("Poland", "48"), "PM": ("Saint Pierre and Miquelon", "508"),
    "PR": ("Puerto Rico", "1"), "PS": ("Palestine", "970"), "PT": ("Portugal", "351"),
    "PW": ("Palau", "680"), "PY": ("Paraguay", "595"), "QA": ("Qatar", "974"),
    "RE": ("Réunion", "262"), "RO": ("Romania", "40"), "RS": ("Serbia", "381"),
    "RU": ("Russia", "7"), "RW": ("Rwanda", "250"), "SA": ("Saudi Arabia", "966"),
    "SB": ("Solomon Islands", "677"), "SC": ("Seychelles", "248"), "SD": ("Sudan", "249"),
    "SE": ("Sweden", "46"), "SG": ("Singapore", "65"), "SH": ("Saint Helena", "290"),
    "SI": ("Slovenia", "386"), "SJ": ("Svalbard and Jan Mayen", "47"), "SK": ("Slovakia", "421"),
    "SL": ("Sierra Leone", "232"), "SM": ("San Marino", "378"), "SN": ("Senegal", "221"),
    "SO": ("Somalia", "252"), "SR": ("Suriname", "597"), "SS": ("South Sudan", "211"),
    "ST": ("Sao Tome and Principe", "239"), "SV": ("El Salvador", "503"), "SX": ("Sint Maarten", "1"),
    "SY": ("Syria", "963"), "SZ": ("Eswatini", "268"), "TA": ("Tristan da Cunha", "290"),
    "TC": ("Turks and Caicos Islands", "1"), "TD": ("Chad", "235"), "TG": ("Togo", "228"),
    "TH": ("Thailand", "66"), "TJ": ("Tajikistan", "992"), "TK": ("Tokelau", "690"),
    "TL": ("Timor-Leste", "670"), "TM": ("Turkmenistan", "993"), "TN": ("Tunisia", "216"),
    "TO": ("Tonga", "676"), "TR": ("Turkey", "90"), "TT": ("Trinidad and Tobago", "1"),
    "TV": ("Tuvalu", "688"), "TW": ("Taiwan", "886"), "TZ": ("Tanzania", "255"),
    "UA": ("Ukraine", "380"), "UG": ("Uganda", "256"), "US": ("United States", "1"),
    "UY": ("Uruguay", "598"), "UZ": ("Uzbekistan", "998"), "VA": ("Vatican City", "39"),
    "VC": ("Saint Vincent and the Grenadines", "1"), "VE": ("Venezuela", "58"), "VG": ("British Virgin Islands", "1"),
    "VI": ("US Virgin Islands", "1"), "VN": ("Vietnam", "84"), "VU": ("Vanuatu", "678"),
    "WF": ("Wallis and Futuna", "681"), "WS": ("Samoa", "685"), "XK": ("Kosovo", "383"),
    "YE": ("Yemen", "967"), "YT": ("Mayotte", "262"), "ZA": ("South Africa", "27"),
    "ZM": ("Zambia", "260"), "ZW": ("Zimbabwe", "263"),
}

# ==========================================
PEM = {
    "ok": '<tg-emoji emoji-id="5352694861990501856">✅</tg-emoji>',
    "no": '<tg-emoji emoji-id="5420130255174145507">❌</tg-emoji>',
    "warn": '<tg-emoji emoji-id="5336944168944047463">⚠️</tg-emoji>',
    "admin": '<tg-emoji emoji-id="5353032893096567467">📊</tg-emoji>',
    "user": '<tg-emoji emoji-id="5352861489541714456">👤</tg-emoji>',
    "file": '<tg-emoji emoji-id="5352721946054268944">📁</tg-emoji>',
    "rocket": '<tg-emoji emoji-id="5352597830089347330">🚀</tg-emoji>',
    "graph": '<tg-emoji emoji-id="5352877703043258544">📊</tg-emoji>',
    "money": '<tg-emoji emoji-id="5348469219761626211">💸</tg-emoji>',
    "gift": '<tg-emoji emoji-id="5420396762189831222">🎁</tg-emoji>',
    "msg": '<tg-emoji emoji-id="5337302974806922068">💬</tg-emoji>',
    "gear": '<tg-emoji emoji-id="5420155432272438703">⚙️</tg-emoji>',
    "link": '<tg-emoji emoji-id="5420517437885943844">🔗</tg-emoji>',
    "trash": '<tg-emoji emoji-id="5422557736330106570">🗑</tg-emoji>',
    "upload": '<tg-emoji emoji-id="5353001161878182134">📤</tg-emoji>',
    "world": '<tg-emoji emoji-id="5336972142066047577">🌐</tg-emoji>',
    "lock": '<tg-emoji emoji-id="5353022963132174959">🔐</tg-emoji>',
    "phone": '<tg-emoji emoji-id="5337132498965010628">📱</tg-emoji>',
    "num": '<tg-emoji emoji-id="5352862640592949843">🔢</tg-emoji>',
    "pin": '<tg-emoji emoji-id="5352922460897452503">📍</tg-emoji>',
    "star": '<tg-emoji emoji-id="5352552689983067014">✨</tg-emoji>',
    "hi": '<tg-emoji emoji-id="5353027129250453493">👋</tg-emoji>',
    # 🌟 Added from NewsEmoji pack
    "gold": '<tg-emoji emoji-id="5440539497383087970">🥇</tg-emoji>',
    "silver": '<tg-emoji emoji-id="5447203607294265305">🥈</tg-emoji>',
    "bronze": '<tg-emoji emoji-id="5453902265922376865">🥉</tg-emoji>',
    "crown": '<tg-emoji emoji-id="5217822164362739968">👑</tg-emoji>',
    "celebrate": '<tg-emoji emoji-id="5461151367559141950">🎉</tg-emoji>',
    "info": '<tg-emoji emoji-id="5334544901428229844">ℹ️</tg-emoji>',
    "search": '<tg-emoji emoji-id="5231012545799666522">🔍</tg-emoji>',
    "refresh": '<tg-emoji emoji-id="5375338737028841420">🔄</tg-emoji>',
    "new": '<tg-emoji emoji-id="5382357040008021292">🆕</tg-emoji>',
    "top": '<tg-emoji emoji-id="5415655814079723871">🔝</tg-emoji>',
    "fire": '<tg-emoji emoji-id="5424972470023104089">🔥</tg-emoji>',
    "check": '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>',
    "idea": '<tg-emoji emoji-id="5422439311196834318">💡</tg-emoji>',
    "calendar": '<tg-emoji emoji-id="5413879192267805083">🗓</tg-emoji>'
}

GLOBAL_BODY_EMOJIS = {
    "➖": "5870818207383686839", "🚫": "5334807341109908955", "😒": "5334763399299506604",
    "🖥": "5334880948259427772", "🌐": "5334590977837403844", "🌟": "5337102391244263212",
    "🕓": "5336983442125001376", "⌛": "5337172996211648018", "💬": "5337302974806922068",
    "🔐": "5337255927735163754", "🍏": "5337132498965010628", "❔": "5336850036145823599",
    "⚠️": "5336944168944047463", "🔥": "5337267511261960341", "💸": "5348469219761626211",
    "🥚": "5348390922507817684", "👨‍⚖": "5334763399299506604", "🐁": "5348494358205207761",
    "🧻": "5348486915026884464", "⚗": "5346311574221000149", "🛴": "5348075478634766440",
    "📊": "5353032893096567467", "🔢": "5352862640592949843", "👤": "5352861489541714456",
    "📁": "5352721946054268944", "🚀": "5352597830089347330", "💎": "5352838545826420397",
    "📍": "5352922460897452503", "👋": "5353027129250453493", "✅": "5352694861990501856",
    "1️⃣": "5352651766288652742", "2️⃣": "5355186458418257716", "3️⃣": "5352867219028091093",
    "4️⃣": "5352566657216714037", "5️⃣": "5353086880835474989", "6️⃣": "5354859211975071385",
    "7️⃣": "5352859127309707652", "8️⃣": "5352957533600389988", "9️⃣": "5353060913463204207",
    "🔤": "5352727417842606016", "📣": "5352980533150259581", "📤": "5353001161878182134",
    "✨": "5352552689983067014", "🔹": "5352638632278660622", "🎙": "5355102594886833928",
    "💴": "5352985330628730418", "📅": "5352585194295564660", "📴": "5352974971167611327",
    "✏️": "5395444784611480792", "📱": "5337132498965010628", "🔗": "5420517437885943844",
    "❌": "5420130255174145507", "⚙️": "5420155432272438703", "🫂": "5420145051336485498",
    "➕": "5420323438508155202", "🗑": "5422557736330106570", "🎁": "5420396762189831222",
    "➤": "5420618897898381296", "🏢": "5420156334215565595", "💳": "5190899075968441286",
    "📝": "5192739271886282680", "🛡": "5190447043545438788", "🤝": "5192805934073685937",
    "💰": "5190576863226933563", "👀": "5190645917711114179", "🕹": "5193100774988617665",
    "🟢": "5192812028632274956", "🧪": "5190781475468915802", "🎨": "5190751148704833975",
    "📂": "5257969839313526622", "🌍": "5780471598922337683", "📌": "5318986077455795572",
    "📢": "5789428375261023681", "🆔": "5352862640592949843", "📈": "5352877703043258544",
    "🔔": "5352980533150259581", "🏦": "5348469219761626211", "🧾": "5192739271886282680",
    "👨‍⚖️": "5334763399299506604", "🔍": "5463352748751753567",
    "🔑": "5197288647275071607",
    # 🌟 Added from NewsEmoji pack - only new emoji chars, existing ones kept as-is
    "🙂": "5461117441612462242", "⚡️": "5456140674028019486", "☄️": "5224607267797606837",
    "🛍": "5229064374403998351", "⛔️": "5260293700088511294", "❗️": "5274099962655816924",
    "‼️": "5440660757194744323", "⁉️": "5314504236132747481", "❓": "5436113877181941026",
    "💭": "5467538555158943525", "🔼": "5449683594425410231", "🔽": "5447183459602669338",
    "🕯": "5451882707875276247", "📉": "5246762912428603768", "✔️": "5206607081334906820",
    "🆒": "5222079954421818267", "🥸": "5391112412445288650", "🤡": "5269531045165816230",
    "🫦": "5395444514028529554", "💵": "5409048419211682843", "💱": "5402186569006210455",
    "▶️": "5264919878082509254", "🔴": "5411225014148014586", "➡️": "5416117059207572332",
    "💥": "5276032951342088188", "🎤": "5224736245665511429", "🤫": "5431609822288033666",
    "👎": "5449875686837726134", "🗣️": "5460795800101594035", "©": "5323442290708985472",
    "ℹ️": "5334544901428229844", "👍": "5337080053119336309", "⏸": "5359543311897998264",
    "💯": "5341498088408234504", "🔄": "5375338737028841420", "🔝": "5415655814079723871",
    "🆕": "5382357040008021292", "🔜": "5440621591387980068", "⭐️": "5438496463044752972",
    "👑": "5217822164362739968", "🔖": "5222444124698853913", "✉️": "5253742260054409879",
    "🔒": "5296369303661067030", "😮": "5303479226882603449", "📎": "5305265301917549162",
    "🎮": "5361741454685256344", "🔈": "5388632425314140043", "⬇️": "5406745015365943482",
    "☀️": "5402477260982731644", "🌧": "5399913388845322366", "🌛": "5449569374065152798",
    "❄️": "5449449325434266744", "🌈": "5409109841538994759", "💧": "5393512611968995988",
    "🗓": "5413879192267805083", "💡": "5422439311196834318", "🥇": "5440539497383087970",
    "🥈": "5447203607294265305", "🥉": "5453902265922376865", "🎵": "5463107823946717464",
    "🆓": "5406756500108501710", "🚨": "5395695537687123235", "🏠": "5416041192905265756",
    "🚩": "5460755126761312667", "🎉": "5461151367559141950"
}

DEFAULT_CUSTOM_MESSAGES = {
    "start": {"text": "╔═══════════╗\n       📊 NUMBER BOT\n╚═══════════╝\n🚀 Welcome to Number & OTP Service\n━━━━━━━━━━━━\n✅ Choose an option below\nto continue using the bot.\n━━━━━━━━━━━━\n💎 Premium OTP Service", "buttons": []},
    "get_number": {"text": f"{PEM['pin']} Select a service:", "buttons": []},
    "select_country": {"text": f"📌 Select a country for {{service}}:", "buttons": []}, 
    "refer": {"text": f"➖➖➖➖➖➖➖\n« {PEM['gift']} REFER & EARN »\n➖➖➖➖➖➖➖\n{PEM['link']} YOUR LINK:\n<code>{{ref_link}}</code>\n➖➖➖➖➖➖➖\n{PEM['user']} TOTAL REFERS: <b>{{total_ref}}</b>\n➖➖➖➖➖➖➖\n{PEM['money']} PER REFER: <b>{{ref_reward}} TK</b>\n➖➖➖➖➖➖➖", "buttons": []},
    "withdrawal": {"text": "➖➖➖➖➖➖➖\n《 😒 WITHDRAWAL 》\n➖➖➖➖➖➖➖\n👋 Total Otp: {total_otp}\n➖➖➖➖➖➖➖\n🫂 Total Reffer :{total_ref}\n➖➖➖➖➖➖➖\n📅 BALANCE: {bal}৳\n➖➖➖➖➖➖➖\n🔐 MINIMUM: {min_w} ৳\n➖➖➖➖➖➖➖\nSELECT METHOD:", "buttons": []},
    "support": {"text": f"{PEM['msg']} Contact us for any help:", "buttons": []}
}

print(f"✅ Local database ready: {DATA_FILE}")
if not DATA_FILE_EXISTED:
    with _local_db_lock:
        _write_local_db()
    print("🆕 New data file created because no backup was found.")
else:
    print("♻️ Existing data file found; it will be used as-is.")

bot_settings = {
    "admins": [OWNER_ID],
    "fw_groups": [], 
    "otp_link": "https://t.me/your_otp_group",
    "withdraw_on": True,
    "min_withdraw": 30.0,
    "otp_reward": 0.1,
    "refer_reward": 0.2,
    "weekly_reset_at": 0,
    "cooldown": 10,
    "num_req": 3,
    "num_share": 1, 
    "support_link": "https://t.me/your_support",
    "w_methods": ["bKash", "Nagad"],
    "w_group": "", 
    
    "fj_on": False,
    "fj_channels": [], 
    "stex_keys": [], 
    "api_base_url": "",
    "api_ranges": {},
    "child_bots": {},
    "stex_services": {},
    "stex_service_rates": {},
    "premium_flags": {
        "1": {"char": "🇺🇸", "iso": "US", "name": "United States", "id": "5913463998522592692"},
        "880": {"char": "🇧🇩", "iso": "BD", "name": "Bangladesh", "id": "5911365056594973179"},
        "91": {"char": "🇮🇳", "iso": "IN", "name": "India", "id": "5913754823643107921"},
        "92": {"char": "🇵🇰", "iso": "PK", "name": "Pakistan", "id": "5913705895375672082"},
        "44": {"char": "🇬🇧", "iso": "GB", "name": "United Kingdom", "id": "5913443365499703513"}
    },
    "premium_apps": {
        # 🌟 Expanded from the Premium_App.txt pack the admin provided, so OTP
        # cards show a real premium icon for every common service instead of
        # the generic 📱 fallback. Keys match SERVICE_SMS_KEYWORDS where one
        # exists (so incoming SMS text auto-detects them); others are matched
        # directly against a manually configured service name.
        "FACEBOOK": {"char": "📘", "id": "5334807341109908955", "name": "Facebook"},
        "WHATSAPP": {"char": "💬", "id": "5334759662677957452", "name": "WhatsApp"},
        "TELEGRAM": {"char": "✈️", "id": "5337010556253543833", "name": "Telegram"},
        "IMO": {"char": "💭", "id": "5337155807752524558", "name": "Imo"},
        "INSTAGRAM": {"char": "📸", "id": "5334868205091459431", "name": "Instagram"},
        "APPLE": {"char": "🍎", "id": "5334637951894722661", "name": "Apple"},
        "GOOGLE": {"char": "🔍", "id": "5335010201005231986", "name": "Google"},
        "MICROSOFT": {"char": "🪟", "id": "5334880948259427772", "name": "Microsoft"},
        "TEAMS": {"char": "🧑‍🤝‍🧑", "id": "5334590977837403844", "name": "Teams"},
        "TIKTOK": {"char": "🎵", "id": "5339213256001102461", "name": "Tiktok"},
        "BKASH": {"char": "🏦", "id": "5348469219761626211", "name": "Bkash"},
        "ROCKET": {"char": "🚀", "id": "5346042941196507141", "name": "Rocket"},
        "BYBIT": {"char": "📈", "id": "5348372939479751825", "name": "Bybit"},
        "BINANCE": {"char": "💱", "id": "5348212415077064131", "name": "Binance"},
        "MELBET": {"char": "🌟", "id": "5337102391244263212", "name": "Melbet"},
        "SNAPCHAT": {"char": "👻", "id": "5359441366554255082", "name": "Snapchat"},
        "UBER": {"char": "🚗", "id": "5298715455316303708", "name": "Uber"},
        "PAYPAL": {"char": "💵", "id": "5776103539872896061", "name": "PayPal"},
        "DISCORD": {"char": "🎬", "id": "5116246243646898866", "name": "Discord"},
        "AMAZON": {"char": "🌟", "id": "4995019580536524226", "name": "Amazon"},
        "VIBER": {"char": "💜", "id": "5463060437572528782", "name": "Viber"},
        "LINKEDIN": {"char": "💼", "id": "6224222994265279792", "name": "Linkedin"},
        "LINE": {"char": "🔒", "id": "5399818044866327279", "name": "Line"},
        "WECHAT": {"char": "🌟", "id": "5782757599560602950", "name": "Wechat"},
        "TWITTER": {"char": "🐦", "id": "5215726959056662534", "name": "Twitter"},
        "REDDIT": {"char": "👽", "id": "4992421103847604984", "name": "Reddit"},
        "PINTEREST": {"char": "📌", "id": "5346103513120258857", "name": "Pinterest"},
        "TWITCH": {"char": "🎮", "id": "5233333563306301418", "name": "Twitch"},
        "ZOOM": {"char": "📹", "id": "5881799193219043268", "name": "Zoom"},
        "SIGNAL": {"char": "💬", "id": "5293998404404272267", "name": "Signal"},
        "SLACK": {"char": "💻", "id": "4994972469040251302", "name": "Slack"},
        "SKYPE": {"char": "☎️", "id": "4992613535562334989", "name": "Skype"},
        "NETFLIX": {"char": "🎥", "id": "6255738712664050133", "name": "Netflix"},
        "SPOTIFY": {"char": "🎵", "id": "5411392711146095115", "name": "Spotify"},
        "AMAZONPRIME": {"char": "📺", "id": "6111801057061374810", "name": "Amazon Prime"},
        "HOICHOI": {"char": "🍿", "id": "6104822598493801746", "name": "Hoichoi"},
        "DARAZ": {"char": "📦", "id": "5336879280578138635", "name": "Daraz"},
        "FOODPANDA": {"char": "🐼", "id": "5336879280578138635", "name": "Foodpanda"},
        "PATHAO": {"char": "🛵", "id": "5336879280578138635", "name": "Pathao"},
        "ALIEXPRESS": {"char": "🛒", "id": "5336879280578138635", "name": "AliExpress"},
        "SHOPEE": {"char": "🛍️", "id": "5336879280578138635", "name": "Shopee"},
        "PAYONEER": {"char": "💳", "id": "5336879280578138635", "name": "Payoneer"},
        "WISE": {"char": "🦉", "id": "5336879280578138635", "name": "Wise"},
        "CHATGPT": {"char": "🤖", "id": "5296516998996445955", "name": "ChatGPT"},
        "NOTION": {"char": "📓", "id": "5336879280578138635", "name": "Notion"},
        "GITHUB": {"char": "🐙", "id": "5417836094098007862", "name": "GitHub"},
        "CANVA": {"char": "🖌️", "id": "5111661409008092227", "name": "Canva"},
        "FIGMA": {"char": "🎨", "id": "5336879280578138635", "name": "Figma"},
        "UPWORK": {"char": "💼", "id": "5336879280578138635", "name": "Upwork"},
        "FIVERR": {"char": "🟢", "id": "5336879280578138635", "name": "Fiverr"},
        "YAHOO": {"char": "🌐", "id": "5336879280578138635", "name": "Yahoo"},
        "DROPBOX": {"char": "☁️", "id": "5336879280578138635", "name": "Dropbox"},
        "COURSERA": {"char": "📚", "id": "5336879280578138635", "name": "Coursera"},
        "DUOLINGO": {"char": "🗣️", "id": "5336879280578138635", "name": "Duolingo"}
    },
    "status_services": [],
    "custom_messages": DEFAULT_CUSTOM_MESSAGES.copy()
}

FS_KEYS = [
    "admins", "fw_groups", "otp_link", "withdraw_on", 
    "min_withdraw", "otp_reward", "refer_reward", "cooldown", 
    "num_req", "num_share", "support_link", "w_methods", "w_group", "stex_keys", "stex_services",
    "fj_on", "fj_channels", "stex_service_rates", "api_base_url", "api_ranges", "child_bots",
    # 🌟 FIX: these were missing before, so every admin edit to premium flags/app icons and
    # custom menu text (start/get_number/refer/withdrawal/support) was silently lost on
    # every restart/redeploy - they are written to the local data file.
    "premium_flags", "premium_apps", "custom_messages", "status_services", "weekly_reset_at"
]

number_batches = {}
used_numbers_list = []
stex_assigned_numbers = {} 
api_numbers = {}
api_otp_log = {}
STEX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"
total_uploaded_stats = 0
total_assigned_stats = 0
processed_otps = set() 
user_banned_cache = {}


# 🌟 sAjaxSource (AJAX/DataTable) এবং Fallback HTML Parser Helper Function
# Track active number sessions to expire them automatically
user_active_sessions = {}
_otp_claim_lock = threading.Lock()

assigned_number_rates = {}

def claim_processed_otp(number, otp, legacy_ids=()):
    """Atomically suppress the same number+OTP, even when two listeners see it."""
    clean_number = re.sub(r"\D", "", str(number))
    clean_otp = str(otp).strip()
    if not clean_number or not clean_otp:
        return False
    unique_id = f"OTP_{clean_number}_{clean_otp}"
    with _otp_claim_lock:
        # Accept the identifiers written by older versions too.  Without this
        # migration check, the first restart after the fix could replay an OTP
        # that was already stored as PANEL_/VOLTX_/YESMS_/STEX_/POLL_ data.
        old_suffix = f"_{clean_number}_{clean_otp}"
        if (
            unique_id in processed_otps
            or any(str(item) == f"{clean_number}_{clean_otp}" or str(item).endswith(old_suffix)
                   for item in processed_otps)
            or any(item in processed_otps for item in legacy_ids)
        ):
            return False
        processed_otps.add(unique_id)
        # Do not clear the whole set: that caused old provider messages to be
        # delivered again whenever the set crossed the previous limit.
        if len(processed_otps) > 10000:
            processed_otps.pop()
        return True

def resolve_manual_service_rate(provider_key, service, rng):
    # 🌟 Manually added Voltx/StexSMS/Zebrasms service+country এর জন্য কাস্টম OTP রেট বের করা
    # provider_key: "stex" | "voltx" | "zebrasms"
    if IS_CHILD:
        return float(bot_settings.get("otp_reward", 0.0))
    if not service:
        return bot_settings.get("otp_reward", 0.0)
    services_dict = bot_settings.get(f"{provider_key}_services", {}).get(service, {})
    for cnt, ranges in services_dict.items():
        if rng in ranges:
            rate = bot_settings.get(f"{provider_key}_service_rates", {}).get(service, {}).get(cnt)
            if rate is not None:
                return float(rate)
            break
    return bot_settings.get("otp_reward", 0.0)

# All persistent state lives in the portable local database file.
DATA_KEYS = [
    "number_batches", "used_numbers_list", "total_uploaded_stats", "total_assigned_stats",
    "stex_assigned_numbers", "processed_otps", "assigned_number_rates", "api_numbers", "api_otp_log",
]

def load_db():
    global bot_settings, number_batches, used_numbers_list, total_uploaded_stats, total_assigned_stats, stex_assigned_numbers, processed_otps, assigned_number_rates, api_numbers, api_otp_log

    try:
        doc = None
        for attempt in range(3):
            try:
                doc = db.collection('settings').document('bot_config').get()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if doc.exists:
            fs_data = doc.to_dict()
            for k, val in fs_data.items():
                if k == "custom_messages":
                    for m_key, m_val in val.items():
                        bot_settings["custom_messages"][m_key] = m_val
                else:
                    bot_settings[k] = val
            for m_key, m_val in DEFAULT_CUSTOM_MESSAGES.items():
                if m_key not in bot_settings["custom_messages"]:
                    bot_settings["custom_messages"][m_key] = m_val
        else:
            db.collection('settings').document('bot_config').set({k: bot_settings[k] for k in FS_KEYS if k in bot_settings})
        print("✅ Config loaded from local file!")
    except Exception as e:
        print(f"❌ Error loading config from local file: {e}")

    try:
        doc = None
        for attempt in range(3):
            try:
                doc = db.collection('settings').document('bot_data').get()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        data = doc.to_dict() if doc.exists else {}
        number_batches = data.get("number_batches", {})
        used_numbers_list = data.get("used_numbers_list", [])
        total_uploaded_stats = data.get("total_uploaded_stats", 0)
        total_assigned_stats = data.get("total_assigned_stats", 0)
        stex_assigned_numbers = data.get("stex_assigned_numbers", {})
        processed_otps = set(data.get("processed_otps", []))
        assigned_number_rates = data.get("assigned_number_rates", {})
        api_numbers = data.get("api_numbers", {})
        api_otp_log = data.get("api_otp_log", {})
        print("✅ Data loaded from local file!")
    except Exception as e:
        print(f"❌ Error loading data from local file: {e}")

def _sync_fs():
    try:
        db.collection('settings').document('bot_config').set({k: bot_settings[k] for k in FS_KEYS if k in bot_settings})
        data = {
            "number_batches": number_batches, "used_numbers_list": used_numbers_list,
            "total_uploaded_stats": total_uploaded_stats, "total_assigned_stats": total_assigned_stats,
            "stex_assigned_numbers": stex_assigned_numbers,
            "processed_otps": list(processed_otps), "assigned_number_rates": assigned_number_rates,
            "api_numbers": api_numbers, "api_otp_log": api_otp_log,
        }
        db.collection('settings').document('bot_data').set(data)
    except Exception as e:
        print(f"❌ Error saving to local file: {e}")

_save_evt = threading.Event()

def _save_worker():
    while True:
        _save_evt.wait()
        time.sleep(0.4)          # coalesce bursts into ONE write
        _save_evt.clear()
        _sync_fs()

threading.Thread(target=_save_worker, daemon=True).start()
atexit.register(_sync_fs)

def save_db():
    # Debounced: returns instantly, many calls => a single disk write.
    _save_evt.set()

load_db()

def retry_local_load():
    # Keep the in-memory state aligned if the data file is replaced manually.
    while True:
        time.sleep(60)
        try:
            load_db()
        except Exception as e:
            print(f"Deferred local database reload failed: {e}")

user_states = {}
temp_data = {}
user_cooldowns = {}
pending_withdrawals = {}

# ==========================================
# Telegram API & Helpers
# ==========================================
tg_session = requests.Session() # 🌟 Keep-Alive Connection (Makes bot 10x faster)
tg_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=96))

def api_call(method, payload=None):
    url = f"{BASE_URL}/{method}"
    # getUpdates uses Telegram long-polling (timeout=50), so the HTTP
    # read timeout must be longer than the Telegram polling timeout.
    request_timeout = (10, 65) if method.startswith("getUpdates") else (10, 20)
    for attempt in range(3):
        try:
            res = tg_session.post(url, json=payload, timeout=request_timeout)
            res.raise_for_status()
            result = res.json()
            if result.get("ok"):
                return result
            description = result.get("description", "unknown Telegram API error")
            parameters = result.get("parameters", {})
            retry_after = parameters.get("retry_after")
            if retry_after and attempt < 2:
                time.sleep(min(int(retry_after), 10))
                continue
            print(f"Telegram API rejected {method}: {description}")
            return result
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                print(f"Telegram API error ({method}): {e}")
                return {}
            time.sleep(2 ** attempt)

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("sendMessage", payload)

def send_photo(chat_id, photo_url_or_file_id, caption="", reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "photo": photo_url_or_file_id, "caption": caption, "parse_mode": parse_mode}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("sendPhoto", payload)

def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("editMessageText", payload)

def delete_message(chat_id, message_id):
    return api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def answer_callback(callback_id, text="", show_alert=False):
    api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": show_alert})

def generate_emoji_txt(mode):
    """Builds a .txt export in the exact format the flag/app TXT importer expects
    (see wait_for_flag_txt / wait_for_app_txt handling above)."""
    lines = []
    if mode == "flags":
        for code, data in bot_settings.get("premium_flags", {}).items():
            name = data.get("name", "")
            iso = data.get("iso", "")
            char = data.get("char", "")
            eid = data.get("id", "")
            payload = json.dumps({"emoji": char, "id": eid}, ensure_ascii=False)
            lines.append(f"{name} ({code})({iso}) {payload}")
    else:
        for name_key, data in bot_settings.get("premium_apps", {}).items():
            name = data.get("name", name_key)
            char = data.get("char", "")
            eid = data.get("id", "")
            payload = json.dumps({"emoji": char, "id": eid}, ensure_ascii=False)
            lines.append(f"{name} {payload}")
    if not lines:
        return None
    return "\n".join(lines)

def send_document(chat_id, filename, text_content):
    url = f"{BASE_URL}/sendDocument"
    files = {'document': (filename, text_content)}
    data = {'chat_id': chat_id}
    try:
        requests.post(url, data=data, files=files, timeout=(10, 30))
    except requests.RequestException as e:
        print(f"Telegram document upload error: {e}")

# In-memory cache of known user IDs, loaded once from the local database.
all_known_users = set()

def sync_users_list():
    global all_known_users
    try:
        for doc in db.collection('users').select([]).stream():
            all_known_users.add(doc.id)
    except: pass

threading.Thread(target=sync_users_list, daemon=True).start()

def register_user_local(uid):
    all_known_users.add(str(uid))

def broadcast_copymessage(from_chat_id, msg_id):
    success = 0
    failed = 0
    users = list(all_known_users)
    
    # 🌟 Dedicated Connection Pool for Broadcast (Fixes Port Exhaustion & Network Lag)
    b_session = requests.Session()
    url = f"{BASE_URL}/copyMessage"
    
    for user_id in users:
        payload = {"chat_id": user_id, "from_chat_id": from_chat_id, "message_id": msg_id}
        try:
            res = b_session.post(url, json=payload, timeout=5).json()
            if res.get("ok"): success += 1
            else: failed += 1
        except:
            failed += 1
        time.sleep(0.035) # Safe speed (28 msgs/sec) to prevent Telegram Ban
        
    send_message(from_chat_id, render_body_text(f"📢 <b>Broadcast Completed!</b>\n✅ Success: {success}\n❌ Failed: {failed}\n👥 Total Sent: {len(users)}"))

def render_body_text(text):
    if not text: return str(text)
    parts = re.split(r'(<tg-emoji.*?</tg-emoji>)', str(text))
    for i in range(len(parts)):
        if not parts[i].startswith('<tg-emoji'):
            for normal_emj, prem_id in GLOBAL_BODY_EMOJIS.items():
                if normal_emj in parts[i]:
                    parts[i] = parts[i].replace(normal_emj, f'<tg-emoji emoji-id="{prem_id}">{normal_emj}</tg-emoji>')
    return "".join(parts)

def extract_premium_html(msg):
    text = msg.get("text", msg.get("caption", ""))
    entities = msg.get("entities", msg.get("caption_entities", []))
    if not entities: return text
    try:
        b_text = text.encode('utf-16-le')
        c_entities = [e for e in entities if e.get("type") == "custom_emoji"]
        c_entities.sort(key=lambda x: x["offset"], reverse=True)
        for ent in c_entities:
            offset = ent["offset"] * 2
            length = ent["length"] * 2
            eid = ent["custom_emoji_id"]
            emoji_char = b_text[offset:offset+length].decode('utf-16-le')
            html_tag = f'<tg-emoji emoji-id="{eid}">{emoji_char}</tg-emoji>'
            replacement = html_tag.encode('utf-16-le')
            b_text = b_text[:offset] + replacement + b_text[offset+length:]
        return b_text.decode('utf-16-le')
    except Exception as e:
        return text 

def country_code_from_label(country):
    """Return the ISO code from BD, Bangladesh, or Bangladesh (BD)."""
    value = str(country or "").strip().upper()
    match = re.search(r"\(([A-Z]{2})\)\s*$", value)
    if match:
        return match.group(1)
    if value in COUNTRY_META:
        return value
    for iso, (name, _) in COUNTRY_META.items():
        if value == name.upper():
            return iso
    for flag_data in bot_settings.get("premium_flags", {}).values():
        if value == str(flag_data.get("name", "")).strip().upper():
            return str(flag_data.get("iso", "")).upper()
    return ""

def country_display_name(country):
    """Store/display countries consistently as Full Name (ISO).

    Also supports a trailing number to create a 2nd/3rd entry for the same
    country, e.g. "MG 2" -> "Madagascar 2 (MG)", "MG 3" -> "Madagascar 3 (MG)".
    This is used identically by every panel (StexSMS/Voltx/Zebrasms/Yesms).
    """
    raw = str(country or "").strip()
    suffix = ""
    m = re.match(r"^(.*\S)\s+(\d+)$", raw)
    base = raw
    if m and country_code_from_label(m.group(1)):
        base = m.group(1)
        suffix = f" {m.group(2)}"
    iso = country_code_from_label(base)
    # Use the admin's premium flag database for every ISO code, not only the
    # small built-in metadata list.
    for flag_data in bot_settings.get("premium_flags", {}).values():
        if flag_data.get("iso", "").upper() == iso:
            return f"{flag_data.get('name', base)}{suffix} ({iso})"
    if not iso:
        return raw
    return f"{COUNTRY_META.get(iso, (base.title(), ''))[0]}{suffix} ({iso})"

def country_calling_code(country):
    iso = country_code_from_label(country)
    for calling_code, flag_data in bot_settings.get("premium_flags", {}).items():
        if flag_data.get("iso", "").upper() == iso and str(calling_code).isdigit():
            return str(calling_code)
    return COUNTRY_META.get(iso, ("", ""))[1]

def unicode_flag(iso):
    iso = str(iso or "").upper()
    if len(iso) != 2 or not iso.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(c)) for c in iso)

def get_country_flag_info(country):
    """Resolve a country flag by exact ISO/name matching only."""
    raw = str(country or "").strip()
    iso = country_code_from_label(raw)
    for flag_data in bot_settings.get("premium_flags", {}).values():
        flag_iso = flag_data.get("iso", "").upper()
        flag_name = str(flag_data.get("name", "")).strip().upper()
        if (iso and flag_iso == iso) or (not iso and raw.upper() == flag_name):
            return flag_data.get("char", unicode_flag(flag_iso)), flag_data.get("id")
    return (unicode_flag(iso) if iso else "🌍"), None

def get_flag_info_from_num(num):
    clean = re.sub(r"[^\d]", "", str(num or ""))
    sorted_codes = sorted(bot_settings.get("premium_flags", {}).keys(), key=len, reverse=True)
    for code in sorted_codes:
        if clean.startswith(code):
            data = bot_settings["premium_flags"][code]
            return data["char"], data.get("iso", "XX"), data.get("id")
    # Fallback for countries that do not have a custom Telegram emoji entry.
    for iso, (_, calling) in COUNTRY_META.items():
        if calling and clean.startswith(calling):
            return unicode_flag(iso), iso, None
    return "🌍", "XX", None

def get_flag_and_code(num):
    char, iso, _ = get_flag_info_from_num(num)
    return char, iso

def get_flag_info_html(num_or_iso, return_full_name=False):
    iso_hint = country_code_from_label(num_or_iso)
    if iso_hint:
        num_or_iso = iso_hint
    if len(str(num_or_iso)) == 2:
        for code, data in bot_settings.get("premium_flags", {}).items():
            if data.get("iso", "").upper() == str(num_or_iso).upper():
                eid = data.get("id")
                char = data.get("char")
                name = data.get("name", num_or_iso)
                if return_full_name: return name
                if eid: return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
                return char
        if str(num_or_iso).upper() in COUNTRY_META:
            iso = str(num_or_iso).upper()
            if return_full_name:
                return COUNTRY_META[iso][0]
            return unicode_flag(iso)
        if return_full_name: return num_or_iso
        return "🌍"
        
    char, detected_iso, eid = get_flag_info_from_num(num_or_iso)
    if return_full_name:
        for code, data in bot_settings.get("premium_flags", {}).items():
            clean = re.sub(r"[^\d]", "", str(num_or_iso))
            if clean.startswith(code): return data.get("name", num_or_iso)
        if detected_iso in COUNTRY_META:
            return COUNTRY_META[detected_iso][0]
        return num_or_iso
        
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
    return char

def mask_number(num):
    clean = num.replace("+", "").replace(" ", "")
    if len(clean) > 6: return f"{clean[:3]}TGZ{clean[-3:]}"
    elif len(clean) > 2: return f"{clean[:1]}TGZ{clean[-1:]}"
    return clean

LANG_MAP = {
    "#EN": "English", "#BN": "Bengali", "#AR": "Arabic", "#HI": "Hindi", 
    "#PA": "Punjabi", "#GU": "Gujarati", "#OR": "Odia", "#TA": "Tamil", 
    "#TE": "Telugu", "#KN": "Kannada", "#ML": "Malayalam", "#SI": "Sinhala", 
    "#TH": "Thai", "#LO": "Lao", "#BO": "Tibetan", "#MY": "Burmese", 
    "#AM": "Amharic", "#KM": "Khmer", "#KA": "Georgian", "#HY": "Armenian", 
    "#HE": "Hebrew", "#EL": "Greek", "#RU": "Russian", "#ZH": "Chinese", 
    "#JA": "Japanese", "#KO": "Korean", "#ID": "Indonesian", "#MS": "Malay", 
    "#VN": "Vietnamese", "#TL": "Filipino", "#ES": "Spanish", "#PT": "Portuguese", 
    "#FR": "French", "#DE": "German", "#IT": "Italian", "#PL": "Polish", 
    "#TR": "Turkish", "#NL": "Dutch", "#SV": "Swedish", "#DA": "Danish", 
    "#NO": "Norwegian", "#FI": "Finnish", "#CS": "Czech", "#SK": "Slovak", 
    "#HU": "Hungarian", "#RO": "Romanian", "#HR": "Croatian", "#BG": "Bulgarian", 
    "#UK": "Ukrainian", "#SW": "Swahili", "#AF": "Afrikaans"
}

# ==========================================
# 🌟 ADVANCED SERVICE & LANGUAGE DETECTION
# ==========================================

SERVICE_SMS_KEYWORDS = {
    # 🟢 Social Media & Chat (Added Arabic Keywords)
    "whatsapp": ["whatsapp", "whatsa", "whatsap", "whats", "whatsapp business", "whatsapp me", "whatsapp code", "whatsap", "واتساب", "واتساپ", "واٹس ایپ", "व्हाट्सएप", "वाट्सएप", "वॉट्सऐप", "व्हाट्सप्प", "হোয়াটসঅ্যাপ", "হোটসঅ্যাপ", "ватсап", "уотсап", "вотсап", "ватс апп", "వాట్సాప్", "വാട്‌സ്ആപ്പ്", "வாட்ஸ்அப்", "ವಾಟ್ಸಾಪ್", "વોટ્સએપ", "ਵਟਸਐਪ", "ହ୍ଵାଟସ୍ ଆପ୍", "වට්ස්ඇප්", "วอตส์แอปป์", "วอทส์แอพ", "ဝက်စ်အက်ပ်", "វ៉តសាប់", "ວອດແອັບ", "ワッツアップ", "왓츠앱", "whatsapp的", "whatsapp验证码", "וואטסאפ", "γουάτσαπ", "ዋትስአፕ", "ვოთსאფი", "վոթսափ"],
    "facebook": ["facebook", "fb", "meta", "fbook", "fb code", "facebook code", "فيسبوك", "فيس بوك"],
    "instagram": ["instagram", "insta", "ig", "ig code", "instagram code", "انستغرام", "انستقرام"],
    "telegram": ["telegram", "tg", "tele", "telegram code", "tg code", "t.me", "تيليجرام", "تليجرام"],
    "tiktok": ["tiktok", "tik tok", "tikvideo", "tiktok code", "tik code", "تيك توك"],
    "snapchat": ["snapchat", "snap", "snap code", "سناب شات"],
    "twitter": ["twitter", "x.com", "x code", "twitter code", "تويتر"],
    "discord": ["discord", "discord code", "ديسكورد"],
    "viber": ["viber", "viber code", "فايبر"],
    "line": ["line", "line code", "line verification", "لاين"],
    "wechat": ["wechat", "we chat", "wechat code", "وي تشات"],
    "signal": ["signal", "signal code", "سيجنال"],
    "linkedin": ["linkedin", "linked in", "لينكد إن"],
    "imo": ["imo", "imo code", "imo verification", "ايمو"],
    "kakaotalk": ["kakao", "kakaotalk", "كاكاو"],
    "qq": ["qq", "tencent qq"],
    "vk": ["vk", "vkontakte"],

    # 🔵 Tech & Mail
    "google": ["google", "gmail", "youtube", "g-", "google voice", "جوجل", "غوغل"],
    "microsoft": ["microsoft", "ms", "outlook", "live.com", "hotmail"],
    "apple": ["apple", "icloud", "itunes", "apple id"],
    "yahoo": ["yahoo", "yahoo code", "ymail"],
    "protonmail": ["proton", "protonmail"],
    
    # 💰 Crypto & Trading
    "binance": ["binance", "bnb", "binances"],
    "coinbase": ["coinbase"],
    "okx": ["okx", "okex"],
    "kucoin": ["kucoin"],
    "bybit": ["bybit"],
    "huobi": ["huobi", "htx"],
    "mexc": ["mexc"],
    "trustwallet": ["trust wallet", "trustwallet"],

    # 💳 Finance & Wallets
    "bkash": ["bkash", "b-kash", "bkash code"],
    "nagad": ["nagad", "nagad code"],
    "rocket": ["rocket", "dutch bangla"],
    "upay": ["upay", "upay code"],
    "paypal": ["paypal", "pay pal"],
    "paytm": ["paytm"],
    "cashapp": ["cash app", "cashapp"],
    "wise": ["wise", "transferwise"],

    # 🛒 E-commerce & Delivery
    "amazon": ["amazon", "amzn", "amazon code"],
    "ebay": ["ebay"],
    "aliexpress": ["aliexpress", "ali express"],
    "alibaba": ["alibaba"],
    "daraz": ["daraz", "daraz code"],
    "foodpanda": ["foodpanda", "food panda"],
    "uber": ["uber", "uber code", "uber verification", "uber eats"],
    "pathao": ["pathao", "pathao ride"],

    # 🎮 Gaming & Entertainment
    "netflix": ["netflix", "netflix code"],
    "spotify": ["spotify", "spotify code"],
    "steam": ["steam", "steam guard"],
    "epicgames": ["epic games", "epicgames"],
    "roblox": ["roblox", "roblox code"],
    "riotgames": ["riot", "riot games", "valorant", "league of legends"],
    "garena": ["garena", "free fire", "freefire"],
    "playstation": ["playstation", "psn"],

    # 🎲 Betting & Casino
    "1xbet": ["1xbet", "1x bet"],
    "melbet": ["melbet", "melbet code"],
    "linebet": ["linebet"],
    "bet365": ["bet365"],
    "megapari": ["megapari"],

    # ❤️ Dating
    "tinder": ["tinder", "tinder code"],
    "bumble": ["bumble"],
    "badoo": ["badoo"]
}

def detect_service(text):
    text_lower = str(text).lower()
    for service_key, keywords in SERVICE_SMS_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return service_key.upper()
    return None

def get_service_info_html(service_text, msg_text=""):
    s = str(service_text).upper().strip()
    m = str(msg_text).lower().strip()
    apps = bot_settings.get("premium_apps", {})
    
    detected_service = s
    if m:
        for service_key, keywords in SERVICE_SMS_KEYWORDS.items():
            for kw in keywords:
                if kw in m:
                    detected_service = service_key.upper()
                    break
            if detected_service != s: break

    clean_s = re.sub(r'[^\w\s]', '', detected_service).strip()
    
    for app_name, data in apps.items():
        if app_name == detected_service or app_name == clean_s or app_name in detected_service or detected_service in app_name:
            full_name = data.get("name", app_name.title())
            char = data.get("char", "📱")
            eid = data.get("id")
            if eid: return full_name, f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
            return full_name, char
            
    if len(detected_service) > 20:
        return "Message", "💬"
        
    return detected_service.title(), "📱"

def detect_language(text):
    if not text: return "#EN"
    text_str = str(text)

    # ১. Unicode Block দিয়ে নিখুঁত বর্ণমালা শনাক্তকরণ (100% Accurate for scripts)
    if any('\u0600' <= c <= '\u06ff' for c in text_str): return "#AR" # Arabic / Persian / Urdu
    if any('\u0980' <= c <= '\u09ff' for c in text_str): return "#BN" # Bengali
    if any('\u0900' <= c <= '\u097f' for c in text_str): return "#HI" # Hindi / Marathi / Nepali
    if any('\u0a00' <= c <= '\u0a7f' for c in text_str): return "#PA" # Punjabi (Gurmukhi)
    if any('\u0a80' <= c <= '\u0aff' for c in text_str): return "#GU" # Gujarati
    if any('\u0b00' <= c <= '\u0b7f' for c in text_str): return "#OR" # Odia
    if any('\u0b80' <= c <= '\u0bff' for c in text_str): return "#TA" # Tamil
    if any('\u0c00' <= c <= '\u0c7f' for c in text_str): return "#TE" # Telugu
    if any('\u0c80' <= c <= '\u0cff' for c in text_str): return "#KN" # Kannada
    if any('\u0d00' <= c <= '\u0d7f' for c in text_str): return "#ML" # Malayalam
    if any('\u0d80' <= c <= '\u0dff' for c in text_str): return "#SI" # Sinhala
    if any('\u0e00' <= c <= '\u0e7f' for c in text_str): return "#TH" # Thai
    if any('\u0e80' <= c <= '\u0eff' for c in text_str): return "#LO" # Lao
    if any('\u0f00' <= c <= '\u0fff' for c in text_str): return "#BO" # Tibetan
    if any('\u1000' <= c <= '\u109f' for c in text_str): return "#MY" # Burmese (Myanmar)
    if any('\u1200' <= c <= '\u137f' for c in text_str): return "#AM" # Amharic (Ethiopic)
    if any('\u1780' <= c <= '\u17ff' for c in text_str): return "#KM" # Khmer
    if any('\u10a0' <= c <= '\u10ff' for c in text_str): return "#KA" # Georgian
    if any('\u0530' <= c <= '\u058f' for c in text_str): return "#HY" # Armenian
    if any('\u0590' <= c <= '\u05ff' for c in text_str): return "#HE" # Hebrew
    if any('\u0370' <= c <= '\u03ff' for c in text_str): return "#EL" # Greek
    if any('\u0400' <= c <= '\u04ff' for c in text_str): return "#RU" # Russian / Ukrainian (Cyrillic)
    if any('\u4e00' <= c <= '\u9fff' for c in text_str): return "#ZH" # Chinese
    if any('\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff' for c in text_str): return "#JA" # Japanese
    if any('\uac00' <= c <= '\ud7af' for c in text_str): return "#KO" # Korean

    # ২. OTP Keyword দিয়ে ভাষা শনাক্তকরণ (Latin script languages)
    text_lower = text_str.lower()
    
    # Asian / Pacific
    if any(w in text_lower for w in ["kode verifikasi", "jangan bagikan", "rahasia"]): return "#ID" # Indonesian
    if any(w in text_lower for w in ["kod pengesahan", "jangan kongsi"]): return "#MS" # Malay
    if any(w in text_lower for w in ["mã của bạn", "không chia sẻ", "mã xác minh"]): return "#VN" # Vietnamese
    if any(w in text_lower for w in ["ang iyong code", "huwag ibahagi"]): return "#TL" # Tagalog / Filipino
    
    # European / Americas
    if any(w in text_lower for w in ["código", "tu código", "verificación", "no compartas"]): return "#ES" # Spanish
    if any(w in text_lower for w in ["seu código", "código de verificação", "não compartilhe"]): return "#PT" # Portuguese
    if any(w in text_lower for w in ["code secret", "ne partagez pas", "votre code"]): return "#FR" # French
    if any(w in text_lower for w in ["dein code", "bestätigungscode", "nicht teilen"]): return "#DE" # German
    if any(w in text_lower for w in ["il tuo codice", "codice di verifica", "non condividere"]): return "#IT" # Italian
    if any(w in text_lower for w in ["twój kod", "nie udostępniaj", "kod weryfikacyjny"]): return "#PL" # Polish
    if any(w in text_lower for w in ["doğrulama kodu", "paylaşmayın", "onay kodu"]): return "#TR" # Turkish
    if any(w in text_lower for w in ["jouw code", "verificatiecode", "niet delen"]): return "#NL" # Dutch
    if any(w in text_lower for w in ["din kod", "verifieringskod", "dela inte"]): return "#SV" # Swedish
    if any(w in text_lower for w in ["bekræftelseskode", "del ikke"]): return "#DA" # Danish
    if any(w in text_lower for w in ["bekreftelseskode", "ikke del"]): return "#NO" # Norwegian
    if any(w in text_lower for w in ["vahvistuskoodi", "älä jaa"]): return "#FI" # Finnish
    if any(w in text_lower for w in ["váš kód", "ověřovací kód", "nesdílejte"]): return "#CS" # Czech
    if any(w in text_lower for w in ["overovací kód", "nezdieľajte"]): return "#SK" # Slovak
    if any(w in text_lower for w in ["ellenőrző kód", "ne oszd meg"]): return "#HU" # Hungarian
    if any(w in text_lower for w in ["codul tău", "codul de verificare", "nu partaja"]): return "#RO" # Romanian
    if any(w in text_lower for w in ["kontrolni kod", "kod za potvrdu", "ne delite"]): return "#HR" # Croatian/Serbian
    if any(w in text_lower for w in ["код за потвърждение", "не споделяйте"]): return "#BG" # Bulgarian
    if any(w in text_lower for w in ["ваш код", "код підтвердження"]): return "#UK" # Ukrainian
    
    # African
    if any(w in text_lower for w in ["msimbo wako", "usishiriki"]): return "#SW" # Swahili
    if any(w in text_lower for w in ["verifikasiekode", "moenie deel nie"]): return "#AF" # Afrikaans
    
    # ৩. উপরের কোনোটি না মিললে ডিফল্ট
    return "#EN"

def parse_chat_id(text):
    text = text.strip()
    if text.startswith("-100") or (text.startswith("-") and text[1:].isdigit()):
        return text
    if "t.me/" in text:
        parts = text.split("/")
        username = parts[-1]
        if username: return "@" + username if not username.startswith("@") else username
    if text.startswith("@"):
        return text
    return "@" + text

def is_admin(user_id):
    return user_id in bot_settings["admins"] or user_id == OWNER_ID

def check_force_join(user_id):
    if not bot_settings["fj_on"] or not bot_settings["fj_channels"]: return True
    if is_admin(user_id): return True
    for ch in bot_settings["fj_channels"]:
        res = api_call("getChatMember", {"chat_id": ch, "user_id": user_id})
        if res.get("ok") and res["result"]["status"] not in ["left", "kicked"]: continue
        else: return False
    return True

def send_force_join_msg(chat_id):
    kb = []
    for ch in bot_settings["fj_channels"]:
        url = f"https://t.me/{ch.replace('@', '')}" if ch.startswith("@") else ch
        kb.append([{"text": f"Join Channel", "icon_custom_emoji_id": "5789428375261023681", "url": url, "style": "primary"}])
    kb.append([{"text": "Check Joined", "icon_custom_emoji_id": "5352694861990501856", "callback_data": "check_fj", "style": "success"}])
    send_message(chat_id, render_body_text(f"{PEM['warn']} <b>Please join our channels to use the bot!</b>"), reply_markup={"inline_keyboard": kb})

def is_user_banned(user_id):
    if is_admin(user_id): return False
    if user_id in user_banned_cache and time.time() - user_banned_cache[user_id]['time'] < 60:
        return user_banned_cache[user_id]['banned']
    banned = False
    if db:
        try:
            doc = db.collection('users').document(str(user_id)).get()
            banned = doc.exists and doc.to_dict().get("banned", False)
        except: pass
    user_banned_cache[user_id] = {'banned': banned, 'time': time.time()}
    return banned

# ==========================================
# Captcha Auto Login & Parsing Core
# ==========================================
def extract_otp_code(text):
    clean_text = re.sub(r'[\u200B-\u200D\uFEFF]', '', str(text))

    # 1. Multi-part OTPs (e.g. 123-456 or 809-761)
    multi_part = re.search(r'(\d{3}[-\s]+\d{3})|(\d{2}[-\s]+\d{2}[-\s]+\d{2})', clean_text)
    if multi_part:
        # হাইফেন (-) থাকলে সেটা রেখে দিবে, কিন্তু স্পেস থাকলে মুছে একসাথে করে দিবে
        return multi_part.group(0).replace(" ", "")

    # 2. Keyword-based extraction
    otp_keywords = ['code', 'is', 'otp', 'pin', 'verification', 'auth', 'কোড', 'رمز', 'your code']
    keywords_pattern = '|'.join(otp_keywords)
    keyword_match = re.search(rf'(?:{keywords_pattern})\s*(?:is|:|-|=)?\s*([a-z0-9]{{4,10}})', clean_text, re.I)
    if keyword_match and keyword_match.group(1).isdigit():
        return keyword_match.group(1)
        
    keyword_match_rev = re.search(rf'([a-z0-9]{{4,10}})\s*(?:is your|is the|কোড)', clean_text, re.I)
    if keyword_match_rev and keyword_match_rev.group(1).isdigit():
        return keyword_match_rev.group(1)

    # 3. Google OTP
    g_match = re.search(r'G-(\d{6})', clean_text, re.IGNORECASE)
    if g_match: return g_match.group(1)

    # 4. Digit sequences fallback
    digit_matches = re.findall(r'(?<!\d)\d{4,8}(?!\d)', clean_text)
    if digit_matches: return digit_matches[0]

    return None

# 🌟 Advanced Automated Background Captcha Solver 🌟
# ==========================================
# Local User Management
# ==========================================
user_cache = {}

def default_user(user_id):
    return {
        "user_id": user_id,
        "balance": 0.0,
        "total_refers": 0,
        "total_otps": 0,
        "banned": False,
        "verified": False,
        "display_name": "User",
        "api_enabled": False,      # API is OFF until the admin enables it
    }

def get_user(user_id):
    if user_id in user_cache: return user_cache[user_id]
    fallback = default_user(user_id)
    doc_ref = db.collection('users').document(str(user_id))
    try:
        doc = doc_ref.get()
        if doc.exists:
            data = {**fallback, **(doc.to_dict() or {})}
            user_cache[user_id] = data
            return data
        doc_ref.set(fallback)
    except Exception as e:
        print(f"Local user read/write deferred for {user_id}: {e}")
    user_cache[user_id] = fallback
    return fallback

_counter_lock = threading.Lock()

def bd_today():
    # "Today" resets at 12:00 AM Bangladesh time (UTC+6)
    return (datetime.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d")

def bump_otp_counters(uid):
    """+1 to total / weekly / today OTP counters (today resets itself daily)."""
    today = bd_today()
    try:
        with _counter_lock:
            u = get_user(uid)
            ref = db.collection('users').document(str(uid))
            if u.get("today_date") != today:
                u["today_date"], u["today_otps"] = today, 0
                ref.update({"today_date": today, "today_otps": 0})
            ref.update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1),
                        "today_otps": local_ops.Increment(1)})
            u["total_otps"] = u.get("total_otps", 0) + 1
            u["weekly_otps"] = u.get("weekly_otps", 0) + 1
            u["today_otps"] = u.get("today_otps", 0) + 1
    except Exception as e:
        print(f"OTP counter error for {uid}: {e}")

def sync_user_display_name(chat_id, msg):
    # 🌟 Keeps each user's display name up to date (used by the public Leader Board).
    # Only writes when the name actually changes.
    try:
        frm = msg.get("from", {})
        full_name = f"{frm.get('first_name', '').strip()} {frm.get('last_name', '').strip()}".strip() or "User"
        u_data = get_user(chat_id)
        if u_data.get("display_name") != full_name:
            u_data["display_name"] = full_name
            user_cache[chat_id] = u_data
            try: db.collection('users').document(str(chat_id)).set({"display_name": full_name}, merge=True)
            except: pass
    except: pass

def update_balance(user_id, amount):
    user = get_user(user_id)
    user["balance"] = user.get("balance", 0.0) + float(amount)
    try:
        doc_ref = db.collection('users').document(str(user_id))
        doc_ref.set({"user_id": user_id, "balance": local_ops.Increment(float(amount))}, merge=True)
    except: pass

def add_referral(inviter_id, new_user_id):
    if not get_user(new_user_id).get("ref_paid", False):
        get_user(new_user_id) 
        reward = bot_settings.get("refer_reward", 0.2)
        update_balance(inviter_id, reward)
        db.collection('users').document(str(inviter_id)).update({"total_refers": local_ops.Increment(1)})
        if inviter_id in user_cache:
            user_cache[inviter_id]["total_refers"] = user_cache[inviter_id].get("total_refers", 0) + 1
        get_user(new_user_id)["ref_paid"] = True
        db.collection('users').document(str(new_user_id)).set({"ref_paid": True}, merge=True)
        
        # আপনার দেওয়া নতুন ডিজাইন
        ref_msg = (
            f"{PEM['gift']} <b>New Referral !</b>\n"
            f"------------------\n"
            f"🔥 <b>You Received {reward} TK</b>\n"
            f"------------------\n"
            f"{PEM['user']} <b>From User ID:</b> <code>{new_user_id}</code>"
        )
        send_message(inviter_id, render_body_text(ref_msg))

# ==========================================
# UI Keyboards & Menu Builders
# ==========================================
def get_cancel_kb():
    return {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_state", "style": "danger"}]]}

def main_menu(user_id):
    kb = [
        [
            {"text": "GET NUMBER", "icon_custom_emoji_id": "5337132498965010628", "style": "primary"}, 
            {"text": "2FA ONLINE", "icon_custom_emoji_id": "5267421176841398765", "style": "primary"}
        ],
        [
            {"text": "Refer", "icon_custom_emoji_id": "5420396762189831222", "style": "success"}, 
            {"text": "WITHDRAWAL", "icon_custom_emoji_id": "5352585194295564660", "style": "danger"}
        ],
        [
            {"text": "Leader Board", "icon_custom_emoji_id": "5440539497383087970", "style": "success"},
            {"text": "SUPPORT", "icon_custom_emoji_id": "5420145051336485498", "style": "primary"}
        ]
    ]
    # 🌟 FIX: STATUS is now visible to every user, not just admins - it shows a
    # rate card of the configured services, each with its country + OTP rate.
    # Admin Panel stays admin-only.
    kb.append([{"text": "API DOCUMENT", "icon_custom_emoji_id": "5197288647275071607", "style": "primary"}])
    if is_admin(user_id):
        kb.append([{"text": "STATUS", "icon_custom_emoji_id": "5352877703043258544", "style": "success"}, {"text": "Admin Panel", "icon_custom_emoji_id": "5420155432272438703", "style": "danger"}])
    else:
        kb.append([{"text": "STATUS", "icon_custom_emoji_id": "5352877703043258544", "style": "success"}])
    return {"keyboard": kb, "resize_keyboard": True}

def get_admin_text():
    users_count = len(all_known_users) # 🌟 Zero Cost User Count!
    total_files = len(number_batches)
    available_nums = sum(len(b["numbers"]) for b in number_batches.values())

    txt = f"""
{PEM['admin']} <b>ADMIN CONTROL PANEL</b> {PEM['admin']}
━━━━━━━━━━━━━━━━━━

{PEM['graph']} <b>DATABASE OVERVIEW</b>
— — — — — — — — — —
{PEM['user']} Users      » {users_count}
{PEM['file']} Files      » {total_files}
{PEM['num']} Numbers    » {total_uploaded_stats}
{PEM['ok']} Assigned   » {total_assigned_stats}
{PEM['rocket']} Available  » {available_nums}

{PEM['graph']} <b>STOCK LEVEL</b>
— — — — — — — — — —
[██████░░░░░░░░░] {available_nums} free
"""
    return render_body_text(txt)

def _admin_panel_keyboard_full():
    return {"inline_keyboard": [
        [{"text": "LEADER BOARD SYSTEM", "icon_custom_emoji_id": "5353032893096567467", "callback_data": "lb_main", "style": "success"}],
        [{"text": "Upload Number", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "upload_num", "style": "primary"},
         {"text": "Delete files", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "delete_files", "style": "danger"}],
        [{"text": "Backup", "icon_custom_emoji_id": "5352597830089347330", "callback_data": "backup_menu", "style": "success"},
         {"text": "API Users", "icon_custom_emoji_id": "5197288647275071607", "callback_data": "ul_p_0", "style": "primary"}],
        [{"text": "Broadcast", "icon_custom_emoji_id": "5789428375261023681", "callback_data": "broadcast_msg", "style": "success"},
         {"text": "System", "icon_custom_emoji_id": "5420155432272438703", "callback_data": "system_settings", "style": "primary"}],
        [{"text": "Used number", "icon_custom_emoji_id": "5352694861990501856", "callback_data": "show_used", "style": "success"},
         {"text": "Unused number", "icon_custom_emoji_id": "5352597830089347330", "callback_data": "show_unused", "style": "success"}],
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]
    ]}

def _system_settings_keyboard_full():
    return {"inline_keyboard": [
        [{"text": "StexSMS Control", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "stex_control", "style": "success"}],
        [{"text": "Force Join System", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "manage_fj", "style": "primary"},
         {"text": "Admin Management", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "manage_admins", "style": "danger"}],
        [{"text": "OTP Group", "icon_custom_emoji_id": "5190447043545438788", "callback_data": "manage_otp_groups", "style": "danger"},
         {"text": "User Management", "icon_custom_emoji_id": "5193063022226086560", "callback_data": "user_management", "style": "primary"}], 
        [{"text": "TGZ Control", "icon_custom_emoji_id": "5193100774988617665", "callback_data": "TGZ_control", "style": "primary"},
         {"text": "Premium Emoji", "icon_custom_emoji_id": "5352552689983067014", "callback_data": "manage_emojis", "style": "success"}],
        [{"text": "Menu Design", "icon_custom_emoji_id": "5190751148704833975", "callback_data": "menu_design_list", "style": "primary"},
         {"text": "Test", "icon_custom_emoji_id": "5190781475468915802", "callback_data": "test_message_flow", "style": "primary"}], 
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
    ]}

# Buttons a CHILD bot's admin must never see / use
CHILD_BLOCKED_CB = (
    "upload_num", "delete_files", "del_b_", "show_used", "show_unused", "dl_used", "dl_unused",
    "api_rng_", "stex_control", "add_stex_key", "view_stex_keys", "del_nxa_", "manage_stex_srv",
    "nx_", "manage_emojis", "up_flags_txt", "up_apps_txt", "add_single_emoji", "del_all_flags",
    "dl_flags_txt", "dl_apps_txt", "bots_",
)

def _filter_child_kb(kb):
    if not IS_CHILD:
        return kb
    rows = []
    for row in kb["inline_keyboard"]:
        row = [b for b in row if not str(b.get("callback_data", "")).startswith(CHILD_BLOCKED_CB)]
        if row:
            rows.append(row)
    return {"inline_keyboard": rows}

def admin_panel_keyboard():
    kb = _admin_panel_keyboard_full()
    if not IS_CHILD:
        kb["inline_keyboard"].insert(-1, [{"text": "Add Bot", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "bots_main", "style": "success"}])
    return _filter_child_kb(kb)

def system_settings_keyboard():
    return _filter_child_kb(_system_settings_keyboard_full())

def get_user_management_text():
    # 🌟 Fast & Free User Management Stats!
    total = len(all_known_users)
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    txt = f"""➖➖➖➖➖➖➖➖
《 👋 USER VIEW 》
➖➖➖➖➖➖➖➖
📊 LIVE STATISTICS:
➖➖➖➖➖➖➖➖
🫂 TOTAL USERS: {total}
✅ VERIFIED USERS: (Hidden to save DB Cost)
🚫 BANNED USERS: (Hidden to save DB Cost)
➖➖➖➖➖➖➖➖
⌛ UPDATED: {now_str}"""
    return render_body_text(txt)

def user_management_keyboard():
    return {"inline_keyboard": [
        [{"text": "Manage Balance", "icon_custom_emoji_id": "5190576863226933563", "callback_data": "um_manage_balance", "style": "primary"},
         {"text": "Ban/Unban User", "icon_custom_emoji_id": "5334807341109908955", "callback_data": "um_ban_unban", "style": "danger"}],
        [{"text": "User Profile", "icon_custom_emoji_id": "5352861489541714456", "callback_data": "um_user_profile", "style": "success"},
         {"text": "User List / API", "icon_custom_emoji_id": "5197288647275071607", "callback_data": "ul_p_0", "style": "primary"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def menu_design_list_keyboard():
    return {"inline_keyboard": [
        [{"text": "Edit /start Menu", "icon_custom_emoji_id": "5395444784611480792", "callback_data": "md_edit_start", "style": "primary"}],
        [{"text": "Edit GET NUMBER", "icon_custom_emoji_id": "5337132498965010628", "callback_data": "md_edit_get_number", "style": "success"},
         {"text": "Edit Select Country", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "md_edit_select_country", "style": "primary"}],
        [{"text": "Edit Refer", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "md_edit_refer", "style": "primary"}],
        [{"text": "Edit WITHDRAWAL", "icon_custom_emoji_id": "5352585194295564660", "callback_data": "md_edit_withdrawal", "style": "danger"},
         {"text": "Edit SUPPORT", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "md_edit_support", "style": "danger"}],
        [{"text": "Reset Defaults", "icon_custom_emoji_id": "5192812028632274956", "callback_data": "md_reset_defaults", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def menu_edit_options_keyboard(menu_key):
    return {"inline_keyboard": [
        [{"text": "Edit Body (Text)", "icon_custom_emoji_id": "5395444784611480792", "callback_data": f"md_text_{menu_key}", "style": "primary"}],
        [{"text": "Edit Inline Buttons", "icon_custom_emoji_id": "5420155432272438703", "callback_data": f"md_btns_{menu_key}", "style": "success"}],
        [{"text": "Back to Menus", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "menu_design_list", "style": "danger"}]
    ]}

def menu_buttons_list_keyboard(menu_key):
    kb = []
    btns = bot_settings["custom_messages"].get(menu_key, {}).get("buttons", [])
    for idx, btn in enumerate(btns):
        kb.append([{"text": f"Del: {btn['text']}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"md_delbtn_{menu_key}_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Inline Button", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"md_addbtn_{menu_key}", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"md_edit_{menu_key}", "style": "primary"}])
    return {"inline_keyboard": kb}

def emoji_settings_keyboard():
    return {"inline_keyboard": [
        [{"text": "Upload Flags (TXT)", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "up_flags_txt", "style": "primary"},
         {"text": "Download Flags", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_flags_txt", "style": "success"}],
        [{"text": "Upload Services (TXT)", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "up_apps_txt", "style": "primary"},
         {"text": "Download Services", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_apps_txt", "style": "success"}],
        [{"text": "Delete All Flags", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "del_all_flags", "style": "danger"},
         {"text": "Add Single Emoji", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_single_emoji", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]
    ]}

def status_services_keyboard():
    kb = []
    for idx, srv in enumerate(bot_settings.get("status_services", [])):
        kb.append([{"text": f"Delete {srv}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"status_del_srv_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "status_add_srv", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}])
    return {"inline_keyboard": kb}

def collect_status_rows(service_name):
    """For one watched service, find every country + OTP rate configured for
    it across all four panels (StexSMS/Voltx/Zebrasms/Yesms), deduplicated."""
    srv_upper = service_name.strip().upper()
    rows = []
    seen_countries = set()
    for provider in ("stex",):
        countries = bot_settings.get(f"{provider}_services", {}).get(srv_upper, {})
        rates = bot_settings.get(f"{provider}_service_rates", {}).get(srv_upper, {})
        for country in countries:
            if country in seen_countries:
                continue
            seen_countries.add(country)
            rate = bot_settings.get("otp_reward", 0.0) if IS_CHILD else rates.get(country, bot_settings.get("otp_reward", 0.0))
            rows.append((country, rate))
    return rows

def get_all_configured_services():
    """Union of every service name that has country/rate data configured in
    any of the four panels (StexSMS/Voltx/Zebrasms/Yesms)."""
    services = set()
    for provider in ("stex",):
        services.update(bot_settings.get(f"{provider}_services", {}).keys())
    return services

def build_status_report_text():
    apps = bot_settings.get("premium_apps", {})
    # 🌟 FIX: previously ONLY services manually added via "STATUS Services"
    # were ever shown, so a service that was fully configured with country +
    # OTP rate in the panels but never separately whitelisted here simply
    # never appeared. Now the curated list (admin-chosen order) is shown
    # first, and any other configured service is added automatically.
    curated = bot_settings.get("status_services", [])
    seen = set()
    services = []
    for srv in curated:
        key = srv.strip().upper()
        if key not in seen:
            seen.add(key)
            services.append(srv)
    for key in sorted(get_all_configured_services()):
        if key not in seen:
            seen.add(key)
            services.append(key)
    blocks = []
    for srv in services:
        srv_key = srv.strip().upper()
        app_data = apps.get(srv_key, {})
        icon = app_data.get("char", "📱")
        eid = app_data.get("id")
        app_html = f'<tg-emoji emoji-id="{eid}">{icon}</tg-emoji>' if eid else icon
        rows = collect_status_rows(srv)
        if not rows:
            continue
        lines = [f"{app_html} <b>{html.escape(srv)}</b> \""]
        for country, rate in rows:
            flag_char, flag_id = get_country_flag_info(country)
            flag_html = f'<tg-emoji emoji-id="{flag_id}">{flag_char}</tg-emoji>' if flag_id else flag_char
            cname = country.split(" (")[0].upper()
            lines.append(f"├ {flag_html} {html.escape(cname)} — {rate}৳")
        blocks.append("\n".join(lines))
    if not blocks:
        body = "└ <i>No services added yet.</i>"
    else:
        body = "\n━━━━━━━━━━━━━━━\n".join(blocks)
    date_str = datetime.now().strftime("%m/%d/%Y")
    return f"{body}\n━━━━━━━━━━━━━━━\n📅 Date: {date_str}"

def status_kb():
    return {"inline_keyboard": [
        [{"text": "Refresh", "icon_custom_emoji_id": "5375338737028841420", "callback_data": "status_refresh", "style": "primary"}],
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "status_close", "style": "danger"}]
    ]}

def fj_settings_keyboard():
    status_text = 'ON' if bot_settings['fj_on'] else 'OFF'
    status_icon = "5352694861990501856" if bot_settings['fj_on'] else "5318840353510408444"
    kb = [[{"text": f"STATUS: {status_text}", "icon_custom_emoji_id": status_icon, "callback_data": "toggle_fj", "style": "primary"}]]
    for idx, ch in enumerate(bot_settings["fj_channels"]):
        kb.append([{"text": f"Delete: {ch}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_fj_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Channel", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_fj", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}])
    return {"inline_keyboard": kb}

def admin_settings_keyboard():
    kb = []
    for idx, adm in enumerate(bot_settings["admins"]):
        text_btn = f"Owner: {adm}" if adm == OWNER_ID else f"Delete: {adm}"
        icon_id = "5353032893096567467" if adm == OWNER_ID else "5420130255174145507"
        cb_data = "ignore" if adm == OWNER_ID else f"del_adm_{idx}"
        kb.append([{"text": text_btn, "icon_custom_emoji_id": icon_id, "callback_data": cb_data, "style": "danger" if adm != OWNER_ID else "primary"}])
    kb.append([{"text": "Add Admin", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_adm", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}])
    return {"inline_keyboard": kb}

def otp_groups_list_keyboard():
    kb = [[{"text": "Edit OTP Button Link", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "edit_otp_link", "style": "primary"}]]
    for idx, fg in enumerate(bot_settings["fw_groups"]):
        kb.append([{"text": f"Group: {fg['chat_id']}", "icon_custom_emoji_id": "5193063022226086560", "callback_data": f"manage_fw_{idx}", "style": "primary"}])
    kb.append([{"text": "Add Forward Group", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_fw", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}])
    return {"inline_keyboard": kb}

def stex_control_keyboard():
    return {"inline_keyboard": [
        [{"text": "Add StexSMS Key", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_stex_key", "style": "success"},
         {"text": "View/Del Keys", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "view_stex_keys", "style": "danger"}],
        [{"text": "Manage StexSMS Services", "icon_custom_emoji_id": "5192739271886282680", "callback_data": "manage_stex_srv", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def specific_fw_group_keyboard(idx):
    group = bot_settings["fw_groups"][idx]
    kb = []
    for b_idx, btn in enumerate(group.get("buttons", [])):
        kb.append([{"text": f"Del: {btn['text']}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_fwbtn_{idx}_{b_idx}", "style": "danger"}])
    
    kb.append([{"text": "Add Inline Button", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"add_fwbtn_{idx}", "style": "success"}])
    kb.append([{"text": "Delete Entire Group", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"del_fw_{idx}", "style": "danger"}])
    kb.append([{"text": "Back to Groups", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_otp_groups", "style": "primary"}])
    return {"inline_keyboard": kb}

def TGZ_control_keyboard():
    w_status = "ON" if bot_settings["withdraw_on"] else "OFF"
    sup_status = "ON" if bot_settings.get("support_link") else "OFF"
    grp_status = "ON" if bot_settings.get("w_group") else "OFF"
    return {"inline_keyboard": [
        [{"text": f"WITHDRAW: {w_status}", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "TGZ_toggle_w", "style": "primary"}],
        [{"text": f"MIN WITHDRAW: {bot_settings['min_withdraw']}", "icon_custom_emoji_id": "5352877703043258544", "callback_data": "TGZ_min_w", "style": "success"},
         {"text": f"OTP REWARD: {bot_settings['otp_reward']}", "icon_custom_emoji_id": "5190576863226933563", "callback_data": "TGZ_otp_r", "style": "primary"}],
        [{"text": f"REFER REWARD: {bot_settings['refer_reward']}", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "TGZ_ref_r", "style": "success"},
         {"text": f"COOLDOWN: {bot_settings['cooldown']}s", "icon_custom_emoji_id": "5337172996211648018", "callback_data": "TGZ_cool", "style": "primary"}],
        [{"text": f"NUM/REQ: {bot_settings['num_req']}", "icon_custom_emoji_id": "5337132498965010628", "callback_data": "TGZ_num_req", "style": "success"},
         {"text": f"NUM/SHARE: {bot_settings['num_share']}", "icon_custom_emoji_id": "5352862640592949843", "callback_data": "TGZ_num_share", "style": "primary"}],
        [{"text": f"SUPPORT LINK: {sup_status}", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "TGZ_sup_link", "style": "success"},
         {"text": "W. METHODS", "icon_custom_emoji_id": "5190899075968441286", "callback_data": "manage_w_methods", "style": "primary"}],
        [{"text": f"W. GROUP: {grp_status}", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "TGZ_w_group", "style": "success"},
         {"text": f"API URL: {'SET' if bot_settings.get('api_base_url') else 'AUTO'}", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "TGZ_api_url", "style": "primary"}],
        [{"text": "BACK", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]
    ]}

def w_methods_keyboard():
    kb = []
    for idx, m in enumerate(bot_settings["w_methods"]):
        kb.append([{"text": f"Delete: {m}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_wm_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Method", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_wm", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "TGZ_control", "style": "primary"}])
    return {"inline_keyboard": kb}

# ==========================================
# Message Handler
# ==========================================
def handle_message(msg):
    global total_uploaded_stats
    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"].get("type", "private")
    
    if chat_type != "private":
        return
        
    text = msg.get("text", "")
    register_user_local(chat_id) # 🌟 Save User locally for Free Broadcasts!
    sync_user_display_name(chat_id, msg) # 🌟 Keep name fresh for the Leader Board

    if is_user_banned(chat_id):
        send_message(chat_id, render_body_text("🚫 <b>You are banned from using this bot!</b>\nIf you think this is a mistake, please contact support."))
        return
    
    # --- REFERRAL FIX: Save inviter BEFORE Force Join ---
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            inviter = int(parts[1])
            if inviter != chat_id:
                if db:
                    try:
                        doc = db.collection('users').document(str(chat_id)).get()
                        if not doc.exists:
                            get_user(chat_id)
                            db.collection('users').document(str(chat_id)).set({"referred_by": inviter, "ref_paid": False}, merge=True)
                    except Exception as e:
                            print(f"Local referral setup deferred for {chat_id}: {e}")
                        
    if not check_force_join(chat_id):
        send_force_join_msg(chat_id)
        return
        
    MAIN_MENU_CMDS = ["GET NUMBER", "Refer", "WITHDRAWAL", "SUPPORT", "Admin Panel", "2FA ONLINE", "STATUS", "Leader Board", "API DOCUMENT"]
    
    is_main_cmd = False
    if text in MAIN_MENU_CMDS or text.startswith("/start"):
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        is_main_cmd = True
    
    if chat_id in user_states and not is_main_cmd:
        state = user_states[chat_id]
        
        # --- User Management Flows ---
        if state == "wait_for_um_bal_uid" and text:
            target_uid_str = text.strip()
            if not target_uid_str.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID! Please send a numeric User ID."), reply_markup=get_cancel_kb())
                return
            target_uid = int(target_uid_str)
            if db:
                doc = db.collection('users').document(str(target_uid)).get()
                if not doc.exists:
                    send_message(chat_id, render_body_text("❌ User not found in database!"), reply_markup=get_cancel_kb())
                    return
                current_bal = doc.to_dict().get('balance', 0.0)
                temp_data[chat_id]["target_uid"] = target_uid
                user_states[chat_id] = "wait_for_um_bal_amt"
                send_message(chat_id, render_body_text(f"✅ User found!\n💰 Current Balance: {current_bal} ৳\n\n📝 Send the amount to ADD (e.g. 50) or REMOVE (e.g. -50):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_um_bal_amt" and text:
            try:
                amt = float(text.strip())
                target_uid = temp_data[chat_id]["target_uid"]
                update_balance(target_uid, amt)
                send_message(chat_id, render_body_text(f"{PEM['ok']} Balance updated successfully for {target_uid}!"), reply_markup=main_menu(chat_id))
                send_message(target_uid, render_body_text(f"🔔 Your balance has been adjusted by <b>{amt} ৳</b> by an Admin."))
                del user_states[chat_id]
                del temp_data[chat_id]
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid amount! Please send a number."), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_um_ban_uid" and text:
            target_uid_str = text.strip()
            if not target_uid_str.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID!"), reply_markup=get_cancel_kb())
                return
            target_uid = int(target_uid_str)
            if db:
                doc_ref = db.collection('users').document(str(target_uid))
                doc = doc_ref.get()
                if not doc.exists:
                    send_message(chat_id, render_body_text("❌ User not found in database!"), reply_markup=get_cancel_kb())
                    return
                current_status = doc.to_dict().get("banned", False)
                new_status = not current_status
                doc_ref.update({"banned": new_status})
                
                user_banned_cache[target_uid] = {'banned': new_status, 'time': time.time()}
                
                status_text = "BANNED 🚫" if new_status else "UNBANNED ✅"
                send_message(chat_id, render_body_text(f"✅ User {target_uid} has been {status_text}!"), reply_markup=main_menu(chat_id))
                del user_states[chat_id]
                del temp_data[chat_id]
            return

        elif state == "wait_ul_find" and text:
            t_uid = text.strip()
            if not t_uid.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID!"), reply_markup=get_cancel_kb())
                return
            vt, vk = api_users_view(0, only_uid=t_uid)
            send_message(chat_id, vt, reply_markup=vk)
            user_states.pop(chat_id, None)
            temp_data.pop(chat_id, None)
            return

        elif state == "wait_for_um_prof_uid" and text:
            target_uid_str = text.strip()
            if not target_uid_str.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID!"), reply_markup=get_cancel_kb())
                return
            target_uid = int(target_uid_str)
            if db:
                doc = db.collection('users').document(str(target_uid)).get()
                if not doc.exists:
                    send_message(chat_id, render_body_text("❌ User not found in database!"), reply_markup=get_cancel_kb())
                    return
                data = doc.to_dict()
                is_verified = True if data.get('total_otps', 0) > 0 else data.get('verified', False)
                prof_text = f"""➖➖➖➖➖➖➖➖
👤 <b>USER PROFILE</b>
➖➖➖➖➖➖➖➖
🆔 ID: <code>{target_uid}</code>
💰 Balance: {data.get('balance', 0.0)} ৳
🤝 Total Refers: {data.get('total_refers', 0)}
🔐 Total OTPs: {data.get('total_otps', 0)}
✅ Verified: {is_verified}
🚫 Banned: {data.get('banned', False)}
🔌 API: {'Enabled ✅' if data.get('api_enabled') else 'Disabled 🚫'}
➖➖➖➖➖➖➖➖"""
                kb = {"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "user_management", "style": "primary"}]]}
                send_message(chat_id, render_body_text(prof_text), reply_markup=kb)
                del user_states[chat_id]
                del temp_data[chat_id]
            return

        # --- Menu Design Flow ---
        elif state == "wait_for_menu_text" and text:
            try:
                menu_key = temp_data[chat_id]["menu_key"]
                formatted_html_text = extract_premium_html(msg)
                
                bot_settings["custom_messages"][menu_key]["text"] = formatted_html_text
                save_db()
                
                delete_message(chat_id, msg["message_id"])
                
                preview_text = render_body_text(formatted_html_text)
                success_text = f"{PEM['ok']} <b>Message Body Updated successfully!</b>\n\n🎨 <b>Editing: {menu_key.upper()}</b>\n\nPreview of current Text:\n{preview_text}"
                edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(success_text), reply_markup=menu_edit_options_keyboard(menu_key))
            except Exception as e:
                send_message(chat_id, f"❌ Error saving text: {e}")
            finally:
                if chat_id in user_states: del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
            return
            
        elif state == "wait_for_menu_btn" and text:
            try:
                menu_key = temp_data[chat_id]["menu_key"]
                if "-" in text:
                    parts = text.split("-", 1)
                    btn_text = parts[0].strip()
                    btn_url = parts[1].strip()
                    
                    emoji_id = None
                    emoji_char = ""
                    for ent in msg.get("entities", []):
                        if ent.get("type") == "custom_emoji":
                            emoji_id = ent.get("custom_emoji_id")
                            offset = ent.get("offset", 0)
                            length = ent.get("length", 0)
                            b_text = text.encode('utf-16-le')
                            emoji_char = b_text[offset*2:(offset+length)*2].decode('utf-16-le')
                            break
                            
                    if emoji_char:
                        btn_text = btn_text.replace(emoji_char, "").strip()
                        
                    btn_data = {"text": btn_text, "url": btn_url, "style": "primary"}
                    if emoji_id:
                        btn_data["icon_custom_emoji_id"] = emoji_id
                        
                    bot_settings["custom_messages"][menu_key]["buttons"].append(btn_data)
                    save_db()
                    delete_message(chat_id, msg["message_id"])
                    edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"{PEM['gear']} <b>Edit Inline Buttons: {menu_key.upper()}</b>"), reply_markup=menu_buttons_list_keyboard(menu_key))
                else:
                    send_message(chat_id, render_body_text(f"{PEM['no']} Invalid format. Use <code>Button Text - https://link.com</code>"))
            except Exception as e:
                 pass
            finally:
                if chat_id in user_states: del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
            return

        elif state == "wait_for_test_service" and text:
            temp_data[chat_id]["service"] = text.strip()
            user_states[chat_id] = "wait_for_test_number"
            send_message(chat_id, render_body_text("📝 Send the Number (e.g. +8801712345678):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_test_number" and text:
            temp_data[chat_id]["number"] = text.strip()
            user_states[chat_id] = "wait_for_test_otp"
            send_message(chat_id, render_body_text("📝 Send the OTP (e.g. 556677):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_test_otp" and text:
            temp_data[chat_id]["otp"] = text.strip()
            user_states[chat_id] = "wait_for_test_lang"
            send_message(chat_id, render_body_text("📝 Send the Language (e.g. EN, AR):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_test_lang" and text:
            lang = text.strip().upper()
            if not lang.startswith("#"):
                lang = "#" + lang
                
            srv = temp_data[chat_id]["service"]
            num = temp_data[chat_id]["number"]
            otp = temp_data[chat_id]["otp"]
            
            masked = mask_number(num)
            prem_flag_html = get_flag_info_html(num)
            char, iso = get_flag_and_code(num)
            app_full_name, prem_app_html = get_service_info_html(srv)
            
            lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
            msg_text = render_body_text(f"{prem_flag_html} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
            
            for fw in bot_settings.get("fw_groups", []):
                kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                temp_row = []
                styles = ["danger", "success", "primary"]
                for i, btn in enumerate(fw.get("buttons", [])):
                    b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                    if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                    temp_row.append(b_obj)
                    if len(temp_row) == 2:
                        kb.append(temp_row)
                        temp_row = []
                if temp_row: kb.append(temp_row)
                send_message(fw["chat_id"], msg_text, reply_markup={"inline_keyboard": kb})
                
            send_message(chat_id, render_body_text(f"{PEM['ok']} Test message formatted and sent to all Forward Groups!"), reply_markup=main_menu(chat_id))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_emoji_extract":
            entities = msg.get("entities", [])
            custom_emoji_id = None
            emoji_text = ""
            for ent in entities:
                if ent.get("type") == "custom_emoji":
                    custom_emoji_id = ent.get("custom_emoji_id")
                    offset = ent.get("offset", 0)
                    length = ent.get("length", 0)
                    b_text = msg.get("text", "").encode('utf-16-le')
                    emoji_text = b_text[offset*2:(offset+length)*2].decode('utf-16-le')
                    break
            
            if custom_emoji_id:
                temp_data[chat_id] = {"id": custom_emoji_id, "char": emoji_text}
                user_states[chat_id] = "wait_for_emoji_details"
                send_message(chat_id, render_body_text(f"{PEM['ok']} Emoji ID পাওয়া গেছে: <code>{custom_emoji_id}</code>\n\n📌 এখন এটি সেভ করার জন্য টাইপ এবং নাম লিখুন।\n\n<b>ফরমেট:</b>\n`FLAG | 880 | BD | Bangladesh`\nঅথবা\n`APP | WhatsApp`"), reply_markup=get_cancel_kb())
            else:
                send_message(chat_id, render_body_text(f"{PEM['no']} কোনো Premium Emoji পাওয়া যায়নি! দয়া করে Custom Emoji সেন্ড করুন।"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_emoji_details" and text:
            parts = [p.strip() for p in text.split("|")]
            mode = parts[0].upper()
            eid = temp_data[chat_id]["id"]
            char = temp_data[chat_id]["char"]
            
            if mode == "FLAG" and len(parts) == 4:
                code, iso, name = parts[1], parts[2], parts[3]
                bot_settings["premium_flags"][code] = {"char": char, "iso": iso.upper(), "name": name, "id": eid}
                save_db()
                send_message(chat_id, render_body_text(f"{PEM['ok']} Flag Emoji সেভ হয়েছে!\nCode: {code} | Name: {name}"), reply_markup=emoji_settings_keyboard())
            elif mode == "APP" and len(parts) == 2:
                name = parts[1]
                bot_settings["premium_apps"][name.upper()] = {"char": char, "id": eid, "name": name.title()}
                save_db()
                send_message(chat_id, render_body_text(f"{PEM['ok']} App Emoji সেভ হয়েছে!\nName: {name}"), reply_markup=emoji_settings_keyboard())
            else:
                send_message(chat_id, render_body_text(f"{PEM['no']} ফরম্যাট ভুল!\n\nসঠিক ফরম্যাট:\n`FLAG | 880 | BD | Bangladesh`\n`APP | WhatsApp`"))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state in ["wait_for_flag_txt", "wait_for_app_txt"] and "document" in msg:
            doc = msg["document"]
            if not doc["file_name"].endswith(".txt"):
                send_message(chat_id, render_body_text(f"{PEM['no']} Please upload a .txt file only."))
                return
            file_id = doc["file_id"]
            file_info = requests.get(
                f"{BASE_URL}/getFile?file_id={file_id}", timeout=(10, 20)
            ).json()
            file_path = file_info["result"]["file_path"]
            content = requests.get(f"{FILE_URL}{file_path}", timeout=(10, 30)).text
            
            mode = "flags" if state == "wait_for_flag_txt" else "apps"
            count = 0
            
            if mode == "flags":
                for line in content.splitlines():
                    json_match = re.search(r'(\{.*\})', line)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                            char = data.get("emoji")
                            eid = data.get("id")
                            
                            prefix_str = line[:json_match.start()].strip()
                            code_match = re.search(r'\((\d+)\)', prefix_str)
                            iso_match = re.search(r'\(([A-Za-z]+)\)', prefix_str)
                            
                            if code_match and iso_match and char and eid:
                                code = code_match.group(1)
                                iso = iso_match.group(1).upper()
                                name = prefix_str.replace(f"({code})", "").replace(f"({iso_match.group(1)})", "").replace(char, "").strip()
                                bot_settings["premium_flags"][code] = {"char": char, "iso": iso, "name": name, "id": eid}
                                count += 1
                        except: pass
            else:
                for line in content.splitlines():
                    json_match = re.search(r'(\{.*\})', line)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                            char = data.get("emoji")
                            eid = data.get("id")
                            
                            name_part = line[:json_match.start()].strip()
                            name = name_part.replace(char, '').strip() if char else name_part
                            
                            if char and eid and name:
                                bot_settings["premium_apps"][name.upper()] = {"char": char, "id": eid, "name": name}
                                count += 1
                        except: pass
            
            save_db()
            send_message(chat_id, render_body_text(f"{PEM['ok']} Successfully loaded {count} Emojis!"), reply_markup=emoji_settings_keyboard())
            del user_states[chat_id]
            return

        elif state == "wait_addbot_token" and text and chat_id == OWNER_ID and not IS_CHILD:
            tok = text.strip()
            delete_message(chat_id, msg["message_id"])
            mid = temp_data[chat_id]["msg_id"]
            if tok == TOKEN or any(b.get("token") == tok for b in bot_settings.get("child_bots", {}).values()):
                edit_message(chat_id, mid, render_body_text("❌ এই Bot Token আগেই যোগ করা আছে!\n\n📝 অন্য Bot Token দিন:"), reply_markup=bots_cancel_kb())
                return
            try:
                r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
            except Exception:
                r = {}
            if not r.get("ok"):
                edit_message(chat_id, mid, render_body_text("❌ Invalid Bot Token!\n\n📝 সঠিক Bot Token দিন:"), reply_markup=bots_cancel_kb())
                return
            temp_data[chat_id].update({"token": tok, "username": r["result"].get("username", "")})
            user_states[chat_id] = "wait_addbot_admin"
            edit_message(chat_id, mid, render_body_text(f"✅ Bot পাওয়া গেছে: @{html.escape(r['result'].get('username', ''))}\n\n📝 এই বটের <b>Admin User ID</b> দিন:"), reply_markup=bots_cancel_kb())
            return

        elif state == "wait_addbot_admin" and text and chat_id == OWNER_ID and not IS_CHILD:
            delete_message(chat_id, msg["message_id"])
            mid = temp_data[chat_id]["msg_id"]
            if not text.strip().isdigit():
                edit_message(chat_id, mid, render_body_text("❌ Invalid ID! শুধু সংখ্যা দিন।\n\n📝 Admin User ID দিন:"), reply_markup=bots_cancel_kb())
                return
            temp_data[chat_id]["admin_id"] = int(text.strip())
            user_states[chat_id] = "wait_addbot_key"
            edit_message(chat_id, mid, render_body_text("📝 এই বটের জন্য <b>StexSMS API Key</b> Set করুন:"), reply_markup=bots_cancel_kb())
            return

        elif state == "wait_addbot_key" and text and chat_id == OWNER_ID and not IS_CHILD:
            delete_message(chat_id, msg["message_id"])
            td = temp_data[chat_id]
            bot_id = uuid.uuid4().hex[:6]
            bot_settings.setdefault("child_bots", {})[bot_id] = {
                "token": td["token"], "username": td.get("username", ""), "admin_id": td["admin_id"],
                "stex_key": text.strip(), "port": _free_child_port(), "created": datetime.now().isoformat(),
            }
            _sync_fs()  # the child reads this file at startup, so write it synchronously
            start_child_bot(bot_id)
            edit_message(chat_id, td["msg_id"], render_body_text(
                f"{PEM['ok']} <b>Bot যোগ হয়েছে ও চালু হচ্ছে!</b>\n🤖 @{html.escape(td.get('username', ''))}\n👤 Admin: <code>{td['admin_id']}</code>"),
                reply_markup=bots_main_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_bot_newkey" and text and chat_id == OWNER_ID and not IS_CHILD:
            delete_message(chat_id, msg["message_id"])
            td = temp_data[chat_id]
            entry = bot_settings.get("child_bots", {}).get(td["bot_id"])
            if entry:
                entry["stex_key"] = text.strip()
                _sync_fs()
            edit_message(chat_id, td["msg_id"], render_body_text(f"{PEM['ok']} StexSMS API Key পরিবর্তন হয়েছে! (২০ সেকেন্ডের মধ্যে বটে কার্যকর হবে)"), reply_markup=bots_main_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_broadcast":
            msg_id = msg["message_id"]
            send_message(chat_id, render_body_text(f"{PEM['ok']} Broadcast started..."))
            threading.Thread(target=broadcast_copymessage, args=(chat_id, msg_id)).start()
            del user_states[chat_id]
            return

        elif state == "wait_for_backup_upload" and "document" in msg:
            doc = msg["document"]
            filename = doc.get("file_name", "").lower()
            if not filename.endswith(".json"):
                send_message(chat_id, render_body_text(f"{PEM['no']} Please upload a .json backup file only."))
                return
            try:
                file_info = requests.get(
                    f"{BASE_URL}/getFile?file_id={doc['file_id']}", timeout=(10, 20)
                ).json()
                file_path = file_info["result"]["file_path"]
                raw_backup = requests.get(f"{FILE_URL}{file_path}", timeout=(10, 30)).content
                # Validate and replace atomically through the local database layer.
                db.import_bytes(raw_backup)
                load_db()
                user_cache.clear()
                all_known_users.clear()
                sync_users_list()
                load_api_keys()
                user_banned_cache.clear()
                del user_states[chat_id]
                send_message(chat_id, render_body_text(
                    f"{PEM['ok']} <b>Backup restored successfully!</b>\n"
                    "All settings, users, numbers, balances and withdrawal history are now loaded."
                ), reply_markup=admin_panel_keyboard())
            except Exception as exc:
                print(f"Backup restore error: {exc}")
                send_message(chat_id, render_body_text(
                    f"{PEM['no']} Backup restore failed. The existing data was kept.\n<code>{html.escape(str(exc))}</code>"
                ), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_txt" and "document" in msg:
            doc = msg["document"]
            if not doc["file_name"].endswith(".txt"):
                send_message(chat_id, render_body_text(f"{PEM['no']} Please upload a .txt file only."))
                return
            file_id = doc["file_id"]
            file_info = requests.get(
                f"{BASE_URL}/getFile?file_id={file_id}", timeout=(10, 20)
            ).json()
            file_path = file_info["result"]["file_path"]
            file_content = requests.get(f"{FILE_URL}{file_path}", timeout=(10, 30)).text
            
            temp_data[chat_id] = {"numbers": file_content.splitlines(), "filename": doc["file_name"]}
            user_states[chat_id] = "wait_for_service"
            send_message(chat_id, render_body_text(f"{PEM['ok']} File received.\n\n📌 Enter the service name (e.g., WHATSAPP):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_service" and text:
            temp_data[chat_id]["service"] = text.upper()
            user_states[chat_id] = "wait_for_country"
            send_message(chat_id, render_body_text(f"{PEM['ok']} Service set.\n\n{PEM['world']} Enter the country name (e.g., YEMEN):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_country" and text:
            country = text.upper()
            temp_data[chat_id]["country"] = country
            user_states[chat_id] = "wait_for_file_rate"
            send_message(chat_id, render_body_text(f"{PEM['ok']} Country set.\n\n{PEM['money']} Enter the OTP reward rate for this file (e.g. 5.0):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_file_rate" and text:
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 5.0):"), reply_markup=get_cancel_kb())
                return
                
            country = temp_data[chat_id]["country"]
            service = temp_data[chat_id]["service"]
            raw_numbers = temp_data[chat_id]["numbers"]
            
            clean_nums = []
            for num in raw_numbers:
                num = num.strip()
                if num:
                    if not num.startswith('+'): num = '+' + num
                    clean_nums.append(num)
            
            batch_id = str(uuid.uuid4())[:8]
            number_batches[batch_id] = {
                "filename": temp_data[chat_id]["filename"], 
                "service": service, 
                "country": country, 
                "rate": rate,
                "numbers": [{"num": n, "shares": 0, "used_by": []} for n in clean_nums]
            }
            total_uploaded_stats += len(clean_nums)
            save_db()
            
            app_full_name, prem_app_html = get_service_info_html(service)
            prem_flag_html = get_flag_info_html(clean_nums[0]) if clean_nums else f"{PEM['world']} "
            
            broadcast_txt = f"➖➖➖➖➖➖➖➖\n《 NEW NUMBERS 》\n➖➖➖➖➖➖➖➖\n{prem_flag_html} {country} {prem_app_html} {service}\n➖➖➖➖➖➖➖➖\n📤 Total Added: <b>{len(clean_nums)}</b>\n💰 Reward: <b>{rate} TK</b>\n➖➖➖➖➖➖➖➖\nUse /start to get your numbers!"
            broadcast_txt = render_body_text(broadcast_txt)
            
            send_message(chat_id, render_body_text(f"{PEM['ok']} Numbers added to local stock with {rate} TK rate! Starting broadcast..."))
            
            def simple_broadcast(txt):
                b_session = requests.Session()
                url = f"{BASE_URL}/sendMessage"
                for u_id in list(all_known_users):
                    try:
                        b_session.post(url, json={"chat_id": u_id, "text": txt, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=5)
                    except: pass
                    time.sleep(0.035)
            threading.Thread(target=simple_broadcast, args=(broadcast_txt,)).start()
            
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_stex_key" and text:
            bot_settings["stex_keys"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"✅ StexSMS API Key Added! Total Keys: {len(bot_settings.get('stex_keys', []))}"), reply_markup=stex_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_nx_srv_name" and text:
            srv = text.strip().upper()
            if "stex_services" not in bot_settings: bot_settings["stex_services"] = {}
            if srv not in bot_settings["stex_services"]: bot_settings["stex_services"][srv] = {}
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": "manage_stex_srv", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_nx_cnt_name" and text:
            cnt = country_display_name(text)
            srv = temp_data[chat_id]["srv"]
            if cnt not in bot_settings["stex_services"][srv]: bot_settings["stex_services"][srv][cnt] = []
            save_db()
            delete_message(chat_id, msg["message_id"])
            wait_msg_id = temp_data[chat_id]["msg_id"]
            user_states[chat_id] = "wait_nx_cnt_rate"
            temp_data[chat_id] = {"msg_id": wait_msg_id, "srv": srv, "cnt": cnt}
            edit_message(chat_id, wait_msg_id, render_body_text(f"{PEM['money']} Enter OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_nx_cnt_rate" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 0.5):"), reply_markup=get_cancel_kb())
                return
            if "stex_service_rates" not in bot_settings: bot_settings["stex_service_rates"] = {}
            if srv not in bot_settings["stex_service_rates"]: bot_settings["stex_service_rates"][srv] = {}
            bot_settings["stex_service_rates"][srv][cnt] = rate
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"nx_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_nx_addr" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            new_range = text.strip().replace("+", "")
            
            if new_range not in bot_settings["stex_services"][srv][cnt]:
                bot_settings["stex_services"][srv][cnt].append(new_range)
                save_db()
                
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"nx_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_api_rng" and text:
            rng = _clean_rid(text)
            delete_message(chat_id, msg["message_id"])
            if not rng.isdigit():
                edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("❌ Invalid range! Digits only (e.g. 88017).\n\n📝 Send the Range again:"), reply_markup=api_rng_cancel_kb())
                return
            temp_data[chat_id]["rng"] = rng
            user_states[chat_id] = "wait_api_rng_price"
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"{PEM['money']} Send the OTP Price for <b>{rng}</b> (e.g. 0.5):"), reply_markup=api_rng_cancel_kb())
            return

        elif state == "wait_api_rng_price" and text:
            delete_message(chat_id, msg["message_id"])
            try:
                price = float(text.strip())
                if price < 0: raise ValueError
            except ValueError:
                edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("❌ Invalid price! Send a number (e.g. 0.5):"), reply_markup=api_rng_cancel_kb())
                return
            rng = temp_data[chat_id]["rng"]
            bot_settings.setdefault("api_ranges", {})[rng] = price
            save_db()
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"{PEM['ok']} <b>Range {rng} added successfully!</b> ({price} TK/OTP)\n\n" + api_ranges_text()), reply_markup=api_ranges_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_wm" and text:
            bot_settings["w_methods"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("💳 <b>WITHDRAWAL METHODS</b>\n\nManage your withdrawal methods below:"), reply_markup=w_methods_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_status_srv_name" and text:
            srv = text.strip()
            if "status_services" not in bot_settings: bot_settings["status_services"] = []
            if srv not in bot_settings["status_services"]:
                bot_settings["status_services"].append(srv)
                save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("💠 <b>STATUS Services</b>\nManage the services shown on the STATUS card below:"), reply_markup=status_services_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_fj" and text:
            bot_settings["fj_channels"].append(parse_chat_id(text))
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("🔗 <b>FORCE JOIN SYSTEM</b>\nManage channels below:\n<i>(Note: For private links, use numeric IDs like -100...)</i>"), reply_markup=fj_settings_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return
            
        elif state == "wait_for_add_adm" and text:
            if text.isdigit():
                bot_settings["admins"].append(int(text))
                save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("👥 <b>ADMIN MANAGEMENT</b>\nManage your bot admins below:"), reply_markup=admin_settings_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_fw_id" and text:
            bot_settings["fw_groups"].append({"chat_id": text.strip(), "buttons": []})
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return
            
        elif state == "wait_for_add_fw_btn" and text:
            fw_idx = temp_data[chat_id]["fw_idx"]
            if "-" in text:
                parts = text.split("-", 1)
                btn_text = parts[0].strip()
                btn_url = parts[1].strip()
                
                emoji_id = None
                emoji_char = ""
                for ent in msg.get("entities", []):
                    if ent.get("type") == "custom_emoji":
                        emoji_id = ent.get("custom_emoji_id")
                        offset = ent.get("offset", 0)
                        length = ent.get("length", 0)
                        b_text = text.encode('utf-16-le')
                        emoji_char = b_text[offset*2:(offset+length)*2].decode('utf-16-le')
                        break
                
                if emoji_char:
                    btn_text = btn_text.replace(emoji_char, "").strip()
                    
                btn_data = {"text": btn_text, "url": btn_url}
                if emoji_id:
                    btn_data["icon_custom_emoji_id"] = emoji_id
                    
                bot_settings["fw_groups"][fw_idx]["buttons"].append(btn_data)
                save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"🛡 <b>Manage Group:</b> {bot_settings['fw_groups'][fw_idx]['chat_id']}"), reply_markup=specific_fw_group_keyboard(fw_idx))
            del user_states[chat_id]
            del temp_data[chat_id]
            return
            
        elif state == "wait_for_otp_link" and text:
            bot_settings["otp_link"] = text.strip()
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "set_TGZ":
            msg_id = temp_data[chat_id]["msg_id"]
            key = temp_data[chat_id]["key"]
            try:
                if key in ["min_withdraw", "otp_reward", "refer_reward"]: bot_settings[key] = float(text)
                elif key in ["cooldown", "num_req", "num_share"]: bot_settings[key] = int(text)
                else: bot_settings[key] = text
                save_db()
                delete_message(chat_id, msg["message_id"])
                edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())
            except:
                delete_message(chat_id, msg["message_id"])
                edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>\n\n❌ Invalid value!"), reply_markup=TGZ_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_withdraw_amount" and text:
            msg_id_to_edit = temp_data[chat_id].get("msg_id")
            try:
                amount = float(text.strip())
                bal = temp_data[chat_id]["balance"]
                min_w = bot_settings['min_withdraw']
                
                if amount < min_w:
                    if msg_id_to_edit: edit_message(chat_id, msg_id_to_edit, render_body_text(f"❌ Minimum withdrawal is {min_w} ৳!\n💰 Balance: {bal} ৳\n\n📝 Enter again:"), reply_markup=get_cancel_kb())
                    return
                if amount > bal:
                    if msg_id_to_edit: edit_message(chat_id, msg_id_to_edit, render_body_text(f"❌ You don't have enough balance!\n💰 Balance: {bal} ৳\n\n📝 Enter again:"), reply_markup=get_cancel_kb())
                    return
                    
                temp_data[chat_id]["amount"] = amount
                user_states[chat_id] = "wait_for_withdraw_number"
                if msg_id_to_edit:
                    edit_message(chat_id, msg_id_to_edit, render_body_text(f"✅ Amount: {amount} ৳\n\n📱 Now send your <b>{temp_data[chat_id]['method']}</b> account number:"), reply_markup=get_cancel_kb())
            except ValueError:
                if msg_id_to_edit: edit_message(chat_id, msg_id_to_edit, render_body_text("❌ Invalid amount!\n\n📝 Please send a valid number:"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_2fa_key" and text:
            msg_id_to_edit = temp_data.get(chat_id, {}).get("msg_id")
            delete_message(chat_id, msg.get("message_id")) # ইউজারের মেসেজ ডিলিট

            if not msg_id_to_edit:
                send_message(chat_id, render_body_text("❌ Error: Message not found. Try again."))
                del user_states[chat_id]
                return

            try:
                secret = text.strip().replace(" ", "")
                totp = pyotp.TOTP(secret)
                code = totp.now()
                remaining_time = 30 - (int(time.time()) % 30)
                
                success_txt = (
                    f"━━━━━━━━━━━━━━━\n"
                    f"《 🔐 <b>2FA CODE</b> 》\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🔐 <b>CODE:</b> <code>{code}</code>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🕓 <b>EXPIRES IN:</b> {remaining_time}s\n"
                    f"━━━━━━━━━━━━━━━"
                )
                kb = [[{"text": f"Click to copy {code}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": code}, "style": "success"}],
                      [{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": f"ref_2fa_{secret}", "style": "primary"},
                       {"text": "New Code", "icon_custom_emoji_id": "5352552689983067014", "callback_data": "gen_2fa", "style": "danger"}],
                      [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
                
                edit_message(chat_id, msg_id_to_edit, render_body_text(success_txt), reply_markup={"inline_keyboard": kb})
                del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
            except Exception:
                error_txt = "━━━━━━━━━━━━━━━\n《 🔑 <b>ENTER 2FA KEY</b> 》\n━━━━━━━━━━━━━━━\n📝 <b>SEND YOUR 2FA SECRET KEY</b>\n━━━━━━━━━━━━━━━\n❌ <b>Invalid Secret Key! Try again.</b>\n━━━━━━━━━━━━━━━"
                cancel_kb = {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_2fa", "style": "danger"}]]}
                edit_message(chat_id, msg_id_to_edit, render_body_text(error_txt), reply_markup=cancel_kb)
            return

        elif state == "wait_for_withdraw_number":
            msg_id_to_edit = temp_data[chat_id].get("msg_id")
            
            method = temp_data[chat_id]["method"]
            amount = temp_data[chat_id]["amount"]
            number = text
            req_id = f"W_{str(uuid.uuid4())[:6].upper()}"
            
            first_name = msg.get("from", {}).get("first_name", "User")
            last_name = msg.get("from", {}).get("last_name", "")
            full_name = f"{first_name} {last_name}".strip()
            
            update_balance(chat_id, -amount)
            pending_withdrawals[req_id] = {"user_id": chat_id, "amount": amount, "method": method, "number": number, "full_name": full_name}
            
            # Save withdrawal history to the local data file.
            if db:
                try:
                    db.collection('withdrawals').document(req_id).set({
                        "user_id": str(chat_id),
                        "amount": amount,
                        "method": method,
                        "status": "pending",
                        "timestamp": local_ops.SERVER_TIMESTAMP
                    })
                except: pass
                
            if bot_settings["w_group"]:
                admin_msg = f"🎙 <b>NEW WITHDRAWAL REQUEST</b>\n\n👤 <b>USER:</b> <a href='tg://user?id={chat_id}'>{full_name}</a>\n💳 <b>WITHDRAWAL:</b> {amount} TK\n🍏 <b>NUMBER:</b> <code>{number}</code>\n🏦 <b>METHOD:</b> {method}\n\n🧾 <b>REQ ID:</b> {req_id}\n👨‍⚖️ <b>PROCESSED BY ADMIN</b>"
                kb = {"inline_keyboard": [[{"text": "APPROVE", "icon_custom_emoji_id": "5352694861990501856", "callback_data": f"wapp_{req_id}", "style": "success"}, {"text": "REJECT", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"wrej_{req_id}", "style": "danger"}]]}
                send_message(bot_settings["w_group"], render_body_text(admin_msg), reply_markup=kb)
            
            kb = {"inline_keyboard": [[{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]}
            success_text = f"{PEM['ok']} Your withdrawal request has been submitted!\n\n🧾 <b>Req ID:</b> {req_id}\n💰 <b>Amount:</b> {amount} ৳\n🏦 <b>Method:</b> {method}\n📱 <b>Number:</b> <code>{number}</code>"
            
            if msg_id_to_edit:
                edit_message(chat_id, msg_id_to_edit, render_body_text(success_text), reply_markup=kb)
            else:
                send_message(chat_id, render_body_text(success_text), reply_markup=kb)
                
            del user_states[chat_id]
            del temp_data[chat_id]
            return

    # --- Regular Commands ---
    if text.startswith("/start"):
        get_user(chat_id)
        
        # --- PROCESS PENDING REFERRAL ---
        if db:
            try:
                doc = db.collection('users').document(str(chat_id)).get()
                if doc.exists:
                    u_data = doc.to_dict()
                    if u_data.get("referred_by") and not u_data.get("ref_paid"):
                        inviter = u_data["referred_by"]
                        db.collection('users').document(str(chat_id)).update({"ref_paid": True})
                        reward = bot_settings.get("refer_reward", 0.2)
                        update_balance(inviter, reward)
                        db.collection('users').document(str(inviter)).update({"total_refers": local_ops.Increment(1)})
                        if inviter in user_cache:
                            user_cache[inviter]["total_refers"] = user_cache[inviter].get("total_refers", 0) + 1
                        ref_msg = (
                            f"{PEM['gift']} <b>New Referral !</b>\n"
                            f"------------------\n"
                            f"🔥 <b>You Received {reward} TK</b>\n"
                            f"------------------\n"
                            f"{PEM['user']} <b>From User ID:</b> <code>{chat_id}</code>"
                        )
                        send_message(inviter, render_body_text(ref_msg))
            except Exception as e:
                print(f"Local referral reward deferred for {chat_id}: {e}")
                    
        c_msg = bot_settings["custom_messages"].get("start", {})
        txt = render_body_text(c_msg.get("text", f"{PEM['hi']} Welcome!"))
        kb = []
        for b in c_msg.get("buttons", []):
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        
        if kb:
            send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})
            send_message(chat_id, render_body_text(f"{PEM['gear']} Navigation Menu:"), reply_markup=main_menu(chat_id))
        else:
            send_message(chat_id, txt, reply_markup=main_menu(chat_id))

    elif text == "Refer":
        u_data = get_user(chat_id)
        ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
        c_msg = bot_settings["custom_messages"].get("refer", {})
        
        raw_txt = c_msg.get("text", f"{PEM['gift']} Refer").replace("{ref_link}", ref_link).replace("{total_ref}", str(u_data.get('total_refers', 0))).replace("{ref_reward}", str(bot_settings['refer_reward']))
        txt = render_body_text(raw_txt)
        
        kb = [[{"text": "COPY LINK", "icon_custom_emoji_id": "5192739271886282680", "copy_text": {"text": ref_link}, "style": "success"}]]
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        kb.append([{"text": "CLOSE", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        
        send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})

    elif text == "WITHDRAWAL":
        if not bot_settings["withdraw_on"]:
            send_message(chat_id, render_body_text(f"{PEM['no']} Withdrawals are currently disabled."))
            return
        
        u_data = get_user(chat_id)
        bal = u_data.get('balance', 0.0)
        
        c_msg = bot_settings["custom_messages"].get("withdrawal", {})
        raw_txt = c_msg.get("text", "Withdrawal").replace("{bal}", str(bal)).replace("{total_otp}", str(u_data.get('total_otps', 0))).replace("{total_ref}", str(u_data.get('total_refers', 0))).replace("{min_w}", str(bot_settings['min_withdraw']))
        txt = render_body_text(raw_txt)
        
        kb = []
        for m in bot_settings["w_methods"]:
            kb.append([{"text": m.strip(), "icon_custom_emoji_id": "5190899075968441286", "callback_data": f"sel_wm_{m.strip()}", "style": "primary"}])
        
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        kb.append([{"text": "Cancel", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})

    elif text == "Admin Panel" and is_admin(chat_id):
        send_message(chat_id, get_admin_text(), reply_markup=admin_panel_keyboard())

    elif text == "GET NUMBER":
        local_srvs = set([b["service"] for b in number_batches.values() if b["numbers"]])
        stex_srvs = set(bot_settings.get("stex_services", {}).keys())
        all_services = local_srvs.union(stex_srvs)
        
        if not all_services:
            send_message(chat_id, render_body_text(f"{PEM['no']} No numbers or services available!"))
        else:
            c_msg = bot_settings["custom_messages"].get("get_number", {})
            txt = render_body_text(c_msg.get("text", f"{PEM['pin']} Select Service"))
            
            apps_db = bot_settings.get("premium_apps", {})
            kb = []
            for s in all_services:
                emoji_id = "5352694861990501856" # Default icon
                for app_key, app_data in apps_db.items():
                    if s.upper() == app_key or s.upper() in app_key or app_key in s.upper():
                        if "id" in app_data:
                            emoji_id = app_data["id"]
                            break
                kb.append([{"text": f"{s}", "icon_custom_emoji_id": emoji_id, "callback_data": f"g_s_{s}", "style": "primary"}])
            
            for b in c_msg.get("buttons", []): 
                b_copy = b.copy()
                if "style" not in b_copy: b_copy["style"] = "primary"
                kb.append([b_copy])
            kb.append([{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
            
            send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})

    elif text == "2FA ONLINE" or text == "🔐 2FA ONLINE":
        txt = "━━━━━━━━━━━━━━━\n《 🔐 <b>2FA ONLINE</b> 》\n━━━━━━━━━━━━━━━\n<i>Generate your 2FA security code instantly using your secret key.</i>\n━━━━━━━━━━━━━━━"
        kb = [[{"text": "Generate 2fa code", "icon_custom_emoji_id": "5353022963132174959", "callback_data": "gen_2fa", "style": "success"}],
              [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
        send_message(chat_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif text == "API DOCUMENT":
        doc_txt, doc_kb = api_doc_message(chat_id)
        send_message(chat_id, doc_txt, reply_markup=doc_kb)

    elif text == "STATUS":
        send_message(chat_id, render_body_text(build_status_report_text()), reply_markup=status_kb())

    elif text == "SUPPORT":
        c_msg = bot_settings["custom_messages"].get("support", {})
        txt = render_body_text(c_msg.get("text", f"{PEM['msg']} Support"))
        if not txt.strip(): txt = render_body_text(f"{PEM['msg']} Support")
        kb = []
        for b in c_msg.get("buttons", []):
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
            
        sup_link = bot_settings.get("support_link", "")
        if sup_link:
            kb.insert(0, [{"text": "Contact Support", "icon_custom_emoji_id": "5337302974806922068", "url": sup_link, "style": "success"}])
            
        kb.append([{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        send_message(chat_id, txt, reply_markup={"inline_keyboard": kb} if kb else None)

    elif text == "Leader Board":
        send_leaderboard(chat_id)

def build_alltime_leaderboard_section():
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    body = ""
    try:
        if not db:
            return "└ <i>Leaderboard unavailable right now.</i>\n"
        top_users = db.collection('users').order_by("total_otps", direction="DESCENDING").limit(10).stream()
        rank = 1
        for u in top_users:
            d = u.to_dict()
            otp_count = d.get("total_otps", 0)
            if otp_count <= 0:
                continue
            name = html.escape(d.get("display_name", "User") or "User")
            rank_icon = medals.get(rank, f"{rank}.")
            body += f"{rank_icon} <b>{name}</b>\n    {PEM['user']} ID: <code>{u.id}</code> │ {PEM['msg']} OTPs: <b>{otp_count}</b>\n\n"
            rank += 1
        if not body:
            body = "└ <i>No OTPs claimed yet. Be the first!</i>\n"
    except Exception:
        body = "└ <i>Failed to load leaderboard. Try again.</i>\n"
    return body

# 🌟 The 7-day board is expensive to recompute (and resets on a 7-day cycle),
# so it's cached and only rebuilt every 8 days - the all-time board above is
# always computed fresh on every 5-second refresh.
_weekly_lb_cache = {"body": "", "built_at": 0}
WEEKLY_LB_REFRESH_SECS = 8 * 24 * 60 * 60   # 8 days
WEEKLY_LB_RESET_SECS = 7 * 24 * 60 * 60     # 7 days

def _reset_weekly_otps_if_due():
    now = time.time()
    last_reset = bot_settings.get("weekly_reset_at", 0)
    if now - last_reset < WEEKLY_LB_RESET_SECS:
        return
    try:
        docs = db.collection('users').where("weekly_otps", ">", 0).stream()
        batch = db.batch()
        n = 0
        for d in docs:
            batch.update(db.collection('users').document(d.id), {"weekly_otps": 0})
            n += 1
            if n >= 400:
                batch.commit()
                batch = db.batch()
                n = 0
        if n:
            batch.commit()
    except Exception as e:
        print(f"❌ Weekly leaderboard reset error: {e}")
    bot_settings["weekly_reset_at"] = now
    save_db()

def build_weekly_leaderboard_section(force=False):
    now = time.time()
    if not force and (now - _weekly_lb_cache["built_at"] < WEEKLY_LB_REFRESH_SECS) and _weekly_lb_cache["body"]:
        return _weekly_lb_cache["body"]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    body = ""
    try:
        if not db:
            body = "└ <i>Leaderboard unavailable right now.</i>\n"
        else:
            _reset_weekly_otps_if_due()
            top_users = db.collection('users').order_by("weekly_otps", direction="DESCENDING").limit(10).stream()
            rank = 1
            for u in top_users:
                d = u.to_dict()
                otp_count = d.get("weekly_otps", 0)
                if otp_count <= 0:
                    continue
                name = html.escape(d.get("display_name", "User") or "User")
                rank_icon = medals.get(rank, f"{rank}.")
                body += f"{rank_icon} <b>{name}</b>\n    {PEM['user']} ID: <code>{u.id}</code> │ {PEM['msg']} OTPs: <b>{otp_count}</b>\n\n"
                rank += 1
            if not body:
                body = "└ <i>No OTPs claimed this week yet.</i>\n"
    except Exception:
        body = "└ <i>Failed to load leaderboard. Try again.</i>\n"

    _weekly_lb_cache["body"] = body
    _weekly_lb_cache["built_at"] = now
    return body

def _build_leaderboard_core(view="both"):
    # 🌟 FIX: added the missing "7-Day" / "All-Time" toggle - the underlying
    # section builders already existed, they just weren't reachable as
    # separate views before.
    updated_at = datetime.now().strftime("%I:%M:%S %p")
    if view == "alltime":
        alltime_body = build_alltime_leaderboard_section()
        return (
            f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>ALL-TIME LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{alltime_body}"
            f"━━━━━━━━━━━━━━━\n<i>{PEM['refresh']} Refreshes every 5 sec • Updated {updated_at}</i>"
        )
    if view == "weekly":
        weekly_body = build_weekly_leaderboard_section()
        return (
            f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>7-DAY LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{weekly_body}"
            f"━━━━━━━━━━━━━━━\n<i>{PEM['refresh']} Refreshes every 8 days • Updated {updated_at}</i>"
        )
    alltime_body = build_alltime_leaderboard_section()
    weekly_body = build_weekly_leaderboard_section()
    return (
        f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>ALL-TIME LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{alltime_body}"
        f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>7-DAY LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{weekly_body}"
        f"━━━━━━━━━━━━━━━\n<i>{PEM['refresh']} All-time refreshes every 5 sec, 7-day every 8 days • Updated {updated_at}</i>"
    )

def build_today_leaderboard_section(limit=5):
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    body = ""
    try:
        rows = (db.collection('users').where("today_date", "==", bd_today())
                  .order_by("today_otps", direction="DESCENDING").limit(limit).stream())
        rank = 1
        for u in rows:
            d = u.to_dict()
            n = d.get("today_otps", 0)
            if n <= 0:
                continue
            name = html.escape(d.get("display_name", "User") or "User")
            body += f"{medals.get(rank, str(rank) + '.')} <b>{name}</b> │ {PEM['msg']} <b>{n}</b>\n"
            rank += 1
        if not body:
            body = "└ <i>No OTPs today yet.</i>\n"
    except Exception:
        body = "└ <i>Failed to load today's top.</i>\n"
    return body

def build_leaderboard_text(view="both"):
    updated_at = datetime.now().strftime("%I:%M:%S %p")
    sep = "━━━━━━━━━━━━━━━"
    if view == "today":
        return (f"{sep}\n《 {PEM['calendar']} <b>TODAY TOP</b> 》\n{sep}\n{build_today_leaderboard_section(10)}"
                f"{sep}\n<i>{PEM['refresh']} Resets daily 12:00 AM (BD) • Updated {updated_at}</i>")
    core = _build_leaderboard_core(view)
    top_n = 3 if view == "both" else 5
    return (core + f"\n{sep}\n《 {PEM['calendar']} <b>TODAY TOP</b> 》\n{sep}\n"
            + build_today_leaderboard_section(top_n) + sep)

def leaderboard_kb(view="both"):
    # Only the two requested views are exposed.  There is intentionally no
    # "Both" button; the initial screen may still show both sections.
    row = [
        {"text": "All-Time", "icon_custom_emoji_id": "5352877703043258544", "callback_data": "lb_view_alltime", "style": "primary"},
        {"text": "7-Day", "icon_custom_emoji_id": "5375338737028841420", "callback_data": "lb_view_weekly", "style": "primary"},
        {"text": "Today", "icon_custom_emoji_id": "5413879192267805083", "callback_data": "lb_view_today", "style": "success"},
    ]
    return {"inline_keyboard": [
        row,
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "lb_close", "style": "danger"}]
    ]}

# 🌟 Tracks open Leader Board messages (and which view each is showing) so we
# can auto-refresh them every 5 seconds.
active_leaderboard_messages = {}

def send_leaderboard(chat_id, view="alltime"):
    res = send_message(chat_id, render_body_text(build_leaderboard_text(view)), reply_markup=leaderboard_kb(view))
    if res and res.get("ok"):
        active_leaderboard_messages[chat_id] = {"msg_id": res["result"]["message_id"], "view": view}

def leaderboard_auto_refresh_thread():
    while True:
        time.sleep(5)  # 5 seconds - only the all-time section is truly recomputed this often
        for cid, info in list(active_leaderboard_messages.items()):
            mid, view = info["msg_id"], info.get("view", "both")
            try:
                res = edit_message(cid, mid, render_body_text(build_leaderboard_text(view)), reply_markup=leaderboard_kb(view))
                if not res or not res.get("ok"):
                    active_leaderboard_messages.pop(cid, None)
            except Exception:
                active_leaderboard_messages.pop(cid, None)


def expire_previous_number(chat_id):
    if chat_id in user_active_sessions:
        prev_data = user_active_sessions[chat_id]
        prev_msg_id = prev_data["msg_id"]
        nums = prev_data["nums"]
        
        # API সিস্টেম থেকে রিমুভ করা যাতে ইনবক্সে আর মেসেজ না যায়
        for num in nums:
            if num in stex_assigned_numbers: del stex_assigned_numbers[num]
        save_db()
        
        # আগের মেসেজ ইডিট করে Expired বাটন বসানো
        kb = [[{"text": "Number Expired", "icon_custom_emoji_id": "5336997731481193790", "callback_data": "ignore", "style": "danger"}]]
        try:
            edit_message(chat_id, prev_msg_id, render_body_text(f"{PEM['no']} <b>Number Expired</b>"), reply_markup={"inline_keyboard": kb})
        except:
            pass
        del user_active_sessions[chat_id]

# ==========================================
# Callback Query Handler
# ==========================================
def handle_callback(call):
    global total_assigned_stats
    chat_id = call["message"]["chat"]["id"]
    chat_type = call["message"]["chat"].get("type", "private")
    data = call.get("data", "")

    # 🌟 Button Loading Fix: বাটন চাপার সাথে সাথেই টেলিগ্রামকে Response দিয়ে দেওয়া, যাতে বাটন আটকে না থাকে!
    if not data.startswith("test_p_conn_") and not data.startswith("c_n_") and not data.startswith("g_c_"):
        try: threading.Thread(target=answer_callback, args=(call["id"],)).start()
        except: pass

    if chat_type != "private" and not (data.startswith("wapp_") or data.startswith("wrej_")):
        return

    msg_id = call["message"]["message_id"]

    if IS_CHILD and data.startswith(CHILD_BLOCKED_CB):
        answer_callback(call["id"], "🚫 এই অপশন এই বটে নেই।", show_alert=True)
        return

    if chat_type == "private":
        register_user_local(chat_id)
        sync_user_display_name(chat_id, call)  # 🌟 Keep name fresh even for button-only users

        if is_user_banned(chat_id):
            answer_callback(call["id"], "🚫 You are banned from using this bot!", show_alert=True)
            return

        if not check_force_join(chat_id) and data != "check_fj":
            send_force_join_msg(chat_id)
            return

    if data == "check_fj":
        if check_force_join(chat_id):
            delete_message(chat_id, msg_id)
            send_message(chat_id, render_body_text(f"{PEM['ok']} Thanks for joining! You can now use the bot."), reply_markup=main_menu(chat_id))
            
            # --- PROCESS PENDING REFERRAL ---
            if db:
                doc = db.collection('users').document(str(chat_id)).get()
                if doc.exists:
                    u_data = doc.to_dict()
                    if u_data.get("referred_by") and not u_data.get("ref_paid"):
                        inviter = u_data["referred_by"]
                        db.collection('users').document(str(chat_id)).update({"ref_paid": True})
                        reward = bot_settings.get("refer_reward", 0.2)
                        update_balance(inviter, reward)
                        db.collection('users').document(str(inviter)).update({"total_refers": local_ops.Increment(1)})
                        if inviter in user_cache:
                            user_cache[inviter]["total_refers"] = user_cache[inviter].get("total_refers", 0) + 1
                        ref_msg = (
                            f"{PEM['gift']} <b>New Referral !</b>\n"
                            f"------------------\n"
                            f"🔥 <b>You Received {reward} TK</b>\n"
                            f"------------------\n"
                            f"{PEM['user']} <b>From User ID:</b> <code>{chat_id}</code>"
                        )
                        send_message(inviter, render_body_text(ref_msg))
        else:
            answer_callback(call["id"], "❌ You haven't joined all channels yet!", show_alert=True)
        return

    if data == "close_msg":
        delete_message(chat_id, msg_id)

    elif data.startswith("ul_p_") and is_admin(chat_id):
        try: page_n = int(data[5:] or 0)
        except ValueError: page_n = 0
        vt, vk = api_users_view(page_n)
        edit_message(chat_id, msg_id, vt, reply_markup=vk)
        answer_callback(call["id"])

    elif data == "ul_find" and is_admin(chat_id):
        user_states[chat_id] = "wait_ul_find"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID:"), reply_markup=get_cancel_kb())
        answer_callback(call["id"])

    elif data.startswith("ul_t_") and is_admin(chat_id):
        _, _, uid_s, page_s = data.split("_", 3)
        t_uid = int(uid_s)
        new_state = not get_user(t_uid).get("api_enabled", False)
        set_user_api_enabled(t_uid, new_state)
        if page_s == "f":
            vt, vk = api_users_view(0, only_uid=uid_s)
        else:
            vt, vk = api_users_view(int(page_s))
        edit_message(chat_id, msg_id, vt, reply_markup=vk)
        answer_callback(call["id"], "✅ API ENABLED" if new_state else "🚫 API DISABLED")

    elif data == "api_rng_main" and is_admin(chat_id):
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        edit_message(chat_id, msg_id, render_body_text(api_ranges_text()), reply_markup=api_ranges_keyboard())

    elif data == "api_rng_add" and is_admin(chat_id):
        user_states[chat_id] = "wait_api_rng"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the Range (digits only, no XXX — e.g. 88017):"), reply_markup=api_rng_cancel_kb())

    elif data.startswith("api_rng_del_") and is_admin(chat_id):
        bot_settings.get("api_ranges", {}).pop(data[len("api_rng_del_"):], None)
        save_db()
        edit_message(chat_id, msg_id, render_body_text(api_ranges_text()), reply_markup=api_ranges_keyboard())

    elif data == "api_doc_full":
        answer_callback(call["id"], "ℹ️ Full document is provided separately.", show_alert=True)

    elif data == "api_regen":
        if not get_user(chat_id).get("api_enabled", False):
            answer_callback(call["id"], "🚫 API is disabled for your account.", show_alert=True)
            return
        ensure_user_api_key(chat_id, regenerate=True)
        doc_txt, doc_kb = api_doc_message(chat_id)
        edit_message(chat_id, msg_id, doc_txt, reply_markup=doc_kb)
        answer_callback(call["id"], "🔄 New key generated. The old key no longer works.", show_alert=True)

    elif data == "lb_close":
        active_leaderboard_messages.pop(chat_id, None)
        delete_message(chat_id, msg_id)

    elif data in ("lb_view_alltime", "lb_view_weekly", "lb_view_today"):
        view = data.replace("lb_view_", "")
        edit_message(chat_id, msg_id, render_body_text(build_leaderboard_text(view)), reply_markup=leaderboard_kb(view))
        if chat_id in active_leaderboard_messages:
            active_leaderboard_messages[chat_id]["view"] = view
        answer_callback(call["id"], f"{PEM['refresh']} Switched!")

    elif data == "status_refresh":
        edit_message(chat_id, msg_id, render_body_text(build_status_report_text()), reply_markup=status_kb())
        answer_callback(call["id"], "🔄 Refreshed!")

    elif data == "status_close":
        delete_message(chat_id, msg_id)

    elif data == "manage_status_srv":
        edit_message(chat_id, msg_id, render_body_text("💠 <b>STATUS Services</b>\nManage the services shown on the STATUS card below:"), reply_markup=status_services_keyboard())

    elif data == "status_add_srv":
        user_states[chat_id] = "wait_status_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter the service name to track (e.g. 1XBET, TELEGRAM):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_status_srv", "style": "danger"}]]})

    elif data.startswith("status_del_srv_"):
        idx = int(data.replace("status_del_srv_", ""))
        services = bot_settings.get("status_services", [])
        if 0 <= idx < len(services):
            services.pop(idx)
            save_db()
        edit_message(chat_id, msg_id, render_body_text("💠 <b>STATUS Services</b>\nManage the services shown on the STATUS card below:"), reply_markup=status_services_keyboard())

    elif data.startswith("bots_") and not IS_CHILD and chat_id == OWNER_ID:
        handle_bots_callback(data, chat_id, msg_id)

    elif data == "cancel_state":
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        delete_message(chat_id, msg_id)

    elif data == "cancel_2fa":
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        txt = "━━━━━━━━━━━━━━━\n《 🔐 <b>2FA ONLINE</b> 》\n━━━━━━━━━━━━━━━\n<i>Generate your 2FA security code instantly using your secret key.</i>\n━━━━━━━━━━━━━━━"
        kb = [[{"text": "Generate 2fa code", "icon_custom_emoji_id": "5353022963132174959", "callback_data": "gen_2fa", "style": "success"}],
              [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})
        answer_callback(call["id"])

    elif data == "gen_2fa":
        user_states[chat_id] = "wait_for_2fa_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        txt = "━━━━━━━━━━━━━━━\n《 🔑 <b>ENTER 2FA KEY</b> 》\n━━━━━━━━━━━━━━━\n📝 <b>SEND YOUR 2FA SECRET KEY</b>\n━━━━━━━━━━━━━━━"
        kb = {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_2fa", "style": "danger"}]]}
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup=kb)
        answer_callback(call["id"])

    elif data.startswith("ref_2fa_"):
        secret = data.replace("ref_2fa_", "")
        try:
            totp = pyotp.TOTP(secret)
            code = totp.now()
            remaining_time = 30 - (int(time.time()) % 30)
            
            success_txt = (
                f"━━━━━━━━━━━━━━━\n"
                f"《 🔐 <b>2FA CODE</b> 》\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🔐 <b>CODE:</b> <code>{code}</code>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🕓 <b>EXPIRES IN:</b> {remaining_time}s\n"
                f"━━━━━━━━━━━━━━━"
            )
            kb = [[{"text": f"Click to copy {code}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": code}, "style": "success"}],
                  [{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": f"ref_2fa_{secret}", "style": "primary"},
                   {"text": "New Code", "icon_custom_emoji_id": "5352552689983067014", "callback_data": "gen_2fa", "style": "danger"}],
                  [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
            
            edit_message(chat_id, msg_id, render_body_text(success_txt), reply_markup={"inline_keyboard": kb})
        except:
            answer_callback(call["id"], "❌ Error refreshing code!", show_alert=True)

    elif data == "cancel_TGZ_edit":
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())
        
    elif data == "dummy_alert":
        answer_callback(call["id"], "This feature will be added later!", show_alert=True)
        
    # --- User Management Flows Integration ---
    elif data == "user_management":
        edit_message(chat_id, msg_id, get_user_management_text(), reply_markup=user_management_keyboard())

    elif data == "um_manage_balance":
        user_states[chat_id] = "wait_for_um_bal_uid"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID to Manage Balance:"), reply_markup=get_cancel_kb())
        
    elif data == "um_ban_unban":
        user_states[chat_id] = "wait_for_um_ban_uid"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID to Ban or Unban:"), reply_markup=get_cancel_kb())

    elif data == "um_user_profile":
        user_states[chat_id] = "wait_for_um_prof_uid"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID to View Profile:"), reply_markup=get_cancel_kb())

    # --- Menu Design Integration ---
    elif data == "menu_design_list":
        edit_message(chat_id, msg_id, render_body_text(f"🎨 <b>Menu Design Editor</b>\n\nSelect a menu block to edit its Body Text and Inline Buttons. You can use Premium Emojis too!"), reply_markup=menu_design_list_keyboard())

    elif data == "md_reset_defaults":
        bot_settings["custom_messages"] = DEFAULT_CUSTOM_MESSAGES.copy()
        save_db()
        answer_callback(call["id"], "✅ Resetted to Premium Defaults!", show_alert=True)

    elif data.startswith("md_edit_"):
        answer_callback(call["id"])
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        key = data.replace("md_edit_", "")
        cm_text = render_body_text(bot_settings["custom_messages"].get(key, {}).get("text", "..."))
        try:
            edit_message(chat_id, msg_id, render_body_text(f"🎨 <b>Editing: {key.upper()}</b>\n\nPreview of current Text:\n{cm_text}"), reply_markup=menu_edit_options_keyboard(key))
        except: pass

    elif data.startswith("md_text_"):
        key = data.replace("md_text_", "")
        user_states[chat_id] = "wait_for_menu_text"
        temp_data[chat_id] = {"msg_id": msg_id, "menu_key": key}
        edit_message(chat_id, msg_id, render_body_text(f"📝 <b>Edit Body: {key.upper()}</b>\n\nSend the new text. You can use Premium Emojis directly here.\n(Use standard HTML like <b>bold</b>, <i>italic</i> for formatting)"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"md_edit_{key}", "style": "danger"}]]})

    elif data.startswith("md_btns_"):
        answer_callback(call["id"]) 
        if chat_id in user_states: del user_states[chat_id] 
        if chat_id in temp_data: del temp_data[chat_id]
        key = data.replace("md_btns_", "")
        try:
            edit_message(chat_id, msg_id, render_body_text(f"⚙️ <b>Edit Inline Buttons: {key.upper()}</b>"), reply_markup=menu_buttons_list_keyboard(key))
        except: pass

    elif data.startswith("md_addbtn_"):
        key = data.replace("md_addbtn_", "")
        user_states[chat_id] = "wait_for_menu_btn"
        temp_data[chat_id] = {"msg_id": msg_id, "menu_key": key}
        edit_message(chat_id, msg_id, render_body_text(f"➕ <b>Add Button: {key.upper()}</b>\n\nSend custom button in this format:\n<code>Button Text - https://link.com</code>\n\n<i>(Only normal Emojis supported here!)</i>"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"md_btns_{key}", "style": "danger"}]]})

    elif data.startswith("md_delbtn_"):
        parts = data.split("_")
        key = parts[2]
        b_idx = int(parts[3])
        if b_idx < len(bot_settings["custom_messages"][key]["buttons"]):
            del bot_settings["custom_messages"][key]["buttons"][b_idx]
            save_db()
            answer_callback(call["id"], "✅ Button Deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text(f"⚙️ <b>Edit Inline Buttons: {key.upper()}</b>"), reply_markup=menu_buttons_list_keyboard(key))

    elif data.startswith("sel_wm_"):
        method = data.replace("sel_wm_", "")
        bal = get_user(chat_id).get('balance', 0.0)
        min_w = bot_settings['min_withdraw']
        
        if bal < min_w:
            answer_callback(call["id"], f"❌ আপনার ব্যালেন্স অপর্যাপ্ত! মিনিমাম {min_w} ৳ প্রয়োজন।", show_alert=True)
            return
            
        temp_data[chat_id] = {"method": method, "balance": bal, "msg_id": msg_id}
        user_states[chat_id] = "wait_for_withdraw_amount"
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['ok']} Method: {method}\n💰 Available Balance: {bal} ৳\n\n📝 Enter the amount you want to withdraw (Min: {min_w} ৳):"), reply_markup=get_cancel_kb())
        answer_callback(call["id"])

    elif data == "test_message_flow":
        user_states[chat_id] = "wait_for_test_service"
        temp_data[chat_id] = {}
        edit_message(chat_id, msg_id, render_body_text("🧪 <b>Test Mode</b>\n\n📝 Send the Service Name (e.g., IG):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]]})

    elif data == "manage_emojis":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['star']} <b>Premium Emoji Management</b>\n\nUpload your TXT files or manually add them below:"), reply_markup=emoji_settings_keyboard())

    elif data == "up_flags_txt":
        user_states[chat_id] = "wait_for_flag_txt"
        edit_message(chat_id, msg_id, render_body_text("📂 Please upload the <b>Flag Emojis</b> <code>.txt</code> file."), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_emojis", "style": "danger"}]]})

    elif data == "up_apps_txt":
        user_states[chat_id] = "wait_for_app_txt"
        edit_message(chat_id, msg_id, render_body_text("📂 Please upload the <b>Service Apps</b> <code>.txt</code> file."), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_emojis", "style": "danger"}]]})

    elif data == "add_single_emoji":
        user_states[chat_id] = "wait_for_emoji_extract"
        edit_message(chat_id, msg_id, render_body_text("📝 যেকোনো একটি Premium Emoji সেন্ড করুন (যেমন: 🇧🇩 বা 🚫):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_emojis", "style": "danger"}]]})

    elif data == "dl_flags_txt":
        content = generate_emoji_txt("flags")
        if content:
            send_document(chat_id, "Flag_Emojis.txt", content)
            answer_callback(call["id"], "✅ Downloaded!")
        else:
            answer_callback(call["id"], "❌ No Flag Emojis found!", show_alert=True)

    elif data == "dl_apps_txt":
        content = generate_emoji_txt("apps")
        if content:
            send_document(chat_id, "Service_Apps.txt", content)
            answer_callback(call["id"], "✅ Downloaded!")
        else:
            answer_callback(call["id"], "❌ No App Emojis found!", show_alert=True)

    elif data == "del_all_flags":
        bot_settings["premium_flags"] = {}
        save_db()
        answer_callback(call["id"], "✅ All Premium Flags Deleted Successfully!", show_alert=True)

    elif data == "broadcast_msg":
        user_states[chat_id] = "wait_for_broadcast"
        edit_message(chat_id, msg_id, render_body_text("📢 <b>Broadcast Mode</b>\n\nSend the message you want to broadcast (Text, Photo, Video, File etc)."), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]})

    elif data == "upload_num":
        user_states[chat_id] = "wait_for_txt"
        edit_message(chat_id, msg_id, render_body_text("📂 Please upload the numbers in a <b>.txt</b> file."), reply_markup={"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]})

    elif data == "backup_menu":
        edit_message(chat_id, msg_id, render_body_text(
            f"💾 <b>BACKUP & RESTORE</b>\n\n"
            f"Your complete bot data is stored in <code>{html.escape(DATA_FILE)}</code>.\n"
            "Download it before moving hosts, then upload the same JSON file on the new host."
        ), reply_markup={"inline_keyboard": [
            [{"text": "Download Backup", "icon_custom_emoji_id": "5352597830089347330", "callback_data": "download_backup", "style": "success"}],
            [{"text": "Upload Backup", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "upload_backup", "style": "primary"}],
            [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]})

    elif data == "download_backup":
        try:
            send_document(chat_id, "bot_backup.json", db.export_bytes())
            answer_callback(call["id"], "✅ Backup downloaded!")
        except Exception as exc:
            answer_callback(call["id"], f"❌ Backup failed: {exc}", show_alert=True)

    elif data == "upload_backup":
        user_states[chat_id] = "wait_for_backup_upload"
        edit_message(chat_id, msg_id, render_body_text(
            "📤 Send the <b>bot_backup.json</b> file now.\n"
            "The current local data will be replaced after the file is validated."
        ), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "backup_menu", "style": "danger"}]]})

    elif data == "delete_files":
        kb = []
        for b_id, b_data in number_batches.items():
            kb.append([{"text": f"{b_data['filename']} ({len(b_data['numbers'])})", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"del_b_{b_id}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "primary"}])
        txt = "🗑 Select a file to delete:" if len(kb) > 1 else f"{PEM['no']} No files found."
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_b_"):
        b_id = data.split("del_b_")[1]
        if b_id in number_batches:
            del number_batches[b_id]
            save_db()
            answer_callback(call["id"], "✅ File deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "delete_files", "id": call["id"]})

    elif data == "show_used":
        kb = {"inline_keyboard": [[{"text": "Download TXT", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_used", "style": "primary"}], [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['ok']} <b>Total Used Numbers:</b> {len(used_numbers_list)}"), reply_markup=kb)

    elif data == "show_unused":
        unused_count = sum(len(b["numbers"]) for b in number_batches.values())
        kb = {"inline_keyboard": [[{"text": "Download TXT", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_unused", "style": "primary"}], [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['rocket']} <b>Total Unused Numbers:</b> {unused_count}"), reply_markup=kb)

    elif data == "dl_used":
        if not used_numbers_list:
            answer_callback(call["id"], "❌ No used numbers found!", show_alert=True)
            return
        content = "\n".join(used_numbers_list).encode('utf-8')
        send_document(chat_id, "used_numbers.txt", content)
        answer_callback(call["id"])

    elif data == "dl_unused":
        unused_list = [n["num"] for b in number_batches.values() for n in b["numbers"]]
        if not unused_list:
            answer_callback(call["id"], "❌ No unused numbers found!", show_alert=True)
            return
        content = "\n".join(unused_list).encode('utf-8')
        send_document(chat_id, "unused_numbers.txt", content)
        answer_callback(call["id"])

    elif data == "lb_main":
        txt = f"━━━━━━━━━━━━━━━\n《 {PEM['admin']} <b>LEADER BOARD MENU</b> 》\n━━━━━━━━━━━━━━━\n<i>Select a category to view the top performers or history.</i>\n━━━━━━━━━━━━━━━"
        kb = [
            [{"text": "Top Referrers", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "lb_top_refs", "style": "primary"}],
            [{"text": "Top OTP Receivers", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "lb_top_otps", "style": "primary"}],
            [{"text": "Withdrawal History", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "lb_w_history", "style": "success"}],
            [{"text": "Back to Admin", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("lb_"):
        sub = data.replace("lb_", "")
        edit_message(chat_id, msg_id, render_body_text("⌛ <i>Fetching Data...</i>"))
        
        num_map = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣", "0": "0️⃣"}
        def get_p_num(n): return "".join([num_map.get(c, c) for c in str(n)])
        
        try:
            if sub == "top_refs":
                title, field, limit, icon = "TOP 5 REFERRERS", "total_refers", 5, PEM.get('user', '👥')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "top_otps":
                title, field, limit, icon = "TOP 5 OTP RECEIVERS", "total_otps", 5, PEM.get('msg', '📩')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "w_history":
                title, limit, icon = "LAST 10 WITHDRAWALS", 10, PEM.get('money', '💸')
                ws = db.collection('withdrawals').order_by('timestamp', direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for w in ws:
                    d = w.to_dict()
                    s = str(d.get('status','Pending')).lower()
                    stat_icon = PEM.get('ok','✅') if s in ["approved","success"] else PEM.get('no','❌') if s=="rejected" else "⏳"
                    uid = d.get('user_id','User')
                    p = "└" if count == limit else "├"
                    res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={uid}'>{uid}</a> ➔ <b>{d.get('amount',0)}৳</b> {stat_icon}\n"
                    count += 1
                if not res_txt: res_txt = "└ <i>No history found.</i>\n"

            final_msg = f"━━━━━━━━━━━━━━━\n{icon} <b>{title}</b>\n━━━━━━━━━━━━━━━\n{res_txt}━━━━━━━━━━━━━━━"
            kb = [[{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": data, "style": "success"}, {"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]
            edit_message(chat_id, msg_id, render_body_text(final_msg), reply_markup={"inline_keyboard": kb})

        except Exception as e:
            edit_message(chat_id, msg_id, render_body_text(f"❌ Error: {e}"), reply_markup={"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]})

    elif data == "lb_main":
        txt = f"━━━━━━━━━━━━━━━\n《 {PEM['admin']} <b>LEADER BOARD MENU</b> 》\n━━━━━━━━━━━━━━━\n<i>Select a category to view the top performers or history.</i>\n━━━━━━━━━━━━━━━"
        kb = [
            [{"text": "Top Referrers", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "lb_top_refs", "style": "primary"}],
            [{"text": "Top OTP Receivers", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "lb_top_otps", "style": "primary"}],
            [{"text": "Withdrawal History", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "lb_w_history", "style": "success"}],
            [{"text": "Back to Admin", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("lb_"):
        sub = data.replace("lb_", "")
        edit_message(chat_id, msg_id, render_body_text("⌛ <i>Fetching Data...</i>"))
        
        num_map = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣", "0": "0️⃣"}
        def get_p_num(n): return "".join([num_map.get(c, c) for c in str(n)])
        
        try:
            if sub == "top_refs":
                title, field, limit, icon = "TOP 5 REFERRERS", "total_refers", 5, PEM.get('user', '👥')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "top_otps":
                title, field, limit, icon = "TOP 5 OTP RECEIVERS", "total_otps", 5, PEM.get('msg', '📩')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "w_history":
                title, limit, icon = "LAST 10 WITHDRAWALS", 10, PEM.get('money', '💸')
                ws = db.collection('withdrawals').order_by('timestamp', direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for w in ws:
                    d = w.to_dict()
                    s = str(d.get('status','Pending')).lower()
                    stat_icon = PEM.get('ok','✅') if s in ["approved","success"] else PEM.get('no','❌') if s=="rejected" else "⏳"
                    uid = d.get('user_id','User')
                    p = "└" if count == limit else "├"
                    res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={uid}'>{uid}</a> ➔ <b>{d.get('amount',0)}৳</b> {stat_icon}\n"
                    count += 1
                if not res_txt: res_txt = "└ <i>No history found.</i>\n"

            final_msg = f"━━━━━━━━━━━━━━━\n{icon} <b>{title}</b>\n━━━━━━━━━━━━━━━\n{res_txt}━━━━━━━━━━━━━━━"
            kb = [[{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": data, "style": "success"}, {"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]
            edit_message(chat_id, msg_id, render_body_text(final_msg), reply_markup={"inline_keyboard": kb})

        except Exception as e:
            edit_message(chat_id, msg_id, render_body_text(f"❌ Error: {e}"), reply_markup={"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]})

    elif data == "back_to_admin":
        if chat_id in user_states: del user_states[chat_id]
        edit_message(chat_id, msg_id, get_admin_text(), reply_markup=admin_panel_keyboard())
        
    elif data == "system_settings":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['gear']} <b>System Settings</b>\nManage advanced bot configurations below:"), reply_markup=system_settings_keyboard())

    elif data == "stex_control":
        edit_message(chat_id, msg_id, render_body_text(f"🌐 <b>StexSMS Control Panel</b>\n\nTotal API Keys: {len(bot_settings.get('stex_keys', []))}\nManage your StexSMS API Keys below:"), reply_markup=stex_control_keyboard())

    elif data == "add_stex_key":
        user_states[chat_id] = "wait_for_add_stex_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new StexSMS API Key (e.g. nxa_...):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "stex_control", "style": "danger"}]]})

    elif data == "view_stex_keys":
        kb = []
        for idx, key in enumerate(bot_settings.get("stex_keys", [])):
            safe_name = key[:10] + "..." if len(key)>10 else key
            kb.append([{"text": f"Delete {safe_name}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_nxa_{idx}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "stex_control", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text("🗑 <b>Select StexSMS Key to Delete:</b>"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_nxa_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings.get("stex_keys", [])):
            del bot_settings["stex_keys"][idx]
            save_db()
            answer_callback(call["id"], "✅ StexSMS Key Deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "view_stex_keys", "id": call["id"]})

    elif data == "manage_stex_srv":
        kb = []
        srvs = bot_settings.get("stex_services", {})
        apps_db = bot_settings.get("premium_apps", {})
        for srv in srvs:
            emoji_id = "5257969839313526622"
            for app_key, app_data in apps_db.items():
                if srv.upper() == app_key or srv.upper() in app_key or app_key in srv.upper():
                    if "id" in app_data:
                        emoji_id = app_data["id"]
                        break
            kb.append([{"text": f"{srv}", "icon_custom_emoji_id": emoji_id, "callback_data": f"nx_srv_{srv}", "style": "primary"}])
        kb.append([{"text": "Add New Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "nx_add_srv", "style": "success"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "stex_control", "style": "danger"}])
        edit_message(chat_id, msg_id, render_body_text("📦 <b>StexSMS Services Manager</b>\nManage your API-based dynamic services below:"), reply_markup={"inline_keyboard": kb})

    elif data == "nx_add_srv":
        user_states[chat_id] = "wait_nx_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Enter Service Name (e.g. TELEGRAM):"), reply_markup=get_cancel_kb())

    elif data.startswith("nx_srv_"):
        srv = data.replace("nx_srv_", "")
        kb = []
        countries = bot_settings["stex_services"].get(srv, {})
        for c in countries:
            _, emoji_id = get_country_flag_info(c)
            emoji_id = emoji_id or "5780471598922337683"
            kb.append([{"text": f"{c} ({len(countries[c])} Ranges)", "icon_custom_emoji_id": emoji_id, "callback_data": f"nx_cnt_{srv}_{c}", "style": "primary"}])
        kb.append([{"text": "Add Country", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"nx_add_cnt_{srv}", "style": "success"}])
        kb.append([{"text": "Delete Service", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"nx_del_srv_{srv}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_stex_srv", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📂 <b>Service: {srv}</b>\nManage countries for this service:"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("nx_add_cnt_"):
        srv = data.replace("nx_add_cnt_", "")
        user_states[chat_id] = "wait_nx_cnt_name"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter Country Name for <b>{srv}</b> (e.g. BD, INDIA):"), reply_markup=get_cancel_kb())

    elif data.startswith("nx_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        ranges = bot_settings["stex_services"][srv].get(cnt, [])
        current_rate = bot_settings.get("stex_service_rates", {}).get(srv, {}).get(cnt, bot_settings.get("otp_reward", 0.0))
        
        kb = []
        row = []
        for r in ranges:
            row.append({"text": f"Delete {r}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"nx_dr_{srv}_{cnt}_{r}", "style": "danger"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row: kb.append(row)
        
        kb.append([{"text": "Add Range", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"nx_addr_{srv}_{cnt}", "style": "success"}])
        kb.append([{"text": f"Set Rate ({current_rate} TK/OTP)", "icon_custom_emoji_id": "5190576863226933563", "callback_data": f"nx_setrate_{srv}_{cnt}", "style": "primary"}])
        kb.append([{"text": "Delete Entire Country", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"nx_del_cnt_{srv}_{cnt}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"nx_srv_{srv}", "style": "primary"}])
        
        txt = f"📍 <b>Service: {srv} | Country: {cnt}</b>\n\n<b>Total Ranges:</b> {len(ranges)}\n💰 <b>OTP Rate:</b> {current_rate} TK/OTP\n<i>Click on a range below to delete it, or add a new one.</i>"
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("nx_setrate_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_nx_cnt_rate"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['money']} Enter new OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"nx_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("nx_addr_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_nx_addr"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"📝 Send the new Range for <b>{cnt}</b> (e.g. 88017):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"nx_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("nx_dr_"):
        parts = data.split("_")
        srv, cnt, rng = parts[2], parts[3], parts[4]
        if rng in bot_settings["stex_services"].get(srv, {}).get(cnt, []):
            bot_settings["stex_services"][srv][cnt].remove(rng)
            save_db()
            answer_callback(call["id"], f"✅ Range {rng} deleted!", show_alert=True)
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"nx_cnt_{srv}_{cnt}", "id": call["id"]})

    elif data.startswith("nx_del_srv_"):
        srv = data.replace("nx_del_srv_", "")
        if srv in bot_settings["stex_services"]: del bot_settings["stex_services"][srv]
        if srv in bot_settings.get("stex_service_rates", {}): del bot_settings["stex_service_rates"][srv]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_stex_srv", "id": call["id"]})

    elif data.startswith("nx_del_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[3], parts[4]
        if cnt in bot_settings["stex_services"].get(srv, {}): del bot_settings["stex_services"][srv][cnt]
        if cnt in bot_settings.get("stex_service_rates", {}).get(srv, {}): del bot_settings["stex_service_rates"][srv][cnt]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"nx_srv_{srv}", "id": call["id"]})

    elif data == "manage_fj":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['link']} <b>FORCE JOIN SYSTEM</b>\nManage channels below:"), reply_markup=fj_settings_keyboard())

    elif data == "toggle_fj":
        bot_settings["fj_on"] = not bot_settings["fj_on"]
        save_db()
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['link']} <b>FORCE JOIN SYSTEM</b>\nManage channels below:"), reply_markup=fj_settings_keyboard())

    elif data == "add_fj":
        user_states[chat_id] = "wait_for_add_fj"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send Channel Username or Invite Link:\n<i>(Note: For private channels, use the numeric ID like -100...)</i>"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_fj", "style": "danger"}]]})

    elif data.startswith("del_fj_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["fj_channels"]):
            del bot_settings["fj_channels"][idx]
            save_db()
            answer_callback(call["id"], "✅ Channel deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text(f"{PEM['link']} <b>FORCE JOIN SYSTEM</b>\nManage channels below:"), reply_markup=fj_settings_keyboard())

    elif data == "manage_admins":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['user']} <b>ADMIN MANAGEMENT</b>\nManage your bot admins below:"), reply_markup=admin_settings_keyboard())

    elif data == "add_adm":
        user_states[chat_id] = "wait_for_add_adm"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID of the new Admin:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_admins", "style": "danger"}]]})

    elif data.startswith("del_adm_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["admins"]):
            del bot_settings["admins"][idx]
            save_db()
            answer_callback(call["id"], "✅ Admin deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text(f"{PEM['user']} <b>ADMIN MANAGEMENT</b>\nManage your bot admins below:"), reply_markup=admin_settings_keyboard())

    elif data == "manage_otp_groups":
        edit_message(chat_id, msg_id, render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())

    elif data == "add_fw":
        user_states[chat_id] = "wait_for_add_fw_id"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the Group ID/Username to forward messages to:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_otp_groups", "style": "danger"}]]})

    elif data.startswith("manage_fw_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["fw_groups"]):
            grp_id = bot_settings["fw_groups"][idx]["chat_id"]
            edit_message(chat_id, msg_id, render_body_text(f"🛡 <b>Manage Group:</b> {grp_id}"), reply_markup=specific_fw_group_keyboard(idx))

    elif data.startswith("add_fwbtn_"):
        idx = int(data.split("_")[2])
        user_states[chat_id] = "wait_for_add_fw_btn"
        temp_data[chat_id] = {"msg_id": msg_id, "fw_idx": idx}
        edit_message(chat_id, msg_id, render_body_text("📝 Send Custom Inline Button format:\n<code>Button Text - https://link.com</code>"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"manage_fw_{idx}", "style": "danger"}]]})

    elif data.startswith("del_fwbtn_"):
        parts = data.split("_")
        idx, b_idx = int(parts[2]), int(parts[3])
        if 0 <= idx < len(bot_settings["fw_groups"]):
            if 0 <= b_idx < len(bot_settings["fw_groups"][idx]["buttons"]):
                del bot_settings["fw_groups"][idx]["buttons"][b_idx]
                save_db()
                answer_callback(call["id"], "✅ Button deleted!", show_alert=True)
                edit_message(chat_id, msg_id, render_body_text(f"🛡 <b>Manage Group:</b> {bot_settings['fw_groups'][idx]['chat_id']}"), reply_markup=specific_fw_group_keyboard(idx))

    elif data.startswith("del_fw_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["fw_groups"]):
            del bot_settings["fw_groups"][idx]
            save_db()
            answer_callback(call["id"], "✅ Group deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())

    elif data == "edit_otp_link":
        user_states[chat_id] = "wait_for_otp_link"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new OTP Group Link:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_otp_groups", "style": "danger"}]]})

    elif data == "TGZ_control":
        if chat_id in user_states: del user_states[chat_id]
        edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())

    elif data == "TGZ_toggle_w":
        bot_settings["withdraw_on"] = not bot_settings["withdraw_on"]
        save_db()
        edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())

    elif data == "manage_w_methods":
        edit_message(chat_id, msg_id, render_body_text("💳 <b>WITHDRAWAL METHODS</b>\n\nManage your withdrawal methods below:"), reply_markup=w_methods_keyboard())

    elif data == "add_wm":
        user_states[chat_id] = "wait_for_add_wm"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the name of the new Withdrawal Method:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_w_methods", "style": "danger"}]]})

    elif data.startswith("del_wm_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["w_methods"]):
            del bot_settings["w_methods"][idx]
            save_db()
            answer_callback(call["id"], "✅ Method deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text("💳 <b>WITHDRAWAL METHODS</b>\n\nManage your withdrawal methods below:"), reply_markup=w_methods_keyboard())

    elif data.startswith("TGZ_"):
        key = data.replace("TGZ_", "")
        key_map = {"min_w": "min_withdraw", "otp_r": "otp_reward", "ref_r": "refer_reward", "cool": "cooldown", "num_req": "num_req", "num_share": "num_share", "sup_link": "support_link", "w_group": "w_group", "api_url": "api_base_url"}
        if key in key_map:
            temp_data[chat_id] = {"msg_id": msg_id, "key": key_map[key]}
            user_states[chat_id] = "set_TGZ"
            cancel_kb = {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_TGZ_edit", "style": "danger"}]]}
            prompt = f"📝 Please send the new value for <code>{key_map[key]}</code>:"
            edit_message(chat_id, msg_id, render_body_text(prompt), reply_markup=cancel_kb)
            answer_callback(call["id"])

    elif data == "back_to_services":
        local_srvs = set([b["service"] for b in number_batches.values() if b["numbers"]])
        stex_srvs = set(bot_settings.get("stex_services", {}).keys())
        all_services = local_srvs.union(stex_srvs)
        
        if not all_services:
            answer_callback(call["id"], "❌ No numbers or services available!", show_alert=True)
            return
            
        c_msg = bot_settings["custom_messages"].get("get_number", {})
        txt = render_body_text(c_msg.get("text", f"{PEM['pin']} Select Service"))
        
        apps_db = bot_settings.get("premium_apps", {})
        kb = []
        for s in all_services:
            emoji_id = "5352694861990501856" 
            for app_key, app_data in apps_db.items():
                if s.upper() == app_key or s.upper() in app_key or app_key in s.upper():
                    if "id" in app_data:
                        emoji_id = app_data["id"]
                        break
            kb.append([{"text": f"{s}", "icon_custom_emoji_id": emoji_id, "callback_data": f"g_s_{s}", "style": "primary"}])
        
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        kb.append([{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        
        edit_message(chat_id, msg_id, txt, reply_markup={"inline_keyboard": kb})

    elif data.startswith("g_s_"):
        service = data.split("g_s_")[1]
        local_cnts = set([b["country"] for b in number_batches.values() if b["service"] == service and b["numbers"]])
        stex_cnts = set(bot_settings.get("stex_services", {}).get(service, {}).keys())
        all_countries = local_cnts.union(stex_cnts)
        
        c_msg = bot_settings["custom_messages"].get("select_country", {})
        raw_txt = c_msg.get("text", "📌 Select a country for {service}:").replace("{service}", service)
        txt = render_body_text(raw_txt)
        
        kb = []
        for c in all_countries:
            _, emoji_id = get_country_flag_info(c)
            if not emoji_id:
                emoji_id = "5780471598922337683"
            
            otp_price = bot_settings.get("otp_reward", 0.0)
            # 🌟 ম্যানুয়ালি অ্যাড করা Voltx/StexSMS/Zebrasms/Yesms সার্ভিসে কাস্টম রেট সেট করা থাকলে সেটা নিবে (Auto Range গুলো এডমিনের গ্লোবাল OTP REWARD রেটই ব্যবহার করবে)
            if c in stex_cnts and not IS_CHILD:
                custom_rate = bot_settings.get("stex_service_rates", {}).get(service, {}).get(c)
                if custom_rate is not None: otp_price = custom_rate
            # 🌟 ফাইলের নির্দিষ্ট কোনো রেট থাকলে সেটা নিয়ে আসবে (সবচেয়ে বেশি প্রায়রিটি)
            for b in number_batches.values():
                if b.get("service") == service and b.get("country") == c and "rate" in b and b.get("numbers"):
                    otp_price = b.get("rate")
                    break
                    
            kb.append([{"text": f"{c} - {otp_price} TK/OTP", "icon_custom_emoji_id": emoji_id, "callback_data": f"g_c_{service}_{c}", "style": "success"}])
        
        
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
            
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_services", "style": "danger"}])
        edit_message(chat_id, msg_id, txt, reply_markup={"inline_keyboard": kb})

    elif data.startswith("g_c_") or data.startswith("c_n_"):
        # ১. গ্লোবাল কুলডাউন চেক (সকল নাম্বার মেথডের জন্য)
        now = time.time()
        if now - user_cooldowns.get(chat_id, 0) < bot_settings["cooldown"]:
            answer_callback(call["id"], f"⌛ Please wait {int(bot_settings['cooldown'] - (now - user_cooldowns.get(chat_id, 0)))}s.", show_alert=True)
            return
        
        # কুলডাউন আপডেট
        user_cooldowns[chat_id] = now
        
        # আগের নাম্বার এক্সপায়ার করা
        expire_previous_number(chat_id)

        # যদি সার্চ নাম্বার থেকে আসে
        if data.startswith("c_n_s_"):
            parts_s = data.split("_", 4)
            query = parts_s[3] if len(parts_s) > 3 else ""
            service_from_cb = parts_s[4] if len(parts_s) > 4 else None
                
            edit_message(chat_id, msg_id, render_body_text("⌛ <i>Processing... Finding Number...</i>"))
            wait_msg_id = msg_id
            
            found_indices = []
            for b_id, b_data in number_batches.items():
                for idx, n_obj in enumerate(b_data["numbers"]):
                    if n_obj["num"].replace("+", "").startswith(query) and chat_id not in n_obj.get("used_by", []):
                        found_indices.append((b_id, idx))
            
            fetched_nums = []
            if not found_indices:
                api_found = False
                req_count = bot_settings.get("num_req", 1)
                
                # StexSMS থেকে নাম্বার নেওয়া (parallel + keep-alive => super fast)
                if len(fetched_nums) < req_count:
                    need = req_count - len(fetched_nums)
                    got = list(_fetch_pool.map(lambda _i: stex_fetch(query)[0], range(need)))
                    for num_str in got:
                        if not num_str or num_str in fetched_nums:
                            continue
                        fetched_nums.append(num_str)
                        stex_assigned_numbers[num_str] = chat_id
                        assigned_number_rates[num_str] = resolve_manual_service_rate("stex", service_from_cb, query)
                        api_found = True
                        total_assigned_stats += 1

                if not api_found:
                    answer_callback(call["id"], "❌ Number out of stock!", show_alert=True)
                    delete_message(chat_id, wait_msg_id)
                    return
                save_db()
            else:
                random.shuffle(found_indices)
                for b_id, idx in found_indices:
                    if len(fetched_nums) >= bot_settings.get("num_req", 1): break
                    n_obj = number_batches[b_id]["numbers"][idx]
                    num_str = n_obj["num"]
                    fetched_nums.append(num_str)
                    
                    # 🌟 Save File Specific Rate
                    assigned_number_rates[num_str] = float(number_batches[b_id].get("rate", bot_settings.get("otp_reward", 0.0)))
                    
                    n_obj["shares"] += 1
                    n_obj["used_by"].append(chat_id)
                    total_assigned_stats += 1
                    if n_obj["shares"] >= bot_settings.get("num_share", 1):
                        n_obj["to_remove"] = True
                        used_numbers_list.append(num_str)
                for b_id in number_batches:
                    number_batches[b_id]["numbers"] = [n for n in number_batches[b_id]["numbers"] if not n.get("to_remove")]
                save_db()
                
            kb = []
            if service_from_cb:
                app_full_name, _ = get_service_info_html(service_from_cb)
                emoji_id_srv = "5337302974806922068"
                for app_key, app_data in bot_settings.get("premium_apps", {}).items():
                    if service_from_cb.upper() == app_key or service_from_cb.upper() in app_key or app_key in service_from_cb.upper():
                        if "id" in app_data: emoji_id_srv = app_data["id"]; break
                kb.append([{"text": f"{app_full_name}", "icon_custom_emoji_id": emoji_id_srv, "callback_data": "ignore", "style": "primary"}])

            flags_db = bot_settings.get("premium_flags", {})
            country_emoji_html = ""
            for num in fetched_nums:
                char_flag, iso = get_flag_and_code(num)
                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                flag_emoji_id = "5780471598922337683"
                for flag_code, flag_data in flags_db.items():
                    if iso == flag_data.get("iso"):
                        if "id" in flag_data: 
                            flag_emoji_id = flag_data["id"]
                            country_emoji_html = f'<tg-emoji emoji-id="{flag_emoji_id}">{flag_data["char"]}</tg-emoji>'
                        break
                if not country_emoji_html: country_emoji_html = char_flag
                kb.append([{"text": f"{display_num}", "icon_custom_emoji_id": flag_emoji_id, "copy_text": {"text": display_num}, "style": "success"}])
            
            vtx_ext = ""
            srv_ext = f"_{service_from_cb}" if service_from_cb else ""
            kb.append([{"text": "Change Number", "icon_custom_emoji_id": "5465368548702446780", "callback_data": f"c_n_s_{query}{srv_ext}{vtx_ext}", "style": "danger"}])
            
            if service_from_cb:
                kb.append([{"text": "Change Country", "icon_custom_emoji_id": "5305517382138112561", "callback_data": f"g_s_{service_from_cb}", "style": "danger"}])
                
            kb.append([{"text": "OTP Group", "icon_custom_emoji_id": "5190447043545438788", "url": bot_settings["otp_link"], "style": "primary"}])
            
            text_numbers = render_body_text(f"{country_emoji_html} NEW NUMBER\n")
            # Change Number করলে পুরনো মেসেজ ডিলিট হয়ে নতুন মেসেজে নতুন নাম্বার আসবে
            delete_message(chat_id, wait_msg_id)
            msg_res = send_message(chat_id, text_numbers, reply_markup={"inline_keyboard": kb})
            new_msg_id = msg_res["result"]["message_id"] if msg_res and "result" in msg_res else wait_msg_id
            user_active_sessions[chat_id] = {
                "msg_id": new_msg_id, "nums": fetched_nums,
            }
            return

        # যদি আপলোড করা বা সার্ভিস থেকে আসে
        parts = data.split("_")
        service = parts[2]
        country = parts[3]

        available_indices = []
        # Check Local Stock First
        for b_id, b_data in number_batches.items():
            if b_data["service"] == service and b_data["country"] == country:
                for idx, n_obj in enumerate(b_data["numbers"]):
                    if chat_id not in n_obj.get("used_by", []):
                        available_indices.append((b_id, idx))

        # IF NO LOCAL STOCK, Check StexSMS Services
        if not available_indices:
            stex_srv_data = bot_settings.get("stex_services", {}).get(service, {}).get(country)
            target_range = None
            if stex_srv_data and len(stex_srv_data) > 0:
                target_range = random.choice(stex_srv_data)
            if target_range:
                user_cooldowns[chat_id] = 0
                handle_callback({"message": call["message"], "data": f"c_n_s_{target_range}_{service}", "id": call["id"]})
                return
            else:
                answer_callback(call["id"], "❌ Number out of stock or range missing!", show_alert=True)
                if data.startswith("c_n_"): delete_message(chat_id, msg_id)
                return

        random.shuffle(available_indices)
        
        fetched_nums = []
        for b_id, idx in available_indices:
            if len(fetched_nums) >= bot_settings["num_req"]: break
            n_obj = number_batches[b_id]["numbers"][idx]
            
            fetched_nums.append(n_obj["num"])
            
            # 🌟 Save File Specific Rate
            assigned_number_rates[n_obj["num"]] = float(number_batches[b_id].get("rate", bot_settings.get("otp_reward", 0.0)))
            
            n_obj["shares"] += 1
            n_obj["used_by"].append(chat_id)
            total_assigned_stats += 1
            
            if n_obj["shares"] >= bot_settings.get("num_share", 1):
                n_obj["to_remove"] = True
                used_numbers_list.append(n_obj["num"])

        for b_id in number_batches:
            number_batches[b_id]["numbers"] = [n for n in number_batches[b_id]["numbers"] if not n.get("to_remove")]
        save_db()

        if not fetched_nums:
            answer_callback(call["id"], "❌ You have already taken all numbers or stock is empty!", show_alert=True)
            if data.startswith("c_n_"): delete_message(chat_id, msg_id)
            return

        app_full_name, _ = get_service_info_html(service)
        emoji_id = "5337302974806922068"
        apps_db = bot_settings.get("premium_apps", {})
        for app_key, app_data in apps_db.items():
            if service.upper() == app_key or service.upper() in app_key or app_key in service.upper():
                if "id" in app_data:
                    emoji_id = app_data["id"]
                    break
        kb = [[{"text": f"{app_full_name}", "icon_custom_emoji_id": emoji_id, "callback_data": "ignore", "style": "primary"}]]
        
        flags_db = bot_settings.get("premium_flags", {})
        country_emoji_html = ""
        for num in fetched_nums:
            char_flag, iso = get_flag_and_code(num)
            display_num = f"+{num}" if not num.startswith("+") else num
            
            flag_emoji_id = "5780471598922337683" # Default Flag
            for flag_code, flag_data in flags_db.items():
                if iso == flag_data.get("iso"):
                    if "id" in flag_data: 
                        flag_emoji_id = flag_data["id"]
                        country_emoji_html = f'<tg-emoji emoji-id="{flag_emoji_id}">{flag_data["char"]}</tg-emoji>'
                    break
            
            if not country_emoji_html: country_emoji_html = char_flag
            kb.append([{"text": f"{display_num}", "icon_custom_emoji_id": flag_emoji_id, "copy_text": {"text": display_num}, "style": "success"}])
            
        kb.append([{"text": "Change Number", "icon_custom_emoji_id": "5465368548702446780", "callback_data": f"c_n_{service}_{country}", "style": "danger"}])
                   
        c_btns = bot_settings["custom_messages"].get("get_number", {}).get("buttons", [])
        for c_b in c_btns: 
            b_copy = c_b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
            
        kb.append([{"text": "Change Country", "icon_custom_emoji_id": "5305517382138112561", "callback_data": f"g_s_{service}", "style": "danger"}])
        kb.append([{"text": "OTP Group", "icon_custom_emoji_id": "5190447043545438788", "url": bot_settings["otp_link"], "style": "primary"}])
        
        text_numbers = render_body_text(f"{country_emoji_html} NEW NUMBER\n")
        # Change Number করলে পুরনো মেসেজ ডিলিট হয়ে নতুন মেসেজে নতুন নাম্বার আসবে
        delete_message(chat_id, msg_id)
        msg_res = send_message(chat_id, text_numbers, reply_markup={"inline_keyboard": kb})
        if msg_res and "result" in msg_res:
            user_active_sessions[chat_id] = {
                "msg_id": msg_res["result"]["message_id"], "nums": fetched_nums,
            }

    elif data.startswith("wapp_") or data.startswith("wrej_"):
        # অ্যাডমিন চেক (User ID চেক করতে হবে)
        user_id_clicked = call["from"]["id"]
        if not is_admin(user_id_clicked):
            answer_callback(call["id"], "🚫 Only Bot Admins can process withdrawals!", show_alert=True)
            return
            
        action = "APPROVE" if data.startswith("wapp_") else "REJECT"
        req_id = data.replace("wapp_", "").replace("wrej_", "")
        
        if req_id in pending_withdrawals:
            req_data = pending_withdrawals[req_id]
            u_id, amt = req_data["user_id"], req_data["amount"]
            num = req_data["number"]
            full_name = req_data.get("full_name", u_id)
            
            if action == "APPROVE" and len(num) >= 7:
                masked_num = f"{num[:4]}❖TGZ❖{num[-3:]}"
            else:
                masked_num = num
            
            status_text = "APPROVED" if action == "APPROVE" else "REJECTED"
            emoji_icon_id = "5352694861990501856" if action == "APPROVE" else "5420130255174145507"
            new_text = f"🎙 <b>WITHDRAWAL {status_text}</b>\n\n👤 <b>USER:</b> <a href='tg://user?id={u_id}'>{full_name}</a>\n💳 <b>WITHDRAWAL:</b> {amt} TK\n🍏 <b>NUMBER:</b> <code>{masked_num}</code>\n🏦 <b>METHOD:</b> {req_data['method']}\n\n🧾 <b>REQ ID:</b> {req_id}\n👨‍⚖️ <b>PROCESSED BY ADMIN</b>"
            
            kb = {"inline_keyboard": [[{"text": status_text, "icon_custom_emoji_id": emoji_icon_id, "callback_data": "ignore", "style": "success" if action == "APPROVE" else "danger"}]]}
            edit_message(chat_id, msg_id, render_body_text(new_text), reply_markup=kb)
            
            if action == "REJECT":
                update_balance(u_id, amt) 
                send_message(u_id, render_body_text(f"❌ Your {amt} TK withdrawal request was rejected. Balance refunded."))
            else:
                send_message(u_id, render_body_text(f"{PEM['ok']} Your {amt} TK withdrawal request has been paid successfully!"))
            
            if db:
                try: db.collection('withdrawals').document(req_id).update({"status": "approved" if action == "APPROVE" else "rejected"})
                except: pass
                
            del pending_withdrawals[req_id]
        else:
            answer_callback(call["id"], "❌ Request already processed!", show_alert=True)

# ==========================================
# Polling Loop
# ==========================================
def poll_otp_with_status(number_id, num_str, owner_id, api_key):
    headers = {"X-API-Key": api_key}
    for _ in range(150): # 150 * 4 seconds = 10 Minutes Polling
        try:
            res = requests.get(f"{STEX_BASE_URL}/api/v1/numbers/{number_id}/sms", headers=headers, timeout=10)
            data = res.json()
            if data.get("success") and data.get("otp"):
                otp = str(data["otp"])
                msg_text = data.get("message", f"Your code is {otp}")
                
                # 🌟 সম্পূর্ণ মেসেজ থেকে ড্যাশসহ বা বড় OTP খোঁজার ফিক্স
                extracted_otp = extract_otp_code(msg_text)
                if extracted_otp and len(extracted_otp) > len(otp):
                    otp = extracted_otp
                    
                # 🌟 সম্পূর্ণ মেসেজ থেকে সার্ভিস/অ্যাপ চেনার ফিক্স
                app_name = data.get("service", "StexSMS Service")
                detected_app = detect_service(msg_text)
                if detected_app:
                    app_name = detected_app
                
                if claim_processed_otp(num_str, otp, [f"POLL_{number_id}_{otp}"]):
                    
                    char, iso = get_flag_and_code(num_str)
                    app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                    
                    save_db()
                    
                    display_num = f"+{num_str}" if not str(num_str).startswith("+") else str(num_str)
                    masked = mask_number(display_num)
                    lang = detect_language(msg_text)
                    
                    display_msg = render_body_text(f"╔═══════════════╗\n║ {prem_app_html} {get_flag_info_html(display_num)} {masked} {lang}\n╚═══════════════╝")
                    
                    for fw in bot_settings.get("fw_groups", []):
                        kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                        temp_row = []
                        styles = ["danger", "success", "primary"]
                        for i, btn in enumerate(fw.get("buttons", [])):
                            b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                            if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                            temp_row.append(b_obj)
                            if len(temp_row) == 2:
                                kb.append(temp_row)
                                temp_row = []
                        if temp_row: kb.append(temp_row)
                        send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                    
                    inbox_msg = render_body_text(f"╔═══════════════╗\n║ {prem_app_html} {get_flag_info_html(display_num)} {display_num} {lang}\n╚═══════════════╝")
                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                    
                    reward = float(bot_settings.get("otp_reward", 0.0)) if IS_CHILD else float(assigned_number_rates.get(num_str, bot_settings.get("otp_reward", 0.0)))
                    if reward > 0:
                        update_balance(owner_id, reward)
                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                    
                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                    
                    if db:
                        bump_otp_counters(owner_id)
                break
        except: pass
        time.sleep(4)

POLL_INTERVAL = 1.0                      # seconds between provider polls
_poll_pool = ThreadPoolExecutor(max_workers=16)
_fetch_pool = ThreadPoolExecutor(max_workers=16)

def _digits(x):
    return re.sub(r"\D", "", str(x or ""))

def _same_number(a, b):
    a, b = _digits(a), _digits(b)
    if not a or not b:
        return False
    return a == b or (len(a) >= 8 and len(b) >= 8 and (a.endswith(b[-8:]) or b.endswith(a[-8:])))

def _find_otp_owner(num):
    """Who owns this number?  API numbers first (exact), then bot sessions."""
    try:
        with _api_lock:
            found = _find_api_number(num)
        if found and found[1].get("uid") is not None:
            return found[1]["uid"]
    except Exception:
        pass
    for uid, sd in list(user_active_sessions.items()):
        for n in sd.get("nums", []):
            if _same_number(n, num):
                return uid
    for n, o in list(stex_assigned_numbers.items()):
        if _same_number(n, num):
            return o
    return None

def _forward_to_groups(display_msg, otp):
    for fw in list(bot_settings.get("fw_groups", [])):
        kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
        temp_row = []
        styles = ["danger", "success", "primary"]
        for i, btn in enumerate(fw.get("buttons", [])):
            b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
            if "icon_custom_emoji_id" in btn:
                b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
            temp_row.append(b_obj)
            if len(temp_row) == 2:
                kb.append(temp_row)
                temp_row = []
        if temp_row:
            kb.append(temp_row)
        send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})

def _handle_sms_item(item):
    num = _digits(item.get("number"))
    if not num:
        return
    msg_text = str(item.get("message", ""))
    # ignore very old hits (e.g. after a restart) - never replay history
    try:
        ts = item.get("time")
        if ts and (time.time() * 1000 - float(ts)) > API_NUMBER_TTL * 1000:
            return
    except Exception:
        pass
    otp = extract_otp_code(msg_text)
    if not otp:
        m = re.search(r"\d{3,8}", msg_text)
        otp = m.group(0) if m else str(item.get("otp_id") or "CODE")
    otp = str(otp)
    if not claim_processed_otp(num, otp):
        return

    app_name = detect_service(msg_text) or "Stex Service"

    # ---- 1) FASTEST PATH: hand the OTP to the owner / API waiters first ----
    owner_id = _find_otp_owner(num)
    if owner_id is not None:
        try:
            api_on_otp(num, owner_id, otp, msg_text, app_name)
        except Exception as api_exc:
            print(f"API OTP hook error: {api_exc}")

    # ---- 2) cards (inbox + forward groups) ----
    char, iso = get_flag_and_code(num)
    app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
    display_num = f"+{num}"
    masked = mask_number(display_num)
    lang = detect_language(msg_text)
    lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
    display_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
    threading.Thread(target=_forward_to_groups, args=(display_msg, otp), daemon=True).start()

    if owner_id is not None:
        inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
        inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
        reward = float(bot_settings.get("otp_reward", 0.0)) if IS_CHILD else float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
        if reward > 0:
            update_balance(owner_id, reward)
            inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
        send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
        bump_otp_counters(owner_id)
    save_db()

def _poll_stex_key(api_key):
    try:
        res = stex_http.get(f"{STEX_BASE_URL}/success-otp", headers={"mauthapi": api_key}, timeout=(3, 6))
        rd = res.json()
    except Exception:
        return
    if (rd.get("meta") or {}).get("code") != 200:
        return
    items = (rd.get("data") or {}).get("otps") or []
    for item in reversed(items):          # oldest first
        try:
            _handle_sms_item(item)
        except Exception as e:
            print(f"SMS item error: {e}")

def global_sms_listener():
    while True:
        t0 = time.time()
        try:
            keys = list(bot_settings.get("stex_keys", []))
            futs = [_poll_pool.submit(_poll_stex_key, k) for k in keys]   # all keys in PARALLEL
            for f in futs:
                try:
                    f.result(timeout=15)
                except Exception:
                    pass
        except Exception as e:
            print(f"Listener error: {e}")
        time.sleep(max(0.2, POLL_INTERVAL - (time.time() - t0)))

# ==========================================
# 🌐 USER API SYSTEM (StexSMS-style HTTP API, one key per user)
# Numbers come from the admin's StexSMS keys for the ranges the admin added
# under  Admin Panel -> API Range  (separate from the bot's service system).
# The OTP reward of the range is credited to the user whose key took the
# number, and the number + OTP also appear in that user's bot inbox.
# ==========================================
import secrets
import ipaddress
import socket
from collections import deque
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from flask import request, jsonify

API_NUMBER_TTL = 30 * 60        # seconds a number stays linked to the user
API_LONGPOLL_MAX = 30           # max seconds for /otp?wait=
_api_lock = threading.RLock()
_otp_cv = threading.Condition()
api_key_index = {}              # api_key -> user_id
api_hits = deque(maxlen=500)    # recent hits for /console

# keep-alive pool to Stex => the fastest possible getnum
stex_http = requests.Session()
stex_http.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=256))
try:
    health_check_web.json.sort_keys = False      # keep meta -> data -> message -> rid order (same as provider)
except Exception:
    health_check_web.config["JSON_SORT_KEYS"] = False

def stex_fetch(rid):
    """Take ONE number for `rid` from the provider keys. -> (no_plus_number, provider_data) or (None, None)."""
    keys = list(bot_settings.get("stex_keys", []))
    random.shuffle(keys)
    for k in keys:
        try:
            r = stex_http.post(f"{STEX_BASE_URL}/getnum", json={"rid": rid},
                               headers={"mauthapi": k}, timeout=(3, 8))
            rd = r.json()
            if (rd.get("meta") or {}).get("code") == 200 and rd.get("data"):
                d = rd["data"]
                num = str(d.get("no_plus_number", "")).replace("+", "") or str(d.get("national_number", ""))
                if num:
                    return num, d
        except Exception:
            continue
    return None, None

_prov_cache = {}
def _provider_get(path, ttl):
    """Cached GET on the provider (liveaccess / console). -> (data, from_cache) or (None, False)."""
    now = time.time()
    c = _prov_cache.get(path)
    if c and now - c[0] < ttl:
        return c[1], True
    keys = list(bot_settings.get("stex_keys", []))
    random.shuffle(keys)
    for k in keys:
        try:
            r = stex_http.get(f"{STEX_BASE_URL}/{path}", headers={"mauthapi": k}, timeout=(3, 6))
            rd = r.json()
            if (rd.get("meta") or {}).get("code") == 200 and isinstance(rd.get("data"), dict):
                _prov_cache[path] = (now, rd["data"])
                return rd["data"], False
        except Exception:
            continue
    return (c[1], True) if c else (None, False)

# debounced saver: API traffic never writes the whole DB on every request
_api_dirty = threading.Event()
def api_mark_dirty():
    _api_dirty.set()

def api_flush_thread():
    while True:
        _api_dirty.wait()
        time.sleep(2)
        _api_dirty.clear()
        save_db()

# ==========================================
# 🌐 AUTO BASE URL  (no manual setup needed on any host)
# Priority:
#   1) Admin panel "API URL" (manual override, optional)
#   2) PUBLIC_BASE_URL env var (optional)
#   3) Hosting-platform env vars (Render, Railway, Koyeb, Fly, Heroku, HF Spaces, Replit, Codespaces, Azure)
#   4) Auto-detected from the first real HTTP request that reaches this server
#      (Render/UptimeRobot health checks, browser visit, API call ...) - saved to disk
#   5) Server public IP + PORT  (VPS / no domain)
# So the Base URL shown in the bot is always a real, working address.
# ==========================================
# Fallback shown in the bot when nothing else is known yet (set env DEFAULT_PUBLIC_BASE to change).
DEFAULT_PUBLIC_BASE = os.environ.get("DEFAULT_PUBLIC_BASE", "https://sidbuyermukter-rc-iasin.4h4.zero-bot.org").strip().rstrip("/")
_detected_base = ""
_public_ip_cache = {"value": "", "ts": 0.0}

def _with_scheme(value):
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value

def _platform_base_from_env():
    e = os.environ.get
    if e("RENDER_EXTERNAL_URL"):
        return _with_scheme(e("RENDER_EXTERNAL_URL"))
    if e("RAILWAY_PUBLIC_DOMAIN"):
        return _with_scheme(e("RAILWAY_PUBLIC_DOMAIN"))
    if e("RAILWAY_STATIC_URL"):
        return _with_scheme(e("RAILWAY_STATIC_URL"))
    if e("KOYEB_PUBLIC_DOMAIN"):
        return _with_scheme(e("KOYEB_PUBLIC_DOMAIN"))
    if e("FLY_APP_NAME"):
        return f"https://{e('FLY_APP_NAME')}.fly.dev"
    if e("HEROKU_APP_NAME"):
        return f"https://{e('HEROKU_APP_NAME')}.herokuapp.com"
    if e("SPACE_HOST"):
        return _with_scheme(e("SPACE_HOST"))
    if e("REPLIT_DEV_DOMAIN"):
        return _with_scheme(e("REPLIT_DEV_DOMAIN"))
    if e("CODESPACE_NAME") and e("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"):
        port = e("PORT", "10000")
        return f"https://{e('CODESPACE_NAME')}-{port}.{e('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN')}"
    if e("WEBSITE_HOSTNAME"):
        return _with_scheme(e("WEBSITE_HOSTNAME"))
    return ""

def _base_cache_file():
    path = MAIN_DATA_FILE if (IS_CHILD and MAIN_DATA_FILE) else DATA_FILE
    return os.path.abspath(path) + ".public_base"

def _is_private_host(host):
    hostname = re.sub(r":\d+$", "", (host or "").strip().lower()).strip("[]")
    if not hostname or hostname in ("localhost", "0.0.0.0", "::1", "::"):
        return True
    if hostname.endswith((".local", ".internal", ".localhost", ".svc", ".cluster.local")):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
    except ValueError:
        return "." not in hostname   # bare internal service names like "web"

def _remember_base(base):
    global _detected_base
    if not base or base == _detected_base:
        return
    _detected_base = base
    try:
        with open(_base_cache_file(), "w", encoding="utf-8") as fp:
            fp.write(base)
    except Exception:
        pass

@health_check_web.before_request
def _auto_detect_public_base():
    # Learn the real public address from any incoming request (main bot only).
    if IS_CHILD:
        return None
    try:
        host = ((request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
                or (request.headers.get("Host") or "").strip())
        if not host or _is_private_host(host):
            return None
        proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if proto not in ("http", "https"):
            proto = "https" if '"https"' in (request.headers.get("CF-Visitor") or "") else ""
        if not proto:
            raw_ip = re.sub(r":\d+$", "", host)
            is_ip = bool(re.fullmatch(r"[\d.]+|\[[0-9a-fA-F:]+\]", raw_ip))
            proto = "http" if (is_ip or re.search(r":\d+$", host)) else "https"
        _remember_base(f"{proto}://{host}")
    except Exception:
        pass
    return None

def _server_ip_base():
    now = time.time()
    if not _public_ip_cache["value"] or now - _public_ip_cache["ts"] > 3600:
        ip = ""
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
            try:
                ip = requests.get(url, timeout=3).text.strip()
                if re.fullmatch(r"[\d.]+", ip):
                    break
                ip = ""
            except Exception:
                ip = ""
        _public_ip_cache.update(value=ip, ts=now)
    ip = _public_ip_cache["value"]
    port = os.environ.get("PORT") or os.environ.get("SERVER_PORT") or "10000"
    sip = (os.environ.get("SERVER_IP") or "").strip()
    if sip and not _is_private_host(sip):
        ip = sip
    return f"http://{ip}:{port}" if ip else f"http://localhost:{port}"

def get_public_base():
    base = (CUSTOM_API_BASE_URL or bot_settings.get("api_base_url") or os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base.endswith(API_PATH):
        base = base[:-len(API_PATH)].rstrip("/")
    if base and "YOUR-DOMAIN" not in base.upper():
        return _with_scheme(base)
    base = _platform_base_from_env()
    if base:
        return base
    if _detected_base:
        return _detected_base
    try:
        with open(_base_cache_file(), "r", encoding="utf-8") as fp:
            cached = fp.read().strip().rstrip("/")
        if cached:
            return cached
    except Exception:
        pass
    if DEFAULT_PUBLIC_BASE:
        return DEFAULT_PUBLIC_BASE
    return _server_ip_base()

@health_check_web.route(API_PATH, methods=["GET", "OPTIONS"])
@health_check_web.route(API_PATH + "/", methods=["GET", "OPTIONS"])
def api_root():
    # Open the Base URL in a browser -> shows that the API is alive.
    if request.method == "OPTIONS":
        return _env(200, "ok")
    base = get_public_base()
    return _env(200, "ok", {
        "service": "TGZ NUMBER & OTP API",
        "base_url": f"{base}{API_PATH}",
        "auth_header": "mauthapi: <your_api_key>",
        "endpoints": ["POST /getnum", "GET /otp", "GET /success-otp", "GET /liveaccess",
                      "GET /console", "GET /balance", "GET /ranges"],
    }, "API is working")

def api_base_full():
    return get_public_base() + API_PATH

def set_user_api_enabled(uid, flag):
    flag = bool(flag)
    u = get_user(uid)
    u["api_enabled"] = flag
    user_cache[uid] = u
    try:
        db.collection('users').document(str(uid)).set({"api_enabled": flag}, merge=True)
    except Exception as e:
        print(f"api_enabled save error {uid}: {e}")
    if flag:
        ensure_user_api_key(uid)
    try:
        send_message(uid, render_body_text("✅ <b>API ENABLED</b>\nAPI DOCUMENT বাটনে গিয়ে আপনার API Key দেখুন।" if flag
                                           else "🚫 <b>API DISABLED</b>\nআপনার API বন্ধ করা হয়েছে।"))
    except Exception:
        pass

API_USERS_PER_PAGE = 8

def api_users_view(page=0, only_uid=None):
    rows = []
    try:
        for snap in db.collection('users').stream():
            rows.append((snap.id, snap.to_dict()))
    except Exception:
        pass
    total = len(rows)
    enabled = sum(1 for _, d in rows if d.get("api_enabled"))
    if only_uid is not None:
        rows = [r for r in rows if r[0] == str(only_uid)]
        page, pages = 0, 1
    else:
        rows.sort(key=lambda r: int(r[0]) if str(r[0]).isdigit() else 0)
        pages = max(1, (len(rows) + API_USERS_PER_PAGE - 1) // API_USERS_PER_PAGE)
        page = max(0, min(page, pages - 1))
        rows = rows[page * API_USERS_PER_PAGE:(page + 1) * API_USERS_PER_PAGE]
    txt = (f"🔌 <b>API USERS</b>\n━━━━━━━━━━━━━━━\n"
           f"👥 Users: <b>{total}</b> │ ✅ API ON: <b>{enabled}</b>\n"
           f"📄 Page {page + 1}/{pages}\n"
           f"<i>Toggle দিয়ে ইউজারের API চালু/বন্ধ করুন। বন্ধ থাকলে API কাজ করবে না এবং Key দেখা যাবে না।</i>")
    if only_uid is not None and not rows:
        txt += "\n\n❌ User not found in database."
    kb = []
    pg = "f" if only_uid is not None else str(page)
    for uid, d in rows:
        name = (d.get("display_name") or "User")[:10]
        on = bool(d.get("api_enabled"))
        kb.append([{"text": f"{name} · {uid}", "callback_data": "ignore", "style": "primary"},
                   {"text": "API ON ✅" if on else "API OFF 🚫", "callback_data": f"ul_t_{uid}_{pg}",
                    "style": "success" if on else "danger"}])
    if only_uid is None:
        nav = []
        if page > 0:
            nav.append({"text": "◀ Prev", "callback_data": f"ul_p_{page - 1}", "style": "primary"})
        if page < pages - 1:
            nav.append({"text": "Next ▶", "callback_data": f"ul_p_{page + 1}", "style": "primary"})
        if nav:
            kb.append(nav)
    kb.append([{"text": "Find User ID", "callback_data": "ul_find", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}])
    return render_body_text(txt), {"inline_keyboard": kb}

def ensure_user_api_key(uid, regenerate=False):
    with _api_lock:
        user = get_user(uid)
        old = user.get("api_key")
        if old and not regenerate:
            api_key_index[old] = uid
            return old
        if old:
            api_key_index.pop(old, None)
        key = "tgz_" + (f"c{CHILD_BOT_ID}_" if IS_CHILD else "") + secrets.token_urlsafe(24)
        user["api_key"] = key
        user_cache[uid] = user
        try:
            db.collection('users').document(str(uid)).set({"api_key": key}, merge=True)
        except Exception as e:
            print(f"API key save error for {uid}: {e}")
        api_key_index[key] = uid
        return key

def load_api_keys():
    with _api_lock:
        api_key_index.clear()
        try:
            for snap in db.collection('users').stream():
                d = snap.to_dict()
                if d.get("api_key"):
                    api_key_index[d["api_key"]] = int(snap.id)
        except Exception as e:
            print(f"API key index load error: {e}")

def _env(code, status, data=None, message="", http=None):
    body = {"meta": {"code": code, "status": status}, "data": data, "message": message,
            "rid": uuid.uuid4().hex[:12]}
    resp = jsonify(body)
    if http is None:
        http = code if code in (400, 401, 403, 404, 429) else 200
    resp.status_code = http
    return resp

def _out_of_stock(msg="out of stock"):
    # same shape as StexSMS: meta.code 2946 / status not_found / data null
    return _env(2946, "not_found", None, msg, http=200)

@health_check_web.after_request
def _api_cors(resp):
    if request.path.startswith(API_PATH + "/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "mauthapi, X-API-Key, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def _api_auth():
    key = (request.headers.get("mauthapi") or request.headers.get("X-API-Key")
           or request.args.get("key") or request.args.get("api_key") or "").strip()
    uid = api_key_index.get(key) if key else None
    if not uid:
        return None, _env(401, "unauthorized", None, "invalid or missing API key")
    if is_user_banned(uid):
        return None, _env(403, "forbidden", None, "account banned")
    if not get_user(uid).get("api_enabled", False):
        return None, _env(403, "forbidden", None, "API access is disabled for this account")
    return uid, None

def _clean_rid(rid):
    return re.sub(r"(?i)x+$", "", str(rid or "").strip().replace("+", ""))

def _find_api_number(num):
    clean = re.sub(r"\D", "", str(num))
    if not clean:
        return None
    if clean in api_numbers:
        return clean, api_numbers[clean]
    if len(clean) >= 8:
        for n, meta in api_numbers.items():
            if n.endswith(clean[-8:]) or clean.endswith(n[-8:]):
                return n, meta
    return None

def is_public_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        return True
    except Exception:
        return False

def _post_webhook(url, payload):
    try:
        if not is_public_url(url):
            return
        requests.post(url, json=payload, timeout=5, allow_redirects=False)
    except Exception as e:
        print(f"API webhook error: {e}")

def _notify_number(uid, num, rng, rate):
    """Show the API-taken number in the user's bot inbox."""
    try:
        display = "+" + num
        txt = render_body_text(
            f"{get_flag_info_html(display)} <b>API NUMBER</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📍 Range: <code>{html.escape(str(rng))}</code>\n"
            f"💰 Rate: <b>{rate} TK/OTP</b>\n"
            f"⌛ OTP আসলে এখানেই পাবেন।")
        kb = [[{"text": display, "icon_custom_emoji_id": "5337132498965010628",
                "copy_text": {"text": display}, "style": "success"}]]
        send_message(uid, txt, reply_markup={"inline_keyboard": kb})
    except Exception as e:
        print(f"API number notify error: {e}")

def api_on_otp(num, owner_id, otp, msg_text, app_name):
    """Called by the SMS listener the moment an OTP arrives for an owned number."""
    with _api_lock:
        found = _find_api_number(num)
        if not found or found[1].get("uid") != owner_id:
            return
        n, meta = found
        ts = int(time.time() * 1000)
        entry = {"otp_id": f"{n}{ts}", "number": n, "otp": str(otp), "message": str(msg_text),
                 "service": app_name, "range": meta.get("range", ""), "time": ts}
        log = api_otp_log.setdefault(str(owner_id), [])
        log.insert(0, entry)
        del log[50:]
        api_hits.appendleft({"range": meta.get("range", ""), "sid": str(app_name).title(),
                             "message": re.sub(r"\d{4,}", lambda m: "*" * len(m.group(0)), str(msg_text)),
                             "time": ts})
        cb = meta.get("callback")
    with _otp_cv:
        _otp_cv.notify_all()          # wakes every waiting /otp?wait= request instantly
    api_mark_dirty()
    if cb:
        threading.Thread(target=_post_webhook,
                         args=(cb, {"event": "otp", **{k: entry[k] for k in ("otp_id", "number", "otp", "message", "service", "time")}}),
                         daemon=True).start()

def api_cleanup_thread():
    while True:
        time.sleep(60)
        try:
            now = time.time()
            with _api_lock:
                for n in [n for n, m in api_numbers.items() if now - m.get("time", 0) > API_NUMBER_TTL]:
                    api_numbers.pop(n, None)
                    stex_assigned_numbers.pop(n, None)
            api_mark_dirty()
        except Exception as e:
            print(f"API cleanup error: {e}")

def _api_rate_for(rid):
    """Rate per OTP. No manual range stock needed: any range the provider can give works."""
    if IS_CHILD:
        return float(bot_settings.get("otp_reward", 0.0))
    for r, p in bot_settings.get("api_ranges", {}).items():          # optional manual override
        if _clean_rid(r) == rid:
            return float(p)
    for srv, cnts in bot_settings.get("stex_services", {}).items():   # same rate as the bot service
        for cnt, ranges in cnts.items():
            if any(_clean_rid(x) == rid for x in ranges):
                rate = bot_settings.get("stex_service_rates", {}).get(srv, {}).get(cnt)
                if rate is not None:
                    return float(rate)
    return float(bot_settings.get("otp_reward", 0.0))

@health_check_web.route(API_PATH + "/getnum", methods=["POST", "GET", "OPTIONS"])
def api_getnum():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    global total_assigned_stats
    uid, err = _api_auth()
    if err: return err
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        body = {}
    rid = _clean_rid(body.get("rid") or body.get("range") or request.args.get("rid")
                     or request.args.get("range") or request.form.get("rid"))
    callback = str(body.get("callback_url") or request.args.get("callback_url") or "").strip()
    if not rid or not re.fullmatch(r"[A-Za-z0-9_\-:.]{1,64}", rid):
        return _env(400, "bad_request", None, "'rid' (range digits, no XXX) is required")
    if callback and not is_public_url(callback):
        return _env(400, "bad_request", None, "callback_url must be a public http(s) URL")
    num, d = stex_fetch(rid)
    if not num:
        return _out_of_stock()
    price = _api_rate_for(rid)
    with _api_lock:
        stex_assigned_numbers[num] = uid
        assigned_number_rates[num] = price
        api_numbers[num] = {"uid": uid, "range": rid, "rate": price, "time": time.time(), "callback": callback}
        total_assigned_stats += 1
    api_mark_dirty()
    threading.Thread(target=_notify_number, args=(uid, num, rid, price), daemon=True).start()
    country = d.get("country") or ""
    if not country:
        cname = get_flag_info_html("+" + num, return_full_name=True)
        country = cname if cname and cname != "+" + num else ""
    return _env(200, "ok", {
        "full_number": "+" + num,
        "national_number": d.get("national_number", ""),
        "no_plus_number": num,
        "country": country,
        "operator": d.get("operator", "")}, "number allocated")

def _log_entry_for(uid, number, since_ms):
    n = re.sub(r"\D", "", str(number))
    for e in api_otp_log.get(str(uid), []):
        if e["time"] >= since_ms and (e["number"] == n or (len(n) >= 8 and e["number"].endswith(n[-8:]))):
            return e
    return None

@health_check_web.route(API_PATH + "/otp", methods=["GET", "OPTIONS"])
def api_otp_for_number():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    uid, err = _api_auth()
    if err: return err
    number = re.sub(r"\D", "", request.args.get("number") or request.args.get("num") or "")
    if not number:
        return _env(400, "bad_request", None, "'number' is required")
    try:
        wait = max(0, min(int(request.args.get("wait", 0) or 0), API_LONGPOLL_MAX))
    except ValueError:
        wait = 0
    end = time.time() + wait
    while True:
        with _api_lock:
            found = _find_api_number(number)
            owns = bool(found and found[1].get("uid") == uid)
            since = int(found[1]["time"] * 1000) - 2000 if owns else 0
            e = _log_entry_for(uid, number, since)
        if e:
            return _env(200, "ok", {k: e[k] for k in ("number", "otp", "message", "service", "time")}, "ok")
        if not owns:
            return _env(404, "not_found", None, "unknown or expired number")
        rem = end - time.time()
        if rem <= 0:
            return _env(200, "waiting", None, "number active, OTP not received yet", http=200)
        with _otp_cv:
            _otp_cv.wait(timeout=min(rem, 1.0))

@health_check_web.route(API_PATH + "/success-otp", methods=["GET", "OPTIONS"])
def api_success_otp():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    uid, err = _api_auth()
    if err: return err
    with _api_lock:
        items = [{"otp_id": e["otp_id"], "number": e["number"], "message": e["message"],
                  "time": e["time"], "otp": e["otp"], "service": e["service"]}
                 for e in api_otp_log.get(str(uid), [])]
    return _env(200, "ok", {"cached": False, "otps": items}, "ok")

@health_check_web.route(API_PATH + "/balance", methods=["GET", "OPTIONS"])
def api_balance():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    uid, err = _api_auth()
    if err: return err
    u = get_user(uid)
    return _env(200, "ok", {"user_id": uid, "balance": round(float(u.get("balance", 0.0)), 4),
                            "total_otps": u.get("total_otps", 0), "currency": "TK"}, "ok")

@health_check_web.route(API_PATH + "/ranges", methods=["GET", "OPTIONS"])
def api_ranges_list():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    uid, err = _api_auth()
    if err: return err
    data, _ = _provider_get("liveaccess", 60)
    seen, out = set(), []
    for sv in (data or {}).get("services", []):
        for r in sv.get("ranges", []):
            rr = _clean_rid(r)
            if rr and rr not in seen:
                seen.add(rr)
                out.append({"range": rr, "rate": _api_rate_for(rr)})
    return _env(200, "ok", {"ranges": out}, "ok")

@health_check_web.route(API_PATH + "/liveaccess", methods=["GET", "OPTIONS"])
def api_liveaccess():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    uid, err = _api_auth()
    if err: return err
    data, cached = _provider_get("liveaccess", 60)
    if data is not None and "services" in data:
        return _env(200, "ok", {"cached": cached, "services": data.get("services", [])}, "ok")
    seen = {}
    for h in list(api_hits):
        seen.setdefault(h["sid"], {"sid": h["sid"], "last_at": h["time"], "ranges": []})
        if h["range"] and h["range"] + "XXX" not in seen[h["sid"]]["ranges"]:
            seen[h["sid"]]["ranges"].append(h["range"] + "XXX")
    return _env(200, "ok", {"cached": False, "services": list(seen.values())}, "ok")

@health_check_web.route(API_PATH + "/console", methods=["GET", "OPTIONS"])
def api_console():
    if request.method == "OPTIONS":
        return _env(200, "ok")
    uid, err = _api_auth()
    if err: return err
    mask = lambda t: re.sub(r"\d{4,}", lambda m: "*" * len(m.group(0)), str(t))
    data, cached = _provider_get("console", 3)
    if data is not None and "hits" in data:
        hits = [{"range": h.get("range", ""), "sid": h.get("sid", ""), "message": mask(h.get("message", "")),
                 "time": h.get("time", 0)} for h in data.get("hits", [])]
        return _env(200, "ok", {"cached": cached, "hits": hits}, "ok")
    cutoff = int(time.time() * 1000) - 15 * 60 * 1000
    hits = [{"range": h["range"] + "XXX", "sid": h["sid"], "message": h["message"], "time": h["time"]}
            for h in list(api_hits) if h["time"] >= cutoff]
    return _env(200, "ok", {"cached": False, "hits": hits}, "ok")

def api_doc_message(uid):
    u = get_user(uid)
    if not u.get("api_enabled", False):
        txt = ("━━━━━━━━━━━━━━━\n《 🔑 <b>API DOCUMENT</b> 》\n━━━━━━━━━━━━━━━\n"
               "🚫 <b>API DISABLED</b>\n"
               "আপনার অ্যাকাউন্টে API বন্ধ আছে।\n"
               "চালু করতে অ্যাডমিনের সাথে যোগাযোগ করুন।\n━━━━━━━━━━━━━━━")
        kb = {"inline_keyboard": [[{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]}
        return render_body_text(txt), kb
    key = ensure_user_api_key(uid)
    txt = (f"━━━━━━━━━━━━━━━\n《 🔑 <b>API DOCUMENT</b> 》\n━━━━━━━━━━━━━━━\n"
           f"✅ <b>API ENABLED</b>\n"
           f"🔑 <b>YOUR API KEY:</b>\n<code>{key}</code>\n━━━━━━━━━━━━━━━\n"
           f"🌐 <b>BASE URL:</b>\n<code>{html.escape(api_base_full())}</code>\n━━━━━━━━━━━━━━━\n"
           f"📌 Header: <code>mauthapi: YOUR_KEY</code>\n"
           f"➤ <code>POST /getnum</code> {{\"rid\":\"26134\"}}\n"
           f"➤ <code>GET /otp?number=…&amp;wait=25</code>\n"
           f"➤ <code>GET /success-otp</code>\n"
           f"➤ <code>GET /liveaccess</code> · <code>/console</code>\n━━━━━━━━━━━━━━━\n"
           f"⚡ নাম্বার ও OTP এই বটের ইনবক্সেও আসবে, OTP এলে ব্যালেন্স যোগ হবে।")
    kb = {"inline_keyboard": [
        [{"text": "Regenerate Key", "icon_custom_emoji_id": "5375338737028841420", "callback_data": "api_regen", "style": "danger"}],
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]}
    return render_body_text(txt), kb

# ---------- Admin: API Range (just for the API, no service/country rules) ----------
def api_ranges_text():
    rs = bot_settings.get("api_ranges", {})
    return f"🔌 <b>API RANGES</b>\n━━━━━━━━━━━━━━━\nTotal: <b>{len(rs)}</b>\n<i>এই range গুলোতেই API থেকে নাম্বার পাওয়া যাবে।</i>"

def api_ranges_keyboard():
    kb = []
    for rng, price in bot_settings.get("api_ranges", {}).items():
        kb.append([{"text": f"{rng} — {price} TK", "icon_custom_emoji_id": "5337132498965010628", "callback_data": "ignore", "style": "primary"},
                   {"text": "Delete", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"api_rng_del_{rng}", "style": "danger"}])
    kb.append([{"text": "Add Range", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "api_rng_add", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}])
    return {"inline_keyboard": kb}

def api_rng_cancel_kb():
    return {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "api_rng_main", "style": "danger"}]]}

# ==========================================
# 🤖 MULTI-BOT SYSTEM
# Every extra bot runs as its own process of this same script (BOT_ROLE=child)
# with its own data file (users, balances, groups, settings).  A child inherits
# services / countries / rates / API ranges / emojis from this main bot, uses
# the StexSMS key set for it here, and has no stock-upload / service / key menus.
# All API traffic uses ONE base URL: the main server routes a child user's key
# (tgz_c<botid>_...) to that bot's private local port.
# ==========================================
CHILD_DATA_DIR = os.environ.get("CHILD_DATA_DIR", "child_bots_data")
MAIN_DATA_FILE = os.environ.get("MAIN_DATA_FILE", "")
INHERITED_KEYS = ["stex_services", "stex_service_rates", "api_ranges", "premium_flags", "premium_apps", "api_base_url"]
child_procs = {}
_child_lock = threading.RLock()

# ----- child side -----
def apply_inherited_settings():
    try:
        with open(MAIN_DATA_FILE, "r", encoding="utf-8") as fp:
            cfg = json.load(fp).get("settings", {}).get("bot_config", {})
    except Exception:
        return
    if "child_bots" not in cfg:
        return
    entry = cfg["child_bots"].get(CHILD_BOT_ID)
    if entry is None:
        print("🗑 This bot was removed from the main bot. Shutting down.")
        os._exit(0)
    for k in INHERITED_KEYS:
        if k in cfg:
            bot_settings[k] = cfg[k]
    bot_settings["stex_keys"] = [entry["stex_key"]] if entry.get("stex_key") else []

def child_sync_thread():
    while True:
        time.sleep(20)
        try:
            apply_inherited_settings()
        except Exception as e:
            print(f"Child sync error: {e}")

# ----- main side: process management -----
def _port_is_free(port):
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
        sk.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        try:
            sk.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

def _free_child_port():
    used = set()
    for b in bot_settings.get("child_bots", {}).values():
        try:
            used.add(int(b.get("port", 0)))
        except Exception:
            pass
    main_port = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT") or 10000)
    p = 18100
    while p in used or p == main_port or not _port_is_free(p):
        p += 1
    return p

# one shared session that NEVER uses system HTTP(S)_PROXY for 127.0.0.1 calls
_child_http = requests.Session()
_child_http.trust_env = False
_child_http.mount("http://", HTTPAdapter(pool_connections=16, pool_maxsize=64))

def _child_alive(entry):
    try:
        r = _child_http.get(f"http://127.0.0.1:{entry['port']}/api/v1/", timeout=3)
        return r.status_code < 500
    except requests.RequestException:
        return False

def _ensure_child_up(bot_id, wait=25):
    """Make sure the child process runs AND its port answers; restart if not."""
    entry = bot_settings.get("child_bots", {}).get(bot_id)
    if not entry:
        return False
    p = child_procs.get(bot_id)
    if p is None or p.poll() is not None:
        try:
            start_child_bot(bot_id)
        except Exception as e:
            print(f"Child start error {bot_id}: {e}")
    end = time.time() + wait
    while time.time() < end:
        if _child_alive(entry):
            return True
        p = child_procs.get(bot_id)
        if p is not None and p.poll() is not None:
            # process died (maybe port clash) -> pick a new port and retry once
            if not _port_is_free(int(entry["port"])):
                entry["port"] = _free_child_port()
                _sync_fs()
            start_child_bot(bot_id)
        time.sleep(1)
    return False

def start_child_bot(bot_id):
    entry = bot_settings.get("child_bots", {}).get(bot_id)
    if not entry:
        return
    with _child_lock:
        old = child_procs.get(bot_id)
        if old and old.poll() is None:
            return
        os.makedirs(CHILD_DATA_DIR, exist_ok=True)
        env = os.environ.copy()
        env.pop("PORT", None)
        env.update({
            "BOT_ROLE": "child", "BOT_TOKEN": entry["token"], "CHILD_BOT_ID": bot_id,
            "CHILD_ADMIN_ID": str(entry["admin_id"]), "CHILD_PORT": str(entry["port"]),
            "BOT_DATA_FILE": os.path.abspath(os.path.join(CHILD_DATA_DIR, f"{bot_id}.json")),
            "MAIN_DATA_FILE": os.path.abspath(DATA_FILE),
        })
        base = get_public_base()
        if "YOUR-DOMAIN" not in base:
            env["PUBLIC_BASE_URL"] = base
        child_procs[bot_id] = subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env)
        print(f"🤖 Child bot started: {bot_id} (@{entry.get('username', '')})")

def stop_child_bot(bot_id):
    with _child_lock:
        p = child_procs.pop(bot_id, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()

def stop_all_children():
    for bid in list(child_procs):
        stop_child_bot(bid)

def child_monitor_thread():
    fails = {}
    while True:
        for bot_id in list(bot_settings.get("child_bots", {})):
            try:
                p = child_procs.get(bot_id)
                if p is None or p.poll() is not None:
                    start_child_bot(bot_id)
                    continue
                entry = bot_settings["child_bots"][bot_id]
                if _child_alive(entry):
                    fails[bot_id] = 0
                else:
                    fails[bot_id] = fails.get(bot_id, 0) + 1
                    if fails[bot_id] >= 4:       # ~1 min unresponsive -> restart
                        fails[bot_id] = 0
                        stop_child_bot(bot_id)
                        start_child_bot(bot_id)
            except Exception as e:
                print(f"Child monitor error {bot_id}: {e}")
        time.sleep(15)

# ----- main side: route a child user's API key to its bot -----
def _find_child_for_key(key):
    """Key format: tgz_c<botid>_<random>.  Match against every known bot id
    (no fragile split), so any number of bots works."""
    if not key.startswith("tgz_c"):
        return None, None
    rest = key[5:]
    for bid, entry in bot_settings.get("child_bots", {}).items():
        if rest.startswith(bid + "_"):
            return bid, entry
    return None, None

@health_check_web.before_request
def _child_api_proxy():
    if IS_CHILD or request.method == "OPTIONS" or not request.path.startswith(API_PATH + "/"):
        return None
    key = (request.headers.get("mauthapi") or request.headers.get("X-API-Key")
           or request.args.get("key") or request.args.get("api_key") or "").strip()
    if not key or key in api_key_index:
        return None
    bot_id, entry = _find_child_for_key(key)
    if not entry:
        return None
    url = f"http://127.0.0.1:{entry['port']}{request.path}"
    qs = request.query_string.decode()
    if qs:
        url += "?" + qs
    fwd = {h: request.headers[h] for h in ("Content-Type", "mauthapi", "X-API-Key") if h in request.headers}
    body = request.get_data()
    last_err = None
    for attempt in range(2):
        try:
            r = _child_http.request(request.method, url, headers=fwd, data=body, timeout=(5, 45))
            return Response(r.content, status=r.status_code,
                            content_type=r.headers.get("Content-Type", "application/json"))
        except requests.RequestException as e:
            last_err = e
            if attempt == 0:
                # child down / starting / wrong port -> bring it up, then retry once
                if not _ensure_child_up(bot_id):
                    break
                url = f"http://127.0.0.1:{bot_settings['child_bots'][bot_id]['port']}{request.path}"
                if qs:
                    url += "?" + qs
    print(f"Child proxy error {bot_id}: {last_err}")
    return _env(503, "unavailable", None, "bot service is starting, retry shortly", http=503)

# ----- main side: Admin Panel -> Add Bot UI -----
def bots_cancel_kb():
    return {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "bots_main", "style": "danger"}]]}

def bots_main_text():
    n = len(bot_settings.get("child_bots", {}))
    return render_body_text(f"🤖 <b>BOT MANAGER</b>\n━━━━━━━━━━━━━━━\nTotal Bots: <b>{n}</b>\n<i>নতুন bot যোগ করুন অথবা আগেরগুলো manage করুন।</i>")

def bots_main_keyboard():
    kb = []
    for bid, b in bot_settings.get("child_bots", {}).items():
        p = child_procs.get(bid)
        run = "🟢" if p and p.poll() is None else "🔴"
        kb.append([{"text": f"{run} @{b.get('username', '?')}", "callback_data": f"bots_view_{bid}", "style": "primary"}])
    kb.append([{"text": "Add Bot", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "bots_add", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}])
    return {"inline_keyboard": kb}

def handle_bots_callback(data, chat_id, msg_id):
    user_states.pop(chat_id, None)
    temp_data.pop(chat_id, None)
    bots = bot_settings.setdefault("child_bots", {})

    if data == "bots_main":
        edit_message(chat_id, msg_id, bots_main_text(), reply_markup=bots_main_keyboard())

    elif data == "bots_add":
        user_states[chat_id] = "wait_addbot_token"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 নতুন বটের <b>Bot Token</b> দিন:"), reply_markup=bots_cancel_kb())

    elif data.startswith("bots_view_"):
        bid = data[len("bots_view_"):]
        b = bots.get(bid)
        if not b:
            return edit_message(chat_id, msg_id, bots_main_text(), reply_markup=bots_main_keyboard())
        p = child_procs.get(bid)
        status = "🟢 Running" if p and p.poll() is None else "🔴 Stopped"
        key = b.get("stex_key", "")
        txt = (f"🤖 <b>@{html.escape(b.get('username', ''))}</b>\n━━━━━━━━━━━━━━━\n"
               f"👤 Admin ID: <code>{b['admin_id']}</code>\n"
               f"🔑 StexSMS Key: <code>{html.escape(key[:8])}…</code>\n"
               f"📡 Status: {status}")
        kb = {"inline_keyboard": [
            [{"text": "Change API Key", "icon_custom_emoji_id": "5197288647275071607", "callback_data": f"bots_key_{bid}", "style": "primary"}],
            [{"text": "Remove Bot", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"bots_del_{bid}", "style": "danger"}],
            [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "bots_main", "style": "primary"}]]}
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup=kb)

    elif data.startswith("bots_key_"):
        bid = data[len("bots_key_"):]
        if bid in bots:
            user_states[chat_id] = "wait_bot_newkey"
            temp_data[chat_id] = {"msg_id": msg_id, "bot_id": bid}
            edit_message(chat_id, msg_id, render_body_text("📝 নতুন <b>StexSMS API Key</b> দিন:"), reply_markup=bots_cancel_kb())

    elif data.startswith("bots_delok_"):
        bid = data[len("bots_delok_"):]
        if bid in bots:
            del bots[bid]
            save_db()
            stop_child_bot(bid)
        edit_message(chat_id, msg_id, bots_main_text(), reply_markup=bots_main_keyboard())

    elif data.startswith("bots_del_"):
        bid = data[len("bots_del_"):]
        kb = {"inline_keyboard": [[
            {"text": "Yes, Remove", "icon_custom_emoji_id": "5352694861990501856", "callback_data": f"bots_delok_{bid}", "style": "danger"},
            {"text": "No", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"bots_view_{bid}", "style": "primary"}]]}
        edit_message(chat_id, msg_id, render_body_text("⚠️ এই বটটি বন্ধ করে মুছে ফেলবেন?"), reply_markup=kb)

def main():
    global BOT_USERNAME
    res = api_call("getMe")
    if res.get("ok"): BOT_USERNAME = res["result"]["username"]
    print(f"🤖 Bot is starting... @{BOT_USERNAME}")
    
    load_api_keys()
    if IS_CHILD:
        apply_inherited_settings()
        threading.Thread(target=child_sync_thread, daemon=True).start()
    else:
        atexit.register(stop_all_children)
        try:
            signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        except Exception:
            pass
        threading.Thread(target=child_monitor_thread, daemon=True).start()
    threading.Thread(target=api_cleanup_thread, daemon=True).start()
    threading.Thread(target=api_flush_thread, daemon=True).start()
    threading.Thread(target=global_sms_listener, daemon=True).start()
    threading.Thread(target=start_health_check_server, daemon=True).start()
    threading.Thread(target=leaderboard_auto_refresh_thread, daemon=True).start()
    print("📡 Background APIs & Global SMS Listener Started!")
    try:
        _p = os.environ.get("PORT") or os.environ.get("SERVER_PORT") or "10000"
        print(f"🌐 [BASEURL-v3] Base URL: {api_base_full()}  (server port {_p})")
    except Exception as _e:
        print(f"Base URL print error: {_e}")

    # 🌟 PRO-LEVEL FAST SYSTEM: 500 Workers Pool
    # Telegram rate limits are easier to respect with a
    # bounded pool than with hundreds of simultaneous handler threads.
    executor = ThreadPoolExecutor(max_workers=64)
    
    offset = None
    while True:
        try:
            updates = api_call(f"getUpdates?timeout=50&offset={offset}")
            if updates and "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        executor.submit(safe_handle_message, update["message"])
                    elif "callback_query" in update:
                        executor.submit(safe_handle_callback, update["callback_query"])
        except Exception as e:
            print(f"Update polling error: {e}")
            time.sleep(2)

def safe_handle_message(message):
    try:
        handle_message(message)
    except Exception as e:
        chat_id = message.get("chat", {}).get("id")
        print(f"Message handler error for {chat_id}: {e}")
        if chat_id:
            send_message(chat_id, "সাময়িক সার্ভার সমস্যার কারণে অনুরোধটি সম্পন্ন হয়নি। একটু পরে আবার চেষ্টা করুন।")

def safe_handle_callback(callback):
    try:
        handle_callback(callback)
    except Exception as e:
        callback_id = callback.get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        print(f"Callback handler error for {chat_id}: {e}")
        if callback_id:
            answer_callback(callback_id, "সাময়িক সমস্যা হয়েছে, আবার চেষ্টা করুন।", True)

if __name__ == "__main__":
    main()
