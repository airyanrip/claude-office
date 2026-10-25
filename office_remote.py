"""세션 사무실 원격 조종: 휴대폰 웹앱(PWA)이 부르는 API 서버와 그 둘레.

- 서버는 127.0.0.1에서만 열고, 바깥 연결은 Cloudflare 임시 터널(cloudflared)이 PC에서 밖으로 연다 (공유기 포트를 열지 않음).
- 터널 주소는 켤 때마다 바뀌므로 ntfy.sh의 비밀 토픽에 지금 주소를 적어 두고, 웹앱은 거기서 찾아온다.
- 기기마다 무작위 토큰을 QR로 한 번 건네고, PC에는 토큰의 해시만 둔다. 모든 API는 토큰이 있어야 한다.
- '확인 필요'가 된 세션 등은 웹 푸시(VAPID, 내용 없는 푸시)로 알린다. 웹앱의 서비스 워커가 받아서 PC에 알림 내용을 묻는다.
- 바깥 패키지 없이 표준 라이브러리만 쓴다 (P-256 서명, QR, PNG를 직접 만든다).
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP_ORIGIN = 'https://airyanrip.github.io'          # 휴대폰 웹앱이 올라가 있는 곳 (설정 remote_app 으로 바꿀 수 있다)
APP_PATH = '/claude-office/m/'
NTFY = 'https://ntfy.sh/'
CF_URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
CREATE_NO_WINDOW = 0x08000000


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


# ───────────────────────────── PNG ─────────────────────────────
def png_bytes(w, h, rgba_rows):
    """rgba_rows: 줄마다 w*4 바이트. 무손실 PNG 한 장."""
    raw = b''.join(b'\x00' + r for r in rgba_rows)

    def chunk(t, d):
        return struct_pack('>I', len(d)) + t + d + struct_pack('>I', zlib.crc32(t + d) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct_pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def struct_pack(fmt, *a):
    import struct
    return struct.pack(fmt, *a)


def hex_rgb(c):
    return bytes(int(c[i:i + 2], 16) for i in (1, 3, 5))


def sprite_png(co, sid, pose, scale, av=None):
    """사무실 NPC 한 장을 PNG로 (휴대폰 화면용)."""
    style, cmap, _seed, acc = co.looks(sid, av)
    g = co.sprite_grid(style, pose if pose in co.POSES else 'idle', acc)
    rows = []
    for row in g:
        line = bytearray()
        for c in row:
            line += (hex_rgb(cmap[c]) + b'\xff') if c else b'\x00\x00\x00\x00'
        line = bytes(b for px in range(len(row)) for b in line[px * 4:px * 4 + 4] * scale)
        rows += [line] * scale
    return png_bytes(co.SW * scale, co.SH * scale, rows)


# ───────────────────────────── QR 코드 (바이트 모드) ─────────────────────────────
# 표준(ISO/IEC 18004) 그대로: 버전별 오류 정정 블록 수·블록당 정정 코드어 수
_ECC_PER_BLOCK = {  # L, M
    'L': (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28),
    'M': (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26),
}
_NUM_BLOCKS = {
    'L': (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8),
    'M': (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16),
}
_ECL_BITS = {'L': 1, 'M': 0}


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        k = ver // 7 + 2
        n -= (25 * k - 10) * k - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver, ecl):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _NUM_BLOCKS[ecl][ver]


def _rs_divisor(deg):
    res = [0] * (deg - 1) + [1]
    root = 1
    for _ in range(deg):
        for j in range(deg):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < deg:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 0x02)
    return res


def _rs_remainder(data, div):
    res = [0] * len(div)
    for b in data:
        f = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, f)
    return res


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def qr_matrix(text, ecl='M'):
    """text를 담은 QR 모듈 표 (True = 검은 칸). 버전 1~20, 마스크는 벌점이 가장 낮은 것."""
    data = text.encode('utf-8')
    for ver in range(1, 21):
        cc_bits = 8 if ver <= 9 else 16
        if 4 + cc_bits + len(data) * 8 <= _data_codewords(ver, ecl) * 8:
            break
    else:
        raise ValueError('QR에 담기엔 너무 깁니다')
    bits = []

    def put(v, n):
        bits.extend((v >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    cap = _data_codewords(ver, ecl) * 8
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    # 블록으로 나눠 오류 정정 코드어를 붙이고 엇갈려 늘어놓는다
    nb, eb = _NUM_BLOCKS[ecl][ver], _ECC_PER_BLOCK[ecl][ver]
    raw = _raw_modules(ver) // 8
    n_short = nb - raw % nb
    short_len = raw // nb
    div = _rs_divisor(eb)
    blocks, k = [], 0
    for i in range(nb):
        dl = short_len - eb + (0 if i < n_short else 1)
        dat = words[k:k + dl]
        k += dl
        ecc = _rs_remainder(dat, div)
        if i < n_short:
            dat = dat + [None]
        blocks.append(dat + ecc)
    seq = []
    for i in range(len(blocks[0])):
        for j, b in enumerate(blocks):
            if b[i] is not None:
                seq.append(b[i])
    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, v):
        mod[y][x] = v
        fn[y][x] = True

    for i in range(size):  # 타이밍
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # 위치 찾기
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, d not in (2, 4))
    if ver > 1:  # 정렬 패턴
        na = ver // 7 + 2
        step = (ver * 8 + na * 3 + 5) // (na * 4 - 4) * 2
        pos = [6] + [size - 7 - i * step for i in range(na - 1)][::-1]
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, na - 1), (na - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = _ECL_BITS[ecl] << 3 | mask
        r = d
        for _ in range(10):
            r = (r << 1) ^ ((r >> 9) * 0x537)
        b = (d << 10 | r) ^ 0x5412
        g = lambda i: (b >> i) & 1 == 1
        for i in range(6):
            setf(8, i, g(i))
        setf(8, 7, g(6))
        setf(8, 8, g(7))
        setf(7, 8, g(8))
        for i in range(9, 15):
            setf(14 - i, 8, g(i))
        for i in range(8):
            setf(size - 1 - i, 8, g(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, g(i))
        setf(8, size - 8, True)

    draw_format(0)
    if ver >= 7:  # 버전 정보
        r = ver
        for _ in range(12):
            r = (r << 1) ^ ((r >> 11) * 0x1F25)
        b = ver << 12 | r
        for i in range(18):
            v = (b >> i) & 1 == 1
            a, c = size - 11 + i % 3, i // 3
            setf(a, c, v)
            setf(c, a, v)
    # 데이터를 지그재그로
    i = 0
    allbits = [(w >> (7 - j)) & 1 for w in seq for j in range(8)]
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = size - 1 - vert if up else vert
                if not fn[y][x] and i < len(allbits):
                    mod[y][x] = allbits[i] == 1
                    i += 1
        right -= 2
    masks = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)

    def apply(m):
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and masks[m](x, y):
                    mod[y][x] = not mod[y][x]

    best, best_pen = 0, None
    for m in range(8):
        apply(m)
        draw_format(m)
        p = _penalty(mod, size)
        if best_pen is None or p < best_pen:
            best, best_pen = m, p
        apply(m)
    apply(best)
    draw_format(best)
    return mod


def _penalty(mod, size):
    p = 0
    for lines in (mod, [list(c) for c in zip(*mod)]):
        for row in lines:
            run, prev = 0, None
            for v in row:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, v
            if run >= 5:
                p += run - 2
            s = ''.join('1' if v else '0' for v in row)
            p += 40 * (s.count('10111010000') + s.count('00001011101'))
    for y in range(size - 1):
        for x in range(size - 1):
            c = mod[y][x]
            if c == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                p += 3
    dark = sum(map(sum, mod))
    total = size * size
    p += (abs(dark * 20 - total * 10) + total - 1) // total * 10 - 10 if dark * 20 != total * 10 else 0
    return p


# ───────────────────────────── P-256 서명 (웹 푸시 VAPID) ─────────────────────────────
_P = 2 ** 256 - 2 ** 224 + 2 ** 192 + 2 ** 96 - 1
_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
      0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _ec_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    (x1, y1), (x2, y2) = a, b
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        lam = (3 * x1 * x1 - 3) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return x3, (lam * (x1 - x3) - y1) % _P


def _ec_mul(k, pt=_G):
    r = None
    while k:
        if k & 1:
            r = _ec_add(r, pt)
        pt = _ec_add(pt, pt)
        k >>= 1
    return r


def vapid_keys(priv=None):
    priv = priv or (secrets.randbelow(_N - 1) + 1)
    x, y = _ec_mul(priv)
    return priv, b'\x04' + x.to_bytes(32, 'big') + y.to_bytes(32, 'big')


def es256_sign(priv, msg):
    z = int.from_bytes(hashlib.sha256(msg).digest(), 'big')
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _ec_mul(k)[0] % _N
        s = pow(k, -1, _N) * (z + r * priv) % _N
        if r and s:
            return r.to_bytes(32, 'big') + s.to_bytes(32, 'big')


def vapid_header(priv, pub, endpoint):
    u = urllib.parse.urlsplit(endpoint)
    head = b64u(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
    body = b64u(json.dumps({'aud': f'{u.scheme}://{u.netloc}', 'exp': int(time.time()) + 12 * 3600,
                            'sub': 'https://github.com/airyanrip/claude-office'}, separators=(',', ':')).encode())
    sig = b64u(es256_sign(priv, f'{head}.{body}'.encode()))
    return f'vapid t={head}.{body}.{sig}, k={b64u(pub)}'


# ───────────────────────────── 터널 ─────────────────────────────
def find_cloudflared(cache_dir):
    for p in (shutil.which('cloudflared'), os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'cloudflared', 'cloudflared.exe'),
              os.path.join(os.environ.get('ProgramFiles', ''), 'cloudflared', 'cloudflared.exe'),
              os.path.join(cache_dir, 'cloudflared.exe')):
        if p and os.path.isfile(p):
            return p
    return None


class Tunnel:
    """cloudflared 임시 터널. 주소가 나오면 on_url(url)을 부른다. 끊기면 잠시 뒤 다시 연다."""

    def __init__(self, exe, port, on_url, on_log):
        self.exe, self.port, self.on_url, self.on_log = exe, port, on_url, on_log
        self.proc, self.url, self.stop_ev = None, None, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        wait = 3
        while not self.stop_ev.is_set():
            try:
                self.proc = subprocess.Popen([self.exe, 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{self.port}'],
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             creationflags=CREATE_NO_WINDOW, text=True, encoding='utf-8', errors='replace')
            except OSError as ex:
                self.on_log(f'cloudflared: {ex}')
                return
            for line in self.proc.stdout:
                m = CF_URL_RE.search(line)
                if m and m.group(0) != self.url:
                    self.url = m.group(0)
                    self.on_url(self.url)
                    wait = 3
                if self.stop_ev.is_set():
                    break
            self.proc.wait()
            self.url = None
            self.on_url(None)
            if self.stop_ev.wait(wait):
                break
            wait = min(wait * 2, 120)

    def stop(self):
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ───────────────────────────── 서버 ─────────────────────────────
class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):  # 휴대폰이 응답 도중 떠남 — 흔한 일
            return
        super().handle_error(request, client_address)


class Remote:
    """원격 조종의 상태(설정 파일 remote.json)와 서버·터널·푸시를 묶는다. app = 세션 사무실 App, co = 본 모듈."""

    def __init__(self, app, co):
        self.app, self.co = app, co
        self.path = os.path.join(co.CACHE_DIR, 'remote.json')
        self.cfg = co.read_json(self.path) or {}
        if not self.cfg.get('topic'):
            self.cfg['topic'] = 'co-' + secrets.token_urlsafe(18).replace('_', 'x').replace('-', 'y')
        if not self.cfg.get('vapid'):
            self.cfg['vapid'] = hex(vapid_keys()[0])
        self.cfg.setdefault('devices', [])
        self.priv, self.pub = vapid_keys(int(self.cfg['vapid'], 16))
        self.lock = threading.Lock()
        self.httpd = self.tunnel = None
        self.url = None
        self.status = 'off'      # off / starting / on / no_cloudflared / error
        self.log = []
        self.alerts = []         # 휴대폰에 알릴 것 [{id, ts, title, body, sid}]
        self.fails = []          # 실패한 인증 시각 (막무가내 시도 막기)
        self.q = queue.Queue()   # Tk 스레드에서 돌릴 일
        self.sprites = {}
        self.save()

    @property
    def app_url(self):
        return (self.app.cfg.get('remote_app') or APP_ORIGIN + APP_PATH)

    @property
    def app_origin(self):
        u = urllib.parse.urlsplit(self.app_url)
        return f'{u.scheme}://{u.netloc}'

    def save(self):
        with self.lock:
            data = json.dumps(self.cfg, ensure_ascii=False, indent=1)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(data)
        os.replace(tmp, self.path)

    def note(self, text):
        self.log = (self.log + [time.strftime('%H:%M:%S ') + text])[-30:]

    # ── 켜고 끄기 ──
    def start(self):
        if self.httpd:
            return True
        exe = find_cloudflared(self.co.CACHE_DIR)
        if not exe:
            self.status = 'no_cloudflared'
            return False
        handler = type('H', (_Handler,), {'remote': self})
        self.httpd = _Server(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.status = 'starting'
        self.note(f'API 127.0.0.1:{self.port}')
        self.tunnel = Tunnel(exe, self.port, self._on_url, self.note)
        threading.Thread(target=self._announce_loop, daemon=True).start()
        return True

    def stop(self, wait=0):
        """끈다. 휴대폰이 '꺼짐'을 알 수 있게 빈 주소를 알린다 (wait초까지 기다림 — 프로그램을 닫을 때)."""
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.url = None
        self.status = 'off'
        self.note('stopped')
        t = threading.Thread(target=self._publish, args=(None,), daemon=True)
        t.start()
        if wait:
            t.join(wait)

    def _on_url(self, url):
        self.url = url
        self.status = 'on' if url else ('starting' if self.httpd else 'off')
        self.note(f'tunnel {url or "down"}')
        if url:
            threading.Thread(target=self._publish, args=(url,), daemon=True).start()

    def _announce_loop(self):
        """ntfy는 12시간 보관이라 켜져 있는 동안 6시간마다 다시 알린다."""
        while self.httpd:
            for _ in range(6 * 3600 // 5):
                if not self.httpd:
                    return
                time.sleep(5)
            if self.url:
                self._publish(self.url)

    def _publish(self, url):
        try:
            body = json.dumps({'u': url or '', 'ts': int(time.time())}).encode()
            req = urllib.request.Request(NTFY + self.cfg['topic'], data=body, method='POST',
                                         headers={'Title': 'claude-office', 'Priority': 'min', 'Tags': 'computer'})
            urllib.request.urlopen(req, timeout=15).read()
            self.note('announced' if url else 'announced off')
        except (OSError, urllib.error.URLError) as ex:
            self.note(f'ntfy: {ex}')

    # ── 기기 ──
    def pair(self, name=''):
        """새 기기용 토큰을 만들고 휴대폰이 열 주소(QR에 담을 것)를 돌려준다. 토큰은 PC에 해시로만 남는다."""
        tok = secrets.token_urlsafe(24)
        dev = {'id': secrets.token_hex(4), 'name': name or '새 기기', 'hash': hashlib.sha256(tok.encode()).hexdigest(),
               'created': int(time.time()), 'seen': 0, 'push': None}
        with self.lock:
            self.cfg['devices'].append(dev)
        self.save()
        frag = urllib.parse.urlencode({'k': tok, 't': self.cfg['topic'], 'u': self.url or ''})
        return dev['id'], f'{self.app_url}#{frag}'

    def revoke(self, dev_id):
        with self.lock:
            self.cfg['devices'] = [d for d in self.cfg['devices'] if d['id'] != dev_id]
        self.save()

    def auth(self, header, ip):
        now = time.time()
        self.fails = [t for t in self.fails if now - t < 600]
        if len(self.fails) > 40:  # 10분에 40번 넘게 틀리면 잠시 모두 거절
            return None
        tok = header[7:] if header.startswith('Bearer ') else ''
        hsh = hashlib.sha256(tok.encode()).hexdigest()
        with self.lock:
            dev = next((d for d in self.cfg['devices'] if hmac.compare_digest(d['hash'], hsh)), None) if tok else None
            if dev:
                dev['seen'] = int(now)
        if not dev:
            self.fails.append(now)
        return dev

    # ── 알림 ──
    def alert(self, title, body, sid=''):
        """휴대폰에 알림 (쌓아 두고, 푸시를 등록한 기기에 '새 알림 있음' 푸시)."""
        a = {'id': secrets.token_hex(4), 'ts': int(time.time()), 'title': title, 'body': body, 'sid': sid}
        self.alerts = (self.alerts + [a])[-40:]
        if not self.httpd:
            return
        with self.lock:
            subs = [(d['id'], d['push']) for d in self.cfg['devices'] if d.get('push')]
        for dev_id, sub in subs:
            threading.Thread(target=self._push, args=(dev_id, sub), daemon=True).start()

    def _push(self, dev_id, sub):
        ep = sub.get('endpoint', '')
        if not ep.startswith('https://'):
            return
        try:
            req = urllib.request.Request(ep, data=b'', method='POST', headers={
                'TTL': '3600', 'Urgency': 'high', 'Content-Length': '0',
                'Authorization': vapid_header(self.priv, self.pub, ep)})
            urllib.request.urlopen(req, timeout=15).read()
        except urllib.error.HTTPError as ex:
            self.note(f'push {ex.code}')
            if ex.code in (404, 410):  # 구독이 사라짐
                with self.lock:
                    for d in self.cfg['devices']:
                        if d['id'] == dev_id:
                            d['push'] = None
                self.save()
        except OSError as ex:
            self.note(f'push: {ex}')

    # ── Tk 스레드에서 돌리기 ──
    def ui(self, fn, timeout=20):
        """fn을 Tk 스레드에서 돌리고 결과를 기다린다 (App의 상태는 Tk 스레드만 만진다)."""
        box, ev, lock = {}, threading.Event(), threading.Lock()

        def run():
            with lock:
                if box.get('gone'):  # 휴대폰은 이미 '실패'를 받았다 → 늦게라도 실행하지 않는다 (명령이 두 번 가지 않게)
                    return
                box['started'] = True
            try:
                box['v'] = fn()
            except Exception as ex:  # 휴대폰에 오류로 돌려준다
                box['e'] = ex
            ev.set()

        self.q.put(run)
        if not ev.wait(timeout):
            with lock:
                if not box.get('started'):
                    box['gone'] = True
                    raise TimeoutError('ui busy')
            ev.wait()  # 이미 시작했으면 끝까지 기다려 결과를 돌려준다
        if 'e' in box:
            raise box['e']
        return box.get('v')

    def seen_req(self, key, result=None, forget=False):
        """요청 번호 기억 (10분). result를 주면 적고, forget이면 지우고, 둘 다 아니면 처리한 적 있을 때 그 결과(없으면 None)."""
        now = time.time()
        with self.lock:
            done = self.__dict__.setdefault('_done', {})
            for k in [k for k, (t, _r) in done.items() if now - t > 600]:
                del done[k]
            if forget:
                done.pop(key, None)
                return None
            if result is not None:
                done[key] = (now, dict(result, repeat=True) if isinstance(result, dict) else result)
                return result
            hit = done.get(key)
            return hit[1] if hit else None

    def pump(self):
        """Tk 쪽에서 자주 불러 큐에 쌓인 일을 처리한다."""
        for _ in range(20):
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                return
            fn()


class _Handler(http.server.BaseHTTPRequestHandler):
    remote = None
    server_version = 'ClaudeOffice'
    sys_version = ''

    def log_message(self, *a):
        pass

    def _cors(self):
        origin = self.headers.get('Origin', '')
        if origin == self.remote.app_origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-If-Same, X-Req-Id')
            self.send_header('Access-Control-Expose-Headers', 'X-Etag')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Max-Age', '600')

    def _send(self, code, body, ctype='application/json; charset=utf-8', cache='no-store'):
        etag = None
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
            if code == 200:  # 지난번과 내용이 같으면 짧은 '같음'만 보낸다 (휴대폰 데이터 절약; 304는 브라우저가 끊어 버려 쓰지 않음)
                etag = hashlib.sha1(body).hexdigest()[:20]
                if self.headers.get('X-If-Same') == etag:
                    body = b'{"same":true}'
        self.send_response(code)
        if etag:
            self.send_header('X-Etag', etag)
        self._cors()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def _handle(self, method):
        u = urllib.parse.urlsplit(self.path)
        if u.path == '/api/ping':
            return self._send(200, {'ok': True, 'app': 'claude-office'})
        ip = self.headers.get('CF-Connecting-IP') or self.client_address[0]
        dev = self.remote.auth(self.headers.get('Authorization', ''), ip)
        if not dev:
            time.sleep(0.4)
            return self._send(401, {'error': 'unauthorized'})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        body = {}
        if method == 'POST':
            n = min(int(self.headers.get('Content-Length') or 0), 64_000)
            try:
                body = json.loads(self.rfile.read(n) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
        fn = API.get((method, u.path))
        if not fn:
            return self._send(404, {'error': 'not found'})
        # 같은 요청 번호로 다시 오면(응답이 끊겨 휴대폰이 다시 보냄) 한 번만 처리하고 처음 결과를 돌려준다
        rid = self.headers.get('X-Req-Id', '') if method == 'POST' else ''
        rid = rid if re.fullmatch(r'[A-Za-z0-9-]{8,48}', rid) else ''
        if rid:
            done = self.remote.seen_req(dev['id'] + rid)
            if done is not None:
                return self._send(200, done)
            self.remote.seen_req(dev['id'] + rid, {'ok': True, 'pending': True})  # 처리 중에 다시 와도 또 실행하지 않게
        try:
            res = fn(self.remote, dev, q, body)
        except Exception as ex:
            if rid:  # 실행되지 않았으니 같은 번호로 다시 보낼 수 있게 지운다
                self.remote.seen_req(dev['id'] + rid, forget=True)
            if isinstance(ex, TimeoutError):
                return self._send(503, {'error': 'busy'})
            if isinstance(ex, ValueError):
                return self._send(400, {'error': str(ex)})
            print('remote api error', u.path, repr(ex), file=sys.stderr)
            return self._send(500, {'error': 'internal'})
        if rid:
            self.remote.seen_req(dev['id'] + rid, res)
        if isinstance(res, tuple):  # (바이트, 형식, 캐시)
            return self._send(200, res[0], res[1], res[2])
        self._send(200, res)


# ───────────────────────────── API ─────────────────────────────
def _clip(s, n):
    s = str(s or '')
    return s if len(s) <= n else s[:n] + '…'


def api_state(r, dev, q, body):
    app, co = r.app, r.co

    def build():
        mon = app.mon
        out = []
        with mon.lock:
            live = app.sort_sessions(mon.sessions.values())
            for s in live:
                acc = s.acc
                dv = acc.days.get(co.today(), [0, 0, 0, 0])
                peer = s.peer or {}
                out.append({'sid': s.sid, 'name': mon.display_name(s), 'mode': s.mode(), 'waiting': s.waiting,
                            'tool': _clip(s.last_tool, 120), 'cwd': s.cwd, 'started': s.started, 'model': acc.model,
                            'ctx': acc.ctx, 'tok': dv[0] + dv[1] + dv[2] + dv[3], 'goal': _clip(peer.get('goal'), 200),
                            'progress': s.overall(),
                            'tasks': [{'t': _clip(t.get('title'), 100), 's': t.get('status'), 'p': t.get('percent')}
                                      for t in (peer.get('tasks') or [])[:12] if isinstance(t, dict)],
                            'av': app.avatars.get(s.sid) or {}})
            chat_ver = mon.chat_ver
        sec = mon.sec or {}
        items = []
        for it in app.sec_items(all_=True):
            items.append({'key': it['_key'], 'kind': it.get('kind') or 'info', 'title': _clip(it.get('title'), 200),
                          'detail': _clip(it.get('detail'), 1500), 'target': _clip(it.get('target'), 60),
                          'url': it.get('url') if str(it.get('url') or '').startswith(('http://', 'https://')) else '',
                          'done': it['_key'] in app.sec_done,
                          'choices': [{'i': i, 'label': ch['label'], 'on': bool(ch.get('sid'))}
                                      for i, ch in enumerate(app.sec_choices(it))]})
        return {'v': 1, 'lang': co.LANG, 'sessions': out, 'chat_ver': chat_ver,
                'sec': {'sid': app.sec_sid, 'summary': _clip(sec.get('summary'), 600), 'updated': sec.get('updatedAt'),
                        'briefing': app.briefing(), 'items': items},
                'meeting': _meeting(r), 'alerts': r.alerts[-10:]}

    return r.ui(build)


def _meeting(r):
    co = r.co
    m = co.read_json(co.MEET_FILE)
    if not (isinstance(m, dict) and m.get('id') and isinstance(m.get('members'), list)):
        return None
    folder = os.path.join(co.MEET_DIR, str(m['id']))
    mon = r.app.mon
    roster = []
    with mon.lock:
        alive = {sid: (mon.display_name(s), s.mode()) for sid, s in mon.sessions.items()}
    for sid in m['members']:
        name, mode = alive.get(sid, ((m.get('names') or {}).get(sid) or sid[:8], None))
        rep = co.read_json(os.path.join(folder, sid + '.json'))
        if not (isinstance(rep, dict) and (rep.get('summary') or rep.get('items'))):
            rep = None
        if rep:
            items = []
            for it in (rep.get('items') or [])[:20]:
                if not isinstance(it, dict):
                    continue
                imgs = []
                for p in it.get('images') or []:
                    name_ = os.path.basename(str(p))
                    if re.fullmatch(r'[\w.-]+\.(png|gif)', name_, re.I) and os.path.isfile(os.path.join(folder, name_)):
                        imgs.append(name_)
                items.append({'request': _clip(it.get('request'), 300), 'status': it.get('status') or 'done',
                              'result': _clip(it.get('result'), 2000), 'files': [_clip(f, 200) for f in (it.get('files') or [])[:8]],
                              'images': imgs})
            rep = {'title': _clip(rep.get('title'), 200), 'summary': _clip(rep.get('summary'), 1500), 'items': items,
                   'next': _clip(rep.get('next'), 800)}
        roster.append({'sid': sid, 'name': name, 'mode': mode, 'report': rep, 'av': r.app.avatars.get(sid) or {}})
    return {'id': m['id'], 'started': m.get('startedAt'), 'turns': m.get('turns'), 'roster': roster,
            'presented': m.get('presented') or []}


def api_session(r, dev, q, body):
    sid = q.get('sid', '')
    mon = r.app.mon
    with mon.lock:
        s = mon.sessions.get(sid)
        if not s:
            raise ValueError('no session')
        qa = [{'q': _clip(e[1], 3000), 'qt': e[0], 'a': _clip(e[3], 6000), 'at': e[2]} for e in list(s.qa)[-15:]]
        ev = [{'ts': e[1], 'k': e[2], 't': _clip(e[3], 400)} for e in list(s.events)[-80:]]
    return {'sid': sid, 'qa': qa, 'events': ev}


def api_chat(r, dev, q, body):
    co, app = r.co, r.app

    def build():
        fake = type('F', (), {})()
        fake.app = app
        msgs, live, names, typing = co.ChatWin.collect(fake)
        room = q.get('room', '*')
        if room != '*':
            msgs = [m for m in msgs if m['from'] == room or m['to'] == room]
        out = [{'id': m['id'], 'from': m['from'], 'to': m['to'], 'fromLabel': m.get('fromLabel', ''), 'toLabel': m.get('toLabel', ''),
                'kind': m.get('kind', ''), 'text': _clip(m.get('text'), 3000), 'ts': m['ts'], 'read': bool(m.get('read')),
                'reply': bool(m.get('reply'))} for m in msgs[-80:]]
        return {'msgs': out, 'typing': sorted(typing)}

    return r.ui(build)


def api_send(r, dev, q, body):
    sid, text = str(body.get('sid') or ''), str(body.get('text') or '').strip()
    if not text or len(text) > 8000:
        raise ValueError('text')

    def run():
        if sid not in r.app.mon.sessions:
            raise ValueError('session is off')
        r.app.mon.send_user(sid, text)
        return {'ok': True}

    return r.ui(run)


def api_brief(r, dev, q, body):
    def run():
        app = r.app
        if not app.sec_sid:
            raise ValueError('no secretary')
        if app.briefing():
            return {'ok': True, 'already': True}
        app.mon.send_user(app.sec_sid, r.co.BRIEF_TEXT)
        app.brief_at = time.time()
        app.draw_sec_board()
        return {'ok': True}

    return r.ui(run)


def api_choice(r, dev, q, body):
    key, i = str(body.get('key') or ''), int(body.get('i') or 0)

    def run():
        app = r.app
        it = next((x for x in app.sec_items(all_=True) if x['_key'] == key), None)
        chs = app.sec_choices(it) if it else []
        if not it or not 0 <= i < len(chs) or not chs[i].get('sid'):
            raise ValueError('choice')
        app.send_choice(it, chs[i])
        return {'ok': True}

    return r.ui(run)


def api_secmark(r, dev, q, body):
    key, done = str(body.get('key') or ''), bool(body.get('done', True))
    return r.ui(lambda: (r.app.sec_mark(key, done), {'ok': True})[1])


def api_meet_start(r, dev, q, body):
    sids = [str(x) for x in body.get('sids') or []][:12]
    turns = int(body.get('turns') or 3)
    if turns not in r.co.MEET_TURNS or not sids:
        raise ValueError('sids/turns')
    return r.ui(lambda: {'ok': True, 'sent': r.co.meeting_start(r.app, sids, turns)})


def api_meet_end(r, dev, q, body):
    def run():
        try:
            os.remove(r.co.MEET_FILE)
        except OSError:
            pass
        return {'ok': True}

    return r.ui(run)


def api_meet_presented(r, dev, q, body):
    sid = str(body.get('sid') or '')
    co = r.co

    def run():
        m = co.read_json(co.MEET_FILE)
        if isinstance(m, dict) and sid in (m.get('members') or []):
            m['presented'] = sorted(set(m.get('presented') or []) | {sid})
            co.write_json(co.MEET_FILE, m)
        return {'ok': True}

    return r.ui(run)


def api_img(r, dev, q, body):
    co = r.co
    mid, name = q.get('m', ''), q.get('f', '')
    if not re.fullmatch(r'[\w-]+', mid) or not re.fullmatch(r'[\w.-]+\.(png|gif)', name, re.I):
        raise ValueError('name')
    p = os.path.join(co.MEET_DIR, mid, name)
    if not os.path.isfile(p):
        raise ValueError('missing')
    with open(p, 'rb') as f:
        data = f.read()
    return data, 'image/png' if name.lower().endswith('.png') else 'image/gif', 'private, max-age=3600'


def api_sprite(r, dev, q, body):
    sid, pose = q.get('sid', ''), q.get('pose', 'idle')
    sc = max(2, min(8, int(q.get('s') or 4)))
    if not re.fullmatch(r'[0-9a-f-]{8,40}', sid):
        raise ValueError('sid')
    av = r.app.avatars.get(sid) or {}
    key = (sid, pose, sc, json.dumps(av, sort_keys=True))
    if key not in r.sprites:
        if len(r.sprites) > 400:
            r.sprites.clear()
        r.sprites[key] = sprite_png(r.co, sid, pose, sc, av)
    return r.sprites[key], 'image/png', 'private, max-age=600'


def api_past(r, dev, q, body):
    co = r.co
    with r.app.mon.lock:
        live = set(r.app.mon.sessions)
    names = r.app.mon.names
    out = []
    for it in co.scan_past(live, limit=40):
        out.append({'sid': it['sid'], 'name': _clip(names.get(it['sid']) or it['title'] or it['first'] or
                                                    os.path.basename(it['cwd'].rstrip('\\/')), 60),
                    'cwd': it['cwd'], 'mtime': it['mtime'], 'last': _clip(it['last'] or it['first'], 160)})
    with r.app.mon.lock:
        live_cwds = [s.cwd for s in r.app.mon.sessions.values()]
    cwds = []
    for x in [r.app.cfg.get('last_cwd') or ''] + live_cwds + [it['cwd'] for it in out]:
        if x and x not in cwds and os.path.isdir(x):
            cwds.append(x)
    return {'past': out, 'cwds': cwds[:12]}


def api_resume(r, dev, q, body):
    sid = str(body.get('sid') or '')
    if not re.fullmatch(r'[0-9a-f-]{36}', sid):
        raise ValueError('sid')

    def run():
        hits = r.co.glob.glob(os.path.join(r.co.PROJ, '*', sid + '.jsonl'))
        info = r.co.past_session(hits[0]) if hits else None
        if not info or not os.path.isdir(info.get('cwd') or ''):
            raise ValueError('folder missing')
        return {'ok': bool(r.app.resume_session(sid, info))}

    return r.ui(run)


def api_new(r, dev, q, body):
    cwd, name = str(body.get('cwd') or ''), str(body.get('name') or '')[:40]
    if not os.path.isdir(cwd):
        raise ValueError('folder missing')

    def run():
        r.app.cfg['last_cwd'] = cwd
        return {'ok': True, 'sid': r.app.new_session(cwd, name)}

    return r.ui(run)


def api_hello(r, dev, q, body):
    name = str(body.get('name') or '')[:40]
    if name:
        with r.lock:
            for d in r.cfg['devices']:
                if d['id'] == dev['id']:
                    d['name'] = name
        r.save()
    return {'ok': True, 'device': dev['id'], 'vapid': b64u(r.pub), 'lang': r.co.LANG}


def api_push(r, dev, q, body):
    sub = body.get('sub')
    if sub is not None and not (isinstance(sub, dict) and str(sub.get('endpoint', '')).startswith('https://')):
        raise ValueError('sub')
    with r.lock:
        for d in r.cfg['devices']:
            if d['id'] == dev['id']:
                d['push'] = sub
    r.save()
    if sub and body.get('test'):
        r.alert('Claude Office', '✓ push', '')
    return {'ok': True}


def api_alerts(r, dev, q, body):
    since = int(q.get('since') or 0)
    return {'alerts': [a for a in r.alerts if a['ts'] > since][-10:]}


API = {
    ('GET', '/api/state'): api_state, ('GET', '/api/session'): api_session, ('GET', '/api/chat'): api_chat,
    ('POST', '/api/send'): api_send, ('POST', '/api/brief'): api_brief, ('POST', '/api/choice'): api_choice,
    ('POST', '/api/secmark'): api_secmark, ('POST', '/api/meeting/start'): api_meet_start,
    ('POST', '/api/meeting/end'): api_meet_end, ('POST', '/api/meeting/presented'): api_meet_presented,
    ('GET', '/api/img'): api_img, ('GET', '/api/sprite'): api_sprite, ('GET', '/api/past'): api_past,
    ('POST', '/api/resume'): api_resume, ('POST', '/api/new'): api_new, ('POST', '/api/hello'): api_hello,
    ('POST', '/api/push'): api_push, ('GET', '/api/alerts'): api_alerts,
}
