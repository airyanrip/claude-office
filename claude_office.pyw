# -*- coding: utf-8 -*-
"""Claude Office - 이 PC에서 돌아가는 Claude Code 세션들을 사무실 캐릭터로 보여 주는 모니터.

데이터 출처 (모두 읽기 전용):
  ~/.claude/sessions/<pid>.json          세션 id, 작업 폴더, 상태(busy/idle/waiting), 이름
  ~/.claude/session-collab/peer-*.json   session-panel mod 진행 보고(목표, 단계, 진행도)
  ~/.claude/session-collab/out-*.json    세션 간 메시지
  ~/.claude/projects/<폴더>/<세션>.jsonl  대화 기록 (토큰, 도구 호출, 요청 로그)

가볍게 돌도록: 표준 라이브러리만 사용, 대화 기록은 새로 붙은 바이트만 읽고,
토큰 총계는 한 번 훑은 뒤 %LOCALAPPDATA%\\ClaudeOffice\\tokens.json 에 저장해 다음부터는 이어서 읽는다.
"""
import base64
import collections
import ctypes
import datetime
import glob
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import uuid
import time
import tkinter as tk
import tkinter.filedialog as tkfile
import tkinter.font as tkfont
import tkinter.messagebox as tkmsg
import webbrowser
from ctypes import wintypes

HOME = os.path.expanduser('~')
CLAUDE = os.path.join(HOME, '.claude')
SESS_DIR = os.path.join(CLAUDE, 'sessions')
COLLAB = os.path.join(CLAUDE, 'session-collab')
PROJ = os.path.join(CLAUDE, 'projects')
CACHE_DIR = os.path.join(os.environ.get('LOCALAPPDATA', HOME), 'ClaudeOffice')
CACHE_FILE = os.path.join(CACHE_DIR, 'tokens.json')

POLL_SEC = 2.0
TAIL_BYTES = 4_000_000          # 시작할 때 로그용으로 읽는 대화 기록 끝부분
QA_MAX = 8000                   # 대화 탭: 요청·답변 하나의 최대 글자 수
QA_KEEP = 60                    # 대화 탭: 세션마다 기억하는 Q&A 수
TALK_HISTORY = 20               # 세션톡: 세션마다 보여 주는 최근 대화 (터미널에서 직접 나눈 것 포함)
TALK_PAGE = 50                  # 세션톡: 처음에 그리는 메시지 수 (위의 [이전 대화 더 보기]로 더)
TALK_IMG_RECENT = 20            # 세션톡: 사진을 바로 보여 주는 최근 메시지 수 (약 10턴). 그보다 오래된 건 눌러야 보인다
TALK_IMG_MAX = 300 * 1024 * 1024  # 세션톡 사진 폴더가 이보다 커지면 오래된 것부터 지운다
QA_SCAN = 1_500_000_000         # 대화 탭: 요청을 찾으러 거슬러 올라가는 최대 바이트
QA_ANSWER_SCAN = 64_000_000     # 대화 탭: 요청 하나의 답변을 찾는 최대 바이트
LIVE_CHUNK = 16 * 1024 * 1024   # 한 번 폴링에 읽는 최대 바이트
SLEEP_AFTER = 15 * 60           # 이만큼 조용하면 조는 캐릭터
UI = 'Malgun Gothic'            # Pretendard를 못 올렸을 때 쓰는 글꼴
APP_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(APP_DIR, 'fonts')   # Pretendard (SIL OFL 1.1, 무료)
SKILL_SRC = os.path.join(APP_DIR, 'assets', 'secretary')
SKILL_DIR = os.path.join(CLAUDE, 'skills', 'secretary')
MOD_SRC = os.path.join(APP_DIR, 'mod', 'session-panel')     # 이 프로그램에 든 session-panel mod
MOD_DIR = os.path.join(CLAUDE, 'mods', 'session-panel')     # 설치할 곳
CLAUDE_SETTINGS = os.path.join(CLAUDE, 'settings.json')
MOD_ENV = 'CLAUDE_CODE_PLUGIN_DIRS'                         # Claude Code가 mod를 불러오는 폴더 목록
MOD_WATCH = 'CLAUDE_CODE_PLUGIN_DIR_WATCH'                  # 데스크탑 앱 세션도 mod가 바뀌면 바로 다시 불러오게
PEER_LIVE = 20                                              # mod 상태 파일이 이 초 안에 새로 적혔으면 살아 있는 세션
MOD_STALE = 60                                              # 켠 지 이만큼 지났는데 mod 상태 파일이 없으면 mod 없이 켜진 세션


def load_fonts():
    """fonts 폴더의 글꼴을 이 프로세스에만 올린다 (설치하지 않음). Tk를 만들기 전에 불러야 한다."""
    n = 0
    for p in glob.glob(os.path.join(FONT_DIR, '*.otf')) + glob.glob(os.path.join(FONT_DIR, '*.ttf')):
        try:
            n += ctypes.windll.gdi32.AddFontResourceExW(p, 0x10, 0) > 0  # FR_PRIVATE
        except Exception:
            pass
    return n


def claude_exe():
    return shutil.which('claude') or os.path.join(HOME, '.local', 'bin', 'claude.exe')


# 세션 사무실을 Claude 세션 안에서 켰을 때 물려받는 변수 — 새 세션이 자기를 하위 세션으로 착각하지 않게 뺀다
_CHILD_ENV = {'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT', 'AI_AGENT', 'CLAUDE_SESSION_ID', 'CLAUDE_CODE_CHILD_SESSION',
              'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH',
              'CLAUDE_CODE_PLUGIN_DIRS', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_SSE_PORT'}


def launch_claude(cwd, args):
    """새 콘솔 창에서 claude를 켠다 (터미널에서도 보이고, 사무실에는 세션 파일로 출근한다)."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _CHILD_ENV}
    subprocess.Popen([claude_exe()] + list(args), cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)

# ───────────────────────────── Win32 프로세스 정보 ─────────────────────────────
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]


class PMC(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]


def _ft(f):
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


def proc_stat(pid):
    """(생성 FILETIME, CPU 100ns 누적, 워킹셋 바이트) 또는 죽었으면 None."""
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
            return None
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ws = pmc.WorkingSetSize if k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0
        return _ft(c), _ft(kt) + _ft(ut), ws
    finally:
        k32.CloseHandle(h)


# ───────────────────────────── 공용 도우미 ─────────────────────────────
_ASTRAL = re.compile('[\U00010000-\U0010FFFF]')
_CTRL = re.compile('[\x00-\x08\x0b-\x1f\x7f]')


def clean(s, n=None):
    """Tk 8.6이 못 그리는 이모지·제어문자 제거, 길이 제한."""
    s = _CTRL.sub('', _ASTRAL.sub('', str(s)))
    if n and len(s) > n:
        s = s[:n - 1] + '…'
    return s


def one_line(s, n=None):
    return clean(' '.join(str(s).split()), n)


def fmt_n(v):
    v = float(v)
    for unit, d in (('B', 1e9), ('M', 1e6), ('K', 1e3)):
        if v >= d:
            x = v / d
            return f'{x:.1f}{unit}' if x < 100 else f'{x:.0f}{unit}'
    return f'{int(v)}'


def fmt_bytes(v):
    for unit, d in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if v >= d:
            x = v / d
            return f'{x:.1f}{unit}' if x < 100 else f'{x:.0f}{unit}'
    return f'{int(v)}B'


def dir_usage(path, stop=None):
    """폴더 전체 (바이트, 파일 수). 링크는 따라가지 않는다."""
    total = files = 0
    stack = [path]
    while stack:
        if stop and stop.is_set():
            break
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    pass
    return total, files


def open_folder(path, select=False):
    """탐색기로 연다. select면 그 파일을 고른 채로 폴더를 연다."""
    try:
        if select and os.path.isfile(path):
            import subprocess
            subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
        elif os.path.isdir(path):
            os.startfile(path)
    except OSError as ex:
        print('open error', ex, file=sys.stderr)


def read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a.strip())) == os.path.normcase(os.path.normpath(b.strip()))


def _mod_ver(path):
    v = str((read_json(os.path.join(path, '.claude-plugin', 'plugin.json')) or {}).get('version') or '0')
    return tuple(int(x) for x in re.findall(r'\d+', v)[:3]) or (0,)


def _claude_settings():
    """~/.claude/settings.json (없으면 {}, 읽을 수 없으면 None)."""
    if not os.path.exists(CLAUDE_SETTINGS):
        return {}
    try:
        with open(CLAUDE_SETTINGS, encoding='utf-8-sig') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def mod_state():
    """session-panel mod: 'ok' / 'missing'(없음) / 'unlinked'(있지만 Claude Code가 불러오지 않음) / 'old'(이 프로그램에 든 것이 더 새것)
    / 'nowatch'(데스크탑 앱 세션이 mod 변경을 바로 다시 불러오는 설정이 없음)."""
    if not os.path.isfile(os.path.join(MOD_DIR, 'hooks', 'register.tsx')):
        return 'missing'
    env = (_claude_settings() or {}).get('env') or {}
    dirs = str(env.get(MOD_ENV) or os.environ.get(MOD_ENV) or '')
    if not any(_same_path(x, MOD_DIR) for x in dirs.split(os.pathsep) if x.strip()):
        return 'unlinked'
    if os.path.isdir(MOD_SRC) and _mod_ver(MOD_SRC) > _mod_ver(MOD_DIR):
        return 'old'
    if str(env.get(MOD_WATCH) or '') != '1':
        return 'nowatch'
    return 'ok'


def install_mod():
    """mod를 ~/.claude/mods/session-panel 에 복사하고 ~/.claude/settings.json 의 env 에 불러오기 설정을 더한다.
    settings.json은 그 칸만 고치고(다른 mod 경로가 있으면 덧붙임), 고치기 전 사본을 settings.json.office-backup 으로 남긴다.
    읽을 수 없는 settings.json이면 아무것도 고치지 않고 ValueError."""
    cfg = _claude_settings()
    if cfg is None or not isinstance(cfg.get('env', {}), dict):
        raise ValueError(T('~/.claude/settings.json을 읽을 수 없어요'))
    shutil.copytree(MOD_SRC, MOD_DIR, dirs_exist_ok=True, ignore=shutil.ignore_patterns('*.test.tsx'))
    env = cfg.setdefault('env', {})
    cur = [x for x in str(env.get(MOD_ENV) or '').split(os.pathsep) if x.strip()]
    linked = any(_same_path(x, MOD_DIR) for x in cur)
    if not linked or str(env.get(MOD_WATCH) or '') != '1':
        if os.path.exists(CLAUDE_SETTINGS):
            shutil.copy2(CLAUDE_SETTINGS, CLAUDE_SETTINGS + '.office-backup')
        if not linked:
            env[MOD_ENV] = os.pathsep.join(cur + [MOD_DIR])
        env[MOD_WATCH] = '1'  # 데스크탑 앱(Code 탭) 세션도 mod를 고치면 다시 켜지 않고 바로 다시 불러온다
        tmp = CLAUDE_SETTINGS + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CLAUDE_SETTINGS)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ───────────────────────────── 번역 ─────────────────────────────
# 화면 문구는 한국어 원문을 열쇠로 i18n/<언어>.json 에서 찾는다. 설정 lang (없으면 윈도 표시 언어).
LANGS = (('ko', '한국어'), ('en', 'English'), ('ja', '日本語'), ('zh', '中文'))


def _sys_lang():
    try:
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3ff
    except Exception:
        return 'en'
    return {0x12: 'ko', 0x11: 'ja', 0x04: 'zh'}.get(lid, 'en')


LANG = (os.environ.get('CLAUDE_OFFICE_LANG') or (read_json(os.path.join(CACHE_DIR, 'settings.json')) or {}).get('lang')
        or _sys_lang())
if LANG not in dict(LANGS):
    LANG = 'en'
_TR = (read_json(os.path.join(APP_DIR, 'i18n', LANG + '.json')) or {}) if LANG != 'ko' else {}


def T(text):
    """화면 문구 번역 (원문 = 한국어). 번역이 없으면 원문 그대로."""
    return _TR.get(text) or text


def iso_epoch(ts):
    try:
        return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
    except Exception:
        return 0


_day_cache = {}


def local_day(hour_key):
    """b'2026-10-20T09' (UTC) -> '2026-10-20' (로컬 날짜)."""
    v = _day_cache.get(hour_key)
    if v is None:
        try:
            v = (datetime.datetime.strptime(hour_key.decode(), '%Y-%m-%dT%H')
                 .replace(tzinfo=datetime.timezone.utc).astimezone().strftime('%Y-%m-%d'))
        except Exception:
            v = '?'
        _day_cache[hour_key] = v
    return v


def today():
    return datetime.date.today().strftime('%Y-%m-%d')


# ───────────────────────────── 토큰 집계 ─────────────────────────────
USAGE_RE = re.compile(rb'"usage":\{"input_tokens":(\d+),"cache_creation_input_tokens":(\d+),'
                      rb'"cache_read_input_tokens":(\d+),"output_tokens":(\d+)')
MSGID_RE = re.compile(rb'"id":"(msg_[A-Za-z0-9_]+)"')
MODEL_RE = re.compile(rb'"model":"([^"]+)"')
TS_RE = re.compile(rb'"timestamp":"(\d{4}-\d\d-\d\dT\d\d)')


class Acc:
    """토큰 누계 [입력, 캐시생성, 캐시읽기, 출력] + 날짜별."""

    def __init__(self, d=None):
        d = d or {}
        self.tot = list(d.get('tot', [0, 0, 0, 0]))
        self.days = {k: list(v) for k, v in d.get('days', {}).items()}
        self.model = d.get('model', '')
        self.ctx = d.get('ctx', 0)

    def add(self, v, day):
        dd = self.days.setdefault(day, [0, 0, 0, 0])
        for i in range(4):
            self.tot[i] += v[i]
            dd[i] += v[i]

    def dump(self):
        return {'tot': self.tot, 'days': self.days, 'model': self.model, 'ctx': self.ctx}

    @staticmethod
    def merge(*accs):
        m = Acc()
        for a in accs:
            for i in range(4):
                m.tot[i] += a.tot[i]
            for k, v in a.days.items():
                dd = m.days.setdefault(k, [0, 0, 0, 0])
                for i in range(4):
                    dd[i] += v[i]
            if a.model:
                m.model = a.model
            if a.ctx:
                m.ctx = a.ctx
        return m


class Seen:
    """스트리밍으로 같은 메시지가 여러 줄에 나뉘어 기록되므로 최근 id만 기억해 중복 제거."""

    def __init__(self):
        self.d = collections.OrderedDict()

    def add(self, k):
        if k in self.d:
            return False
        self.d[k] = 1
        if len(self.d) > 256:
            self.d.popitem(last=False)
        return True


def scan_usage(buf, acc, seen):
    """buf(완전한 줄들) 안의 assistant usage를 acc에 더한다. json 파싱 없이 정규식만."""
    for m in USAGE_RE.finditer(buf):
        ls = buf.rfind(b'\n', 0, m.start()) + 1
        if not buf.startswith(b'{"parentUuid"', ls):
            continue
        p = buf.find(b'"message":{', ls, ls + 600)
        if p < 0 or buf.find(b'"role":"assistant"', p, p + 400) < 0:
            continue
        im = MSGID_RE.search(buf, p, p + 400)
        if im and not seen.add(im.group(1)):
            continue
        le = buf.find(b'\n', m.end())
        if le < 0:
            le = len(buf)
        tm = TS_RE.search(buf, m.end(), le)
        v = [int(m.group(i)) for i in (1, 2, 3, 4)]
        acc.add(v, local_day(tm.group(1)) if tm else '?')
        mm = MODEL_RE.search(buf, p, p + 200)
        if mm:
            acc.model = mm.group(1).decode('utf-8', 'replace')
        acc.ctx = v[0] + v[1] + v[2]


# ───────────────────────────── 대화 기록 → 로그 이벤트 ─────────────────────────────
BRIEF_KEYS = ('description', 'command', 'file_path', 'pattern', 'query', 'url', 'prompt',
              'skill', 'goal', 'title', 'path', 'message', 'text')


def brief_input(inp):
    if isinstance(inp, dict):
        for k in BRIEF_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return one_line(v, 90)
        if 'tasks' in inp:
            return T('단계 {0}개 보고').format(len(inp["tasks"]))
        for v in inp.values():
            if isinstance(v, str) and v.strip():
                return one_line(v, 90)
    return ''


def short_tool(name):
    if name.startswith('mcp__'):
        parts = name.split('__')
        return parts[-1] if len(parts) > 2 else name
    return name


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


_PASTE_RE = re.compile(r'<pasted_content id="([^"]*)">\n?(.*?)</pasted_content id="\1">', re.S)


def _fold_paste(m):
    body = m.group(2).strip()
    first = one_line(body.split('\n', 1)[0], 40)
    return T('〔붙여 넣은 글 {0}줄: {1}〕').format(body.count(chr(10)) + 1, first)


# 세션톡에서 보낸 명령은 진행 패널 mod가 이 머리글을 붙여 넣는다 (기록에는 플러그인이 보낸 프롬프트로 남는다)
_OFFICE_HEAD = re.compile(r'^(?:The session-panel plugin sent a message:\s*)?\[사용자 명령 · 세션 사무실 [^\]\n]*\]\s*\n')
# 플러그인이 넣은 프롬프트 끝에 Claude Code가 붙이는 안내 문단
_HARNESS_TAIL = re.compile(r'\n\s*This is how Claude Code surfaces a prompt a plugin submits[^\n]*\s*$')


def question_of(d):
    """사람이 직접 보낸 요청(세션톡 명령 포함)이면 그 글(대화 탭의 Q), 아니면 None."""
    o = d.get('origin')
    office = isinstance(o, dict) and o.get('kind') == 'plugin' and o.get('name') == 'session-panel'
    origin = d.get('turnOrigin')
    if not office and origin is not None and origin != 'human':
        return None  # 다른 세션 메시지, 작업 알림 등
    if not office and (d.get('promptSource') == 'system' or d.get('isCompactSummary')):
        return None
    content = (d.get('message') or {}).get('content')
    if isinstance(content, list):
        parts = [c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text']
        if not parts:
            return None  # 도구 결과
        content = '\n'.join(parts)
    if not isinstance(content, str):
        return None
    s = content.strip()
    m = _OFFICE_HEAD.match(s)
    if m:
        s = _HARNESS_TAIL.sub('', s[m.end():]).strip()
    elif office:
        return None  # 협업 메시지 등 mod가 넣은 다른 글
    m = re.match(r'<command-name>(.*?)</command-name>', s, re.S)
    if m:
        a = re.search(r'<command-args>(.*?)</command-args>', s, re.S)
        s = m.group(1).strip() + (' ' + a.group(1).strip() if a and a.group(1).strip() else '')
    elif not s or s.startswith(('<', '[Request interrupted')):
        return None
    return clean(_PASTE_RE.sub(_fold_paste, s), QA_MAX)


def parse_line(d):
    """한 줄(json) → [(epoch, kind, text)] , kind: user/say/tool/err/title, q/a(대화 탭)."""
    t = d.get('type')
    ts = iso_epoch(d.get('timestamp', '')) if d.get('timestamp') else 0
    out = []
    if d.get('isSidechain'):
        return out
    if t == 'assistant':
        msg = d.get('message') or {}
        usage = msg.get('usage') if isinstance(msg.get('usage'), dict) else None
        if usage and msg.get('id'):  # 세션톡의 '작업 중 (… · ↓ 토큰)' 표시용
            out.append((ts, 'tok', (msg['id'], int(usage.get('output_tokens') or 0))))
        for c in (d.get('message') or {}).get('content') or []:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'text' and c.get('text', '').strip():
                out.append((ts, 'say', one_line(c['text'], 400)))
                out.append((ts, 'a', clean(c['text'].strip(), QA_MAX)))
            elif c.get('type') == 'tool_use':
                name = short_tool(c.get('name', '?'))
                out.append((ts, 'tool', f'{name}  {brief_input(c.get("input"))}'.rstrip()))
    elif t == 'user' and not d.get('isMeta'):
        q = question_of(d)
        if q:
            out.append((ts, 'q', q))
        content = (d.get('message') or {}).get('content')
        if isinstance(content, str):
            s = content.strip()
            if s and not s.startswith('<'):
                out.append((ts, 'user', one_line(s, 500)))
            elif s.startswith('<command-name>'):
                m = re.search(r'<command-name>(.*?)</command-name>', s)
                if m:
                    out.append((ts, 'user', m.group(1)))
        elif isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get('type') == 'text':
                    s = c.get('text', '').strip()
                    if s and not s.startswith('<'):
                        out.append((ts, 'user', one_line(s, 500)))
                elif c.get('type') == 'tool_result' and c.get('is_error'):
                    out.append((ts, 'err', one_line(text_of(c.get('content')) or T('오류'), 200)))
    elif t in ('custom-title', 'ai-title'):
        title = d.get('customTitle') or d.get('aiTitle') or d.get('title')
        if title:
            out.append((ts, 'title' if t == 'ai-title' else 'ctitle', clean(title, 60)))
    return out


# ───────────────────────────── 대화 탭(Q&A) ─────────────────────────────
def _lines_back(f, end, start=0, chunk=4 * 1024 * 1024):
    """[start, end) 구간을 뒤에서부터 (줄 시작 오프셋, 완전한 줄들 bytes) 덩어리로."""
    pos, carry = end, b''
    while pos > start:
        n = min(chunk, pos - start)
        pos -= n
        f.seek(pos)
        buf = f.read(n) + carry
        cut = buf.find(b'\n') + 1 if pos > start else 0
        carry, body = buf[:cut], buf[cut:]
        yield pos + cut, body
        time.sleep(0.002)  # 화면 스레드에 GIL 양보
    if carry:
        yield start, carry


def _json_lines(body, needle, also=None):
    """body 안에서 needle(과 also)이 든 줄을 뒤에서부터 (줄 시작, dict)로."""
    i = len(body)
    while True:
        i = body.rfind(needle, 0, i)
        if i < 0:
            return
        ls = body.rfind(b'\n', 0, i) + 1
        le = body.find(b'\n', i)
        line = body[ls:le if le >= 0 else len(body)]
        i = ls
        if also and also not in line:
            continue
        try:
            yield ls, json.loads(line)
        except Exception:
            pass


def load_qa(path, end, keep=QA_KEEP, limit=QA_SCAN):
    """대화 기록을 뒤에서부터 훑어 사람 요청 keep개와 각 요청의 마지막 답변을 찾는다."""
    prompts = []
    with open(path, 'rb') as f:
        for base, body in _lines_back(f, end, max(0, end - limit)):
            found = []
            for needle in (b'"turnOrigin":"human"', b'"name":"session-panel"'):  # 직접 친 요청, 세션톡 명령
                for ls, d in _json_lines(body, needle):
                    q = question_of(d) if d.get('type') == 'user' and not d.get('isSidechain') else None
                    if q:
                        found.append((base + ls, iso_epoch(d.get('timestamp', '')) if d.get('timestamp') else 0, q))
            prompts += sorted(set(found), reverse=True)
            if len(prompts) >= keep:
                break
        prompts = prompts[:keep][::-1]
        out = []
        for i, (off, qts, q) in enumerate(prompts):
            stop = prompts[i + 1][0] if i + 1 < len(prompts) else end
            ats, a = 0, ''
            for base, body in _lines_back(f, stop, max(off, stop - QA_ANSWER_SCAN)):
                for ls, d in _json_lines(body, b'"type":"assistant"', b'"type":"text"'):
                    if d.get('isSidechain'):
                        continue
                    texts = [c.get('text', '') for c in (d.get('message') or {}).get('content') or []
                             if isinstance(c, dict) and c.get('type') == 'text' and c.get('text', '').strip()]
                    if texts:
                        ats, a = iso_epoch(d.get('timestamp', '')) if d.get('timestamp') else 0, clean(texts[-1].strip(), QA_MAX)
                        break
                if a:
                    break
            out.append([qts, q, ats, a])
    return out


# ───────────────────────────── 꺼진 세션 찾기 ─────────────────────────────
def _read_part(path, start, n):
    with open(path, 'rb') as f:
        f.seek(max(0, start))
        return f.read(n)


def past_session(path):
    """대화 기록 하나에서 다시 켤 때 필요한 것: 작업 폴더, 제목, 첫·마지막 요청."""
    size = os.path.getsize(path)
    head = _read_part(path, 0, 256 * 1024)
    tail = _read_part(path, size - 768 * 1024, 768 * 1024) if size > 256 * 1024 else head
    m = re.search(rb'"cwd":"((?:[^"\\]|\\.)*)"', head) or re.search(rb'"cwd":"((?:[^"\\]|\\.)*)"', tail)
    if not m:
        return None
    try:
        cwd = json.loads(b'"' + m.group(1) + b'"')
    except Exception:
        return None
    if not os.path.isdir(cwd):
        return None  # 작업 폴더가 지워진 세션은 다시 켤 수 없다
    out = {'cwd': cwd, 'title': '', 'first': '', 'last': ''}
    for buf, key in ((head, 'first'), (tail, 'last')):
        lines = buf.split(b'\n')
        for line in (lines if key == 'first' else reversed(lines)):
            if b'"turnOrigin":"human"' not in line or b'"type":"user"' not in line:
                continue
            try:
                q = question_of(json.loads(line))
            except Exception:
                continue
            if q:
                out[key] = one_line(q, 160)
                break
    for line in reversed(tail.split(b'\n')):
        if b'title"' in line and (b'"custom-title"' in line or b'"ai-title"' in line):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get('customTitle') or d.get('aiTitle') or d.get('title')
            if t:
                out['title'] = clean(t, 60)
                if d.get('type') == 'custom-title':
                    break
    return out


def clean_empty_sessions(live, min_age=600, peer_age=3600):
    """쓸모없는 세션 흔적을 지운다. 지운 (대화 기록 수, 상태 파일 수).
    - 대화 기록(jsonl) 가운데 사용자·Claude의 말이 한 줄도 없는 것 (예: 새로 켜기만 한 터미널에 mod 다시 불러오기
      알림만 적혀 '/resume' 목록에 'session-panel: reloaded'로 보이던 것). 켜져 있는 세션, min_age초 안에 바뀐 것은 둔다.
    - 진행 패널 mod의 peer-<세션>.json 가운데 꺼진 세션이고 peer_age초 넘게 갱신되지 않은 것 (세션 목록에 'bin'으로 쌓이던 것).
      주고받은 메시지(out-*.json)와 받은 메시지 기록(seen-*.json)은 대화 내용이라 건드리지 않는다."""
    now = time.time()
    n_tr = n_peer = 0
    for p in glob.glob(os.path.join(PROJ, '*', '*.jsonl')):
        sid = os.path.basename(p)[:-6]
        try:
            st = os.stat(p)
        except OSError:
            continue
        if sid in live or len(sid) != 36 or st.st_size > 64_000 or now - st.st_mtime < min_age:
            continue
        talk = False
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        kind = json.loads(line).get('type')
                    except ValueError:
                        talk = True  # 읽을 수 없는 줄이 있으면 손대지 않는다
                        break
                    if kind in ('user', 'assistant', 'summary', 'custom-title', 'ai-title'):
                        talk = True
                        break
        except OSError:
            continue
        if talk or os.path.isdir(os.path.join(os.path.dirname(p), sid)):  # 하위 폴더(도구 결과 등)가 있으면 둔다
            continue
        try:
            os.remove(p)
            n_tr += 1
        except OSError:
            pass
    for p in glob.glob(os.path.join(COLLAB, 'peer-*.json')):
        sid = os.path.basename(p)[5:-5]
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if sid in live or age < peer_age:
            continue
        try:
            os.remove(p)
            n_peer += 1
        except OSError:
            pass
    return n_tr, n_peer


TALK_IMG_DIR = os.path.join(COLLAB, 'talk')     # 세션톡에 보여 줄 사진을 세션이 저장하는 곳
IMG_WANT = re.compile(r'캡처|캡쳐|스크린\s*샷|사진|이미지|화면을?\s*(보여|보내|찍)|screen\s*shot|capture|image|picture|photo|'
                      r'スクショ|キャプチャ|画像|写真|截图|截屏|图片|照片', re.I)
CAPTURE_WANT = re.compile(r'캡처|캡쳐|스크린\s*샷|screen\s*shot|capture|スクショ|キャプチャ|截图|截屏', re.I)  # 사진을 붙여 보낼 때는 이것만 캡처 요청으로
IMG_HINT = T('[세션톡 사진 안내] 사진을 보여 줄 때는 PNG로 {0} 폴더에 저장하고, 답에 ![설명](파일 전체 경로) 한 줄로 적으세요. '
             '화면 캡처: powershell -ExecutionPolicy Bypass -File "{1}" -Out "{0}\\이름.png" -Title "창 제목 일부" (-Title을 빼면 전체 화면).')


def talk_text(sid, text):
    """세션톡에서 보내는 명령: 사진·캡처를 원하면 저장 위치와 보여 주는 법을 덧붙인다 (그럴 때만 — 토큰 절약)."""
    if not IMG_WANT.search(text):
        return text
    folder = os.path.join(TALK_IMG_DIR, sid[:8])
    return text + '\n\n' + IMG_HINT.format(folder, CAPTURE_PS1)


UPLOAD_NOTE = T('[세션톡 첨부 사진] 사용자가 휴대폰에서 사진 {0}장을 함께 보냈습니다. 위에 적힌 경로의 사진을 Read 도구로 열어 보고 요청에 답하세요.')
UPLOAD_MAX = 4                  # 한 번에 올리는 사진 수
UPLOAD_BYTES = 6 * 1024 * 1024  # 사진 한 장 크기 한도 (휴대폰이 줄여서 보내므로 보통 1MB 안쪽)


def strip_hint(text):
    """대화에 보일 때는 사진 안내(세션에만 가는 덧붙임)를 뺀다."""
    cut = [i for i in (text.find(IMG_HINT.split('{0}')[0][:16]), text.find(UPLOAD_NOTE.split('{0}')[0][:12])) if i > 0]
    return text[:min(cut)].rstrip() if cut else text


SID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def save_upload(sid, images):
    """휴대폰에서 올린 사진들을 세션톡 사진 폴더에 저장한다. images: [{'data': JPEG·PNG base64, 'preview': PNG base64(선택)}].
    PC 말풍선은 PNG만 그릴 수 있어 JPEG 옆에 작은 PNG(-p.png)를 둔다. 저장한 원본 경로 목록."""
    if not re.fullmatch(r'[0-9a-f-]{8,40}', sid) or not isinstance(images, list) or len(images) > UPLOAD_MAX:
        raise ValueError('images')
    sigs = {'.jpg': b'\xff\xd8\xff', '.png': b'\x89PNG\r\n\x1a\n'}

    def dec(s):
        try:
            raw = base64.b64decode(str(s).split(',', 1)[-1], validate=True)
        except (ValueError, TypeError):
            raise ValueError('image')
        if len(raw) > UPLOAD_BYTES:
            raise ValueError('image too big')
        ext = next((e for e, sig in sigs.items() if raw.startswith(sig)), None)
        if not ext:
            raise ValueError('image type')
        return raw, ext

    files = []
    for im in images:
        if not isinstance(im, dict):
            raise ValueError('image')
        raw, ext = dec(im.get('data'))
        pv = dec(im['preview']) if im.get('preview') else None
        if pv and pv[1] != '.png':
            raise ValueError('preview')
        files.append((raw, ext, pv and pv[0]))
    folder = os.path.join(TALK_IMG_DIR, sid[:8])
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = []
    for i, (raw, ext, pv) in enumerate(files, 1):
        base = os.path.join(folder, f'up-{stamp}-{os.urandom(2).hex()}-{i}')
        with open(base + ext, 'wb') as f:
            f.write(raw)
        if pv and ext != '.png':
            with open(base + '-p.png', 'wb') as f:
                f.write(pv)
        out.append(base + ext)
    return out


def upload_text(text, paths):
    """(말풍선 글, 세션에만 가는 덧붙임): 말풍선엔 사진을 ![사진 n](경로)로 보이고, 세션엔 사진을 열어 보라고 덧붙인다."""
    lines = '\n'.join(f'![{T("사진")} {i}]({p})' for i, p in enumerate(paths, 1))
    return (text + '\n\n' + lines).strip(), '\n\n' + UPLOAD_NOTE.format(len(paths))


def clean_talk_images(limit=TALK_IMG_MAX):
    """세션톡 사진 폴더가 limit을 넘으면 오래된 사진부터 지운다 (넘지 않으면 그대로 둔다). 지운 수."""
    files = []
    for root_, _d, fs in os.walk(TALK_IMG_DIR):
        for f in fs:
            p = os.path.join(root_, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    total = sum(x[1] for x in files)
    n = 0
    for _m, size, p in sorted(files):
        if total <= limit * 0.7 or total <= limit and n == 0:
            break
        try:
            os.remove(p)
            total -= size
            n += 1
        except OSError:
            pass
    return n


def read_ask(sid):
    """세션이 지금 고르라고 묻는 중이면 {id, at, questions:[{question, header, multiSelect, options:[{label, description}]}]}.
    진행 패널 mod가 협업 폴더 ask-<세션>.json 에 적고, 답하면 done 으로 바꾼다."""
    d = read_json(os.path.join(COLLAB, f'ask-{sid}.json'))
    if not isinstance(d, dict) or d.get('done') or not isinstance(d.get('questions'), list) or not d.get('id'):
        return None
    qs = []
    for q in d['questions'][:4]:
        if not isinstance(q, dict) or not q.get('question'):
            continue
        opts = [{'label': one_line(str(o.get('label') or ''), 60), 'description': one_line(str(o.get('description') or ''), 160)}
                for o in (q.get('options') if isinstance(q.get('options'), list) else [])[:6] if isinstance(o, dict) and o.get('label')]
        qs.append({'question': clean(str(q['question']), 600), 'header': one_line(str(q.get('header') or ''), 20),
                   'multiSelect': bool(q.get('multiSelect')), 'options': opts})
    return {'id': str(d['id']), 'at': d.get('at') or 0, 'questions': qs} if qs else None


def write_answer(sid, ask_id, answers):
    """세션톡에서 고른 답을 answer-<세션>.json 에 적는다. mod가 읽어 Claude에게 그 답으로 이어 가게 한다."""
    os.makedirs(COLLAB, exist_ok=True)
    path = os.path.join(COLLAB, f'answer-{sid}.json')
    with open(path + '.tmp', 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'id': ask_id, 'answers': answers, 'at': int(time.time() * 1000)}, f, ensure_ascii=False)
    os.replace(path + '.tmp', path)


def turn_state(s):
    """세션이 지금 답을 만드는 중이면 {start, tok, tool, mode} (CLI 아래의 '✻ … (6m 2s · ↓ 26.8k tokens)'), 아니면 None."""
    mode = s.mode()
    if mode not in ('busy', 'wait') or not s.qa:
        return None
    start = s.qa[-1][0]
    tok = sum(n for ts, n in s.toks.values() if ts >= start - 1)
    return {'start': start, 'tok': tok, 'tool': one_line(s.last_tool, 80) if mode == 'busy' else '', 'mode': mode}


def fmt_elapsed(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f'{sec}s'
    if sec < 3600:
        return f'{sec // 60}m {sec % 60}s'
    return f'{sec // 3600}h {sec % 3600 // 60}m'


def fmt_tok(n):
    return f'{n / 1000:.1f}k' if n >= 1000 else str(n)


SPIN = '·✢✳✶✻✽✻✶✳✢'


def turn_line(st, spin=0):
    """'✻ 작업 중… (6m 2s · ↓ 26.8k tokens)' 한 줄."""
    head = T('확인을 기다리는 중') if st['mode'] == 'wait' else T('작업 중')
    return f'{SPIN[spin % len(SPIN)]} {head}… ({fmt_elapsed(time.time() - st["start"])} · ↓ {fmt_tok(st["tok"])} tokens)'


def scan_past(skip, days=30, limit=80):
    """최근 days일 안에 쓰인 대화 기록 중 지금 켜져 있지 않은 세션 (새것부터)."""
    cut = time.time() - days * 86400
    files = []
    for p in glob.glob(os.path.join(PROJ, '*', '*.jsonl')):
        sid = os.path.basename(p)[:-6]
        if sid in skip or len(sid) != 36:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime >= cut and st.st_size > 2000:
            files.append((st.st_mtime, st.st_size, sid, p))
    files.sort(reverse=True)
    out = []
    for mtime, size, sid, p in files[:limit]:
        try:
            info = past_session(p)
        except OSError:
            continue
        if info:
            info.update(sid=sid, path=p, mtime=mtime, size=size)
            out.append(info)
    return out


# ───────────────────────────── 파일 추적 ─────────────────────────────
class FileTrack:
    """대화 기록 하나: 캐시된 지점부터 끝까지 토큰을 메우고(backfill), 이후 새 바이트만 읽는다."""

    def __init__(self, path, cached, events):
        self.path = path
        self.events = events
        size = os.path.getsize(path)
        self.boundary = self._line_boundary(size)
        if cached and cached.get('offset', 0) <= self.boundary:
            self.base = Acc(cached)
            self.back_from = cached['offset']
        else:
            self.base = Acc()
            self.back_from = 0
        self.bf = Acc()
        self.live = Acc()
        self.live_off = self.boundary
        self.back_done = self.back_from >= self.boundary
        self.seen = Seen()

    def _line_boundary(self, size):
        if size == 0:
            return 0
        with open(self.path, 'rb') as f:
            start = max(0, size - 65536)
            f.seek(start)
            data = f.read(size - start)
        i = data.rfind(b'\n')
        return start + i + 1 if i >= 0 else start

    def tail_lines(self):
        """시작 시 로그를 채울 끝부분 줄들."""
        start = max(0, self.boundary - TAIL_BYTES)
        with open(self.path, 'rb') as f:
            f.seek(start)
            data = f.read(self.boundary - start)
        if start > 0:
            data = data[data.find(b'\n') + 1:]
        return data

    def backfill(self, stop):
        """[back_from, boundary) 구간을 훑어 토큰 합계. 백그라운드 스레드에서 천천히."""
        seen = Seen()
        with open(self.path, 'rb') as f:
            f.seek(self.back_from)
            pos, carry = self.back_from, b''
            while pos < self.boundary and not stop.is_set():
                chunk = f.read(min(2 * 1024 * 1024, self.boundary - pos))
                if not chunk:
                    break
                pos += len(chunk)
                buf = carry + chunk
                cut = buf.rfind(b'\n') + 1
                scan_usage(buf[:cut], self.bf, seen)
                carry = buf[cut:]
                time.sleep(0.01)  # 화면 스레드에 GIL 양보
        self.back_done = not stop.is_set()

    def poll(self):
        """새로 붙은 완전한 줄들(bytes) 반환, 토큰은 즉시 반영."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return b''
        if size < self.live_off:  # 파일이 줄었다(거의 없음) → 처음부터
            self.__init__(self.path, None, self.events)
            return b''
        if size == self.live_off:
            return b''
        with open(self.path, 'rb') as f:
            f.seek(self.live_off)
            data = f.read(min(size - self.live_off, LIVE_CHUNK))
        cut = data.rfind(b'\n') + 1
        if cut == 0:
            return b''
        data = data[:cut]
        self.live_off += cut
        scan_usage(data, self.live, self.seen)
        return data

    def total(self):
        return Acc.merge(self.base, self.bf, self.live)

    def cache_entry(self):
        if not self.back_done:
            return None
        e = self.total().dump()
        e['offset'] = self.live_off
        return e


# ───────────────────────────── 세션 모델 ─────────────────────────────
class Session:
    def __init__(self, sid):
        self.sid = sid
        self.pid = 0
        self.cwd = ''
        self.kind = ''
        self.status = 'idle'
        self.waiting = ''
        self.started = 0
        self.sess_name = ''
        self.name_source = ''
        self.title = ''
        self.ctitle = ''
        self.peer = {}
        self.events = collections.deque(maxlen=500)
        self.qa = collections.deque(maxlen=QA_KEEP)  # [요청 시각, 요청, 답변 시각, 마지막 답변]
        self.qa_ver = 0
        self.qa_loading = False
        self.qa_late = None
        self.seq = 0
        self.last_act = 0
        self.last_tool = ''
        self.last_kind = ''
        self.toks = collections.OrderedDict()  # 메시지 id → (시각, 출력 토큰)
        self.tracks = {}
        self.sub_scan = 0
        self.acc = Acc()
        self.cpu = 0.0
        self.mem = 0
        self.cpu_prev = None
        self.ver = 0

    def add_event(self, ts, kind, text):
        if kind == 'tok':
            mid, n = text
            old = self.toks.get(mid)
            self.toks[mid] = (old[0] if old else ts or time.time(), max(n, old[1] if old else 0))
            while len(self.toks) > 3000:
                self.toks.popitem(last=False)
            return
        if kind == 'title':
            self.title = text
            return
        if kind == 'ctitle':
            self.ctitle = text
            return
        if kind == 'q':
            self.qa.append([ts or time.time(), text, 0, ''])
            self.qa_ver += 1
            return
        if kind == 'a':
            if self.qa:  # 같은 요청 안에서는 마지막 답변이 남는다
                self.qa[-1][2:] = [ts or time.time(), text]
                self.qa_ver += 1
            elif self.qa_loading:  # 지난 요청을 읽는 중 → 끝나면 마지막 요청에 붙인다
                self.qa_late = [ts or time.time(), text]
            return
        self.seq += 1
        self.events.append((self.seq, ts or time.time(), kind, text))
        if ts and ts > self.last_act:
            self.last_act = ts
        self.last_kind = kind
        if kind == 'tool':
            self.last_tool = text

    def overall(self):
        tasks = self.peer.get('tasks') or []
        if not tasks:
            return None
        s = 0
        for t in tasks:
            st = t.get('status')
            s += 100 if st == 'completed' else (t.get('percent') or 30) if st == 'in_progress' else 0
        return s / len(tasks)

    def mode(self):
        if self.status == 'waiting':
            return 'wait'
        if self.status == 'busy':
            return 'busy'
        if self.last_act and time.time() - self.last_act > SLEEP_AFTER:
            return 'sleep'
        return 'idle'


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}
        self.names = {}
        self.version = 0
        self.flights = []          # (from_sid, to_sid|None)
        self.says = []             # (말한 세션, 말풍선 글) — 메시지를 보낸 책상 위에 잠깐 띄운다
        self.chat = {}             # 메시지 id → 세션톡용 메시지 (최근 7일)
        self.chat_ver = 0
        self.sec = {}              # 비서 세션이 쓰는 secretary.json
        self.msg_seen = set()
        self.msg_first = True
        self._mtimes = {}
        self.cache = read_json(CACHE_FILE) or {}
        self.cache_dirty = False
        self.cache_saved = time.time()
        self.stop = threading.Event()
        self.self_mem = 0
        self.self_cpu = 0.0
        self._self_prev = None
        self._ncpu = os.cpu_count() or 1
        self._last_poll = time.time()

    # 파일이 바뀌었을 때만 다시 읽기
    def _changed(self, path):
        try:
            m = os.path.getmtime(path)
        except OSError:
            return False
        if self._mtimes.get(path) == m:
            return False
        self._mtimes[path] = m
        return True

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._backfill_loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception as ex:  # 모니터는 죽지 않는다
                print('poll error', ex, file=sys.stderr)
            self.stop.wait(POLL_SEC)
        self.save_cache(force=True)

    def _backfill_loop(self):
        while not self.stop.is_set():
            todo = []
            with self.lock:
                for s in self.sessions.values():
                    for tr in s.tracks.values():
                        if not tr.back_done:
                            todo.append(tr)
            if not todo:
                self.stop.wait(1.5)
                continue
            todo.sort(key=lambda t: t.boundary - t.back_from)
            tr = todo[0]
            try:
                tr.backfill(self.stop)
            except Exception as ex:
                print('backfill error', ex, file=sys.stderr)
                tr.back_done = True
            self.cache_dirty = True
            with self.lock:
                self.version += 1

    def poll(self):
        now = time.time()
        dt = max(0.5, now - self._last_poll)
        self._last_poll = now
        live = {}
        try:
            files = os.listdir(SESS_DIR)
        except OSError:
            files = []
        for fn in files:
            if not fn.endswith('.json'):
                continue
            d = read_json(os.path.join(SESS_DIR, fn))
            if not d or not d.get('sessionId') or not d.get('pid'):
                continue
            st = proc_stat(int(d['pid']))
            if not st:
                continue
            ps = d.get('procStart')
            if ps and str(ps).isdigit() and abs(int(ps) - st[0]) > 50_000_000:
                continue  # pid 재사용
            live[d['sessionId']] = (d, st)
        self._desktop_sessions(live, now)

        if self._changed(os.path.join(COLLAB, 'names.json')):
            self.names = read_json(os.path.join(COLLAB, 'names.json')) or {}
        if self._changed(SEC_FILE) or (self.sec and not os.path.exists(SEC_FILE)):
            sec = read_json(SEC_FILE)
            with self.lock:
                self.sec = sec if isinstance(sec, dict) else {}
                self.version += 1

        changed = False
        with self.lock:
            for sid in list(self.sessions):
                if sid not in live:
                    s = self.sessions.pop(sid)
                    for p, tr in s.tracks.items():
                        e = tr.cache_entry()
                        if e:
                            self.cache[p] = e
                    changed = True
            for sid, (d, st) in live.items():
                s = self.sessions.get(sid)
                if s is None:
                    s = self.sessions[sid] = Session(sid)
                    changed = True
                before = (s.status, s.waiting, s.sess_name)
                s.pid = int(d.get('pid') or 0)
                s.cwd = d.get('cwd', '')
                s.kind = d.get('kind', '')
                s.status = d.get('status', 'idle')
                s.waiting = d.get('waitingFor', '') if s.status == 'waiting' else ''
                s.started = (d.get('startedAt') or 0) / 1000
                s.sess_name = d.get('name', '')
                s.name_source = d.get('nameSource', '')
                if before != (s.status, s.waiting, s.sess_name):
                    s.ver += 1
                    changed = True
                if not st:  # 데스크탑 앱 세션: 프로세스를 모른다
                    continue
                cpu = st[1]
                if s.cpu_prev is not None:
                    s.cpu = max(0.0, (cpu - s.cpu_prev) / (dt * 1e7 * self._ncpu) * 100)
                s.cpu_prev = cpu
                s.mem = st[2]

        # 진행 보고 (session-panel mod)
        for sid, s in list(self.sessions.items()):
            p = os.path.join(COLLAB, f'peer-{sid}.json')
            if self._changed(p):
                d = read_json(p)
                if d is not None:
                    with self.lock:
                        s.peer = d
                        s.ver += 1
                    changed = True

        # 대화 기록
        for sid, s in list(self.sessions.items()):
            if self._poll_transcripts(s, now):
                changed = True

        self._poll_messages()

        me = proc_stat(os.getpid())
        if me:
            if self._self_prev is not None:
                self.self_cpu = max(0.0, (me[1] - self._self_prev) / (dt * 1e7 * self._ncpu) * 100)
            self._self_prev = me[1]
            self.self_mem = me[2]

        if changed:
            with self.lock:
                self.version += 1
        self.save_cache()

    def _desktop_sessions(self, live, now):
        """Claude 데스크탑 앱(Code 탭)에서 켠 세션: 세션 목록 파일(~/.claude/sessions)에 없을 수 있어서
        session-panel mod가 4초마다 적는 상태 파일(peer-<세션>.json, surface가 terminal이 아닌 것)로 찾는다."""
        try:
            names = os.listdir(COLLAB)
        except OSError:
            return
        for fn in names:
            if not (fn.startswith('peer-') and fn.endswith('.json')):
                continue
            sid = fn[5:-5]
            if sid in live:
                continue
            p = os.path.join(COLLAB, fn)
            try:
                if now - os.path.getmtime(p) > PEER_LIVE:
                    continue
            except OSError:
                continue
            d = read_json(p)
            if not isinstance(d, dict) or d.get('surface') in (None, '', 'terminal'):
                continue
            busy = d.get('phase') == 'working'
            live[sid] = ({'sessionId': sid, 'pid': 0, 'cwd': d.get('root') or '', 'kind': 'desktop',
                          'status': 'busy' if busy else 'idle'}, None)

    def _transcript_path(self, s):
        p = os.path.join(PROJ, re.sub(r'[^A-Za-z0-9]', '-', s.cwd), s.sid + '.jsonl')
        if os.path.exists(p):
            return p
        hits = glob.glob(os.path.join(PROJ, '*', s.sid + '.jsonl'))
        return hits[0] if hits else None

    def _poll_transcripts(self, s, now):
        changed = False
        if not s.tracks:
            p = self._transcript_path(s)
            if not p:
                return False
            tr = FileTrack(p, self.cache.get(p), True)
            tail = tr.tail_lines()
            evs = []
            for line in tail.split(b'\n'):
                evs.extend(e for e in self._line_events(line) if e[1] not in ('q', 'a'))
            with self.lock:
                s.tracks[p] = tr
                for e in evs:
                    s.add_event(*e)
                s.ver += 1
                s.qa_loading = True
            # 대화 탭은 기록을 거슬러 올라가며 따로 채운다 (오래 걸릴 수 있어 스레드로)
            threading.Thread(target=self._load_qa, args=(s, p, tr.boundary), daemon=True).start()
            # 끝부분에서 모델/컨텍스트 크기 알아내기
            tmp = Acc()
            scan_usage(tail, tmp, Seen())
            tr.live.model = tmp.model
            tr.live.ctx = tmp.ctx
            changed = True
        # 서브에이전트 기록은 15초마다 찾아본다 (토큰만 집계)
        if now - s.sub_scan > 15:
            s.sub_scan = now
            main = next(iter(s.tracks))
            subdir = os.path.join(main[:-6], 'subagents')
            if os.path.isdir(subdir):
                for p in glob.glob(os.path.join(subdir, '*.jsonl')):
                    if p not in s.tracks:
                        try:
                            tr = FileTrack(p, self.cache.get(p), False)
                        except OSError:
                            continue
                        with self.lock:
                            s.tracks[p] = tr
        for p, tr in list(s.tracks.items()):
            data = tr.poll()
            if not data:
                continue
            changed = True
            if tr.events:
                evs = []
                for line in data.split(b'\n'):
                    evs.extend(self._line_events(line))
                with self.lock:
                    for e in evs:
                        s.add_event(*e)
            with self.lock:
                s.ver += 1
        main = next(iter(s.tracks))
        main_tr = s.tracks[main]
        acc = Acc.merge(*(t.total() for t in s.tracks.values()))
        acc.model = main_tr.live.model or main_tr.total().model
        acc.ctx = main_tr.live.ctx or main_tr.total().ctx
        with self.lock:
            s.acc = acc
            if s.last_act == 0:
                try:
                    s.last_act = os.path.getmtime(main)
                except OSError:
                    pass
        return changed

    def _load_qa(self, s, path, end):
        try:
            qa = load_qa(path, end)
        except Exception as ex:
            print('qa error', ex, file=sys.stderr)
            qa = []
        with self.lock:
            # 읽는 동안 새로 들어온 요청은 뒤에 이어 붙인다
            if s.qa_late and qa and not s.qa:
                qa[-1][2:] = s.qa_late
            s.qa_late = None
            s.qa = collections.deque(qa + list(s.qa), maxlen=QA_KEEP)
            s.qa_loading = False
            s.qa_ver += 1
            s.ver += 1
            self.version += 1

    @staticmethod
    def _line_events(line):
        if not line or line.startswith(b'{"type":"api-request') or len(line) > 4_000_000:
            return []
        if b'"type":"assistant"' not in line and b'"type":"user"' not in line and b'title"' not in line:
            return []
        try:
            return parse_line(json.loads(line))
        except Exception:
            return []

    def display_name(self, s):
        if s.sid in self.names:
            return clean(self.names[s.sid], 40)
        if s.ctitle:
            return s.ctitle
        if s.sess_name and s.name_source != 'derived':
            return clean(s.sess_name, 40)
        if s.title:
            return s.title
        label = s.peer.get('label')
        if label:
            return clean(label, 40)
        return (os.path.basename(s.cwd.rstrip('\\/')) or 'claude') + '-' + s.sid[:4]

    def set_name(self, sid, name):
        """세션 이름을 협업 폴더 names.json에 쓴다 (진행 패널 mod의 /collab name과 같은 곳). 빈 이름이면 지운다."""
        path = os.path.join(COLLAB, 'names.json')
        names = read_json(path) or {}
        if name:
            names[sid] = name
        else:
            names.pop(sid, None)
        os.makedirs(COLLAB, exist_ok=True)
        tmp = path + '.office.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:  # mod가 쓰는 LF 그대로
            json.dump(names, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, path)
        with self.lock:
            self.names = names
            s = self.sessions.get(sid)
            if s:
                s.ver += 1
            self.version += 1

    def _resolve(self, label):
        label = (label or '').strip()
        for s in self.sessions.values():
            if label in (self.display_name(s), s.peer.get('label'), self.names.get(s.sid), s.sid, s.sid[:8]):
                return s
        return None

    def _poll_messages(self):
        for p in glob.glob(os.path.join(COLLAB, 'out-*.json')):
            if not self._changed(p):
                continue
            msgs = read_json(p)
            if not isinstance(msgs, list):
                continue
            is_user = os.path.basename(p) == os.path.basename(USER_OUT)
            for m in msgs:
                mid = m.get('id') if isinstance(m, dict) else None
                if not mid or mid in self.msg_seen:
                    continue
                self.msg_seen.add(mid)
                ts = time.time()
                try:
                    ts = m['at'] / 1000 if isinstance(m.get('at'), (int, float)) else int(mid.split('-')[1], 36) / 1000
                except Exception:
                    pass
                with self.lock:
                    to = m.get('to') or '*'
                    dst = None if to in ('*', 'all', '전체') else self._resolve(to)
                    if time.time() - ts < 7 * 86400:
                        self.chat[mid] = {'id': mid, 'from': 'user' if is_user else m.get('from') or '',
                                          'fromLabel': T('나') if is_user else clean(m.get('fromLabel') or '?', 40),
                                          'to': dst.sid if dst else to, 'toLabel': clean(to, 40),
                                          'kind': m.get('kind') or 'info', 'text': clean(m.get('text', ''), 6000), 'ts': ts}
                        self.chat_ver += 1
                if self.msg_first and time.time() - ts > 6 * 3600:
                    continue
                with self.lock:
                    src = None if is_user else self.sessions.get(m.get('from'))
                    kind = T('명령') if is_user else KIND_TEXT.get(m.get('kind'), T('메시지'))
                    text = one_line(m.get('text', ''), 300)
                    if src:
                        src.add_event(ts, 'msg', f'→ {to} ({kind}) {text}')
                        src.ver += 1
                    targets = [dst] if dst else ([x for x in self.sessions.values() if x is not src] if to in ('*', 'all', '전체') else [])
                    for t in targets:
                        t.add_event(ts, 'msg', f'← {T("사용자") if is_user else m.get("fromLabel") or "?"} ({kind}) {text}')
                        t.ver += 1
                    self.version += 1
                    if not self.msg_first and src:
                        for t in targets or [None]:
                            self.flights.append((src.sid, t.sid if t else None))
                        who = self.display_name(dst) if dst else T('모두')
                        self.says.append((src.sid, T('{0}에게 {1}: {2}').format(who, KIND_TEXT.get(m.get("kind"), ""), text)))
                    elif not self.msg_first and is_user and dst:
                        self.says.append((dst.sid, T('사용자 명령 받음: {0}').format(text)))
        if len(self.chat) > 1500:
            for k in sorted(self.chat, key=lambda k: self.chat[k]['ts'])[:-1200]:
                del self.chat[k]
        self.msg_first = False

    def send_user(self, sid, text, extra=''):
        """대화창에서 사용자가 쓴 명령을 out-office-user.json에 적는다. 진행 패널 mod가 그 세션에 사용자 요청으로 넣는다.
        extra는 세션에만 가는 덧붙임(사진 안내 등) — 세션톡 말풍선에는 보이지 않는다."""
        text = text.strip()
        if not text:
            return
        now_ms = int(time.time() * 1000)
        base36 = ''
        n = now_ms
        while n:
            n, r = divmod(n, 36)
            base36 = '0123456789abcdefghijklmnopqrstuvwxyz'[r] + base36
        msg = {'id': f'office-{base36}-{os.urandom(2).hex()}', 'from': 'office-user', 'fromLabel': '사용자',
               'to': sid, 'kind': 'request', 'text': text + extra, 'at': now_ms}
        out = read_json(USER_OUT)
        out = (out if isinstance(out, list) else [])[-199:] + [msg]
        os.makedirs(COLLAB, exist_ok=True)
        tmp = USER_OUT + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, USER_OUT)
        # 대화창에는 바로 보이게 넣어 두고, 로그·말풍선은 모니터 스레드가 다음 폴링에 붙인다
        with self.lock:
            s = self.sessions.get(sid)
            self.chat[msg['id']] = {'id': msg['id'], 'from': 'user', 'fromLabel': T('나'), 'to': sid,
                                    'toLabel': self.display_name(s) if s else sid, 'kind': 'request',
                                    'text': clean(text, 6000), 'ts': now_ms / 1000}
            self.chat_ver += 1

    def save_cache(self, force=False):
        now = time.time()
        if not force and now - self.cache_saved < 60:
            return
        self.cache_saved = now
        with self.lock:
            for s in self.sessions.values():
                for p, tr in s.tracks.items():
                    e = tr.cache_entry()
                    if e:
                        self.cache[p] = e
            data = dict(self.cache)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = CACHE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, CACHE_FILE)
        except OSError:
            pass


# ───────────────────────────── 캐릭터 스프라이트 ─────────────────────────────
HAIR = ['#2b2118', '#5a3a22', '#c48a3a', '#e8d27a', '#8a4fb8', '#d0503c', '#3c6fd0', '#e7e7ee', '#1f6b52', '#ff7aa8']
SHIRT = ['#3f7fd8', '#d8573f', '#3fa85a', '#9b59b6', '#e0a030', '#2fa3a3', '#e05c8a', '#5c6bc0', '#7b8d42', '#555f6e']
SKIN = ['#f6d3b3', '#eec19c', '#d9a27c', '#b97a56', '#8d5a3b']
SW, SH = 24, 24
POSES = ('idle', 'breath', 'blink', 'type_a', 'type_b', 'raise', 'sleep')


def shade(hexc, f):
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return '#%02x%02x%02x' % tuple(max(0, min(255, int(c * f))) for c in (r, g, b))


def _lift(hexc, t):
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return '#%02x%02x%02x' % tuple(int(c + (255 - c) * t) for c in (r, g, b))


HAIR_STYLES = (T('짧은 머리'), T('긴 머리'), T('뻗친 머리'), T('모자'), T('단발'), T('묶은 머리'))
ACCS = (('', T('없음')), ('glasses', T('안경')), ('sunglasses', T('선글라스')), ('headset', T('헤드셋')), ('ribbon', T('리본')),
        ('tie', T('넥타이')), ('crown', T('왕관')))


def looks(sid, av=None):
    """(머리 모양, 색 표, 깜빡임 시드, 소품). av = 사용자가 꾸민 값 {style, hair, skin, shirt, acc}."""
    h = hashlib.md5(sid.encode()).digest()
    av = av or {}
    style = av.get('style')
    if not isinstance(style, int) or not 0 <= style < len(HAIR_STYLES):
        style = h[3] % 5
    shirt = av.get('shirt') or SHIRT[h[1] % len(SHIRT)]
    hair = av.get('hair') or HAIR[h[0] % len(HAIR)]
    skin = av.get('skin') or SKIN[h[2] % len(SKIN)]
    # 대문자 = 기본색, 소문자 = 그늘/밝은 결. O = 윤곽선
    cmap = {'H': hair, 'h': _lift(hair, 0.35), 'D': shade(hair, 0.68),
            'S': skin, 's': shade(skin, 0.86), 'E': '#2a2230', 'W': '#ffffff', 'w': '#e9edf2',
            'M': '#b5524a', 'm': '#7a2f2c', 'P': '#f29a9a',
            'B': shirt, 'b': shade(shirt, 0.78), 'v': _lift(shirt, 0.25), 'C': shade(shirt, 0.6), 'c': shade(shirt, 0.42),
            'G': '#23232b', 'L': '#2c3448', 'l': '#5d6f93', 'K': '#30343c', 'k': '#e05c5c',
            'R': '#ff5c8a', 'r': '#c8386a', 'T': '#b8323a', 't': '#8a1f2a', 'Y': '#f2c14e', 'y': '#c8962a',
            'O': shade(_mix_dark(shirt, hair), 1.0)}
    return style, cmap, h[4], av.get('acc') or ''


def _mix_dark(a, b):
    """윤곽선: 옷과 머리색을 섞어 아주 어둡게 (검정보다 부드럽게)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(int((x + y) / 2 * 0.22 + 18) for x, y in zip(ca, cb))


def sprite_grid(style, pose, acc=''):
    """24x24 캐릭터 한 장. 그린 뒤 바깥에 윤곽선(O)을 두른다."""
    g = [[None] * SW for _ in range(SH)]

    def P(x, y, c):
        if 0 <= x < SW and 0 <= y < SH:
            g[y][x] = c

    def R(x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(SH - 1, y1) + 1):
            for x in range(max(0, x0), min(SW - 1, x1) + 1):
                g[y][x] = c

    sleep = pose == 'sleep'
    d = 5 if sleep else 1 if pose == 'breath' else 0   # 머리가 내려가는 정도
    # ── 몸통 (옷깃, 그늘) ──
    top = 15
    R(5, top, 18, 23, 'B')
    P(5, top, None); P(18, top, None)
    R(16, top + 1, 18, 23, 'b'); R(5, 22, 18, 23, 'b')
    R(6, top + 1, 7, 20, 'v')                       # 왼쪽 밝은 결
    R(10, top, 13, top + 1, 'S'); P(11, top + 2, 'S'); P(12, top + 2, 's')   # 브이넥
    P(9, top, 'W'); P(9, top + 1, 'W'); P(10, top + 2, 'W'); P(14, top, 'W'); P(14, top + 1, 'W'); P(13, top + 2, 'W')
    P(11, top + 4, 'b'); P(11, top + 6, 'b')        # 단추
    # ── 팔 ──
    if pose in ('idle', 'breath', 'blink'):
        R(3, top + 1, 4, 21, 'B'); R(19, top + 1, 20, 21, 'b')
        R(3, 22, 4, 23, 'S'); R(19, 22, 20, 23, 's')
    elif pose in ('type_a', 'type_b', 'raise'):
        la = -1 if pose == 'type_a' else 0
        ra = -1 if pose == 'type_b' else 0
        R(3, top + 1, 4, 20, 'B'); R(4, 20, 7, 21, 'B'); R(7, 21 + la, 9, 22 + la, 'S'); P(9, 22 + la, 's')
        if pose == 'raise':
            R(19, 4, 20, top + 2, 'b'); R(19, 1, 20, 3, 'S'); P(21, 2, 'S'); P(18, 2, 'S')
        else:
            R(19, top + 1, 20, 20, 'b'); R(16, 20, 19, 21, 'b'); R(14, 21 + ra, 16, 22 + ra, 'S'); P(14, 22 + ra, 's')
    elif sleep:
        R(2, 18, 21, 21, 'B'); R(2, 20, 21, 21, 'b'); R(4, 17, 19, 17, 'v')
    # ── 목, 얼굴 ──
    if not sleep:
        R(10, 13 + d, 13, 14, 's')
    R(6, 3 + d, 17, 13 + d, 'S')
    for x, y in ((6, 3), (17, 3), (6, 13), (17, 13)):   # 둥근 모서리
        P(x, y + d, None)
    R(17, 4 + d, 17, 12 + d, 's'); R(7, 13 + d, 16, 13 + d, 's')   # 얼굴 그늘
    P(5, 8 + d, 'S'); P(5, 9 + d, 's'); P(18, 8 + d, 's'); P(18, 9 + d, 's')   # 귀
    # 눈, 눈썹, 볼, 입
    closed = pose in ('blink', 'sleep')
    for ex in (8, 14):
        if closed:
            R(ex, 9 + d, ex + 1, 9 + d, 'E')
        else:
            R(ex, 8 + d, ex + 1, 9 + d, 'E'); P(ex, 8 + d, 'W')
        R(ex, 6 + d, ex + 1, 6 + d, 'D')
    P(7, 11 + d, 'P'); P(16, 11 + d, 'P')
    if sleep:
        P(12, 12 + d, 'm')
    elif pose == 'raise':
        R(11, 11 + d, 12, 12 + d, 'm'); P(11, 11 + d, 'M')
    else:
        R(11, 12 + d, 12, 12 + d, 'M')
    # ── 머리카락 ──
    def cap(y0=1):
        R(7, y0 + d, 16, y0 + d, 'H'); R(6, y0 + 1 + d, 17, 4 + d, 'H'); R(5, 3 + d, 5, 7 + d, 'H'); R(18, 3 + d, 18, 7 + d, 'H')
        R(9, y0 + 1 + d, 11, y0 + 1 + d, 'h'); P(8, y0 + 2 + d, 'h')
        R(15, 3 + d, 17, 4 + d, 'D'); P(18, 6 + d, 'D'); P(18, 7 + d, 'D')

    if style == 0:    # 짧은 머리: 옆 가르마 앞머리
        cap()
        R(6, 5 + d, 11, 5 + d, 'H'); P(6, 6 + d, 'H'); P(7, 6 + d, 'D')
    elif style == 1:  # 긴 머리: 어깨까지
        cap()
        R(6, 5 + d, 9, 5 + d, 'H'); R(14, 5 + d, 17, 5 + d, 'H')
        R(4, 4 + d, 5, 18, 'H'); R(18, 4 + d, 19, 18, 'D'); R(5, 8 + d, 5, 16, 'h')
    elif style == 2:  # 뻗친 머리
        cap(2)
        for x in (6, 9, 12, 15):
            P(x, 1 + d, 'H'); P(x + 1, 0 + d, 'H'); P(x + 1, 1 + d, 'h')
        R(7, 5 + d, 9, 5 + d, 'H'); R(13, 5 + d, 15, 5 + d, 'H'); P(11, 5 + d, 'H')
    elif style == 3:  # 모자
        R(5, 4 + d, 5, 8 + d, 'H'); R(18, 4 + d, 18, 8 + d, 'D')
        R(7, 0 + d, 16, 0 + d, 'C'); R(6, 1 + d, 17, 4 + d, 'C'); R(8, 1 + d, 11, 1 + d, 'v')
        R(4, 5 + d, 19, 5 + d, 'c'); R(15, 1 + d, 17, 4 + d, 'c'); P(11, 2 + d, 'W'); P(12, 2 + d, 'W')
    elif style == 4:  # 단발: 일자 앞머리
        cap()
        R(6, 5 + d, 17, 5 + d, 'H'); R(4, 4 + d, 5, 13 + d, 'H'); R(18, 4 + d, 19, 13 + d, 'D')
        P(9, 5 + d, 'h'); P(13, 5 + d, 'h')
    else:             # 묶은 머리 (정수리 똥머리)
        cap(2)
        R(10, 0 + d, 13, 1 + d, 'H'); P(11, 0 + d, 'h'); R(13, 0 + d, 13, 1 + d, 'D')
        R(6, 5 + d, 8, 5 + d, 'H'); R(15, 5 + d, 17, 5 + d, 'H')
    # ── 소품 ──
    if acc == 'glasses':
        for ex in (7, 13):
            R(ex, 7 + d, ex + 3, 7 + d, 'G'); R(ex, 10 + d, ex + 3, 10 + d, 'G')
            R(ex, 7 + d, ex, 10 + d, 'G'); R(ex + 3, 7 + d, ex + 3, 10 + d, 'G')
        R(11, 8 + d, 12, 8 + d, 'G')
    elif acc == 'sunglasses':
        R(7, 8 + d, 10, 10 + d, 'L'); R(13, 8 + d, 16, 10 + d, 'L'); R(7, 7 + d, 16, 7 + d, 'G')
        P(8, 8 + d, 'l'); P(14, 8 + d, 'l')
    elif acc == 'headset':
        R(6, 0 + d, 17, 0 + d, 'K'); P(5, 1 + d, 'K'); P(18, 1 + d, 'K')
        R(3, 7 + d, 5, 11 + d, 'K'); R(18, 7 + d, 20, 11 + d, 'K'); P(4, 9 + d, 'k'); P(19, 9 + d, 'k')
        R(6, 12 + d, 8, 12 + d, 'K'); P(9, 12 + d, 'k')
    elif acc == 'ribbon':
        R(14, 0 + d, 16, 2 + d, 'R'); R(18, 0 + d, 20, 2 + d, 'R'); R(17, 1 + d, 17, 1 + d, 'r')
        P(16, 1 + d, 'r'); P(18, 1 + d, 'r'); P(14, 0 + d, 'r'); P(20, 2 + d, 'r')
    elif acc == 'tie' and not sleep:
        R(11, 17, 12, 17, 't'); R(11, 18, 12, 21, 'T'); P(12, 19, 't'); P(12, 21, 't'); R(11, 22, 12, 22, 't')
    elif acc == 'crown' and not sleep:
        R(7, 1, 16, 2, 'Y'); R(7, 2, 16, 2, 'y')
        for x in (7, 11, 12, 16):
            P(x, 0, 'Y')
        P(9, 1, 'R'); P(14, 1, 'l')
    # ── 윤곽선: 그림 바깥 4방향 이웃에 O ──
    out = [row[:] for row in g]
    for y in range(SH):
        for x in range(SW):
            if g[y][x] is None and any(0 <= x + dx < SW and 0 <= y + dy < SH and g[y + dy][x + dx] is not None
                                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                out[y][x] = 'O'
    return out


def make_frames(sid, sc, av=None):
    """sc = 옛 16칸 기준 배율 (소수 가능). 24칸 그림에 맞춰 크기가 비슷하게 정수 배율로 바꾼다."""
    sc = max(1, int(round(sc * 16 / SW)))
    style, cmap, seed, acc = looks(sid, av)
    frames = {}
    for pose in POSES:
        g = sprite_grid(style, pose, acc)
        img = tk.PhotoImage(width=SW * sc, height=SH * sc)
        for y, row in enumerate(g):
            x = 0
            while x < SW:
                c = row[x]
                if c is None:
                    x += 1
                    continue
                x2 = x
                while x2 + 1 < SW and row[x2 + 1] == c:
                    x2 += 1
                img.put(cmap[c], to=(x * sc, y * sc, (x2 + 1) * sc, (y + 1) * sc))
                x = x2 + 1
        frames[pose] = img
    return frames, cmap['B'], seed


# ───────────────────────────── GUI ─────────────────────────────
# 캔버스 좌표는 '단위'로 적고 줌 배율 Z를 곱해 픽셀로 바꾼다. 글꼴도 Z를 따른다.
CELL_W, CELL_H = 250, 214
WALL_H = 172
ZOOMS = (1.0, 1.25, 1.5, 1.75, 2.0)
SETTINGS_FILE = os.path.join(CACHE_DIR, 'settings.json')
# 테마: 밝은 인트라넷 풍(흰 카드·민트 초록) / 다크(짙은 남회색·밤 사무실). 설정 theme, 바꾸면 창을 다시 연다.
# 환경 변수 CLAUDE_OFFICE_THEME 로 덮어쓸 수 있다 (시험용).
THEME = os.environ.get('CLAUDE_OFFICE_THEME') or (read_json(SETTINGS_FILE) or {}).get('theme') or 'light'
DARK = THEME == 'dark'


def _t(light, dark):
    return dark if DARK else light


BG, CARD, CARD2 = _t('#f2f5f8', '#12161c'), _t('#ffffff', '#1b2129'), _t('#f3f6f9', '#242c36')
FG, DIM, ACCENT, TOPBG = _t('#1f2933', '#e6ebf0'), _t('#7c8896', '#8d99a8'), '#2fb985', _t('#ffffff', '#171c23')
EDGE, HOVER = _t('#e2e8ee', '#2b343f'), _t('#e8f6ef', '#1f3a31')   # 카드 테두리, 버튼 위에 올렸을 때
ACC_DARK, BLUE, RED = '#22a273', '#3b9cf0', '#f05a5a'               # ACC_DARK = 초록 버튼 위에 올렸을 때
ACC_TEXT = _t('#22a273', '#5fd6a8')                                  # 초록 글자
SURF = _t('#ffffff', '#1f2630')                                      # 캔버스 위 카드·말풍선·명패
TEXT2 = _t('#4a5562', '#b3bfcb')                                     # 보조 글자
MINT = _t('#e7f6ef', '#1c3a30')                       # 아바타 바탕, 고른 줄
HOVER_LINE = _t('#8fd6b8', '#3f9a77')                 # 책상 위에 올렸을 때 테두리
SIDE = _t('#3fbf8e', '#173a2f')                       # 왼쪽 아이콘 막대
SIDE_HOVER = _t('#5fd0a3', '#22503f')
GRAD = _t(('#45c98f', '#3aa3e6'), ('#1f7a5a', '#21588a'))   # 프로필 카드·게임 메뉴 그라데이션
FLOOR, FLOOR2, SEAM = _t('#e2c8a2', '#3b3029'), _t('#e8d1ae', '#41352d'), _t('#cdae86', '#2d241e')   # 바닥 마루
WALL, WALL2, WAINSCOT = _t('#f4f8f6', '#1b2925'), _t('#eaf3ee', '#1f2f2a'), _t('#d6ebe1', '#22352e')
ROOM = {  # 사무실 그림 색
    'rail': _t('#ffffff', '#2f4a40'), 'base': _t('#b9d3c7', '#121c19'), 'panel': _t('#c2ddd0', '#2c443b'),
    'frame': _t('#ffffff', '#2d3b37'), 'frame_edge': _t('#d5e3dc', '#3a4a45'),
    'curtain': _t('#9fdcc2', '#2e6a55'), 'curtain2': _t('#7cc8a8', '#245747'),
    'board_shadow': _t('#d7e4de', '#0c1210'), 'clock': _t('#ffffff', '#e4eaee'), 'pot': _t('#ffffff', '#2a333d'),
    'desk_top': _t('#ffffff', '#2c3540'), 'desk_front': _t('#f1f5f8', '#232b34'), 'desk_edge': _t('#d3dde4', '#3a4552'),
    'plate_edge': _t('#cfe9dd', '#2f5547'), 'info': _t('#5b4630', '#dccbb4'), 'empty': _t('#6b5236', '#dccbb4'),
    'empty2': _t('#7a6248', '#bfae98'), 'carpet': _t('#dcf2e8', '#16302a'), 'carpet_in': _t('#bfe6d4', '#24493d'),
    'sec_empty': _t('#4a7a66', '#8fc9b0'), 'bub_shadow': _t('#c5e3d5', '#0b1411'), 'sec_shadow': _t('#c4e6d6', '#0f231d'),
    'say': _t('#e3f0ff', '#22364d'), 'bub_edge': _t('#8a909a', '#4a5563'), 'bub_text': _t('#22262e', '#e6ebf0'),
    'log_bg': _t('#fafbfc', '#151a20'), 'note': _t('#b07a1f', '#e0a84a'), 'toast': _t('#26313d', '#33404e'),
}
PIN_COL = '#e0a100'                              # 세션톡에서 고정한 세션 (금색)
PIN_BG = _t('#fff7e0', '#2c2818')
CHAT = {  # 세션톡 (카카오톡 풍)
    'bg': _t('#b2c7d9', '#1b2430'), 'head': _t('#a9bfd2', '#161e28'), 'title': _t('#1e2329', '#e6ebf0'),
    'sub': _t('#46525e', '#8d99a8'), 'other': _t('#ffffff', '#2a3440'), 'text': _t('#191919', '#e6ebf0'),
    'name': _t('#2b333c', '#d5dde5'), 'time': _t('#56636f', '#8d99a8'), 'pill': _t('#93a8bc', '#2c3846'),
    'empty': _t('#4f5d6b', '#8d99a8'), 'typing': _t('#6b7580', '#8d99a8'), 'input': _t('#ffffff', '#1b2129'),
    'input_fg': _t('#1e1e1e', '#e6ebf0'), 'input_edge': _t('#dfe3e8', '#2b343f'), 'chip': _t('#eef1f4', '#242c36'),
    'hint': _t('#8a929c', '#8d99a8'),
    'code': _t('#b4235a', '#ff9ac1'), 'code_bg': _t('#eef1f4', '#202832'), 'link': _t('#1f6fd1', '#7db4ff'),  # 마크다운
    'typing_hl': _t('#c2410c', '#ffae6b'),  # 일하는 중 줄 (CLI의 주황 별)
}


def chip_colors(col):
    """상태 칩: 옅은 바탕 + 진한 글자 (다크에선 어두운 바탕 + 밝은 글자)."""
    if DARK:
        return mix(col, BG, 0.78), mix(col, '#ffffff', 0.25)
    return mix(col, '#ffffff', 0.86), shade(col, 0.72)


def is_light(hexc):
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return r * 0.299 + g * 0.587 + b * 0.114 > 150


SEC_H = 236                           # 비서실 줄 높이 (단위)
KIND_TEXT = {'request': T('부탁'), 'question': T('질문'), 'answer': T('답장'), 'info': T('알림')}
SEC_KIND = {'next': (T('다음 업무'), BLUE), 'check': (T('확인 권유'), '#f0a020'), 'warn': (T('주의'), RED),
            'info': (T('참고'), '#8a96a3')}
SEC_FILE = os.path.join(COLLAB, 'secretary.json')
USER_OUT = os.path.join(COLLAB, 'out-office-user.json')   # 세션톡에서 보낸 사용자 명령 (진행 패널 mod가 배달)
BRIEF_TEXT = T('비서 브리핑을 해 주세요 (secretary 스킬 순서대로, 마지막에 secretary.json 갱신).')
REMOTE_NEW_DEVICE = T('새 기기가 암호로 연결됐어요 · {0}')                       # 원격: 휴대폰 알림 문구
REMOTE_DEVICE = T('새 기기')
REMOTE_LOCKED = T('암호를 너무 많이 틀려서 1시간 동안 암호 연결을 막았어요')
SEC_HELLO = (T('당신은 이 PC의 비서 세션입니다. secretary 스킬(~/.claude/skills/secretary/SKILL.md)을 읽고 역할을 익히세요. '
             '사용자가 세션 사무실의 [브리핑 받기] 버튼을 누르거나 직접 요청할 때만 브리핑하고, 그 전에는 아무 작업도 하지 마세요. '
             '지금은 준비됐다고 한 줄로만 답하세요.'))
MEET_DIR = os.path.join(COLLAB, 'meeting')                 # 회의실: 회의마다 하위 폴더에 세션별 보고서·캡처
MEET_FILE = os.path.join(MEET_DIR, 'meeting.json')         # 지금 열린 회의 {id, startedAt, turns, members}
CAPTURE_PS1 = os.path.join(APP_DIR, 'assets', 'capture.ps1')
MEET_TURNS = (1, 3, 5, 10)
REPORT_ST = {'done': (T('완료'), '#2fb985'), 'partial': (T('일부'), '#f0a020'), 'todo': (T('못 함'), RED)}


MEET_STYLES = ('brief', 'rich')   # 간단 보고(토큰 절약) / 상세 보고(핵심 수치·그래프·캡처 확대)
REPORT_RICH = T('\n[상세 보고] 회사에서 보고하듯 한눈에 보이게 정리해 주세요. 위 JSON에 아래 칸을 더합니다.\n'
                '· highlights: 사용자가 꼭 알아야 할 핵심 2~4줄.\n'
                '· metrics: 직접 확인한 숫자만 2~6개 (테스트 통과 수, 걸린 시간, 바뀐 파일 수, 성능, 진행률 등). '
                '추측한 숫자는 쓰지 마세요. tone은 good/bad/neutral.\n'
                '· charts: 비교하거나 변화를 보여 줄 숫자가 있을 때만 1~3개. 그림 파일은 만들지 말고 데이터만 쓰면 회의실이 그려요. '
                'type은 bar(비교)/line(변화)/pie(비율)이고, labels와 values 개수를 맞추세요.\n'
                '· 캡처는 중요한 화면을 2~4장 찍고, 찍은 그림을 직접 열어 보고 가장 중요한 부분을 focus [x, y, 너비, 높이] '
                '(그림 전체를 1로 본 비율)로, 무엇을 봐야 하는지를 caption으로 적으세요. 회의실이 그 부분을 확대해 보여 줘요.\n'
                '"highlights":["..."],"metrics":[{"label":"테스트 통과","value":"41/42","note":"+3","tone":"good"}],'
                '"charts":[{"title":"빌드 시간","type":"bar","unit":"초","labels":["전","후"],"series":[{"name":"빌드","values":[120,45]}]}]\n'
                'items의 images 칸에는 {"file":"<sid>-1.png","caption":"무엇을 볼지","focus":[0.1,0.2,0.4,0.3]} 형식도 쓸 수 있어요.')


def meet_prompt(n, reqs, folder, sid, style='brief'):
    """회의실에서 세션에 보내는 업무 보고 요청. 보고서는 folder/<sid>.json, 캡처는 folder/<sid>-N.png.
    style='rich'이면 핵심 수치·그래프 데이터·캡처의 중요한 부분(focus)까지 적게 한다 (세션이 토큰을 더 쓴다)."""
    lines = '\n'.join(f'{i}) {one_line(q, 160)}' for i, q in enumerate(reqs, 1)) or T('(기록을 찾지 못했어요. 최근 대화에서 직접 찾아 주세요)')
    report = os.path.join(folder, sid + '.json')
    shot = os.path.join(folder, sid + '-1.png')
    return T('[회의실 업무 보고] 최근 {0}턴 동안 맡았던 작업의 결과를 보고서로 정리해 주세요. 코드는 바꾸지 말고 보고만 하세요.\n'
             '보고할 요청:\n{1}\n\n'
             '1. 요청마다 한 일, 결과(done=완료/partial=일부/todo=못 함), 바뀐 주요 파일, 남은 일을 짧게 정리합니다.\n'
             '2. 결과를 화면으로 보여 줄 수 있으면(UI·게임·웹·그래프·문서 등) 캡처해 PNG로 저장합니다. '
             '창 캡처: powershell -ExecutionPolicy Bypass -File "{2}" -Out "{3}" -Title "창 제목 일부" '
             '(-Title을 빼면 전체 화면). 만든 프로그램을 잠깐 띄워 찍고 닫아도 되고, 프로젝트의 스크린샷 방법을 써도 됩니다. '
             '파일 이름 끝 번호를 늘려 여러 장 찍어도 됩니다. 보여 줄 화면이 없으면 생략하세요.\n'
             '3. 마지막에 아래 JSON을 UTF-8로 {4} 에 씁니다.\n'
             '{{"sessionId":"{5}","title":"보고 제목","summary":"두세 문장 요약",'
             '"items":[{{"request":"요청 요약","status":"done","result":"결과","files":["경로"],"images":["{6}"]}}],'
             '"next":"다음에 할 일","updatedAt":<현재 시각 ms>}}\n'
             '다 쓰면 "보고서를 올렸어요" 한 줄로 답하세요.').format(n, lines, CAPTURE_PS1, shot, report, sid,
                                                          os.path.basename(shot)) + \
        (REPORT_RICH.replace('<sid>', sid) if style == 'rich' else '')


def meeting_start(app, sids, turns, style='brief'):
    """고른 세션들에 업무 보고를 요청하고 meeting.json을 새로 쓴다 (회의실 화면·휴대폰 둘 다 쓴다). 보낸 수를 돌려준다."""
    style = style if style in MEET_STYLES else 'brief'
    mid = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    m = {'id': mid, 'startedAt': int(time.time() * 1000), 'turns': turns, 'style': style, 'members': [], 'names': {}}
    folder = os.path.join(MEET_DIR, mid)
    os.makedirs(folder, exist_ok=True)
    head = meet_prompt(1, [], '', '').split(']', 1)[0] + ']'  # 지난 회의 요청은 보고할 턴에서 뺀다
    with app.mon.lock:
        todo = [(sid, app.mon.display_name(app.mon.sessions[sid]),
                 [e[1] for e in app.mon.sessions[sid].qa if not e[1].startswith(head)][-turns:])
                for sid in sids if sid in app.mon.sessions]
    for sid, name, reqs in todo:
        try:
            app.mon.send_user(sid, meet_prompt(turns, reqs, folder, sid, style))
        except OSError as ex:
            app.toast(T('보내기 실패: {0}').format(ex), warn=True)
            continue
        m['members'].append(sid)
        m['names'][sid] = name
    write_json(MEET_FILE, m)
    app.save_cfg()
    app.toast(T('{0}개 세션에 업무 보고를 요청했어요').format(len(m['members'])))
    return len(m['members'])


# 회의실 대사 타이핑 속도 (글자 사이 시간 배율, 0 = 한 번에)
TYPE_SPEEDS = (('slow', 1.6), ('normal', 1.0), ('fast', 0.45), ('instant', 0))
TYPE_SPEED_TEXT = {'slow': T('느리게'), 'normal': T('보통'), 'fast': T('빠르게'), 'instant': T('바로')}

MODE_TEXT = {'busy': T('작업 중'), 'wait': T('확인 필요'), 'idle': T('휴식'), 'sleep': T('조는 중')}
MODE_COL = {'busy': '#2fb985', 'wait': '#f0a020', 'idle': '#5b9ee8', 'sleep': '#9a86c8'}
LINE_COLS = ['#7fdbca', '#c792ea', '#ffcb6b', '#82aaff', '#f78c6c']
TOK_PARTS = ((T('새 입력'), '#2fb985'), (T('캐시 생성'), '#a77be0'), (T('캐시 읽기'), '#5b9ee8'), (T('출력'), '#f0a020'))
LOG_KINDS = (('all', T('전체')), ('user', T('요청')), ('tool', T('도구')), ('say', T('응답')), ('err', T('오류')), ('msg', T('메시지')))
LOG_FIRST = 300                 # 세션을 고를 때 로그에 처음 채우는 줄 수
LOG_ICON = {'user': '▶', 'say': '“', 'tool': '⚙', 'err': '⚠', 'msg': '✉'}
LOG_COL = {'user': _t('#1e9a66', '#4fd39a'), 'say': _t('#34404c', '#d5dde5'), 'tool': _t('#2f74d0', '#6aa8ff'),
           'err': _t('#d94848', '#ff7b7b'), 'msg': _t('#b7791f', '#f0b85a')}
FONT_SPECS = {  # 이름: (줌 100% 기준 pt, 굵기 normal/medium/bold/heavy)
    'brand': (13, 'heavy'), 'stat': (10, 'medium'), 'statb': (10, 'bold'),
    'h1': (16, 'heavy'), 'h2': (11, 'bold'), 'body': (10, 'normal'), 'bodyb': (10, 'bold'),
    'small': (9, 'medium'), 'num': (15, 'heavy'), 'log': (10, 'normal'), 'logb': (10, 'bold'),
    'c_name': (9, 'bold'), 'c_info': (9, 'medium'), 'c_bub': (9, 'medium'),
    'c_wbt': (11, 'heavy'), 'c_wbn': (9, 'bold'), 'c_wb': (9, 'medium'), 'c_z': (10, 'bold'), 'c_empty': (13, 'heavy'),
    'menu': (10, 'bold'), 'tiny': (8, 'bold'),
}
PRETENDARD = {'normal': ('Pretendard', 'normal'), 'medium': ('Pretendard Medium', 'normal'),
              'bold': ('Pretendard SemiBold', 'normal'), 'heavy': ('Pretendard ExtraBold', 'normal')}


def dark_titlebar(win):
    """윈도 제목 표시줄도 어둡게 (윈도 10 2004 이후). 실패하면 그대로 둔다."""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (새/옛 번호)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        win.withdraw()  # 제목 표시줄 색을 바로 다시 그리게
        win.deiconify()
    except Exception:
        pass


def font_face(weight, has_pretendard):
    bold = 'bold' if weight in ('bold', 'heavy') else 'normal'
    if LANG == 'ja':  # 가나·한자는 일본어 글꼴로 (Pretendard에 없음)
        return ('Yu Gothic UI Semibold', 'normal') if weight == 'bold' else ('Yu Gothic UI', bold)
    if LANG == 'zh':
        return 'Microsoft YaHei UI', bold
    if has_pretendard:
        return PRETENDARD[weight]
    return UI, 'bold' if weight in ('bold', 'heavy') else 'normal'


def mix(a, b, t):
    """색 a와 b를 t(0~1) 비율로 섞는다."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(int(x + (y - x) * t) for x, y in zip(ca, cb))


def ease_out(t):
    return 1 - (1 - t) ** 3


def ease_in_out(t):
    return t * t * (3 - 2 * t)


def ease_back(t):  # 살짝 지나쳤다가 제자리 (튀어나오는 것)
    c = 1.4
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


# ── 마크다운 (Claude Code 터미널처럼 세션톡 말풍선에) ──
MD_IMG = re.compile(r'!\[([^\]\n]*)\]\(<?([^)\n<>]+?)>?\)')
MD_INLINE = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__|`([^`\n]+)`|~~(?=\S)(.+?)(?<=\S)~~'
                       r'|\[([^\]\n]+)\]\(([^)\s]+)\)|(?<![*A-Za-z0-9])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*A-Za-z0-9])|(?<![_A-Za-z0-9])_(?=\S)([^_\n]+?)(?<=\S)_(?![_A-Za-z0-9])')
# (한국어 조사가 닫는 * 바로 뒤에 붙어도 기울임으로: 둘레 검사는 영문·숫자만)


def md_plain(text, n=None):
    """마크다운 기호를 걷어 낸 한 줄 (방 목록 미리보기 등)."""
    out = []
    for kind, prefix, runs in md_blocks(text):
        if kind in ('gap', 'hr'):
            continue
        if kind == 'img':  # 경로 대신 [사진]
            out.append('[' + T('사진') + ']')
            continue
        out.append((prefix.strip() + ' ' if prefix else '') + ''.join(t for t, _st in runs))
    return one_line(' '.join(out), n)


def md_inline(s, st=''):
    """한 줄 → [(글자, 스타일)]. 스타일 글자: b 굵게, i 기울임, c 코드, s 취소선, u 링크."""
    out, pos = [], 0
    for m in MD_INLINE.finditer(s):
        if m.start() > pos:
            out.append((s[pos:m.start()], st))
        g = m.groups()
        if g[0] is not None or g[1] is not None:
            out += md_inline(g[0] if g[0] is not None else g[1], st + 'b')
        elif g[2] is not None:
            out.append((g[2], st + 'c'))
        elif g[3] is not None:
            out += md_inline(g[3], st + 's')
        elif g[4] is not None:
            out += md_inline(g[4], st + 'u')
        else:
            out += md_inline(g[6] if g[6] is not None else g[7], st + 'i')
        pos = m.end()
    if pos < len(s):
        out.append((s[pos:], st))
    return out


def md_blocks(text):
    """줄 단위 블록 [(종류, 앞머리, [(글자, 스타일)])]. 종류: p 문단, h 제목, li 목록, q 인용, code 코드 블록, hr 줄, gap 빈 줄."""
    out, code = [], False
    for ln in text.replace('\r', '').split('\n'):
        st = ln.strip()
        if st.startswith('```'):
            code = not code
            continue
        if code:
            out.append(('code', '', [(ln.expandtabs(4) or ' ', 'c')]))
            continue
        if not st:
            if out and out[-1][0] != 'gap':
                out.append(('gap', '', []))
            continue
        m = re.match(r'(#{1,6})\s+(.*)', st)
        if m:
            out.append(('h', '', md_inline(m.group(2), 'b')))
            continue
        imgs = MD_IMG.findall(st)
        if imgs and not MD_IMG.sub('', st).strip():  # 사진만 있는 줄
            for alt, path in imgs:
                out.append(('img', '', [(path.strip(), alt.strip())]))
            continue
        if re.fullmatch(r'(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}', st):
            out.append(('hr', '', []))
            continue
        m = re.match(r'(\s*)([-*+]|\d+[.)])\s+(.*)', ln)
        if m:
            mark = '•' if m.group(2) in '-*+' else m.group(2)
            out.append(('li', '    ' * min(3, len(m.group(1).expandtabs(4)) // 2) + mark + ' ', md_inline(m.group(3))))
            continue
        m = re.match(r'>\s?(.*)', st)
        if m:
            out.append(('q', '', md_inline(m.group(1))))
            continue
        if st.startswith('|') and st.endswith('|'):  # 표는 고정폭 글자로 그대로
            out.append(('code', '', [(st, 'c')]))
            continue
        out.append(('p', '', md_inline(ln.rstrip())))
    while out and out[-1][0] == 'gap':
        out.pop()
    return out


_MD_TOKEN = re.compile(r'\s+|[A-Za-z0-9_\-./:@#%&+=?!,;\'"()\[\]{}<>~^$*|\\]+|.')


def md_layout(text, maxw, fonts, img=None):
    """마크다운을 maxw 안에 줄바꿈해 그릴 조각들로. ([('t', x, y, 글자, 스타일) | ('bg', x0, y0, x1, y1) | ('bar', x, y0, y1)
    | ('hr', x0, x1, y)], 너비, 높이). 좌표는 왼쪽 위 기준."""
    fb, fbb, fi, fm = fonts['body'], fonts['bodyb'], fonts['bodyi'], fonts['mono']
    lh = max(fb.metrics('linespace'), fm.metrics('linespace'))
    font_of = lambda s: fm if 'c' in s else fbb if 'b' in s else fi if 'i' in s else fb
    ops, y, used = [], 0, 0
    for kind, prefix, runs in md_blocks(text):
        if kind == 'gap':
            y += lh * 0.4
            continue
        if kind == 'img':  # 사진: img(경로) → PhotoImage(그릴 때) 또는 None(오래된 대화·못 읽음 → 눌러 보는 줄)
            path, alt = runs[0]
            ph = img(path) if img else None
            if ph:
                ops.append(('img', 0, y, path, ph))
                used = max(used, ph.width())
                y += ph.height() + lh * 0.3
            else:
                label = '🖼 ' + (alt or os.path.basename(path)) + '  ' + T('(눌러서 보기)')
                ops.append(('imglink', 0, y, path, label))
                used = max(used, min(maxw, fb.measure(label)))
                y += lh
            continue
        if kind == 'hr':
            ops.append(('hr', 0, maxw, y + lh / 2))
            used = max(used, maxw * 0.5)
            y += lh
            continue
        left = 0
        if prefix:
            ops.append(('t', 0, y, prefix, ''))
            left = fb.measure(prefix)
        if kind == 'q':
            left = 12
        y0, x = y, left
        seg = None  # [x, 글자, 스타일]

        def flush():
            nonlocal seg
            if seg and seg[1]:
                ops.append(('t', seg[0], y, seg[1], seg[2]))
            seg = None

        for s, st in runs:
            f = font_of(st)
            for tok in _MD_TOKEN.findall(s):
                pieces = [tok] if f.measure(tok) <= maxw - left else list(tok)  # 아주 긴 낱말(주소 등)은 글자 단위로
                for p in pieces:
                    w = f.measure(p)
                    if x + w > maxw and x > left:
                        flush()
                        used = max(used, x)
                        y += lh
                        x = left
                        if p.isspace():
                            continue
                    if seg and seg[2] == st:
                        seg[1] += p
                    else:
                        flush()
                        seg = [x, p, st]
                    x += w
        flush()
        used = max(used, x)
        y += lh
        if kind == 'q':
            ops.append(('bar', 3, y0 + 2, y - 2))
        if kind == 'code':  # 코드 블록 줄은 바탕을 끝까지
            ops.append(('bg', 0, y0, maxw, y))
    # 글자 너비 기준으로 코드 블록 바탕을 맞추고, 인라인 코드 바탕을 붙인다
    used = min(maxw, max(used, 1))
    final = []
    for o in ops:
        if o[0] == 'bg':
            final.append(('bg', o[1] - 3, o[2], used + 3, o[4]))
        elif o[0] == 'hr':
            final.append(('hr', 0, used, o[3]))
        else:
            if o[0] == 't' and 'c' in o[4] and o[3].strip():
                w = fm.measure(o[3])
                final.append(('cbg', o[1] - 2, o[2] + 1, o[1] + w + 2, o[2] + lh - 1))
            final.append(o)
    return final, used, max(y, lh)


def open_talk_image(path):
    """세션톡 사진을 크게 보기 (PNG·GIF는 앱 안에서, 그 밖의 형식은 기본 프로그램으로)."""
    if not os.path.isfile(path):
        _APP[0].toast(T('사진 파일이 없어요 · {0}').format(path), warn=True) if _APP else None
        return
    if path.lower().endswith(('.png', '.gif')) and _APP:
        ImageViewer(_APP[0], [path], 0)
    else:
        os.startfile(path)


_APP = []  # 지금 앱 (사진 크게 보기 창을 띄울 때)


def md_draw(c, ops, ox, oy, fonts, fg, code_fg, code_bg, link_fg, dim):
    """md_layout의 조각들을 (ox, oy)에 그린다."""
    fb, fbb, fi, fm = fonts['body'], fonts['bodyb'], fonts['bodyi'], fonts['mono']
    for o in ops:
        k = o[0]
        if k in ('bg', 'cbg'):
            c.create_rectangle(ox + o[1], oy + o[2], ox + o[3], oy + o[4], fill=code_bg, outline='')
        elif k == 'bar':
            c.create_line(ox + o[1], oy + o[2], ox + o[1], oy + o[3], fill=dim, width=3)
        elif k == 'hr':
            c.create_line(ox + o[1], oy + o[3], ox + o[2], oy + o[3], fill=dim)
    for o in ops:
        if o[0] == 'img':
            t = c.create_image(ox + o[1], oy + o[2], image=o[4], anchor='nw')
            c.tag_bind(t, '<Button-1>', lambda e, p=o[3]: open_talk_image(p))
        elif o[0] == 'imglink':
            t = c.create_text(ox + o[1], oy + o[2], text=o[4], anchor='nw', font=fb, fill=link_fg)
            c.tag_bind(t, '<Button-1>', lambda e, p=o[3]: open_talk_image(p))
    for o in ops:
        if o[0] != 't':
            continue
        st = o[4]
        f = fm if 'c' in st else fbb if 'b' in st else fi if 'i' in st else fb
        col = code_fg if 'c' in st else link_fg if 'u' in st else fg
        t = c.create_text(ox + o[1], oy + o[2], text=o[3], anchor='nw', font=f, fill=col)
        if 'u' in st or 's' in st:
            x0, y0, x1, y1 = c.bbox(t)
            yy = y1 - 2 if 'u' in st else (y0 + y1) / 2
            c.create_line(x0, yy, x1, yy, fill=col)


def mix_any(a, b, t):
    """mix인데 '#rrggbb'가 아닌 색 이름이 섞여 있으면 끝 색으로."""
    if not (isinstance(a, str) and isinstance(b, str) and len(a) == 7 == len(b) and a[0] == b[0] == '#'):
        return b if t >= 1 else a
    return mix(a, b, t)


class Tweens:
    """시간 기준 애니메이션 (프레임이 밀려도 정해진 시간 안에 끝난다). 같은 key로 새로 부르면 앞의 것은 그 자리에서 멈추고
    새 것이 이어받는다. 설정에서 움직임을 끄면 바로 마지막 모습으로."""

    def __init__(self, root, on):
        self.root, self.on, self.jobs = root, on, {}

    def run(self, ms, step, done=None, ease=ease_out, key=None):
        token = object()
        if key is not None:
            self.jobs[key] = token
        if ms <= 0 or not self.on():
            self._end(key, token, step, done, 1.0)
            return
        t0 = time.perf_counter()

        def tick():
            if key is not None and self.jobs.get(key) is not token:
                return
            p = min(1.0, (time.perf_counter() - t0) * 1000 / ms)
            if p >= 1:
                self._end(key, token, step, done, 1.0)
                return
            try:
                step(ease(p))
            except tk.TclError:  # 위젯이 사라짐
                self.jobs.pop(key, None)
                return
            self.root.after(15, tick)

        tick()

    def _end(self, key, token, step, done, p):
        if key is not None and self.jobs.get(key) is token:
            del self.jobs[key]
        try:
            step(p)
            if done:
                done()
        except tk.TclError:
            pass

    def busy(self, key):
        return key in self.jobs

    def stop(self, key):
        self.jobs.pop(key, None)


def gradient(c, x0, y0, x1, y1, c0, c1, tags=(), step=3):
    """가로 그라데이션 띠 (캔버스 픽셀 좌표)."""
    w = max(1, int(x1 - x0))
    for i in range(0, w, step):
        c.create_rectangle(x0 + i, y0, x0 + i + step, y1, fill=mix(c0, c1, i / w), outline='', tags=tags)


def draw_icon(c, kind, cx, cy, s, col, bg='#ffffff', tags=()):
    """간단한 선 아이콘. s = 아이콘 반지름(px), bg = 구멍(문, 말줄임표) 색."""
    w = max(1.5, s * 0.16)
    kw = dict(fill=col, width=w, capstyle='round', joinstyle='round', tags=tags)
    ow = dict(outline=col, width=w, tags=tags)
    if kind == 'menu':
        for dy in (-0.55, 0, 0.55):
            c.create_line(cx - s * 0.75, cy + dy * s, cx + s * 0.75, cy + dy * s, **kw)
    elif kind == 'home':
        c.create_polygon(cx - s * 0.85, cy - s * 0.05, cx, cy - s * 0.85, cx + s * 0.85, cy - s * 0.05, cx + s * 0.62, cy - s * 0.05,
                         cx + s * 0.62, cy + s * 0.8, cx - s * 0.62, cy + s * 0.8, cx - s * 0.62, cy - s * 0.05, fill=col, outline='',
                         tags=tags)
        c.create_rectangle(cx - s * 0.18, cy + s * 0.25, cx + s * 0.18, cy + s * 0.8, fill=bg,
                           outline='', tags=tags)
    elif kind == 'chat':
        round_rect(c, cx - s * 0.85, cy - s * 0.7, cx + s * 0.85, cy + s * 0.45, s * 0.35, fill=col, outline='', tags=tags)
        c.create_polygon(cx - s * 0.45, cy + s * 0.3, cx - s * 0.55, cy + s * 0.85, cx - s * 0.05, cy + s * 0.4, fill=col,
                         outline='', tags=tags)
        for dx in (-0.4, 0, 0.4):
            c.create_oval(cx + dx * s - s * 0.09, cy - s * 0.2, cx + dx * s + s * 0.09, cy - s * 0.02, fill=bg,
                          outline='', tags=tags)
    elif kind == 'star':
        pts = []
        for k in range(10):
            r = s * (0.9 if k % 2 == 0 else 0.4)
            a = math.radians(-90 + k * 36)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        c.create_polygon(*pts, fill=col, outline='', tags=tags)
    elif kind == 'spark':  # 브리핑: 말풍선 + 반짝
        c.create_oval(cx - s * 0.8, cy - s * 0.6, cx + s * 0.6, cy + s * 0.55, **ow)
        c.create_line(cx - s * 0.45, cy + s * 0.45, cx - s * 0.7, cy + s * 0.85, **kw)
        c.create_line(cx + s * 0.55, cy - s * 0.95, cx + s * 0.55, cy - s * 0.45, **kw)
        c.create_line(cx + s * 0.3, cy - s * 0.7, cx + s * 0.8, cy - s * 0.7, **kw)
        c.create_line(cx - s * 0.4, cy - s * 0.05, cx + s * 0.25, cy - s * 0.05, **kw)
    elif kind == 'power':
        c.create_arc(cx - s * 0.75, cy - s * 0.7, cx + s * 0.75, cy + s * 0.8, start=120, extent=300, style='arc', **ow)
        c.create_line(cx, cy - s * 0.95, cx, cy + s * 0.05, **kw)
    elif kind == 'plus':
        c.create_oval(cx - s * 0.85, cy - s * 0.85, cx + s * 0.85, cy + s * 0.85, **ow)
        c.create_line(cx - s * 0.4, cy, cx + s * 0.4, cy, **kw)
        c.create_line(cx, cy - s * 0.4, cx, cy + s * 0.4, **kw)
    elif kind == 'folder':
        c.create_polygon(cx - s * 0.85, cy - s * 0.6, cx - s * 0.25, cy - s * 0.6, cx - s * 0.1, cy - s * 0.4, cx + s * 0.85, cy - s * 0.4,
                         cx + s * 0.85, cy + s * 0.7, cx - s * 0.85, cy + s * 0.7, fill=col, outline='', tags=tags)
    elif kind == 'user':
        c.create_oval(cx - s * 0.38, cy - s * 0.85, cx + s * 0.38, cy - s * 0.1, fill=col, outline='', tags=tags)
        c.create_arc(cx - s * 0.8, cy - s * 0.05, cx + s * 0.8, cy + s * 1.5, start=0, extent=180, fill=col, outline='',
                     tags=tags)
    elif kind == 'tab':
        c.create_rectangle(cx - s * 0.85, cy - s * 0.4, cx + s * 0.85, cy + s * 0.75, **ow)
        c.create_rectangle(cx - s * 0.85, cy - s * 0.8, cx - s * 0.1, cy - s * 0.4, fill=col, outline=col, tags=tags)
    elif kind in ('zoomin', 'zoomout'):
        c.create_text(cx - s * 0.15, cy + s * 0.1, text=T('가'), fill=col, font=('Malgun Gothic', max(6, int(s * 0.9)), 'bold'),
                      tags=tags)
        c.create_line(cx + s * 0.45, cy - s * 0.55, cx + s * 0.95, cy - s * 0.55, **kw)
        if kind == 'zoomin':
            c.create_line(cx + s * 0.7, cy - s * 0.8, cx + s * 0.7, cy - s * 0.3, **kw)
    elif kind == 'pin':
        c.create_oval(cx - s * 0.45, cy - s * 0.9, cx + s * 0.45, cy - s * 0.0, fill=col, outline='', tags=tags)
        c.create_line(cx, cy - s * 0.1, cx, cy + s * 0.9, **kw)
    elif kind == 'play':
        c.create_polygon(cx - s * 0.5, cy - s * 0.75, cx + s * 0.8, cy, cx - s * 0.5, cy + s * 0.75, fill=col, outline='', tags=tags)
    elif kind == 'window':
        c.create_rectangle(cx - s * 0.85, cy - s * 0.7, cx + s * 0.85, cy + s * 0.75, **ow)
        c.create_rectangle(cx - s * 0.85, cy - s * 0.7, cx + s * 0.85, cy - s * 0.3, fill=col, outline=col, tags=tags)
        c.create_rectangle(cx - s * 0.2, cy - s * 0.05, cx + s * 0.6, cy + s * 0.5, **ow)
    elif kind == 'moon':
        c.create_oval(cx - s * 0.8, cy - s * 0.8, cx + s * 0.8, cy + s * 0.8, fill=col, outline='', tags=tags)
        c.create_oval(cx - s * 0.35, cy - s * 1.0, cx + s * 1.05, cy + s * 0.45, fill=bg, outline='', tags=tags)
    elif kind == 'globe':
        c.create_oval(cx - s * 0.85, cy - s * 0.85, cx + s * 0.85, cy + s * 0.85, **ow)
        c.create_oval(cx - s * 0.38, cy - s * 0.85, cx + s * 0.38, cy + s * 0.85, **ow)
        c.create_line(cx - s * 0.85, cy, cx + s * 0.85, cy, **kw)
    elif kind == 'meet':  # 회의실: 둥근 탁자에 둘러앉은 세 사람
        for dx, hy in ((-0.6, -0.5), (0.6, -0.5), (0, -0.68)):  # 머리 + 어깨 (가운데 사람이 앞)
            c.create_arc(cx + (dx - 0.32) * s, cy + (hy + 0.2) * s, cx + (dx + 0.32) * s, cy + (hy + 0.9) * s, start=0, extent=180,
                         fill=col, outline=bg, width=max(1, w * 0.6), tags=tags)
            c.create_oval(cx + (dx - 0.18) * s, cy + (hy - 0.18) * s, cx + (dx + 0.18) * s, cy + (hy + 0.18) * s, fill=col,
                          outline=bg, width=max(1, w * 0.6), tags=tags)
        round_rect(c, cx - s * 0.95, cy + s * 0.22, cx + s * 0.95, cy + s * 0.55, s * 0.12, fill=col, tags=tags)
        c.create_line(cx - s * 0.95, cy + s * 0.2, cx + s * 0.95, cy + s * 0.2, fill=bg, width=max(1, w * 0.6), tags=tags)
        c.create_line(cx - s * 0.6, cy + s * 0.55, cx - s * 0.6, cy + s * 0.92, **kw)
        c.create_line(cx + s * 0.6, cy + s * 0.55, cx + s * 0.6, cy + s * 0.92, **kw)
    elif kind == 'type':  # 대사 속도: 말풍선 안에 길이가 다른 줄
        round_rect(c, cx - s * 0.85, cy - s * 0.7, cx + s * 0.85, cy + s * 0.45, s * 0.3, outline=col, width=w, tags=tags)
        c.create_polygon(cx - s * 0.45, cy + s * 0.38, cx - s * 0.55, cy + s * 0.85, cx - s * 0.1, cy + s * 0.42, fill=col, outline='', tags=tags)
        for k, (x1, y_) in enumerate(((0.5, -0.36), (0.15, -0.08), (-0.15, 0.2))):
            c.create_line(cx - s * 0.5, cy + s * y_, cx + s * x1, cy + s * y_, **kw)
    elif kind == 'phone':  # 원격 조종: 휴대폰 + 전파
        round_rect(c, cx - s * 0.42, cy - s * 0.85, cx + s * 0.32, cy + s * 0.85, s * 0.16, outline=col, width=w, tags=tags)
        c.create_line(cx - s * 0.18, cy + s * 0.6, cx + s * 0.08, cy + s * 0.6, **kw)
        for r_ in (0.32, 0.6):
            c.create_arc(cx + s * 0.38 - r_ * s, cy - s * 0.55 - r_ * s, cx + s * 0.38 + r_ * s, cy - s * 0.55 + r_ * s,
                         start=-30, extent=90, style='arc', **ow)
    elif kind == 'plug':  # 세션 연결 mod: 플러그
        round_rect(c, cx - s * 0.55, cy - s * 0.35, cx + s * 0.55, cy + s * 0.35, s * 0.14, fill=col, outline='', tags=tags)
        for dx in (-0.25, 0.25):
            c.create_line(cx + dx * s, cy - s * 0.35, cx + dx * s, cy - s * 0.85, **kw)
        c.create_line(cx, cy + s * 0.35, cx, cy + s * 0.7, **kw)
    elif kind == 'close':
        c.create_line(cx - s * 0.6, cy - s * 0.6, cx + s * 0.6, cy + s * 0.6, **kw)
        c.create_line(cx - s * 0.6, cy + s * 0.6, cx + s * 0.6, cy - s * 0.6, **kw)


LOCAL = os.environ.get('LOCALAPPDATA', HOME)
STORAGE = (  # (묶음, 이름, 경로, 설명, 하위 폴더도 보이기)
    ('Claude Code', T('대화 기록'), os.path.join(CLAUDE, 'projects'),
     T('세션 대화 전문(.jsonl). 지우면 그 세션을 /resume으로 이어갈 수 없습니다'), True),
    ('Claude Code', T('파일 편집 기록'), os.path.join(CLAUDE, 'file-history'), T('되돌리기(rewind)용 파일 사본'), False),
    ('Claude Code', T('프로그램 버전'), os.path.join(HOME, '.local', 'share', 'claude', 'versions'),
     T('설치된 Claude Code 실행 파일 (버전마다 하나)'), True),
    ('Claude Code', T('임시 폴더'), os.path.join(LOCAL, 'Temp', 'claude'), T('세션별 임시 작업 공간과 백그라운드 작업 출력'), True),
    ('Claude Code', T('플러그인'), os.path.join(CLAUDE, 'plugins'), T('설치한 플러그인과 마켓플레이스'), False),
    ('Claude Code', T('스킬'), os.path.join(CLAUDE, 'skills'), '', False),
    ('Claude Code', T('캐시'), os.path.join(CLAUDE, 'cache'), '', False),
    ('Claude Code', T('붙여 넣기 캐시'), os.path.join(CLAUDE, 'paste-cache'), '', False),
    ('Claude Code', T('셸 스냅숏'), os.path.join(CLAUDE, 'shell-snapshots'), '', False),
    ('Claude Code', T('백그라운드 작업'), os.path.join(CLAUDE, 'jobs'), '', False),
    ('Claude Code', T('설정 백업'), os.path.join(CLAUDE, 'backups'), '', False),
    ('Claude Code', T('CLI 캐시'), os.path.join(LOCAL, 'claude-cli-nodejs'), '', False),
    (T('Claude 앱'), T('앱 데이터'), os.path.join(LOCAL, 'Claude-Data'),
     T('설정·로그인·Cowork 작업. 직접 지우지 말고 앱의 설정 > 저장 공간에서 정리하세요'), False),
    (T('Claude 앱'), T('앱 캐시'), os.path.join(LOCAL, 'Claude'), T('화면 캐시·로그'), False),
    ('Claude Office', T('이 프로그램'), CACHE_DIR, T('토큰 집계 캐시와 창 설정'), False),
)


class _ThisModule:
    """원격 모듈에 이 프로그램의 함수·상수를 넘길 때 쓴다 (시험에서 바꾼 값도 그대로 보이게 늘 지금 값을 읽는다)."""

    def __getattr__(self, k):
        return globals()[k]


class PageHost(tk.Frame):
    """메뉴 기능을 별도 창 대신 사무실 화면 자리에 띄우는 틀. 창(Toplevel)처럼 불러도 되게 창 전용 메서드는 받아 넘긴다."""

    def __init__(self, app, key):
        super().__init__(app.page_slot, bg=BG)
        self.app, self.key = app, key
        # 오른쪽에서 살짝 밀려 들어온다 (안쪽 위젯은 통째로 따라 움직여 다시 배치하지 않는다).
        # 화면을 다 만든 뒤(after_idle)에 시작해야 무거운 화면도 미끄러지는 모습이 보인다
        self.place(x=int(56 * app.Z), y=0, relwidth=1, relheight=1)
        app.root.after_idle(lambda: self.winfo_exists() and app.tw.run(
            280, lambda e: self.place_configure(x=int(56 * app.Z * (1 - e))), ease=ease_out, key='page'))

    def title(self, text=None):
        if text:
            self._title = str(text).split(' · ')[0]
            self.app.page_title.configure(text=self._title)

    def bind(self, seq=None, func=None, add=None):
        # 창이면 안쪽 위젯 모두에 걸리는 단축키·휠을, 틀에서는 이 틀 안의 위젯에서 일어난 것만 받도록 넘긴다
        self.app.route(seq, self, func)

    def destroy(self):
        super().destroy()
        self.app._page_gone(self)

    def geometry(self, *a):
        pass

    def minsize(self, *a):
        pass

    def resizable(self, *a):
        pass

    def transient(self, *a):
        pass

    def protocol(self, *a):
        pass

    def attributes(self, *a):
        pass

    def deiconify(self):
        """숨겨 둔 화면(KEEP_PAGES)을 다시 보인다."""
        app = self.app
        if app.page is self:
            return
        if app.page:
            old, app.page = app.page, None
            app._drop_page(old)
        app.paned.pack_forget()
        app.page_wrap.pack(fill='both', expand=True)
        app.page, app.page_key = self, self.key
        self.place(x=int(56 * app.Z), y=0, relwidth=1, relheight=1)
        app.tw.run(280, lambda e: self.place_configure(x=int(56 * app.Z * (1 - e))), ease=ease_out, key='page')
        if getattr(self, '_title', None):
            app.page_title.configure(text=self._title)
        app.draw_side(force=True)


KEEP_PAGES = ('chat',)  # 자주 오가는 화면: 닫지 않고 숨겨 두어 다시 열 때 렉이 없게


class StorageWin:
    """Claude 관련 폴더들이 차지하는 용량을 보여 주고 폴더를 연다."""

    def __init__(self, app):
        self.app = app
        F = app.fonts
        self.win = w = app.make_host('folder')
        w.title(T('저장 공간 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(820 * app.Z)}x{int(640 * app.Z)}')
        w.minsize(600, 400)
        w.protocol('WM_DELETE_WINDOW', self.close)
        head = tk.Frame(w, bg=TOPBG, padx=14, pady=8)
        head.pack(fill='x')
        tk.Label(head, text=T('저장 공간'), font=F['brand'], bg=TOPBG, fg=ACCENT).pack(side='left')
        self.total = tk.Label(head, text='', font=F['statb'], bg=TOPBG, fg=FG)
        self.total.pack(side='left', padx=16)
        self.rescan = app._btn(head, T('다시 계산'), self.scan)
        self.rescan.pack(side='right')
        self.tabs = {}
        for k, label in (('files', T('PC 파일')), ('claude', T('Claude 폴더'))):
            b = app._btn(head, label, lambda k=k: self.show(k))
            b.pack(side='right', padx=(0, 6))
            self.tabs[k] = b
        self.claude_box = box = tk.Frame(w, bg=BG)
        tk.Label(box, text=T('보기와 폴더 열기만 합니다 (아무것도 지우지 않음). 크기는 백그라운드에서 셉니다.'),
                 font=F['small'], bg=BG, fg=DIM, anchor='w', padx=14).pack(fill='x', pady=(6, 0))
        body = tk.Frame(box, bg=BG)
        body.pack(fill='both', expand=True, padx=(14, 0), pady=8)
        self.cv = cv = tk.Canvas(body, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(body, command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        cv.pack(side='left', fill='both', expand=True)
        self.inner = tk.Frame(cv, bg=BG)
        self.inner_id = cv.create_window(0, 0, window=self.inner, anchor='nw')
        self.inner.bind('<Configure>', lambda e: cv.configure(scrollregion=cv.bbox('all')))
        cv.bind('<Configure>', lambda e: cv.itemconfig(self.inner_id, width=e.width - 12))
        self.files = FileBrowser(app, w)
        w.bind('<MouseWheel>', lambda e: (self.files.cv if self.tab == 'files' else cv).yview_scroll(int(-e.delta / 120), 'units'))
        self.stop = threading.Event()
        self.results = collections.deque()
        self.rows = {}
        self.tab = None
        self.build()
        self.show(app.cfg.get('storage_tab') or 'claude')

    def show(self, k):
        """위쪽 탭: Claude 폴더(용량) / PC 파일(탐색기)."""
        if k == self.tab:
            return
        self.tab = k
        self.app.cfg['storage_tab'] = k
        for key, b in self.tabs.items():
            on = key == k
            b._bg = ACCENT if on else CARD2
            b.configure(bg=b._bg, fg='#ffffff' if on else FG)
        self.claude_box.pack_forget()
        self.files.frame.pack_forget()
        if k == 'files':
            self.rescan.pack_forget()
            self.total.configure(text='')
            self.files.frame.pack(fill='both', expand=True)
            self.files.start()
        else:
            self.rescan.pack(side='right', before=self.tabs['files'])
            self.claude_box.pack(fill='both', expand=True)
            if not any(r['val'] is not None for r in self.rows.values()):
                self.scan()

    def build(self):
        F, app = self.app.fonts, self.app
        group = None
        for i, (grp, name, path, note, sub) in enumerate(STORAGE):
            if grp != group:
                group = grp
                tk.Label(self.inner, text=grp, font=F['h2'], bg=BG, fg=ACCENT, anchor='w').pack(fill='x', pady=(10, 2))
            card = tk.Frame(self.inner, bg=CARD, padx=12, pady=8)
            card.pack(fill='x', pady=(0, 6), padx=(0, 4))
            top = tk.Frame(card, bg=CARD)
            top.pack(fill='x')
            app._btn(top, T('폴더 열기'), lambda p=path: open_folder(p)).pack(side='right')
            tk.Label(top, text=name, font=F['bodyb'], bg=CARD, fg=FG).pack(side='left')
            size = tk.Label(top, text=T('계산 중…'), font=F['bodyb'], bg=CARD, fg=DIM)
            size.pack(side='left', padx=(14, 0))
            cnt = tk.Label(top, text='', font=F['small'], bg=CARD, fg=DIM)
            cnt.pack(side='left', padx=(10, 0))
            bar = tk.Canvas(card, height=int(6 * app.Z), bg=CARD2, highlightthickness=0)
            bar.pack(fill='x', pady=(6, 2))
            tk.Label(card, text=path, font=F['small'], bg=CARD, fg=DIM, anchor='w').pack(fill='x')
            if note:
                tk.Label(card, text=note, font=F['small'], bg=CARD, fg=ROOM['note'], anchor='w', justify='left',
                         wraplength=int(700 * app.Z)).pack(fill='x')
            subf = tk.Frame(card, bg=CARD)
            subf.pack(fill='x')
            self.rows[i] = r = dict(size=size, cnt=cnt, bar=bar, sub=subf, val=None, frac=0)
            bar.bind('<Configure>', lambda e, r=r: self._bar(r['bar'], r['frac']))

    def scan(self):
        self.stop.set()
        self.stop = stop = threading.Event()
        for r in self.rows.values():
            r['size'].configure(text=T('계산 중…'), fg=DIM)
            r['val'] = None
        self.total.configure(text='')
        threading.Thread(target=self._work, args=(stop,), daemon=True).start()
        self.app.root.after(200, self._drain)

    def _work(self, stop):
        for i, (_g, _n, path, _note, sub) in enumerate(STORAGE):
            if stop.is_set():
                return
            if not os.path.isdir(path):
                self.results.append((i, None, None, []))
                continue
            kids = []
            if sub:
                total = files = 0
                try:
                    entries = list(os.scandir(path))
                except OSError:
                    entries = []
                for e in entries:
                    try:
                        d = e.is_dir(follow_symlinks=False)
                        b, n = dir_usage(e.path, stop) if d else (e.stat(follow_symlinks=False).st_size, 1)
                    except OSError:
                        continue
                    total += b
                    files += n
                    if b:
                        kids.append((e.name, e.path if d else path, b, n))
            else:
                total, files = dir_usage(path, stop)
            kids.sort(key=lambda k: -k[2])
            self.results.append((i, total, files, kids[:8]))

    def _drain(self):
        if not self.win.winfo_exists():
            return
        while self.results:
            i, total, files, kids = self.results.popleft()
            r = self.rows[i]
            if total is None:
                r['size'].configure(text=T('없음'), fg=DIM)
                continue
            r['val'] = total
            r['size'].configure(text=fmt_bytes(total), fg=FG)
            r['cnt'].configure(text=T('파일 {0:,}개').format(files))
            for c in r['sub'].winfo_children():
                c.destroy()
            F = self.app.fonts
            for name, path, b, n in kids:
                row = tk.Frame(r['sub'], bg=CARD)
                row.pack(fill='x', pady=(3, 0))
                self.app._btn(row, T('열기'), lambda p=path: open_folder(p)).pack(side='right')
                tk.Label(row, text=f'└ {fmt_bytes(b):>8}', font=F['small'], bg=CARD, fg=FG).pack(side='left')
                short = name if len(name) <= 56 else name[:26] + '…' + name[-26:]
                tk.Label(row, text='  ' + short, font=F['small'], bg=CARD, fg=DIM).pack(side='left')
        vals = [r['val'] for r in self.rows.values() if r['val'] is not None]
        if vals:
            big = max(vals) or 1
            for r in self.rows.values():
                if r['val'] is not None:
                    r['frac'] = r['val'] / big
                    self._bar(r['bar'], r['frac'])
            pending = sum(1 for r in self.rows.values() if r['size'].cget('text') == T('계산 중…'))
            self.total.configure(text=T('합계 {0}').format(fmt_bytes(sum(vals))) + (T('  (계산 중 {0}곳)').format(pending) if pending else ''))
            if not pending:
                return
        self.app.root.after(300, self._drain)

    @staticmethod
    def _bar(c, frac):
        c.delete('all')
        w, h = c.winfo_width(), int(c['height'])
        c.create_rectangle(0, 0, max(2, w * frac), h, fill='#82b4ff' if frac < 0.5 else '#f0b429', outline='')

    def close(self):
        self.stop.set()
        self.app.storage = None
        self.win.destroy()


FILES_MAX = 3000  # PC 파일: 한 폴더에서 보여 주는 항목 수 (그보다 많으면 검색으로 좁힌다)


def drives():
    """이 PC의 드라이브 (C:\\, D:\\ …)."""
    if os.name != 'nt':
        return ['/']
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f'{chr(65 + i)}:\\' for i in range(26) if mask >> i & 1]


def list_dir(path):
    """폴더 안 (이름, 전체 경로, 폴더인지, 크기, 고친 시각). 폴더 먼저, 이름순. 못 읽는 항목은 건너뛴다."""
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                d = e.is_dir()
                st = e.stat(follow_symlinks=False)
                out.append((e.name, e.path, d, 0 if d else st.st_size, st.st_mtime))
            except OSError:
                out.append((e.name, e.path, False, 0, 0))
    out.sort(key=lambda x: (not x[2], x[0].lower()))
    return out


class FileBrowser:
    """저장 공간의 [PC 파일] 탭: 파일 탐색기처럼 폴더를 오가며 경로를 확인하고 복사한다. 보기·열기만 하고 아무것도 바꾸지 않는다."""

    def __init__(self, app, parent):
        self.app = app
        F = app.fonts
        Z = app.Z
        self.frame = fr = tk.Frame(parent, bg=BG)
        self.path, self.items, self.sel, self.hist = None, [], None, []
        self.results = collections.deque()
        # 바로 가기: 드라이브·자주 쓰는 폴더
        quick = tk.Frame(fr, bg=BG, padx=14)
        quick.pack(fill='x', pady=(8, 0))
        spots = [(d, d) for d in drives()]
        spots += [(T('홈'), HOME), (T('바탕 화면'), os.path.join(HOME, 'Desktop')), (T('다운로드'), os.path.join(HOME, 'Downloads')),
                  (T('문서'), os.path.join(HOME, 'Documents')), ('.claude', CLAUDE)]
        for label, p in spots:
            if os.path.isdir(p):
                app._btn(quick, label, lambda p=p: self.go(p), font='small').pack(side='left', padx=(0, 4))
        # 주소 줄
        bar = tk.Frame(fr, bg=BG, padx=14)
        bar.pack(fill='x', pady=(6, 0))
        app._btn(bar, '←', self.back).pack(side='left')
        app._btn(bar, '↑', self.up).pack(side='left', padx=(4, 6))
        self.addr = tk.Entry(bar, font=F['body'], bg=CARD2, fg=FG, relief='flat', highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=ACCENT, insertbackground=FG)
        self.addr.pack(side='left', fill='x', expand=True, ipady=3)
        self.addr.bind('<Return>', lambda e: self.go(self.addr.get().strip().strip('"')))
        app._btn(bar, T('탐색기에서 열기'), lambda: open_folder(self.sel or self.path or HOME, select=bool(self.sel))).pack(side='right', padx=(6, 0))
        app._btn(bar, T('경로 복사'), self.copy, bg=ACCENT, fg='#ffffff', hover=ACC_DARK).pack(side='right', padx=(6, 0))
        # 검색·상태
        row = tk.Frame(fr, bg=BG, padx=14)
        row.pack(fill='x', pady=(6, 0))
        self.status = tk.Label(row, text='', font=F['small'], bg=BG, fg=DIM, anchor='w')
        self.status.pack(side='left', fill='x', expand=True)
        self.q = tk.StringVar()
        self.q.trace_add('write', lambda *a: self.draw())
        tk.Entry(row, textvariable=self.q, font=F['body'], bg=CARD, fg=FG, relief='flat', width=22, highlightthickness=1,
                 highlightbackground=EDGE, highlightcolor=ACCENT, insertbackground=FG).pack(side='right', ipady=2)
        tk.Label(row, text=T('이 폴더에서 찾기'), font=F['small'], bg=BG, fg=DIM).pack(side='right', padx=6)
        # 목록
        body = tk.Frame(fr, bg=BG)
        body.pack(fill='both', expand=True, padx=(14, 0), pady=(6, 0))
        self.cv = cv = tk.Canvas(body, bg=CARD, highlightthickness=0)
        sb = tk.Scrollbar(body, command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        cv.pack(side='left', fill='both', expand=True)
        cv.bind('<Configure>', lambda e: self.draw())
        # 고른 항목의 전체 경로
        foot = tk.Frame(fr, bg=TOPBG, padx=14, pady=6)
        foot.pack(fill='x', side='bottom')
        self.full = tk.Entry(foot, font=F['small'], bg=TOPBG, fg=FG, relief='flat', readonlybackground=TOPBG, highlightthickness=0)
        self.full.pack(fill='x')
        self.full.configure(state='readonly')
        self.row_h = int(26 * Z)

    def start(self):
        if self.path is None:
            self.go(self.app.cfg.get('files_path') or HOME, push=False)

    def go(self, path, push=True):
        path = os.path.expandvars(os.path.expanduser(path or ''))
        if os.path.isfile(path):  # 파일 경로를 넣으면 그 폴더로 가서 그 파일을 고른다
            self.go(os.path.dirname(path), push)
            self.pick(os.path.abspath(path))
            return
        if not os.path.isdir(path):
            self.app.toast(T('폴더를 찾을 수 없어요 · {0}').format(path), warn=True)
            return
        path = os.path.abspath(path)
        if push and self.path and self.path != path:
            self.hist.append(self.path)
        self.path, self.sel, self.items = path, None, None
        self.app.cfg['files_path'] = path
        self.addr.delete(0, 'end')
        self.addr.insert(0, path)
        self.q.set('')
        self._show_full(path)
        self.status.configure(text=T('읽는 중…'))
        self.draw()
        threading.Thread(target=self._read, args=(path,), daemon=True).start()  # 네트워크 드라이브가 느려도 화면이 멈추지 않게
        self.app.root.after(60, self._drain)

    def _read(self, path):
        try:
            self.results.append((path, list_dir(path), None))
        except OSError as ex:
            self.results.append((path, [], ex.strerror or str(ex)))

    def _drain(self):
        if not self.frame.winfo_exists():
            return
        while self.results:
            path, items, err = self.results.popleft()
            if path != self.path:
                continue
            self.items, self.err = items, err
            self.cv.yview_moveto(0)
            self.draw()
            return
        self.app.root.after(60, self._drain)

    def back(self):
        if self.hist:
            self.go(self.hist.pop(), push=False)

    def up(self):
        if self.path:
            parent = os.path.dirname(self.path.rstrip('\\/')) or self.path
            if parent != self.path and os.path.isdir(parent):
                self.go(parent)

    def shown(self):
        q = self.q.get().strip().lower()
        items = self.items or []
        return [x for x in items if q in x[0].lower()] if q else items

    def draw(self):
        c, F, Z = self.cv, self.app.fonts, self.app.Z
        c.delete('all')
        W = max(200, c.winfo_width())
        if self.items is None:
            return
        items = self.shown()
        n_dir = sum(1 for x in items if x[2])
        txt = T('폴더 {0}개 · 파일 {1}개').format(n_dir, len(items) - n_dir)
        if getattr(self, 'err', None):
            txt = T('열 수 없는 폴더예요 · {0}').format(self.err)
        if len(items) > FILES_MAX:
            txt += '  ' + T('(처음 {0}개만 보여요 · 찾기로 좁혀 주세요)').format(FILES_MAX)
        self.status.configure(text=txt)
        rh = self.row_h
        size_x, date_x = W - int(250 * Z), W - int(150 * Z)
        for i, (name, path, d, size, mt) in enumerate(items[:FILES_MAX]):
            y = i * rh
            on = path == self.sel
            tag = f'r{i}'
            c.create_rectangle(0, y, W, y + rh, fill=HOVER if on else (CARD if i % 2 == 0 else CARD2), outline='', tags=(tag, 'bg'))
            bgc = HOVER if on else (CARD if i % 2 == 0 else CARD2)
            if d:
                draw_icon(c, 'folder', int(20 * Z), y + rh / 2, 7 * Z, ACC_TEXT, bgc, tags=tag)
            else:  # 문서: 모서리 접힌 종이
                x0, y0, x1, y1 = int(15 * Z), y + rh / 2 - 7 * Z, int(25 * Z), y + rh / 2 + 7 * Z
                c.create_polygon(x0, y0, x1 - 3 * Z, y0, x1, y0 + 3 * Z, x1, y1, x0, y1, fill='', outline=DIM, width=max(1, Z), tags=tag)
            label = name if F['body'].measure(name) < size_x - int(50 * Z) else name[:40] + '…' + name[-14:]
            c.create_text(int(36 * Z), y + rh / 2, text=label, anchor='w', font=F['bodyb' if d else 'body'], fill=FG, tags=tag)
            if not d:
                c.create_text(date_x - int(14 * Z), y + rh / 2, text=fmt_bytes(size), anchor='e', font=F['small'], fill=DIM, tags=tag)
            if mt:
                c.create_text(date_x, y + rh / 2, text=time.strftime('%Y-%m-%d %H:%M', time.localtime(mt)), anchor='w',
                              font=F['small'], fill=DIM, tags=tag)
            c.tag_bind(tag, '<Button-1>', lambda e, p=path: self.pick(p))
            c.tag_bind(tag, '<Double-Button-1>', lambda e, p=path, d=d: self.go(p) if d else open_folder(p, select=True))
            c.tag_bind(tag, '<Button-3>', lambda e, p=path: (self.pick(p), self.copy()))
        if not items:
            c.create_text(W / 2, int(40 * Z), text=T('비어 있어요') if not self.q.get() else T('찾는 이름이 없어요'), font=F['body'], fill=DIM)
        c.configure(scrollregion=(0, 0, W, max(len(items[:FILES_MAX]) * rh, 1)))

    def pick(self, path):
        self.sel = path
        self._show_full(path)
        self.draw()

    def _show_full(self, path):
        self.full.configure(state='normal')
        self.full.delete(0, 'end')
        self.full.insert(0, path)
        self.full.configure(state='readonly')

    def copy(self):
        p = self.sel or self.path
        if not p:
            return
        self.frame.clipboard_clear()
        self.frame.clipboard_append(p)
        self.app.toast(T('경로를 복사했어요 · {0}').format(p))


def scroll_frame(parent, bg):
    """세로로 굴리는 프레임 (캔버스 + 안쪽 프레임). (캔버스, 안쪽 프레임)을 돌려준다."""
    body = tk.Frame(parent, bg=bg)
    body.pack(fill='both', expand=True)
    cv = tk.Canvas(body, bg=bg, highlightthickness=0)
    sb = tk.Scrollbar(body, command=cv.yview)
    cv.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y')
    cv.pack(side='left', fill='both', expand=True)
    inner = tk.Frame(cv, bg=bg)
    wid = cv.create_window(0, 0, window=inner, anchor='nw')
    inner.bind('<Configure>', lambda e: cv.configure(scrollregion=cv.bbox('all')))
    cv.bind('<Configure>', lambda e: cv.itemconfig(wid, width=e.width))
    return cv, inner


_RR = [0]


def round_rect(c, x0, y0, x1, y1, r, fill='', outline='', width=1, tags=()):
    """둥근 사각형. 매끈한 다각형(smooth)은 굵은 테두리가 울퉁불퉁하게 나와서 사각형·호·선으로 나눠 그린다.
    여러 도형을 한 묶음 태그로 돌려준다 (tag_lower 등에 그대로 쓸 수 있음)."""
    _RR[0] += 1
    g = f'rr{_RR[0]}'
    t = ((tags,) if isinstance(tags, str) else tuple(tags)) + (g,)
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    d = 2 * r
    corners = ((x0, y0, 90), (x1 - d, y0, 0), (x0, y1 - d, 180), (x1 - d, y1 - d, 270))
    if fill:
        if r:
            c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill, tags=t)
            c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill, tags=t)
            for ax, ay, st in corners:
                c.create_arc(ax, ay, ax + d, ay + d, start=st, extent=90, style='pieslice', fill=fill, outline=fill, tags=t)
        else:
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=fill, tags=t)
    if outline and width:
        kw = dict(fill=outline, width=width, tags=t)
        c.create_line(x0 + r, y0, x1 - r, y0, **kw)
        c.create_line(x0 + r, y1, x1 - r, y1, **kw)
        c.create_line(x0, y0 + r, x0, y1 - r, **kw)
        c.create_line(x1, y0 + r, x1, y1 - r, **kw)
        if r:
            for ax, ay, st in corners:
                c.create_arc(ax, ay, ax + d, ay + d, start=st, extent=90, style='arc', outline=outline, width=width, tags=t)
    if not fill and not outline:
        c.create_rectangle(x0, y0, x0, y0, outline='', tags=t)  # 빈 묶음도 태그는 남긴다
    return g


class AvatarWin:
    """세션 캐릭터의 머리 모양·색·옷·소품을 고른다. 저장하면 설정 파일에 남는다."""
    SWATCH = {'hair': HAIR + ['#0f0f12', '#b0b6c0'], 'skin': SKIN + ['#ffe3cc', '#6b4129'],
              'shirt': SHIRT + ['#1c1f26', '#f2f2f2', '#c9a46a']}

    def __init__(self, app, sid, name):
        self.app, self.sid = app, sid
        self.av = self._from_looks(app.avatars.get(sid))
        self.default = self._from_looks(None)
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('user')
        w.title(T('아바타 꾸미기 · {0}').format(name))
        w.configure(bg=BG)
        w.resizable(False, False)
        w.transient(app.root)
        head = tk.Frame(w, bg=TOPBG, padx=16, pady=10)
        head.pack(fill='x')
        tk.Label(head, text=T('아바타 꾸미기'), font=F['brand'], bg=TOPBG, fg=ACCENT).pack(side='left')
        tk.Label(head, text=name, font=F['body'], bg=TOPBG, fg=FG).pack(side='left', padx=12)
        body = tk.Frame(w, bg=BG, padx=18, pady=14)
        body.pack(fill='both')
        card = tk.Frame(body, bg=CARD, padx=16, pady=14, highlightthickness=1, highlightbackground=EDGE)
        card.pack(side='left', fill='y')
        self.big = tk.Label(card, bg=MINT, bd=0, padx=14, pady=10)
        self.big.pack()
        poses = tk.Frame(card, bg=CARD)
        poses.pack(pady=(12, 0))
        self.small = []
        for pose, label in (('type_a', T('작업 중')), ('raise', T('확인 필요')), ('sleep', T('조는 중'))):
            f = tk.Frame(poses, bg=CARD)
            f.pack(side='left', padx=4)
            l = tk.Label(f, bg=CARD2, bd=0, padx=4, pady=4)
            l.pack()
            tk.Label(f, text=label, font=F['small'], bg=CARD, fg=DIM).pack()
            self.small.append((pose, l))
        opts = tk.Frame(body, bg=BG)
        opts.pack(side='left', fill='both', padx=(18, 0))
        self.widgets = {}
        self._choices(opts, T('머리 모양'), 'style', list(enumerate(HAIR_STYLES)))
        self._swatches(opts, T('머리 색'), 'hair')
        self._swatches(opts, T('피부'), 'skin')
        self._swatches(opts, T('옷 색'), 'shirt')
        self._choices(opts, T('소품'), 'acc', list(ACCS))
        bar = tk.Frame(w, bg=BG, padx=18, pady=(0))
        bar.pack(fill='x', pady=(0, 14))
        app._btn(bar, T('무작위'), self.randomize).pack(side='left')
        app._btn(bar, T('원래 모습'), self.reset).pack(side='left', padx=6)
        app._btn(bar, T('저장'), self.save, bg=ACCENT, fg='#ffffff', hover=ACC_DARK).pack(side='right')
        app._btn(bar, T('취소'), w.destroy).pack(side='right', padx=6)
        w.bind('<Escape>', lambda e: w.destroy())
        self.show()

    def _from_looks(self, av):
        style, cmap, _seed, acc = looks(self.sid, av)
        return {'style': style, 'hair': cmap['H'], 'skin': cmap['S'], 'shirt': cmap['B'], 'acc': acc}

    def _row(self, parent, title):
        tk.Label(parent, text=title, font=self.app.fonts['h2'], bg=BG, fg=ACCENT, anchor='w').pack(fill='x', pady=(8, 4))
        row = tk.Frame(parent, bg=BG)
        row.pack(fill='x')
        return row

    def _choices(self, parent, title, key, items):
        row = self._row(parent, title)
        ws = []
        for i, (val, label) in enumerate(items):
            b = tk.Label(row, text=label, font=self.app.fonts['body'], padx=10, pady=3, cursor='hand2')
            b.grid(row=i // 4, column=i % 4, sticky='ew', padx=(0, 5), pady=(0, 5))
            b.bind('<Button-1>', lambda e, v=val: self.set(key, v))
            ws.append((val, b))
        self.widgets[key] = ws

    def _swatches(self, parent, title, key):
        row = self._row(parent, title)
        ws = []
        s = int(24 * self.app.Z)
        for i, col in enumerate(self.SWATCH[key]):
            b = tk.Frame(row, bg=col, width=s, height=s, highlightthickness=3, cursor='hand2')
            b.grid(row=0, column=i, padx=(0, 4))
            b.bind('<Button-1>', lambda e, v=col: self.set(key, v))
            ws.append((col, b))
        self.widgets[key] = ws

    def set(self, key, val):
        self.av[key] = val
        self.show()

    def randomize(self):
        self.av = {'style': random.randrange(len(HAIR_STYLES)), 'hair': random.choice(self.SWATCH['hair']),
                   'skin': random.choice(self.SWATCH['skin']), 'shirt': random.choice(self.SWATCH['shirt']),
                   'acc': random.choice(ACCS)[0]}
        self.show()

    def reset(self):
        self.av = dict(self.default)
        self.show()

    def show(self):
        frames = make_frames(self.sid, 9 * self.app.Z, self.av)[0]
        small = make_frames(self.sid, 3.4 * self.app.Z, self.av)[0]
        self.imgs = (frames, small)  # PhotoImage는 붙잡아 두어야 보인다
        self.big.configure(image=frames['idle'])
        for pose, l in self.small:
            l.configure(image=small[pose])
        for key, ws in self.widgets.items():
            for val, b in ws:
                on = self.av.get(key) == val
                if isinstance(b, tk.Label):
                    b.configure(bg=ACCENT if on else CARD, fg='#ffffff' if on else FG)
                else:
                    b.configure(highlightbackground=ACCENT if on else BG)

    def save(self):
        self.app.set_avatar(self.sid, None if self.av == self.default else dict(self.av))
        self.win.destroy()


class ChatWin:
    """세션톡: 세션끼리 주고받은 메시지를 카카오톡 대화방처럼 보여 주고, 사용자가 세션에 명령을 보낸다."""
    CHAT_BG, ME, OTHER = CHAT['bg'], '#fee500', CHAT['other']

    def __init__(self, app):
        self.app = app
        self.room = '*'
        self.target = None
        self.faces = app.__dict__.setdefault('chat_faces', {})
        self.limit = TALK_PAGE
        self.read = {}          # 방 → 마지막으로 본 시각
        self.sig = None
        self.room_sig = None
        self.room_ids, self.room_rows = None, {}
        self.md_cache = app.__dict__.setdefault('chat_md', {})  # 글 배치 캐시도 앱에 남긴다
        self.ask_sel, self.ask_sent = {}, {}  # 선택지 질문: 고르는 중인 답, 보낸 답
        self.spin, self.typ_items = 0, {}
        self.dirty = True
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('chat')
        w.title(T('세션톡 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(1040 * Z)}x{int(760 * Z)}')
        w.minsize(720, 480)
        side = tk.Frame(w, bg=CARD, width=int(280 * Z))
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        sh = tk.Frame(side, bg=CARD, padx=16, pady=12)
        sh.pack(fill='x')
        tk.Label(sh, text=T('세션톡'), font=F['brand'], bg=CARD, fg=FG).pack(side='left')
        tk.Label(sh, text=fit(T('세션끼리 주고받은 일'), F['small'], int(280 * Z) - F['brand'].measure(T('세션톡')) - 48), font=F['small'],
                 bg=CARD, fg=DIM).pack(side='left', padx=8, pady=(4, 0))
        tk.Frame(side, bg=EDGE, height=1).pack(fill='x')
        self.rooms_cv, self.rooms = scroll_frame(side, CARD)
        main = tk.Frame(w, bg=self.CHAT_BG)
        main.pack(side='left', fill='both', expand=True)
        hd = tk.Frame(main, bg=CHAT['head'], padx=16, pady=10)
        hd.pack(fill='x')
        self.title = tk.Label(hd, text='', font=F['h2'], bg=CHAT['head'], fg=CHAT['title'], anchor='w')
        self.title.pack(side='left')
        self.sub = tk.Label(hd, text='', font=F['small'], bg=CHAT['head'], fg=CHAT['sub'], anchor='w')
        self.sub.pack(side='left', padx=10, pady=(3, 0))
        # 꺼진 세션 방에서는 바로 다시 켤 수 있게
        self.wake_btn = app._btn(hd, T('이 세션 켜기'), self._wake, bg=ACCENT, fg='#ffffff', hover=ACC_DARK)
        # 입력칸
        box = tk.Frame(main, bg=CHAT['input'])
        box.pack(side='bottom', fill='x')
        top = tk.Frame(box, bg=CHAT['input'], padx=12, pady=6)
        top.pack(fill='x')
        self.to_btn = tk.Label(top, text='', font=F['small'], bg=CHAT['chip'], fg=FG, padx=10, pady=2, cursor='hand2')
        self.to_btn.pack(side='left')
        self.to_btn.bind('<Button-1>', self._pick_target)
        tk.Label(top, text=T('Enter 보내기 · Shift+Enter 줄바꿈'), font=F['small'], bg=CHAT['input'], fg=CHAT['hint']).pack(side='left', padx=10)
        row = tk.Frame(box, bg=CHAT['input'], padx=12)
        row.pack(fill='x', pady=(0, 10))
        self.send_btn = tk.Label(row, text=T('전송'), font=F['bodyb'], bg=self.ME, fg='#3c1e1e', padx=16, pady=8, cursor='hand2')
        self.send_btn.pack(side='right', fill='y', padx=(8, 0))
        self.send_btn.bind('<Button-1>', lambda e: self.send())
        self.inp = tk.Text(row, height=3, wrap='word', font=F['body'], bg=CHAT['input'], fg=CHAT['input_fg'], relief='flat',
                           insertbackground=CHAT['input_fg'], highlightthickness=1, highlightbackground=CHAT['input_edge'],
                           highlightcolor=ACCENT, padx=8, pady=6)
        self.inp.pack(side='left', fill='x', expand=True)
        self.inp.bind('<Return>', self._enter)
        self.inp.bind('<KP_Enter>', self._enter)
        body = tk.Frame(main, bg=self.CHAT_BG)
        body.pack(fill='both', expand=True)
        self.cv = tk.Canvas(body, bg=self.CHAT_BG, highlightthickness=0)
        sb = tk.Scrollbar(body, command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.cv.pack(side='left', fill='both', expand=True)
        self.cv.bind('<Configure>', lambda e: self._mark())
        self.cv.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.rooms_cv.bind('<MouseWheel>', lambda e: self.rooms_cv.yview_scroll(int(-e.delta / 120), 'units'))
        w.protocol('WM_DELETE_WINDOW', w.destroy)
        self.dots = 0
        self.set_room('*')
        self._tick()
        self._spin_tick()

    def _mark(self):
        self.dirty = True

    def set_room(self, room):
        self.room = room
        self.limit = TALK_PAGE
        self.read[room] = time.time()
        if room != '*':
            self.target = room
        self.dirty = True
        self.draw(to_end=True)

    def _wake(self):
        if self.room != '*' and self.room not in self.app.mon.sessions:
            self.app.resume_session(self.room)

    def _enter(self, e):
        if e.state & 1:  # Shift+Enter = 줄바꿈
            return None
        self.send()
        return 'break'

    def _pick_target(self, e):
        mon = self.app.mon
        with mon.lock:
            items = [(s.sid, mon.display_name(s)) for s in self.app.sort_sessions(mon.sessions.values())]
        m = tk.Menu(self.win, tearoff=0, font=self.app.fonts['small'])
        for sid, name in items:
            m.add_command(label=name, command=lambda sid=sid: self._set_target(sid))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _set_target(self, sid):
        self.target = sid
        if self.room != '*' and self.room != sid:
            self.set_room(sid)
        self.dirty = True
        self.draw()

    def send(self):
        text = self.inp.get('1.0', 'end').strip()
        if not text:
            return
        if not self.target or self.target not in self.app.mon.sessions:
            self.to_btn.configure(bg='#ffd2d2')
            self.win.after(700, lambda: self.to_btn.configure(bg=CHAT['chip']))
            return
        try:
            self.app.mon.send_user(self.target, text, talk_text(self.target, text)[len(text):])
        except OSError as ex:
            tkmsg.showerror(T('보내기 실패'), str(ex), parent=self.win)
            return
        self.inp.delete('1.0', 'end')
        self.dirty = True
        self.draw(to_end=True)

    def face(self, sid, size=2):
        av = self.app.avatars.get(sid)
        key = (sid, size, self.app.Z, json.dumps(av, sort_keys=True) if av else '')
        if key not in self.faces:  # 앱에 남겨 두어 세션톡을 다시 열 때 다시 만들지 않는다
            self.faces[key] = make_frames(sid, size * self.app.Z, av)[0]['idle']
        return self.faces[key]

    def more(self):
        """이전 대화를 더 그리고, 보던 자리(원래 맨 위 메시지)가 그대로 보이게 한다."""
        old = self.cv.bbox('all')
        self.limit += TALK_PAGE
        self.dirty = True
        self.draw()
        new = self.cv.bbox('all')
        if old and new and new[3] > 0:
            self.cv.yview_moveto(max(0.0, (new[3] - old[3]) / new[3]))

    # ── 데이터 ──
    def collect(self):
        """(방 메시지들, 방 목록 정보, 세션 이름, 입력 중인 세션)."""
        mon = self.app.mon
        with mon.lock:
            msgs = sorted(mon.chat.values(), key=lambda m: m['ts'])
            live = self.app.sort_sessions(mon.sessions.values())
            names = {s.sid: mon.display_name(s) for s in live}
            qa = {s.sid: [tuple(e) for e in s.qa] for s in live}
            modes = {s.sid: s.mode() for s in live}
        out, typing, used = [], {}, set()
        by_name = {v: k for k, v in names.items()}
        for m in msgs:
            m = dict(m)
            for k in ('from', 'to'):  # 세션 이름으로 보낸 메시지는 그 세션 방으로 (보낼 때 꺼져 있었으면 이름이 남는다)
                if m[k] in by_name:
                    m[k] = by_name[m[k]]
            if m['from'] == 'user':
                m['text'] = strip_hint(m['text'])  # 세션에만 가는 사진 안내는 대화에 보이지 않게
            out.append(m)
            if m['from'] != 'user' or m['to'] not in qa:
                continue
            # 내가 보낸 명령이 그 세션의 요청으로 들어갔는지 대화 기록에서 찾아 '읽음'과 마지막 답변을 붙인다
            want = one_line(m['text'])[:40]
            hit = next((e for e in qa[m['to']] if e[0] >= m['ts'] - 30 and
                        (one_line(e[1]).startswith(want) or want.startswith(one_line(e[1])[:40]))), None)
            if hit:
                used.add((m['to'], hit[0]))
                m['read'] = True
                if hit[3]:
                    out.append({'id': m['id'] + '-a', 'from': m['to'], 'fromLabel': names.get(m['to'], ''), 'to': 'user',
                                'toLabel': T('나'), 'kind': 'answer', 'text': hit[3], 'ts': max(hit[2], m['ts'] + 0.001),
                                'reply': True})

        # 터미널에서 직접 나눈 대화도 세션마다 최근 TALK_HISTORY개 (세션톡에서 보낸 것은 위에서 이미 넣었다)
        for sid, entries in qa.items():
            for e in entries[-TALK_HISTORY:]:
                if (sid, e[0]) in used or not e[1]:
                    continue
                out.append({'id': f'h-{sid[:8]}-{e[0]:.3f}', 'from': 'user', 'to': sid, 'fromLabel': T('나'),
                            'toLabel': names.get(sid, ''), 'kind': 'question', 'text': strip_hint(e[1]), 'ts': e[0], 'read': True, 'cli': True})
                if e[3]:
                    out.append({'id': f'h-{sid[:8]}-{e[0]:.3f}-a', 'from': sid, 'fromLabel': names.get(sid, ''), 'to': 'user',
                                'toLabel': T('나'), 'kind': 'answer', 'text': e[3], 'ts': max(e[2], e[0] + 0.001), 'reply': True,
                                'cli': True})
        self.asks = {s.sid: a for s in live for a in [read_ask(s.sid)] if a}  # 고르라고 묻는 중 (선택지 카드)
        with mon.lock:  # 지금 답을 만드는 중인 세션 (세션톡에서 보냈든 터미널에서 쳤든)
            for s in live:
                st = turn_state(s)
                if st and s.sid not in self.asks:
                    typing[s.sid] = st
        out.sort(key=lambda m: m['ts'])
        return out, live, names, typing

    def in_room(self, m, room):
        if room == '*':
            return True
        return m['from'] == room or m['to'] == room

    # ── 그리기 ──
    def _tick(self):
        if not self.win.winfo_exists():
            return
        if not self.cv.winfo_ismapped():  # 숨겨 둔 동안은 쉰다
            self.win.after(1000, self._tick)
            return
        try:
            self.dots = (self.dots + 1) % 4
            self.draw()
        except Exception as ex:
            print('chat error', ex, file=sys.stderr)
        self.win.after(1000, self._tick)

    def _spin_tick(self):
        """일하는 중 줄만 빠르게 (별 모양이 돌고 시간이 흐른다). 대화 전체는 다시 그리지 않는다."""
        if not self.win.winfo_exists():
            return
        self.spin += 1
        if getattr(self, 'typing', None) and self.cv.winfo_ismapped():
            self._refresh_typing()
        self._blink_rooms()
        self.win.after(160, self._spin_tick)

    def _blink_rooms(self):
        """방 목록: 일을 맡아 하는 중인 세션은 오른쪽 아래 •••이 깜빡인다."""
        typing = getattr(self, 'typing', None) or {}
        on = self.spin // 3 % 2 == 0
        for r, w in self.room_rows.items():
            bz = w.get('busy')
            if not bz:
                continue
            bg = w['bgs'][0]['bg']
            fg = (ACCENT if on else mix(bg, ACCENT, 0.3)) if r in typing else bg
            if bz['bg'] != bg or bz['fg'] != fg:
                bz.configure(bg=bg, fg=fg)

    def _refresh_typing(self):
        mon = self.app.mon
        for sid, (line, tool) in list(getattr(self, 'typ_items', {}).items()):
            with mon.lock:
                s = mon.sessions.get(sid)
                st = turn_state(s) if s else None
            if not st:
                self.dirty = True  # 다 끝났다 → 다음 그리기에서 답변으로 바뀐다
                continue
            try:
                self.cv.itemconfig(line, text=turn_line(st, self.spin))
                if tool and st['tool']:
                    self.cv.itemconfig(tool, text='⎿  ' + fit(st['tool'], self.app.fonts['small'], 520 * self.app.Z))
            except tk.TclError:
                pass

    def draw(self, to_end=False):
        mon = self.app.mon
        with mon.lock:
            sids = list(mon.sessions)
            sig = (mon.chat_ver, tuple((s.sid, s.qa_ver, s.mode(), mon.display_name(s)) for s in mon.sessions.values()),
                   self.room, self.target)
        sig += (tuple((sid, a['id']) for sid in sids for a in [read_ask(sid)] if a), repr(self.ask_sel))
        if sig == self.sig and not self.dirty:
            if getattr(self, 'typing', None):
                self._refresh_typing()
            return
        self.sig, self.dirty = sig, False
        msgs, live, names, typing = self.collect()
        self.typing = typing
        self.names = names
        self.draw_rooms(msgs, live, names)
        room = self.room
        if room != '*' and room not in names and not any(self.in_room(m, room) for m in msgs):
            room = self.room = '*'
        mine = [m for m in msgs if self.in_room(m, room)]
        shown = mine[-self.limit:]
        self.hidden = len(mine) - len(shown)
        if room == '*':
            self.title.configure(text=T('전체 대화'))
            self.sub.configure(text=T('세션 {0}개 · 최근 7일').format(len(live)))
        else:
            self.title.configure(text=names.get(room, room[:8]))
            self.sub.configure(text=T('이 세션과 주고받은 메시지 · 여기서 보낸 글은 내가 직접 친 요청처럼 들어갑니다')
                               if room in names else T('이 세션과 주고받은 메시지 · 지금은 꺼져 있음'))
        if room != '*' and room not in names:
            self.wake_btn.pack(side='right')
        else:
            self.wake_btn.pack_forget()
        tgt = self.target if self.target in names else None
        self.to_btn.configure(text=T('받는 세션: {0} ▾').format(names[tgt]) if tgt else T('받는 세션 고르기 ▾'))
        self.read[room] = time.time()
        self.draw_chat(shown, names, typing if room == '*' else {k: v for k, v in typing.items() if k == room}, to_end)

    def draw_rooms(self, msgs, live, names):
        rooms = ['*'] + [s.sid for s in live]
        for m in msgs:  # 꺼진 세션과 주고받은 기록도 방으로 남긴다
            for sid in (m['from'], m['to']):
                if sid not in rooms and SID_RE.fullmatch(sid):
                    rooms.append(sid)
        pins = [p for p in self.app.cfg.get('chat_pins') or [] if p in rooms]  # 고정한 세션은 전체 대화 바로 아래
        rooms = ['*'] + pins + [r for r in rooms[1:] if r not in pins]
        info = []
        for r in rooms:
            ms = [m for m in msgs if self.in_room(m, r)]
            last = ms[-1] if ms else None
            unread = sum(1 for m in ms if m['ts'] > self.read.get(r, self.app.chat_seen) and m['from'] != 'user')
            label = T('전체 대화') if r == '*' else names.get(r) or next((m['fromLabel'] for m in ms if m['from'] == r), r[:8])
            info.append((r, label, md_plain(last['text'], 40) if last else '', last['ts'] if last else 0,
                         0 if r == self.room else unread))
        sig = (tuple(info), self.room, tuple(pins))
        if sig == self.room_sig:
            return
        self.room_sig = sig
        # 방 목록이 그대로면 줄을 다시 만들지 않고 글자·색만 바꾼다 (다시 만들면 목록이 접혔다 펴지며 튄다)
        ids = [r for r, *_ in info]
        if (ids, tuple(pins)) != self.room_ids:
            for c in self.rooms.winfo_children():
                c.destroy()
            F, Z = self.app.fonts, self.app.Z
            self.room_rows = {}
            for r in ids:
                if pins and r == pins[0]:
                    tk.Label(self.rooms, text='📌  ' + T('고정한 세션'), font=F['tiny'], bg=CARD, fg=PIN_COL, anchor='w',
                             padx=14).pack(fill='x', pady=(8, 2))
                self.room_rows[r] = self._room_row(r)
                if pins and r == pins[-1]:
                    tk.Frame(self.rooms, bg=EDGE, height=1).pack(fill='x', padx=12, pady=(4, 4))
            self.room_ids = (ids, tuple(pins))
        F = self.app.fonts
        for r, label, prev, ts, unread in info:
            w = self.room_rows[r]
            bg = MINT if r == self.room else PIN_BG if r in pins else CARD
            if w.get('bar'):
                w['bar'].configure(bg=PIN_COL if r in pins else bg)
            for wd in w['bgs']:
                wd.configure(bg=bg)
            room_w = int(280 * self.app.Z) - 110
            when = self._when(ts) if ts else ''
            w['when'].configure(text=when)
            if when:
                room_w -= F['small'].measure(when) + 6
            w['name'].configure(text=fit(label, F['bodyb'], room_w), fg=FG if r in names or r == '*' else DIM)
            if w.get('pin'):
                pc = w['pin']
                pc.configure(bg=bg)
                pc.delete('all')
                s = int(pc['width'])
                draw_icon(pc, 'pin', s / 2, s / 2, s * 0.36, PIN_COL if r in pins else mix(EDGE, DIM, 0.35), bg)
            w['prev'].configure(text=fit(prev or T('아직 대화 없음'), F['small'], int(280 * self.app.Z) - 130))
            if unread:
                w['badge'].configure(text=str(unread))
                w['badge'].pack(side='right', before=w['busy'])
            else:
                w['badge'].pack_forget()

    def _room_row(self, r):
        F, Z = self.app.fonts, self.app.Z
        row = tk.Frame(self.rooms, bg=CARD)
        row.pack(fill='x')
        bar = tk.Frame(row, bg=CARD, width=max(3, int(4 * Z)))  # 고정한 세션은 왼쪽 금색 막대
        bar.pack(side='left', fill='y')
        f = tk.Frame(row, bg=CARD, padx=12, pady=8, cursor='hand2')
        f.pack(side='left', fill='x', expand=True)
        if r == '*':
            s = int(38 * Z)
            ic = tk.Canvas(f, width=s, height=s, bg=CARD, highlightthickness=0)
            round_rect(ic, 1, 1, s - 1, s - 1, 11 * Z, fill=ACCENT, outline='')
            draw_icon(ic, 'chat', s / 2, s / 2 + 1, s * 0.3, '#ffffff', ACCENT)
        else:
            ic = tk.Label(f, image=self.face(r), bg=MINT, padx=3, pady=3)
        ic.pack(side='left', padx=(0, 10))
        tx = tk.Frame(f, bg=CARD)
        tx.pack(side='left', fill='x', expand=True)
        top = tk.Frame(tx, bg=CARD)
        top.pack(fill='x')
        pin = None
        if r != '*':  # 누르면 이 세션을 목록 위에 고정 / 풀기
            pin = tk.Canvas(top, width=int(18 * Z), height=int(18 * Z), bg=CARD, highlightthickness=0, cursor='hand2')
            pin.pack(side='right', padx=(6, 0))
            pin.bind('<Button-1>', lambda e, r=r: self.toggle_pin(r))
        when = tk.Label(top, text='', font=F['small'], bg=CARD, fg=DIM)
        when.pack(side='right')
        name = tk.Label(top, text='', font=F['bodyb'], bg=CARD, fg=FG, anchor='w')
        name.pack(side='left')
        bot = tk.Frame(tx, bg=CARD)
        bot.pack(fill='x')
        prev = tk.Label(bot, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w')
        prev.pack(side='left')
        badge = tk.Label(bot, text='', font=F['tiny'], bg=RED, fg='#ffffff', padx=6)
        busy = tk.Label(bot, text='•••', font=F['bodyb'], bg=CARD, fg=CARD)  # 일하는 중이면 깜빡인다
        busy.pack(side='right')
        bgs = [f, tx, top, bot, when, name, prev] + ([ic] if r == '*' else [])
        for wd in bgs + [ic, badge]:
            wd.bind('<Button-1>', lambda e, r=r: self.set_room(r))
            wd.bind('<MouseWheel>', lambda e: self.rooms_cv.yview_scroll(int(-e.delta / 120), 'units'))
        busy.bind('<Button-1>', lambda e, r=r: self.set_room(r))
        return {'bgs': bgs, 'when': when, 'name': name, 'prev': prev, 'badge': badge, 'pin': pin, 'bar': bar, 'busy': busy}

    def toggle_pin(self, sid):
        pins = [p for p in self.app.cfg.get('chat_pins') or [] if p != sid]
        if sid not in (self.app.cfg.get('chat_pins') or []):
            pins.append(sid)
        self.app.cfg['chat_pins'] = pins[-20:]
        self.app.save_cfg()
        self.app.toast(T('대화 상대 목록 위에 고정했어요') if sid in pins else T('고정을 풀었어요'))
        self.room_sig = None
        self.dirty = True  # 바로 다시 그린다
        self.draw()

    @staticmethod
    def _when(ts):
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.date() == datetime.date.today():
            h = dt.hour
            return f'{T("오전") if h < 12 else T("오후")} {h % 12 or 12}:{dt.minute:02d}'
        return dt.strftime(T('%m월 %d일'))

    def draw_chat(self, msgs, names, typing, to_end):
        c, F, Z = self.cv, self.app.fonts, self.app.Z
        at_end = to_end or c.yview()[1] > 0.98
        c.delete('all')
        W = max(400, c.winfo_width())
        pad, gap = 10 * Z, 6 * Z
        maxw = min(560 * Z, W * 0.6)
        y = 16 * Z
        day = None
        prev_from = None
        if getattr(self, 'hidden', 0):  # 오래된 대화는 눌러야 그린다 (열 때 가볍게)
            t = c.create_text(W / 2, y + 10 * Z, text=T('↑ 이전 대화 {0}개 더 보기').format(self.hidden), font=F['bodyb'], fill=ACC_TEXT,
                              tags='more')
            x0, y0, x1, y1 = c.bbox(t)
            c.tag_lower(round_rect(c, x0 - 14 * Z, y0 - 6 * Z, x1 + 14 * Z, y1 + 6 * Z, 12 * Z, fill=CHAT['other'], outline='', tags='more'), t)
            c.tag_bind('more', '<Button-1>', lambda e: self.more())
            c.tag_bind('more', '<Enter>', lambda e: c.configure(cursor='hand2'))
            c.tag_bind('more', '<Leave>', lambda e: c.configure(cursor=''))
            y = y1 + 18 * Z
        if not msgs:
            c.create_text(W / 2, 80 * Z, text=T('아직 주고받은 메시지가 없어요.\n세션끼리 협업 메시지를 보내면 여기에 대화로 쌓입니다.'),
                          justify='center', font=F['body'], fill=CHAT['empty'])
        recent = {id(m) for m in msgs[-TALK_IMG_RECENT:]}  # 최근 약 10턴만 사진을 바로 그린다 (메모리 절약)
        for m in msgs:
            d = datetime.date.fromtimestamp(m['ts'])
            if d != day:
                day = d
                t = c.create_text(W / 2, y + 10 * Z, text=T('{0}년 {1}월 {2}일 {3}요일').format(
                    d.year, d.month, d.day, T('월,화,수,목,금,토,일').split(',')[d.weekday()]),
                                  font=F['small'], fill='#ffffff')
                x0, y0, x1, y1 = c.bbox(t)
                c.tag_lower(round_rect(c, x0 - 12 * Z, y0 - 4 * Z, x1 + 12 * Z, y1 + 4 * Z, 8 * Z, fill=CHAT['pill'], outline=''), t)
                y = y1 + 16 * Z
                prev_from = None
            text = m['text'] if len(m['text']) <= 1200 else m['text'][:1200] + T(' …(이하 생략)')
            show = id(m) in recent
            key = (text, maxw, self.app.Z, show)
            if key not in self.md_cache:
                if len(self.md_cache) > 800:
                    self.md_cache.clear()
                self.md_cache[key] = md_layout(text, maxw, F, self._talk_img if show else None)
            ops, tw, th = self.md_cache[key]
            mine = m['from'] == 'user'
            kind = T('명령') if mine else (T('답변') if m.get('reply') else KIND_TEXT.get(m['kind'], ''))
            if mine:
                head = f'→ {names.get(m["to"], m.get("toLabel") or "")}' if self.room == '*' else ''
                if head:
                    t = c.create_text(W - 16 * Z, y, text=head, anchor='ne', font=F['small'], fill=CHAT['sub'])
                    y = c.bbox(t)[3] + 3 * Z
                bw, bh = tw, th
                bx1 = W - 16 * Z
                bx0 = bx1 - bw - 2 * pad
                round_rect(c, bx0, y, bx1, y + bh + 1.6 * pad, 9 * Z, fill=self.ME, outline='')
                c.create_polygon(bx1 - 6 * Z, y + 6 * Z, bx1 + 7 * Z, y + 4 * Z, bx1 - 2 * Z, y + 15 * Z, fill=self.ME, outline='')
                md_draw(c, ops, bx0 + pad, y + pad * 0.8, F, '#191919', '#7a3e00', '#f2d400', '#0b57b0', '#8a7a00')
                tt = c.create_text(bx0 - 6 * Z, y + bh + 1.6 * pad, text=self._clock(m['ts']), anchor='se', font=F['small'],
                                   fill=CHAT['time'])
                if not m.get('read'):
                    c.create_text(bx0 - 6 * Z, c.bbox(tt)[1] - 1, text='1', anchor='se', font=F['small'], fill='#e0a800')
                y += bh + 1.6 * pad + gap * 2
                prev_from = 'user'
                continue
            sid = m['from']
            show_head = prev_from != (sid, m['to'])
            prev_from = (sid, m['to'])
            ax = 16 * Z
            bx0 = ax + 50 * Z
            if show_head:
                round_rect(c, ax, y, ax + 42 * Z, y + 42 * Z, 12 * Z, fill=MINT, outline='')
                if sid in names or len(sid) > 20:
                    c.create_image(ax + 21 * Z, y + 21 * Z, image=self.face(sid))
                who = names.get(sid, m['fromLabel'])
                to = m['to']
                to_label = T('나') if to == 'user' else T('모두') if to in ('*', 'all', '전체') else names.get(to, m.get('toLabel') or '')
                t = c.create_text(bx0, y, anchor='nw', font=F['bodyb'], fill=CHAT['name'], text=who)
                hx = c.bbox(t)[2] + 6 * Z
                c.create_text(hx, y + 1 * Z, anchor='nw', font=F['small'], fill=CHAT['time'],
                              text=f'→ {to_label}' + (f' · {kind}' if kind else ''))
                y = c.bbox(t)[3] + 4 * Z
            bw, bh = tw, th
            round_rect(c, bx0, y, bx0 + bw + 2 * pad, y + bh + 1.6 * pad, 9 * Z, fill=self.OTHER, outline='')
            if show_head:
                c.create_polygon(bx0 + 6 * Z, y + 6 * Z, bx0 - 7 * Z, y + 4 * Z, bx0 + 2 * Z, y + 15 * Z, fill=self.OTHER, outline='')
            md_draw(c, ops, bx0 + pad, y + pad * 0.8, F, CHAT['text'], CHAT['code'], CHAT['code_bg'], CHAT['link'], CHAT['time'])
            c.create_text(bx0 + bw + 2 * pad + 6 * Z, y + bh + 1.6 * pad, text=self._clock(m['ts']), anchor='sw',
                          font=F['small'], fill=CHAT['time'])
            y += bh + 1.6 * pad + gap * 2
        for sid, ask in (getattr(self, 'asks', {}) or {}).items():
            if self.room in ('*', sid):
                y = self._ask_card(c, y, sid, ask, names) + gap * 2
        self.typ_items = {}
        for sid, st in typing.items():  # 지금 답을 만드는 중 (CLI 아래 줄처럼: 경과 시간 · ↓ 출력 토큰 · 지금 도구)
            ax, bx0 = 16 * Z, 66 * Z
            round_rect(c, ax, y, ax + 42 * Z, y + 42 * Z, 12 * Z, fill=MINT, outline='')
            c.create_image(ax + 21 * Z, y + 21 * Z, image=self.face(sid))
            t = c.create_text(bx0, y, anchor='nw', font=F['bodyb'], fill=CHAT['name'], text=names.get(sid, ''))
            y2 = c.bbox(t)[3] + 4 * Z
            w = max(F['bodyb'].measure(turn_line(dict(st, start=time.time() - 35999, tok=999999))),
                    F['small'].measure('⎿  ' + st['tool']) if st['tool'] else 0)
            line = c.create_text(bx0 + pad, y2 + pad * 0.8, anchor='nw', font=F['bodyb'], fill=CHAT['typing_hl'],
                                 text=turn_line(st, self.spin))
            yb = c.bbox(line)[3]
            tool = None
            if st['tool']:
                tool = c.create_text(bx0 + pad, yb + 3 * Z, anchor='nw', font=F['small'], fill=CHAT['typing'], text='⎿  ' + st['tool'])
                yb = c.bbox(tool)[3]
            c.tag_lower(round_rect(c, bx0, y2, bx0 + w + 2 * pad, yb + pad * 0.8, 9 * Z, fill=self.OTHER, outline=''), line)
            self.typ_items[sid] = (line, tool)
            y = yb + pad + gap * 2
        c.configure(scrollregion=(0, 0, W, y + 10 * Z))
        if at_end:
            c.yview_moveto(1.0)

    def _talk_img(self, path):
        """세션톡 말풍선에 넣을 사진 (폭 360·높이 240 안으로). PNG·GIF가 아니거나 없으면 None."""
        if path.lower().endswith(('.jpg', '.jpeg')):  # 휴대폰에서 올린 사진: 옆에 둔 작은 PNG로 그린다 (Tk는 JPEG를 못 읽음)
            path = os.path.splitext(path)[0] + '-p.png'
        if not path.lower().endswith(('.png', '.gif')) or not os.path.isfile(path):
            return None
        Z = self.app.Z
        return photo_fit(path, int(360 * Z), int(240 * Z), self.app.__dict__.setdefault('talk_imgs', {}))

    def _ask_card(self, c, y, sid, ask, names):
        """Claude가 고르라고 묻는 질문: 선택지를 누르면 그 답으로 이어 간다 (여럿 고르기·직접 입력도)."""
        F, Z = self.app.fonts, self.app.Z
        pad = 12 * Z
        ax, bx0 = 16 * Z, 66 * Z
        cw = min(560 * Z, max(320 * Z, self.cv.winfo_width() * 0.62))
        key = (sid, ask['id'])
        sel = self.ask_sel.setdefault(key, {})
        sent = self.ask_sent.get(key)
        round_rect(c, ax, y, ax + 42 * Z, y + 42 * Z, 12 * Z, fill=MINT, outline='')
        c.create_image(ax + 21 * Z, y + 21 * Z, image=self.face(sid))
        t = c.create_text(bx0, y, anchor='nw', font=F['bodyb'], fill=CHAT['name'], text=names.get(sid, ''))
        c.create_text(c.bbox(t)[2] + 6 * Z, y + 1 * Z, anchor='nw', font=F['small'], fill=CHAT['time'],
                      text=T('→ 나 · 고르면 바로 이어서 진행해요'))
        top = c.bbox(t)[3] + 4 * Z
        first = None
        yy = top + pad
        multi_any = len(ask['questions']) > 1 or any(q['multiSelect'] for q in ask['questions'])
        for qi, q in enumerate(ask['questions']):
            if q['header']:
                h = c.create_text(bx0 + pad + 8 * Z, yy + 2 * Z, anchor='nw', text=q['header'], font=F['tiny'], fill='#ffffff')
                hb = c.bbox(h)
                chip = round_rect(c, hb[0] - 8 * Z, hb[1] - 2 * Z, hb[2] + 8 * Z, hb[3] + 2 * Z, 8 * Z, fill=ACCENT, outline='')
                c.tag_lower(chip, h)
                first = first or chip  # 카드 바탕은 이것보다 아래에
                yy = hb[3] + 6 * Z
            qt = c.create_text(bx0 + pad, yy, anchor='nw', text=q['question'], font=F['bodyb'], fill=CHAT['text'], width=cw - 2 * pad)
            first = first or qt
            yy = c.bbox(qt)[3] + 8 * Z
            picked = sel.get(qi, set())
            free = [x for x in picked if x not in {o['label'] for o in q['options']}]
            for oi, o in enumerate(q['options'] + [{'label': None, 'description': ''}]):
                is_free = o['label'] is None
                on = (bool(free) if is_free else o['label'] in picked)
                mark = ('☑ ' if on else '☐ ') if q['multiSelect'] else ('● ' if on else f'{oi + 1}. ')
                label = (T('✎ 직접 입력…') if not free else '✎ ' + free[0]) if is_free else o['label']
                tag = f'ask-{qi}-{oi}-{id(ask)}'
                lt = c.create_text(bx0 + pad + 12 * Z, yy + 8 * Z, anchor='nw', text=(mark if not is_free else '') + label, font=F['bodyb'],
                                   fill=ACC_TEXT if on else CHAT['text'], width=cw - 2 * pad - 24 * Z, tags=tag)
                yb = c.bbox(lt)[3]
                if o.get('description'):
                    dt = c.create_text(bx0 + pad + 12 * Z, yb + 2 * Z, anchor='nw', text=o['description'], font=F['small'], fill=CHAT['time'],
                                       width=cw - 2 * pad - 24 * Z, tags=tag)
                    yb = c.bbox(dt)[3]
                box = round_rect(c, bx0 + pad, yy, bx0 + cw - pad, yb + 8 * Z, 9 * Z, fill=MINT if on else CHAT['code_bg'],
                                 outline=ACCENT if on else '', width=2 if on else 1, tags=tag)
                c.tag_lower(box, lt)
                if not sent:
                    c.tag_bind(tag, '<Button-1>', lambda e, qi=qi, lab=o['label']: self.pick_ask(sid, ask, qi, lab))
                    c.tag_bind(tag, '<Enter>', lambda e: c.configure(cursor='hand2'))
                    c.tag_bind(tag, '<Leave>', lambda e: c.configure(cursor=''))
                yy = yb + 14 * Z
            yy += 4 * Z
        if sent:
            st = c.create_text(bx0 + pad, yy, anchor='nw', text=T('보냈어요 · {0}').format(sent), font=F['small'], fill=ACC_TEXT,
                               width=cw - 2 * pad)
            yy = c.bbox(st)[3] + 6 * Z
        elif multi_any:
            ready = all(sel.get(qi) for qi in range(len(ask['questions'])))
            bt = c.create_text(bx0 + cw - pad - 10 * Z, yy + 6 * Z, anchor='ne', text=T('답 보내기'), font=F['bodyb'],
                               fill='#ffffff' if ready else CHAT['time'], tags='ask-send')
            b = c.bbox(bt)
            c.tag_lower(round_rect(c, b[0] - 14 * Z, b[1] - 6 * Z, b[2] + 14 * Z, b[3] + 6 * Z, 10 * Z, fill=ACCENT if ready else CHAT['code_bg'],
                                   outline='', tags='ask-send'), bt)
            if ready:
                c.tag_bind('ask-send', '<Button-1>', lambda e: self.send_ask(sid, ask))
            yy = b[3] + 12 * Z
        bg = round_rect(c, bx0, top, bx0 + cw, yy, 9 * Z, fill=self.OTHER, outline=ACCENT, width=2)
        if first:
            c.tag_lower(bg, first)
        return yy

    def pick_ask(self, sid, ask, qi, label):
        q = ask['questions'][qi]
        sel = self.ask_sel.setdefault((sid, ask['id']), {})
        if label is None:  # 직접 입력
            import tkinter.simpledialog as tksd
            text = tksd.askstring(T('직접 입력'), q['question'], parent=self.app.root)
            if not text or not text.strip():
                return
            sel[qi] = {text.strip()}
        elif q['multiSelect']:
            cur = set(sel.get(qi, set()))
            cur ^= {label}
            sel[qi] = cur
        else:
            sel[qi] = {label}
        if len(ask['questions']) == 1 and not q['multiSelect']:
            self.send_ask(sid, ask)
            return
        self.dirty = True
        self.draw()

    def send_ask(self, sid, ask):
        sel = self.ask_sel.get((sid, ask['id']), {})
        answers = {}
        for qi, q in enumerate(ask['questions']):
            labels = [o['label'] for o in q['options'] if o['label'] in sel.get(qi, set())]
            labels += [x for x in sel.get(qi, set()) if x not in labels]
            if labels:
                answers[q['question']] = ', '.join(labels)
        if not answers:
            return
        if not self.app.answer_ask(sid, ask, answers):
            return
        self.ask_sent[(sid, ask['id'])] = ' / '.join(answers.values())
        self.dirty = True
        self.draw(to_end=True)

    def _clock(self, ts):
        dt = datetime.datetime.fromtimestamp(ts)
        h = dt.hour
        return f'{T("오전") if h < 12 else T("오후")} {h % 12 or 12}:{dt.minute:02d}'


class SecretaryWin:
    """비서 세션이 secretary.json에 적은 제안을 카드로 보여 준다. 선택지 버튼 = 그 세션에 바로 요청."""

    def __init__(self, app):
        self.app = app
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('star')
        w.title(T('비서의 제안 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(800 * Z)}x{int(720 * Z)}')
        w.minsize(560, 400)
        head = tk.Canvas(w, height=int(92 * Z), bg=GRAD[0], highlightthickness=0)
        head.pack(fill='x')
        self.head = head
        head.bind('<Configure>', lambda e: self._head())
        self.show_done = tk.BooleanVar(value=False)
        bar = tk.Frame(w, bg=BG, padx=16)
        bar.pack(fill='x', pady=(10, 0))
        self.brief_btn = app._btn(bar, T('브리핑 받기'), app.ask_brief, bg=ACCENT, fg='#ffffff', hover=ACC_DARK)
        self.brief_btn.pack(side='right')
        tk.Checkbutton(bar, text=T('확인한 것도 보기'), variable=self.show_done, command=self.build, font=F['stat'], bg=BG,
                       fg=DIM, selectcolor=CARD, activebackground=BG, activeforeground=FG, bd=0).pack(side='right', padx=10)
        self.note = tk.Label(bar, text='', font=F['small'], bg=BG, fg=DIM, anchor='w', justify='left')
        self.note.pack(side='left', fill='x')
        holder = tk.Frame(w, bg=BG, padx=14)
        holder.pack(fill='both', expand=True, pady=(8, 12))
        self.cv, self.inner = scroll_frame(holder, BG)
        w.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.cv.bind('<Configure>', lambda e: self._wrap(e.width), add='+')
        self.wraps = []
        self.seen = None
        self.sig = None
        self.build()
        self._tick()

    def _head(self):
        c, F, Z = self.head, self.app.fonts, self.app.Z
        c.delete('all')
        W, H = c.winfo_width(), int(c['height'])
        gradient(c, 0, 0, W, H, *GRAD)
        r = 26 * Z
        c.create_oval(18 * Z, H / 2 - r, 18 * Z + 2 * r, H / 2 + r, fill='#ffffff', outline='')
        draw_icon(c, 'star', 18 * Z + r, H / 2, r * 0.62, ACCENT)
        x = 18 * Z + 2 * r + 14 * Z
        c.create_text(x, H / 2 - 12 * Z, text=T('비서의 제안'), anchor='w', font=F['h1'], fill='#ffffff')
        sec = self.app.mon.sec or {}
        text = one_line(sec.get('summary') or T('아직 브리핑이 없어요. [브리핑 받기]를 누르면 비서가 현황을 보고 제안을 적어요.'))
        c.create_text(x, H / 2 + 14 * Z, anchor='w', font=F['small'], fill='#eafff6',
                      text=fit(text, F['small'], W - x - 20 * Z))

    def _wrap(self, width):
        for l in self.wraps:
            l.configure(wraplength=max(200, width - 60))

    def _tick(self):
        if not self.win.winfo_exists():
            return
        if self.app.mon.sec is not self.seen:
            self.build()
        self._note()
        self.win.after(1500, self._tick)

    def _note(self):
        app, mon = self.app, self.app.mon
        with mon.lock:
            s = mon.sessions.get(app.sec_sid) if app.sec_sid else None
            who = mon.display_name(s) if s else ''
            mode = s.mode() if s else ''
        upd = (mon.sec or {}).get('updatedAt')
        when = (datetime.datetime.fromtimestamp(upd / 1000).strftime(T('%m월 %d일 %H:%M'))
                if isinstance(upd, (int, float)) and upd else '')
        if not s:
            txt = T('비서 세션이 꺼져 있어요 · [브리핑 받기]를 누르면 켜거나 새로 만들 수 있어요')
        elif app.briefing():
            txt = T('{0} · 브리핑 중').format(who) + '.' * (int(time.time()) % 3 + 1)
        else:
            txt = f'{who} · ' + (T('{0} 브리핑').format(when) if when else T('아직 브리핑 없음')) + (T(' · 다른 일 하는 중') if mode == 'busy' else '')
        self.note.configure(text=txt)

    def build(self):
        app, F = self.app, self.app.fonts
        sec = self.seen = app.mon.sec
        items = app.sec_items(all_=self.show_done.get())
        keys = tuple(it['_key'] for it in items)
        keep = self.sig == (id(sec), keys)  # 같은 제안 목록이면 보던 자리를 지킨다
        self.sig = (id(sec), keys)
        pos = self.cv.yview()[0]
        for c in self.inner.winfo_children():
            c.destroy()
        self.wraps = []
        self._head()
        if not items:
            tk.Label(self.inner, font=F['body'], bg=BG, fg=DIM, anchor='w', justify='left', pady=20,
                     text=T('보여 줄 제안이 없습니다.\n[브리핑 받기]를 누르면 비서 세션이 다른 세션들을 살펴보고 여기에 제안을 적어요.')
                     ).pack(fill='x')
        for it in items:
            key = it['_key']
            done = key in app.sec_done
            label, col = SEC_KIND.get(it.get('kind'), SEC_KIND['info'])
            card = tk.Frame(self.inner, bg=CARD, padx=16, pady=14, highlightthickness=1, highlightbackground=EDGE)
            card.pack(fill='x', pady=(0, 10))
            top = tk.Frame(card, bg=CARD)
            top.pack(fill='x')
            tk.Label(top, text=label, font=F['tiny'], bg=col, fg='#ffffff', padx=8, pady=1).pack(side='left')
            tk.Label(top, text=one_line(it['title'], 80), font=F['h2'], bg=CARD, fg=DIM if done else FG,
                     anchor='w').pack(side='left', padx=10)
            if it.get('target'):
                tk.Label(top, text='→ ' + one_line(it['target'], 30), font=F['small'], bg=CARD, fg=DIM).pack(side='right')
            if it.get('detail'):
                l = tk.Label(card, text=clean(it['detail'], 800), font=F['body'], bg=CARD, fg=TEXT2, anchor='w',
                             justify='left')
                l.pack(fill='x', pady=(8, 0))
                self.wraps.append(l)
            if it.get('prompt'):
                tk.Label(card, text=T('보낼 지시문'), font=F['small'], bg=CARD, fg=ACCENT, anchor='w').pack(fill='x', pady=(8, 2))
                l = tk.Label(card, text=clean(it['prompt'], 1500), font=F['body'], bg=CARD2, fg=FG, anchor='w',
                             justify='left', padx=10, pady=6)
                l.pack(fill='x')
                self.wraps.append(l)
            bar = tk.Frame(card, bg=CARD)
            bar.pack(fill='x', pady=(10, 0))
            for i, ch in enumerate(app.sec_choices(it)):
                if ch['sid']:
                    b = app._btn(bar, ch['label'], lambda it=it, ch=ch: app.send_choice(it, ch),
                                 bg=ACCENT if i == 0 else CARD, fg='#ffffff' if i == 0 else ACC_TEXT,
                                 hover=ACC_DARK if i == 0 else HOVER)
                    if i:
                        b.configure(highlightbackground=ACCENT)
                    b.pack(side='left', padx=(0, 6))
                else:
                    tk.Label(bar, text=T('{0} · "{1}" 세션이 꺼져 있어요').format(ch["label"], ch["target"]), font=F['small'], bg=CARD,
                             fg=DIM).pack(side='left', padx=(0, 6))
            url = str(it.get('url') or '')
            if url.startswith(('http://', 'https://')):
                app._btn(bar, T('링크 열기'), lambda u=url: webbrowser.open(u)).pack(side='left', padx=(0, 6))
            app._btn(bar, T('다시 보이기') if done else T('확인함'), lambda k=key, d=done: app.sec_mark(k, not d)).pack(side='right')
            self._result(card, app.sec_result(key))
        self._wrap(self.cv.winfo_width())
        self.inner.update_idletasks()
        self.cv.configure(scrollregion=self.cv.bbox('all'))
        self.cv.yview_moveto(pos if keep else 0)

    def _result(self, card, res):
        """비서 제안으로 맡긴 일의 진행 결과 (답변 앞부분 + 세션톡에서 자세히 보기)."""
        if not res:
            return
        app, F = self.app, self.app.fonts
        box = tk.Frame(card, bg=CARD2, padx=12, pady=10)
        box.pack(fill='x', pady=(10, 0))
        top = tk.Frame(box, bg=CARD2)
        top.pack(fill='x')
        label, col = SEC_RESULT_ST.get(res['state'], SEC_RESULT_ST['sent'])
        tk.Label(top, text=T('진행 결과'), font=F['bodyb'], bg=CARD2, fg=FG).pack(side='left')
        tk.Label(top, text=label, font=F['tiny'], bg=col, fg='#ffffff', padx=8, pady=1).pack(side='left', padx=8)
        tk.Label(top, text=f'{res["name"]} · {res["label"]} · ' + datetime.datetime.fromtimestamp(res['ts']).strftime(T('%m월 %d일 %H:%M')),
                 font=F['small'], bg=CARD2, fg=DIM).pack(side='left')
        b = app._btn(top, T('세션톡에서 자세히 보기'), lambda sid=res['sid']: app.open_chat(sid), bg=CARD, fg=ACC_TEXT, hover=HOVER)
        b.configure(highlightbackground=ACCENT)
        b.pack(side='right')
        text = md_plain(res['answer'], 420) if res['answer'] else {
            'sent': T('세션이 아직 이 요청을 받지 않았어요 (하던 일을 마치면 받아요).'), 'working': T('세션이 이 일을 하는 중이에요.'),
            'off': T('맡긴 세션이 꺼져 있어 결과를 볼 수 없어요.'), 'done': ''}[res['state']]
        l = tk.Label(box, text=text, font=F['body'], bg=CARD2, fg=TEXT2, anchor='w', justify='left')
        l.pack(fill='x', pady=(6, 0))
        self.wraps.append(l)


SEC_RESULT_ST = {'sent': (T('요청 보냄'), '#f0a020'), 'working': (T('진행 중'), '#5b9ee8'), 'done': (T('결과 도착'), '#2fb985'),
                 'off': (T('세션 꺼짐'), '#8a96a3')}



def grad_head(parent, app, icon, title, sub, height=88):
    """창 머리: 초록→파랑 그라데이션 위에 흰 동그라미 아이콘, 제목, 설명."""
    Z, F = app.Z, app.fonts
    c = tk.Canvas(parent, height=int(height * Z), bg=GRAD[0], highlightthickness=0)

    def draw(e=None):
        c.delete('all')
        W, H = c.winfo_width(), int(c['height'])
        gradient(c, 0, 0, W, H, *GRAD)
        r = 25 * Z
        c.create_oval(18 * Z, H / 2 - r, 18 * Z + 2 * r, H / 2 + r, fill='#ffffff', outline='')
        draw_icon(c, icon, 18 * Z + r, H / 2, r * 0.6, ACCENT)
        x = 18 * Z + 2 * r + 14 * Z
        c.create_text(x, H / 2 - 11 * Z, text=title, anchor='w', font=F['h1'], fill='#ffffff')
        c.create_text(x, H / 2 + 14 * Z, text=fit(sub, F['small'], W - x - 16 * Z), anchor='w', font=F['small'], fill='#eafff6')

    c.bind('<Configure>', draw)
    return c


class LaunchWin:
    """꺼진 세션을 다시 켜거나 새 세션을 시작한다. 새 콘솔 창(CLI)에 뜨고, 사무실에도 출근한다."""

    def __init__(self, app):
        self.app = app
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('power')
        w.title(T('세션 켜기 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(860 * Z)}x{int(760 * Z)}')
        w.minsize(620, 460)
        grad_head(w, app, 'power', T('세션 켜기'),
                  T('꺼진 세션을 이어서 켜거나 새 세션을 시작해요. 새 터미널 창(CLI)에 뜨고, 사무실에도 바로 출근해요.')).pack(fill='x')
        body = tk.Frame(w, bg=BG, padx=16, pady=12)
        body.pack(fill='both', expand=True)
        # 새 세션
        card = tk.Frame(body, bg=CARD, padx=16, pady=12, highlightthickness=1, highlightbackground=EDGE)
        card.pack(fill='x')
        tk.Label(card, text=T('새 세션 시작'), font=F['h2'], bg=CARD, fg=FG, anchor='w').pack(fill='x')
        row = tk.Frame(card, bg=CARD)
        row.pack(fill='x', pady=(8, 0))
        lw = max(F['small'].measure(T('작업 폴더')), F['small'].measure(T('이름'))) + 12
        tk.Frame(row, bg=CARD, width=lw, height=1).pack(side='left')
        tk.Label(row, text=T('작업 폴더'), font=F['small'], bg=CARD, fg=DIM, anchor='w').place(x=0, rely=0.5, anchor='w')
        self.cwd = tk.Entry(row, font=F['body'], bg=CARD2, fg=FG, relief='flat', highlightthickness=1,
                            highlightbackground=EDGE, highlightcolor=ACCENT, insertbackground=FG)
        self.cwd.insert(0, app.cfg.get('last_cwd') or HOME)
        self.cwd.pack(side='left', fill='x', expand=True, ipady=3)
        app._btn(row, T('폴더 고르기'), self.pick).pack(side='left', padx=(6, 0))
        row = tk.Frame(card, bg=CARD)
        row.pack(fill='x', pady=(6, 0))
        tk.Frame(row, bg=CARD, width=lw, height=1).pack(side='left')
        tk.Label(row, text=T('이름'), font=F['small'], bg=CARD, fg=DIM, anchor='w').place(x=0, rely=0.5, anchor='w')
        self.name = tk.Entry(row, font=F['body'], bg=CARD2, fg=FG, relief='flat', highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=ACCENT, insertbackground=FG)
        self.name.pack(side='left', fill='x', expand=True, ipady=3)
        self.name.bind('<Return>', lambda e: self.start())
        app._btn(row, T('새 세션 시작'), self.start, bg=ACCENT, fg='#ffffff', hover=ACC_DARK).pack(side='left', padx=(6, 0))
        # 꺼진 세션 목록
        top = tk.Frame(body, bg=BG)
        top.pack(fill='x', pady=(14, 6))
        tk.Label(top, text=T('꺼진 세션 다시 켜기'), font=F['h2'], bg=BG, fg=FG).pack(side='left')
        self.count = tk.Label(top, text=T('찾는 중…'), font=F['small'], bg=BG, fg=DIM)
        self.count.pack(side='left', padx=10)
        self.q = tk.StringVar()
        self.q.trace_add('write', lambda *a: self.filter())
        e = tk.Entry(top, textvariable=self.q, font=F['body'], bg=CARD, fg=FG, relief='flat', width=22, highlightthickness=1,
                     highlightbackground=EDGE, highlightcolor=ACCENT, insertbackground=FG)
        e.pack(side='right', ipady=3)
        tk.Label(top, text=T('검색'), font=F['small'], bg=BG, fg=DIM).pack(side='right', padx=6)
        self.cv, self.inner = scroll_frame(body, BG)
        w.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.rows = []
        self.faces = []
        self.items = None
        with app.mon.lock:
            live = set(app.mon.sessions)
        threading.Thread(target=self._load, args=(live,), daemon=True).start()
        self._wait()

    def pick(self):
        d = tkfile.askdirectory(parent=self.win, initialdir=self.cwd.get() or HOME, title=T('새 세션의 작업 폴더'))
        if d:
            self.cwd.delete(0, 'end')
            self.cwd.insert(0, os.path.normpath(d))

    def start(self):
        cwd = self.cwd.get().strip()
        if not os.path.isdir(cwd):
            tkmsg.showerror(T('폴더 없음'), T('폴더가 없습니다:\n{0}').format(cwd), parent=self.win)
            return
        self.app.cfg['last_cwd'] = cwd
        if self.app.new_session(cwd, one_line(self.name.get(), 40)):
            self.win.destroy()

    def _load(self, live):
        try:
            self.items = scan_past(live)
        except Exception as ex:
            print('scan error', ex, file=sys.stderr)
            self.items = []

    def _wait(self):
        if not self.win.winfo_exists():
            return
        if self.items is None:
            self.win.after(150, self._wait)
            return
        self.build()

    def label(self, it):
        names = self.app.mon.names
        return clean(names.get(it['sid']) or it['title'] or it['first'] or os.path.basename(it['cwd'].rstrip('\\/')), 50)

    def build(self):
        app, F, Z = self.app, self.app.fonts, self.app.Z
        for it in self.items:
            f = tk.Frame(self.inner, bg=CARD, padx=14, pady=10, highlightthickness=1, highlightbackground=EDGE)
            img = make_frames(it['sid'], 2.5 * Z, app.avatars.get(it['sid']))[0]['sleep']
            self.faces.append(img)
            tk.Label(f, image=img, bg=MINT, padx=4, pady=4).pack(side='left', padx=(0, 12))
            btn = app._btn(f, T('켜기'), lambda it=it: self.wake(it), bg=ACCENT, fg='#ffffff', hover=ACC_DARK)
            btn.pack(side='right', padx=(10, 0))
            it['btn'] = btn
            tx = tk.Frame(f, bg=CARD)
            tx.pack(side='left', fill='x', expand=True)
            tk.Label(tx, text=self.label(it), font=F['bodyb'], bg=CARD, fg=FG, anchor='w').pack(fill='x')
            when = datetime.datetime.fromtimestamp(it['mtime']).strftime(T('%m월 %d일 %H:%M'))
            tk.Label(tx, text=T('{0} 마지막 활동 · {1} · {2}').format(when, fmt_bytes(it["size"]), it["cwd"]), font=F['small'], bg=CARD, fg=DIM,
                     anchor='w').pack(fill='x')
            if it['last'] or it['first']:
                req = T('최근 요청: ') + it['last'] if it['last'] else T('첫 요청: ') + it['first']
                tk.Label(tx, text=fit(req, F['small'], int(560 * Z)), font=F['small'], bg=CARD,
                         fg=TEXT2, anchor='w').pack(fill='x')
            for wd in [f, tx] + list(tx.winfo_children()):
                wd.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
            it['row'] = f
            it['key'] = ' '.join((self.label(it), it['cwd'], it['first'], it['last'], it['sid'])).lower()
        self.filter()

    def filter(self):
        if not self.items:
            if self.items == []:
                self.count.configure(text=T('최근 30일 안에 꺼진 세션이 없어요'))
            return
        q = self.q.get().strip().lower()
        n = 0
        for it in self.items:
            it['row'].pack_forget()
        for it in self.items:
            if not q or q in it['key']:
                it['row'].pack(fill='x', pady=(0, 8))
                n += 1
        self.count.configure(text=T('최근 30일 · {0}개').format(n))
        self.cv.yview_moveto(0)

    def wake(self, it):
        if self.app.resume_session(it['sid'], it):
            it['btn'].configure(text=T('켜는 중…'), bg=CARD2, fg=DIM)
            it['btn'].unbind('<Button-1>')
            it['btn'].unbind('<Enter>')
            it['btn'].unbind('<Leave>')


class Fold:
    """접고 펼치는 칸. 머리(▸ 제목)를 누르면 몸이 서랍처럼 부드럽게 열리고 닫힌다. 몸(body)은 clip 안에 place로 얹어
    높이를 줄여도 내용이 찌그러지지 않고 잘려 보이게 한다."""

    def __init__(self, app, parent, title, open_=True):
        F, Z = app.fonts, app.Z
        self.app, self.is_open = app, open_
        self.outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=EDGE)
        self.head = tk.Frame(self.outer, bg=CARD, padx=14, pady=10, cursor='hand2')
        self.head.pack(fill='x')
        self.chev = tk.Canvas(self.head, width=int(14 * Z), height=int(14 * Z), bg=CARD, highlightthickness=0, cursor='hand2')
        self.chev.pack(side='left', padx=(0, 8))
        self.title = tk.Label(self.head, text=title, font=F['h2'], bg=CARD, fg=FG, cursor='hand2')
        self.title.pack(side='left')
        self.sub = tk.Label(self.head, text='', font=F['small'], bg=CARD, fg=DIM, cursor='hand2')
        self.sub.pack(side='left', padx=10)
        self.tools = tk.Frame(self.head, bg=CARD)  # 오른쪽 버튼 자리 (눌러도 접히지 않는다)
        self.tools.pack(side='right')
        for w in (self.head, self.chev, self.title, self.sub):
            w.bind('<Button-1>', lambda e: self.toggle())
            w.bind('<Enter>', lambda e: self._chev(self.ang, ACCENT))
            w.bind('<Leave>', lambda e: self._chev(self.ang, DIM))
        self.clip = tk.Frame(self.outer, bg=CARD, height=1)
        self.clip.pack_propagate(False)
        self.body = tk.Frame(self.clip, bg=CARD, padx=16, pady=2)
        self.body.place(x=0, y=0, relwidth=1)
        self.body.bind('<Configure>', lambda e: self._fit())
        self.ang = 90 if open_ else 0
        if open_:
            self.clip.pack(fill='x', pady=(0, 12))
        self._chev(self.ang, DIM)

    def _chev(self, ang, col):
        c, s = self.chev, int(self.chev['width'])
        c.delete('all')
        a = math.radians(ang)
        pts = []
        for px, py in ((-0.25, -0.38), (0.3, 0), (-0.25, 0.38)):  # ▸ 를 ang만큼 돌린다 (90° = ▾)
            pts += [s / 2 + (px * math.cos(a) - py * math.sin(a)) * s, s / 2 + (px * math.sin(a) + py * math.cos(a)) * s]
        c.create_line(*pts, fill=col, width=max(2, s / 7), capstyle='round', joinstyle='round')

    def _fit(self):
        if self.is_open and not self.app.tw.busy(('fold', str(self.outer))):
            h = self.body.winfo_reqheight()
            if abs(int(self.clip['height']) - h) > 1:
                self.clip.configure(height=h)

    def toggle(self, open_=None):
        open_ = (not self.is_open) if open_ is None else open_
        if open_ == self.is_open:
            return
        self.is_open = open_
        self.outer.update_idletasks()
        full = max(1, self.body.winfo_reqheight())
        start = self.clip.winfo_height() if self.clip.winfo_ismapped() else 1
        goal = full if open_ else 1
        a0, a1 = self.ang, (90 if open_ else 0)
        self.clip.configure(height=start)
        self.clip.pack(fill='x', pady=(0, 12))

        def step(e):
            h = max(1, int(start + (goal - start) * e))
            self.clip.configure(height=h)
            self.body.place_configure(y=h - full)  # 내용이 위에서 내려오며 드러난다
            self.ang = a0 + (a1 - a0) * e
            self._chev(self.ang, DIM)

        def done():
            self.body.place_configure(y=0)
            if not self.is_open:
                self.clip.pack_forget()
            else:
                self._fit()

        ms = int((280 if open_ else 220) * min(1.0, 0.5 + full / 900))  # 긴 칸은 조금 더 천천히
        self.app.tw.run(ms, step, done, ease=ease_in_out, key=('fold', str(self.outer)))


MW = {  # 회의실 그림 색
    'wall': _t('#eef6f1', '#1b2420'), 'wains': _t('#dcece3', '#17201c'), 'rail': _t('#ffffff', '#2f4a40'),
    'floor': _t('#eadfce', '#29231d'), 'plank': _t('#dfd1bc', '#231e19'), 'rug': _t('#cfe8dc', '#1f3a31'),
    'rug_edge': _t('#b7dcc9', '#2a4d41'), 'table': _t('#c99a6b', '#7a5b3f'), 'table_hi': _t('#d8ad80', '#8a6a4b'),
    'table_edge': _t('#9a6f48', '#4f3a28'), 'leg': _t('#7d5a3a', '#3d2d20'), 'chair': _t('#5f8a78', '#2f4a40'),
    'chair_hi': _t('#76a08d', '#3d5d50'), 'frame': _t('#3a4450', '#0b0e12'), 'screen': _t('#ffffff', '#1a2129'),
    'board': _t('#ffffff', '#e4e9ec'), 'board_ink': _t('#3b4651', '#2a333c'), 'board_edge': _t('#c9d3da', '#9aa6b0'),
    'sky': _t('#bfe3ff', '#20344d'), 'sky2': _t('#e6f4ff', '#2b4563'), 'spot': _t('#fff5d1', '#3b3622'),
    'paper': _t('#ffffff', '#dfe5ea'), 'cup': _t('#ffffff', '#cfd6dc'), 'pot': _t('#ffffff', '#2a333d'),
    'leaf': _t('#56b98c', '#3f8f6b'), 'leaf2': _t('#3fa577', '#2f7556'), 'plate': _t('#ffffff', '#26303a'),
}
MEET_SEAT = {'busy': ('…', '#5b9ee8'), 'wait': ('?', '#f0a020'), 'ready': ('!', '#2fb985'), 'done': ('✓', '#8a96a3'),
             'off': ('z', '#9a86c8')}


def wrap_text(text, font, px):
    """글자 폭에 맞춰 줄을 나눈다 (띄어쓰기가 있으면 그 자리에서, 없으면 글자 단위)."""
    out = []
    for para in str(text).split('\n'):
        line = ''
        for ch in para:
            if font.measure(line + ch) <= px:
                line += ch
                continue
            cut = line.rfind(' ')
            if cut > len(line) * 0.5:
                out.append(line[:cut])
                line = line[cut + 1:] + ch
            else:
                out.append(line)
                line = ch
        out.append(line)
    return out


# ── 상세 보고: 데이터 정리 (세션이 쓴 JSON은 믿지 않고 모양·크기를 맞춘다) ──
CHART_COLS = ('#2fb985', '#5b9ee8', '#f0a020', '#9a86c8', '#e8505b', '#3cc6c9', '#8a96a3')
TONE_COL = {'good': '#2fb985', 'bad': '#e8505b'}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def norm_focus(v):
    """[x, y, 너비, 높이] (0~1) → 그림 안으로 맞춘 튜플, 이상하면 None."""
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    x, y, w, h = (min(1.0, max(0.0, _num(a))) for a in v)
    w, h = min(w, 1 - x), min(h, 1 - y)
    return (x, y, w, h) if w >= 0.03 and h >= 0.03 and (w < 0.98 or h < 0.98) else None


def report_shots(it):
    """보고 항목의 캡처 → [(파일 이름, 설명, focus)]. images 칸은 문자열 또는 {file, caption, focus}."""
    out = []
    for p in (it.get('images') if isinstance(it.get('images'), list) else [])[:8]:
        if isinstance(p, dict):
            out.append((str(p.get('file') or p.get('path') or ''), one_line(str(p.get('caption') or ''), 120), norm_focus(p.get('focus'))))
        elif p:
            out.append((str(p), '', None))
    return [s for s in out if s[0]]


def norm_chart(c):
    if not isinstance(c, dict):
        return None
    labels = [one_line(str(x), 24) for x in (c.get('labels') if isinstance(c.get('labels'), list) else [])][:12]
    series = []
    for s in (c.get('series') if isinstance(c.get('series'), list) else [])[:4]:
        if isinstance(s, dict) and isinstance(s.get('values'), list):
            vals = [_num(v) for v in s['values'][:len(labels)]]
            vals += [0.0] * (len(labels) - len(vals))
            series.append({'name': one_line(str(s.get('name') or ''), 24), 'values': vals})
    typ = c.get('type') if c.get('type') in ('bar', 'line', 'pie') else 'bar'
    if typ == 'pie':
        series = [{'name': s['name'], 'values': [max(0.0, v) for v in s['values']]} for s in series[:1]]
    if not labels or not series or not any(v for s in series for v in s['values']):
        return None
    return {'title': one_line(str(c.get('title') or ''), 60), 'type': typ, 'unit': one_line(str(c.get('unit') or ''), 12),
            'labels': labels, 'series': series}


def report_extras(rep):
    """(핵심 줄, 핵심 수치, 그래프) — 없으면 빈 목록."""
    hl = [one_line(str(x), 200) for x in (rep.get('highlights') if isinstance(rep.get('highlights'), list) else []) if x][:5]
    ms = []
    for x in (rep.get('metrics') if isinstance(rep.get('metrics'), list) else [])[:6]:
        if isinstance(x, dict) and (x.get('label') or x.get('value')):
            ms.append({'label': one_line(str(x.get('label') or ''), 30), 'value': one_line(str(x.get('value') if x.get('value') is not None else ''), 16),
                       'note': one_line(str(x.get('note') or ''), 40), 'tone': x.get('tone') if x.get('tone') in ('good', 'bad') else 'neutral'})
    ch = [c for c in (norm_chart(x) for x in (rep.get('charts') if isinstance(rep.get('charts'), list) else [])[:4]) if c]
    return hl, ms, ch


# ── 그래프: 한 번 배치(chart_layout)해서 PC 캔버스·HTML(SVG)·휴대폰(SVG)이 같은 그림을 그린다 ──
def _tw(s, size):
    """대략적인 글자 너비 (px). 한중일 글자는 넓게."""
    return sum(size * (1.0 if ord(ch) > 0x2e80 else 0.58) for ch in s)


def _tfit(s, size, w):
    if _tw(s, size) <= w:
        return s
    while s and _tw(s + '…', size) > w:
        s = s[:-1]
    return s + '…' if s else ''


def _nice(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    f = v / e
    return (1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10) * e


def _fmtn(v):
    if abs(v) >= 100 or v == int(v):
        return f'{v:,.0f}'
    return f'{v:.1f}' if abs(v) >= 10 else f'{v:.2f}'.rstrip('0').rstrip('.')


def chart_layout(ch, w, h, fg, dim, grid, s=1.0):
    """그래프를 그릴 조각들: ('rect', x0, y0, x1, y1, 색) · ('line', [x, y, …], 색, 굵기) · ('text', x, y, 글, 크기, 색, start|middle|end, 굵게)
    · ('wedge', cx, cy, r, 시작각, 각도, 색) · ('dot', cx, cy, r, 색). 글자는 y가 가운데."""
    P, pad = [], 12 * s
    y = pad
    title = ch['title'] + (f' ({ch["unit"]})' if ch['unit'] else '')
    if title:
        P.append(('text', pad, y + 8 * s, _tfit(title, 15 * s, w - 2 * pad), 15 * s, fg, 'start', True))
        y += 26 * s
    series, labels = ch['series'], ch['labels']
    if len(series) > 1:
        x = pad
        for i, sr in enumerate(series):
            col = CHART_COLS[i % len(CHART_COLS)]
            P.append(('rect', x, y + 3 * s, x + 10 * s, y + 13 * s, col))
            P.append(('text', x + 14 * s, y + 8 * s, sr['name'], 11 * s, dim, 'start', False))
            x += 30 * s + _tw(sr['name'], 11 * s)
        y += 20 * s
    if ch['type'] == 'pie':
        vals = series[0]['values']
        tot = sum(vals) or 1
        r = max(10, min((h - y - pad) / 2, (w - 2 * pad) * 0.26))
        cx, cy = pad + r + 4 * s, y + (h - y - pad) / 2
        a = 90.0
        lx, ly = cx + r + 24 * s, cy - len(vals) * 10 * s
        for i, v in enumerate(vals):
            col = CHART_COLS[i % len(CHART_COLS)]
            ext = -min(359.99, 360 * v / tot)
            if v > 0:
                P.append(('wedge', cx, cy, r, a, ext, col))
            a += ext
            P.append(('rect', lx, ly + i * 20 * s - 5 * s, lx + 10 * s, ly + i * 20 * s + 5 * s, col))
            P.append(('text', lx + 16 * s, ly + i * 20 * s, _tfit(f'{labels[i]}  {v / tot * 100:.0f}%', 11 * s, w - lx - 20 * s),
                      11 * s, fg, 'start', False))
        return P
    allv = [v for sr in series for v in sr['values']]
    vmax = _nice(max(allv + [0]))
    vmin = -_nice(-min(allv)) if min(allv) < 0 else 0
    left = pad + max(_tw(_fmtn(vmax), 10 * s), _tw(_fmtn(vmin), 10 * s)) + 8 * s
    top, bottom, right = y + 8 * s, h - pad - 18 * s, w - pad
    yv = lambda v: bottom - (v - vmin) / ((vmax - vmin) or 1) * (bottom - top)
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        P.append(('line', [left, yv(v), right, yv(v)], grid, 1))
        P.append(('text', left - 6 * s, yv(v), _fmtn(v), 10 * s, dim, 'end', False))
    n = len(labels)
    slot = (right - left) / n
    for i, lab in enumerate(labels):
        P.append(('text', left + slot * (i + 0.5), bottom + 11 * s, _tfit(lab, 10 * s, slot - 4), 10 * s, dim, 'middle', False))
    if ch['type'] == 'bar':
        bw = slot * 0.7 / len(series)
        show = n * len(series) <= 16 and bw >= 18 * s
        for k, sr in enumerate(series):
            col = CHART_COLS[k % len(CHART_COLS)]
            for i, v in enumerate(sr['values']):
                x0 = left + slot * i + slot * 0.15 + bw * k
                P.append(('rect', x0, min(yv(v), yv(0)), x0 + bw - 2 * s, max(yv(v), yv(0)), col))
                if show:
                    P.append(('text', x0 + bw / 2 - s, yv(v) - 8 * s if v >= 0 else yv(v) + 8 * s, _fmtn(v), 10 * s, fg, 'middle', True))
    else:
        for k, sr in enumerate(series):
            col = CHART_COLS[k % len(CHART_COLS)]
            pts = []
            for i, v in enumerate(sr['values']):
                pts += [left + slot * (i + 0.5), yv(v)]
            if len(pts) >= 4:
                P.append(('line', pts, col, 2.5 * s))
            for i in range(0, len(pts), 2):
                P.append(('dot', pts[i], pts[i + 1], 3.5 * s, col))
            if n <= 8:
                for i, v in enumerate(sr['values']):
                    P.append(('text', left + slot * (i + 0.5), yv(v) - 11 * s, _fmtn(v), 10 * s, col, 'middle', True))
    if vmin < 0:
        P.append(('line', [left, yv(0), right, yv(0)], dim, 1.5))
    return P


def draw_chart(c, x0, y0, prims, family, bg, tags=()):
    """chart_layout 조각을 Tk 캔버스에 그린다."""
    anc = {'start': 'w', 'middle': 'center', 'end': 'e'}
    for p in prims:
        k = p[0]
        if k == 'rect':
            c.create_rectangle(x0 + p[1], y0 + p[2], x0 + p[3], y0 + p[4], fill=p[5], outline='', tags=tags)
        elif k == 'line':
            c.create_line(*[v + (x0 if i % 2 == 0 else y0) for i, v in enumerate(p[1])], fill=p[2], width=p[3], tags=tags)
        elif k == 'text':
            c.create_text(x0 + p[1], y0 + p[2], text=p[3], fill=p[5], anchor=anc[p[6]], tags=tags,
                          font=(family, -max(7, int(p[4])), 'bold' if p[7] else 'normal'))
        elif k == 'wedge':
            cx, cy, r = x0 + p[1], y0 + p[2], p[3]
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=p[4], extent=p[5], fill=p[6], outline=bg, width=2, style='pieslice',
                         tags=tags)
        elif k == 'dot':
            c.create_oval(x0 + p[1] - p[3], y0 + p[2] - p[3], x0 + p[1] + p[3], y0 + p[2] + p[3], fill=p[4], outline=bg, tags=tags)


def chart_svg(prims, w, h, family='Pretendard, Malgun Gothic, sans-serif'):
    """chart_layout 조각 → SVG 글 (회의 자료 HTML용). 글자는 모두 이스케이프한다."""
    import html as H
    out = [f'<svg viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" style="max-width:100%;height:auto" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="{H.escape(family)}">']
    for p in prims:
        k = p[0]
        if k == 'rect':
            out.append(f'<rect x="{p[1]:.1f}" y="{p[2]:.1f}" width="{max(0, p[3] - p[1]):.1f}" height="{max(0, p[4] - p[2]):.1f}" fill="{p[5]}" rx="2"/>')
        elif k == 'line':
            pts = ' '.join(f'{p[1][i]:.1f},{p[1][i + 1]:.1f}' for i in range(0, len(p[1]), 2))
            out.append(f'<polyline points="{pts}" fill="none" stroke="{p[2]}" stroke-width="{p[3]}" stroke-linejoin="round"/>')
        elif k == 'text':
            fw = ' font-weight="700"' if p[7] else ''
            out.append(f'<text x="{p[1]:.1f}" y="{p[2]:.1f}" font-size="{p[4]:.1f}" fill="{p[5]}" text-anchor="{p[6]}" '
                       f'dominant-baseline="middle"{fw}>{H.escape(p[3])}</text>')
        elif k == 'wedge':
            out.append(f'<path d="{wedge_path(*p[1:6])}" fill="{p[6]}" stroke="#ffffff" stroke-width="2"/>')
        elif k == 'dot':
            out.append(f'<circle cx="{p[1]:.1f}" cy="{p[2]:.1f}" r="{p[3]:.1f}" fill="{p[4]}"/>')
    out.append('</svg>')
    return ''.join(out)


def wedge_path(cx, cy, r, a0, ext):
    """Tk 방식 각도(3시 방향에서 반시계, ext<0이면 시계 방향) → SVG 부채꼴 경로."""
    a1 = a0 + ext
    x1, y1 = cx + r * math.cos(math.radians(a0)), cy - r * math.sin(math.radians(a0))
    x2, y2 = cx + r * math.cos(math.radians(a1)), cy - r * math.sin(math.radians(a1))
    return f'M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {1 if abs(ext) > 180 else 0} {1 if ext < 0 else 0} {x2:.1f},{y2:.1f} Z'


def png_size(path):
    """PNG·GIF 그림 크기 (읽지 못하면 None)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(26)
        if head[:8] == b'\x89PNG\r\n\x1a\n':
            return int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')
        if head[:3] == b'GIF':
            return int.from_bytes(head[6:8], 'little'), int.from_bytes(head[8:10], 'little')
    except OSError:
        pass
    return None


def photo_crop(path, focus, width, height, cache):
    """그림의 focus 부분만 잘라 width×height 안에 맞춰 키운 PhotoImage (중요한 부분 확대). 못 읽으면 None."""
    try:
        key = ('crop', path, os.path.getmtime(path), focus, width, height)
    except OSError:
        return None
    if key in cache:
        return cache[key]
    try:
        full = tk.PhotoImage(file=path)
    except tk.TclError:
        return None
    iw, ih = full.width(), full.height()
    x, y, w, h = focus
    x0, y0 = int(x * iw), int(y * ih)
    x1, y1 = max(x0 + 2, int((x + w) * iw)), max(y0 + 2, int((y + h) * ih))
    part = tk.PhotoImage()
    part.tk.call(part, 'copy', full, '-from', x0, y0, x1, y1)
    cache[key] = _photo_scale(part, width, height, up=4)
    return cache[key]


def _photo_scale(img, width, height=None, up=3):
    """정수배 줄임만 되므로 a/b (a=1~up) 중 칸에 들어가는 가장 큰 배율로 맞춘다."""
    iw, ih = img.width(), img.height()
    best = (0, 1, 1)
    for a in range(1, up + 1):
        if a > 1 and iw * ih * a * a > 8_000_000:  # 큰 그림을 키우면 메모리를 많이 잡는다
            break
        b = max(1, -(-iw * a // width), -(-ih * a // height) if height else 1)
        if iw * a / b > best[0] and (a == 1 or b > a or a / b > 1):
            best = (iw * a / b, a, b)
    _w, a, b = best
    if a > 1:
        img = img.zoom(a, a)
    return img.subsample(b, b) if b > 1 else img


def photo_fit(path, width, height=None, cache=None):
    """그림을 width×height 안에 들도록 정수배로 줄이거나(작으면 3배까지) 키운 PhotoImage. 못 읽으면 None."""
    try:
        key = (path, os.path.getmtime(path), width, height)
    except OSError:
        return None
    if cache is not None and key in cache:
        return cache[key]
    try:
        img = tk.PhotoImage(file=path)
    except tk.TclError:
        return None
    # 정수배 줄임만 되므로 a/b (a=1~3) 중 칸에 들어가는 가장 큰 배율을 고른다
    iw, ih = img.width(), img.height()
    best = (0, 1, 1)
    for a in (1, 2, 3):
        if a > 1 and iw * ih * a * a > 8_000_000:  # 큰 캡처를 키우면 메모리를 많이 잡는다
            break
        b = max(1, -(-iw * a // width), -(-ih * a // height) if height else 1)
        if iw * a / b > best[0] and (a == 1 or b > a):
            best = (iw * a / b, a, b)
    _w, a, b = best
    if a > 1:
        img = img.zoom(a, a)
    img = img.subsample(b, b) if b > 1 else img
    if cache is not None:
        cache[key] = img
    return img


STYLE_NOTE = {
    'brief': T('요청별 결과와 캡처만 정리해요. 세션이 쓰는 토큰이 적어요.'),
    'rich': T('핵심 줄·핵심 수치·그래프 데이터를 더 적고, 찍은 캡처를 직접 열어 중요한 부분을 표시해요. '
              '회사 보고처럼 보기 쉽지만 세션이 토큰을 더 써요 (캡처 한 장을 보는 데 수천 토큰).'),
}


def draw_metrics(c, x0, y0, x1, y1, ms, family, s, tags=()):
    """핵심 수치판: 칸마다 이름·값(좋음 초록, 나쁨 빨강)·덧말."""
    n = len(ms)
    if not n:
        return
    cols = 3 if n > 4 else 2 if n > 1 else 1
    rows = -(-n // cols)
    gw, gh = (x1 - x0) / cols, (y1 - y0) / rows
    for i, x in enumerate(ms):
        cx0, cy0 = x0 + (i % cols) * gw, y0 + (i // cols) * gh
        round_rect(c, cx0 + 3, cy0 + 3, cx0 + gw - 3, cy0 + gh - 3, 8 * s, fill=CARD, outline=EDGE, tags=tags)
        col = TONE_COL.get(x['tone'], FG)
        c.create_text(cx0 + 12 * s, cy0 + gh * 0.28, text=_tfit(x['label'], 11 * s, gw - 24 * s), anchor='w', fill=DIM,
                      font=(family, -int(11 * s)), tags=tags)
        c.create_text(cx0 + 12 * s, cy0 + gh * 0.58, text=_tfit(x['value'], 20 * s, gw - 24 * s), anchor='w', fill=col,
                      font=(family, -int(min(22 * s, gh * 0.32)), 'bold'), tags=tags)
        if x['note']:
            c.create_text(cx0 + 12 * s, cy0 + gh * 0.84, text=_tfit(x['note'], 10 * s, gw - 24 * s), anchor='w', fill=col if col != FG else DIM,
                          font=(family, -int(10 * s)), tags=tags)


class ChartWin:
    """그래프 크게 보기 (창 크기에 맞춰 다시 그린다, Esc로 닫기)."""

    def __init__(self, app, ch):
        Z = app.Z
        w = self.win = tk.Toplevel(app.root)
        w.title((ch['title'] or T('그래프')) + ' · Claude Office')
        w.configure(bg=CARD)
        w.transient(app.root)
        w.geometry(f'{int(900 * Z)}x{int(540 * Z)}')
        c = tk.Canvas(w, bg=CARD, highlightthickness=0)
        c.pack(fill='both', expand=True, padx=int(12 * Z), pady=int(12 * Z))
        fam = app.fonts['body'].actual('family')

        def draw(_e=None):
            c.delete('all')
            W, H = max(200, c.winfo_width()), max(150, c.winfo_height())
            draw_chart(c, 0, 0, chart_layout(ch, W, H, FG, DIM, EDGE, s=Z * 1.4), fam, CARD)

        c.bind('<Configure>', draw)
        w.bind('<Escape>', lambda e: w.destroy())
        w.after(30, lambda: (w.lift(), w.focus_force()))


class ImageViewer:
    """캡처 크게 보기: 앱 창 크기의 어두운 창에 그림을 맞춰 보여 준다.
    ← →·[이전]/[다음]으로 같은 묶음의 다른 그림, 더블클릭·[원래 크기]로 100% 보기(끌어서 이동, 휠로 위아래),
    Esc·[닫기]·그림 바깥 클릭으로 닫는다."""
    BG = '#0e1116'

    def __init__(self, app, paths, index=0):
        self.app, self.paths, self.i = app, list(paths), max(0, min(index, len(paths) - 1))
        self.full, self.cache, self.orig, self._after = False, {}, {}, None
        F, Z, root = app.fonts, app.Z, app.root
        w = self.win = tk.Toplevel(root)
        w.title(T('크게 보기'))
        w.configure(bg=self.BG)
        w.transient(root)
        root.update_idletasks()
        w.geometry(f'{max(640, root.winfo_width())}x{max(480, root.winfo_height())}+{root.winfo_rootx()}+{root.winfo_rooty()}')
        bar = tk.Frame(w, bg=self.BG, padx=int(12 * Z), pady=int(8 * Z))
        bar.pack(fill='x')
        bk = dict(bg='#232a33', fg='#ffffff', hover='#323b47')
        self.prev_b = app._btn(bar, '‹ ' + T('이전'), lambda: self.go(-1), **bk)
        self.next_b = app._btn(bar, T('다음') + ' ›', lambda: self.go(1), **bk)
        if len(self.paths) > 1:
            self.prev_b.pack(side='left')
            self.next_b.pack(side='left', padx=(6, 0))
        self.label = tk.Label(bar, text='', font=F['small'], bg=self.BG, fg='#c9d1da', anchor='w')
        self.label.pack(side='left', padx=12, fill='x', expand=True)
        app._btn(bar, T('닫기') + '  ✕', self.close, **bk).pack(side='right')
        app._btn(bar, T('파일 열기'), lambda: os.startfile(self.paths[self.i]), **bk).pack(side='right', padx=6)
        self.size_b = app._btn(bar, '', self.toggle, **bk)
        self.size_b.pack(side='right')
        self.c = c = tk.Canvas(w, bg=self.BG, highlightthickness=0)
        c.pack(fill='both', expand=True)
        for seq, fn in (('<Escape>', lambda e: self.close()), ('<Left>', lambda e: self.go(-1)), ('<Right>', lambda e: self.go(1)),
                        ('<space>', lambda e: self.toggle())):
            w.bind(seq, fn)
        c.bind('<Configure>', lambda e: self.redraw_later())
        c.bind('<Double-Button-1>', lambda e: self.toggle())
        c.bind('<ButtonPress-1>', self.press)
        c.bind('<B1-Motion>', lambda e: self.full and c.scan_dragto(e.x, e.y, gain=1))
        c.bind('<MouseWheel>', lambda e: self.full and c.yview_scroll(int(-e.delta / 120) * 3, 'units'))
        w.protocol('WM_DELETE_WINDOW', self.close)
        w.after(30, lambda: (w.lift(), w.focus_force(), self.draw()))

    def redraw_later(self):
        if self._after:
            self.win.after_cancel(self._after)
        self._after = self.win.after(60, self.draw)

    def draw(self):
        self._after = None
        if not self.win.winfo_exists():
            return
        c, p = self.c, self.paths[self.i]
        W, H = max(50, c.winfo_width()), max(50, c.winfo_height())
        c.delete('all')
        if self.full:
            if p not in self.orig:
                try:
                    self.orig[p] = tk.PhotoImage(file=p)
                except tk.TclError:
                    self.orig[p] = None
            img = self.orig[p]
        else:
            img = photo_fit(p, W - 32, H - 32, self.cache)
        if not img:
            c.create_text(W / 2, H / 2, text=T('그림을 열 수 없어요'), fill='#c9d1da', font=self.app.fonts['body'])
            self.img_box = (0, 0, 0, 0)
        elif self.full:
            iw, ih = img.width(), img.height()
            x, y = max(0, (W - iw) // 2), max(0, (H - ih) // 2)
            c.create_image(x, y, image=img, anchor='nw')
            c.configure(scrollregion=(0, 0, max(W, iw), max(H, ih)))
            self.img_box = (x, y, x + iw, y + ih)
        else:
            c.configure(scrollregion=(0, 0, W, H))
            c.xview_moveto(0)
            c.yview_moveto(0)
            c.create_image(W / 2, H / 2, image=img)
            iw, ih = img.width(), img.height()
            self.img_box = ((W - iw) / 2, (H - ih) / 2, (W + iw) / 2, (H + ih) / 2)
        n = len(self.paths)
        self.label.configure(text=(f'{self.i + 1} / {n}  ·  ' if n > 1 else '') + os.path.basename(p))
        self.size_b.configure(text=T('창에 맞추기') if self.full else T('원래 크기'))
        c.configure(cursor='fleur' if self.full else 'arrow')

    def press(self, e):
        if self.full:
            self.c.scan_mark(e.x, e.y)
            return
        x0, y0, x1, y1 = self.img_box
        if not (x0 <= e.x <= x1 and y0 <= e.y <= y1):  # 그림 바깥을 누르면 닫는다
            self.close()

    def toggle(self):
        self.full = not self.full
        self.draw()

    def go(self, d):
        if len(self.paths) > 1:
            self.i = (self.i + d) % len(self.paths)
            self.full = False
            self.draw()

    def close(self):
        if self.win.winfo_exists():
            self.win.destroy()


class MeetingWin:
    """회의실: 세션을 골라 초대하고 최근 N턴의 작업 결과를 보고받는다. 위쪽은 회의 장면(탁자에 둘러앉은 세션, 발표 스크린,
    대사 창), 아래는 '회의 준비'와 '회의록' 접는 칸. 보고서가 오면 그 세션이 손을 들고, 차례로 일어나 발표하듯 대사가
    타이핑되어 나온다."""

    TYPE_MS = 28  # 한 글자 (초당 약 35자)
    PAUSE = {'.': 260, '!': 260, '?': 260, '。': 260, '！': 260, '？': 260, '\n': 220, ',': 110, '，': 110, '、': 110}

    def __init__(self, app):
        self.app = app
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('meet')
        w.title(T('회의실 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(980 * Z)}x{int(860 * Z)}')
        w.minsize(680, 560)
        self.scene = tk.Canvas(w, height=int(380 * Z), bg=MW['wall'], highlightthickness=0)
        self.scene.pack(fill='x')
        self.scene.bind('<Configure>', lambda e: self.draw_scene())
        for k in ('<space>', '<Return>', '<Right>'):
            self.scene.bind(k, lambda e: self.next())
        self.scene.bind('<Left>', lambda e: self.prev())
        # 문서 크게 보기: 회의 장면을 접고 회의 준비·회의록이 창 전체를 쓴다 (그림·그래프도 크게)
        self.big = bool(app.cfg.get('meet_docs_big'))
        self.docbar = tk.Frame(w, bg=BG, padx=14)
        self.docbar.pack(fill='x', pady=(8, 0))
        self.big_btn = app._btn(self.docbar, '', self.toggle_big, bg=CARD, fg=ACC_TEXT, hover=HOVER)
        self.big_btn.configure(highlightbackground=ACCENT)
        self.big_btn.pack(side='right')
        self.big_note = tk.Label(self.docbar, text='', font=F['small'], bg=BG, fg=DIM, anchor='w')
        self.big_note.pack(side='left')
        holder = tk.Frame(w, bg=BG, padx=14)
        holder.pack(fill='both', expand=True, pady=(8, 12))
        self.cv, self.inner = scroll_frame(holder, BG)
        w.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.cv.bind('<Configure>', lambda e: self._wrap(e.width), add='+')
        self.pick = set(app.cfg.get('meet_pick') or [])
        self.turns = app.cfg.get('meet_turns') if app.cfg.get('meet_turns') in MEET_TURNS else 3
        self.style = app.cfg.get('meet_style') if app.cfg.get('meet_style') in MEET_STYLES else 'brief'
        self.faces, self.thumbs, self.sprites, self.wraps = {}, {}, {}, []
        m = self.meeting()
        self.presented = set((m or {}).get('presented') or [])
        self.f_setup = Fold(app, self.inner, T('회의 준비'), open_=not m)
        self.f_setup.outer.pack(fill='x', pady=(0, 10))
        self.f_min = Fold(app, self.inner, T('회의록'), open_=True)
        self.f_min.outer.pack(fill='x')
        self.setup_sig = self.min_sig = self.scene_sig = None
        self.seated = set()      # 이미 자리에 앉은 세션 (새로 온 세션만 일어서는 연출)
        self.pop_off = {}        # 일어나는 중인 세션의 남은 높이
        self.known = None        # 보고서가 온 세션 (새로 오면 회의록 카드를 반짝)
        self.roster = []
        self.speaker, self.pages, self.pi = None, [], 0
        self.ty = {'text': '', 'n': 0, 'job': None}
        self.ph = 0
        self._apply_big()
        self.refresh()
        self._loop()
        self._anim_tick()

    def toggle_big(self):
        self.big = self.app.cfg['meet_docs_big'] = not self.big
        self.app.save_cfg()
        self._apply_big()
        self.min_sig = self.setup_sig = None
        self.refresh()

    def _apply_big(self):
        if self.big:
            self.scene.pack_forget()
        elif not self.scene.winfo_ismapped():
            self.scene.pack(fill='x', before=self.docbar)
        self.big_btn.configure(text=('▣  ' + T('회의 장면 보기')) if self.big else ('⤢  ' + T('문서 크게 보기')))
        self.big_note.configure(text=T('회의 장면을 접고 문서를 크게 보는 중이에요') if self.big else '')

    # ── 상태 ──
    def meeting(self):
        m = read_json(MEET_FILE)
        return m if isinstance(m, dict) and m.get('id') and isinstance(m.get('members'), list) else None

    @staticmethod
    def folder(m):
        return os.path.join(MEET_DIR, str(m['id']))

    def report(self, m, sid):
        r = read_json(os.path.join(self.folder(m), sid + '.json'))
        return r if isinstance(r, dict) and (r.get('summary') or r.get('items')) else None

    def live(self):
        app = self.app
        with app.mon.lock:
            return [(s.sid, app.mon.display_name(s), s.mode()) for s in app.sessions if s.sid in app.mon.sessions]

    def make_roster(self, m):
        """회의 자리: [{sid, name, mode, report, state}] — state = guest/off/busy/wait/ready/talk/done."""
        app = self.app
        with app.mon.lock:
            alive = {sid: (app.mon.display_name(s), s.mode()) for sid, s in app.mon.sessions.items()}
        order = [sid for sid, _n, _m in self.live()]
        sids = m['members'] if m else [sid for sid in order if sid in self.pick]
        names = (m or {}).get('names') or {}
        out = []
        for sid in sids[:12]:
            name, mode = alive.get(sid, (names.get(sid) or sid[:8], None))
            r = self.report(m, sid) if m else None
            if not m:
                st = 'guest'
            elif r:
                st = 'talk' if sid == self.speaker else 'done' if sid in self.presented else 'ready'
            elif mode is None:
                st = 'off'
            else:
                st = 'busy' if mode == 'busy' else 'wait'
            out.append({'sid': sid, 'name': name, 'mode': mode, 'report': r, 'state': st})
        return out

    def refresh(self):
        if not self.win.winfo_exists():
            return
        m = self.meeting()
        self.roster = self.make_roster(m)
        live = self.live()
        self.pick &= {sid for sid, _n, _m in live}
        sig = (tuple(live), m is not None, self.app.Z)
        if sig != self.setup_sig:
            self.setup_sig = sig
            self.build_setup(m, live)
        else:
            self.f_setup.sub.configure(text=T('{0}명 선택').format(len(self.pick)))
        files = ()
        if m:
            try:
                files = tuple(sorted((e.name, e.stat().st_mtime) for e in os.scandir(self.folder(m))))
            except OSError:
                pass
        mm = {k: v for k, v in (m or {}).items() if k != 'presented'}
        sig = (json.dumps(mm, sort_keys=True), files, tuple((r['sid'], r['name'], r['mode']) for r in self.roster), self.speaker)
        if sig != self.min_sig:
            self.min_sig = sig
            self.build_minutes(m)
        # 보고서가 온 세션이 있는데 발표 중이 아니면 차례로 발표를 시작한다
        if m and self.speaker is None:
            nxt = next((r for r in self.roster if r['state'] == 'ready'), None)
            if nxt:
                self.present(nxt['sid'])
                return
        if self.speaker is None:
            self.say_system(m)
        sig = (tuple((r['sid'], r['state'], r['name']) for r in self.roster), self.speaker)
        if sig != self.scene_sig:
            self.draw_scene()

    def _loop(self):
        if not self.win.winfo_exists():
            return
        self.refresh()
        self.win.after(1200, self._loop)

    # ── 회의 장면 ──
    def sprite(self, sid):
        if sid not in self.sprites:
            self.sprites[sid] = make_frames(sid, 4.4 * self.app.Z, self.app.avatars.get(sid))
        return self.sprites[sid]

    def draw_scene(self):
        c, Z, F = self.scene, self.app.Z, self.app.fonts
        W, H = c.winfo_width(), c.winfo_height()
        if W < 100:
            return
        self.scene_sig = (tuple((r['sid'], r['state'], r['name']) for r in self.roster), self.speaker)
        c.delete('all')
        self.seat_items = {}
        wall_h = int(H * 0.48)
        self.geo = g = {'W': W, 'H': H, 'wall': wall_h}
        # 벽·바닥
        c.create_rectangle(0, 0, W, wall_h, fill=MW['wall'], outline='')
        c.create_rectangle(0, wall_h - 24 * Z, W, wall_h, fill=MW['wains'], outline='')
        c.create_line(0, wall_h - 24 * Z, W, wall_h - 24 * Z, fill=MW['rail'], width=max(2, int(3 * Z)))
        c.create_rectangle(0, wall_h, W, H, fill=MW['floor'], outline='')
        for i in range(1, 6):
            y = wall_h + (H - wall_h) * i / 6
            c.create_line(0, y, W, y, fill=MW['plank'])
        c.create_oval(W * 0.08, wall_h + 2 * Z, W * 0.92, wall_h + (H - wall_h) * 0.66, fill=MW['rug'], outline=MW['rug_edge'],
                      width=max(1, int(2 * Z)))
        # 발표 스크린 (가운데), 안건 화이트보드 (왼쪽), 창문·화분 (오른쪽)
        sw = min(W * 0.36, 360 * Z)
        sx0, sy0, sx1, sy1 = W / 2 - sw / 2, 12 * Z, W / 2 + sw / 2, wall_h - 50 * Z
        c.create_line(W / 2, 0, W / 2, sy0, fill=MW['frame'], width=max(2, int(2 * Z)))
        round_rect(c, sx0 - 6 * Z, sy0 - 6 * Z, sx1 + 6 * Z, sy1 + 6 * Z, 6 * Z, fill=MW['frame'])
        c.create_rectangle(sx0, sy0, sx1, sy1, fill=MW['screen'], outline='')
        g['scr'] = (sx0, sy0, sx1, sy1)
        side = (W - sw) / 2 - 56 * Z
        if side > 120 * Z:
            bw = min(200 * Z, side)
            bx0, by0, bx1, by1 = 34 * Z, 22 * Z, 34 * Z + bw, wall_h - 46 * Z
            round_rect(c, bx0 - 4 * Z, by0 - 4 * Z, bx1 + 4 * Z, by1 + 4 * Z, 5 * Z, fill=MW['board_edge'])
            c.create_rectangle(bx0, by0, bx1, by1, fill=MW['board'], outline='')
            c.create_rectangle(bx0 + 10 * Z, by1 + 4 * Z, bx1 - 10 * Z, by1 + 8 * Z, fill=MW['board_edge'], outline='')
            g['board'] = (bx0, by0, bx1, by1)
            wx1 = W - 34 * Z
            wx0 = wx1 - min(170 * Z, side)
            round_rect(c, wx0 - 5 * Z, 20 * Z, wx1 + 5 * Z, wall_h - 40 * Z, 4 * Z, fill=MW['rail'])
            c.create_rectangle(wx0, 25 * Z, wx1, wall_h - 45 * Z, fill=MW['sky'], outline='')
            c.create_rectangle(wx0, 25 * Z, wx1, (25 + 22) * Z, fill=MW['sky2'], outline='')
            c.create_line((wx0 + wx1) / 2, 25 * Z, (wx0 + wx1) / 2, wall_h - 45 * Z, fill=MW['rail'], width=max(2, int(4 * Z)))
            px = wx1 - 14 * Z  # 화분
            for dx, dy, r, col in ((-10, -40, 16, 'leaf2'), (8, -46, 15, 'leaf'), (0, -58, 13, 'leaf')):
                c.create_oval(px + (dx - r) * Z, wall_h + (dy - r) * Z, px + (dx + r) * Z, wall_h + (dy + r) * Z, fill=MW[col], outline='')
            round_rect(c, px - 16 * Z, wall_h - 30 * Z, px + 16 * Z, wall_h + 6 * Z, 5 * Z, fill=MW['pot'], outline=EDGE)
        # 탁자와 자리
        n = len(self.roster)
        seat_w = min(126 * Z, (W * 0.84 - 60 * Z) / max(1, n))
        tw = max(n * seat_w + 60 * Z, W * 0.42)
        tx0, tx1 = (W - tw) / 2, (W + tw) / 2
        ty0 = wall_h + 30 * Z
        ty1 = ty0 + 32 * Z
        g['seats'] = {}
        sc = max(1, int(round(4.4 * Z * 16 / SW)))
        for i, r in enumerate(self.roster):
            x = (W - n * seat_w) / 2 + seat_w * (i + 0.5)
            g['seats'][r['sid']] = x
            if r['state'] == 'talk':  # 발표자 뒤 조명
                c.create_oval(x - 56 * Z, ty0 - 96 * Z, x + 56 * Z, ty0 + 6 * Z, fill=MW['spot'], outline='')
            round_rect(c, x - 32 * Z, ty0 - 72 * Z, x + 32 * Z, ty0 + 4 * Z, 12 * Z, fill=MW['chair'])
            c.create_rectangle(x - 22 * Z, ty0 - 64 * Z, x + 22 * Z, ty0 - 60 * Z, fill=MW['chair_hi'], outline='')
            frames = self.sprite(r['sid'])[0]
            pose = self.pose(r, 0)
            tag = f's{i}'
            img = c.create_image(x, ty0 + 16 * Z, image=frames[pose], anchor='s', tags=(tag, 'pop' + r['sid']))
            top = ty0 + 16 * Z - SH * sc
            b = MEET_SEAT.get(r['state'])
            bub = txt = None
            if b:
                bub = c.create_oval(x + 10 * Z, top - 6 * Z, x + 30 * Z, top + 14 * Z, fill=b[1], outline='#ffffff',
                                    width=max(1, int(1.5 * Z)), tags=(tag + 'b', 'pop' + r['sid']))
                txt = c.create_text(x + 20 * Z, top + 4 * Z, text=b[0], font=F['tiny'], fill='#ffffff', tags=(tag + 'b', 'pop' + r['sid']))
            self.seat_items[r['sid']] = {'img': img, 'pose': pose, 'bub': bub, 'txt': txt, 'bob': 0, 'r': r}
            for it in (img,):
                c.tag_bind(it, '<Button-1>', lambda e, s=r['sid']: self.present(s, user=True))
        # 탁자 (사람 아랫몸을 가린다)
        round_rect(c, tx0, ty0, tx1, ty1, 12 * Z, fill=MW['table'], outline=MW['table_edge'], width=max(1, int(Z)))
        c.create_line(tx0 + 14 * Z, ty0 + 6 * Z, tx1 - 14 * Z, ty0 + 6 * Z, fill=MW['table_hi'], width=max(1, int(2 * Z)))
        c.create_rectangle(tx0 + 8 * Z, ty1, tx1 - 8 * Z, ty1 + 10 * Z, fill=MW['table_edge'], outline='')
        for lx in (tx0 + 26 * Z, tx1 - 26 * Z):
            c.create_rectangle(lx - 4 * Z, ty1 + 10 * Z, lx + 4 * Z, ty1 + 26 * Z, fill=MW['leg'], outline='')
        for r in self.roster:
            x = g['seats'][r['sid']]
            c.create_rectangle(x - 22 * Z, ty0 + 9 * Z, x - 4 * Z, ty0 + 22 * Z, fill=MW['paper'], outline=EDGE)
            for k in range(3):
                c.create_line(x - 19 * Z, ty0 + (12 + k * 3) * Z, x - 8 * Z, ty0 + (12 + k * 3) * Z, fill=EDGE)
            c.create_oval(x + 8 * Z, ty0 + 10 * Z, x + 18 * Z, ty0 + 20 * Z, fill=MW['cup'], outline=EDGE)
            pw = min(seat_w - 12 * Z, 104 * Z)
            on = r['state'] == 'talk'
            round_rect(c, x - pw / 2, ty1 - 3 * Z, x + pw / 2, ty1 + 15 * Z, 4 * Z, fill=ACCENT if on else MW['plate'],
                       outline=ACCENT if on else EDGE)
            c.create_text(x, ty1 + 6 * Z, text=fit(r['name'], F['tiny'], pw - 8 * Z), font=F['tiny'],
                          fill='#ffffff' if on else FG)
        if not self.roster:
            c.create_text(W / 2, ty0 + 16 * Z, text=T('빈 회의실'), font=F['bodyb'], fill=MW['table_edge'])
        # 새로 온 세션은 탁자 뒤에서 일어나 앉는다. 올라오는 도중에 다시 그려도 지금 높이(pop_off)에서 이어진다
        new = [r['sid'] for r in self.roster if r['sid'] not in self.seated]
        for k, sid in enumerate(new):
            self.pop_off[sid] = 34 * Z
            self._pop(sid, 34 * Z, k * 90)
        for sid, off in self.pop_off.items():
            if off:
                c.move('pop' + sid, 0, off)
        self.seated = {r['sid'] for r in self.roster}
        self.draw_board()
        self.draw_dialog()
        self.draw_screen(wipe=False)

    def _pop(self, sid, dy, delay):
        def step(e):
            off = dy * (1 - e)
            self.scene.move('pop' + sid, 0, off - self.pop_off.get(sid, 0))
            self.pop_off[sid] = off

        self.win.after(delay, lambda: self.app.tw.run(420, step, ease=ease_back, key=('pop', sid)))

    def pose(self, r, ph):
        st = r['state']
        if st == 'off':
            return 'sleep'
        if st == 'busy':
            return 'type_a' if (ph // 3) % 2 else 'type_b'
        if st == 'ready':
            return 'raise'
        seed = sum(map(ord, r['sid'][:6]))
        if (ph + seed) % 41 == 0:
            return 'blink'
        return 'breath' if ((ph + seed) // 12) % 2 else 'idle'

    def draw_board(self):
        g = self.geo
        if 'board' not in g:
            return
        c, Z, F = self.scene, self.app.Z, self.app.fonts
        x0, y0, x1, y1 = g['board']
        m = self.meeting()
        got = sum(1 for r in self.roster if r['report'])
        lines = [T('최근 {0}턴 업무 보고').format(m.get('turns', self.turns) if m else self.turns),
                 T('참석 {0}명').format(len(self.roster)),
                 (T('보고 {0}/{1} 도착').format(got, len(self.roster)) if m else T('회의 전'))]
        c.create_text(x0 + 12 * Z, y0 + 10 * Z, text=T('오늘의 안건'), anchor='nw', font=F['c_wbt'], fill=MW['board_ink'])
        c.create_line(x0 + 12 * Z, y0 + 30 * Z, x0 + 12 * Z + min(x1 - x0 - 24 * Z, 90 * Z), y0 + 30 * Z, fill=ACCENT,
                      width=max(2, int(2 * Z)))
        for i, t in enumerate(lines):
            y = y0 + (40 + i * 17) * Z
            if y > y1 - 12 * Z:
                break
            c.create_text(x0 + 12 * Z, y, text='• ' + fit(t, F['c_wb'], x1 - x0 - 30 * Z), anchor='nw', font=F['c_wb'],
                          fill=MW['board_ink'])

    def draw_screen(self, wipe=True):
        """발표 스크린: 지금 대사에 캡처가 있으면 캡처, 없으면 제목 카드. 바뀔 땐 왼쪽에서 오른쪽으로 걷히며 나타난다."""
        g = self.geo if hasattr(self, 'geo') else None
        if not g:
            return
        c, Z, F = self.scene, self.app.Z, self.app.fonts
        c.delete('scr')
        x0, y0, x1, y1 = g['scr']
        page = self.pages[self.pi] if self.pages else {}
        imgs = page.get('images') or []
        vis = page.get('visual')
        img = self.thumb(imgs[0], int(x1 - x0 - 12 * Z), int(y1 - y0 - 12 * Z)) if imgs and not vis else None
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hand = lambda: (c.tag_bind('scr', '<Enter>', lambda e: c.configure(cursor='hand2')),
                        c.tag_bind('scr', '<Leave>', lambda e: c.configure(cursor='')))
        if vis and vis[0] == 'chart':
            fam = F['body'].actual('family')
            draw_chart(c, x0 + 6 * Z, y0 + 4 * Z, chart_layout(vis[1], x1 - x0 - 12 * Z, y1 - y0 - 8 * Z, FG, DIM, EDGE, s=Z * 0.78),
                       fam, MW['screen'], tags='scr')
            c.tag_bind('scr', '<Button-1>', lambda e, ch=vis[1]: ChartWin(self.app, ch))
            hand()
        elif vis and vis[0] == 'metrics':
            draw_metrics(c, x0 + 8 * Z, y0 + 8 * Z, x1 - 8 * Z, y1 - 8 * Z, vis[1], F['body'].actual('family'), Z * 0.85, tags='scr')
        elif vis and vis[0] == 'crop':
            crop = photo_crop(vis[1], vis[2], int(x1 - x0 - 12 * Z), int(y1 - y0 - 12 * Z), self.thumbs)
            if crop:
                c.create_image(cx, cy, image=crop, tags='scr')
            c.create_text(x0 + 6 * Z, y1 - 4 * Z, text='🔍 ' + T('확대'), anchor='sw', font=F['tiny'], fill=ACCENT, tags='scr')
            c.tag_bind('scr', '<Button-1>', lambda e, p=vis[1]: ImageViewer(self.app, [p], 0))
            hand()
        elif img:
            c.create_image(cx, cy, image=img, tags='scr')
            if len(imgs) > 1:
                c.create_text(x1 - 6 * Z, y1 - 4 * Z, text=f'+{len(imgs) - 1}', anchor='se', font=F['tiny'], fill=ACCENT, tags='scr')
            c.create_text(x0 + 6 * Z, y1 - 4 * Z, text='⤢ ' + T('크게 보기'), anchor='sw', font=F['tiny'], fill=ACCENT, tags='scr')
            c.tag_bind('scr', '<Button-1>', lambda e, ps=list(imgs): ImageViewer(self.app, ps, 0))
            c.tag_bind('scr', '<Enter>', lambda e: c.configure(cursor='hand2'))
            c.tag_bind('scr', '<Leave>', lambda e: c.configure(cursor=''))
        else:
            r = next((x for x in self.roster if x['sid'] == self.speaker), None)
            if r and r['report']:
                rep = r['report']
                its = [it for it in rep.get('items') or [] if isinstance(it, dict)]
                cnt = collections.Counter(it.get('status') for it in its)
                c.create_text(cx, cy - 14 * Z, text=fit(one_line(rep.get('title') or r['name']), F['h2'], x1 - x0 - 24 * Z),
                              font=F['h2'], fill=FG, tags='scr')
                stat = ' · '.join(f'{REPORT_ST[k][0]} {cnt[k]}' for k in ('done', 'partial', 'todo') if cnt.get(k))
                c.create_text(cx, cy + 12 * Z, text=stat or r['name'], font=F['small'], fill=DIM, tags='scr')
            else:
                draw_icon(c, 'meet', cx, cy - 12 * Z, 16 * Z, ACCENT, MW['screen'], tags='scr')
                c.create_text(cx, cy + 18 * Z, text=T('회의실'), font=F['h2'], fill=FG, tags='scr')
        if wipe:  # 막 바뀐 화면은 덮개가 걷히듯
            cover = c.create_rectangle(x0, y0, x1, y1, fill=MW['screen'], outline='', tags='scr')
            self.app.tw.run(300, lambda e: c.coords(cover, x0 + (x1 - x0) * e, y0, x1, y1), ease=ease_in_out, key='scr')

    def thumb(self, path, width, height=None):
        return photo_fit(path, width, height, self.thumbs)

    # ── 대사 창 ──
    def draw_dialog(self):
        g = self.geo
        c, Z, F = self.scene, self.app.Z, self.app.fonts
        W, H = g['W'], g['H']
        x0, x1, y1 = 18 * Z, W - 18 * Z, H - 12 * Z
        y0 = y1 - 84 * Z
        g['dlg'] = (x0, y0, x1, y1)
        c.delete('dlg')
        round_rect(c, x0 + 3 * Z, y0 + 4 * Z, x1 + 3 * Z, y1 + 4 * Z, 14 * Z, fill=mix(MW['floor'], '#000000', 0.12), tags='dlg')
        round_rect(c, x0, y0, x1, y1, 14 * Z, fill=SURF, outline=ACCENT, width=max(2, int(2 * Z)), tags='dlg')
        r = next((x for x in self.roster if x['sid'] == self.speaker), None)
        name = r['name'] if r else T('회의 진행')
        nw = F['bodyb'].measure(name) + 28 * Z
        round_rect(c, x0 + 18 * Z, y0 - 13 * Z, x0 + 18 * Z + nw, y0 + 13 * Z, 13 * Z, fill=ACCENT if r else BLUE, tags='dlg')
        c.create_text(x0 + 18 * Z + nw / 2, y0, text=name, font=F['bodyb'], fill='#ffffff', tags='dlg')
        self.t_id = c.create_text(x0 + 22 * Z, y0 + 20 * Z, text=self.ty['text'][:self.ty['n']], anchor='nw', font=F['body'],
                                  fill=FG, width=x1 - x0 - 110 * Z, tags='dlg')
        npg = len(self.pages)
        self.pg_id = c.create_text(x1 - 16 * Z, y1 - 10 * Z, text=f'{self.pi + 1}/{npg}' if npg > 1 else '', anchor='se',
                                   font=F['small'], fill=DIM, tags='dlg')
        self.more_id = c.create_text(x1 - 22 * Z, y0 + 22 * Z, text='▼', anchor='ne', font=F['small'], fill=ACCENT,
                                     state='hidden', tags='dlg')
        for k, (tx, sym) in enumerate(((x1 - 78 * Z, '◀'), (x1 - 50 * Z, '▶'))):
            cy = y1 - 30 * Z
            tag = ('prev', 'next')[k]
            c.create_oval(tx - 11 * Z, cy - 11 * Z, tx + 11 * Z, cy + 11 * Z, fill=CARD2, outline=EDGE, tags=('dlg', tag))
            c.create_text(tx, cy, text=sym, font=F['tiny'], fill=ACC_TEXT, tags=('dlg', tag))
            c.tag_bind(tag, '<Button-1>', lambda e, k=k: (self.scene.focus_set(), self.prev() if k == 0 else self.next()))
        for it in c.find_withtag('dlg'):
            tags = c.gettags(it)
            if 'prev' not in tags and 'next' not in tags:
                c.tag_bind(it, '<Button-1>', lambda e: (self.scene.focus_set(), self.next()))
        self.text_px = x1 - x0 - 110 * Z
        if self.typing() and not self.ty['job']:  # 창이 그려지기 전에 시작한 대사는 여기서 이어 친다
            self.ty['job'] = self.win.after(max(1, int(self.TYPE_MS * self.app.type_scale())), self._type)

    def say_system(self, m):
        got = sum(1 for r in self.roster if r['report'])
        if not m:
            text = (T('{0}명을 초대했어요. 보고 범위를 고르고 [회의 시작]을 눌러 주세요.').format(len(self.roster)) if self.roster
                    else T('어서 오세요! 아래 [회의 준비]에서 회의에 부를 세션을 골라 주세요.'))
        elif got and got == len(self.roster) and all(r['state'] == 'done' for r in self.roster):
            text = T('모든 보고가 끝났어요. 회의록에서 [자료로 저장]을 누르면 보고서 한 장으로 남겨요.')
        elif got:
            text = T('들은 보고를 다시 보려면 자리의 세션을 누르세요. 나머지 보고를 기다리는 중이에요… ({0}/{1})').format(got, len(self.roster))
        else:
            text = T('보고를 요청했어요. 세션들이 결과를 정리하고 화면을 캡처하는 중이에요… ({0}/{1})').format(got, len(self.roster))
        if self.speaker is None and (not self.pages or self.pages[0]['text'] != text):
            self.pages, self.pi = [{'text': text, 'images': []}], 0
            self.draw_dialog() if hasattr(self, 'geo') else None
            self.type_out(text)
            self.draw_screen()

    def build_pages(self, r):
        """발표 장들: 대사(세 줄씩) + 스크린에 띄울 것(캡처, 또는 visual = 핵심 수치판·그래프·캡처의 확대 부분)."""
        rep, F = r['report'], self.app.fonts
        segs = []
        head = '\n'.join(x for x in (one_line(rep.get('title') or ''), clean(rep.get('summary') or '', 900)) if x)
        if head:
            segs.append((head, [], None))
        hl, ms, chs = report_extras(rep)
        if hl or ms:
            text = '\n'.join('• ' + x for x in hl) if hl else '\n'.join(f'{x["label"]} {x["value"]}' for x in ms)
            segs.append((T('핵심') + '\n' + text, [], ('metrics', ms) if ms else None))
        for ch in chs:
            segs.append((T('그래프 · {0}').format(ch['title'] or T('수치')), [], ('chart', ch)))
        m = self.meeting()
        for it in rep.get('items') or []:
            if not isinstance(it, dict):
                continue
            st = REPORT_ST.get(it.get('status'), REPORT_ST['done'])[0]
            text = f'[{st}] ' + one_line(it.get('request') or '', 160)
            if it.get('result'):
                text += '\n' + clean(it['result'], 1200)
            shots = self.shots(m, it) if m else []
            segs.append((text, [p for p, _c, _f in shots], None))
            for p, cap, foc in shots:  # 중요한 부분은 확대해서 한 장 더
                if foc:
                    segs.append(('🔍 ' + (cap or T('중요한 부분을 확대했어요')), [p], ('crop', p, foc)))
                elif cap:
                    segs.append(('🖼 ' + cap, [p], None))
        if rep.get('next'):
            segs.append((T('다음 할 일 · ') + clean(rep['next'], 600), [], None))
        px = getattr(self, 'text_px', 600)
        pages = []
        for text, imgs, vis in segs:
            lines = wrap_text(text, F['body'], px)
            for k in range(0, len(lines), 3):  # 대사 창 한 번에 세 줄
                pages.append({'text': '\n'.join(lines[k:k + 3]), 'images': imgs, 'visual': vis})
        return pages or [{'text': T('(빈 보고서)'), 'images': []}]

    def present(self, sid, user=False):
        r = next((x for x in self.roster if x['sid'] == sid), None)
        if not r or not r['report']:
            if user and r:
                self.app.toast(T('{0}의 보고서가 아직 없어요').format(r['name']))
            return
        self.speaker = sid
        self.roster = self.make_roster(self.meeting())
        self.draw_scene()
        self.pages, self.pi = self.build_pages(r), 0
        self.show_page()

    def show_page(self):
        page = self.pages[self.pi]
        if hasattr(self, 'pg_id'):
            self.scene.itemconfig(self.pg_id, text=f'{self.pi + 1}/{len(self.pages)}' if len(self.pages) > 1 else '')
        self.type_out(page['text'])
        prev = self.pages[self.pi - 1] if self.pi else {}
        self.draw_screen(wipe=(page['images'], page.get('visual')) != (prev.get('images'), prev.get('visual')))

    def type_out(self, text):
        """대사를 한 글자씩. 문장 끝·쉼표에서 잠깐 쉰다. 움직임을 끄면 한 번에."""
        if self.ty['job']:
            self.win.after_cancel(self.ty['job'])
        self.ty = {'text': text, 'n': 0, 'job': None}
        if not self.app.anim_on.get() or self.app.type_scale() == 0:
            self.ty['n'] = len(text)
        self._type()

    def _type(self):
        ty, c = self.ty, self.scene
        ty['job'] = None
        if not self.win.winfo_exists() or not hasattr(self, 't_id'):
            return
        c.itemconfig(self.t_id, text=ty['text'][:ty['n']])
        done = ty['n'] >= len(ty['text'])
        c.itemconfig(self.more_id, state='hidden')
        if done:
            return
        ch = ty['text'][ty['n']]
        ty['n'] += 1
        ty['job'] = self.win.after(max(1, int(self.PAUSE.get(ch, self.TYPE_MS) * self.app.type_scale())), self._type)

    def typing(self):
        return self.ty['n'] < len(self.ty['text'])

    def next(self):
        if self.typing():  # 타이핑 중이면 그 줄을 바로 다 보이기
            self.ty['n'] = len(self.ty['text'])
            if self.ty['job']:
                self.win.after_cancel(self.ty['job'])
            self._type()
            return
        if self.pi + 1 < len(self.pages):
            self.pi += 1
            self.show_page()
            return
        if self.speaker:  # 발표 끝 → 다음 사람
            self.presented.add(self.speaker)
            self.speaker = None
            m = self.meeting()
            if m:
                m['presented'] = sorted(self.presented)
                try:
                    write_json(MEET_FILE, m)
                except OSError:
                    pass
            self.pages = []
            self.refresh()
            self.draw_scene()

    def prev(self):
        if self.pi > 0:
            self.pi -= 1
            self.show_page()

    def _anim_tick(self):
        """자리의 세션들: 숨쉬기·눈 깜빡임·타자·말할 때 들썩임, 머리 위 표시, 대사 다 나오면 ▼ 깜빡임."""
        c, Z = self.scene, self.app.Z
        self.ph += 1
        if c.winfo_ismapped() and hasattr(self, 'seat_items'):
            for sid, it in self.seat_items.items():
                r = it['r']
                frames = self.sprite(sid)[0]
                pose = self.pose(r, self.ph) if self.app.anim_on.get() else self.pose(r, 0)
                if pose != it['pose']:
                    c.itemconfig(it['img'], image=frames[pose])
                    it['pose'] = pose
                bob = (-2 * Z if (self.ph // 2) % 2 else 0) if (r['state'] == 'talk' and self.typing()) else 0
                if bob != it['bob']:
                    c.move(it['img'], 0, bob - it['bob'])
                    it['bob'] = bob
                if it['txt'] and r['state'] == 'busy':
                    c.itemconfig(it['txt'], text='.' * ((self.ph // 3) % 3 + 1))
            if hasattr(self, 'more_id'):
                show = not self.typing() and (self.speaker or self.pi + 1 < len(self.pages)) and (self.ph // 5) % 2 == 0
                c.itemconfig(self.more_id, state='normal' if show else 'hidden')
        self.win.after(110, self._anim_tick)

    # ── 회의 준비 ──
    def build_setup(self, m, live):
        app, F, f = self.app, self.app.fonts, self.f_setup
        for w in list(f.body.winfo_children()) + list(f.tools.winfo_children()):
            w.destroy()
        self.wraps = [l for l in self.wraps if l.winfo_exists()]
        f.sub.configure(text=T('{0}명 선택').format(len(self.pick)))
        if live:
            allon = len(self.pick) == len(live)
            app._btn(f.tools, T('모두 빼기') if allon else T('모두 초대'),
                     lambda: self._set_pick(set() if len(self.pick) == len(self.live()) else {s for s, _n, _m in self.live()})
                     ).pack(side='right')
        grid = tk.Frame(f.body, bg=CARD)
        grid.pack(fill='x', pady=(2, 0))
        if not live:
            tk.Label(grid, text=T('켜져 있는 세션이 없어요. 세션 켜기에서 먼저 켜 주세요.'), font=F['body'], bg=CARD,
                     fg=DIM).pack(anchor='w')
        cols = max(1, (self.cv.winfo_width() - 60) // int(210 * app.Z)) if self.cv.winfo_width() > 1 else 3
        self.chips = {}
        for i, (sid, name, mode) in enumerate(live):
            label = fit(name, F['bodyb'], int(130 * app.Z)) + ('' if mode != 'busy' else T(' · 작업 중'))
            b = self._chip(grid, label, sid in self.pick, lambda s=sid: self._toggle(s), self.face(sid))
            b.grid(row=i // cols, column=i % cols, sticky='we', padx=(0, 8), pady=(0, 8))
            self.chips[sid] = b
        for col in range(cols):
            grid.columnconfigure(col, weight=1, uniform='m')
        row = tk.Frame(f.body, bg=CARD)
        row.pack(fill='x', pady=(6, 0))
        tk.Label(row, text=T('보고 범위'), font=F['h2'], bg=CARD, fg=FG).pack(side='left', padx=(0, 12))
        self.turn_chips = {}
        for n in MEET_TURNS:
            b = self._chip(row, T('최근 1턴') if n == 1 else T('최근 {0}턴').format(n), n == self.turns, lambda n=n: self._set_turns(n))
            b.pack(side='left', padx=(0, 6))
            self.turn_chips[n] = b
        app._btn(row, T('회의 시작'), self.start, bg=ACCENT, fg='#ffffff', hover=ACC_DARK, font='menu').pack(side='right')
        if m:
            app._btn(row, T('회의 끝내기'), self.end).pack(side='right', padx=(0, 8))
        row = tk.Frame(f.body, bg=CARD)
        row.pack(fill='x', pady=(10, 0))
        tk.Label(row, text=T('보고 방식'), font=F['h2'], bg=CARD, fg=FG).pack(side='left', padx=(0, 12))
        self.style_chips = {}
        for k, label in (('brief', T('간단 · 토큰 절약')), ('rich', T('상세 · 핵심 수치·그래프·캡처 확대'))):
            b = self._chip(row, label, k == self.style, lambda k=k: self._set_style(k))
            b.pack(side='left', padx=(0, 6))
            self.style_chips[k] = b
        self.style_note = tk.Label(f.body, text=STYLE_NOTE[self.style], font=F['small'], bg=CARD, fg=TEXT2, anchor='w', justify='left')
        self.style_note.pack(fill='x', pady=(6, 0))
        self.wraps.append(self.style_note)
        l = tk.Label(f.body, text=T('회의를 시작하면 고른 세션마다 최근 N턴의 요청 목록과 보고 지시가 가요. 바쁜 세션은 하던 일을 마친 뒤 보고해요.'),
                     font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left')
        l.pack(fill='x', pady=(10, 4))
        self.wraps.append(l)
        self._bind_wheel(f.outer)
        self._wrap(self.cv.winfo_width())

    def _chip(self, parent, text, on, cmd, img=None):
        b = tk.Label(parent, text=text, image=img or '', compound='left', anchor='w' if img else 'center', padx=10, pady=4,
                     cursor='hand2')
        self._chip_style(b, on, 0)
        b.bind('<Button-1>', lambda e: cmd())
        return b

    def _chip_style(self, b, on, ms=170):
        F = self.app.fonts
        b.configure(font=F['bodyb' if on else 'body'], highlightthickness=2 if on else 1,
                    highlightbackground=ACCENT if on else EDGE)
        if ms:
            self.app.fade_bg(b, MINT if on else CARD, ms, fg=ACC_TEXT if on else FG)
        else:
            b.configure(bg=MINT if on else CARD, fg=ACC_TEXT if on else FG)

    def face(self, sid):
        if sid not in self.faces:
            self.faces[sid] = make_frames(sid, 2.2 * self.app.Z, self.app.avatars.get(sid))[0]['idle']
        return self.faces[sid]

    def _toggle(self, sid):
        self.pick ^= {sid}
        self.app.cfg['meet_pick'] = sorted(self.pick)
        if sid in getattr(self, 'chips', {}):
            self._chip_style(self.chips[sid], sid in self.pick)
        self.f_setup.sub.configure(text=T('{0}명 선택').format(len(self.pick)))
        self.refresh()

    def _set_pick(self, pick):
        self.pick = pick
        self.app.cfg['meet_pick'] = sorted(pick)
        for sid, b in getattr(self, 'chips', {}).items():
            self._chip_style(b, sid in pick)
        self.setup_sig = None
        self.refresh()

    def _set_style(self, k):
        self.style = self.app.cfg['meet_style'] = k
        for key, b in self.style_chips.items():
            self._chip_style(b, key == k)
        self.style_note.configure(text=STYLE_NOTE[k])

    def _set_turns(self, n):
        self.turns = self.app.cfg['meet_turns'] = n
        for k, b in self.turn_chips.items():
            self._chip_style(b, k == n)
        if hasattr(self, 'geo'):
            self.scene.delete('all')
            self.draw_scene()

    # ── 회의록 ──
    def build_minutes(self, m):
        app, F, Z, f = self.app, self.app.fonts, self.app.Z, self.f_min
        pos = self.cv.yview()[0]
        for w in list(f.body.winfo_children()) + list(f.tools.winfo_children()):
            w.destroy()
        self.wraps = [l for l in self.wraps if l.winfo_exists()]
        if not m:
            f.sub.configure(text='')
            tk.Label(f.body, text=T('회의를 시작하면 세션들의 보고서가 여기에 쌓여요.'), font=F['body'], bg=CARD, fg=DIM,
                     anchor='w').pack(fill='x', pady=(0, 6))
            self._bind_wheel(f.outer)
            return
        when = datetime.datetime.fromtimestamp(m.get('startedAt', 0) / 1000).strftime(T('%m월 %d일 %H:%M'))
        got = [r['sid'] for r in self.roster if r['report']]
        f.sub.configure(text=T('{0} 시작 · 최근 {1}턴 · {2}/{3} 도착').format(when, m.get('turns', '?'), len(got), len(m['members'])))
        app._btn(f.tools, T('자료로 저장'), lambda: self.export(m)).pack(side='right')
        app._btn(f.tools, T('폴더 열기'), lambda: os.startfile(self.folder(m)) if os.path.isdir(self.folder(m)) else None
                 ).pack(side='right', padx=(0, 6))
        fresh = set(got) - self.known if self.known is not None else set()
        self.known = set(got)
        for r in self.roster:
            self._card(m, r, r['sid'] in fresh)
        self._bind_wheel(f.outer)
        self._wrap(self.cv.winfo_width())
        self.win.after_idle(lambda: self.cv.yview_moveto(pos))

    def _card(self, m, r, fresh):
        app, F = self.app, self.app.fonts
        st = {'ready': (T('보고 완료'), '#2fb985'), 'talk': (T('발표 중'), ACCENT), 'done': (T('보고 완료'), '#2fb985'),
              'off': (T('꺼져 있음'), '#8a96a3'), 'busy': (T('보고 준비 중'), '#5b9ee8'), 'wait': (T('요청 보냄'), '#f0a020')}
        label, col = st.get(r['state'], st['wait'])
        card = tk.Frame(self.f_min.body, bg=CARD2, padx=14, pady=12, highlightthickness=2,
                        highlightbackground=ACCENT if fresh else CARD2)
        card.pack(fill='x', pady=(0, 10))
        if fresh:  # 막 도착한 보고서는 테두리가 반짝였다가 가라앉는다
            app.tw.run(1600, lambda e: card.configure(highlightbackground=mix(ACCENT, CARD2, e)), ease=ease_in_out,
                       key=('fresh', str(card)))
        hd = tk.Frame(card, bg=CARD2)
        hd.pack(fill='x')
        tk.Label(hd, image=self.face(r['sid']), bg=MINT, padx=4, pady=4).pack(side='left', padx=(0, 12))
        tx = tk.Frame(hd, bg=CARD2)
        tx.pack(side='left', fill='x', expand=True)
        t1 = tk.Frame(tx, bg=CARD2)
        t1.pack(fill='x')
        tk.Label(t1, text=clean(r['name'], 40), font=F['h2'], bg=CARD2, fg=FG).pack(side='left')
        tk.Label(t1, text=label, font=F['tiny'], bg=col, fg='#ffffff', padx=8, pady=1).pack(side='left', padx=10)
        rep = r['report']
        if rep:
            b = app._btn(hd, T('발표 보기'), lambda s=r['sid']: (self.cv.yview_moveto(0), self.present(s, user=True)),
                         bg=CARD, fg=ACC_TEXT, hover=HOVER)
            b.configure(highlightbackground=ACCENT)
            b.pack(side='right')
        if rep and rep.get('title'):
            tk.Label(tx, text=one_line(rep['title'], 90), font=F['bodyb'], bg=CARD2, fg=ACC_TEXT, anchor='w').pack(fill='x', pady=(2, 0))
        if not rep:
            tk.Label(tx, text=T('보고서를 기다리는 중이에요.') if r['mode'] else T('세션이 꺼져서 보고를 받을 수 없어요.'),
                     font=F['small'], bg=CARD2, fg=DIM, anchor='w').pack(fill='x', pady=(2, 0))
            return
        if rep.get('summary'):
            l = tk.Label(card, text=clean(rep['summary'], 1200), font=F['body'], bg=CARD, fg=FG, anchor='w',
                         justify='left', padx=12, pady=8)
            l.pack(fill='x', pady=(10, 0))
            self.wraps.append(l)
        self._extras(card, rep)
        for it in (rep.get('items') if isinstance(rep.get('items'), list) else [])[:20]:
            if isinstance(it, dict):
                self._item(card, m, it)
        if rep.get('next'):
            l = tk.Label(card, text=T('다음 할 일 · ') + clean(rep['next'], 600), font=F['small'], bg=CARD2, fg=TEXT2,
                         anchor='w', justify='left')
            l.pack(fill='x', pady=(10, 0))
            self.wraps.append(l)

    def _extras(self, card, rep):
        """상세 보고: 핵심 줄, 핵심 수치 칸, 그래프 (누르면 크게)."""
        app, F, Z = self.app, self.app.fonts, self.app.Z
        hl, ms, chs = report_extras(rep)
        if hl:
            box = tk.Frame(card, bg=MINT, padx=12, pady=8)
            box.pack(fill='x', pady=(10, 0))
            tk.Label(box, text=T('핵심'), font=F['tiny'], bg=ACCENT, fg='#ffffff', padx=8).pack(anchor='w', pady=(0, 4))
            for x in hl:
                l = tk.Label(box, text='✔  ' + x, font=F['bodyb'], bg=MINT, fg=FG, anchor='w', justify='left')
                l.pack(fill='x', pady=1)
                self.wraps.append(l)
        if ms:
            grid = tk.Frame(card, bg=CARD2)
            grid.pack(fill='x', pady=(10, 0))
            cols = 3 if len(ms) > 4 or self.big else 2 if len(ms) > 1 else 1
            cols = min(cols, len(ms)) if len(ms) < 3 else cols
            for i, x in enumerate(ms):
                tile = tk.Frame(grid, bg=CARD, padx=14, pady=10, highlightthickness=1, highlightbackground=EDGE)
                tile.grid(row=i // cols, column=i % cols, sticky='nsew', padx=(0, 8), pady=(0, 8))
                tk.Label(tile, text=x['label'], font=F['small'], bg=CARD, fg=DIM, anchor='w').pack(fill='x')
                tk.Label(tile, text=x['value'], font=F['num'], bg=CARD, fg=TONE_COL.get(x['tone'], FG), anchor='w').pack(fill='x')
                if x['note']:
                    tk.Label(tile, text=x['note'], font=F['tiny'], bg=CARD, fg=TONE_COL.get(x['tone'], DIM), anchor='w').pack(fill='x')
            for col in range(cols):
                grid.columnconfigure(col, weight=1, uniform='mt')
        if chs:
            W = max(360, min(int((980 if self.big else 640) * Z), self.cv.winfo_width() - int(90 * Z)))
            H = int((330 if self.big else 250) * Z)
            fam = F['body'].actual('family')
            for ch in chs:
                cv = tk.Canvas(card, width=W, height=H, bg=CARD, highlightthickness=1, highlightbackground=EDGE, cursor='hand2')
                cv.pack(anchor='w', pady=(10, 0))
                draw_chart(cv, 0, 0, chart_layout(ch, W, H, FG, DIM, EDGE, s=Z * (1.05 if self.big else 0.9)), fam, CARD)
                cv.bind('<Button-1>', lambda e, ch=ch: ChartWin(app, ch))

    def _item(self, card, m, it):
        F, Z = self.app.fonts, self.app.Z
        f = tk.Frame(card, bg=CARD2)
        f.pack(fill='x', pady=(12, 0))
        tk.Frame(f, bg=EDGE, height=1).pack(fill='x', pady=(0, 10))
        top = tk.Frame(f, bg=CARD2)
        top.pack(fill='x')
        st, col = REPORT_ST.get(it.get('status'), REPORT_ST['done'])
        tk.Label(top, text=st, font=F['tiny'], bg=col, fg='#ffffff', padx=8, pady=1).pack(side='left')
        l = tk.Label(top, text=one_line(it.get('request') or '', 160), font=F['bodyb'], bg=CARD2, fg=FG, anchor='w', justify='left')
        l.pack(side='left', fill='x', expand=True, padx=10)
        self.wraps.append(l)
        if it.get('result'):
            l = tk.Label(f, text=clean(it['result'], 1500), font=F['body'], bg=CARD2, fg=TEXT2, anchor='w', justify='left')
            l.pack(fill='x', pady=(6, 0))
            self.wraps.append(l)
        files = [str(x) for x in it.get('files') or [] if x][:8]
        if files:
            l = tk.Label(f, text=T('파일 · ') + ', '.join(files), font=F['small'], bg=CARD2, fg=DIM, anchor='w', justify='left')
            l.pack(fill='x', pady=(4, 0))
            self.wraps.append(l)
        shots = tk.Frame(f, bg=CARD2)
        shots.pack(fill='x', pady=(6, 0))
        sh = self.shots(m, it)[:6]
        ps = [p for p, _c, _f in sh]
        tw = int((440 if self.big else 260) * Z)
        for i, (p, cap, foc) in enumerate(sh):
            img = self.thumb(p, tw)
            if not img:
                continue
            col = tk.Frame(shots, bg=CARD2)
            col.pack(side='left', anchor='n', padx=(0, 10), pady=(0, 6))
            pics = tk.Frame(col, bg=CARD2)
            pics.pack(anchor='w')
            b = tk.Label(pics, image=img, bg=CARD2, cursor='hand2', highlightthickness=1, highlightbackground=EDGE)
            b.pack(side='left')
            b.bind('<Button-1>', lambda e, i=i: ImageViewer(self.app, ps, i))
            if foc:  # 중요한 부분 확대
                crop = photo_crop(p, foc, tw, img.height(), self.thumbs)
                if crop:
                    z = tk.Label(pics, image=crop, bg=CARD2, cursor='hand2', highlightthickness=2, highlightbackground=ACCENT)
                    z.pack(side='left', padx=(6, 0))
                    z.bind('<Button-1>', lambda e, i=i: ImageViewer(self.app, ps, i))
            if cap or foc:
                l = tk.Label(col, text=('🔍 ' if foc else '') + (cap or T('중요한 부분을 확대했어요')), font=F['small'], bg=CARD2,
                             fg=TEXT2, anchor='w', justify='left', wraplength=tw * (2 if foc else 1))
                l.pack(anchor='w', pady=(4, 0))

    def _bind_wheel(self, root):
        stack = [root]
        while stack:
            w = stack.pop()
            w.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'), add='+')
            stack += w.winfo_children()

    def _wrap(self, width):
        for l in self.wraps:
            if l.winfo_exists():
                l.configure(wraplength=max(240, width - 100))

    def shots(self, m, it):
        """[(캡처 경로, 설명, focus)] — 있는 PNG·GIF만."""
        out = []
        for p, cap, foc in report_shots(it):
            p = p if os.path.isabs(p) else os.path.join(self.folder(m), p)
            if p.lower().endswith(('.png', '.gif')) and os.path.isfile(p):
                out.append((p, cap, foc))
        return out

    def images(self, m, it):
        return [p for p, _c, _f in self.shots(m, it)]

    # ── 조작 ──
    def start(self):
        app = self.app
        if not self.pick:
            app.toast(T('초대할 세션을 먼저 골라 주세요'), warn=True)
            return
        if read_json(MEET_FILE) and not tkmsg.askyesno(T('회의실'), T('지금 회의를 끝내고 새 회의를 시작할까요?\n(지난 보고서는 폴더에 남아요)'),
                                                        parent=self.win):
            return
        meeting_start(app, self.pick, self.turns, self.style)
        self.presented, self.speaker, self.pages, self.known = set(), None, [], set()
        self.f_setup.toggle(False)  # 준비 칸은 접고 회의 장면에 집중
        self.refresh()

    def end(self):
        try:
            os.remove(MEET_FILE)
        except OSError:
            pass
        self.speaker, self.pages, self.known = None, [], None
        self.presented = set()
        self.f_setup.toggle(True)
        self.refresh()

    def export(self, m):
        """보고서를 캡처와 함께 HTML 한 장으로 만들어 브라우저로 연다."""
        import html as H
        e = H.escape
        when = datetime.datetime.fromtimestamp(m.get('startedAt', 0) / 1000).strftime('%Y-%m-%d %H:%M')
        names = m.get('names') or {}
        out = [f'<!doctype html><meta charset="utf-8"><title>{e(T("회의 보고서"))} {e(when)}</title>',
               '<style>body{font-family:"Pretendard","Malgun Gothic",sans-serif;background:#f4f7f6;color:#1e2329;margin:0;padding:32px}'
               'h1{margin:0 0 4px}.sub{color:#6b7580;margin-bottom:24px}.card{background:#fff;border:1px solid #e2e8ee;'
               'border-radius:14px;padding:20px 24px;margin-bottom:18px}h2{margin:0 0 6px}.ttl{color:#22a273;font-weight:700}'
               '.sum{background:#f1f5f4;border-radius:10px;padding:10px 14px;margin:12px 0}.it{border-top:1px solid #e2e8ee;'
               'padding-top:12px;margin-top:12px}.st{display:inline-block;color:#fff;border-radius:6px;padding:1px 8px;font-size:12px;'
               'margin-right:8px}.files{color:#6b7580;font-size:13px}img{max-width:420px;border:1px solid #e2e8ee;'
               'border-radius:8px;margin:8px 8px 0 0}.hl{background:#e7f6ef;border-radius:10px;padding:10px 16px;margin:12px 0}'
               '.hl li{font-weight:700;margin:3px 0}.mt{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;'
               'margin:12px 0}.mt div{border:1px solid #e2e8ee;border-radius:10px;padding:10px 14px}.mt small{color:#6b7580;display:block}'
               '.mt b{font-size:24px;display:block}.chart{border:1px solid #e2e8ee;border-radius:10px;padding:6px;margin:10px 0;'
               'max-width:680px}.shot{display:inline-block;vertical-align:top;margin:8px 12px 0 0}.shot .cap{color:#4a5562;font-size:13px;'
               'max-width:420px}.crop{width:300px;border:2px solid #2fb985;border-radius:8px;background-repeat:no-repeat;'
               'display:inline-block;margin:8px 8px 0 0}</style>',
               f'<h1>{e(T("회의 보고서"))}</h1><div class="sub">{e(when)} · {e(T("최근 {0}턴").format(m.get("turns")))}</div>']
        for sid in m['members']:
            r = self.report(m, sid) or {}
            out.append(f'<div class="card"><h2>{e(self.app.mon.names.get(sid) or names.get(sid) or sid[:8])}</h2>')
            if not r:
                out.append(f'<p class="files">{e(T("보고서가 아직 없어요."))}</p></div>')
                continue
            if r.get('title'):
                out.append(f'<div class="ttl">{e(str(r["title"]))}</div>')
            if r.get('summary'):
                out.append(f'<div class="sum">{e(str(r["summary"]))}</div>')
            hl, ms, chs = report_extras(r)
            if hl:
                out.append('<ul class="hl">' + ''.join(f'<li>{e(x)}</li>' for x in hl) + '</ul>')
            if ms:
                out.append('<div class="mt">' + ''.join(
                    f'<div><small>{e(x["label"])}</small><b style="color:{TONE_COL.get(x["tone"], "#1e2329")}">{e(x["value"])}</b>'
                    f'<small>{e(x["note"])}</small></div>' for x in ms) + '</div>')
            for ch in chs:
                out.append('<div class="chart">' + chart_svg(chart_layout(ch, 660, 300, '#1e2329', '#6b7580', '#e2e8ee'), 660, 300) + '</div>')
            for it in r.get('items') if isinstance(r.get('items'), list) else []:
                if not isinstance(it, dict):
                    continue
                st, col = REPORT_ST.get(it.get('status'), REPORT_ST['done'])
                out.append(f'<div class="it"><span class="st" style="background:{col}">{e(st)}</span>'
                           f'<b>{e(str(it.get("request") or ""))}</b><p>{e(str(it.get("result") or ""))}</p>')
                if it.get('files'):
                    out.append(f'<div class="files">{e(", ".join(map(str, it["files"])))}</div>')
                for p, cap, foc in self.shots(m, it):
                    out.append(f'<div class="shot"><a href="file:///{e(p)}"><img src="file:///{e(p)}"></a>')
                    size = png_size(p)
                    if foc and size:  # 중요한 부분 확대: 배경 그림을 그 부분만 보이게 키운다
                        x, y, w, h = foc
                        px = x / (1 - w) * 100 if w < 1 else 0
                        py = y / (1 - h) * 100 if h < 1 else 0
                        url = 'file:///' + p.replace('\\', '/')
                        out.append(f'<div class="crop" style="background-image:url(&quot;{e(url)}&quot;);background-size:{100 / w:.2f}% {100 / h:.2f}%;'
                                   f'background-position:{px:.2f}% {py:.2f}%;aspect-ratio:{w * size[0]:.0f}/{h * size[1]:.0f}"></div>')
                    if cap:
                        out.append(f'<div class="cap">{"🔍 " if foc else ""}{e(cap)}</div>')
                    out.append('</div>')
                out.append('</div>')
            if r.get('next'):
                out.append(f'<p class="files">{e(T("다음 할 일 · "))}{e(str(r["next"]))}</p>')
            out.append('</div>')
        path = os.path.join(self.folder(m), 'report.html')
        try:
            os.makedirs(self.folder(m), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(out))
        except OSError as ex:
            self.app.toast(T('저장 실패: {0}').format(ex), warn=True)
            return
        webbrowser.open('file:///' + path.replace('\\', '/'))
        self.app.toast(T('회의 자료를 저장했어요 · {0}').format(path))


CLOUDFLARED_URL = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
PW_MIN_LEN = 6   # 원격 암호 최소 길이 (office_remote.PW_MIN과 같게)


class RemoteWin:
    """원격 조종: 켜고 끄기, 휴대폰 연결(QR), 연결된 기기, 기록."""

    def __init__(self, app):
        self.app = app
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('phone')
        w.title(T('원격 조종 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(820 * Z)}x{int(820 * Z)}')
        w.minsize(560, 480)
        grad_head(w, app, 'phone', T('원격 조종'),
                  T('휴대폰으로 어디서나 세션을 보고 명령을 보내요. PC에서 바깥으로만 연결해 공유기 설정이 필요 없어요.')).pack(fill='x')
        holder = tk.Frame(w, bg=BG, padx=14)
        holder.pack(fill='both', expand=True, pady=(10, 12))
        self.cv, self.inner = scroll_frame(holder, BG)
        w.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.wraps = []
        # 연결
        c = self._card(T('원격 연결'))
        row = tk.Frame(c, bg=CARD)
        row.pack(fill='x')
        self.dot = tk.Canvas(row, width=int(14 * Z), height=int(14 * Z), bg=CARD, highlightthickness=0)
        self.dot.pack(side='left', padx=(0, 8))
        self.status = tk.Label(row, text='', font=F['bodyb'], bg=CARD, fg=FG, anchor='w')
        self.status.pack(side='left')
        self.toggle_btn = app._btn(row, '', self.toggle, bg=ACCENT, fg='#ffffff', hover=ACC_DARK, font='menu')
        self.toggle_btn.pack(side='right')
        self.url = tk.Label(c, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left')
        self.url.pack(fill='x', pady=(6, 0))
        self.wraps.append(self.url)
        self.share = tk.Frame(c, bg=CARD)  # 켜져 있을 때: 주소 복사
        b = app._btn(self.share, T('주소 복사'), self.copy_address, bg=CARD, fg=ACC_TEXT, hover=HOVER)
        b.configure(highlightbackground=ACCENT)
        b.pack(side='left')
        self.share_note = tk.Label(self.share, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left',
                                   wraplength=int(440 * Z))
        self.share_note.pack(side='left', fill='x', expand=True, padx=(10, 0))
        self.dl = tk.Frame(c, bg=CARD)  # cloudflared가 없을 때
        # 미리보기
        c = self._card(T('휴대폰 화면 미리보기'))
        l = tk.Label(c, text=T('휴대폰 웹앱을 이 PC에서 휴대폰 크기 창으로 띄워 봐요. 바깥 연결 없이 이 PC 안에서만 열리고, 실제 세션이 그대로 보이며 버튼도 실제로 동작해요.'),
                     font=F['body'], bg=CARD, fg=TEXT2, anchor='w', justify='left')
        l.pack(fill='x')
        self.wraps.append(l)
        b = app._btn(c, T('휴대폰 화면 열기'), app.remote_preview, bg=CARD, fg=ACC_TEXT, hover=HOVER)
        b.configure(highlightbackground=ACCENT)
        b.pack(anchor='w', pady=(10, 0))
        # 휴대폰 연결
        c = self._card(T('휴대폰 연결'))
        self.pair_note = tk.Label(c, text='', font=F['body'], bg=CARD, fg=TEXT2, anchor='w', justify='left')
        self.pair_note.pack(fill='x')
        self.wraps.append(self.pair_note)
        self.pair_btn = app._btn(c, T('휴대폰 연결하기 (QR 보기)'), self.show_qr, bg=CARD, fg=ACC_TEXT, hover=HOVER)
        self.pair_btn.configure(highlightbackground=ACCENT)
        self.pair_btn.pack(anchor='w', pady=(10, 0))
        self.qr_box = tk.Frame(c, bg=CARD)
        self.pairing = None  # (기기 id, 띄운 시각)
        # 암호
        c = self._card(T('암호 잠금'))
        self.pw_note = tk.Label(c, text='', font=F['body'], bg=CARD, fg=TEXT2, anchor='w', justify='left')
        self.pw_note.pack(fill='x')
        self.wraps.append(self.pw_note)
        self.fp = tk.Label(c, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left')
        self.fp.pack(fill='x', pady=(4, 0))
        self.wraps.append(self.fp)
        bar = tk.Frame(c, bg=CARD)
        bar.pack(fill='x', pady=(10, 0))
        self.pw_btn = app._btn(bar, '', self.ask_password, bg=CARD, fg=ACC_TEXT, hover=HOVER)
        self.pw_btn.configure(highlightbackground=ACCENT)
        self.pw_btn.pack(side='left')
        self.pw_clear = app._btn(bar, T('암호 없애기'), self.clear_password)
        self.pw_sig = None
        # 기기
        self.dev_card = self._card(T('연결된 기기'))
        self.dev_sig = None
        # 안내
        c = self._card(T('어떻게 동작하나요'))
        l = tk.Label(c, font=F['small'], bg=CARD, fg=TEXT2, anchor='w', justify='left', text=T(
            '· PC가 Cloudflare 임시 터널을 바깥으로 열고, 휴대폰 웹앱은 그 주소로 들어와요. 공유기 포트를 열지 않아요.\n'
            '· 터널 주소는 켤 때마다 바뀌어서, ntfy.sh의 비밀 채널에 지금 주소를 적어 두고 휴대폰이 찾아가요.\n'
            '· 기기마다 다른 열쇠(토큰)를 줘요. 열쇠는 QR의 한 번용 코드(10분)나, 복사한 주소로 들어와 맞힌 암호와 바꿔서만 받아요. '
            'PC에는 열쇠의 지문(해시)만 남고, 열쇠 없는 요청은 모두 거절해요.\n'
            '· 휴대폰은 PC의 서명을 확인한 뒤에만 열쇠·암호를 보내요. 주소가 새어 누가 가짜 서버를 세워도 그쪽으로는 보내지 않아요.\n'
            '· 세션이 확인을 기다리거나 회의 보고서가 오면 휴대폰으로 알림을 보내요 (휴대폰 앱에서 알림을 켠 경우).\n'
            '· 오가는 내용은 HTTPS로 암호화되지만 Cloudflare를 거쳐요. 쓰지 않을 땐 꺼 두세요.'))
        l.pack(fill='x')
        self.wraps.append(l)
        c = self._card(T('기록'))
        self.log = tk.Label(c, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left')
        self.log.pack(fill='x')
        self.cv.bind('<Configure>', lambda e: [x.configure(wraplength=max(240, e.width - 80)) for x in self.wraps], add='+')
        self.refresh()

    def _card(self, title):
        outer = tk.Frame(self.inner, bg=CARD, padx=16, pady=12, highlightthickness=1, highlightbackground=EDGE)
        outer.pack(fill='x', pady=(0, 10))
        tk.Label(outer, text=title, font=self.app.fonts['h2'], bg=CARD, fg=FG, anchor='w').pack(fill='x', pady=(0, 8))
        return outer

    def toggle(self):
        app = self.app
        if app.remote and app.remote.tunnel:
            if self.pairing:
                self.hide_qr(revoke=True)
            app.remote_stop()
        else:
            app.remote_start()
        self.refresh(force=True)

    def refresh(self, force=False):
        if not self.win.winfo_exists():
            return
        app, F, Z = self.app, self.app.fonts, self.app.Z
        r = app.remote
        st = r.status if r else 'off'
        on = bool(r and r.tunnel)
        col = {'on': ACCENT, 'local': BLUE, 'starting': '#f0a020', 'no_cloudflared': RED}.get(st, '#8a96a3')
        self.dot.delete('all')
        s = int(self.dot['width'])
        self.dot.create_oval(2, 2, s - 2, s - 2, fill=col, outline='')
        self.status.configure(text={'on': T('켜짐 · 휴대폰에서 접속할 수 있어요'), 'starting': T('연결하는 중…'),
                                    'no_cloudflared': T('cloudflared가 없어요'),
                                    'local': T('PC 안에서만 켜짐 (미리보기) · 휴대폰에서 쓰려면 [원격 켜기]')}.get(st, T('꺼짐')))
        self.toggle_btn.configure(text=T('원격 끄기') if on else T('원격 켜기'))
        self.toggle_btn._bg = CARD2 if on else ACCENT
        self.toggle_btn.configure(bg=self.toggle_btn._bg, fg=FG if on else '#ffffff')
        self.url.configure(text=(T('지금 주소: {0}').format(r.url) if r and r.url else
                                 T('켜면 Cloudflare 임시 터널을 열고 휴대폰 웹앱이 찾아올 수 있게 주소를 알려요.')))
        if st == 'no_cloudflared' and not self.dl.winfo_ismapped():
            for x in self.dl.winfo_children():
                x.destroy()
            tk.Label(self.dl, text=T('원격 연결에는 Cloudflare의 cloudflared(무료, 약 55MB)가 필요해요.'), font=F['small'], bg=CARD,
                     fg=TEXT2).pack(side='left')
            app._btn(self.dl, T('받기'), self.download).pack(side='left', padx=8)
            self.dl.pack(fill='x', pady=(8, 0))
        elif st != 'no_cloudflared' and self.dl.winfo_ismapped() and not getattr(self, 'downloading', False):
            self.dl.pack_forget()
        self.pair_btn.configure(state='normal')
        self.pair_note.configure(text=(T('휴대폰 카메라로 QR을 찍으면 휴대폰 웹앱이 열리고 이 PC와 연결돼요. 홈 화면에 추가하면 앱처럼 쓸 수 있어요.')
                                       if on else T('먼저 원격을 켜 주세요.')))
        if not on:
            self.pair_btn.pack_forget()
        elif not self.pairing and not self.pair_btn.winfo_ismapped():
            self.pair_btn.pack(anchor='w', pady=(10, 0))
        if self.pairing and r:
            dev = next((d for d in r.cfg['devices'] if d['id'] == self.pairing[0]), None)
            if dev and dev.get('seen'):
                self.hide_qr()
                app.toast(T('휴대폰이 연결됐어요 · {0}').format(dev['name']))
            elif time.time() - self.pairing[1] > 600:
                self.hide_qr(revoke=True)
        if on and r.url:
            self.share_note.configure(text=(T('복사한 주소로 들어온 기기는 암호를 맞혀야 연결돼요.') if r.has_password
                                       else T('주소로 새 기기를 연결하려면 먼저 암호를 정해야 해요.'))
                                      + ('\n' + T('휴대폰 앱 페이지(GitHub Pages)가 열리지 않아 지금 터널 주소로 건네요. 원격을 다시 켜면 주소가 바뀌니 새로 복사해 주세요.')
                                         if r.app_ok is False else ''))
            if not self.share.winfo_ismapped():
                self.share.pack(fill='x', pady=(8, 0), after=self.url)
        elif self.share.winfo_ismapped():
            self.share.pack_forget()
        has_pw = bool(r and r.has_password)
        sig = (has_pw, r.fingerprint if r else '')
        if sig != self.pw_sig:
            self.pw_sig = sig
            self.pw_note.configure(text=T('암호가 걸려 있어요. 복사한 주소로 들어온 기기는 이 암호를 맞혀야 연결되고, 여러 번 틀리면 막혀요.')
                                   if has_pw else T('암호가 없어요. 지금은 QR로만 새 기기를 연결할 수 있어요. 암호를 정하면 주소만 건네도 연결할 수 있어요.'))
            self.fp.configure(text=T('PC 확인 코드: {0} · 휴대폰 암호 화면에 같은 코드가 보이는지 확인하세요.').format(sig[1]) if r else '')
            self.pw_btn.configure(text=T('암호 바꾸기') if has_pw else T('암호 정하기'))
            if has_pw and not self.pw_clear.winfo_ismapped():
                self.pw_clear.pack(side='left', padx=6)
            elif not has_pw:
                self.pw_clear.pack_forget()
        self.draw_devices(force)
        if r:
            self.log.configure(text='\n'.join(r.log[-8:]) or '-')
        self.win.after(1000, self.refresh)

    def show_qr(self):
        r = self.app.remote
        if not r or not r.httpd:
            return
        import office_remote
        dev_id, link = r.pair()
        self.pairing = (dev_id, time.time())
        self.pair_btn.pack_forget()
        for x in self.qr_box.winfo_children():
            x.destroy()
        m = office_remote.qr_matrix(link, 'M')
        n = len(m)
        cell = max(4, int(5 * self.app.Z))
        q = 4
        cv = tk.Canvas(self.qr_box, width=(n + 2 * q) * cell, height=(n + 2 * q) * cell, bg='#ffffff', highlightthickness=0)
        for y in range(n):
            for x in range(n):
                if m[y][x]:
                    cv.create_rectangle((x + q) * cell, (y + q) * cell, (x + q + 1) * cell, (y + q + 1) * cell, fill='#000000',
                                        outline='')
        cv.pack(side='left')
        side = tk.Frame(self.qr_box, bg=CARD)
        side.pack(side='left', fill='both', expand=True, padx=(16, 0))
        F = self.app.fonts
        l = tk.Label(side, font=F['body'], bg=CARD, fg=FG, anchor='w', justify='left', text=T(
            '1. 휴대폰 카메라로 QR을 찍어 링크를 열어요.\n2. 웹앱이 열리면 이 PC와 바로 연결돼요.\n'
            '3. 브라우저 메뉴의 "홈 화면에 추가"로 앱처럼 쓸 수 있어요.\n\n이 QR은 한 기기에 한 번만 써 주세요. 10분 뒤 사라져요.'))
        l.pack(fill='x')
        self.wraps.append(l)
        bar = tk.Frame(side, bg=CARD)
        bar.pack(fill='x', pady=(10, 0))
        self.app._btn(bar, T('링크 복사'), lambda: (self.win.clipboard_clear(), self.win.clipboard_append(link),
                                                  self.app.toast(T('연결 링크를 복사했어요 · 남에게 보내지 마세요')))).pack(side='left')
        self.app._btn(bar, T('취소'), lambda: self.hide_qr(revoke=True)).pack(side='left', padx=6)
        self.qr_box.pack(fill='x', pady=(12, 0))

    def copy_address(self):
        """지금 주소(열쇠 없음)를 복사한다. 이 주소로 들어온 기기는 암호를 맞혀야 연결된다."""
        r = self.app.remote
        if not (r and r.url):
            self.app.toast(T('먼저 원격을 켜 주세요'), warn=True)
            return
        if not r.has_password:
            if tkmsg.askyesno(T('주소 복사'), T('주소로 들어온 기기는 암호를 맞혀야 연결돼요.\n지금 암호를 정할까요?'), parent=self.win):
                self.ask_password(then=self.copy_address)
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(r.share_link())
        self.app.toast(T('주소를 복사했어요 · 들어온 기기는 암호를 맞혀야 연결돼요'))

    def ask_password(self, then=None):
        """암호 정하기/바꾸기 창."""
        app, F, Z = self.app, self.app.fonts, self.app.Z
        r = app.remote_obj()
        d = tk.Toplevel(app.root)
        d.title(T('암호 잠금'))
        d.configure(bg=CARD)
        d.resizable(False, False)
        d.transient(app.root)
        body = tk.Frame(d, bg=CARD, padx=24, pady=18)
        body.pack(fill='both')
        tk.Label(body, text=T('암호 바꾸기') if r.has_password else T('암호 정하기'), font=F['h2'], bg=CARD, fg=FG, anchor='w').pack(fill='x')
        tk.Label(body, text=T('{0}자 이상으로 정해 주세요. 다른 곳에서 쓰는 암호는 피해 주세요.').format(PW_MIN_LEN), font=F['small'], bg=CARD,
                 fg=DIM, anchor='w', justify='left', wraplength=int(380 * Z)).pack(fill='x', pady=(4, 10))
        ents = []
        for label in (T('새 암호'), T('한 번 더')):
            tk.Label(body, text=label, font=F['small'], bg=CARD, fg=TEXT2, anchor='w').pack(fill='x')
            e = tk.Entry(body, font=F['body'], bg=CARD2, fg=FG, relief='flat', highlightthickness=1, show='•', width=30,
                         highlightbackground=EDGE, highlightcolor=ACCENT, insertbackground=FG)
            e.pack(fill='x', ipady=3, pady=(2, 8))
            ents.append(e)
        err = tk.Label(body, text='', font=F['small'], bg=CARD, fg=RED, anchor='w')
        err.pack(fill='x')

        def save(_e=None):
            a, b = ents[0].get(), ents[1].get()
            if len(a) < PW_MIN_LEN:
                err.configure(text=T('암호가 너무 짧아요'))
                return
            if len(a) > 128:
                err.configure(text=T('암호가 너무 길어요'))
                return
            if a != b:
                err.configure(text=T('두 암호가 달라요'))
                return
            r.set_password(a)
            d.destroy()
            devs = [x for x in r.cfg['devices'] if not x.get('preview')]
            if devs and tkmsg.askyesno(T('암호 잠금'), T('이미 연결된 기기 {0}대는 그대로 쓸 수 있어요.\n모두 끊고 새 암호로 다시 연결하게 할까요?').format(len(devs)),
                                       parent=self.win):
                r.revoke_all()
            app.toast(T('암호를 정했어요'))
            self.pw_sig = None
            if then:
                then()

        ents[1].bind('<Return>', save)
        bar = tk.Frame(d, bg=CARD, padx=24)
        bar.pack(fill='x', pady=(0, 18))
        app._btn(bar, T('저장'), save, bg=ACCENT, fg='#ffffff', hover=ACC_DARK).pack(side='right')
        app._btn(bar, T('취소'), d.destroy).pack(side='right', padx=8)
        d.update_idletasks()
        d.geometry(f'+{app.root.winfo_rootx() + 120}+{app.root.winfo_rooty() + 120}')
        d.grab_set()
        ents[0].focus_set()

    def clear_password(self):
        r = self.app.remote
        if r and r.has_password and tkmsg.askyesno(
                T('암호 잠금'), T('암호를 없앨까요?\n이미 연결된 기기는 그대로 쓰고, 주소로는 새 기기를 연결할 수 없게 돼요.'), parent=self.win):
            r.clear_password()
            self.pw_sig = None

    def hide_qr(self, revoke=False):
        if self.pairing and revoke and self.app.remote:
            self.app.remote.cancel_pair(self.pairing[0])  # 쓰지 않은 한 번용 코드는 없앤다
        self.pairing = None
        self.qr_box.pack_forget()
        for x in self.qr_box.winfo_children():
            x.destroy()

    def draw_devices(self, force=False):
        r = self.app.remote
        devs = [d for d in (r.cfg['devices'] if r else []) if d.get('seen') and not d.get('preview')]
        now = time.time()
        sig = tuple((d['id'], d['name'], int((now - d['seen']) // 60), bool(d.get('push'))) for d in devs)
        if sig == self.dev_sig and not force:
            return
        self.dev_sig = sig
        F = self.app.fonts
        for x in self.dev_card.winfo_children()[1:]:
            x.destroy()
        if not devs:
            tk.Label(self.dev_card, text=T('아직 연결된 기기가 없어요.'), font=F['body'], bg=CARD, fg=DIM, anchor='w').pack(fill='x')
            return
        for d in devs:
            row = tk.Frame(self.dev_card, bg=CARD2, padx=12, pady=8)
            row.pack(fill='x', pady=(0, 6))
            mins = int((now - d['seen']) // 60)
            seen = T('방금 접속') if mins < 1 else T('{0}분 전 접속').format(mins) if mins < 120 else T('{0}시간 전 접속').format(mins // 60)
            seen += {'pw': T(' · 암호로 연결'), 'qr': T(' · QR로 연결')}.get(d.get('via'), '')
            tk.Label(row, text=clean(d['name'], 40), font=F['bodyb'], bg=CARD2, fg=FG).pack(side='left')
            tk.Label(row, text=seen + (T(' · 알림 켬') if d.get('push') else ''), font=F['small'], bg=CARD2, fg=DIM).pack(side='left', padx=10)
            self.app._btn(row, T('연결 끊기'), lambda i=d['id']: self.revoke(i)).pack(side='right')

    def revoke(self, dev_id):
        if tkmsg.askyesno(T('원격 조종'), T('이 기기의 연결을 끊을까요?\n다시 쓰려면 QR로 새로 연결해야 해요.'), parent=self.win):
            self.app.remote.revoke(dev_id)
            self.draw_devices(force=True)

    def download(self):
        """cloudflared를 GitHub(Cloudflare 공식 배포)에서 받아 이 프로그램의 데이터 폴더에 둔다."""
        if getattr(self, 'downloading', False):
            return
        self.downloading = True
        for x in self.dl.winfo_children():
            x.destroy()
        lab = tk.Label(self.dl, text=T('받는 중… 0%'), font=self.app.fonts['small'], bg=CARD, fg=TEXT2)
        lab.pack(side='left')
        dst = os.path.join(CACHE_DIR, 'cloudflared.exe')
        prog = {'p': 0, 'err': None, 'done': False}

        def work():
            import urllib.request
            try:
                with urllib.request.urlopen(CLOUDFLARED_URL, timeout=30) as resp, open(dst + '.part', 'wb') as f:
                    total = int(resp.headers.get('Content-Length') or 0)
                    got = 0
                    while True:
                        b = resp.read(1 << 16)
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                        prog['p'] = int(got * 100 / total) if total else 0
                os.replace(dst + '.part', dst)
            except OSError as ex:
                prog['err'] = str(ex)
            prog['done'] = True

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if not self.win.winfo_exists():
                return
            if not prog['done']:
                lab.configure(text=T('받는 중… {0}%').format(prog['p']))
                self.win.after(300, poll)
                return
            self.downloading = False
            if prog['err']:
                lab.configure(text=T('받지 못했어요: {0}').format(prog['err']), fg=RED)
                return
            self.dl.pack_forget()
            self.app.remote_start()

        poll()



class OnboardWin:
    """처음 쓰는 환경: 비서 세션을 만들지 묻는다."""

    def __init__(self, app):
        self.app = app
        F, Z = app.fonts, app.Z
        self.win = w = tk.Toplevel(app.root)
        w.title(T('환영합니다 · Claude Office'))
        w.configure(bg=CARD)
        w.resizable(False, False)
        w.transient(app.root)
        grad_head(w, app, 'star', T('세션 사무실에 오신 걸 환영해요'),
                  T('이 PC에서 돌아가는 Claude Code 세션들이 사무실 캐릭터로 출근해요.'), height=96).pack(fill='x')
        body = tk.Frame(w, bg=CARD, padx=24, pady=18)
        body.pack(fill='both')
        tk.Label(body, text=T('비서 세션을 만들까요?'), font=F['h2'], bg=CARD, fg=FG, anchor='w').pack(fill='x')
        tk.Label(body, font=F['body'], bg=CARD, fg=TEXT2, anchor='w', justify='left', wraplength=int(520 * Z), text=(
            T('비서는 다른 세션들의 진행 상황을 살펴보고, 다음에 어느 세션에 무슨 일을 시키면 좋을지 제안해 줘요.\n'
            '· 사무실 위쪽 비서실에 앉아 말풍선으로 제안해요.\n'
            '· [브리핑 받기]를 누를 때만 일해요 (혼자 돌아가지 않아요).\n'
            '· 제안의 선택지 버튼을 누르면 그 일이 해당 세션에 바로 요청돼요.\n\n'
            '만들면 새 터미널 창에서 "비서" 세션이 켜지고, 필요한 secretary 스킬도 설치돼요.'))).pack(fill='x', pady=(8, 0))
        bar = tk.Frame(w, bg=CARD, padx=24)
        bar.pack(fill='x', pady=(0, 18))
        app._btn(bar, T('비서 세션 만들기'), self.yes, bg=ACCENT, fg='#ffffff', hover=ACC_DARK).pack(side='right')
        app._btn(bar, T('나중에'), self.no).pack(side='right', padx=8)
        tk.Label(bar, text=T('나중에 메뉴 > 비서 만들기 에서도 할 수 있어요'), font=F['small'], bg=CARD, fg=DIM).pack(side='left')
        w.protocol('WM_DELETE_WINDOW', self.no)
        w.update_idletasks()
        rx, ry = app.root.winfo_rootx(), app.root.winfo_rooty()
        x = rx + (app.root.winfo_width() - w.winfo_width()) // 2
        y = ry + (app.root.winfo_height() - w.winfo_height()) // 3
        w.geometry(f'+{max(0, x)}+{max(0, y)}')
        w.lift()

    def _done(self):
        self.app.cfg['onboarded'] = True
        self.app.save_cfg()
        self.win.destroy()
        self.app.root.after(600, self.app._maybe_mod)

    def yes(self):
        self._done()
        self.app.create_secretary()

    def no(self):
        self._done()


class Desk:
    pass


def fit(text, font, px):
    """픽셀 폭 px 안에 들어가도록 자른다 (font = tkfont.Font)."""
    text = clean(text)
    if font.measure(text) <= px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # 이분 탐색
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + '…') <= px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + '…'


def bar_text(p, n=10):
    k = int(round(max(0, min(100, p)) / 100 * n))
    return '█' * k + '░' * (n - k)


class App:
    def __init__(self, root, mon):
        self.root, self.mon = root, mon
        self.first_run = not os.path.exists(SETTINGS_FILE)
        self.cfg = read_json(SETTINGS_FILE) or {}
        z = self.cfg.get('zoom', 1.25)
        self.Z = z if z in ZOOMS else 1.25
        self.has_pd = 'Pretendard' in tkfont.families(root)
        self.fonts = {}
        for k, (size, weight) in FONT_SPECS.items():
            fam, wt = font_face(weight, self.has_pd)
            self.fonts[k] = tkfont.Font(family=fam, size=self._fs(size), weight=wt)
        fam, wt = font_face('normal', self.has_pd)
        self.fonts['bodyi'] = tkfont.Font(family=fam, size=self._fs(10), weight=wt, slant='italic')  # 세션톡 마크다운
        self.fonts['mono'] = tkfont.Font(family='Consolas', size=self._fs(10))
        self.desks, self.frames, self.order = {}, {}, []
        self.cols, self.room_w, self.room_px, self.rows = 0, 0, 0, 0
        self.sel = None
        self.hover = None
        self.storage = None
        self.p_path = None
        self._panel_job = None
        self.seen_ver = -1
        self.tick = None
        self.panel_sid, self.panel_seq, self.panel_ver = None, 0, -1
        self.flights, self.anim_k, self.minute, self.visible = [], -1, -1, True
        self.log_filter = 'all'
        self.bar_vals = None
        self._relayout_job = None
        self.sessions = []
        # 사용자가 정한 책상 순서(세션 id 목록). 없는 세션은 출근 순으로 뒤에 붙는다
        self.desk_order = [x for x in self.cfg.get('desk_order') or [] if isinstance(x, str)]
        self.drag = None
        # 꾸민 아바타 {세션 id: {style, hair, skin, shirt, acc}}, 탭 [{name, sids}], 비서
        self.avatars = {k: v for k, v in (self.cfg.get('avatars') or {}).items() if isinstance(v, dict)}
        self.groups = [g for g in self.cfg.get('groups') or [] if isinstance(g, dict) and g.get('name')]
        for g in self.groups:
            g['sids'] = [x for x in g.get('sids') or [] if isinstance(x, str)]
        self.group = self.cfg.get('group') or ''
        if not self.group_of(self.group):
            self.group = ''
        self.sec_pick = self.cfg.get('secretary') or ''
        self.sec_done = set(self.cfg.get('sec_done') or [])
        self.sec_sent = dict(self.cfg.get('sec_sent') or {})  # 비서 제안 → 맡긴 세션·시각 (진행 결과를 찾는 데)
        self.sec_sid, self.sec_on, self.sec_box = None, False, None
        self.sec_hits = []          # 비서 말풍선 안 버튼들 (캔버스 좌표, 할 일)
        self.wb_sig = self.secb_sig = None
        self.brief_at = 0           # 마지막으로 브리핑을 부탁한 시각
        self.layout_key = None
        self.grid_y = WALL_H + 4
        self.tab_sig = None
        self.tab_drop = None
        self.chatwin = self.secwin = self.avwin = self.launchwin = self.meetwin = self.remotewin = None
        self.remote = None  # 원격 조종 (켤 때 만든다)
        self.chat_seen = self.cfg.get('chat_seen') or time.time()
        self.pending = {}           # 이 창에서 켠 세션 id → (켠 시각, 이름) — 출근하면 골라 준다
        self.mod_restart = {}       # mod를 적용하려고 다시 켤 세션 id → 요청 시각 (일하는 중이면 끝날 때까지 기다림)
        self.menu_open, self.menu_hover, self.menu_boxes, self.menu_off = False, None, [], 0
        self.side_hover, self.side_sig = None, None
        self.toast_w = None
        self.head, self.head_hits, self.head_sig = {}, [], None
        # 메뉴 기능(세션톡·세션 켜기 등)을 사무실 화면 자리에 띄울지(기본), 별도 창으로 띄울지
        self.popup = tk.BooleanVar(value=bool(self.cfg.get('popup_windows', False)))
        self.page, self.page_key, self.routes = None, 'home', {}

        root.title('Claude Office')
        try:
            root.iconbitmap(default=os.path.join(APP_DIR, 'claude_office.ico'))
        except Exception:
            pass
        root.configure(bg=BG)
        root.geometry(self.cfg.get('geometry', '1600x960+40+20'))
        if DARK:
            root.after(10, lambda: dark_titlebar(root))
        root.minsize(1000, 640)
        self.anim_on = tk.BooleanVar(value=self.cfg.get('anim', True))
        self.topmost = tk.BooleanVar(value=self.cfg.get('topmost', False))
        self.tw = Tweens(root, self.anim_on.get)
        self.menu_p = 0.0  # 게임 메뉴가 펼쳐진 정도 (0~1)
        self._side_py = None  # 왼쪽 막대에서 고른 칸 표시의 지금 위치
        # 왼쪽 아이콘 막대 | 오른쪽 (상단 막대, 게임 메뉴, 사무실+패널)
        self.side = tk.Canvas(root, width=int(84 * self.Z), bg=SIDE, highlightthickness=0)
        self.side.pack(side='left', fill='y')
        self.side.bind('<Configure>', lambda e: self.draw_side(force=True))
        self.side.bind('<Motion>', self._side_motion)
        self.side.bind('<Leave>', lambda e: self._side_motion(None))
        self.side.bind('<Button-1>', self._side_click)
        main = self.main = tk.Frame(root, bg=BG)
        main.pack(side='left', fill='both', expand=True)
        self._build_top(main)
        self.gm = tk.Canvas(main, height=1, bg=GRAD[0], highlightthickness=0)
        self.gm.bind('<Configure>', lambda e: self.draw_menu())
        self.gm.bind('<Motion>', self._menu_motion)
        self.gm.bind('<Leave>', lambda e: self._menu_motion(None))
        self.gm.bind('<Button-1>', self._menu_click)

        self.paned = tk.PanedWindow(main, orient='horizontal', sashwidth=6, bg=EDGE, bd=0, sashrelief='flat')
        self.paned.pack(fill='both', expand=True)
        left = tk.Frame(self.paned, bg=BG)
        self._build_tabbar(left)
        self.cv = tk.Canvas(left, bg=FLOOR, highlightthickness=0)
        vsb = tk.Scrollbar(left, orient='vertical', command=self.cv.yview)
        self.cv.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.cv.pack(side='left', fill='both', expand=True)
        self.right = tk.Frame(self.paned, bg=BG)
        self._build_panel(self.right)
        self.paned.add(left, minsize=420, stretch='always')
        # 화면 전환용 자리: 머리(사무실로 돌아가기 + 제목) 아래에 기능 화면이 들어간다
        self.page_wrap = tk.Frame(main, bg=BG)
        ph = tk.Frame(self.page_wrap, bg=TOPBG, padx=14, pady=8)
        ph.pack(fill='x')
        self._btn(ph, T('← 사무실로'), self.show_office, bg=ACCENT, fg='#ffffff', hover=ACC_DARK).pack(side='left')
        self.page_title = tk.Label(ph, text='', font=self.fonts['h2'], bg=TOPBG, fg=FG)
        self.page_title.pack(side='left', padx=14)
        tk.Label(ph, text=T('Esc = 사무실로 · 메뉴 > 창으로 열기를 켜면 별도 창으로 열려요'), font=self.fonts['small'], bg=TOPBG,
                 fg=DIM).pack(side='right')
        tk.Frame(self.page_wrap, bg=EDGE, height=1).pack(fill='x')
        self.page_slot = tk.Frame(self.page_wrap, bg=BG)
        self.page_slot.pack(fill='both', expand=True)
        self.paned.add(self.right, minsize=400, stretch='never', width=self.cfg.get('panel_w', 560))

        self.cv.bind('<Configure>', self._on_cv_resize)
        self.cv.bind('<MouseWheel>', self._on_wheel)
        self.cv.bind('<Motion>', self._on_motion)
        self.cv.bind('<Leave>', lambda e: self._set_hover(None))
        self.cv.bind('<Button-1>', self._on_click)
        self.cv.bind('<B1-Motion>', self._on_drag)
        self.cv.bind('<ButtonRelease-1>', self._on_drop)
        self.cv.bind('<Button-3>', self._on_menu)
        root.bind('<Escape>', self._on_escape)
        self.right.bind('<Configure>', self._on_panel_resize)
        for seq, dz in (('<Control-equal>', 1), ('<Control-plus>', 1), ('<Control-minus>', -1),
                        ('<Control-KP_Add>', 1), ('<Control-KP_Subtract>', -1)):
            root.bind(seq, lambda e, dz=dz: self.set_zoom(dz))
        root.bind('<Control-0>', lambda e: self.set_zoom(0, reset=True))
        root.bind('<Left>', lambda e: None if isinstance(e.widget, tk.Entry) else self.cycle(-1))
        root.bind('<Right>', lambda e: None if isinstance(e.widget, tk.Entry) else self.cycle(1))
        root.bind('<Unmap>', lambda e: self._vis(False) if e.widget is root else None)
        root.bind('<Map>', lambda e: self._vis(True) if e.widget is root else None)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        self.toggle_top()
        root.after(200, self.refresh)
        root.after(300, self.animate)
        if self.cfg.get('remote_on'):  # 지난번에 켜 두었으면 다시 켠다
            root.after(1500, self.remote_start)
        if self.first_run and not self.cfg.get('onboarded'):
            root.after(3000, self._maybe_onboard)  # 첫 폴링으로 비서 세션이 이미 있는지 본 뒤
        else:
            root.after(4000, self._maybe_mod)  # 이미 쓰던 PC: 더 새 mod가 들어 있으면 업데이트를 권한다
            root.after(MOD_STALE * 1000 + 15_000, self.offer_mod_restart)  # mod 없이 켜진 세션이 있으면 다시 켜기를 권한다
        root.after(20_000, self._clean_loop)
        _APP[:] = [self]

    def _clean_loop(self):
        """켤 때와 30분마다 빈 세션 기록·꺼진 세션의 상태 파일을 치운다 (파일 읽기는 다른 스레드에서)."""
        with self.mon.lock:
            live = set(self.mon.sessions)
        live |= set(self.pending)  # 막 켜는 중인 세션

        def work():
            try:
                n = clean_empty_sessions(live)
                if any(n):
                    print('cleaned empty sessions', n, file=sys.stderr)
                if clean_talk_images():
                    print('cleaned old talk images', file=sys.stderr)
            except Exception as ex:
                print('clean error', ex, file=sys.stderr)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(30 * 60_000, self._clean_loop)

    # ── 공용 ──
    def _fs(self, base):
        return max(7, int(base * self.Z + 0.5))

    def P(self, *units):
        return [v * self.Z for v in units]

    def _vis(self, v):
        self.visible = v

    def toggle_top(self):
        self.root.attributes('-topmost', self.topmost.get())

    def save_cfg(self):
        try:
            self.cfg.update(geometry=self.root.geometry(), zoom=self.Z, anim=self.anim_on.get(),
                            topmost=self.topmost.get(), panel_w=self.right.winfo_width(), tab=self.tab,
                            desk_order=self.desk_order, avatars=self.avatars, groups=self.groups, group=self.group,
                            secretary=self.sec_pick, sec_done=sorted(self.sec_done)[-300:], sec_sent=self.sec_sent, chat_seen=self.chat_seen,
                            popup_windows=self.popup.get())
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.cfg, f)
        except Exception:
            pass

    def quit(self):
        self.save_cfg()
        if self.remote and self.remote.httpd:
            self.remote.stop(wait=3)  # 휴대폰이 '꺼짐'을 알 수 있게 잠깐 기다려 알린다
        self.mon.stop.set()
        self.mon.save_cache(force=True)
        self.root.destroy()

    def open_storage(self):
        self._open_win('storage', lambda: StorageWin(self))

    def _btn(self, parent, text, cmd, bg=CARD2, fg=FG, hover=HOVER, font='statb'):
        b = tk.Label(parent, text=text, font=self.fonts[font], bg=bg, fg=fg, padx=12, pady=4, cursor='hand2',
                     highlightthickness=1, highlightbackground=EDGE if bg in (CARD, CARD2) else bg)
        b.bind('<Button-1>', lambda e: cmd())
        b.bind('<Enter>', lambda e: self.fade_bg(b, hover, 110))
        b.bind('<Leave>', lambda e: self.fade_bg(b, b._bg, 170))
        b._bg = bg
        return b

    def fade_bg(self, w, col, ms, fg=None):
        """위젯 바탕(과 글자)색을 지금 색에서 col로 부드럽게 바꾼다."""
        try:
            a, af = w.cget('bg'), w.cget('fg')
        except tk.TclError:
            return
        self.tw.run(ms, lambda e: w.configure(bg=mix_any(a, col, e), **({'fg': mix_any(af, fg, e)} if fg else {})),
                    ease=ease_in_out, key=('bg', str(w)))

    def toast(self, text, warn=False):
        """창 아래쪽에 잠깐 뜨는 알림. 아래에서 살짝 튀어 올라왔다가 내려가며 사라진다."""
        if self.toast_w:
            self.toast_w.destroy()
        l = tk.Label(self.root, text=clean(text, 120), font=self.fonts['bodyb'], bg='#e8505b' if warn else ROOM['toast'],
                     fg='#ffffff', padx=18, pady=9)
        l.place(relx=0.5, rely=1.0, y=40, anchor='s')
        self.toast_w = l
        self.tw.run(320, lambda e: l.place_configure(y=int(40 - 70 * e)), ease=ease_back, key='toast')

        def out():
            if l.winfo_exists():
                self.tw.run(220, lambda e: l.place_configure(y=int(-30 + 80 * e)), lambda: l.destroy(),
                            ease=lambda t: t * t, key='toast')

        self.root.after(3600, out)

    def _on_escape(self, e):
        if self.close_lang():
            pass
        elif self.menu_open:
            self.toggle_menu(False)
        elif self.page:
            self.show_office()
        self._drag_cancel()

    # ── 기능 화면: 사무실 자리에 띄우기 / 별도 창 ──
    def make_host(self, key):
        """기능 화면을 담을 틀. 창 모드면 별도 창, 아니면 사무실 화면 자리를 바꿔 끼운다."""
        if self.popup.get():
            return tk.Toplevel(self.root)
        if self.page:  # 한 번에 한 화면
            old, self.page = self.page, None
            self._drop_page(old)
        self.paned.pack_forget()
        self.page_wrap.pack(fill='both', expand=True)
        self.page = PageHost(self, key)
        self.page_key = key
        self.draw_side(force=True)
        return self.page

    def show_office(self):
        if self.page:
            old, self.page = self.page, None
            self._drop_page(old)
        self._office_back()

    def _drop_page(self, old):
        """화면을 닫는다. 자주 오가는 화면(KEEP_PAGES)은 지우지 않고 숨겨 두었다가 다시 열 때 그대로 보인다."""
        if old.key in KEEP_PAGES and old.winfo_exists():
            old.place_forget()
            return
        old.destroy()

    def _office_back(self):
        self.page_wrap.pack_forget()
        if not self.paned.winfo_manager():
            self.paned.pack(fill='both', expand=True)
        self.page_key = 'home'
        self.draw_side(force=True)

    def _page_gone(self, host):
        """화면이 스스로 닫혔을 때(저장, 취소 등) 사무실로 돌아간다."""
        if self.page is host:
            self.page = None
            try:
                self._office_back()
            except tk.TclError:
                pass  # 프로그램을 끄는 중

    def route(self, seq, host, fn):
        if seq not in self.routes:
            self.routes[seq] = []
            self.root.bind_all(seq, lambda e, s=seq: self._dispatch(s, e), add='+')
        self.routes[seq].append((host, fn))

    def _dispatch(self, seq, e):
        w = str(e.widget)
        keep = []
        hit = None
        for host, fn in self.routes[seq]:
            if not host.winfo_exists():
                continue
            keep.append((host, fn))
            h = str(host)
            if hit is None and (w == h or w.startswith(h + '.')):
                hit = fn
        self.routes[seq] = keep
        return hit(e) if hit else None

    def toggle_theme(self):
        """밝은/다크 테마를 바꾼다. 색이 화면 곳곳에 박혀 있어 설정을 저장하고 창을 새로 띄운다."""
        q = T('밝은 테마로 바꿀까요?\n세션 사무실 창이 잠깐 닫혔다가 다시 열립니다.') if DARK else \
            T('다크 테마로 바꿀까요?\n세션 사무실 창이 잠깐 닫혔다가 다시 열립니다.')
        if not tkmsg.askyesno(T('테마 바꾸기'), q, parent=self.root):
            return
        self.cfg['theme'] = 'light' if DARK else 'dark'
        self.restart()

    def restart(self):
        """설정을 저장하고 같은 파이썬으로 새로 띄운 뒤 이 창을 닫는다 (테마·언어는 시작할 때 정해진다)."""
        self.save_cfg()
        env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_OFFICE_THEME', 'CLAUDE_OFFICE_LANG')}
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=APP_DIR, env=env)
        self.quit()

    def pick_lang(self, e=None):
        """언어 고르기 카드. 메뉴의 '언어' 칸 바로 아래(메뉴가 닫혀 있으면 화면 가운데)에 띄운다."""
        self.close_lang()
        Z, F = self.Z, self.fonts
        W, RH, HD = int(260 * Z), int(46 * Z), int(64 * Z)
        H = HD + RH * len(LANGS) + int(12 * Z)
        c = self.lang_card = tk.Canvas(self.root, width=W, height=H, bg=SURF, highlightthickness=1,
                                       highlightbackground=EDGE, bd=0, cursor='hand2')
        draw_icon(c, 'globe', 30 * Z, 30 * Z, 11 * Z, ACCENT, SURF)
        c.create_text(50 * Z, 22 * Z, text=T('언어'), font=F['h2'], fill=FG, anchor='w')
        c.create_text(50 * Z, 42 * Z, text=T('고르면 창이 다시 열려요'), font=F['small'], fill=DIM, anchor='w')
        c.create_line(16 * Z, HD - 6 * Z, W - 16 * Z, HD - 6 * Z, fill=EDGE)
        self._lang_rows = []
        for i, (code, name) in enumerate(LANGS):
            y0 = HD + i * RH
            self._lang_rows.append((y0, y0 + RH, code))
        self._lang_hover = None
        self._draw_lang_rows()
        c.bind('<Motion>', lambda ev: self._lang_motion(ev.y))
        c.bind('<Leave>', lambda ev: self._lang_motion(-1))
        c.bind('<Button-1>', self._lang_click)
        box = next((b for b in getattr(self, 'menu_boxes', []) if b[5] == 'globe'), None)
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        if self.menu_open and box:
            x = self.gm.winfo_rootx() - self.root.winfo_rootx() + box[0]
            y = self.gm.winfo_rooty() - self.root.winfo_rooty() + box[3] + 8 * Z
        else:
            x, y = (rw - W) / 2, (rh - H) / 2
        c.place(x=max(8, min(x, rw - W - 8)), y=max(8, min(y, rh - H - 8)), height=1)
        c.tk.call('raise', c._w)
        self.tw.run(220, lambda e: c.place_configure(height=max(1, int((H + 2) * e))), ease=ease_out, key='lang')
        self._lang_armed = False  # 카드를 연 그 클릭은 '밖 클릭'으로 치지 않는다
        self.root.after_idle(lambda: setattr(self, '_lang_armed', True))
        if not getattr(self, '_lang_bound', False):  # 카드 밖을 누르면 닫는다
            self._lang_bound = True
            self.root.bind_all('<Button-1>', self._lang_outside, add='+')

    def _draw_lang_rows(self):
        c, Z, F = self.lang_card, self.Z, self.fonts
        c.delete('row')
        W = int(c['width'])
        for y0, y1, code in self._lang_rows:
            on, hov = code == LANG, code == self._lang_hover
            if on or hov:
                round_rect(c, 10 * Z, y0 + 3 * Z, W - 10 * Z, y1 - 3 * Z, 10 * Z, fill=MINT if on else HOVER,
                           outline=ACCENT if on else '', width=max(1, int(Z)), tags='row')
            cx, cy, r = 32 * Z, (y0 + y1) / 2, 8 * Z
            if on:  # 고른 언어: 채운 원 + 흰 체크
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=ACCENT, outline=ACCENT, tags='row')
                c.create_line(cx - 4 * Z, cy, cx - 1 * Z, cy + 3 * Z, cx + 4 * Z, cy - 3 * Z, fill='#ffffff',
                              width=max(2, int(2 * Z)), capstyle='round', joinstyle='round', tags='row')
            else:
                c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=DIM, width=max(1, int(1.5 * Z)), tags='row')
            c.create_text(52 * Z, cy, text=dict(LANGS)[code], font=F['bodyb'] if on else F['body'],
                          fill=ACC_TEXT if on else FG, anchor='w', tags='row')
            if on:
                c.create_text(W - 24 * Z, cy, text=T('사용 중'), font=F['small'], fill=ACC_TEXT, anchor='e', tags='row')

    def _lang_row_at(self, y):
        return next((code for y0, y1, code in self._lang_rows if y0 <= y < y1), None)

    def _lang_motion(self, y):
        code = self._lang_row_at(y)
        if code != self._lang_hover:
            self._lang_hover = code
            self._draw_lang_rows()

    def _lang_click(self, e):
        code = self._lang_row_at(e.y)
        if code:
            self.close_lang()
            self.set_lang(code)

    def _lang_outside(self, e):
        c = getattr(self, 'lang_card', None)
        if c and self._lang_armed and str(e.widget) != str(c):
            self.close_lang()

    def close_lang(self):
        c = getattr(self, 'lang_card', None)
        if c:
            self.lang_card = None
            h0 = c.winfo_height()  # 위로 접히며 사라진다
            self.tw.run(150, lambda e: c.place_configure(height=max(1, int(h0 * (1 - e)))), c.destroy, ease=ease_in_out,
                        key=('lang_out', str(c)))
            return True
        return False

    def set_lang(self, code):
        if code == LANG:
            return
        self.cfg['lang'] = code
        self.restart()

    def toggle_popup(self):
        self.popup.set(not self.popup.get())
        self.save_cfg()
        self.toast(T('메뉴 기능을 별도 창으로 열어요') if self.popup.get() else T('메뉴 기능을 사무실 화면에서 바꿔 보여 줘요'))

    # ── 상단 막대 ──
    def _build_top(self, parent):
        F = self.fonts
        top = tk.Frame(parent, bg=TOPBG, padx=18, height=int(56 * self.Z))
        top.pack(side='top', fill='x')
        top.pack_propagate(False)
        self.top = top
        top.bind('<Configure>', lambda e: self.root.after_idle(self._fit_top) if not self.menu_open else None)
        brand = self.brand = tk.Frame(top, bg=TOPBG)
        brand.pack(side='left')
        tk.Label(brand, text='●', font=F['small'], bg=TOPBG, fg=ACCENT).pack(side='left', padx=(0, 6))
        tk.Label(brand, text='Claude Office', font=F['brand'], bg=TOPBG, fg=ACCENT).pack(side='left')
        self.brand_sub = tk.Label(brand, text=T('세션 사무실'), font=F['small'], bg=TOPBG, fg=DIM)
        self.brand_sub.pack(side='left', padx=(8, 0), pady=(3, 0))
        self.chips = {}
        for i, key in enumerate(('busy', 'wait', 'idle', 'sleep')):
            col = MODE_COL[key]
            cb, cf = chip_colors(col)
            l = tk.Label(top, text='', font=F['statb'], bg=cb, fg=cf, padx=10, pady=2)
            l.pack(side='left', padx=(20 if i == 0 else 6, 0))
            self.chips[key] = l
        # 오른쪽 위 조작부: 게임 메뉴를 열면 옆으로 밀려 사라진다
        self.menu_btn = self._btn(top, T('☰  메뉴'), self.toggle_menu, bg=ACCENT, fg='#ffffff', hover=ACC_DARK, font='menu')
        # 기능 이동은 왼쪽 막대와 ☰ 메뉴가 맡고, 여기엔 글자 크기만 둔다
        self.tr = tr = tk.Frame(top, bg=TOPBG)
        self._btn(tr, T('가-'), lambda: self.set_zoom(-1)).pack(side='left')
        self.zoom_lbl = tk.Label(tr, text='', font=F['stat'], bg=TOPBG, fg=DIM, width=5)
        self.zoom_lbl.pack(side='left')
        self._btn(tr, T('가+'), lambda: self.set_zoom(1)).pack(side='left')
        self.zoom_lbl.configure(text=f'{self.Z * 100:.0f}%')
        self.menu_btn.place(relx=1.0, rely=0.5, x=-18, anchor='e')
        self.root.after(50, lambda: self._place_tr(0))
        self.top_line = tk.Frame(parent, bg=EDGE, height=1)
        self.top_line.pack(side='top', fill='x')

    def _place_tr_now(self):
        mb = max(self.menu_btn.winfo_reqwidth(), self.menu_btn.winfo_width())
        self.tr.place(relx=1.0, rely=0.5, x=-(mb + 18 + int(16 * self.Z)), anchor='e')

    def _place_tr(self, off):
        mb = max(self.menu_btn.winfo_reqwidth(), self.menu_btn.winfo_width())
        self.tr.place(relx=1.0, rely=0.5, x=-(mb + 18 + int(16 * self.Z)) + off, anchor='e')
        self.menu_btn.lift()
        if off == 0:
            self.root.after_idle(self._fit_top)

    def _fit_top(self):
        """창이 좁거나 글자가 커서 겹치면 상태 칩(뒤에서부터) → 부제목 순으로 숨긴다."""
        if not getattr(self, 'tr', None) or not self.tr.winfo_ismapped():
            return
        self.brand_sub.pack(side='left', padx=(8, 0), pady=(3, 0))
        keys = ('busy', 'wait', 'idle', 'sleep')
        for k in keys:
            self.chips[k].pack(side='left', padx=(20 if k == 'busy' else 6, 0))
        self._place_tr_now()
        self.top.update_idletasks()

        def right():
            return self.tr.winfo_x() - 12

        for k in reversed(keys[1:]):
            l = self.chips[k]
            if l.winfo_x() + l.winfo_width() > right():
                l.pack_forget()
                self.top.update_idletasks()
        if self.brand.winfo_x() + self.brand.winfo_width() > right():
            self.brand_sub.pack_forget()

    # ── 왼쪽 아이콘 막대 ──
    def side_items(self):
        with self.mon.lock:
            unread = sum(1 for m in self.mon.chat.values() if m['ts'] > self.chat_seen and m['from'] != 'user')
        if self.chatwin and self.chatwin.win.winfo_exists():
            unread = 0
        return [('menu', T('메뉴'), self.toggle_menu, 0), ('home', T('사무실'), lambda: (self.toggle_menu(False), self.show_office()), 0),
                ('chat', T('세션톡'), self.open_chat, unread), ('star', T('비서'), self.open_secretary, len(self.sec_items())),
                ('meet', T('회의실'), self.open_meeting, 0),
                ('phone', T('원격'), self.open_remote, 0),
                ('power', T('세션 켜기'), self.open_launcher, 0), ('folder', T('저장 공간'), self.open_storage, 0)]

    def draw_side(self, force=False):
        items = self.side_items()
        sig = (tuple((k, n) for k, _l, _f, n in items), self.side_hover, self.Z, self.menu_open, self.page_key, self._side_py,
               self.side.winfo_height())
        if sig == self.side_sig and not force:
            return
        self.side_sig = sig
        c, Z, F = self.side, self.Z, self.fonts
        c.delete('all')
        W = int(84 * Z)
        # 칸이 많거나 창이 낮으면 칸 높이를 줄여 모두 보이게 한다
        h = max(44 * Z, min(66 * Z, (max(c.winfo_height(), 200) - 14 * Z) / max(1, len(items)) - 6 * Z))
        k = h / (66 * Z)
        c.configure(width=W)
        self.side_boxes = []
        # 고른 칸 표시(본문 바탕색으로 이어 붙인 둥근 칸)는 칸을 바꾸면 새 자리로 미끄러져 간다
        sel = next((i for i, it in enumerate(items) if it[0] == ('menu' if self.menu_open else self.page_key if self.page else 'home')), 0)
        goal = 14 * Z + sel * (h + 6 * Z)
        if self._side_py is None or getattr(self, '_side_z', None) != (Z, h):
            self._side_py, self._side_z, self._side_goal = goal, (Z, h), goal
        elif goal != getattr(self, '_side_goal', goal):
            self._side_goal, start = goal, self._side_py

            def step(e, start=start, goal=goal):
                self._side_py = start + (goal - start) * e
                self.draw_side(force=True)

            self.tw.run(260, step, ease=ease_out, key='side')
        py = self._side_py
        if self.side_hover is not None and self.side_hover != sel:
            hy = 14 * Z + self.side_hover * (h + 6 * Z)
            round_rect(c, 5 * Z, hy, W - 5 * Z, hy + h, 16 * Z, fill=SIDE_HOVER, outline='')
        round_rect(c, 5 * Z, py, W + 30 * Z, py + h, 18 * Z, fill=BG, outline='')
        y = 14 * Z
        for i, (key, label, fn, n) in enumerate(items):
            on = abs(py - y) < h / 2
            col = (ACCENT if DARK else SIDE) if on else '#ffffff'
            wrap = F['tiny'].measure(label) > W - 12 * Z  # 긴 이름(번역)은 두 줄로
            draw_icon(c, key, W / 2, y + (22 if wrap else 26) * Z * k, 13 * Z * min(1, k + 0.1), col, BG if on else SIDE)
            c.create_text(W / 2, y + h - (16 if wrap else 14) * Z * k, text=label, font=F['tiny'], fill=ACC_TEXT if on else '#eafff6',
                          width=W - 12 * Z, justify='center')
            if n:
                c.create_oval(W / 2 + 10 * Z, y + 8 * Z, W / 2 + 19 * Z, y + 17 * Z, fill=RED, outline='#ffffff', width=Z)
            self.side_boxes.append((y, y + h, fn))
            y += h + 6 * Z

    def _side_at(self, e):
        if e is None:
            return None
        for i, (y0, y1, _fn) in enumerate(getattr(self, 'side_boxes', [])):
            if y0 <= e.y < y1:
                return i
        return None

    def _side_motion(self, e):
        i = self._side_at(e)
        self.side.configure(cursor='hand2' if i is not None else '')
        if i != self.side_hover:
            self.side_hover = i
            self.draw_side()

    def _side_click(self, e):
        i = self._side_at(e)
        if i is not None:
            self.side_boxes[i][2]()

    # ── 게임식 큰 메뉴 ──
    def menu_tiles(self):
        n_chat = self.side_items()[2][3]
        has_sec = bool(self.sec_sid)
        tiles = [('home', T('사무실'), lambda: (self.toggle_menu(False), self.show_office()), '', 0),
                 ('chat', T('세션톡'), self.open_chat, '', n_chat),
                 ('star', T('비서 제안'), self.open_secretary, '', len(self.sec_items())),
                 ('spark', T('브리핑 받기') if has_sec else T('비서 만들기'), self.ask_brief if has_sec else self.create_secretary,
                  T('브리핑 중') if self.briefing() else '', 0),
                 ('meet', T('회의실'), self.open_meeting, '', 0),
                 ('phone', T('원격 조종'), self.open_remote, T('켬') if self.remote and self.remote.httpd else '', 0),
                 ('power', T('세션 켜기'), self.open_launcher, '', 0),
                 ('user', T('아바타'), lambda: self.open_avatar(self.sel), '', 0),
                 ('tab', T('새 탭'), lambda: (self.toggle_menu(False), self._tab_edit(None)), '', 0),
                 ('folder', T('저장 공간'), self.open_storage, '', 0),
                 ('zoomin', T('글자 크게'), lambda: self.set_zoom(1), f'{self.Z * 100:.0f}%', 0),
                 ('zoomout', T('글자 작게'), lambda: self.set_zoom(-1), '', 0),
                 ('pin', T('항상 위'), lambda: (self.topmost.set(not self.topmost.get()), self.toggle_top()),
                  T('켬') if self.topmost.get() else T('끔'), 0),
                 ('play', T('움직임'), lambda: self.anim_on.set(not self.anim_on.get()), T('켬') if self.anim_on.get() else T('끔'), 0),
                 ('type', T('대사 속도'), self.cycle_type_speed, TYPE_SPEED_TEXT[self.type_speed()], 0),
                 ('plug', T('세션 연결 mod'), lambda: (self.toggle_menu(False), self.mod_dialog()),
                  {'ok': T('켬'), 'old': T('업데이트')}.get(mod_state(), T('끔')), 0),
                 ('window', T('창으로 열기'), self.toggle_popup, T('켬') if self.popup.get() else T('끔'), 0),
                 ('moon', T('다크 테마'), self.toggle_theme, T('켬') if DARK else T('끔'), 0),
                 ('globe', T('언어'), self.pick_lang, dict(LANGS)[LANG], 0)]
        return tiles

    def _menu_geom(self):
        """(타일 크기, 간격, 줄 수, 메뉴 높이, 제목 폭) — 창 폭에 맞춘다."""
        Z = self.Z
        W = max(400, self.main.winfo_width())
        title_w = 170 * Z
        gap = 14 * Z
        n = len(self.menu_tiles())
        avail = W - title_w - 30 * Z
        t = min(92 * Z, avail / n - gap)
        rows = 1
        if t < 72 * Z:  # 좁으면 두 줄
            rows = 2
            t = min(92 * Z, avail / math.ceil(n / rows) - gap)
        return t, gap, rows, rows * (t + gap) + 30 * Z, title_w

    def draw_menu(self):
        c, Z, F = self.gm, self.Z, self.fonts
        if not self.menu_open and not getattr(self, '_menu_anim_on', False):
            return
        c.delete('all')
        W = max(400, self.main.winfo_width())
        t, gap, rows, H, title_w = self._menu_geom()
        gradient(c, 0, 0, W, H, *GRAD, step=4)
        c.create_rectangle(0, H - 3 * Z, W, H, fill=mix(GRAD[1], '#000000', 0.25), outline='')  # 아래 가장자리
        c.create_text(26 * Z, 22 * Z, text=T('메뉴'), anchor='nw', font=F['h1'], fill='#ffffff')
        c.create_text(26 * Z, 52 * Z, text=T('모든 기능을 한눈에\nEsc 또는 ✕ 로 닫기'), anchor='nw', font=F['small'], fill='#eafff6')
        tiles = self.menu_tiles()
        per = math.ceil(len(tiles) / rows)
        self.menu_boxes = []
        for i, (icon, label, fn, state, badge) in enumerate(tiles):
            col, row = i % per, i // per
            x0 = title_w + col * (t + gap)
            y0 = 16 * Z + row * (t + gap)
            lift = 4 * Z if i == self.menu_hover else 0
            round_rect(c, x0 + 2 * Z, y0 + 5 * Z, x0 + t + 2 * Z, y0 + t + 5 * Z, 18 * Z, fill=mix(GRAD[0], '#000000', 0.18),
                       outline='')
            round_rect(c, x0, y0 - lift, x0 + t, y0 + t - lift, 18 * Z, fill=SURF,
                       outline=ACCENT if i == self.menu_hover else '', width=2 * Z)
            draw_icon(c, icon, x0 + t / 2, y0 + t * 0.38 - lift, t * 0.17, ACCENT, SURF)
            fnt = F['menu'] if F['menu'].measure(label) <= t - 12 * Z else F['tiny']  # 긴 번역은 작은 글씨, 그래도 길면 두 줄
            lt = c.create_text(x0 + t / 2, y0 + t * 0.72 - lift, text=label, font=fnt, fill=FG, width=t - 10 * Z, justify='center')
            if state:  # 두 줄로 접힌 이름이 아래 상태 글자와 겹치지 않게 위로 올린다
                over = c.bbox(lt)[3] - (y0 + t * 0.81 - lift)
                if over > 0:
                    c.move(lt, 0, -min(over, t * 0.1))
                c.create_text(x0 + t / 2, y0 + t * 0.88 - lift, text=state, font=F['tiny'], fill=DIM)
            if badge:
                r = 11 * Z
                c.create_oval(x0 + t - r * 1.6, y0 - r * 0.6 - lift, x0 + t + r * 0.4, y0 + r * 1.4 - lift, fill=RED,
                              outline='#ffffff', width=2 * Z)
                c.create_text(x0 + t - r * 0.6, y0 + r * 0.4 - lift, text=str(min(badge, 99)), font=F['tiny'], fill='#ffffff')
            self.menu_boxes.append((x0, y0, x0 + t, y0 + t, fn, icon))
        if self.menu_off:
            c.move('all', 0, self.menu_off)

    def _menu_at(self, e):
        if e is None:
            return None
        for i, (x0, y0, x1, y1, _fn, _ic) in enumerate(self.menu_boxes):
            if x0 <= e.x < x1 and y0 <= e.y < y1:
                return i
        return None

    def _menu_motion(self, e):
        i = self._menu_at(e)
        self.gm.configure(cursor='hand2' if i is not None else '')
        if i != self.menu_hover:
            self.menu_hover = i
            self.draw_menu()

    def _menu_click(self, e):
        i = self._menu_at(e)
        if i is None:
            return
        _x0, _y0, _x1, _y1, fn, icon = self.menu_boxes[i]
        if icon in ('chat', 'star', 'meet', 'phone', 'power', 'user', 'folder', 'spark'):
            self.toggle_menu(False)  # 창을 여는 칸은 메뉴를 닫고 연다
        fn()
        if self.menu_open:
            self.draw_menu()

    def toggle_menu(self, open_=None):
        open_ = (not self.menu_open) if open_ is None else open_
        if open_ == self.menu_open:
            return
        self.menu_open = open_
        self.menu_btn.configure(text=T('✕  닫기') if open_ else T('☰  메뉴'))
        self.menu_hover = None
        if open_ and not self.gm.winfo_ismapped():
            # 아래 화면을 밀지 않고 상단 막대 바로 아래에 덮어 띄운다
            self.menu_p = 0.0
            self.menu_off = 1 - int(self._menu_geom()[3])
            self.gm.place(in_=self.main, x=0, y=self.top.winfo_height() + 1, relwidth=1, height=1)
            self.gm.tk.call('raise', self.gm._w)  # Canvas.lift는 그림 순서용이라 창 순서는 raise로
            self.draw_menu()
        self._menu_anim_on = True
        self.draw_side()
        start, goal = self.menu_p, 1.0 if open_ else 0.0
        # 펼칠 땐 빠르게 나와 천천히 멈추고, 접을 땐 조금 더 빨리 부드럽게 (도중에 다시 누르면 그 자리에서 되돌아간다)
        self.tw.run(int((260 if open_ else 190) * max(0.35, abs(goal - start))), lambda e: self._menu_set(start + (goal - start) * e),
                    self._menu_done, ease=ease_out if open_ else ease_in_out, key='menu')

    def _menu_set(self, p):
        self.menu_p = p
        H = int(self._menu_geom()[3])
        h = max(1, int(H * p))
        self.gm.place_configure(height=h)
        off = h - H  # 위에서 아래로 미끄러져 내려오게 그림을 같이 움직인다
        self.gm.move('all', 0, off - self.menu_off)
        self.menu_off = off
        self._place_tr(int((self.tr.winfo_width() + 60) * p))

    def _menu_done(self):
        self._menu_anim_on = False
        if self.menu_open:
            self.tr.place_forget()
        else:
            self.gm.place_forget()
            self._place_tr(0)

    # ── 탭 (세션 묶음) ──
    def _build_tabbar(self, parent):
        bar = tk.Frame(parent, bg=TOPBG)
        bar.pack(side='top', fill='x')
        self.tabbar = tk.Frame(bar, bg=TOPBG)
        self.tabbar.pack(side='left', padx=(8, 0))
        self.res = tk.Label(bar, text='', font=self.fonts['small'], bg=TOPBG, fg=DIM)
        self.res.pack(side='right', padx=(6, 12))
        self.stat = tk.Label(bar, text='', font=self.fonts['small'], bg=TOPBG, fg=TEXT2)
        self.stat.pack(side='right', padx=6)
        tk.Frame(parent, bg=EDGE, height=1).pack(side='top', fill='x')
        self.tab_entry = None

    def group_of(self, name):
        return next((g for g in self.groups if g['name'] == name), None) if name else None

    def update_tabbar(self, sessions, force=False):
        live = {s.sid for s in sessions if s.sid != self.sec_sid}
        tabs = [('', T('전체'), len(live))] + [(g['name'], g['name'], len(live & set(g['sids']))) for g in self.groups]
        sig = (tuple(tabs), self.group, self.tab_entry is not None)
        if sig == self.tab_sig and not force:
            return
        shape = (tuple(k for k, _l, _n in tabs), self.tab_entry is not None)
        F = self.fonts
        if force or shape != getattr(self, 'tab_shape', None):
            # 탭이 늘거나 줄었을 때만 다시 만든다 (고르기·숫자 변화는 색과 글자만 바꿔 막대가 튀지 않게)
            self.tab_shape = shape
            for w in self.tabbar.winfo_children():
                if w is not self.tab_entry:
                    w.destroy()
            self.tab_ws = {}
            for key, label, n in tabs:
                f = tk.Frame(self.tabbar, bg=TOPBG)
                f.pack(side='left', padx=(0, 2))
                l = tk.Label(f, text='', font=F['bodyb'], bg=TOPBG, padx=14, pady=7, cursor='hand2')
                l.pack()
                line = tk.Frame(f, bg=TOPBG, height=3)
                line.pack(fill='x')
                l._group = key
                l.bind('<Button-1>', lambda e, k=key: self.set_group(k))
                l.bind('<Enter>', lambda e, l=l: l.configure(fg=ACC_TEXT))
                l.bind('<Leave>', lambda e, l=l, k=key: l.configure(fg=ACC_TEXT if k == self.group else DIM))
                if key:
                    l.bind('<Button-3>', lambda e, k=key: self._tab_menu(e, k))
                self.tab_ws[key] = (l, line)
            if self.tab_entry is not None:
                self.tab_entry.lift()
                self.tab_entry.pack_forget()
                self.tab_entry.pack(side='left', padx=6)
            else:
                plus = tk.Label(self.tabbar, text=T('＋ 새 탭'), font=F['bodyb'], bg=TOPBG, fg=ACCENT, padx=12, pady=7, cursor='hand2')
                plus.pack(side='left')
                plus._group = None
                plus.bind('<Button-1>', lambda e: self._tab_edit(None))
        self.tab_sig = sig
        for key, label, n in tabs:
            l, line = self.tab_ws[key]
            on = key == self.group
            l.configure(text=f'{label}  {n}', fg=ACC_TEXT if on else DIM)
            line.configure(bg=ACCENT if on else TOPBG)
        self._tab_highlight()

    def _tab_highlight(self):
        """책상을 끄는 동안 포인터 아래 탭만 밝힌다 (막대를 다시 만들면 놓을 자리가 사라지므로 색만 바꾼다)."""
        for f in self.tabbar.winfo_children():
            for l in f.winfo_children():
                key = getattr(l, '_group', None)
                if isinstance(l, tk.Label) and key is not None:
                    l.configure(bg=HOVER if key and key == self.tab_drop else TOPBG)

    def _tab_edit(self, old, then_add=None):
        """탭 막대 안에서 이름을 입력받는다. old=None 이면 새 탭, then_add = 만든 뒤 넣을 세션."""
        if self.tab_entry is not None:
            self.tab_entry.destroy()
        e = tk.Entry(self.tabbar, font=self.fonts['body'], bg=CARD2, fg=FG, insertbackground=FG, relief='flat', width=14,
                     highlightthickness=1, highlightcolor=ACCENT, highlightbackground=ACCENT)
        self.tab_entry = e
        e.insert(0, old or '')
        e.select_range(0, 'end')

        def done(save):
            if self.tab_entry is not e:
                return
            name = one_line(e.get(), 20)
            self.tab_entry = None
            e.destroy()
            if save and name:
                if old:
                    self.rename_group(old, name)
                else:
                    self.new_group(name, then_add, switch=then_add is None)
            self.update_tabbar(self.sessions, force=True)

        e.bind('<Return>', lambda ev: done(True))
        e.bind('<KP_Enter>', lambda ev: done(True))
        e.bind('<Escape>', lambda ev: done(False))
        e.bind('<FocusOut>', lambda ev: done(True))
        self.update_tabbar(self.sessions, force=True)
        e.focus_set()

    def _unique_group(self, name):
        base, i = name, 2
        while self.group_of(name):
            name = f'{base} {i}'
            i += 1
        return name

    def new_group(self, name, sid=None, switch=False):
        name = self._unique_group(name)
        self.groups.append({'name': name, 'sids': [sid] if sid else []})
        if switch:
            self.group = name
        self._groups_changed()

    def rename_group(self, old, new):
        g = self.group_of(old)
        if not g or new == old:
            return
        g['name'] = self._unique_group(new)
        if self.group == old:
            self.group = g['name']
        self._groups_changed()

    def delete_group(self, name):
        self.groups = [g for g in self.groups if g['name'] != name]
        if self.group == name:
            self.group = ''
        self._groups_changed()

    def shift_group(self, name, step):
        i = next(i for i, g in enumerate(self.groups) if g['name'] == name)
        j = max(0, min(len(self.groups) - 1, i + step))
        self.groups.insert(j, self.groups.pop(i))
        self._groups_changed()

    def toggle_member(self, name, sid, on=None):
        g = self.group_of(name)
        if not g:
            return
        has = sid in g['sids']
        if on is None:
            on = not has
        if on and not has:
            g['sids'].append(sid)
        elif not on and has:
            g['sids'].remove(sid)
        self._groups_changed()

    def set_group(self, name):
        if name == self.group:
            return
        self.group = name
        self.save_cfg()
        self.layout(self.sessions, force=True)
        self.update_tabbar(self.sessions)

    def _groups_changed(self):
        self.save_cfg()
        self.layout(self.sessions, force=True)
        self.update_tabbar(self.sessions, force=True)

    def _tab_menu(self, e, name):
        i = next(i for i, g in enumerate(self.groups) if g['name'] == name)
        m = tk.Menu(self.root, tearoff=0, font=self.fonts['small'])
        m.add_command(label=T('이름 바꾸기'), command=lambda: self._tab_edit(name))
        m.add_command(label=T('왼쪽으로'), command=lambda: self.shift_group(name, -1), state='normal' if i else 'disabled')
        m.add_command(label=T('오른쪽으로'), command=lambda: self.shift_group(name, 1),
                      state='normal' if i < len(self.groups) - 1 else 'disabled')
        m.add_separator()
        m.add_command(label=T('탭 지우기 (세션은 그대로)'), command=lambda: self.delete_group(name))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def set_zoom(self, dz, reset=False):
        i = ZOOMS.index(self.Z)
        ni = ZOOMS.index(1.25) if reset else max(0, min(len(ZOOMS) - 1, i + dz))
        if ZOOMS[ni] == self.Z:
            return
        self.Z = ZOOMS[ni]
        for k, (size, _w) in FONT_SPECS.items():
            self.fonts[k].configure(size=self._fs(size))
        self.fonts['bodyi'].configure(size=self._fs(10))
        self.fonts['mono'].configure(size=self._fs(10))
        self.zoom_lbl.configure(text=f'{self.Z * 100:.0f}%')
        self.top.configure(height=int(56 * self.Z))
        self.root.after(120, lambda: self._place_tr(0) if not self.menu_open else None)
        self.draw_side(force=True)
        if self.menu_open:
            self.gm.place_configure(height=int(self._menu_geom()[3]))
            self.draw_menu()
        self.frames.clear()
        self.relayout(force=True)
        self.panel_ver = -1
        self.bar_vals = None
        self.head_sig = None
        self._apply_log_layout()
        self._on_panel_resize()
        self.update_panel()

    # ── 오른쪽 상세 패널 ──
    def _card(self, parent, title, expand=False):
        outer = tk.Frame(parent, bg=CARD, padx=16, pady=12, highlightthickness=1, highlightbackground=EDGE)
        outer.pack(fill='both' if expand else 'x', expand=expand, pady=(10, 0))
        tk.Label(outer, text=title, font=self.fonts['h2'], bg=CARD, fg=FG, anchor='w').pack(fill='x', pady=(0, 8))
        return outer

    def _build_panel(self, outer):
        F = self.fonts
        pn = tk.Frame(outer, bg=BG, padx=14, pady=12)
        pn.pack(fill='both', expand=True)
        # 머리: 초록→파랑 프로필 카드 (아바타, 이름, 상태, 버튼)
        self.p_head = tk.Canvas(pn, height=int(150 * self.Z), bg=GRAD[0], highlightthickness=0, cursor='')
        self.p_head.pack(fill='x')
        self.p_head.bind('<Configure>', lambda e: self.draw_head(force=True))
        self.p_head.bind('<Button-1>', self._head_click)
        self.p_head.bind('<Motion>', lambda e: self.p_head.configure(cursor='hand2' if self._head_hit(e) else ''))
        self.p_entry = tk.Entry(self.p_head, font=F['h2'], bg=SURF, fg=FG, insertbackground=FG, relief='flat',
                                highlightthickness=2, highlightcolor=SURF, highlightbackground=SURF)
        self.p_entry.bind('<Return>', lambda e: self.rename_end(True))
        self.p_entry.bind('<KP_Enter>', lambda e: self.rename_end(True))
        self.p_entry.bind('<Escape>', lambda e: self.rename_end(False))
        self.renaming = None

        tabs = tk.Frame(pn, bg=BG)
        tabs.pack(fill='x', pady=(12, 0))
        self.tab_btns = {}
        for key, label in (('ov', T('개요')), ('log', T('작업 로그')), ('qa', T('대화'))):
            b = tk.Label(tabs, text=label, font=F['h2'], padx=18, pady=5, cursor='hand2')
            b.pack(side='left', padx=(0, 4))
            b.bind('<Button-1>', lambda e, k=key: self.set_tab(k))
            self.tab_btns[key] = b
        tk.Frame(pn, bg=ACCENT, height=2).pack(fill='x')
        self.tab_frames = {k: tk.Frame(pn, bg=BG) for k in ('ov', 'log', 'qa')}
        self.unseen = 0
        pn = self.tab_frames['ov']

        card = self._card(pn, T('세션 정보'))
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill='x')
        self.p_info = {}
        for key, r, col, span in ((T('프로세스'), 0, 0, 2), (T('모델'), 1, 0, 1), (T('시작'), 1, 1, 1), (T('컨텍스트'), 2, 0, 1), (T('대화 기록'), 2, 1, 1)):
            tk.Label(grid, text=key, font=F['small'], bg=CARD, fg=DIM, anchor='w').grid(row=r * 2, column=col, columnspan=span, sticky='w')
            v = tk.Label(grid, text='-', font=F['body'], bg=CARD, fg=FG, anchor='w', justify='left')
            v.grid(row=r * 2 + 1, column=col, columnspan=span, sticky='w', pady=(0, 4), padx=(0, 16))
            self.p_info[key] = v
        v = self.p_info[T('대화 기록')]
        v.configure(cursor='hand2', fg='#82b4ff')
        v.bind('<Button-1>', lambda e: self.p_path and open_folder(self.p_path, select=True))
        grid.columnconfigure(0, weight=1, uniform='i')
        grid.columnconfigure(1, weight=1, uniform='i')

        card = self._card(pn, T('토큰 사용량'))
        tiles = tk.Frame(card, bg=CARD)
        tiles.pack(fill='x')
        self.p_tiles = {}
        for i, (key, label) in enumerate((('t_in', T('오늘 입력')), ('t_out', T('오늘 출력')), ('a_in', T('전체 입력')), ('a_out', T('전체 출력')))):
            f = tk.Frame(tiles, bg=CARD2, padx=10, pady=6)
            f.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 6, 0))
            tiles.columnconfigure(i, weight=1, uniform='t')
            tk.Label(f, text=label, font=F['small'], bg=CARD2, fg=DIM, anchor='w').pack(fill='x')
            v = tk.Label(f, text='-', font=F['num'], bg=CARD2, fg=FG, anchor='w')
            v.pack(fill='x')
            self.p_tiles[key] = v
        self.p_bar_title = tk.Label(card, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w')
        self.p_bar_title.pack(fill='x', pady=(8, 2))
        self.p_bar = tk.Canvas(card, height=14, bg=CARD, highlightthickness=0)
        self.p_bar.pack(fill='x')
        self.p_bar.bind('<Configure>', lambda e: self.draw_bar())
        lg = tk.Frame(card, bg=CARD)
        lg.pack(fill='x', pady=(4, 0))
        self.p_legend = []
        for i, (name, col) in enumerate(TOK_PARTS):
            l = tk.Label(lg, text='', font=F['small'], bg=CARD, fg=col, anchor='w')
            l.grid(row=i // 2, column=i % 2, sticky='w')
            lg.columnconfigure(i % 2, weight=1, uniform='l')
            self.p_legend.append(l)

        card = self._card(pn, T('목표 · 작업 분배'))
        self.p_tasks = tk.Text(card, height=7, wrap='word', bg=CARD, fg=FG, relief='flat', bd=0, font=F['body'],
                               cursor='arrow', insertwidth=0, spacing1=2, spacing3=2)
        self.p_tasks.pack(fill='x')

        pn = self.tab_frames['log']
        card = self._card(pn, T('작업 로그'), expand=True)
        fb = tk.Frame(card, bg=CARD)
        fb.pack(fill='x', pady=(0, 6))
        self.log_btns = {}
        for k, label in LOG_KINDS:
            b = tk.Label(fb, text=label, font=F['small'], padx=9, pady=1, cursor='hand2')
            b.pack(side='left', padx=(0, 5))
            b.bind('<Button-1>', lambda e, k=k: self.set_filter(k))
            self.log_btns[k] = b
        lf = tk.Frame(card, bg=CARD)
        lf.pack(fill='both', expand=True)
        self.p_log = tk.Text(lf, wrap='word', bg=ROOM['log_bg'], fg=FG, relief='flat', bd=0, font=F['log'],
                             padx=10, pady=8, cursor='arrow', insertwidth=0, spacing1=3, spacing3=2)
        sb = tk.Scrollbar(lf, command=self.p_log.yview)
        self.p_log.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.p_log.pack(side='left', fill='both', expand=True)
        for w in (self.p_tasks, self.p_log):
            w.tag_configure('dim', foreground=DIM)
            w.tag_configure('done', foreground='#2fa36b')
            w.tag_configure('doing', foreground=_t('#2f7fd8', '#6aa8ff'))
            w.tag_configure('todo', foreground='#9aa5b1')
            w.tag_configure('bold', font=F['bodyb'])
            for k, col in LOG_COL.items():
                w.tag_configure(k, foreground=col)
            w.bind('<Key>', lambda e: None if (e.state & 4 and e.keysym.lower() in ('c', 'a')) else 'break')
        self.p_log.tag_configure('user', font=F['logb'])

        card = self._card(self.tab_frames['qa'], T('대화 · 요청과 마지막 답변'), expand=True)
        tk.Label(card, text=T('직접 보낸 요청(Q)과 그 작업의 마지막 답변(A) · 최근 {0}개까지').format(QA_KEEP), font=F['small'],
                 bg=CARD, fg=DIM, anchor='w').pack(fill='x', pady=(0, 6))
        qf = tk.Frame(card, bg=CARD)
        qf.pack(fill='both', expand=True)
        self.p_qa = tk.Text(qf, wrap='word', bg=ROOM['log_bg'], fg=FG, relief='flat', bd=0, font=F['log'],
                            padx=12, pady=10, cursor='arrow', insertwidth=0, spacing1=2, spacing3=2)
        sb = tk.Scrollbar(qf, command=self.p_qa.yview)
        self.p_qa.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.p_qa.pack(side='left', fill='both', expand=True)
        w = self.p_qa
        w.tag_configure('qb', background=ACCENT, foreground='#ffffff', font=F['logb'])
        w.tag_configure('ab', background=BLUE, foreground='#ffffff', font=F['logb'])
        w.tag_configure('time', foreground=DIM)
        w.tag_configure('q', font=F['logb'], foreground=FG)
        w.tag_configure('wait', foreground=DIM)
        w.tag_configure('sep', foreground=EDGE)
        w.bind('<Key>', lambda e: None if (e.state & 4 and e.keysym.lower() in ('c', 'a')) else 'break')
        self.qa_keys, self.qa_sid, self.qa_ver, self.qa_dirty = [], None, -1, False
        self._apply_log_layout()
        self.set_filter('all')
        self.set_tab(self.cfg.get('tab', 'ov'))

    def set_tab(self, k):
        self.tab = k
        for key, f in self.tab_frames.items():
            if key == k:
                f.pack(fill='both', expand=True)
            else:
                f.pack_forget()
        for key, b in self.tab_btns.items():
            if b.cget('bg') in (ACCENT, CARD2):
                self.fade_bg(b, ACCENT if key == k else CARD2, 180, fg='#ffffff' if key == k else FG)
            else:  # 처음 그릴 때
                b.configure(bg=ACCENT if key == k else CARD2, fg='#ffffff' if key == k else FG)
        if k == 'log':
            self.unseen = 0
            self.p_log.see('end')
        self._tab_label()

    def _tab_label(self):
        self.tab_btns['log'].configure(text=T('작업 로그') + (f'  +{self.unseen}' if self.unseen else ''))

    def _apply_log_layout(self):
        # 줄이 넘칠 때 시각 뒤로 들여쓰기
        w = self.fonts['log'].measure('00:00:00 ⚙ ')
        self.p_log.tag_configure('hang', lmargin1=0, lmargin2=w)
        ind = int(16 * self.Z)
        self.p_qa.tag_configure('body', lmargin1=ind, lmargin2=ind, spacing3=int(6 * self.Z))
        self.p_bar.configure(height=int(14 * self.Z))

    def _on_panel_resize(self, e=None):
        self.draw_head()

    def draw_head(self, force=False):
        """패널 머리 프로필 카드. 바뀐 것이 있을 때만 다시 그린다."""
        c, Z, F, h = self.p_head, self.Z, self.fonts, self.head
        W = c.winfo_width()
        sig = (W, self.Z, self.renaming, tuple(sorted((k, v) for k, v in h.items() if k != 'img')), id(h.get('img')))
        if sig == self.head_sig and not force:
            return
        self.head_sig = sig
        c.delete('all')
        self.head_hits = []
        H = int(c['height'])
        gradient(c, 0, 0, W, H + 2, *GRAD)
        # 오른쪽 위 은은한 원 장식
        c.create_oval(W - 120 * Z, -60 * Z, W + 40 * Z, 90 * Z, fill=mix(GRAD[1], '#ffffff', 0.12), outline='')
        r = 42 * Z
        ax, ay = 18 * Z, 18 * Z
        c.create_oval(ax, ay, ax + 2 * r, ay + 2 * r, fill='#ffffff', outline=mix(GRAD[0], '#ffffff', 0.5), width=3 * Z)
        if h.get('img'):
            c.create_image(ax + r, ay + r + 3 * Z, image=h['img'])
        self.head_hits.append((ax, ay, ax + 2 * r, ay + 2 * r, lambda: self.open_avatar(self.sel)))
        x = ax + 2 * r + 16 * Z
        tw = max(80, W - x - 16 * Z)
        if not h:
            c.create_text(x, ay + r, text=T('캐릭터를 클릭하세요'), anchor='w', font=F['h1'], fill='#ffffff')
            return
        y = ay + 2 * Z
        if self.renaming:
            c.create_window(x, y, window=self.p_entry, anchor='nw', width=tw)
            self.p_entry.update_idletasks()
            y += self.p_entry.winfo_reqheight() + 6 * Z
        else:
            t = c.create_text(x, y, text=h.get('name', ''), anchor='nw', font=F['h1'], fill='#ffffff', width=tw)
            y = c.bbox(t)[3] + 6 * Z
        mode = h.get('mode', 'idle')
        chip = MODE_TEXT[mode] + (f' · {h["waiting"]}' if h.get('waiting') else '')
        t = c.create_text(x + 10 * Z, y + 2 * Z, text=chip, anchor='nw', font=F['statb'], fill=MODE_COL[mode] if DARK else shade(MODE_COL[mode], 0.8))
        bb = c.bbox(t)
        c.tag_lower(round_rect(c, bb[0] - 10 * Z, bb[1] - 3 * Z, bb[2] + 10 * Z, bb[3] + 3 * Z, 10 * Z, fill=SURF,
                               outline=''), t)
        since = T('Enter 저장 · Esc 취소 · 비우면 원래 이름') if self.renaming else h.get('since', '')
        if since:
            c.create_text(bb[2] + 20 * Z, (bb[1] + bb[3]) / 2, text=since, anchor='w', font=F['small'], fill='#f0fffa')
        y = bb[3] + 8 * Z
        t = c.create_text(x, y, text=fit(h.get('sub', ''), F['small'], tw), anchor='nw', font=F['small'], fill='#e3fbf2')
        y = c.bbox(t)[3] + 10 * Z
        bx, box = x, None
        for label, fn in ((T('✔ 저장') if self.renaming else T('✎ 이름 바꾸기'), self.rename_click),
                          (T('꾸미기'), lambda: self.open_avatar(self.sel)), (T('세션톡에서 명령'), lambda: self.open_chat(self.sel))):
            bw = F['small'].measure(label) + 24 * Z
            if bx > x and bx + bw > W - 12 * Z:  # 자리가 모자라면 다음 줄로
                bx = x
                y = box[3] + 8 * Z
            t = c.create_text(bx + 12 * Z, y + 4 * Z, text=label, anchor='nw', font=F['small'], fill='#ffffff')
            bb = c.bbox(t)
            box = (bb[0] - 12 * Z, bb[1] - 4 * Z, bb[2] + 12 * Z, bb[3] + 4 * Z)
            c.tag_lower(round_rect(c, *box, 12 * Z, fill=mix(GRAD[0], '#ffffff', 0.18), outline='#ffffff', width=Z), t)
            self.head_hits.append(box + (fn,))
            bx = box[2] + 8 * Z
        need = int(max(ay + 2 * r, y + (bb[3] - bb[1]) + 8 * Z) + 18 * Z)
        if need != H:
            c.configure(height=need)  # <Configure>가 다시 그린다

    def _head_hit(self, e):
        for x0, y0, x1, y1, fn in self.head_hits:
            if x0 <= e.x < x1 and y0 <= e.y < y1:
                return fn
        return None

    def _head_click(self, e):
        fn = self._head_hit(e)
        if fn and self.sel:
            fn()

    def set_filter(self, k):
        self.log_filter = k
        for kind, _ in LOG_KINDS[1:]:
            self.p_log.tag_configure('k_' + kind, elide=(k != 'all' and k != kind))
        for kind, b in self.log_btns.items():
            b.configure(bg=ACCENT if kind == k else CARD2, fg='#ffffff' if kind == k else FG)
        self.p_log.see('end')

    def draw_bar(self):
        c = self.p_bar
        c.delete('all')
        if not self.bar_vals:
            return
        w, h = c.winfo_width(), int(c['height'])
        tot = sum(self.bar_vals) or 1
        x = 0
        for v, (_n, col) in zip(self.bar_vals, TOK_PARTS):
            dx = w * v / tot
            if dx > 0:
                c.create_rectangle(x, 0, x + dx, h, fill=col, outline='')
            x += dx

    # ── 캔버스: 크기, 줌, 배치 ──
    def _on_cv_resize(self, e):
        if self._relayout_job:
            self.root.after_cancel(self._relayout_job)
        self._relayout_job = self.root.after(150, self.relayout)

    def _on_wheel(self, e):
        if e.state & 4:  # Ctrl + 휠 = 확대/축소
            self.set_zoom(1 if e.delta > 0 else -1)
        else:
            self.cv.yview_scroll(int(-e.delta / 120), 'units')

    def relayout(self, force=False):
        self._relayout_job = None
        w = max(self.cv.winfo_width(), 300)
        cols = max(1, int((w / self.Z - 20) // CELL_W))
        if not force and cols == self.cols and abs(w - self.room_px) < 2:
            return
        self.cols, self.room_px = cols, w
        self.layout(self.sessions, force=True)

    # ── 비서, 보이는 세션 ──
    def find_secretary(self, sessions):
        """비서 세션: 사용자가 지정한 것 > secretary.json의 sessionId > 이름에 '비서'가 들어간 세션."""
        ids = {s.sid for s in sessions}
        for cand in (self.sec_pick, (self.mon.sec or {}).get('sessionId')):
            if cand and cand in ids:
                return cand
        for s in sessions:
            if any(k in self.mon.display_name(s) for k in ('비서', T('비서'))):
                return s.sid
        return None

    def visible_sessions(self, sessions):
        vis = [s for s in sessions if s.sid != self.sec_sid]
        g = self.group_of(self.group)
        if g:
            members = set(g['sids'])
            vis = [s for s in vis if s.sid in members]
        return vis

    def sec_items(self, all_=False):
        items = (self.mon.sec or {}).get('items')
        out = []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get('title'):
                key = str(it.get('id') or it['title'])
                if all_ or key not in self.sec_done:
                    out.append(dict(it, _key=key))
        return out

    def layout(self, sessions, force=False):
        if self.drag and self.drag.get('on') and not force:
            return  # 끄는 중에는 다시 그리지 않는다 (끝나면 refresh가 반영)
        self.sec_sid = self.find_secretary(sessions)
        sec = next((s for s in sessions if s.sid == self.sec_sid), None)
        vis = self.visible_sessions(sessions)
        sec_on = bool(sec or self.sec_items(all_=True))
        order = [s.sid for s in vis]
        key = (tuple(order), self.sec_sid, self.group, sec_on)
        if key == self.layout_key and not force:
            return
        self.layout_key = key
        if not self.cols:
            self.cols = max(1, int((max(self.cv.winfo_width(), 300) / self.Z - 20) // CELL_W))
        self.cv.delete('all')
        self.wb_sig = self.secb_sig = None  # 캔버스를 비웠으니 분배판·말풍선도 새로
        self.desks.clear()
        self.hover = None
        self.flights = []
        self.sec_on = sec_on
        # 비서실 아래에 여유를 두어 첫 줄 책상의 말풍선이 비서실을 덮지 않게 한다
        self.grid_y = WALL_H + 4 + (SEC_H + 36 if sec_on else 0)
        self.rows = max(1, (len(order) + self.cols - 1) // self.cols)
        self.room_w = max(self.cv.winfo_width() / self.Z, self.cols * CELL_W + 20)
        self.draw_room()
        x0 = (self.room_w - self.cols * CELL_W) / 2
        if sec_on:
            self.draw_sec_area(sec, x0 - 5)
        for i, s in enumerate(vis):
            # 책상 그림은 가운데가 130 단위 → 칸(250) 가운데에 오도록 5 당긴다
            self.create_desk(s, x0 - 5 + (i % self.cols) * CELL_W, self.grid_y + (i // self.cols) * CELL_H)
            self.update_desk(self.desks[s.sid], s)
        if not vis:
            cy = self.grid_y + 100
            self.cv.create_text(*self.P(self.room_w / 2, cy), text=T('이 탭에 세션이 없습니다') if self.group else T('출근한 세션이 없습니다'),
                                font=self.fonts['c_empty'], fill=ROOM['empty'])
            if self.group:
                self.cv.create_text(*self.P(self.room_w / 2, cy + 30), font=self.fonts['c_info'], fill=ROOM['empty2'],
                                    text=T('[전체] 탭에서 책상을 이 탭 위로 끌어 놓거나, 책상 오른쪽 클릭 > 탭에 넣기'))
        keep = {s.sid for s in sessions}  # 다른 탭 세션도 패널·세션톡에 그림이 쓰인다
        for sid in list(self.frames):
            if sid not in keep:
                del self.frames[sid]
        self.order = order
        self.anim_k = -1
        self.draw_board(vis)
        self.update_tabbar(sessions)

    # ── 배경(벽, 창문, 분배판, 시계, 바닥) ──
    def draw_room(self):
        c, P, W, Z = self.cv, self.P, self.room_w, self.Z
        H = max(self.grid_y + self.rows * CELL_H + 30, self.cv.winfo_height() / Z)
        c.configure(scrollregion=(0, 0, W * Z, H * Z))
        # 벽: 위는 연한 민트 줄무늬, 아래는 판벽과 흰 몰딩
        rail = WALL_H - 40
        c.create_rectangle(*P(0, 0, W, WALL_H), fill=WALL, outline='')
        for x in range(0, int(W), 28):
            c.create_rectangle(*P(x, 0, x + 14, rail), fill=WALL2, outline='')
        c.create_rectangle(*P(0, rail, W, WALL_H - 6), fill=WAINSCOT, outline='')
        for x in range(12, int(W) - 60, 92):
            c.create_rectangle(*P(x, rail + 8, x + 78, WALL_H - 13), outline=ROOM['panel'], width=Z)
        c.create_rectangle(*P(0, rail - 2, W, rail + 2), fill=ROOM['rail'], outline='')
        c.create_rectangle(*P(0, WALL_H - 6, W, WALL_H), fill=ROOM['base'], outline='')
        # 바닥: 밝은 원목 마루
        c.create_rectangle(*P(0, WALL_H, W, H), fill=FLOOR, outline='')
        ph, pw = 24, 190
        for i, y in enumerate(range(WALL_H, int(H) + ph, ph)):
            off = (i * 67) % pw
            for j, x in enumerate(range(-off, int(W), pw)):
                if (i + j) % 3 == 0:
                    c.create_rectangle(*P(x, y, x + pw, y + ph), fill=FLOOR2, outline='')
                c.create_line(*P(x, y, x, y + ph), fill=SEAM)
            c.create_line(*P(0, y, W, y), fill=SEAM)
        # 창문 (흰 창틀, 민트 커튼)
        c.create_rectangle(*P(22, 18, 178, 122), fill=ROOM['frame'], outline=ROOM['frame_edge'])
        self.sky = c.create_rectangle(*P(28, 24, 172, 116), fill='#9fd3f5', outline='')
        self.sun = c.create_oval(*P(134, 34, 160, 60), fill='#ffe27a', outline='')
        c.create_line(*P(100, 24, 100, 116), fill=ROOM['frame'], width=4 * Z)
        c.create_line(*P(28, 70, 172, 70), fill=ROOM['frame'], width=4 * Z)
        for x0, sgn in ((14, 1), (186, -1)):
            c.create_polygon(*P(x0, 12, x0 + 26 * sgn, 12, x0 + 16 * sgn, 70, x0 + 22 * sgn, 128, x0, 128),
                             fill=ROOM['curtain'], outline='')
            c.create_line(*P(x0 + 9 * sgn, 14, x0 + 6 * sgn, 126), fill=ROOM['curtain2'], width=2 * Z)
        c.create_rectangle(*P(8, 8, 192, 12), fill=ACCENT, outline='')
        # 분배판(흰 카드), 시계, 화분
        has_clock = W >= 200 + 300 + 150
        bx0 = 210
        bx1 = min(W - 150, 880) if has_clock else W - 16
        self.wb = (bx0, bx1)
        round_rect(c, *P(bx0 + 2, 12, bx1 + 2, 166), 14 * Z, fill=ROOM['board_shadow'], outline='')
        round_rect(c, *P(bx0, 8, bx1, 162), 14 * Z, fill=SURF, outline='')
        if has_clock:
            cx = W - 95
            self.clock_c = (cx, 64)
            c.create_oval(*P(cx - 36, 28, cx + 36, 100), fill=ACCENT, outline='')
            c.create_oval(*P(cx - 31, 33, cx + 31, 95), fill=ROOM['clock'], outline='')
            for k in range(12):
                a = math.radians(k * 30)
                c.create_line(*P(cx + 25 * math.sin(a), 64 - 25 * math.cos(a), cx + 29 * math.sin(a), 64 - 29 * math.cos(a)),
                              fill='#7c8896', width=(2 if k % 3 else 3) * Z)
            self.clock_h = c.create_line(0, 0, 0, 0, width=4 * Z, fill='#2b3440', capstyle='round')
            self.clock_m = c.create_line(0, 0, 0, 0, width=3 * Z, fill='#2b3440', capstyle='round')
            c.create_oval(*P(cx - 3, 61, cx + 3, 67), fill=ACCENT, outline='')
            px = W - 34
            c.create_polygon(*P(px - 14, 128, px + 14, 128, px + 10, 166, px - 10, 166), fill=ROOM['pot'], outline=ROOM['frame_edge'])
            c.create_rectangle(*P(px - 15, 126, px + 15, 131), fill=ACCENT, outline='')
            c.create_oval(*P(px - 24, 86, px + 24, 130), fill='#3f9a5e', outline='')
            c.create_oval(*P(px - 15, 74, px + 13, 106), fill='#5cb878', outline='')
            c.create_oval(*P(px - 4, 92, px + 22, 118), fill='#4eaa6a', outline='')
        else:
            self.clock_c = None
        self.minute = -1
        self.update_wall()

    def update_wall(self):
        now = datetime.datetime.now()
        if now.minute == self.minute:
            return
        self.minute = now.minute
        hr = now.hour + now.minute / 60
        if 6 <= hr < 17:
            sky, sun = '#9fd3f5', '#ffe27a'
        elif 17 <= hr < 19.5:
            sky, sun = '#f5b07a', '#ff8c5a'
        else:
            sky, sun = '#1d2a4a', '#f2f0d8'
        self.cv.itemconfig(self.sky, fill=sky)
        self.cv.itemconfig(self.sun, fill=sun)
        if self.clock_c:
            cx, cy = self.clock_c
            a_m = math.radians(now.minute * 6)
            a_h = math.radians((now.hour % 12) * 30 + now.minute * 0.5)
            self.cv.coords(self.clock_m, *self.P(cx, cy, cx + 23 * math.sin(a_m), cy - 23 * math.cos(a_m)))
            self.cv.coords(self.clock_h, *self.P(cx, cy, cx + 14 * math.sin(a_h), cy - 14 * math.cos(a_h)))

    def draw_board(self, sessions):
        c, P, F, Z = self.cv, self.P, self.fonts, self.Z
        # 보이는 내용이 그대로면 지우고 다시 그리지 않는다 (2초마다 불린다)
        sig = (self.wb, self.group, Z, tuple((s.sid, self.mon.display_name(s), s.mode(), s.peer.get('goal'), s.overall(),
                                             s.last_tool if s.mode() == 'busy' else '', self.desks[s.sid].shirt
                                             if s.sid in self.desks else '') for s in sessions))
        if sig == self.wb_sig:
            return
        self.wb_sig = sig
        c.delete('wb')
        bx0, bx1 = self.wb
        title = T('작업 분배판') + (f' · {self.group}' if self.group else '')
        c.create_text(*P(bx0 + 18, 30), text=title, anchor='w', font=F['c_wbt'], fill=FG, tags='wb')
        n = len(sessions)
        busy = sum(1 for s in sessions if s.mode() == 'busy')
        c.create_text(*P(bx1 - 16, 30), text=T('출근 {0} · 작업 중 {1}').format(n, busy), anchor='e', font=F['c_wb'], fill=DIM, tags='wb')
        c.create_line(*P(bx0 + 16, 44, bx1 - 16, 44), fill=EDGE, tags='wb')
        rows = 6
        shown = sessions if n <= rows else sessions[:rows - 1]
        bar_w = 64
        name_w = min(132, bx1 - 18 - bar_w - 48 - (bx0 + 32))  # 좁으면 이름 칸도 줄인다
        goal_x = bx0 + 32 + name_w
        goal_w = (bx1 - 18 - bar_w - 44) - goal_x
        y = 58
        for s in shown:
            d = self.desks.get(s.sid)
            mode = s.mode()
            c.create_oval(*P(bx0 + 16, y - 5, bx0 + 26, y + 5), fill=d.shirt if d else '#888', outline='', tags='wb')
            c.create_text(*P(bx0 + 32, y), text=fit(self.mon.display_name(s), F['c_wbn'], name_w * Z - 6), anchor='w',
                          font=F['c_wbn'], fill=FG, tags='wb')
            goal = s.peer.get('goal') or (s.last_tool if mode == 'busy' else '') or MODE_TEXT[mode]
            if goal_w > 30:
                c.create_text(*P(goal_x, y), text=fit(one_line(goal), F['c_wb'], goal_w * Z), anchor='w',
                              font=F['c_wb'], fill=DIM, tags='wb')
            pct = s.overall()
            bx = bx1 - 18 - bar_w - 40
            round_rect(c, *P(bx, y - 3, bx + bar_w, y + 3), 3 * Z, fill=CARD2, outline='', tags='wb')
            if pct is not None:
                if pct > 0:
                    round_rect(c, *P(bx, y - 3, bx + max(6, bar_w * pct / 100), y + 3), 3 * Z, fill=MODE_COL[mode],
                               outline='', tags='wb')
                c.create_text(*P(bx1 - 16, y), text=f'{pct:.0f}%', anchor='e', font=F['c_wb'], fill=TEXT2, tags='wb')
            y += 17
        if n > len(shown):
            c.create_text(*P(bx1 - 16, y), text=T('외 {0}명').format(n - len(shown)), anchor='e', font=F['c_wb'], fill=DIM, tags='wb')
        if not sessions:
            c.create_text(*P((bx0 + bx1) / 2, 92), text=T('이 탭에 세션이 없습니다') if self.group else T('출근한 세션이 없습니다'),
                          font=F['c_wb'], fill=DIM, tags='wb')

    # ── 비서실: 비서 책상과 제안 말풍선 ──
    def draw_sec_area(self, sec, bx):
        c, P, Z, F = self.cv, self.P, self.Z, self.fonts
        W = self.room_w
        y0, y1 = WALL_H + 8, WALL_H + SEC_H - 4
        round_rect(c, *P(10, y0, W - 10, y1), 16 * Z, fill=ROOM['carpet'], outline=ACCENT, width=2 * Z)
        round_rect(c, *P(17, y0 + 7, W - 17, y1 - 7), 12 * Z, fill='', outline=ROOM['carpet_in'], width=Z)
        t = c.create_text(*P(32, y0 + 22), text=T('비서실'), anchor='w', font=F['c_wbn'], fill='#ffffff')
        bb = c.bbox(t)
        c.tag_lower(round_rect(c, bb[0] - 10 * Z, bb[1] - 4 * Z, bb[2] + 10 * Z, bb[3] + 4 * Z, 9 * Z, fill=ACCENT,
                               outline=''), t)
        if sec:
            self.create_desk(sec, bx, y0 - 4, shadow=ROOM['sec_shadow'])
            self.update_desk(self.desks[sec.sid], sec)
        else:
            c.create_text(*P(bx + 130, y0 + 110), text=T('비서 자리 비움\n(비서 세션이 꺼져 있어요)'), justify='center',
                          font=F['c_info'], fill=ROOM['sec_empty'])
        bl = bx + CELL_W + 22
        self.sec_bubble = (bl, y0 + 18, min(W - 28, bl + 900), y1 - 16)
        self.draw_sec_board()

    def _pill(self, x1, y, text, fn, primary=True, tags='secb'):
        """오른쪽 끝이 x1(단위)인 둥근 버튼을 그리고 눌렀을 때 할 일을 등록한다. 왼쪽 끝(단위)을 돌려준다."""
        c, P, Z, F = self.cv, self.P, self.Z, self.fonts
        w = F['c_wbn'].measure(text) / Z + 20
        x0 = x1 - w
        round_rect(c, *P(x0, y - 10, x1, y + 10), 10 * Z, fill=ACCENT if primary else SURF,
                   outline=ACCENT, width=Z, tags=tags)
        c.create_text(*P((x0 + x1) / 2, y), text=text, font=F['c_wbn'], fill='#ffffff' if primary else ACC_TEXT, tags=tags)
        self.sec_hits.append((*P(x0, y - 11, x1, y + 11), fn))
        return x0

    def draw_sec_board(self):
        c = self.cv
        briefing = self.briefing()
        upd = (self.mon.sec or {}).get('updatedAt')
        ago = int((time.time() - upd / 1000) / 60) if isinstance(upd, (int, float)) and upd > 0 else None
        sig = (self.sec_on, getattr(self, 'sec_bubble', None), self.Z, id(self.mon.sec), frozenset(self.sec_done), self.sec_sid,
               int(time.time() / 2) if briefing else briefing, ago, tuple(x.sid for x in self.sessions), id(self.mon.names))
        if sig == self.secb_sig:
            return
        self.secb_sig = sig
        c.delete('secb')
        self.sec_hits = []
        if not self.sec_on:
            self.sec_box = None
            return
        P, Z, F = self.P, self.Z, self.fonts
        x0, y0, x1, y1 = self.sec_bubble
        if x1 - x0 < 160:
            self.sec_box = None
            return
        ty = y0 + 64
        # 그림자, 몸통(둥근 상자), 왼쪽 꼬리: 테두리가 고르게 나오도록 선과 호로 그린다
        round_rect(c, *P(x0 + 2, y0 + 3, x1 + 2, y1 + 3), 14 * Z, fill=ROOM['bub_shadow'], tags='secb')
        round_rect(c, *P(x0, y0, x1, y1), 14 * Z, fill=SURF, outline=ACCENT, width=2 * Z, tags='secb')
        c.create_polygon(*P(x0, ty - 10), x0 * Z + 2 * Z, ty * Z - 10 * Z, x0 * Z + 2 * Z, ty * Z + 10 * Z, *P(x0, ty + 10, x0 - 18, ty),
                         fill=SURF, outline='', tags='secb')
        c.create_line(*P(x0, ty - 10, x0 - 18, ty, x0, ty + 10), fill=ACCENT, width=2 * Z, joinstyle='round', capstyle='round',
                      tags='secb')
        sec = self.mon.sec or {}
        # 브리핑은 이 버튼을 누를 때만. 버튼을 먼저 놓고 제목·시각은 남은 폭 안에서만 쓴다
        if self.briefing():
            t = c.create_text(*P(x1 - 18, y0 + 22), anchor='e', font=F['c_wbn'], fill=ACC_TEXT, tags='secb',
                              text=T('브리핑 중') + '.' * (int(time.time() / 2) % 3 + 1))
            right = c.bbox(t)[0] / Z - 10
        else:
            right = self._pill(x1 - 16, y0 + 22, T('브리핑 받기') if self.sec_sid else T('비서 켜기'), self.ask_brief) - 10
        t = c.create_text(*P(x0 + 20, y0 + 22), text=fit(T('비서의 제안'), F['c_wbt'], (right - x0 - 20) * Z), anchor='w',
                          font=F['c_wbt'], fill=FG, tags='secb')
        upd = sec.get('updatedAt')
        hx = c.bbox(t)[2] / Z + 10
        if isinstance(upd, (int, float)) and upd > 0:
            m = int((time.time() - upd / 1000) / 60)
            when = T('방금 브리핑') if m < 1 else T('{0}분 전 브리핑').format(m) if m < 120 else T('{0}시간 전 브리핑').format(m // 60)
            if F['c_wb'].measure(when) / Z <= right - hx:
                c.create_text(*P(hx, y0 + 23), text=when, anchor='w', font=F['c_wb'], fill=DIM, tags='secb')
        if sec.get('summary'):
            c.create_text(*P(x0 + 20, y0 + 46), text=fit(one_line(sec['summary']), F['c_wb'], (x1 - x0 - 40) * Z),
                          anchor='w', font=F['c_wb'], fill=TEXT2, tags='secb')
        items = self.sec_items()
        y = y0 + 72
        for it in items[:4]:
            label, col = SEC_KIND.get(it.get('kind'), SEC_KIND['info'])
            cw = F['c_wbn'].measure(label) / Z + 14
            round_rect(c, *P(x0 + 20, y - 9, x0 + 20 + cw, y + 9), 8 * Z, fill=col, outline='', tags='secb')
            c.create_text(*P(x0 + 20 + cw / 2, y), text=label, font=F['c_wbn'], fill='#ffffff', tags='secb')
            # 오른쪽부터: 확인(✓), 선택지 버튼, 링크, 대상 세션
            bx = self._pill(x1 - 16, y, '✓', lambda k=it['_key']: self.sec_mark(k, True), primary=False) - 6
            url = str(it.get('url') or '')
            chs = [ch for ch in self.sec_choices(it)[:2] if ch['sid']]
            # 좁으면 링크 → 두 번째 선택지 순으로 빼고, 첫 선택지도 칩을 덮으면 버튼 없이 제목만 (자세히 보기 창에서 누를 수 있음)
            if not url.startswith(('http://', 'https://')):
                url = ''
            for cut in range(3):
                pills = [ch['label'] for ch in chs] + ([T('열어보기')] if url else [])
                if bx - sum(F['c_wbn'].measure(lb) / Z + 26 for lb in pills) >= x0 + 30 + cw + 40:
                    break
                if url:
                    url = ''
                else:
                    chs = chs[:-1]
            else:
                chs, url = [], ''
            for i, ch in enumerate(chs[::-1]):
                bx = self._pill(bx, y, ch['label'], lambda it=it, ch=ch: self.send_choice(it, ch),
                                primary=i == len(chs) - 1) - 6
            if url.startswith(('http://', 'https://')):
                bx = self._pill(bx, y, T('열어보기'), lambda u=url: webbrowser.open(u), primary=False) - 6
            tgt = one_line(it.get('target') or '', 18)
            if tgt and bx - (x0 + 30 + cw) > 200:
                t = c.create_text(*P(bx - 4, y), text='→ ' + tgt, anchor='e', font=F['c_wb'], fill=DIM, tags='secb')
                bx = c.bbox(t)[0] / Z - 6
            c.create_text(*P(x0 + 30 + cw, y), text=fit(one_line(it['title']), F['c_wbn'], (bx - x0 - cw - 36) * Z),
                          anchor='w', font=F['c_wbn'], fill=FG, tags='secb')
            y += 27
        if not items:  # 빈 말풍선: 가운데에 아이콘과 안내
            done = len(self.sec_items(all_=True))
            cx, cy = (x0 + x1) / 2, (y0 + 56 + y1) / 2 - 8
            c.create_oval(*P(cx - 20, cy - 40, cx + 20, cy), fill=MINT, outline='', tags='secb')
            draw_icon(c, 'star', cx * Z, (cy - 20) * Z, 12 * Z, ACCENT, tags=('secb',))
            c.create_text(*P(cx, cy + 18), font=F['c_wbn'], fill=FG, tags='secb',
                          text=T('제안을 모두 확인했어요') if done else T('아직 제안이 없어요'))
            c.create_text(*P(cx, cy + 38), font=F['c_wb'], fill=DIM, tags='secb', justify='center',
                          text=T('오른쪽 위 [브리핑 받기]를 누르면 비서가 다른 세션들을 살펴보고 제안해요'))
        else:
            more = len(items) - 4
            c.create_text(*P(x1 - 18, y1 - 14), anchor='e', font=F['c_wbn'], fill=ACC_TEXT, tags='secb',
                          text=(T('외 {0}건 · ').format(more) if more > 0 else '') + T('눌러서 자세히 보기 ›'))
        self.sec_box = P(x0 - 18, y0, x1, y1)

    # ── 책상과 캐릭터 ──
    def create_desk(self, s, bx, by, shadow=None):
        c, Z, F = self.cv, self.Z, self.fonts
        t = 'desk_' + s.sid
        sc = 4.4 * Z
        if s.sid not in self.frames:
            self.frames[s.sid] = make_frames(s.sid, sc, self.avatars.get(s.sid))
        frames, shirt, seed = self.frames[s.sid]

        def pt(*a):
            return [(bx + v) * Z if i % 2 == 0 else (by + v) * Z for i, v in enumerate(a)]

        d = Desk()
        d.sid, d.tag, d.pt, d.frames, d.shirt, d.seed = s.sid, t, pt, frames, shirt, seed
        d.cur_img = d.cur_scr = d.cur_phase = d.mode = d.bubble_text = d.bubble_fill = d.say = None
        d.sel = c.create_rectangle(*pt(10, 2, CELL_W, CELL_H - 2), outline='', width=3, dash=(6, 4), tags=t)
        c.create_oval(*pt(22, 156, 238, 210), fill=shadow or shade(FLOOR, 0.9), outline='', tags=t)
        c.create_rectangle(*pt(101, 70, 159, 116), fill='#3b3f4a', outline='#2a2d35', tags=t)
        d.spr = c.create_image(*pt(130, 117), anchor='s', image=frames['idle'], tags=t)  # 발밑을 책상에 맞춘다
        # 책상
        c.create_polygon(*pt(34, 112, 226, 112, 234, 124, 26, 124), fill=ROOM['desk_top'], outline=ROOM['desk_edge'], tags=t)
        c.create_rectangle(*pt(26, 124, 234, 164), fill=ROOM['desk_front'], outline=ROOM['desk_edge'], tags=t)
        c.create_line(*pt(26, 125, 234, 125), fill=ACCENT, width=2 * Z, tags=t)
        c.create_rectangle(*pt(108, 113, 152, 119), fill='#4a4f5c', outline='', tags=t)
        # 모니터
        c.create_rectangle(*pt(166, 70, 222, 108), fill='#2d313b', outline='', tags=t)
        d.scr = c.create_rectangle(*pt(170, 74, 218, 104), fill='#16202e', outline='', tags=t)
        d.lines = [c.create_rectangle(*pt(173, 78 + i * 6, 186, 81 + i * 6), fill=LINE_COLS[i], outline='', tags=t)
                   for i in range(4)]
        c.create_rectangle(*pt(190, 108, 198, 114), fill='#2d313b', outline='', tags=t)
        c.create_rectangle(*pt(182, 113, 206, 116), fill='#2d313b', outline='', tags=t)
        # 머그컵과 김
        c.create_rectangle(*pt(50, 100, 62, 113), fill='#f4f1ea', outline='#c9c3b6', tags=t)
        c.create_arc(*pt(58, 103, 67, 110), start=-90, extent=180, style='arc', outline='#c9c3b6', width=2 * Z, tags=t)
        d.steam = [c.create_line(0, 0, 0, 0, smooth=True, fill='#ffffff', width=2 * Z, state='hidden', tags=t)
                   for _ in range(2)]
        # 명패, 진행 막대, 상태 줄
        round_rect(c, *pt(50, 129, 210, 151), 8 * Z, fill=SURF, outline=ROOM['plate_edge'], width=Z, tags=t)
        d.dot = c.create_oval(*pt(57, 135, 66, 144), fill='#888', outline='', tags=t)
        d.name = c.create_text(*pt(72, 139.5), text='', anchor='w', font=F['c_name'], fill=FG, tags=t)
        d.pbg = c.create_rectangle(*pt(50, 155, 210, 161), fill=CARD2, outline='', tags=t)
        d.pfg = c.create_rectangle(*pt(50, 155, 50, 161), fill='#3fbf6a', outline='', tags=t)
        d.info = c.create_text(*pt(130, 184), text='', font=F['c_info'], fill=ROOM['info'], tags=t)
        # 말풍선, Z
        d.bub = c.create_polygon(0, 0, 0, 0, 0, 0, fill=SURF, outline=ROOM['bub_edge'], width=Z, smooth=True,
                                 state='hidden', tags=t)
        d.btxt = c.create_text(*pt(130, 26), text='', font=F['c_bub'], fill=ROOM['bub_text'], width=232 * Z,
                               justify='center', state='hidden', tags=t)
        d.zs = [c.create_text(*pt(155, 60), text='z', font=F['c_z'], fill='#6b5f8a', state='hidden', tags=t)
                for _ in range(3)]
        # 클릭·호버는 캔버스 전체 바인딩에서 칸 좌표로 판정한다.
        # (도형마다 Enter/Leave를 걸면 애니메이션으로 도형이 바뀔 때마다 이벤트가 쏟아진다)
        d.box = (bx * Z, by * Z, (bx + CELL_W) * Z, (by + CELL_H) * Z)
        self.desks[s.sid] = d
        return d

    def desk_at(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        for sid, d in self.desks.items():
            x0, y0, x1, y1 = d.box
            if x0 <= x < x1 and y0 <= y < y1:
                return sid
        return None

    def in_sec_bubble(self, e):
        if not self.sec_box:
            return False
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        x0, y0, x1, y1 = self.sec_box
        return x0 <= x < x1 and y0 <= y < y1

    def sec_hit(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        for x0, y0, x1, y1, fn in self.sec_hits:
            if x0 <= x < x1 and y0 <= y < y1:
                return fn
        return None

    def _on_motion(self, e):
        self._set_hover(self.desk_at(e))
        if not self.hover:
            self.cv.configure(cursor='hand2' if self.in_sec_bubble(e) else '')

    def _on_click(self, e):
        if self.menu_open:
            self.toggle_menu(False)
            self.drag = None
            return
        fn = self.sec_hit(e)
        if fn:
            self.drag = None
            fn()
            return
        if self.in_sec_bubble(e):
            self.drag = None
            self.open_secretary()
            return
        sid = self.desk_at(e)
        # 비서 책상은 비서실 자리라 끌어 옮기지 않는다
        self.drag = {'sid': sid, 'x': e.x, 'y': e.y} if sid and sid != self.sec_sid else None
        if sid and sid != self.sel:
            self.select(sid)

    def _set_hover(self, sid):
        if sid == self.hover:
            return
        old, self.hover = self.hover, sid
        if old in self.desks and old != self.sel:
            self.cv.itemconfig(self.desks[old].sel, outline='')
        if sid in self.desks and sid != self.sel:
            self.cv.itemconfig(self.desks[sid].sel, outline=HOVER_LINE)
        self.cv.configure(cursor='hand2' if sid else '')

    def set_bubble(self, d, text, fill=None):
        fill = fill or SURF
        c = self.cv
        if text == d.bubble_text and fill == d.bubble_fill:
            return
        d.bubble_text, d.bubble_fill = text, fill
        if not text:
            c.itemconfig(d.bub, state='hidden')
            c.itemconfig(d.btxt, state='hidden')
            return
        # 노란 확인 말풍선 위에선 어두운 글자, 다크 테마의 어두운 말풍선 위에선 밝은 글자
        c.itemconfig(d.btxt, text=text, state='normal', fill='#22262e' if is_light(fill) else ROOM['bub_text'])
        x0, y0, x1, y1 = c.bbox(d.btxt)
        cx, base = d.pt(130, 38)
        c.coords(d.btxt, cx, base - 9 * self.Z - (y1 - y0) / 2)
        x0, y0, x1, y1 = c.bbox(d.btxt)
        p, r, tip = 9 * self.Z, 5 * self.Z, 7 * self.Z
        x0, x1, y0, y1 = x0 - p, x1 + p, y0 - p / 2, y1 + p / 2
        # smooth=True 에서 모서리를 살리려고 꼭짓점을 겹쳐 준다
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               cx + tip, y1, cx + tip, y1, cx, y1 + tip, cx - tip, y1, cx - tip, y1, x0 + r, y1, x0, y1, x0, y1 - r,
               x0, y0 + r, x0, y0]
        c.coords(d.bub, *pts)
        c.itemconfig(d.bub, state='normal', fill=fill)
        c.tag_raise(d.bub)
        c.tag_raise(d.btxt)

    def update_desk(self, d, s):
        c, F = self.cv, self.fonts
        mode = s.mode()
        d.mode = mode
        c.itemconfig(d.name, text=fit(self.mon.display_name(s), F['c_name'], 134 * self.Z))
        c.itemconfig(d.dot, fill=MODE_COL[mode])
        pct = s.overall()
        c.coords(d.pfg, *d.pt(50, 155, 50 + 160 * (pct or 0) / 100, 161))
        c.itemconfig(d.pfg, fill=MODE_COL[mode], state='normal' if pct is not None else 'hidden')
        c.itemconfig(d.pbg, state='normal' if pct is not None else 'hidden')
        tok_today = sum(s.acc.days.get(today(), [0, 0, 0, 0]))
        extra = f' · {pct:.0f}%' if pct is not None else ''
        c.itemconfig(d.info, text=T('{0}{1} · 오늘 {2} 토큰').format(MODE_TEXT[mode], extra, fmt_n(tok_today)))
        if d.say and d.say[1] < time.time():
            d.say = None
        if mode != 'wait' and d.say:  # 다른 세션에 메시지를 보냈다 → 잠깐 그 말을 띄운다
            self.set_bubble(d, one_line(d.say[0], 70), ROOM['say'])
        elif mode == 'wait':
            self.set_bubble(d, T('확인이 필요해요!') + (f'\n({clean(s.waiting, 30)})' if s.waiting else ''), '#ffe066')
        elif mode == 'busy':
            act = s.peer.get('activity') if time.time() - (s.peer.get('updatedAt') or 0) / 1000 < 120 else ''
            if s.last_kind == 'tool' and s.last_tool:
                txt = s.last_tool
            elif s.last_kind == 'user':
                txt = T('요청 읽는 중…')
            else:
                txt = act or T('생각 중…')
            self.set_bubble(d, one_line(txt, 56))
        else:
            self.set_bubble(d, '')
        c.itemconfig(d.sel, outline=ACCENT if s.sid == self.sel else HOVER_LINE if s.sid == self.hover else '')

    # ── 주기 갱신 ──
    def refresh(self, again=True):
        mon = self.mon
        try:
            with mon.lock:
                ver = mon.version
                sessions = self.sort_sessions(mon.sessions.values())
                flights, mon.flights = mon.flights, []
                says, mon.says = mon.says, []
                unread = sum(1 for m in mon.chat.values() if m['ts'] > self.chat_seen and m['from'] != 'user')
            self.sessions = sessions
            now = time.time()
            for sid, text in says:
                d = self.desks.get(sid)
                if d:
                    d.say = (text, now + 9)
            by_sid = {s.sid: s for s in sessions}
            for sid, d in self.desks.items():
                if d.say and sid in by_sid and (says or d.say[1] < now):
                    self.update_desk(d, by_sid[sid])
            if self.chatwin and self.chatwin.win.winfo_exists():
                self.chat_seen, unread = now, 0
            self.draw_side()
            b = self.briefing()
            if b or getattr(self, '_was_brief', False):  # '브리핑 중…' 점 움직임, 끝나면 한 번 더
                self._was_brief = b
                self.draw_sec_board()
            # 이 창에서 켠 세션이 출근하면 골라 보여 준다
            for sid, (t0, name) in list(self.pending.items()):
                if sid in by_sid:
                    del self.pending[sid]
                    self.toast(T('{0} 출근했어요').format(clean(name, 30)))
                    self.root.after(600, lambda sid=sid: self.select(sid) if sid in self.mon.sessions else None)
                elif now - t0 > 120:
                    del self.pending[sid]
            if self.menu_open and not getattr(self, '_menu_anim_on', False):
                sig = tuple((t[1], t[3], t[4]) for t in self.menu_tiles())
                if sig != getattr(self, 'menu_sig', None):
                    self.menu_sig = sig
                    self.draw_menu()
            self.remote_watch(sessions)
            tick = int(time.time() // 60)
            if ver != self.seen_ver or tick != self.tick:
                self.seen_ver, self.tick = ver, tick
                self.layout(sessions)
                for s in sessions:
                    if s.sid in self.desks:  # 끄는 중이면 새 세션 책상은 아직 없다
                        self.update_desk(self.desks[s.sid], s)
                self.draw_board(self.visible_sessions(sessions))
                self.draw_sec_board()
                self.update_tabbar(sessions)
                self.update_stats(sessions)
                if self.sel not in mon.sessions:
                    busy = [s for s in sessions if s.mode() in ('busy', 'wait')]
                    first = (busy or sessions or [None])[0]
                    self.sel = first.sid if first else None
                    self.select(self.sel)
                self.update_panel()
            else:
                self.update_stats(sessions)
                self.update_panel(light=True)
            for f in flights:
                self.launch_flight(*f)
            self.update_wall()
        except Exception as ex:
            print('refresh error', ex, file=sys.stderr)
        if again:
            self.root.after(2000, self.refresh)

    def update_stats(self, sessions):
        modes = collections.Counter(s.mode() for s in sessions)
        chips = tuple(f'{MODE_TEXT[k]} {modes[k]}' for k in self.chips)
        if chips != getattr(self, 'chip_sig', None):  # 숫자 자리가 바뀌면 폭도 바뀌니 다시 맞춘다
            self.chip_sig = chips
            for (k, l), text in zip(self.chips.items(), chips):
                l.configure(text=text)
            if not self.menu_open:
                self.root.after_idle(self._fit_top)
        td = today()
        tin = tout = 0
        for s in sessions:
            v = s.acc.days.get(td)
            if v:
                tin += v[0] + v[1] + v[2]
                tout += v[3]
        pending = sum(1 for s in sessions for t in s.tracks.values() if not t.back_done)
        scan = T('   토큰 기록 훑는 중({0})').format(pending) if pending else ''
        self.stat.configure(text=T('오늘 입력 {0} · 출력 {1}{2}').format(fmt_n(tin), fmt_n(tout), scan))
        self.res.configure(text=T('이 창 {0:.0f}MB · CPU {1:.1f}%').format(self.mon.self_mem / 1048576, self.mon.self_cpu))

    def cycle(self, step):
        if not self.order:
            return
        i = self.order.index(self.sel) if self.sel in self.order else 0
        self.select(self.order[(i + step) % len(self.order)])

    # ── 책상 순서 (끌어서 옮기기, 오른쪽 클릭 메뉴) ──
    def sort_sessions(self, sessions):
        rank = {sid: i for i, sid in enumerate(self.desk_order)}
        return sorted(sessions, key=lambda s: (rank.get(s.sid, len(rank)), s.started, s.sid))

    def move_desk(self, sid, idx):
        """sid 책상을 지금 보이는 순서의 idx 자리로 옮기고 저장한다. idx=None 이면 저장된 순서를 지운다."""
        if idx is None:
            self.desk_order = []
        else:
            vis = [x for x in self.order if x != sid]
            vis.insert(max(0, min(idx, len(vis))), sid)
            # 전체 순서에서 지금 탭에 보이는 세션들이 차지한 자리만 새 순서로 채운다
            # (다른 탭 세션과 꺼진 세션의 자리는 그대로 — 다시 켜지면 원래 자리로)
            base = self.desk_order + [s.sid for s in self.sort_sessions(self.sessions) if s.sid not in self.desk_order]
            members = set(vis)
            slots = [i for i, x in enumerate(base) if x in members]
            for i, x in zip(slots, vis):
                base[i] = x
            self.desk_order = base[-200:]
        self.save_cfg()
        with self.mon.lock:
            sessions = self.sort_sessions(self.mon.sessions.values())
        self.sessions = sessions
        self.layout(sessions, force=True)

    def _drag_marks(self):
        self.cv.delete('dragui')

    def _drag_cancel(self):
        if self.drag:
            on = self.drag.get('on')
            self.drag = None
            self._drag_marks()
            if on:
                self.cv.configure(cursor='')
            if self.tab_drop is not None:
                self.tab_drop = None
                self._tab_highlight()

    def slot_at(self, e):
        """포인터가 가리키는 칸 번호 (책상이 없는 빈 칸이면 맨 뒤)."""
        if not self.order or not self.cols:
            return None
        x, y = self.cv.canvasx(e.x) / self.Z, self.cv.canvasy(e.y) / self.Z
        x0 = (self.room_w - self.cols * CELL_W) / 2 - 5
        col = int(max(0, min(self.cols - 1, (x - x0) // CELL_W)))
        row = int(max(0, (y - self.grid_y) // CELL_H))
        return min(row * self.cols + col, len(self.order) - 1)

    def tab_at(self, e):
        """포인터 아래 탭 이름 (탭 막대 밖이면 None)."""
        w = self.root.winfo_containing(e.x_root, e.y_root)
        return getattr(w, '_group', None)

    def _on_drag(self, e):
        d = self.drag
        if not d:
            return
        if not d.get('on'):
            if abs(e.x - d['x']) + abs(e.y - d['y']) < 8:
                return  # 손떨림은 클릭으로 본다
            d['on'] = True
            self._set_hover(None)
            self.cv.configure(cursor='fleur')
        c, Z = self.cv, self.Z
        self._drag_marks()
        tab = self.tab_at(e)
        if tab != self.tab_drop:
            self.tab_drop = tab
            self._tab_highlight()
        idx = None if tab is not None else self.slot_at(e)
        d['idx'] = idx
        if idx is not None and idx < len(self.order):
            x0, y0, x1, y1 = self.desks[self.order[idx]].box
            c.create_rectangle(x0 + 6 * Z, y0 + 2 * Z, x1 - 2 * Z, y1 - 2 * Z, outline=ACCENT, width=3 * Z,
                               fill='#f6e7c8', stipple='gray25', tags='dragui')
        s = next((s for s in self.sessions if s.sid == d['sid']), None)
        name = fit(self.mon.display_name(s), self.fonts['c_name'], 200 * Z) if s else ''
        where = T('여기로') if tab is None else T('전체 탭') if tab == '' else T('[{0}] 탭에 넣기').format(tab)
        cx, cy = c.canvasx(e.x), c.canvasy(e.y)
        t = c.create_text(cx + 14 * Z, cy + 14 * Z, text=f'{name}  → {where}', anchor='nw', font=self.fonts['c_name'],
                          fill='#22262e', tags='dragui')
        bx0, by0, bx1, by1 = c.bbox(t)
        right = c.canvasx(c.winfo_width()) - 8 * Z
        if bx1 + 6 * Z > right:  # 오른쪽 끝이면 포인터 왼쪽에 붙인다
            dx = -(bx1 - bx0) - 28 * Z
            c.move(t, dx, 0)
            bx0, bx1 = bx0 + dx, bx1 + dx
        c.tag_lower(c.create_rectangle(bx0 - 6 * Z, by0 - 3 * Z, bx1 + 6 * Z, by1 + 3 * Z, fill='#fff6e0',
                                       outline=ACCENT, width=Z, tags='dragui'), t)
        c.tag_raise('dragui')
        # 위아래 끝에 닿으면 굴린다
        h = c.winfo_height()
        if tab is None and e.y < 30 * Z:
            c.yview_scroll(-1, 'units')
        elif e.y > h - 30 * Z:
            c.yview_scroll(1, 'units')

    def _on_drop(self, e):
        d = self.drag
        on = d and d.get('on')
        tab = self.tab_drop if on else None
        self._drag_cancel()
        if not on:
            return
        if tab is not None:
            if tab:  # 전체 탭에 놓으면 아무 일도 없다
                self.toggle_member(tab, d['sid'], True)
            return
        idx = self.slot_at(e)
        if idx is not None and d['sid'] in self.order and self.order.index(d['sid']) != idx:
            self.move_desk(d['sid'], idx)

    def _on_menu(self, e):
        sid = self.desk_at(e)
        if not sid or sid not in self.desks:
            return
        if sid != self.sel:
            self.select(sid)
        F = self.fonts['small']
        m = tk.Menu(self.root, tearoff=0, font=F)
        if sid in self.order:
            i, n = self.order.index(sid), len(self.order)
            m.add_command(label=T('맨 앞으로'), command=lambda: self.move_desk(sid, 0), state='normal' if i else 'disabled')
            m.add_command(label=T('한 칸 앞으로'), command=lambda: self.move_desk(sid, i - 1), state='normal' if i else 'disabled')
            m.add_command(label=T('한 칸 뒤로'), command=lambda: self.move_desk(sid, i + 1),
                          state='normal' if i < n - 1 else 'disabled')
            m.add_command(label=T('맨 뒤로'), command=lambda: self.move_desk(sid, n - 1),
                          state='normal' if i < n - 1 else 'disabled')
            m.add_command(label=T('처음 순서로 (출근 순)'), command=lambda: self.move_desk(sid, None),
                          state='normal' if self.desk_order else 'disabled')
            m.add_separator()
            sub = tk.Menu(m, tearoff=0, font=F)
            for g in self.groups:
                var = tk.BooleanVar(value=sid in g['sids'])
                sub.add_checkbutton(label=g['name'], variable=var,
                                    command=lambda name=g['name'], v=var: self.toggle_member(name, sid, v.get()))
                sub._vars = getattr(sub, '_vars', []) + [var]  # 메뉴가 떠 있는 동안 변수를 붙잡아 둔다
            if self.groups:
                sub.add_separator()
            sub.add_command(label=T('새 탭 만들어 넣기…'), command=lambda: self._tab_edit(None, then_add=sid))
            m.add_cascade(label=T('탭에 넣기'), menu=sub)
            if self.group:
                m.add_command(label=T('[{0}] 탭에서 빼기').format(self.group), command=lambda: self.toggle_member(self.group, sid, False))
            m.add_separator()
        m.add_command(label=T('아바타 꾸미기…'), command=lambda: self.open_avatar(sid))
        m.add_command(label=T('이 세션에 명령 보내기 (세션톡)'), command=lambda: self.open_chat(sid))
        if sid == self.sec_sid:
            m.add_command(label=T('비서 지정 풀기'), command=lambda: self.set_secretary(''),
                          state='normal' if self.sec_pick == sid else 'disabled')
        else:
            m.add_command(label=T('이 세션을 비서로 지정'), command=lambda: self.set_secretary(sid))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def set_secretary(self, sid):
        self.sec_pick = sid
        self.save_cfg()
        self.layout(self.sessions, force=True)

    # ── 창: 아바타, 세션톡, 비서 제안 ──
    def _open_win(self, attr, make):
        w = getattr(self, attr)
        if w and w.win.winfo_exists():
            w.win.deiconify()
            w.win.lift()
            w.win.focus_force()
            return w
        w = make()
        setattr(self, attr, w)
        return w

    def open_avatar(self, sid):
        with self.mon.lock:
            s = self.mon.sessions.get(sid)
            name = self.mon.display_name(s) if s else ''
        if not s:
            return
        if self.avwin and self.avwin.win.winfo_exists():
            self.avwin.win.destroy()
        self.avwin = AvatarWin(self, sid, name)

    def set_avatar(self, sid, av):
        if av:
            self.avatars[sid] = av
        else:
            self.avatars.pop(sid, None)
        self.save_cfg()
        self.frames.pop(sid, None)
        if self.chatwin and self.chatwin.win.winfo_exists():
            self.chatwin.faces.clear()
            self.chatwin.dirty = True
        self.layout(self.sessions, force=True)
        self.panel_ver = -1
        self.update_panel()

    def open_chat(self, sid=None):
        reuse = bool(self.chatwin and self.chatwin.win.winfo_exists())
        w = self._open_win('chatwin', lambda: ChatWin(self))
        if sid:
            w.set_room(sid)
        elif reuse:  # 숨겨 두었던 세션톡: 바뀐 것만 반영하고 맨 아래로
            w.draw(to_end=True)
            w.cv.yview_moveto(1.0)
        self.chat_seen = time.time()
        self.draw_side()

    def open_secretary(self):
        self._open_win('secwin', lambda: SecretaryWin(self))

    def open_meeting(self):
        self._open_win('meetwin', lambda: MeetingWin(self))

    def sec_mark(self, key, done=True):
        if done:
            self.sec_done.add(key)
        else:
            self.sec_done.discard(key)
        self.save_cfg()
        self.draw_sec_board()
        if self.secwin and self.secwin.win.winfo_exists():
            self.secwin.build()

    # ── 비서: 브리핑 요청(버튼을 누를 때만), 선택지 보내기 ──
    def sec_choices(self, it):
        """제안 하나의 선택지 버튼들 [{label, prompt, target, sid, name}] (sid=None 이면 그 세션이 꺼져 있음)."""
        out = []
        chs = it.get('choices') if isinstance(it.get('choices'), list) else []
        for ch in chs[:3]:
            if isinstance(ch, dict) and (ch.get('prompt') or it.get('prompt')):
                # 선택지에 대상이 따로 있으면 그 대상의 id만 (없으면 이름으로), 없으면 제안의 대상
                own = ch.get('target') or ch.get('targetSessionId')
                out.append({'label': one_line(ch.get('label') or T('요청하기'), 12), 'prompt': ch.get('prompt') or it['prompt'],
                            'target': one_line(ch.get('target') or it.get('target') or '', 40),
                            'tsid': str((ch.get('targetSessionId') if own else it.get('targetSessionId')) or '')})
        if not out and it.get('prompt'):
            out.append({'label': T('요청하기'), 'prompt': it['prompt'], 'target': one_line(it.get('target') or '', 40),
                        'tsid': str(it.get('targetSessionId') or '')})
        out = [ch for ch in out if ch['target'] or ch['tsid']]
        with self.mon.lock:
            for ch in out:
                # 세션 이름이 겹치거나 비는 때가 있어 비서가 적은 세션 id(targetSessionId)로 먼저 찾고, 없을 때만 이름으로
                dst = self.mon.sessions.get(ch['tsid']) or self.mon._resolve(ch['target'])
                ch['sid'] = dst.sid if dst else None
                ch['name'] = self.mon.display_name(dst) if dst else (ch['target'] or ch['tsid'][:8])
                ch['target'] = ch['target'] or ch['name']
        return out

    def send_choice(self, it, ch):
        if not ch.get('sid') or ch['sid'] not in self.mon.sessions:
            self.toast(T('"{0}" 세션이 꺼져 있어요 · 세션 켜기에서 먼저 켜 주세요').format(ch["target"]), warn=True)
            return
        try:
            self.mon.send_user(ch['sid'], clean(ch['prompt'], 4000))
        except OSError as ex:
            self.toast(T('보내기 실패: {0}').format(ex), warn=True)
            return
        self.sec_sent[it['_key']] = {'sid': ch['sid'], 'name': ch['name'], 'label': ch['label'], 'ts': time.time(),
                                     'want': one_line(clean(ch['prompt'], 4000))[:40]}
        while len(self.sec_sent) > 100:
            self.sec_sent.pop(next(iter(self.sec_sent)))
        self.sec_mark(it['_key'], True)
        self.toast(T('{0}에 요청했어요 · {1}').format(ch["name"], ch["label"]))

    def answer_ask(self, sid, ask, answers):
        """세션톡(PC·휴대폰)에서 고른 답을 보낸다: 그 질문이 아직 열려 있을 때만. 대화에도 질문·답을 남긴다."""
        cur = read_ask(sid)
        if not cur or cur['id'] != ask['id']:
            self.toast(T('이미 답한 질문이에요'), warn=True)
            return False
        try:
            write_answer(sid, ask['id'], answers)
        except OSError as ex:
            self.toast(T('보내기 실패: {0}').format(ex), warn=True)
            return False
        with self.mon.lock:
            s = self.mon.sessions.get(sid)
            name = self.mon.display_name(s) if s else sid[:8]
            qtext = '\n\n'.join('❓ **' + q['question'] + '**\n' + '\n'.join(f'- {o["label"]}' for o in q['options'])
                                 for q in cur['questions'])
            now = time.time()
            self.mon.chat[f'ask-{ask["id"]}'] = {'id': f'ask-{ask["id"]}', 'from': sid, 'to': 'user', 'fromLabel': name, 'toLabel': T('나'),
                                                 'kind': 'question', 'text': qtext, 'ts': (cur['at'] / 1000) or now - 1}
            self.mon.chat[f'ask-{ask["id"]}-a'] = {'id': f'ask-{ask["id"]}-a', 'from': 'user', 'to': sid, 'fromLabel': T('나'),
                                                   'toLabel': name, 'kind': 'answer', 'text': ' / '.join(answers.values()), 'ts': now,
                                                   'read': True}
            self.mon.chat_ver += 1
        self.toast(T('{0}에 답했어요 · {1}').format(name, ' / '.join(answers.values())))
        return True

    def sec_result(self, key):
        """비서 제안으로 세션에 맡긴 일의 결과: {state: sent|working|done|off, sid, name, label, ts, answer} 또는 None.
        맡긴 세션의 대화 기록에서 그때 보낸 지시문을 찾아 마지막 답변을 붙인다."""
        d = self.sec_sent.get(key)
        if not d:
            return None
        out = dict(d, state='sent', answer='')
        with self.mon.lock:
            s = self.mon.sessions.get(d['sid'])
            if not s:
                out['state'] = 'off'
                return out
            want = d.get('want') or ''
            hit = next((e for e in s.qa if e[0] >= d['ts'] - 30 and
                        (one_line(e[1]).startswith(want) or want.startswith(one_line(e[1])[:40]))), None)
            mode = s.mode()
        if hit:
            out['answer'] = hit[3]
            out['state'] = 'done' if hit[3] and mode != 'busy' else 'working'
        return out

    def briefing(self):
        """브리핑을 부탁한 뒤 비서가 아직 결과(secretary.json)를 안 썼으면 True."""
        if not self.brief_at or time.time() - self.brief_at > 20 * 60:
            return False
        upd = (self.mon.sec or {}).get('updatedAt')
        return not (isinstance(upd, (int, float)) and upd / 1000 >= self.brief_at)

    def ask_brief(self):
        if not self.sec_sid:
            past = next((x for x in (self.sec_pick, (self.mon.sec or {}).get('sessionId')) if x), '')
            if past and glob.glob(os.path.join(PROJ, '*', past + '.jsonl')):
                ans = tkmsg.askyesnocancel(T('비서 세션이 꺼져 있어요'), T('꺼진 비서 세션을 다시 켤까요?\n\n예 = 다시 켜기 · 아니요 = 새 비서 만들기'))
                if ans is None:
                    return
                if ans:
                    self.resume_session(past)
                    return
            elif not tkmsg.askyesno(T('비서 세션 없음'), T('비서 세션이 없어요. 새로 만들까요?\n(새 터미널 창에서 "비서" 세션이 켜집니다)')):
                return
            self.create_secretary()
            return
        if self.briefing():
            self.toast(T('비서가 브리핑하는 중이에요. 끝나면 말풍선이 새로 바뀝니다'))
            return
        try:
            self.mon.send_user(self.sec_sid, BRIEF_TEXT)
        except OSError as ex:
            self.toast(T('보내기 실패: {0}').format(ex), warn=True)
            return
        self.brief_at = time.time()
        self.toast(T('비서에게 브리핑을 부탁했어요'))
        self.draw_sec_board()

    def install_skill(self):
        """secretary 스킬이 없으면 이 프로그램에 든 사본을 ~/.claude/skills/secretary 에 넣는다."""
        if os.path.exists(os.path.join(SKILL_DIR, 'SKILL.md')) or not os.path.isdir(SKILL_SRC):
            return
        shutil.copytree(SKILL_SRC, SKILL_DIR, dirs_exist_ok=True)

    def create_secretary(self):
        try:
            self.install_skill()
        except OSError as ex:
            self.toast(T('스킬 설치 실패: {0}').format(ex), warn=True)
        sid = self.new_session(HOME, T('비서'), SEC_HELLO)
        if sid:
            self.sec_pick = sid
            self.save_cfg()
            self.layout(self.sessions, force=True)

    def new_session(self, cwd, name='', prompt=None):
        """새 콘솔 창에 새 세션을 켠다. 세션 id를 미리 정해 두어 이름·비서 지정을 바로 붙인다."""
        sid = str(uuid.uuid4())
        args = ['--session-id', sid] + (['-n', name] if name else []) + ([prompt] if prompt else [])
        try:
            if name:
                self.mon.set_name(sid, name)
            launch_claude(cwd, args)
        except OSError as ex:
            tkmsg.showerror(T('세션을 켜지 못했어요'), f'{claude_exe()}\n{ex}')
            return None
        self.pending[sid] = (time.time(), name or T('새 세션'))
        self.save_cfg()
        self.toast(T('{0}을 켜는 중… 새 터미널 창을 확인하세요').format(name or T('새 세션')))
        return sid

    def resume_session(self, sid, info=None):
        """꺼진 세션을 원래 작업 폴더에서 claude --resume 으로 다시 켠다."""
        if sid in self.mon.sessions:
            self.select(sid)
            return False
        if not info:
            hits = glob.glob(os.path.join(PROJ, '*', sid + '.jsonl'))
            info = past_session(hits[0]) if hits else None
        cwd = (info or {}).get('cwd')
        if not cwd or not os.path.isdir(cwd):
            tkmsg.showerror(T('세션 켜기'), T('이 세션의 작업 폴더를 찾지 못했어요.\n{0}').format(cwd or sid))
            return False
        try:
            launch_claude(cwd, ['--resume', sid])
        except OSError as ex:
            tkmsg.showerror(T('세션을 켜지 못했어요'), f'{claude_exe()}\n{ex}')
            return False
        name = self.mon.names.get(sid) or (info or {}).get('title') or sid[:8]
        self.pending[sid] = (time.time(), name)
        self.toast(T('{0} 다시 켜는 중… 새 터미널 창을 확인하세요').format(clean(name, 30)))
        return True

    def open_launcher(self):
        if self.launchwin and self.launchwin.win.winfo_exists():
            self.launchwin.win.destroy()  # 꺼진 세션 목록은 열 때마다 새로
        self.launchwin = LaunchWin(self)

    def type_speed(self):
        v = self.cfg.get('type_speed')
        return v if v in dict(TYPE_SPEEDS) else 'normal'

    def type_scale(self):
        return dict(TYPE_SPEEDS)[self.type_speed()]

    def cycle_type_speed(self):
        keys = [k for k, _ in TYPE_SPEEDS]
        self.cfg['type_speed'] = keys[(keys.index(self.type_speed()) + 1) % len(keys)]
        self.save_cfg()
        self.toast(T('회의실 대사 속도: {0}').format(TYPE_SPEED_TEXT[self.type_speed()]))

    # ── 원격 조종 (휴대폰 웹앱) ──
    def open_remote(self):
        self._open_win('remotewin', lambda: RemoteWin(self))

    def remote_obj(self):
        if self.remote is None:
            if APP_DIR not in sys.path:
                sys.path.insert(0, APP_DIR)
            import office_remote
            self.remote = office_remote.Remote(self, _ThisModule())
        return self.remote

    def remote_start(self):
        r = self.remote_obj()
        if not r.start():
            self.toast(T('cloudflared가 없어 원격을 켤 수 없어요 · 원격 조종 화면에서 받을 수 있어요'), warn=True)
            return False
        self.cfg['remote_on'] = True
        self.save_cfg()
        self._ensure_pump()
        return True

    def _ensure_pump(self):
        if not getattr(self, '_pumping', False):
            self._remote_pump()

    def remote_preview(self):
        """휴대폰 웹앱을 이 PC에서 휴대폰 크기 창으로 띄운다 (터널 없이 PC 안에서만, 실제 세션이 그대로 보임)."""
        r = self.remote_obj()
        r.start(tunnel=False)
        self._ensure_pump()
        link = r.preview_link()
        exe = next((x for x in (
            os.path.join(os.environ.get('ProgramFiles', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(LOCAL, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(os.environ.get('ProgramFiles', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe')) if os.path.isfile(x)), None)
        if exe:  # 따로 쓰는 프로필로 앱 창 (평소 브라우저와 섞이지 않게)
            subprocess.Popen([exe, f'--app={link}', '--window-size=420,880', '--no-first-run', '--no-default-browser-check',
                              '--user-data-dir=' + os.path.join(CACHE_DIR, 'preview-browser')])
        else:
            webbrowser.open(link)
        self.toast(T('휴대폰 화면 미리보기를 열었어요 · 이 PC 안에서만 연결돼요'))

    def remote_stop(self):
        if self.remote:
            self.remote.stop()
        self.cfg['remote_on'] = False
        self.save_cfg()

    def _remote_pump(self):
        """휴대폰 요청 중 사무실 화면 상태를 만지는 일은 Tk 스레드에서 처리한다."""
        r = self.remote
        if not r or not r.httpd:
            self._pumping = False
            return
        self._pumping = True
        r.pump()
        self.root.after(80, self._remote_pump)

    def remote_watch(self, sessions):
        """휴대폰에 알릴 일: 확인이 필요해진 세션, 도착한 회의 보고서, 끝난 브리핑. 원격이 꺼져 있으면 기준만 맞춰 둔다."""
        r = self.remote
        on = bool(r and r.httpd)
        modes = {s.sid: s.mode() for s in sessions}
        old = getattr(self, '_rw_modes', None)
        if on and old is not None:
            with self.mon.lock:
                for s in sessions:
                    if modes[s.sid] == 'wait' and old.get(s.sid) != 'wait':
                        what = ' · '.join(x for x in (s.waiting, one_line(s.last_tool, 60)) if x)
                        r.alert(self.mon.display_name(s), T('확인이 필요해요') + (f' · {what}' if what else ''), s.sid)
        self._rw_modes = modes
        m = read_json(MEET_FILE)
        got = set()
        if isinstance(m, dict) and isinstance(m.get('members'), list):
            folder = os.path.join(MEET_DIR, str(m.get('id')))
            got = {(m.get('id'), sid) for sid in m['members'] if os.path.isfile(os.path.join(folder, sid + '.json'))}
            old_got = getattr(self, '_rw_reports', None)
            if on and old_got is not None:
                for _mid, sid in got - old_got:
                    r.alert((m.get('names') or {}).get(sid) or sid[:8], T('회의 보고서가 도착했어요'), sid)
        self._rw_reports = got
        b = self.briefing()
        if on and getattr(self, '_rw_brief', False) and not b and self.brief_at:
            r.alert(T('비서'), T('브리핑이 끝났어요 · 새 제안을 확인해 보세요'), self.sec_sid or '')
        self._rw_brief = b

    def _maybe_onboard(self):
        if self.sec_sid or self.cfg.get('onboarded'):
            self.cfg['onboarded'] = True
            self._maybe_mod()
            return
        OnboardWin(self)

    def _maybe_mod(self):
        """처음 한 번(또는 더 새 mod가 생겼을 때) session-panel mod 설치를 권한다."""
        st = mod_state()
        if st == 'ok' or (st != 'old' and self.cfg.get('mod_asked')) or (st == 'old' and self.cfg.get('mod_asked_ver') == list(_mod_ver(MOD_SRC))):
            return
        self.mod_dialog(first=True)

    def mod_dialog(self, first=False):
        """session-panel mod 설치·업데이트. 세션톡·휴대폰 명령 배달과 진행도 표시에 필요하다."""
        st = mod_state()
        if st == 'ok':
            if not self.offer_mod_restart(force=True):
                tkmsg.showinfo(T('세션 연결 mod'), T('session-panel mod가 설치되어 켜져 있어요.'), parent=self.root)
            return
        text = (T('이 프로그램에 든 session-panel mod가 설치된 것보다 새것이에요. 업데이트할까요?\n새로 켜는 Claude Code 세션부터 적용돼요.')
                if st == 'old' else
                T('Claude 데스크탑 앱(Code 탭)에서 켠 세션도 mod가 바뀌면 다시 켜지 않고 바로 다시 불러오게 설정을 하나 더할까요?\n'
                  '(~/.claude/settings.json 의 env 에 CLAUDE_CODE_PLUGIN_DIR_WATCH=1 · 원래 파일은 settings.json.office-backup 으로 남겨요)')
                if st == 'nowatch' else
                T('세션톡·휴대폰에서 보낸 명령을 세션에 전하고 진행도를 보여 주는 session-panel mod가 이 PC에서 켜져 있지 않아요.\n\n'
                  '설치하면:\n· ~/.claude/mods/session-panel 에 mod를 복사해요\n'
                  '· ~/.claude/settings.json 에 불러오기 설정을 더해요 (원래 파일은 settings.json.office-backup 으로 남겨요)\n\n'
                  '새로 켜는 Claude Code 세션부터 적용돼요. 설치할까요?'))
        if not tkmsg.askyesno(T('세션 연결 mod'), text, parent=self.root):
            if first:
                self.cfg['mod_asked'] = True
                if st == 'old':
                    self.cfg['mod_asked_ver'] = list(_mod_ver(MOD_SRC))
                self.save_cfg()
                self.toast(T('나중에 메뉴 > 세션 연결 mod 에서 설치할 수 있어요'))
            return
        try:
            install_mod()
        except (OSError, ValueError) as ex:
            tkmsg.showerror(T('세션 연결 mod'), T('mod를 설치하지 못했어요: {0}\nREADME의 수동 설치 방법을 참고하세요.').format(ex), parent=self.root)
            return
        self.toast(T('session-panel mod를 설치했어요 · 새로 켜는 세션부터 적용돼요'))
        self.root.after(1500, lambda: self.offer_mod_restart(force=True))

    def stale_sessions(self):
        """mod 없이 켜진 세션 (mod를 설치하기 전부터 켜져 있던 터미널 세션): 켠 지 MOD_STALE초가 지났는데 mod 상태 파일이 없거나 멈춤."""
        now = time.time()
        out = []
        with self.mon.lock:
            items = [(sid, s.kind, s.started, s.pid, self.mon.display_name(s)) for sid, s in self.mon.sessions.items()]
        for sid, kind, started, pid, name in items:
            if kind != 'interactive' or not pid or sid in self.mod_restart or now - started < MOD_STALE:  # 터미널 세션만 (claude -p·데스크탑 제외)
                continue
            try:
                fresh = now - os.path.getmtime(os.path.join(COLLAB, f'peer-{sid}.json')) < MOD_STALE
            except OSError:
                fresh = False
            if not fresh:
                out.append((sid, name))
        return out

    def offer_mod_restart(self, force=False):
        """mod 없이 켜진 세션이 있으면, 대화를 그대로 이어서(--resume) 다시 켜 mod를 적용할지 묻는다.
        실행 중인 세션은 mod를 새로 불러올 수 없어서 다시 켜야 한다. 물어본 세션은 다시 묻지 않는다(force면 다시). 물었으면 True."""
        if mod_state() not in ('ok', 'nowatch'):
            return False
        asked = set(self.cfg.get('mod_restart_asked') or [])
        stale = [x for x in self.stale_sessions() if force or x[0] not in asked]
        if not stale:
            return False
        self.cfg['mod_restart_asked'] = sorted(asked | {sid for sid, _n in stale})[-200:]
        self.save_cfg()
        names = '\n'.join('· ' + clean(n, 40) for _sid, n in stale[:12]) + ('\n…' if len(stale) > 12 else '')
        if not tkmsg.askyesno(T('세션 연결 mod'), T('session-panel mod 없이 켜져 있는 세션이 {0}개 있어요 (mod를 설치하기 전에 켠 세션).\n{1}\n\n'
                                                   '이런 세션은 세션톡·휴대폰 명령을 받지 못하고 진행도도 안 보여요. 켜져 있는 세션은 mod를 새로 불러올 수 없어서, '
                                                   '대화를 그대로 이어서(--resume) 새 터미널 창으로 다시 켜야 해요.\n'
                                                   '· 쉬는 세션은 바로, 일하는 세션은 일이 끝나면 다시 켜요\n· 원래 터미널 창(탭)은 닫혀요\n\n지금 다시 켤까요?')
                              .format(len(stale), names), parent=self.root):
            return True
        for sid, _n in stale:
            self.mod_restart[sid] = time.time()
        self._mod_restart_tick()
        return True

    def _mod_restart_tick(self):
        """다시 켜기를 기다리는 세션: 쉬고 있으면 끄고 같은 폴더에서 claude --resume 으로 다시 켠다."""
        for sid in list(self.mod_restart):
            with self.mon.lock:
                s = self.mon.sessions.get(sid)
                info = (s.mode(), s.pid, s.cwd, self.mon.display_name(s)) if s else None
            if not info:  # 그 사이 사용자가 껐다
                del self.mod_restart[sid]
                continue
            mode, pid, cwd, name = info
            if mode in ('busy', 'wait') or not pid or not os.path.isdir(cwd):
                continue
            del self.mod_restart[sid]
            try:
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except (OSError, subprocess.SubprocessError) as ex:
                print('mod restart kill error', ex, file=sys.stderr)
                continue
            self._relaunch_when_gone(sid, pid, cwd, name, time.time())
        if self.mod_restart:
            self.root.after(5000, self._mod_restart_tick)

    def _relaunch_when_gone(self, sid, pid, cwd, name, t0):
        if proc_stat(pid) and time.time() - t0 < 20:
            self.root.after(500, lambda: self._relaunch_when_gone(sid, pid, cwd, name, t0))
            return
        try:
            launch_claude(cwd, ['--resume', sid])
        except OSError as ex:
            tkmsg.showerror(T('세션을 켜지 못했어요'), f'{claude_exe()}\n{ex}')
            return
        self.pending[sid] = (time.time(), name)
        self.toast(T('{0} 다시 켜는 중… 새 터미널 창을 확인하세요').format(clean(name, 30)))

    # ── 세션 이름 바꾸기 ──
    def rename_click(self):
        if self.renaming:
            self.rename_end(True)
        elif self.sel:
            self.rename_start()

    def rename_start(self):
        with self.mon.lock:
            s = self.mon.sessions.get(self.sel)
            name = self.mon.display_name(s) if s else ''
        if not s:
            return
        self.renaming = self.sel
        self.p_entry.delete(0, 'end')
        self.p_entry.insert(0, name)
        self.draw_head()
        self.p_entry.select_range(0, 'end')
        self.p_entry.focus_set()

    def rename_end(self, save):
        sid, self.renaming = self.renaming, None
        if not sid:
            return
        name = one_line(self.p_entry.get(), 40)
        self.draw_head()
        self.root.focus_set()
        if save:
            try:
                self.mon.set_name(sid, name)
            except OSError as ex:
                print('rename error', ex, file=sys.stderr)
        self.seen_ver = -1  # 책상 명패·분배판도 다시
        self.panel_ver = -1
        self.refresh(again=False)

    def select(self, sid):
        if self.renaming and self.renaming != sid:
            self.rename_end(False)
        old, self.sel = self.sel, sid
        if old in self.desks:
            self.cv.itemconfig(self.desks[old].sel, outline=HOVER_LINE if old == self.hover else '')
        if sid in self.desks:
            self.cv.itemconfig(self.desks[sid].sel, outline=ACCENT)
        # 패널 채우기는 클릭 처리 뒤로 미루고, 연달아 누르면 마지막 것만 그린다
        if self._panel_job:
            self.root.after_cancel(self._panel_job)
        self._panel_job = self.root.after(30, self._fill_panel)

    def _fill_panel(self):
        self._panel_job = None
        self.panel_ver = -1
        self.update_panel()

    def update_panel(self, light=False):
        mon = self.mon
        with mon.lock:
            s = mon.sessions.get(self.sel)
            if s is None:
                return
            first_fill = s.sid != self.panel_sid
            if first_fill:
                self.panel_sid, self.panel_seq, self.panel_ver = s.sid, 0, -1
                self.p_log.delete('1.0', 'end')
                self.unseen = 0
                self._tab_label()
                light = False
            new_events = [e for e in s.events if e[0] > self.panel_seq]
            if first_fill:
                new_events = new_events[-LOG_FIRST:]
            ver, mode, name, peer, acc = s.ver, s.mode(), mon.display_name(s), dict(s.peer), s.acc
            pid, mem, cpu, cwd, started, waiting, last_act = s.pid, s.mem, s.cpu, s.cwd, s.started, s.waiting, s.last_act
            done = all(tr.back_done for tr in s.tracks.values())
            tpath = next(iter(s.tracks), None)
            sid = s.sid
            qa_key = (sid, s.qa_ver, mode, s.qa_loading)
            qa = [list(e) for e in s.qa] if qa_key != self.qa_ver else None
        self.p_info[T('프로세스')].configure(text=f'PID {pid} · {mem / 1048576:.0f}MB · CPU {cpu:.1f}%' if pid else T('Claude 데스크탑 앱 (Code 탭)'))
        if light:
            return
        if qa is not None:
            self.qa_ver = qa_key
            self.fill_qa(sid, qa, mode, qa_key[3])
        fr = self.frames.get(sid)
        since = ''
        if last_act:
            m = int((time.time() - last_act) / 60)
            since = T('방금 활동') if m < 1 else T('마지막 활동 {0}분 전').format(m) if m < 120 else T('마지막 활동 {0}시간 전').format(m // 60)
        self.head = {'name': name, 'mode': mode, 'waiting': waiting, 'since': since, 'sub': T('{0}  ·  세션 {1}').format(cwd, sid[:8]),
                     'img': fr[0]['type_a' if mode == 'busy' else 'raise' if mode == 'wait' else 'sleep' if mode == 'sleep'
                                  else 'idle'] if fr else None}
        self.draw_head()
        self.p_info[T('시작')].configure(text=datetime.datetime.fromtimestamp(started).strftime(T('%m월 %d일 %H:%M')) if started else '-')
        self.p_info[T('모델')].configure(text=acc.model or '-')
        self.p_info[T('컨텍스트')].configure(text=T('{0} 토큰').format(fmt_n(acc.ctx)))
        self.p_path = tpath
        try:
            self.p_info[T('대화 기록')].configure(text=T('{0} · 폴더 열기 ›').format(fmt_bytes(os.path.getsize(tpath))) if tpath else '-')
        except OSError:
            self.p_info[T('대화 기록')].configure(text='-')
        dv = acc.days.get(today(), [0, 0, 0, 0])
        t = acc.tot
        self.p_tiles['t_in'].configure(text=fmt_n(dv[0] + dv[1] + dv[2]))
        self.p_tiles['t_out'].configure(text=fmt_n(dv[3]))
        self.p_tiles['a_in'].configure(text=fmt_n(t[0] + t[1] + t[2]) if done else T('집계 중'), fg=FG if done else DIM)
        self.p_tiles['a_out'].configure(text=fmt_n(t[3]) if done else '…', fg=FG if done else DIM)
        vals = dv if sum(dv) else t
        self.p_bar_title.configure(text=T('오늘 토큰 구성') if sum(dv) else T('전체 토큰 구성'))
        for l, (n, _c), v in zip(self.p_legend, TOK_PARTS, vals):
            l.configure(text=f'■ {n} {fmt_n(v)}')
        if vals != self.bar_vals:
            self.bar_vals = list(vals)
            self.draw_bar()

        if ver != self.panel_ver:
            self.panel_ver = ver
            w = self.p_tasks
            w.delete('1.0', 'end')
            if peer.get('goal') or peer.get('tasks'):
                w.insert('end', T('목표  '), 'dim')
                w.insert('end', clean(peer.get('goal') or '-') + '\n', 'bold')
                for tk_ in peer.get('tasks') or []:
                    stt, title = tk_.get('status'), clean(tk_.get('title', ''))
                    if stt == 'completed':
                        w.insert('end', '  ✔  ' + title + '\n', 'done')
                    elif stt == 'in_progress':
                        p = tk_.get('percent')
                        w.insert('end', '  ▶  ' + title, ('doing', 'bold'))
                        if isinstance(p, (int, float)):
                            w.insert('end', f'   {bar_text(p)} {p:.0f}%', 'doing')
                        w.insert('end', '\n')
                    else:
                        w.insert('end', '  ○  ' + title + '\n', 'todo')
                if peer.get('activity'):
                    w.insert('end', T('\n지금  '), 'dim')
                    w.insert('end', one_line(peer['activity'], 140) + '\n')
            else:
                w.insert('end', T('진행 패널 보고가 아직 없습니다.\n'), 'dim')
            if peer.get('request'):
                w.insert('end', T('\n받은 요청  '), 'dim')
                w.insert('end', one_line(peer['request'], 300) + '\n')
            lines = int(w.index('end-1c').split('.')[0])
            w.configure(height=max(3, min(14, lines)))

        if new_events:
            log = self.p_log
            at_end = log.yview()[1] > 0.98
            for seq, ts, kind, text in new_events:
                tags = ('hang', 'k_' + kind)
                log.insert('end', datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S ') if ts else '', tags + ('dim',))
                log.insert('end', LOG_ICON.get(kind, '·') + ' ', tags + (kind,))
                log.insert('end', text + '\n', tags + (kind,))
            self.panel_seq = new_events[-1][0]
            if self.tab != 'log' and not first_fill:
                self.unseen += len(new_events)
                self._tab_label()
            lines = int(log.index('end-1c').split('.')[0])
            if lines > 800:
                log.delete('1.0', f'{lines - 600}.0')
            if at_end:
                log.see('end')

    def fill_qa(self, sid, qa, mode, loading=False):
        w = self.p_qa
        keys = [e[0] for e in qa]
        at_end = w.yview()[1] > 0.98
        if sid == self.qa_sid and keys and keys == self.qa_keys:
            w.delete('qa_last', 'end')  # 같은 요청의 답변만 바뀌었다 → 마지막 것만 다시
            qa = qa[-1:]
        else:
            w.delete('1.0', 'end')
            at_end = True
            if not qa:
                w.insert('end', T('대화 기록에서 요청을 찾는 중…') if loading else T('아직 주고받은 요청이 없습니다.'), 'wait')
        self.qa_sid, self.qa_keys = sid, keys
        for qts, q, ats, a in qa:
            last = qts == keys[-1]
            if last:
                w.mark_set('qa_last', 'end-1c')
                w.mark_gravity('qa_last', 'left')
            if w.index('end-1c') != '1.0':
                w.insert('end', '─' * 30 + '\n', 'sep')
            w.insert('end', ' Q ', 'qb')
            w.insert('end', '  ' + self._qa_time(qts) + '\n', 'time')
            w.insert('end', q + '\n', ('body', 'q'))
            w.insert('end', ' A ', 'ab')
            if a:
                busy = last and mode in ('busy', 'wait')
                w.insert('end', '  ' + self._qa_time(ats) + (T('  · 작업 중, 중간 답변') if busy else '') + '\n', 'time')
                w.insert('end', re.sub(r'\n\s*\n+', '\n', a).replace('**', '') + '\n', 'body')
            else:
                w.insert('end', '\n')
                w.insert('end', (T('작업 중…') if last and mode in ('busy', 'wait') else T('(답변 없음)')) + '\n', ('body', 'wait'))
        if at_end:
            w.see('end')

    @staticmethod
    def _qa_time(ts):
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime('%H:%M' if dt.date() == datetime.date.today() else T('%m월 %d일 %H:%M'))

    # ── 애니메이션 ──
    def animate(self):
        try:
            if self.visible and self.anim_on.get():
                k = int(time.time() * 4)
                if k != self.anim_k:
                    self.anim_k = k
                    # 화면에 보이는 책상만 움직인다 (굴려서 가려진 책상은 건너뜀)
                    vy0 = self.cv.canvasy(0)
                    vy1 = vy0 + self.cv.winfo_height()
                    for d in self.desks.values():
                        if d.box[3] >= vy0 and d.box[1] <= vy1:
                            self.anim_desk(d, k)
                if self.flights:
                    self.step_flights()
        except Exception as ex:
            print('anim error', ex, file=sys.stderr)
        self.root.after(50 if self.flights else 250, self.animate)

    def anim_desk(self, d, k):
        """바뀐 것만 Tk에 알린다 (그대로인 도형에 itemconfig를 다시 보내지 않는다)."""
        c, pt = self.cv, d.pt
        m = d.mode
        if m == 'busy':
            img = 'type_a' if k % 2 else 'type_b'
        elif m == 'wait':
            img = 'raise'
        elif m == 'sleep':
            img = 'sleep'
        else:
            # 가끔 눈을 깜빡이고, 평소엔 숨 쉬듯 머리가 한 칸 오르내린다
            img = 'blink' if (k + d.seed) % 23 == 0 else 'breath' if (k // 3 + d.seed) % 2 else 'idle'
        if img != d.cur_img:
            d.cur_img = img
            c.itemconfig(d.spr, image=d.frames[img])
        # 모드가 바뀔 때만: 모니터 화면, 김·Z 보이기
        if d.cur_scr != m:
            d.cur_scr, d.cur_phase = m, None
            if m == 'busy':
                c.itemconfig(d.scr, fill='#16202e')
                for i, ln in enumerate(d.lines):
                    c.itemconfig(ln, fill=LINE_COLS[i], state='normal')
            elif m == 'wait':
                for ln in d.lines:
                    c.itemconfig(ln, state='hidden')
            elif m == 'sleep':
                c.itemconfig(d.scr, fill='#0b0d12')
                for i, ln in enumerate(d.lines):
                    c.itemconfig(ln, state='hidden' if i else 'normal', fill='#3a4a6a')
            else:
                c.itemconfig(d.scr, fill='#24324a')
                for i, ln in enumerate(d.lines):
                    c.itemconfig(ln, state='normal', fill='#4d6a94')
                    c.coords(ln, *pt(173, 78 + i * 6, 181 + (i * 9 + d.seed) % 26, 81 + i * 6))
            for st in d.steam:
                c.itemconfig(st, state='normal' if m == 'idle' else 'hidden')
            for z in d.zs:
                c.itemconfig(z, state='normal' if m == 'sleep' else 'hidden')
        if m == 'busy':  # 타자 치는 화면 줄은 1초에 4번
            for i, ln in enumerate(d.lines):
                w = 6 + (k * 7 + i * 13 + d.seed) % 34
                ind = ((k + i) // 3 % 3) * 4
                c.coords(ln, *pt(173 + ind, 78 + i * 6, 173 + ind + w, 81 + i * 6))
            return
        # 나머지 움직임은 1초에 2번이면 충분하다
        phase = k // 2
        if phase == d.cur_phase:
            return
        d.cur_phase = phase
        if m == 'wait':
            on = phase % 2 == 0
            c.itemconfig(d.scr, fill='#f0b429' if on else '#7a5a10')
            self.set_bubble(d, d.bubble_text, '#ffe066' if on else '#fff3b8')
        elif m == 'sleep':
            x = 172 + (phase * 6) % 42
            y = 76 + abs((phase * 4) % 52 - 26)
            c.coords(d.lines[0], *pt(x, y, x + 3, y + 3))
            for i, z in enumerate(d.zs):
                ph = (phase + i * 2) % 6
                c.coords(z, *pt(151 + ph * 4 + i * 4, 70 - ph * 8))
        elif m == 'idle':
            for i, st in enumerate(d.steam):
                dx = 2 if (phase + i) % 2 else -2
                x = 53 + i * 5
                c.coords(st, *pt(x, 96, x + dx, 90, x, 84))

    def desk_center(self, sid):
        d = self.desks.get(sid)
        if not d:
            bx0, bx1 = self.wb
            return self.P((bx0 + bx1) / 2, 80)
        return d.pt(130, 80)

    def launch_flight(self, src, dst):
        if src not in self.desks:
            return
        item = self.cv.create_polygon(0, 0, 0, 0, 0, 0, fill='#ffffff', outline='#5a6170', width=self.Z, tags='fly')
        self.flights.append({'p0': self.desk_center(src), 'p1': self.desk_center(dst), 't0': time.time(), 'item': item})

    def step_flights(self):
        now, Z = time.time(), self.Z
        keep = []
        for f in self.flights:
            t = (now - f['t0']) / 1.6
            if t >= 1 or not self.anim_on.get():
                self.cv.delete(f['item'])
                continue
            (x0, y0), (x1, y1) = f['p0'], f['p1']
            arc = 90 * Z
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t - math.sin(math.pi * t) * arc
            ang = math.atan2((y1 - y0) - math.cos(math.pi * t) * arc * math.pi, x1 - x0)
            ca, sa = math.cos(ang), math.sin(ang)
            coords = []
            for px, py in ((14, 0), (-9, -8), (-9, 8)):
                coords += [x + (px * ca - py * sa) * Z, y + (px * sa + py * ca) * Z]
            self.cv.coords(f['item'], *coords)
            self.cv.tag_raise(f['item'])
            keep.append(f)
        self.flights = keep


def selftest():
    """GUI 없이 몇 번 폴링해서 요약 출력 (검증용)."""
    mon = Monitor()
    for _ in range(2):
        mon.poll()
        time.sleep(0.5)
    for s in mon.sessions.values():
        a = s.acc
        print(f'{s.sid[:8]} pid={s.pid} mode={s.mode():5} name={mon.display_name(s)!r} '
              f'mem={s.mem / 1048576:.0f}MB events={len(s.events)} tracks={len(s.tracks)} model={a.model} ctx={a.ctx} '
              f'last_tool={s.last_tool[:40]!r} pct={s.overall()}')
    t0 = time.time()
    for s in mon.sessions.values():
        for tr in s.tracks.values():
            tr.backfill(mon.stop)
    print(f'backfill {time.time() - t0:.1f}s')
    for s in mon.sessions.values():
        mon._poll_transcripts(s, time.time())
        t = s.acc.tot
        print(f'{s.sid[:8]} total in={fmt_n(t[0])} cc={fmt_n(t[1])} cr={fmt_n(t[2])} out={fmt_n(t[3])} today={s.acc.days.get(today())}')
    mon.save_cache(force=True)


def main():
    try:
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL: 일하는 세션보다 뒤로
    except Exception:
        pass
    if '--selftest' in sys.argv:
        selftest()
        return
    mon = Monitor()
    mon.start()
    load_fonts()
    root = tk.Tk()
    App(root, mon)
    root.mainloop()
    mon.stop.set()


if __name__ == '__main__':
    main()
