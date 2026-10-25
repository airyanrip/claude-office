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
QA_SCAN = 1_500_000_000         # 대화 탭: 요청을 찾으러 거슬러 올라가는 최대 바이트
QA_ANSWER_SCAN = 64_000_000     # 대화 탭: 요청 하나의 답변을 찾는 최대 바이트
LIVE_CHUNK = 16 * 1024 * 1024   # 한 번 폴링에 읽는 최대 바이트
SLEEP_AFTER = 15 * 60           # 이만큼 조용하면 조는 캐릭터
UI = 'Malgun Gothic'            # Pretendard를 못 올렸을 때 쓰는 글꼴
APP_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(APP_DIR, 'fonts')   # Pretendard (SIL OFL 1.1, 무료)
SKILL_SRC = os.path.join(APP_DIR, 'assets', 'secretary')
SKILL_DIR = os.path.join(CLAUDE, 'skills', 'secretary')


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
        self.tracks = {}
        self.sub_scan = 0
        self.acc = Acc()
        self.cpu = 0.0
        self.mem = 0
        self.cpu_prev = None
        self.ver = 0

    def add_event(self, ts, kind, text):
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
                s.pid = int(d['pid'])
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

    def send_user(self, sid, text):
        """대화창에서 사용자가 쓴 명령을 out-office-user.json에 적는다. 진행 패널 mod가 그 세션에 사용자 요청으로 넣는다."""
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
               'to': sid, 'kind': 'request', 'text': text, 'at': now_ms}
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
CHAT = {  # 세션톡 (카카오톡 풍)
    'bg': _t('#b2c7d9', '#1b2430'), 'head': _t('#a9bfd2', '#161e28'), 'title': _t('#1e2329', '#e6ebf0'),
    'sub': _t('#46525e', '#8d99a8'), 'other': _t('#ffffff', '#2a3440'), 'text': _t('#191919', '#e6ebf0'),
    'name': _t('#2b333c', '#d5dde5'), 'time': _t('#56636f', '#8d99a8'), 'pill': _t('#93a8bc', '#2c3846'),
    'empty': _t('#4f5d6b', '#8d99a8'), 'typing': _t('#6b7580', '#8d99a8'), 'input': _t('#ffffff', '#1b2129'),
    'input_fg': _t('#1e1e1e', '#e6ebf0'), 'input_edge': _t('#dfe3e8', '#2b343f'), 'chip': _t('#eef1f4', '#242c36'),
    'hint': _t('#8a929c', '#8d99a8'),
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
SEC_HELLO = (T('당신은 이 PC의 비서 세션입니다. secretary 스킬(~/.claude/skills/secretary/SKILL.md)을 읽고 역할을 익히세요. '
             '사용자가 세션 사무실의 [브리핑 받기] 버튼을 누르거나 직접 요청할 때만 브리핑하고, 그 전에는 아무 작업도 하지 마세요. '
             '지금은 준비됐다고 한 줄로만 답하세요.'))
MEET_DIR = os.path.join(COLLAB, 'meeting')                 # 회의실: 회의마다 하위 폴더에 세션별 보고서·캡처
MEET_FILE = os.path.join(MEET_DIR, 'meeting.json')         # 지금 열린 회의 {id, startedAt, turns, members}
CAPTURE_PS1 = os.path.join(APP_DIR, 'assets', 'capture.ps1')
MEET_TURNS = (1, 3, 5, 10)
REPORT_ST = {'done': (T('완료'), '#2fb985'), 'partial': (T('일부'), '#f0a020'), 'todo': (T('못 함'), RED)}


def meet_prompt(n, reqs, folder, sid):
    """회의실에서 세션에 보내는 업무 보고 요청. 보고서는 folder/<sid>.json, 캡처는 folder/<sid>-N.png."""
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
                                                          os.path.basename(shot))


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


