"""커밋하기 전에 개인 정보·비밀이 섞여 있지 않은지 검사한다 (.githooks/pre-commit 이 부른다).

    python tools/check_private.py          # 이번 커밋에 올라갈(staged) 내용만
    python tools/check_private.py --all    # 저장소에 있는 파일 전부

찾는 것
- 이 PC의 사용자 이름·홈 폴더 경로, git의 실제 이메일(noreply가 아니면)
- 세션 사무실 원격 설정(remote.json)의 비밀: ntfy 토픽, 서명·푸시 열쇠, 기기 열쇠 해시, 암호 해시
- 흔한 비밀 모양: API 키·토큰, 개인 열쇠, Bearer 토큰, 터널 주소, 이메일
- .private-words 파일(저장소에 올라가지 않음)에 한 줄에 하나씩 적어 둔 단어 (예: 실명, 회사, 다른 프로젝트 이름)
- 올리면 안 되는 파일 이름(remote.json, settings.json, *.jsonl …)과 5MB가 넘는 파일

걸리면 어디서 걸렸는지 보여 주고 커밋을 멈춘다. 찾은 값은 일부만 보여 준다.
"""
import json
import os
import re
import subprocess
import sys

ROOT = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip() or os.getcwd()
BAD_NAMES = re.compile(r'(^|/)(remote\.json|settings\.json[^/]*|tokens\.json|secretary\.json|names\.json|peer-[^/]*\.json|'
                       r'out-[^/]*\.json|[^/]*\.jsonl|\.env(\.[^/]*)?|[^/]*\.pem|[^/]*\.key|cloudflared[^/]*\.exe|\.private-words)$', re.I)
MAX_BYTES = 5 * 1024 * 1024
GENERIC = [
    ('API 키', re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}')),
    ('GitHub 토큰', re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}')),
    ('Slack 토큰', re.compile(r'xox[abpr]-[A-Za-z0-9-]{10,}')),
    ('개인 열쇠', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('Bearer 토큰', re.compile(r'Bearer [A-Za-z0-9_\-.]{24,}')),
    ('터널 주소', re.compile(r'https://(?!example)[a-z0-9-]+-[a-z0-9-]+\.trycloudflare\.com')),
    ('이메일', re.compile(r'[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com|anthropic\.com|example\.(?:com|org))[A-Za-z0-9.-]+\.[A-Za-z]{2,}')),
]


def git(*a, binary=False):
    r = subprocess.run(['git', *a], capture_output=True, cwd=ROOT)
    return r.stdout if binary else r.stdout.decode('utf-8', 'replace')


def personal_words():
    """이 PC에서만 알 수 있는 값들 (스크립트 안에는 아무 개인 정보도 적지 않는다)."""
    words = []
    user = os.environ.get('USERNAME') or os.environ.get('USER') or ''
    if len(user) >= 3 and user.lower() not in ('user', 'admin', 'administrator', 'runner'):
        words.append(('사용자 이름', user))
    home = os.path.expanduser('~')
    if home and home not in ('~', '/root'):
        words += [('홈 폴더 경로', home), ('홈 폴더 경로', home.replace('\\', '/')), ('홈 폴더 경로', home.replace('\\', '\\\\'))]
    email = git('config', 'user.email').strip()
    if email and 'noreply' not in email:
        words.append(('git 이메일', email))
    cfg_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'ClaudeOffice')
    try:
        rc = json.load(open(os.path.join(cfg_dir, 'remote.json'), encoding='utf-8'))
        for k in ('topic', 'vapid', 'idkey'):
            if rc.get(k):
                words.append((f'원격 비밀({k})', str(rc[k])))
        for d in rc.get('devices') or []:
            if d.get('hash'):
                words.append(('기기 열쇠 해시', d['hash']))
        if isinstance(rc.get('pw'), dict):
            words += [('암호 해시', rc['pw'].get('hash', '')), ('암호 소금', rc['pw'].get('salt', ''))]
    except (OSError, ValueError):
        pass
    try:
        for line in open(os.path.join(ROOT, '.private-words'), encoding='utf-8'):
            w = line.strip()
            if w and not w.startswith('#') and len(w) >= 3:
                words.append(('.private-words', w))
    except OSError:
        pass
    return [(label, w) for label, w in words if w]


def mask(s):
    s = s.strip()
    return s if len(s) <= 6 else s[:3] + '…' + s[-2:]


def scan(path, data, words):
    hits = []
    if BAD_NAMES.search(path):
        hits.append((path, 0, '올리면 안 되는 파일', os.path.basename(path)))
    if len(data) > MAX_BYTES:
        hits.append((path, 0, '5MB가 넘는 파일', f'{len(data) // 1024}KB'))
    if b'\0' in data[:8000]:  # 그림·글꼴 같은 바이너리: 개인 단어만 바이트로 찾는다
        for label, w in words:
            if w.encode('utf-8') in data:
                hits.append((path, 0, label, mask(w)))
        return hits
    text = data.decode('utf-8', 'replace')
    low = text.lower()
    for label, w in words:
        i = low.find(w.lower())
        if i >= 0:
            hits.append((path, text.count('\n', 0, i) + 1, label, mask(w)))
    for label, rx in GENERIC:
        for m in rx.finditer(text):
            hits.append((path, text.count('\n', 0, m.start()) + 1, label, mask(m.group(0))))
            break
    return hits


def main():
    everything = '--all' in sys.argv
    words = personal_words()
    if everything:
        files = [f for f in git('ls-files', '-z').split('\0') if f]
        read = lambda f: open(os.path.join(ROOT, f), 'rb').read()
    else:
        files = [f for f in git('diff', '--cached', '--name-only', '-z', '--diff-filter=ACMR').split('\0') if f]
        read = lambda f: git('show', ':' + f, binary=True)
    hits = []
    for f in files:
        if f == 'tools/check_private.py':  # 이 검사기 자체의 예시 패턴
            continue
        try:
            hits += scan(f, read(f), words)
        except OSError:
            pass
    author = git('var', 'GIT_AUTHOR_IDENT')
    m = re.search(r'<([^>]*)>', author)
    if not everything and m and m.group(1) and 'noreply' not in m.group(1):
        hits.append(('(커밋 작성자)', 0, '실제 이메일로 커밋됨', mask(m.group(1))))
    if not hits:
        print(f'개인 정보 검사 통과 ({len(files)}개 파일)')
        return 0
    print('개인 정보·비밀로 보이는 것이 있어 멈췄어요:\n')
    for f, line, label, val in hits:
        print(f'  {f}{":" + str(line) if line else ""}  [{label}] {val}')
    print('\n고친 뒤 다시 커밋하세요. 정말 괜찮은 내용이면 git commit --no-verify 로 건너뛸 수 있어요.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
