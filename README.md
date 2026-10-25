# Claude Office · 세션 사무실

**Watch every Claude Code session on your PC as a pixel-art office worker.**
A small Windows desktop app (Python + tkinter, no extra packages) that turns your running
[Claude Code](https://claude.com/claude-code) CLI sessions into characters at desks — typing when they work,
raising a hand when they need you, dozing when idle — with a secretary that suggests what to do next,
a KakaoTalk-style chat between sessions, and one-click session launch / resume.

[English](#english) · [한국어](#한국어)

![Claude Office](docs/screenshot_en.png)

---

## English

### Features
- **Live office** – one desk per running Claude Code session: status (working / needs input / idle / dozing),
  current tool in a speech bubble, progress bar, today's tokens. Paper planes fly when sessions message each other.
- **Side panel** – overview (process, model, context, token usage), work log, and a Q&A view of your
  requests and the last answer of each.
- **Tabs** – group sessions into tabs (drag a desk onto a tab); reorder desks by drag & drop.
- **Avatars** – 6 hair styles, colors, skin, outfits and 7 accessories per session.
- **Secretary** – a dedicated Claude session (created for you on first run) that reviews the other sessions only
  when you press **Get briefing**, then shows suggestions in a speech bubble. Each suggestion has choice
  buttons that send the task straight to the right session.
- **SessionTalk** – a chat view of messages between sessions; type into it to give any session a command.
  Messages show markdown like the Claude Code terminal (bold, italic, code, code blocks, lists, quotes, links).
  Each room also shows the last 20 exchanges typed straight into the terminal, and you can pin important sessions to the top.
  While a session works you see the CLI's line ("✻ Working… (6m 2s · ↓ 26.8k tokens)" and the current tool), and when Claude
  asks you to choose (AskUserQuestion) the options appear as buttons: pick one (or several, or type your own) and the session
  continues as if you had answered in the terminal (needs the session-panel mod 0.3+).
- **Meeting room** – invite sessions and pick the last 1/3/5/10 turns; each session writes a report of what it did
  for those requests (done / partial / not done, files, next steps) and captures its results on screen with
  [`assets/capture.ps1`](assets/capture.ps1). Invited sessions sit around a conference table; when a report arrives
  that session raises a hand and presents it line by line in a typed-out dialogue box, with its captures on the
  projector screen (click a capture to view it large). In *Meeting setup* choose **Brief** (fewer tokens) or **Detailed**:
  sessions also write key points, key figures and chart data, and mark the important part of each capture, so the
  meeting shows a figures board, bar/line/pie charts and zoomed-in captures like a report at work. **Enlarge documents**
  folds the scene so setup and minutes use the whole window. Reports are also kept as cards in the minutes and can be saved as one HTML document. (Sessions may ask permission to write the report file or run the capture script.)
- **Remote control from your phone** – a phone web app (PWA) shows the office, SessionTalk, the secretary and the
  meeting room, sends commands, starts/resumes sessions and notifies you when a session needs approval. Turn it on in
  **Remote**: the PC opens a temporary Cloudflare tunnel outward (no router setup), you connect a phone by scanning a
  QR code (or by opening a copied address and entering a password you set), and the app finds the PC again through a
  secret ntfy.sh channel. Approving tool permissions still happens in the PC terminal. See *Remote control security*.
- **Launch session** – start a new session in a folder, or resume a closed one (`claude --resume`) in a new terminal.
- **Smooth motion** – menus slide, sections fold open, screens glide in, the sidebar highlight follows your choice
  (turn it all off with *Animation* in the menu).
- **Game-style menu**, light / **dark theme**, **Korean / English / Japanese / Chinese** UI, zoom 100–200%.
- Light on resources (~60 MB RAM, a few % of one CPU core). Only reads local files; nothing leaves your PC unless you
  turn on Remote.

| | |
|---|---|
| ![menu](docs/menu_en.png) | ![dark](docs/screenshot_ja_dark.png) |

![meeting room](docs/meeting_en.png)

![characters](docs/characters.png)

### Requirements
- Windows 10/11
- Python 3.10+ with tkinter (the python.org installer or `uv python install 3.11` both include it)
- Claude Code CLI (`claude`) installed and on `PATH`

### Run
```bat
git clone https://github.com/airyanrip/claude-office.git
cd claude-office
pythonw claude_office.pyw
```
or double-click `run.bat`. Settings are stored in `%LOCALAPPDATA%\ClaudeOffice\settings.json`.

### session-panel mod (needed for SessionTalk / phone commands and progress)
The office reads progress reports and inter-session messages written by the **session-panel** mod in
[`mod/session-panel`](mod/session-panel), and commands you send from SessionTalk or your phone are delivered by it.
On first run the app offers to install it (also **☰ Menu → Session link mod**, which also offers updates): it copies
the folder to `%USERPROFILE%\.claude\mods\session-panel` and adds that folder to `env.CLAUDE_CODE_PLUGIN_DIRS` in
`%USERPROFILE%\.claude\settings.json` (other settings are kept; a backup is saved as `settings.json.office-backup`).
To do it by hand, do the same two steps. It applies to Claude Code sessions started afterwards. Without it the office
still shows sessions, status, tokens and logs, but commands are not delivered and the task board stays empty.

### Secretary skill
On first run the app offers to create a secretary session and installs the `secretary` skill from
[`assets/secretary`](assets/secretary) into `%USERPROFILE%\.claude\skills\secretary` (only if missing).
The secretary writes its suggestions to `%USERPROFILE%\.claude\session-collab\secretary.json`.

### Remote control security
- **Nothing is reachable without a key.** Every API call needs a per-device key; the PC keeps only its SHA-256 hash.
  A valid key always works, wrong keys are slowed down per IP.
- **Keys are only handed out two ways:** a QR code holding a *one-time* code (single device, expires in 10 minutes),
  or **Copy address** + the **password** you set in Remote (PBKDF2-hashed; 5 wrong tries per IP in 15 minutes or 20 in
  total per hour lock password login, and you get a notification). A copied address alone grants nothing.
- **The phone checks it is talking to your PC.** The PC has its own signing key; the link carries the public half.
  The phone sends its key or password only to an address that signs a fresh challenge for that exact address, and
  ignores unsigned tunnel addresses on ntfy — so a leaked channel name or a fake server gets nothing. The password
  screen also shows a *PC code* that must match the one in Remote on your PC.
- Push notifications go only to known push services; the server listens on 127.0.0.1 only and the tunnel is outbound.
- Changing the password can disconnect all devices; you can also disconnect devices one by one. Turn Remote off when
  you are not using it.

### Contributing / publishing a fork
Your settings, keys and transcripts live outside the repository (`%LOCALAPPDATA%\ClaudeOffice`, `~/.claude`), and
`.gitignore` blocks those file names. To also check every commit for personal data and secrets, run once:
```bat
git config core.hooksPath .githooks
```
The hook ([`tools/check_private.py`](tools/check_private.py)) stops a commit that contains your Windows user name or
home path, a real e-mail, the Remote keys/channel on this PC, common API tokens, or words you list in a local
`.private-words` file (one per line, never committed). `python tools/check_private.py --all` scans the whole tree.

### Language, theme, windows
Open **☰ Menu** → *Language* (한국어 / English / 日本語 / 中文), *Dark theme*, *Open as window*
(feature screens as separate windows instead of switching the office view). The first language follows your
Windows display language.

### Data it reads
`~/.claude/sessions/*.json`, `~/.claude/projects/**/*.jsonl` (transcripts, read-only),
`~/.claude/session-collab/*` (mod files). It writes only its own settings / token cache, names you give sessions
(`session-collab/names.json`), the commands you send (`session-collab/out-office-user.json`) and meeting state /
saved meeting documents (`session-collab/meeting/`).

---

## 한국어

### 기능
- **사무실 화면** – 켜져 있는 Claude Code 세션마다 책상 하나. 작업 중이면 타자를 치고, 확인이 필요하면 손을 들고,
  쉬면 졸아요. 지금 쓰는 도구가 말풍선으로, 진행도와 오늘 토큰이 책상 아래에 보여요. 세션끼리 메시지를 보내면 종이비행기가 날아가요.
- **오른쪽 패널** – 개요(프로세스·모델·컨텍스트·토큰), 작업 로그, 내가 보낸 요청과 마지막 답변(대화).
- **탭** – 책상을 탭 위로 끌어 세션을 묶고, 끌어서 자리를 바꿔요.
- **아바타 꾸미기** – 머리 6종, 색, 피부, 옷, 소품 7종.
- **비서** – 다른 세션들을 살펴보고 다음 할 일을 제안하는 전용 세션. **[브리핑 받기]**를 누를 때만 일하고,
  제안의 선택지 버튼을 누르면 그 일이 해당 세션에 바로 요청돼요. 처음 실행할 때 만들지 물어봐요.
- **세션톡** – 세션끼리 주고받은 메시지를 카톡처럼 보고, 아래 입력칸으로 세션에 직접 명령을 보내요.
  메시지는 Claude Code 터미널처럼 굵게·기울임·코드·코드 블록·목록·인용·링크로 보여요.
  터미널에서 직접 나눈 대화도 방마다 최근 20개까지 보이고, 중요한 세션은 목록 위에 고정할 수 있어요.
  세션이 일하는 동안은 CLI처럼 "✻ 작업 중… (6m 2s · ↓ 26.8k tokens)"와 지금 도구가 보이고, Claude가 고르라고 물으면
  (AskUserQuestion) 선택지가 버튼으로 떠요. 고르면(여럿 고르기·직접 입력도) 터미널에서 답한 것처럼 이어서 진행해요
  (session-panel mod 0.3 이상).
- **회의실** – 세션을 초대하고 최근 1·3·5·10턴을 고르면, 세션마다 그동안 맡은 일의 결과(완료·일부·못 함, 파일, 남은 일)를
  보고서로 정리하고 [`assets/capture.ps1`](assets/capture.ps1)로 결과 화면을 캡처해 올려요. 초대한 세션은 회의 탁자에 둘러앉고,
  보고서가 오면 손을 든 뒤 차례로 발표해요. 대사는 타이핑되듯 나오고 캡처는 발표 스크린에 떠요(누르면 크게 보기). 회의 준비에서 **간단**(토큰 절약)과 **상세**를 고를 수 있어요.
  상세로 하면 세션이 핵심 줄·핵심 수치·그래프 데이터를 더 적고 캡처의 중요한 부분을 표시해, 회의에서 수치판·막대/꺾은선/원 그래프·
  확대한 캡처로 회사 보고처럼 보여 줘요. **문서 크게 보기**로 장면을 접고 회의 준비·회의록을 창 전체로 볼 수 있어요. 보고서는 회의록 카드로도
  남고, HTML 자료 한 장으로 저장할 수 있어요. (보고서 파일 쓰기·캡처 실행에 세션이 권한을 물을 수 있어요.)
- **휴대폰 원격 조종** – 휴대폰 웹앱(PWA)으로 사무실·세션톡·비서·회의실을 보고 명령을 보내고, 세션을 켜고, 세션이 확인을
  기다리면 알림을 받아요. **원격** 화면에서 켜면 PC가 Cloudflare 임시 터널을 바깥으로 열고(공유기 설정 없음), 휴대폰은 QR을
  찍거나 **복사한 주소**를 열고 내가 정한 **암호**를 넣어 연결해요. 이후엔 ntfy.sh의 비밀 채널로 PC를 다시 찾아요.
  도구 권한 승인은 여전히 PC 터미널에서 해요. 아래 *원격 조종 보안*을 참고하세요.
- **세션 켜기** – 새 세션을 시작하거나 꺼진 세션을 원래 폴더에서 이어서 켜요(새 터미널 창).
- **부드러운 움직임** – 메뉴가 미끄러져 열리고, 칸이 접히고 펼쳐지고, 화면이 밀려 들어오고, 왼쪽 막대 표시가 따라 움직여요
  (메뉴 > 움직임으로 끌 수 있어요).
- **게임식 메뉴**, 밝은/**다크 테마**, **한국어·영어·일본어·중국어**, 글자 크기 100~200%.
- 가벼워요(메모리 약 60MB, CPU 한 코어의 몇 %). 내 PC의 파일만 읽고, 원격을 켜지 않으면 밖으로 보내는 것은 없어요.

![세션 사무실](docs/screenshot_ko.png)

### 준비물
- Windows 10/11
- tkinter가 들어 있는 Python 3.10 이상 (python.org 설치본, `uv python install 3.11` 모두 OK)
- Claude Code CLI(`claude`)가 설치되어 `PATH`에 있어야 해요

### 실행
```bat
git clone https://github.com/airyanrip/claude-office.git
cd claude-office
pythonw claude_office.pyw
```
또는 `run.bat`을 더블클릭. 설정은 `%LOCALAPPDATA%\ClaudeOffice\settings.json`에 저장돼요.

### session-panel mod (세션톡·휴대폰 명령, 작업 분배판에 필요)
진행 보고와 세션 간 메시지는 [`mod/session-panel`](mod/session-panel)의 **session-panel** mod가 쓰고, 세션톡·휴대폰에서 보낸
명령도 이 mod가 세션에 전해요. 처음 실행할 때 설치할지 물어봐요(**☰ 메뉴 → 세션 연결 mod**에서도 설치·업데이트).
설치하면 폴더를 `%USERPROFILE%\.claude\mods\session-panel`에 복사하고, `%USERPROFILE%\.claude\settings.json`의
`env.CLAUDE_CODE_PLUGIN_DIRS`에 그 폴더를 더해요(다른 설정은 그대로, 원래 파일은 `settings.json.office-backup`으로 백업).
직접 하려면 이 두 가지를 똑같이 하면 돼요. 그 뒤에 켜는 Claude Code 세션부터 적용돼요. 없어도 세션·상태·토큰·로그는 보이지만,
명령은 전달되지 않고 작업 분배판은 비어 있어요.

### 비서 스킬
처음 실행할 때 비서 세션을 만들면 [`assets/secretary`](assets/secretary)의 `secretary` 스킬을
`%USERPROFILE%\.claude\skills\secretary`에 설치해요(없을 때만). 비서는 제안을
`%USERPROFILE%\.claude\session-collab\secretary.json`에 적어요.

### 원격 조종 보안
- **열쇠 없이는 아무것도 못 해요.** 모든 요청에 기기별 열쇠가 필요하고, PC에는 열쇠의 해시만 남아요.
- **열쇠는 두 가지로만 받아요:** QR의 *한 번용* 코드(한 기기, 10분), 또는 **[주소 복사]**로 건넨 주소 + 원격 화면에서 정한
  **암호**(PBKDF2 해시로 저장. 한 IP에서 15분에 5번, 모두 합쳐 1시간에 20번 틀리면 암호 연결을 막고 알려 줘요).
  주소만으로는 연결되지 않아요.
- **휴대폰은 진짜 내 PC인지 확인해요.** PC는 자기 서명 열쇠를 갖고, 링크에는 공개 열쇠가 들어 있어요. 휴대폰은 그 주소에
  대해 새로 서명할 수 있는 곳에만 열쇠·암호를 보내고, ntfy에 서명 없이 올라온 주소는 무시해요. 채널 이름이 새거나 가짜
  서버가 있어도 아무것도 넘어가지 않아요. 암호 화면의 *PC 확인 코드*가 PC 원격 화면의 코드와 같은지도 볼 수 있어요.
- 푸시 알림은 알려진 푸시 서비스로만 보내고, 서버는 127.0.0.1에서만 열며 터널은 바깥으로만 열어요.
- 암호를 바꿀 때 기기를 모두 끊을 수 있고, 기기별로도 끊을 수 있어요. 쓰지 않을 땐 원격을 꺼 두세요.

### 기여하거나 내 저장소로 공개할 때
설정·열쇠·대화 기록은 저장소 밖(`%LOCALAPPDATA%\ClaudeOffice`, `~/.claude`)에 있고, `.gitignore`가 그 파일 이름들을 막아요.
커밋마다 개인 정보·비밀을 검사하려면 한 번만:
```bat
git config core.hooksPath .githooks
```
[`tools/check_private.py`](tools/check_private.py)가 Windows 사용자 이름·홈 폴더 경로, 실제 이메일, 이 PC의 원격 열쇠·채널,
흔한 API 토큰, 그리고 로컬 `.private-words` 파일(한 줄에 하나, 올라가지 않음)에 적은 단어가 들어간 커밋을 멈춰요.
`python tools/check_private.py --all`로 전체를 검사할 수 있어요.

---

## License
Code: [MIT](LICENSE). Font: [Pretendard](https://github.com/orioncactus/pretendard) by Kil Hyung-jin,
SIL Open Font License 1.1 ([fonts/LICENSE.txt](fonts/LICENSE.txt)).
Claude and Claude Code are products of Anthropic; this is an unofficial fan-made tool.