class PageHost(tk.Frame):
    """메뉴 기능을 별도 창 대신 사무실 화면 자리에 띄우는 틀. 창(Toplevel)처럼 불러도 되게 창 전용 메서드는 받아 넘긴다."""

    def __init__(self, app, key):
        super().__init__(app.page_wrap, bg=BG)
        self.app, self.key = app, key
        self.pack(fill='both', expand=True)

    def title(self, text=None):
        if text:
            self.app.page_title.configure(text=str(text).split(' · ')[0])

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
        pass


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
        app._btn(head, T('다시 계산'), self.scan).pack(side='right')
        tk.Label(w, text=T('보기와 폴더 열기만 합니다 (아무것도 지우지 않음). 크기는 백그라운드에서 셉니다.'),
                 font=F['small'], bg=BG, fg=DIM, anchor='w', padx=14).pack(fill='x', pady=(6, 0))
        body = tk.Frame(w, bg=BG)
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
        w.bind('<MouseWheel>', lambda e: cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.stop = threading.Event()
        self.results = collections.deque()
        self.rows = {}
        self.build()
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
        self.faces = {}
        self.read = {}          # 방 → 마지막으로 본 시각
        self.sig = None
        self.room_sig = None
        self.room_ids, self.room_rows = None, {}
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
        tk.Label(sh, text=T('세션끼리 주고받은 일'), font=F['small'], bg=CARD, fg=DIM).pack(side='left', padx=8, pady=(4, 0))
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

    def _mark(self):
        self.dirty = True

    def set_room(self, room):
        self.room = room
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
            self.app.mon.send_user(self.target, text)
        except OSError as ex:
            tkmsg.showerror(T('보내기 실패'), str(ex), parent=self.win)
            return
        self.inp.delete('1.0', 'end')
        self.dirty = True
        self.draw(to_end=True)

    def face(self, sid, size=2):
        key = (sid, size)
        if key not in self.faces:
            self.faces[key] = make_frames(sid, size * self.app.Z, self.app.avatars.get(sid))[0]['idle']
        return self.faces[key]

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
        out, typing = [], set()
        for m in msgs:
            m = dict(m)
            out.append(m)
            if m['from'] != 'user' or m['to'] not in qa:
                continue
            # 내가 보낸 명령이 그 세션의 요청으로 들어갔는지 대화 기록에서 찾아 '읽음'과 마지막 답변을 붙인다
            want = one_line(m['text'])[:40]
            hit = next((e for e in qa[m['to']] if e[0] >= m['ts'] - 30 and
                        (one_line(e[1]).startswith(want) or want.startswith(one_line(e[1])[:40]))), None)
            if hit:
                m['read'] = True
                if hit[3]:
                    out.append({'id': m['id'] + '-a', 'from': m['to'], 'fromLabel': names.get(m['to'], ''), 'to': 'user',
                                'toLabel': T('나'), 'kind': 'answer', 'text': hit[3], 'ts': max(hit[2], m['ts'] + 0.001),
                                'reply': True})
                elif modes.get(m['to']) in ('busy', 'wait'):
                    typing.add(m['to'])
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
        try:
            self.dots = (self.dots + 1) % 4
            self.draw()
        except Exception as ex:
            print('chat error', ex, file=sys.stderr)
        self.win.after(1000, self._tick)

    def draw(self, to_end=False):
        mon = self.app.mon
        with mon.lock:
            sig = (mon.chat_ver, tuple((s.sid, s.qa_ver, s.mode(), mon.display_name(s)) for s in mon.sessions.values()),
                   self.room, self.target)
        if sig == self.sig and not self.dirty and not getattr(self, 'typing', None):
            return
        self.sig, self.dirty = sig, False
        msgs, live, names, typing = self.collect()
        self.typing = typing
        self.names = names
        self.draw_rooms(msgs, live, names)
        room = self.room
        if room != '*' and room not in names and not any(self.in_room(m, room) for m in msgs):
            room = self.room = '*'
        shown = [m for m in msgs if self.in_room(m, room)][-200:]
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
        self.draw_chat(shown, names, typing if room == '*' else typing & {room}, to_end)

    def draw_rooms(self, msgs, live, names):
        rooms = ['*'] + [s.sid for s in live]
        for m in msgs:  # 꺼진 세션과 주고받은 기록도 방으로 남긴다
            for sid in (m['from'], m['to']):
                if sid not in rooms and sid not in ('user', '*', 'all', T('전체')) and len(sid) > 20:
                    rooms.append(sid)
        info = []
        for r in rooms:
            ms = [m for m in msgs if self.in_room(m, r)]
            last = ms[-1] if ms else None
            unread = sum(1 for m in ms if m['ts'] > self.read.get(r, self.app.chat_seen) and m['from'] != 'user')
            label = T('전체 대화') if r == '*' else names.get(r) or next((m['fromLabel'] for m in ms if m['from'] == r), r[:8])
            info.append((r, label, one_line(last['text'], 40) if last else '', last['ts'] if last else 0,
                         0 if r == self.room else unread))
        sig = (tuple(info), self.room)
        if sig == self.room_sig:
            return
        self.room_sig = sig
        # 방 목록이 그대로면 줄을 다시 만들지 않고 글자·색만 바꾼다 (다시 만들면 목록이 접혔다 펴지며 튄다)
        ids = [r for r, *_ in info]
        if ids != self.room_ids:
            for c in self.rooms.winfo_children():
                c.destroy()
            self.room_rows = {r: self._room_row(r) for r in ids}
            self.room_ids = ids
        F = self.app.fonts
        for r, label, prev, ts, unread in info:
            w = self.room_rows[r]
            bg = MINT if r == self.room else CARD
            for wd in w['bgs']:
                wd.configure(bg=bg)
            room_w = int(280 * self.app.Z) - 110
            when = self._when(ts) if ts else ''
            w['when'].configure(text=when)
            if when:
                room_w -= F['small'].measure(when) + 6
            w['name'].configure(text=fit(label, F['bodyb'], room_w), fg=FG if r in names or r == '*' else DIM)
            w['prev'].configure(text=fit(prev or T('아직 대화 없음'), F['small'], int(280 * self.app.Z) - 130))
            if unread:
                w['badge'].configure(text=str(unread))
                w['badge'].pack(side='right')
            else:
                w['badge'].pack_forget()

    def _room_row(self, r):
        F, Z = self.app.fonts, self.app.Z
        f = tk.Frame(self.rooms, bg=CARD, padx=12, pady=8, cursor='hand2')
        f.pack(fill='x')
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
        when = tk.Label(top, text='', font=F['small'], bg=CARD, fg=DIM)
        when.pack(side='right')
        name = tk.Label(top, text='', font=F['bodyb'], bg=CARD, fg=FG, anchor='w')
        name.pack(side='left')
        bot = tk.Frame(tx, bg=CARD)
        bot.pack(fill='x')
        prev = tk.Label(bot, text='', font=F['small'], bg=CARD, fg=DIM, anchor='w')
        prev.pack(side='left')
        badge = tk.Label(bot, text='', font=F['tiny'], bg=RED, fg='#ffffff', padx=6)
        bgs = [f, tx, top, bot, when, name, prev] + ([ic] if r == '*' else [])
        for wd in bgs + [ic, badge]:
            wd.bind('<Button-1>', lambda e, r=r: self.set_room(r))
            wd.bind('<MouseWheel>', lambda e: self.rooms_cv.yview_scroll(int(-e.delta / 120), 'units'))
        return {'bgs': bgs, 'when': when, 'name': name, 'prev': prev, 'badge': badge}

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
        if not msgs:
            c.create_text(W / 2, 80 * Z, text=T('아직 주고받은 메시지가 없어요.\n세션끼리 협업 메시지를 보내면 여기에 대화로 쌓입니다.'),
                          justify='center', font=F['body'], fill=CHAT['empty'])
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
            text = re.sub(r'\n\s*\n+', '\n', text).replace('**', '')
            mine = m['from'] == 'user'
            kind = T('명령') if mine else (T('답변') if m.get('reply') else KIND_TEXT.get(m['kind'], ''))
            if mine:
                head = f'→ {names.get(m["to"], m.get("toLabel") or "")}' if self.room == '*' else ''
                if head:
                    t = c.create_text(W - 16 * Z, y, text=head, anchor='ne', font=F['small'], fill=CHAT['sub'])
                    y = c.bbox(t)[3] + 3 * Z
                t = c.create_text(0, 0, text=text, width=maxw, anchor='nw', font=F['body'], fill='#191919')
                x0, y0, x1, y1 = c.bbox(t)
                bw, bh = x1 - x0, y1 - y0
                bx1 = W - 16 * Z
                bx0 = bx1 - bw - 2 * pad
                c.coords(t, bx0 + pad, y + pad * 0.8)
                round_rect(c, bx0, y, bx1, y + bh + 1.6 * pad, 9 * Z, fill=self.ME, outline='')
                c.create_polygon(bx1 - 6 * Z, y + 6 * Z, bx1 + 7 * Z, y + 4 * Z, bx1 - 2 * Z, y + 15 * Z, fill=self.ME, outline='')
                c.tag_raise(t)
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
            t = c.create_text(0, 0, text=text, width=maxw, anchor='nw', font=F['body'], fill=CHAT['text'])
            x0, y0, x1, y1 = c.bbox(t)
            bw, bh = x1 - x0, y1 - y0
            c.coords(t, bx0 + pad, y + pad * 0.8)
            round_rect(c, bx0, y, bx0 + bw + 2 * pad, y + bh + 1.6 * pad, 9 * Z, fill=self.OTHER, outline='')
            if show_head:
                c.create_polygon(bx0 + 6 * Z, y + 6 * Z, bx0 - 7 * Z, y + 4 * Z, bx0 + 2 * Z, y + 15 * Z, fill=self.OTHER, outline='')
            c.tag_raise(t)
            c.create_text(bx0 + bw + 2 * pad + 6 * Z, y + bh + 1.6 * pad, text=self._clock(m['ts']), anchor='sw',
                          font=F['small'], fill=CHAT['time'])
            y += bh + 1.6 * pad + gap * 2
        for sid in typing:  # 내 명령을 받아 일하는 중
            ax, bx0 = 16 * Z, 66 * Z
            round_rect(c, ax, y, ax + 42 * Z, y + 42 * Z, 12 * Z, fill=MINT, outline='')
            c.create_image(ax + 21 * Z, y + 21 * Z, image=self.face(sid))
            t = c.create_text(bx0, y, anchor='nw', font=F['bodyb'], fill=CHAT['name'], text=names.get(sid, ''))
            y2 = c.bbox(t)[3] + 4 * Z
            t = c.create_text(bx0 + pad, y2 + pad * 0.8, anchor='nw', font=F['body'], fill=CHAT['typing'],
                              text=T('작업 중') + '.' * (self.dots or 1))
            x0, y0, x1, y1 = c.bbox(t)
            c.tag_lower(round_rect(c, bx0, y2, x1 + pad, y1 + pad * 0.8, 9 * Z, fill=self.OTHER, outline=''), t)
            y = y1 + pad + gap * 2
        c.configure(scrollregion=(0, 0, W, y + 10 * Z))
        if at_end:
            c.yview_moveto(1.0)

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
        self._wrap(self.cv.winfo_width())
        self.inner.update_idletasks()
        self.cv.configure(scrollregion=self.cv.bbox('all'))
        self.cv.yview_moveto(pos if keep else 0)


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


class MeetingWin:
    """회의실: 세션을 골라 초대하고 최근 N턴의 작업 결과를 보고받는다. 세션이 쓴 보고서·캡처를 카드로 모아 보여 준다."""

    def __init__(self, app):
        self.app = app
        F, Z = app.fonts, app.Z
        self.win = w = app.make_host('meet')
        w.title(T('회의실 · Claude Office'))
        w.configure(bg=BG)
        w.geometry(f'{int(900 * Z)}x{int(780 * Z)}')
        w.minsize(640, 480)
        grad_head(w, app, 'meet', T('회의실'),
                  T('세션을 초대해 최근 작업을 보고받아요. 세션이 결과를 정리하고 화면을 캡처해 보고서를 올려요.')).pack(fill='x')
        holder = tk.Frame(w, bg=BG, padx=14)
        holder.pack(fill='both', expand=True, pady=(10, 12))
        self.cv, self.inner = scroll_frame(holder, BG)
        w.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'))
        self.cv.bind('<Configure>', lambda e: self._wrap(e.width), add='+')
        self.pick = set(app.cfg.get('meet_pick') or [])
        self.turns = app.cfg.get('meet_turns') if app.cfg.get('meet_turns') in MEET_TURNS else 3
        self.faces, self.thumbs, self.wraps = {}, {}, []
        self.sig = None
        self.build()
        self._tick()

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

    def _sig(self):
        m = self.meeting()
        files = ()
        if m:
            d = self.folder(m)
            try:
                files = tuple(sorted((e.name, e.stat().st_mtime) for e in os.scandir(d)))
            except OSError:
                pass
        return (json.dumps(m, sort_keys=True) if m else '', files, tuple(self.live()), tuple(sorted(self.pick)), self.turns)

    def _tick(self):
        if not self.win.winfo_exists():
            return
        sig = self._sig()
        if sig != self.sig:
            self.build()
        self.win.after(1500, self._tick)

    def _wrap(self, width):
        for l in self.wraps:
            l.configure(wraplength=max(240, width - 80))

    def face(self, sid):
        if sid not in self.faces:
            self.faces[sid] = make_frames(sid, 2.2 * self.app.Z, self.app.avatars.get(sid))[0]['idle']
        return self.faces[sid]

    def thumb(self, path, width):
        """캡처 미리보기 (가로 width px 이하로 정수배 줄임). 못 읽으면 None."""
        try:
            key = (path, os.path.getmtime(path), width)
        except OSError:
            return None
        if key not in self.thumbs:
            try:
                img = tk.PhotoImage(file=path)
            except tk.TclError:
                return None
            k = max(1, -(-img.width() // width))
            self.thumbs[key] = img.subsample(k, k) if k > 1 else img
        return self.thumbs[key]

    # ── 화면 ──
    def build(self):
        app, F, Z = self.app, self.app.fonts, self.app.Z
        self.sig = self._sig()
        pos = self.cv.yview()[0]
        for c in self.inner.winfo_children():
            c.destroy()
        self.wraps = []
        m = self.meeting()
        self._setup(m)
        if m:
            self._reports(m)
        for wd in self._all(self.inner):
            wd.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(int(-e.delta / 120), 'units'), add='+')
        self._wrap(self.cv.winfo_width())
        self.inner.update_idletasks()
        self.cv.configure(scrollregion=self.cv.bbox('all'))
        self.cv.yview_moveto(pos)

    def _all(self, w):
        out = [w]
        for c in w.winfo_children():
            out += self._all(c)
        return out

    def _chip(self, parent, text, on, cmd, img=None):
        b = tk.Label(parent, text=text, image=img or '', compound='left', anchor='w' if img else 'center',
                     font=self.app.fonts['bodyb' if on else 'body'],
                     bg=MINT if on else CARD, fg=ACC_TEXT if on else FG, padx=10, pady=4, cursor='hand2',
                     highlightthickness=2 if on else 1, highlightbackground=ACCENT if on else EDGE)
        b.bind('<Button-1>', lambda e: cmd())
        return b

    def _setup(self, m):
        app, F = self.app, self.app.fonts
        card = tk.Frame(self.inner, bg=CARD, padx=16, pady=14, highlightthickness=1, highlightbackground=EDGE)
        card.pack(fill='x', pady=(0, 12))
        top = tk.Frame(card, bg=CARD)
        top.pack(fill='x')
        tk.Label(top, text=T('초대할 세션'), font=F['h2'], bg=CARD, fg=FG).pack(side='left')
        live = self.live()
        self.pick &= {sid for sid, _n, _m in live}
        tk.Label(top, text=T('{0}명 선택').format(len(self.pick)), font=F['small'], bg=CARD, fg=DIM).pack(side='left', padx=10)
        if live:
            allon = len(self.pick) == len(live)
            app._btn(top, T('모두 빼기') if allon else T('모두 초대'),
                     lambda: self._set_pick(set() if allon else {s for s, _n, _m in live})).pack(side='right')
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill='x', pady=(10, 0))
        if not live:
            tk.Label(grid, text=T('켜져 있는 세션이 없어요. 세션 켜기에서 먼저 켜 주세요.'), font=F['body'], bg=CARD,
                     fg=DIM).pack(anchor='w')
        cols = max(1, (self.cv.winfo_width() - 60) // int(210 * app.Z)) if self.cv.winfo_width() > 1 else 3
        for i, (sid, name, mode) in enumerate(live):
            on = sid in self.pick
            label = fit(name, F['bodyb'], int(130 * app.Z)) + ('' if mode != 'busy' else T(' · 작업 중'))
            b = self._chip(grid, label, on, lambda s=sid: self._toggle(s), self.face(sid))
            b.grid(row=i // cols, column=i % cols, sticky='we', padx=(0, 8), pady=(0, 8))
        for c in range(cols):
            grid.columnconfigure(c, weight=1, uniform='m')
        row = tk.Frame(card, bg=CARD)
        row.pack(fill='x', pady=(6, 0))
        tk.Label(row, text=T('보고 범위'), font=F['h2'], bg=CARD, fg=FG).pack(side='left', padx=(0, 12))
        for n in MEET_TURNS:
            self._chip(row, T('최근 1턴') if n == 1 else T('최근 {0}턴').format(n), n == self.turns, lambda n=n: self._set_turns(n)).pack(side='left', padx=(0, 6))
        go = app._btn(row, T('회의 시작'), self.start, bg=ACCENT, fg='#ffffff', hover=ACC_DARK, font='menu')
        go.pack(side='right')
        if m:
            app._btn(row, T('회의 끝내기'), self.end).pack(side='right', padx=(0, 8))
        l = tk.Label(card, text=T('회의를 시작하면 고른 세션마다 최근 N턴의 요청 목록과 보고 지시가 가요. 바쁜 세션은 하던 일을 마친 뒤 보고해요.'),
                     font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left')
        l.pack(fill='x', pady=(10, 0))
        self.wraps.append(l)

    def _reports(self, m):
        app, F, Z = self.app, self.app.fonts, self.app.Z
        top = tk.Frame(self.inner, bg=BG)
        top.pack(fill='x', pady=(4, 8))
        when = datetime.datetime.fromtimestamp(m.get('startedAt', 0) / 1000).strftime(T('%m월 %d일 %H:%M'))
        got = sum(1 for sid in m['members'] if self.report(m, sid))
        tk.Label(top, text=T('보고서'), font=F['h2'], bg=BG, fg=FG).pack(side='left')
        tk.Label(top, text=T('{0} 시작 · 최근 {1}턴 · {2}/{3} 도착').format(when, m.get('turns', '?'), got, len(m['members'])),
                 font=F['small'], bg=BG, fg=DIM).pack(side='left', padx=10)
        app._btn(top, T('자료로 저장'), lambda: self.export(m)).pack(side='right')
        app._btn(top, T('폴더 열기'), lambda: os.startfile(self.folder(m)) if os.path.isdir(self.folder(m)) else None
                 ).pack(side='right', padx=(0, 6))
        with app.mon.lock:
            alive = {sid: (app.mon.display_name(s), s.mode()) for sid, s in app.mon.sessions.items()}
        names = m.get('names') or {}
        for sid in m['members']:
            r = self.report(m, sid)
            name, mode = alive.get(sid, (names.get(sid) or sid[:8], None))
            if r:
                st, col = T('보고 완료'), '#2fb985'
            elif mode is None:
                st, col = T('꺼져 있음'), '#8a96a3'
            elif mode == 'busy':
                st, col = T('보고 준비 중'), '#5b9ee8'
            else:
                st, col = T('요청 보냄'), '#f0a020'
            card = tk.Frame(self.inner, bg=CARD, padx=16, pady=14, highlightthickness=1, highlightbackground=EDGE)
            card.pack(fill='x', pady=(0, 10))
            hd = tk.Frame(card, bg=CARD)
            hd.pack(fill='x')
            tk.Label(hd, image=self.face(sid), bg=MINT, padx=4, pady=4).pack(side='left', padx=(0, 12))
            tx = tk.Frame(hd, bg=CARD)
            tx.pack(side='left', fill='x', expand=True)
            t1 = tk.Frame(tx, bg=CARD)
            t1.pack(fill='x')
            tk.Label(t1, text=clean(name, 40), font=F['h2'], bg=CARD, fg=FG).pack(side='left')
            tk.Label(t1, text=st, font=F['tiny'], bg=col, fg='#ffffff', padx=8, pady=1).pack(side='left', padx=10)
            if r and r.get('title'):
                tk.Label(tx, text=one_line(r['title'], 90), font=F['bodyb'], bg=CARD, fg=ACC_TEXT, anchor='w').pack(fill='x', pady=(2, 0))
            if not r:
                tk.Label(tx, text=T('보고서를 기다리는 중이에요.') if mode else T('세션이 꺼져서 보고를 받을 수 없어요.'),
                         font=F['small'], bg=CARD, fg=DIM, anchor='w').pack(fill='x', pady=(2, 0))
                continue
            if r.get('summary'):
                l = tk.Label(card, text=clean(r['summary'], 1200), font=F['body'], bg=CARD2, fg=FG, anchor='w',
                             justify='left', padx=12, pady=8)
                l.pack(fill='x', pady=(10, 0))
                self.wraps.append(l)
            for it in (r.get('items') if isinstance(r.get('items'), list) else [])[:20]:
                if isinstance(it, dict):
                    self._item(card, m, it)
            if r.get('next'):
                l = tk.Label(card, text=T('다음 할 일 · ') + clean(r['next'], 600), font=F['small'], bg=CARD, fg=TEXT2,
                             anchor='w', justify='left')
                l.pack(fill='x', pady=(10, 0))
                self.wraps.append(l)

    def _item(self, card, m, it):
        F, Z = self.app.fonts, self.app.Z
        f = tk.Frame(card, bg=CARD)
        f.pack(fill='x', pady=(12, 0))
        tk.Frame(f, bg=EDGE, height=1).pack(fill='x', pady=(0, 10))
        top = tk.Frame(f, bg=CARD)
        top.pack(fill='x')
        st, col = REPORT_ST.get(it.get('status'), REPORT_ST['done'])
        tk.Label(top, text=st, font=F['tiny'], bg=col, fg='#ffffff', padx=8, pady=1).pack(side='left')
        l = tk.Label(top, text=one_line(it.get('request') or '', 160), font=F['bodyb'], bg=CARD, fg=FG, anchor='w', justify='left')
        l.pack(side='left', fill='x', expand=True, padx=10)
        self.wraps.append(l)
        if it.get('result'):
            l = tk.Label(f, text=clean(it['result'], 1500), font=F['body'], bg=CARD, fg=TEXT2, anchor='w', justify='left')
            l.pack(fill='x', pady=(6, 0))
            self.wraps.append(l)
        files = [str(x) for x in it.get('files') or [] if x][:8]
        if files:
            l = tk.Label(f, text=T('파일 · ') + ', '.join(files), font=F['small'], bg=CARD, fg=DIM, anchor='w', justify='left')
            l.pack(fill='x', pady=(4, 0))
            self.wraps.append(l)
        shots = tk.Frame(f, bg=CARD)
        shots.pack(fill='x', pady=(6, 0))
        for p in self.images(m, it)[:6]:
            img = self.thumb(p, int(260 * Z))
            if not img:
                continue
            b = tk.Label(shots, image=img, bg=CARD, cursor='hand2', highlightthickness=1, highlightbackground=EDGE)
            b.pack(side='left', padx=(0, 8))
            b.bind('<Button-1>', lambda e, p=p: os.startfile(p))

    def images(self, m, it):
        out = []
        for p in it.get('images') or []:
            p = str(p)
            p = p if os.path.isabs(p) else os.path.join(self.folder(m), p)
            if p.lower().endswith(('.png', '.gif')) and os.path.isfile(p):
                out.append(p)
        return out

    # ── 조작 ──
    def _toggle(self, sid):
        self._set_pick(self.pick ^ {sid})

    def _set_pick(self, pick):
        self.pick = pick
        self.app.cfg['meet_pick'] = sorted(pick)
        self.build()

    def _set_turns(self, n):
        self.turns = self.app.cfg['meet_turns'] = n
        self.build()

    def start(self):
        app = self.app
        if not self.pick:
            app.toast(T('초대할 세션을 먼저 골라 주세요'), warn=True)
            return
        old = self.meeting()
        if old and not tkmsg.askyesno(T('회의실'), T('지금 회의를 끝내고 새 회의를 시작할까요?\n(지난 보고서는 폴더에 남아요)'), parent=self.win):
            return
        mid = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        m = {'id': mid, 'startedAt': int(time.time() * 1000), 'turns': self.turns, 'members': [], 'names': {}}
        os.makedirs(self.folder(m), exist_ok=True)
        head = meet_prompt(1, [], '', '').split(']', 1)[0] + ']'  # 지난 회의 요청은 보고할 턴에서 뺀다
        with app.mon.lock:
            todo = [(sid, app.mon.display_name(app.mon.sessions[sid]),
                     [e[1] for e in app.mon.sessions[sid].qa if not e[1].startswith(head)][-self.turns:])
                    for sid in self.pick if sid in app.mon.sessions]
        sent = 0
        for sid, name, reqs in todo:
            try:
                app.mon.send_user(sid, meet_prompt(self.turns, reqs, self.folder(m), sid))
            except OSError as ex:
                app.toast(T('보내기 실패: {0}').format(ex), warn=True)
                continue
            m['members'].append(sid)
            m['names'][sid] = name
            sent += 1
        write_json(MEET_FILE, m)
        app.save_cfg()
        app.toast(T('{0}개 세션에 업무 보고를 요청했어요').format(sent))
        self.build()

    def end(self):
        try:
            os.remove(MEET_FILE)
        except OSError:
            pass
        self.build()

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
               'border-radius:8px;margin:8px 8px 0 0}</style>',
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
            for it in r.get('items') if isinstance(r.get('items'), list) else []:
                if not isinstance(it, dict):
                    continue
                st, col = REPORT_ST.get(it.get('status'), REPORT_ST['done'])
                out.append(f'<div class="it"><span class="st" style="background:{col}">{e(st)}</span>'
                           f'<b>{e(str(it.get("request") or ""))}</b><p>{e(str(it.get("result") or ""))}</p>')
                if it.get('files'):
                    out.append(f'<div class="files">{e(", ".join(map(str, it["files"])))}</div>')
                for p in self.images(m, it):
                    out.append(f'<a href="file:///{e(p)}"><img src="file:///{e(p)}"></a>')
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
        self.sec_sid, self.sec_on, self.sec_box = None, False, None
        self.sec_hits = []          # 비서 말풍선 안 버튼들 (캔버스 좌표, 할 일)
        self.wb_sig = self.secb_sig = None
        self.brief_at = 0           # 마지막으로 브리핑을 부탁한 시각
        self.layout_key = None
        self.grid_y = WALL_H + 4
        self.tab_sig = None
        self.tab_drop = None
        self.chatwin = self.secwin = self.avwin = self.launchwin = self.meetwin = None
        self.chat_seen = self.cfg.get('chat_seen') or time.time()
        self.pending = {}           # 이 창에서 켠 세션 id → (켠 시각, 이름) — 출근하면 골라 준다
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
        if self.first_run and not self.cfg.get('onboarded'):
            root.after(3000, self._maybe_onboard)  # 첫 폴링으로 비서 세션이 이미 있는지 본 뒤

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
                            secretary=self.sec_pick, sec_done=sorted(self.sec_done)[-300:], chat_seen=self.chat_seen,
                            popup_windows=self.popup.get())
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.cfg, f)
        except Exception:
            pass

    def quit(self):
        self.save_cfg()
        self.mon.stop.set()
        self.mon.save_cache(force=True)
        self.root.destroy()

    def open_storage(self):
        self._open_win('storage', lambda: StorageWin(self))

    def _btn(self, parent, text, cmd, bg=CARD2, fg=FG, hover=HOVER, font='statb'):
        b = tk.Label(parent, text=text, font=self.fonts[font], bg=bg, fg=fg, padx=12, pady=4, cursor='hand2',
                     highlightthickness=1, highlightbackground=EDGE if bg in (CARD, CARD2) else bg)
        b.bind('<Button-1>', lambda e: cmd())
        b.bind('<Enter>', lambda e: b.configure(bg=hover))
        b.bind('<Leave>', lambda e: b.configure(bg=b._bg))
        b._bg = bg
        return b

    def toast(self, text, warn=False):
        """창 아래쪽에 잠깐 뜨는 알림."""
        if self.toast_w:
            self.toast_w.destroy()
        l = tk.Label(self.root, text=clean(text, 120), font=self.fonts['bodyb'], bg='#e8505b' if warn else ROOM['toast'],
                     fg='#ffffff', padx=18, pady=9)
        l.place(relx=0.5, rely=1.0, y=-30, anchor='s')
        self.toast_w = l
        self.root.after(3800, lambda: l.destroy() if l.winfo_exists() else None)

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
            old.destroy()
        self.paned.pack_forget()
        self.page_wrap.pack(fill='both', expand=True)
        self.page = PageHost(self, key)
        self.page_key = key
        self.draw_side(force=True)
        return self.page

    def show_office(self):
        if self.page:
            old, self.page = self.page, None
            old.destroy()
        self._office_back()

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
        c.place(x=max(8, min(x, rw - W - 8)), y=max(8, min(y, rh - H - 8)))
        c.tk.call('raise', c._w)
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
            c.destroy()
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
                ('power', T('세션 켜기'), self.open_launcher, 0), ('folder', T('저장 공간'), self.open_storage, 0)]

    def draw_side(self, force=False):
        items = self.side_items()
        sig = (tuple((k, n) for k, _l, _f, n in items), self.side_hover, self.Z, self.menu_open, self.page_key)
        if sig == self.side_sig and not force:
            return
        self.side_sig = sig
        c, Z, F = self.side, self.Z, self.fonts
        c.delete('all')
        W = int(84 * Z)
        c.configure(width=W)
        self.side_boxes = []
        y = 14 * Z
        for i, (key, label, fn, n) in enumerate(items):
            h = 66 * Z
            on = key == ('menu' if self.menu_open else self.page_key if self.page else 'home')
            if on:  # 고른 칸은 본문 바탕색으로 이어 붙인다
                round_rect(c, 5 * Z, y, W + 30 * Z, y + h, 18 * Z, fill=BG, outline='')
            elif i == self.side_hover:
                round_rect(c, 5 * Z, y, W - 5 * Z, y + h, 16 * Z, fill=SIDE_HOVER, outline='')
            col = (ACCENT if DARK else SIDE) if on else '#ffffff'
            wrap = F['tiny'].measure(label) > W - 12 * Z  # 긴 이름(번역)은 두 줄로
            draw_icon(c, key, W / 2, y + (22 if wrap else 26) * Z, 13 * Z, col, BG if on else SIDE)
            c.create_text(W / 2, y + (50 if wrap else 52) * Z, text=label, font=F['tiny'], fill=ACC_TEXT if on else '#eafff6',
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
                 ('power', T('세션 켜기'), self.open_launcher, '', 0),
                 ('user', T('아바타'), lambda: self.open_avatar(self.sel), '', 0),
                 ('tab', T('새 탭'), lambda: (self.toggle_menu(False), self._tab_edit(None)), '', 0),
                 ('folder', T('저장 공간'), self.open_storage, '', 0),
                 ('zoomin', T('글자 크게'), lambda: self.set_zoom(1), f'{self.Z * 100:.0f}%', 0),
                 ('zoomout', T('글자 작게'), lambda: self.set_zoom(-1), '', 0),
                 ('pin', T('항상 위'), lambda: (self.topmost.set(not self.topmost.get()), self.toggle_top()),
                  T('켬') if self.topmost.get() else T('끔'), 0),
                 ('play', T('움직임'), lambda: self.anim_on.set(not self.anim_on.get()), T('켬') if self.anim_on.get() else T('끔'), 0),
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
            c.create_text(x0 + t / 2, y0 + t * 0.72 - lift, text=label, font=fnt, fill=FG, width=t - 10 * Z, justify='center')
            if state:
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
        if icon in ('chat', 'star', 'meet', 'power', 'user', 'folder', 'spark'):
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
        if open_:
            # 아래 화면을 밀지 않고 상단 막대 바로 아래에 덮어 띄운다
            self.menu_off = -int(self._menu_geom()[3])
            self.gm.place(in_=self.main, x=0, y=self.top.winfo_height() + 1, relwidth=1, height=1)
            self.gm.tk.call('raise', self.gm._w)  # Canvas.lift는 그림 순서용이라 창 순서는 raise로
        self._menu_anim_on = True
        self.draw_side()
        self._menu_step(0)

    def _menu_step(self, k):
        n = 10
        e = 1 - (1 - (k + 1) / n) ** 3  # 처음엔 빠르게, 끝에서 천천히
        p = e if self.menu_open else 1 - e
        H = int(self._menu_geom()[3])
        h = max(1, int(H * p))
        self.gm.place_configure(height=h)
        off = h - H  # 위에서 아래로 미끄러져 내려오게 그림을 같이 움직인다
        if k == 0 and self.menu_open:
            self.menu_off = off
            self.draw_menu()
        else:
            self.gm.move('all', 0, off - self.menu_off)
            self.menu_off = off
        self._place_tr(int((self.tr.winfo_width() + 60) * p))
        if k + 1 < n:
            self.root.after(16, self._menu_step, k + 1)
            return
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
        w = self._open_win('chatwin', lambda: ChatWin(self))
        if sid:
            w.set_room(sid)
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
                out.append({'label': one_line(ch.get('label') or T('요청하기'), 12), 'prompt': ch.get('prompt') or it['prompt'],
                            'target': one_line(ch.get('target') or it.get('target') or '', 40)})
        if not out and it.get('prompt'):
            out.append({'label': T('요청하기'), 'prompt': it['prompt'], 'target': one_line(it.get('target') or '', 40)})
        out = [ch for ch in out if ch['target']]
        with self.mon.lock:
            for ch in out:
                dst = self.mon._resolve(ch['target'])
                ch['sid'] = dst.sid if dst else None
                ch['name'] = self.mon.display_name(dst) if dst else ch['target']
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
        self.sec_mark(it['_key'], True)
        self.toast(T('{0}에 요청했어요 · {1}').format(ch["name"], ch["label"]))

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

    def _maybe_onboard(self):
        if self.sec_sid or self.cfg.get('onboarded'):
            self.cfg['onboarded'] = True
            return
        OnboardWin(self)

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
        self.p_info[T('프로세스')].configure(text=f'PID {pid} · {mem / 1048576:.0f}MB · CPU {cpu:.1f}%')
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
