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
- **Launch session** – start a new session in a folder, or resume a closed one (`claude --resume`) in a new terminal.
- **Game-style menu**, light / **dark theme**, **Korean / English / Japanese / Chinese** UI, zoom 100–200%.
- Light on resources (~60 MB RAM, a few % of one CPU core). Only reads local files; nothing leaves your PC.

| | |
|---|---|
| ![menu](docs/menu_en.png) | ![dark](docs/screenshot_ja_dark.png) |

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

### Optional: session-panel mod (needed for SessionTalk commands and progress)
The office reads progress reports and inter-session messages written by the **session-panel** mod in
[`mod/session-panel`](mod/session-panel). Copy that folder to `%USERPROFILE%\.claude\mods\session-panel`;
new Claude Code sessions pick it up automatically. Without it the office still shows sessions, status,
tokens and logs, but SessionTalk commands and the task board stay empty.

### Secretary skill
On first run the app offers to create a secretary session and installs the `secretary` skill from
[`assets/secretary`](assets/secretary) into `%USERPROFILE%\.claude\skills\secretary` (only if missing).
The secretary writes its suggestions to `%USERPROFILE%\.claude\session-collab\secretary.json`.

### Language, theme, windows
Open **☰ Menu** → *Language* (한국어 / English / 日本語 / 中文), *Dark theme*, *Open as window*
(feature screens as separate windows instead of switching the office view). The first language follows your
Windows display language.

### Data it reads
`~/.claude/sessions/*.json`, `~/.claude/projects/**/*.jsonl` (transcripts, read-only),
`~/.claude/session-collab/*` (mod files). It writes only its own settings / token cache, names you give sessions
(`session-collab/names.json`), and the commands you send (`session-collab/out-office-user.json`).

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
- **세션 켜기** – 새 세션을 시작하거나 꺼진 세션을 원래 폴더에서 이어서 켜요(새 터미널 창).
- **게임식 메뉴**, 밝은/**다크 테마**, **한국어·영어·일본어·중국어**, 글자 크기 100~200%.
- 가벼워요(메모리 약 60MB, CPU 한 코어의 몇 %). 내 PC의 파일만 읽고 밖으로 보내는 것은 없어요.

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

### 선택: session-panel mod (세션톡 명령·작업 분배판에 필요)
진행 보고와 세션 간 메시지는 [`mod/session-panel`](mod/session-panel)의 **session-panel** mod가 씁니다.
이 폴더를 `%USERPROFILE%\.claude\mods\session-panel`로 복사하면 새로 켜는 Claude Code 세션이 자동으로 불러와요.
없어도 세션·상태·토큰·로그는 보이지만, 세션톡 명령과 작업 분배판은 비어 있어요.

### 비서 스킬
처음 실행할 때 비서 세션을 만들면 [`assets/secretary`](assets/secretary)의 `secretary` 스킬을
`%USERPROFILE%\.claude\skills\secretary`에 설치해요(없을 때만). 비서는 제안을
`%USERPROFILE%\.claude\session-collab\secretary.json`에 적어요.

---

## License
Code: [MIT](LICENSE). Font: [Pretendard](https://github.com/orioncactus/pretendard) by Kil Hyung-jin,
SIL Open Font License 1.1 ([fonts/LICENSE.txt](fonts/LICENSE.txt)).
Claude and Claude Code are products of Anthropic; this is an unofficial fan-made tool.
