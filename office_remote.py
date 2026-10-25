"""세션 사무실 원격 조종: 휴대폰 웹앱(PWA)이 부르는 API 서버와 그 둘레.

- 서버는 127.0.0.1에서만 열고, 바깥 연결은 Cloudflare 임시 터널(cloudflared)이 PC에서 밖으로 연다 (공유기 포트를 열지 않음).
- 터널 주소는 켤 때마다 바뀌므로 ntfy.sh의 비밀 토픽에 지금 주소를 적어 두고, 웹앱은 거기서 찾아온다.
- 기기마다 무작위 토큰을 건네고, PC에는 토큰의 해시만 둔다. 모든 API는 토큰이 있어야 한다.
  토큰은 QR의 한 번용 코드(10분, 한 기기)나, 사용자가 정한 암호(복사한 주소로 들어온 기기)와 바꿔서만 받는다.
- PC는 자기 서명 열쇠를 가진다. 휴대폰은 링크로 받은 공개 열쇠로 PC의 서명을 확인한 뒤에만 토큰·암호를 보낸다
  (ntfy 토픽이 새어 누가 가짜 주소를 올려도, 그 주소로는 아무것도 보내지 않는다).
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
PAIR_SECS = 600                                    # QR 한 번용 코드 유효 시간
PW_ITER = 300_000                                  # 암호 해시(PBKDF2) 반복 횟수
PW_MIN = 6
PUSH_HOSTS = ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com')
PUSH_SUFFIX = ('.notify.windows.com', '.push.apple.com', '.push.services.mozilla.com')
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


def _ec_mul_ladder(k, pt=_G):
    """서명용 곱셈: 비밀 수를 늘 257비트로 맞추고 비트마다 같은 일(더하기+두 배)을 해서, 걸린 시간으로 비밀이 덜 드러나게."""
    k = k + _N if (k + _N).bit_length() > 256 else k + 2 * _N
    r0, r1 = pt, _ec_add(pt, pt)
    for i in range(255, -1, -1):
        if (k >> i) & 1:
            r0, r1 = _ec_add(r0, r1), _ec_add(r1, r1)
        else:
            r1, r0 = _ec_add(r0, r1), _ec_add(r0, r0)
    return r0


def vapid_keys(priv=None):
    priv = priv or (secrets.randbelow(_N - 1) + 1)
    x, y = _ec_mul(priv)
    return priv, b'\x04' + x.to_bytes(32, 'big') + y.to_bytes(32, 'big')


def es256_sign(priv, msg):
    z = int.from_bytes(hashlib.sha256(msg).digest(), 'big')
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _ec_mul_ladder(k)[0] % _N
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
        if not self.cfg.get('idkey'):  # PC임을 휴대폰에 증명하는 서명 열쇠 (푸시 열쇠와 따로)
            self.cfg['idkey'] = hex(vapid_keys()[0])
        self.cfg.setdefault('devices', [])
        self.priv, self.pub = vapid_keys(int(self.cfg['vapid'], 16))
        self.id_priv, self.id_pub = vapid_keys(int(self.cfg['idkey'], 16))
        self.codes = {}          # QR 한 번용 코드의 해시 → (만료 시각, 만들 기기 id)
        self.fail_log = {}       # (종류, IP) → 실패 시각들 (막무가내 시도 막기)
        self.pw_fails = []       # 암호 실패 시각 (모든 IP 합쳐서)
        self.pw_locked = 0       # 이 시각까지 암호 연결을 막는다
        self.signs = []          # 최근 서명 시각 (서명을 마구 시키지 못하게)
        self.lock = threading.Lock()
        self.httpd = self.tunnel = None
        self.url = None
        self.status = 'off'      # off / starting / on / no_cloudflared / error
        self.log = []
        self.alerts = []         # 휴대폰에 알릴 것 [{id, ts, title, body, sid}]
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
    def start(self, tunnel=True):
        """서버를 켠다. tunnel=False면 이 PC 안에서만 (화면 미리보기용) — 바깥 연결·주소 알림 없음."""
        if self.tunnel or (self.httpd and not tunnel):
            return True
        exe = find_cloudflared(self.co.CACHE_DIR) if tunnel else None
        if tunnel and not exe:
            self.status = 'no_cloudflared'
            return False
        if not self.httpd:
            handler = type('H', (_Handler,), {'remote': self})
            self.httpd = _Server(('127.0.0.1', 0), handler)
            self.port = self.httpd.server_address[1]
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            self.note(f'API 127.0.0.1:{self.port}')
        if not tunnel:
            self.status = 'local'
            return True
        self.status = 'starting'
        self.tunnel = Tunnel(exe, self.port, self._on_url, self.note)
        threading.Thread(target=self._announce_loop, daemon=True).start()
        return True

    @property
    def local_url(self):
        return f'http://127.0.0.1:{self.port}' if self.httpd else None

    def preview_link(self):
        """PC에서 휴대폰 화면을 띄워 볼 주소. 미리보기 기기는 하나만 두고 열 때마다 열쇠를 새로 만든다."""
        with self.lock:
            self.cfg['devices'] = [d for d in self.cfg['devices'] if not d.get('preview')]
        _dev, tok = self._new_device('PC preview', 'preview')
        frag = urllib.parse.urlencode({'k': tok, 't': self.cfg['topic'], 'u': self.local_url, 's': b64u(self.id_pub)})
        return f'{self.local_url}/m/#{frag}'

    def stop(self, wait=0):
        """끈다. 터널을 열었었다면 휴대폰이 '꺼짐'을 알 수 있게 빈 주소를 알린다 (wait초까지 기다림 — 프로그램을 닫을 때)."""
        had_tunnel = bool(self.tunnel)
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
        if not had_tunnel:
            return
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
            ts = int(time.time())
            body = json.dumps({'u': url or '', 'ts': ts, 'sig': self.sign(f'co-url|{url or ""}|{ts}')}).encode()
            req = urllib.request.Request(NTFY + self.cfg['topic'], data=body, method='POST',
                                         headers={'Title': 'claude-office', 'Priority': 'min', 'Tags': 'computer'})
            urllib.request.urlopen(req, timeout=15).read()
            self.note('announced' if url else 'announced off')
        except (OSError, urllib.error.URLError) as ex:
            self.note(f'ntfy: {ex}')

    # ── 기기 ──
    def sign(self, text):
        return b64u(es256_sign(self.id_priv, text.encode()))

    @property
    def fingerprint(self):
        """PC 확인 코드: 휴대폰의 암호 화면에도 같은 코드가 보인다 (다른 PC·가짜 페이지가 아닌지 눈으로 확인)."""
        h = hashlib.sha256(self.id_pub).hexdigest()[:8].upper()
        return h[:4] + '-' + h[4:]

    def _new_device(self, name, via, dev_id=None):
        tok = secrets.token_urlsafe(24)
        now = int(time.time())
        dev = {'id': dev_id or secrets.token_hex(4), 'name': _clip(name, 40) or getattr(self.co, 'REMOTE_DEVICE', 'New device'), 'hash': hashlib.sha256(tok.encode()).hexdigest(),
               'created': now, 'seen': now, 'push': None, 'via': via}
        if via == 'preview':
            dev['preview'] = True
        with self.lock:
            self.cfg['devices'].append(dev)
        self.save()
        return dev, tok

    def _frag(self, **extra):
        return urllib.parse.urlencode(dict(extra, t=self.cfg['topic'], u=self.url or '', s=b64u(self.id_pub)))

    def pair(self, name=''):
        """QR에 담을 링크: 한 번만 쓰는 코드(10분)를 담는다. 휴대폰이 이 코드를 기기 열쇠로 바꾸는 순간 코드는 사라져서,
        링크가 남에게 새어도 다시 쓸 수 없다."""
        code = secrets.token_urlsafe(16)
        dev_id = secrets.token_hex(4)
        now = time.time()
        with self.lock:
            self.codes = {h: v for h, v in self.codes.items() if v[0] > now}
            self.codes[hashlib.sha256(code.encode()).hexdigest()] = (now + PAIR_SECS, dev_id)
        return dev_id, f'{self.app_url}#{self._frag(p=code)}'

    def cancel_pair(self, dev_id):
        with self.lock:
            self.codes = {h: v for h, v in self.codes.items() if v[1] != dev_id}

    def redeem(self, code, name, ip):
        """한 번용 코드 → 기기 열쇠. 틀리거나 지난 코드면 None."""
        with self.lock:
            ent = self.codes.pop(hashlib.sha256(code.encode()).hexdigest(), None) if code else None
        if not ent or ent[0] < time.time():
            self.fail('code', ip)
            return None
        dev, tok = self._new_device(name, 'qr', ent[1])
        self.note(f'paired (QR) {dev["name"]}')
        return tok

    def share_link(self):
        """복사해서 건네는 주소: 열쇠·코드는 없다. 이 주소로 들어온 기기는 암호를 맞혀야 연결된다."""
        return f'{self.app_url}#{self._frag()}'

    # ── 암호 ──
    @property
    def has_password(self):
        return bool(self.cfg.get('pw'))

    def set_password(self, pw):
        if len(pw) < PW_MIN or len(pw) > 128:
            raise ValueError('length')
        salt = secrets.token_bytes(16)
        with self.lock:
            self.cfg['pw'] = {'salt': salt.hex(), 'iter': PW_ITER,
                              'hash': hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, PW_ITER).hex()}
            self.pw_fails, self.pw_locked = [], 0
        self.save()
        self.note('password set')

    def clear_password(self):
        with self.lock:
            self.cfg.pop('pw', None)
        self.save()
        self.note('password removed')

    def revoke_all(self):
        with self.lock:
            self.cfg['devices'] = [d for d in self.cfg['devices'] if d.get('preview')]
        self.save()

    def login(self, pw, name, ip):
        """암호 → 기기 열쇠. ('ok', 열쇠) / ('nopw'|'locked'|'bad', None).
        한 IP에서 15분에 5번, 모두 합쳐 1시간에 20번 틀리면 막는다 (막히면 PC 화면과 휴대폰 알림으로 알린다)."""
        spec = self.cfg.get('pw')
        if not spec:
            return 'nopw', None
        now = time.time()
        if now < self.pw_locked or self.too_many('pw', ip, 5, 900):
            return 'locked', None
        got = hashlib.pbkdf2_hmac('sha256', pw.encode()[:256], bytes.fromhex(spec['salt']), int(spec['iter'])).hex()
        if not hmac.compare_digest(got, spec['hash']):
            self.fail('pw', ip)
            with self.lock:
                self.pw_fails = [t for t in self.pw_fails if now - t < 3600] + [now]
                lock = len(self.pw_fails) >= 20
                if lock:
                    self.pw_locked, self.pw_fails = now + 3600, []
            self.note(f'wrong password ({ip})')
            if lock:
                self.note('password login locked for 1h')
                self.alert('Claude Office', getattr(self.co, 'REMOTE_LOCKED', 'Too many wrong passwords. Password login is locked for 1 hour.'))
            return 'bad', None
        dev, tok = self._new_device(name, 'pw')
        self.note(f'paired (password) {dev["name"]}')
        self.alert('Claude Office', getattr(self.co, 'REMOTE_NEW_DEVICE', 'A new device connected with the password · {0}').format(dev['name']))
        return 'ok', tok

    # ── 실패 기록 ──
    def fail(self, kind, ip):
        now = time.time()
        with self.lock:
            if len(self.fail_log) > 5000:
                self.fail_log.clear()
            k = (kind, ip)
            self.fail_log[k] = [t for t in self.fail_log.get(k, []) if now - t < 3600] + [now]

    def too_many(self, kind, ip, n, secs):
        now = time.time()
        with self.lock:
            return len([t for t in self.fail_log.get((kind, ip), []) if now - t < secs]) >= n

    def may_sign(self):
        """주소 확인 서명은 10초에 30번까지만 (서명을 마구 시켜 PC를 바쁘게 하거나 열쇠를 캐내려는 시도 막기)."""
        now = time.time()
        with self.lock:
            self.signs = [t for t in self.signs if now - t < 10]
            if len(self.signs) >= 30:
                return False
            self.signs.append(now)
            return True

    def revoke(self, dev_id):
        with self.lock:
            self.cfg['devices'] = [d for d in self.cfg['devices'] if d['id'] != dev_id]
        self.save()

    def auth(self, header, ip):
        """맞는 열쇠는 늘 통과한다 (남이 틀린 열쇠를 마구 보내도 내 기기가 막히지 않게). 틀린 열쇠는 IP별로 적어 늦게 답한다."""
        tok = header[7:] if header.startswith('Bearer ') else ''
        hsh = hashlib.sha256(tok.encode()).hexdigest()
        with self.lock:
            dev = next((d for d in self.cfg['devices'] if hmac.compare_digest(d['hash'], hsh)), None) if tok else None
            if dev:
                dev['seen'] = int(time.time())
        if not dev:
            self.fail('tok', ip)
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
        if not push_ok(ep):
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

    STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                    '.png': 'image/png', '.webmanifest': 'application/manifest+json'}

    def _static(self, path):
        """휴대폰 웹앱 파일(docs/m)을 그대로 내준다 — PC 안 미리보기, 또는 GitHub Pages 없이 터널 주소로 열 때. 비밀은 없다."""
        if path in ('/', '/m'):
            self.send_response(302)
            self.send_header('Location', '/m/')
            self.end_headers()
            return
        name = path[3:] or 'index.html'
        root = os.path.join(self.remote.co.APP_DIR, 'docs', 'm')
        ext = os.path.splitext(name)[1].lower()
        p = os.path.join(root, name)
        if not re.fullmatch(r'[\w.-]+', name) or ext not in self.STATIC_TYPES or not os.path.isfile(p):
            return self._send(404, {'error': 'not found'})
        with open(p, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', self.STATIC_TYPES[ext])
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def _body(self):
        """POST 본문(JSON 객체). 크기가 이상하거나 JSON이 아니면 None."""
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return None
        if n < 0 or n > 64_000:
            return None
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _ping(self, q):
        """살아 있는지 + PC 서명: 휴대폰이 보낸 무작위 값과 '휴대폰이 부른 주소'에 서명해, 가짜 서버가 진짜 PC에
        물어 온 서명을 그대로 내밀어도 주소가 달라 걸리게 한다."""
        r = self.remote
        out = {'ok': True, 'app': 'claude-office', 'pw': r.has_password}
        n = q.get('n', '')
        if re.fullmatch(r'[A-Za-z0-9_-]{16,64}', n) and r.may_sign():
            via_tunnel = bool(self.headers.get('CF-Connecting-IP') or self.headers.get('Cf-Ray'))
            base = r.url if via_tunnel else r.local_url
            if base:
                out['sig'] = r.sign(f'co-ping|{n}|{base}')
        return self._send(200, out)

    def _open_api(self, path, ip):
        """열쇠 없이 부르는 것: 한 번용 코드나 암호를 기기 열쇠로 바꾼다."""
        r = self.remote
        body = self._body()
        if body is None:
            return self._send(400, {'error': 'bad json'})
        name = str(body.get('name') or '')[:40]
        if path == '/api/pair':
            tok = r.redeem(str(body.get('code') or '')[:64], name, ip)
            if not tok:
                time.sleep(0.6)
                return self._send(401, {'error': 'expired'})
            return self._send(200, {'k': tok})
        status, tok = r.login(str(body.get('pw') or '')[:256], name, ip)
        if status == 'ok':
            return self._send(200, {'k': tok})
        time.sleep(1.0)
        return self._send({'nopw': 403, 'locked': 429}.get(status, 401), {'error': status})

    def _handle(self, method):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == '/api/ping':
            return self._ping(q)
        if method == 'GET' and (u.path in ('/', '/m') or u.path.startswith('/m/')):
            return self._static(u.path)
        ip = self.headers.get('CF-Connecting-IP') or self.client_address[0]
        if method == 'POST' and u.path in ('/api/pair', '/api/login'):
            return self._open_api(u.path, ip)
        dev = self.remote.auth(self.headers.get('Authorization', ''), ip)
        if not dev:
            time.sleep(1.5 if self.remote.too_many('tok', ip, 30, 600) else 0.4)
            return self._send(401, {'error': 'unauthorized'})
        body = {}
        if method == 'POST':
            body = self._body()
            if body is None:
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
def push_ok(ep):
    """알려진 웹 푸시 서비스 주소만 (PC가 아무 주소로나 요청을 보내게 쓰이지 않게)."""
    u = urllib.parse.urlsplit(ep)
    host = (u.hostname or '').lower()
    return u.scheme == 'https' and (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIX))


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
    cwd, name = str(body.get('cwd') or ''), str(body.get('name') or '')[:40].lstrip('-').strip()  # 이름이 claude 옵션으로 읽히지 않게
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
    return {'ok': True, 'device': dev['id'], 'vapid': b64u(r.pub), 'id': b64u(r.id_pub), 'lang': r.co.LANG}


def api_push(r, dev, q, body):
    sub = body.get('sub')
    if sub is not None and not (isinstance(sub, dict) and push_ok(str(sub.get('endpoint', '')))):
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
