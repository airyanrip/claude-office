/* Claude Office 휴대폰 웹앱: PC의 세션 사무실(원격 조종)에 붙어 세션을 보고 명령을 보낸다.
 * 연결 정보(열쇠·채널·주소)는 QR 링크의 # 뒤로만 받아 이 기기에 저장하고 주소 표시줄에서는 바로 지운다.
 * PC에서 온 글은 모두 textContent로만 넣는다 (HTML로 해석하지 않음). */
'use strict';

// ───────────── 문구 ─────────────
const STR = {
  office: ['사무실', 'Office', 'オフィス', '办公室'],
  talk: ['세션톡', 'SessionTalk', 'セッショントーク', '会话聊天'],
  sec: ['비서', 'Secretary', '秘書', '秘书'],
  meet: ['회의실', 'Meeting', '会議室', '会议室'],
  more: ['더보기', 'More', 'その他', '更多'],
  busy: ['작업 중', 'Working', '作業中', '工作中'], wait: ['확인 필요', 'Needs input', '確認待ち', '待确认'],
  idle: ['휴식', 'Idle', '休憩', '休息'], sleep: ['조는 중', 'Dozing', '居眠り', '打瞌睡'],
  connecting: ['PC 찾는 중…', 'Finding your PC…', 'PCを探しています…', '正在寻找电脑…'],
  connected: ['연결됨', 'Connected', '接続中', '已连接'],
  pcOff: ['PC의 원격이 꺼져 있어요', 'Remote is off on your PC', 'PCのリモートがオフです', '电脑上的远程已关闭'],
  pcOffSub: ['PC의 세션 사무실 > 원격 조종에서 [원격 켜기]를 눌러 주세요. 켜지면 자동으로 다시 연결해요.',
    'Press [Turn on remote] in Session Office > Remote on your PC. This reconnects automatically.',
    'PCのセッションオフィス > リモート操作で[リモートをオン]を押してください。オンになると自動で再接続します。',
    '请在电脑的会话办公室 > 远程控制中点击[开启远程]。开启后会自动重新连接。'],
  notPaired: ['아직 PC와 연결되지 않았어요', 'Not paired with a PC yet', 'まだPCと接続されていません', '尚未与电脑配对'],
  notPairedSub: ['PC의 세션 사무실 > 원격 조종 > [휴대폰 연결하기]의 QR을 휴대폰 카메라로 찍어 주세요.',
    'Scan the QR in Session Office > Remote > [Connect a phone] on your PC with your phone camera.',
    'PCのセッションオフィス > リモート操作 > [スマホを接続]のQRをスマホのカメラで読み取ってください。',
    '请用手机相机扫描电脑上 会话办公室 > 远程控制 > [连接手机] 中的二维码。'],
  revoked: ['PC에서 이 기기의 연결을 끊었어요', 'This device was disconnected on the PC', 'PCでこの端末の接続が解除されました', '电脑已断开此设备'],
  noSessions: ['켜져 있는 세션이 없어요', 'No sessions are running', '起動中のセッションがありません', '没有正在运行的会话'],
  waitNote: ['PC 터미널에서 확인(승인)을 기다리고 있어요', 'Waiting for approval in the PC terminal', 'PCのターミナルで承認を待っています', '正在电脑终端等待确认'],
  send: ['보내기', 'Send', '送信', '发送'], sent: ['보냈어요', 'Sent', '送信しました', '已发送'],
  cmdPh: ['이 세션에 보낼 명령', 'Command for this session', 'このセッションへの指示', '发给此会话的指令'],
  qa: ['대화', 'Q&A', '会話', '对话'], log: ['작업 로그', 'Log', '作業ログ', '工作日志'], tasks: ['작업 분배', 'Tasks', 'タスク', '任务'],
  noQa: ['아직 기록이 없어요', 'Nothing yet', 'まだ記録がありません', '还没有记录'],
  all: ['전체', 'All', 'すべて', '全部'], me: ['나', 'Me', '自分', '我'], read: ['읽음', 'Read', '既読', '已读'],
  joinTitle: ['이 PC에 연결하기', 'Connect to this PC', 'このPCに接続', '连接到此电脑'],
  joinSub: ['PC의 원격 조종 화면에서 정한 암호를 입력해 주세요.', 'Enter the password set in Remote on the PC.', 'PCのリモート操作で決めたパスワードを入力してください。', '请输入在电脑远程控制中设置的密码。'],
  pwPh: ['암호', 'Password', 'パスワード', '密码'], pwBtn: ['연결', 'Connect', '接続', '连接'],
  pwBad: ['암호가 맞지 않아요', 'Wrong password', 'パスワードが違います', '密码不正确'],
  pwLocked: ['너무 많이 틀렸어요. 잠시 뒤에 다시 해 주세요', 'Too many attempts. Try again later', '試行回数が多すぎます。しばらくしてから再度お試しください', '尝试次数过多，请稍后再试'],
  noPwSet: ['PC에 암호가 없어서 주소로는 연결할 수 없어요. PC의 원격 조종 화면에서 암호를 정하거나 QR로 연결해 주세요.', 'The PC has no password, so this address can\'t connect. Set a password in Remote on the PC, or connect with the QR code.', 'PCにパスワードがないため、このアドレスでは接続できません。PCのリモート操作でパスワードを設定するか、QRで接続してください。', '电脑未设置密码，无法通过此地址连接。请在电脑的远程控制中设置密码，或用二维码连接。'],
  linkUsed: ['이 연결 링크는 이미 쓰였거나 10분이 지나 사라졌어요.', 'This link was already used or has expired (10 minutes).', 'この接続リンクは使用済みか、10分が過ぎて無効になりました。', '此连接链接已被使用或已过期（10分钟）。'],
  pcCode: ['PC 확인 코드 {0}', 'PC code {0}', 'PC確認コード {0}', '电脑确认码 {0}'],
  pcCodeSub: ['PC 원격 조종 화면의 코드와 같은지 확인하세요', 'Check that it matches the code in Remote on your PC', 'PCのリモート操作画面のコードと同じか確認してください', '请确认与电脑远程控制中显示的一致'],
  netErr: ['PC에 닿지 못했어요', 'Could not reach the PC', 'PCに接続できませんでした', '无法连接到电脑'],
  zoomHint: ['두 번 탭하거나 두 손가락으로 확대 · 아래로 끌면 닫혀요', 'Double-tap or pinch to zoom · drag down to close', 'ダブルタップかピンチで拡大 · 下にドラッグで閉じる', '双击或双指缩放 · 向下拖动关闭'],
  pinMeet: ['회의실 화면 고정', 'Pin the meeting scene', '会議室の画面を固定', '固定会议室画面'],
  pinnedMeet: ['회의실 화면을 위에 고정했어요', 'Meeting scene pinned to the top', '会議室の画面を上に固定しました', '已将会议室画面固定在顶部'],
  unpinnedMeet: ['고정을 풀었어요', 'Unpinned', '固定を解除しました', '已取消固定'],
  members: ['대화상대', 'Members', 'メンバー', '成员'], searchName: ['이름 검색', 'Search names', '名前で検索', '搜索名称'],
  allRoom: ['전체 채팅방', 'All chats', '全体チャット', '全部聊天'], allRoomSub: ['모든 세션의 대화를 한곳에서', 'Every session in one place', 'すべてのセッションの会話をまとめて', '所有会话的对话'],
  sessionsN: ['세션 {0}', 'Sessions {0}', 'セッション {0}', '会话 {0}'], noMatch: ['찾는 세션이 없어요', 'No matching session', '該当するセッションがありません', '没有匹配的会话'],
  talkPh: ['메시지 보내기', 'Message', 'メッセージ', '发送消息'], pickRoom: ['위에서 세션을 고르면 명령을 보낼 수 있어요', 'Pick a session above to send a command', '上でセッションを選ぶと指示を送れます', '在上方选择会话即可发送指令'],
  brief: ['브리핑 받기', 'Get briefing', 'ブリーフィング', '获取简报'], briefing: ['브리핑 중…', 'Briefing…', 'ブリーフィング中…', '简报中…'],
  noSec: ['비서 세션이 없어요 · PC에서 만들어 주세요', 'No secretary session · create one on the PC', '秘書セッションがありません · PCで作成してください', '没有秘书会话 · 请在电脑上创建'],
  noSugg: ['보여 줄 제안이 없어요', 'No suggestions', '提案はありません', '没有建议'],
  kinds: { next: ['다음 업무', 'Next task', '次の作業', '下一项'], check: ['확인 권유', 'Check this', '確認推奨', '建议确认'],
    warn: ['주의', 'Warning', '注意', '注意'], info: ['참고', 'Info', '参考', '参考'] },
  markDone: ['확인함', 'Done', '確認済み', '已确认'], undo: ['다시 보이기', 'Show again', '再表示', '重新显示'], link: ['링크 열기', 'Open link', 'リンクを開く', '打开链接'],
  showDone: ['확인한 것도 보기', 'Show done', '確認済みも表示', '显示已确认'],
  invite: ['초대할 세션', 'Invite sessions', '招待するセッション', '邀请会话'], range: ['보고 범위', 'Report range', '報告範囲', '汇报范围'],
  turnN: ['최근 {0}턴', 'Last {0} turns', '直近{0}ターン', '最近 {0} 轮'], turn1: ['최근 1턴', 'Last turn', '直近1ターン', '最近 1 轮'],
  start: ['회의 시작', 'Start meeting', '会議を開始', '开始会议'], end: ['회의 끝내기', 'End meeting', '会議を終了', '结束会议'],
  moderator: ['회의 진행', 'Moderator', '司会', '主持人'],
  mWelcome: ['회의에 부를 세션을 골라 [회의 시작]을 눌러 주세요.', 'Pick the sessions to call in and press [Start meeting].', '会議に呼ぶセッションを選んで[会議を開始]を押してください。', '选择要参会的会话并点击[开始会议]。'],
  mWaiting: ['보고를 기다리는 중이에요… ({0}/{1})', 'Waiting for reports… ({0}/{1})', '報告を待っています… ({0}/{1})', '正在等待汇报… ({0}/{1})'],
  mDone: ['모든 보고가 끝났어요.', 'All reports are done.', 'すべての報告が終わりました。', '所有汇报都结束了。'],
  next: ['다음 할 일 · ', 'Next · ', '次にやること · ', '下一步 · '],
  stDone: ['완료', 'Done', '完了', '完成'], stPartial: ['일부', 'Partial', '一部', '部分'], stTodo: ['못 함', 'Not done', '未対応', '未完成'],
  endQ: ['회의를 끝낼까요?', 'End the meeting?', '会議を終了しますか？', '结束会议吗？'],
  launch: ['세션 켜기', 'Launch session', 'セッション起動', '启动会话'], resume: ['켜기', 'Start', '起動', '启动'],
  newSess: ['새 세션', 'New session', '新しいセッション', '新会话'], folder: ['작업 폴더', 'Work folder', '作業フォルダ', '工作文件夹'],
  sessName: ['이름 (선택)', 'Name (optional)', '名前（任意）', '名称（可选）'], loading: ['불러오는 중…', 'Loading…', '読み込み中…', '加载中…'],
  starting: ['PC에서 켜는 중이에요', 'Starting on the PC', 'PCで起動中です', '正在电脑上启动'],
  notif: ['알림', 'Notifications', '通知', '通知'],
  notifSub: ['세션이 확인을 기다리거나 회의 보고서가 오면 알려 줘요.', 'Get notified when a session needs input or a report arrives.', 'セッションが確認を待つときや報告書が届いたときに通知します。', '会话等待确认或收到汇报时通知你。'],
  notifOn: ['알림 켜기', 'Turn on', '通知をオン', '开启通知'], notifOff: ['알림 끄기', 'Turn off', '通知をオフ', '关闭通知'], notifTest: ['시험 알림', 'Test', 'テスト通知', '测试通知'],
  notifIos: ['아이폰은 Safari 공유 > "홈 화면에 추가"로 설치한 뒤 거기서 열어야 알림을 받을 수 있어요.', 'On iPhone, add this app to the Home Screen (Safari Share > Add to Home Screen) and open it from there to get notifications.', 'iPhoneではSafariの共有 >「ホーム画面に追加」で追加し、そこから開くと通知を受け取れます。', '在 iPhone 上，请通过 Safari 分享 >“添加到主屏幕”安装，并从主屏幕打开才能接收通知。'],
  notifDenied: ['이 브라우저에서 알림이 막혀 있어요', 'Notifications are blocked in this browser', 'このブラウザで通知がブロックされています', '此浏览器已阻止通知'],
  conn: ['연결', 'Connection', '接続', '连接'], pcAddr: ['PC 주소', 'PC address', 'PCのアドレス', '电脑地址'],
  forget: ['이 기기 연결 해제', 'Forget this PC', 'この端末の接続を解除', '解除此设备的连接'],
  forgetQ: ['이 휴대폰에서 연결 정보를 지울까요? 다시 쓰려면 QR로 새로 연결해야 해요.', 'Remove the connection from this phone? You will need to scan a new QR.', 'このスマホから接続情報を削除しますか？再び使うには新しいQRで接続します。', '要从此手机删除连接信息吗？之后需要重新扫码连接。'],
  install: ['홈 화면에 추가하면 앱처럼 쓸 수 있어요.', 'Add to your Home Screen to use it like an app.', 'ホーム画面に追加するとアプリのように使えます。', '添加到主屏幕即可像应用一样使用。'],
  err: ['오류: {0}', 'Error: {0}', 'エラー: {0}', '错误：{0}'], offSession: ['세션이 꺼져 있어요', 'The session is off', 'セッションがオフです', '会话已关闭'],
  ago: ['{0}분 전', '{0}m ago', '{0}分前', '{0} 分钟前'], now: ['방금', 'now', 'たった今', '刚刚'],
  today: ['오늘 {0} 토큰', '{0} tokens today', '今日 {0} トークン', '今日 {0} token'],
  presentAgain: ['다시 듣기', 'Replay', 'もう一度', '重听'],
  typeSpeed: ['회의실 대사 속도', 'Meeting dialogue speed', '会議室のセリフ速度', '会议室台词速度'],
  sp_slow: ['느리게', 'Slow', 'ゆっくり', '慢'], sp_normal: ['보통', 'Normal', '普通', '普通'], sp_fast: ['빠르게', 'Fast', '速く', '快'], sp_instant: ['바로', 'Instant', '即時', '立即'],
};
let LANG = (localStorage.getItem('co.lang') || navigator.language || 'ko').slice(0, 2);
const LI = () => ({ ko: 0, en: 1, ja: 2, zh: 3 })[LANG] ?? 1;
function t(key, ...a) {
  let v = key.split('.').reduce((o, k) => o && o[k], STR);
  let s = Array.isArray(v) ? v[LI()] : key;
  a.forEach((x, i) => { s = s.replace('{' + i + '}', x); });
  return s;
}

// ───────────── 작은 도구 ─────────────
const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.style.cssText = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
function toast(text, warn) {
  const el = $('#toast');
  el.textContent = text;
  el.className = warn ? 'warn' : '';
  el.hidden = false;
  el.style.animation = 'none'; void el.offsetWidth; el.style.animation = '';
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, 2600);
}
function ago(ts) {
  const m = Math.floor((Date.now() / 1000 - ts) / 60);
  return m < 1 ? t('now') : m < 120 ? t('ago', m) : new Date(ts * 1000).toLocaleString(LANG, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtN(v) { return v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(v || 0); }
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ───────────── 연결 정보 ─────────────
let conf = JSON.parse(localStorage.getItem('co.conf') || 'null') || {};
// PC 주소로 받아들이는 것: Cloudflare 임시 터널, 또는 이 PC 안(미리보기)
const okU = u => /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(u) || /^http:\/\/(127\.0\.0\.1|localhost):\d{2,5}$/.test(u);
// 링크: #t=채널&u=주소&s=PC 공개 열쇠 + (QR) p=한 번용 코드 / (미리보기) k=열쇠 / (복사한 주소) 없음 → 암호
(function takePairing() {
  if (!location.hash) return;
  const p = new URLSearchParams(location.hash.slice(1));
  const tp = p.get('t') || '', u = p.get('u') || '', s = p.get('s') || '', k = p.get('k'), code = p.get('p');
  if (/^co-[\w-]{8,64}$/.test(tp) && (!u || okU(u)) && (!s || /^[\w-]{80,100}$/.test(s))) {
    const same = conf.k && conf.t === tp && (!s || !conf.s || conf.s === s);  // 이미 이 PC와 연결된 기기는 열쇠를 그대로 쓴다
    if (k) conf = { k, t: tp, u, s };
    else if (same) { if (u && u !== conf.u) conf.alt = u; if (!conf.s && s) conf.s = s; }  // 새 주소는 서명을 확인한 뒤에야 쓴다
    else conf = { t: tp, u, s, p: code || '' };
    saveConf();
  }
  history.replaceState(null, '', location.pathname + location.search);  // 열쇠·코드를 주소 표시줄·기록에 남기지 않는다
})();
function saveConf() {
  localStorage.setItem('co.conf', JSON.stringify(conf));
  idbPut('conf', conf).catch(() => {});  // 서비스 워커(알림)가 읽는다
}
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open('claude-office', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function idbPut(k, v) {
  const db = await idb();
  return new Promise((res, rej) => { const tx = db.transaction('kv', 'readwrite'); tx.objectStore('kv').put(v, k); tx.oncomplete = res; tx.onerror = () => rej(tx.error); });
}

// ───────────── PC와 통신 ─────────────
let online = false, revoked = false, pcOff = false;
const etags = {};
// 보내기(POST)마다 요청 번호를 붙인다. 실패해서 같은 내용을 다시 보내면 같은 번호를 써서, PC가 이미 처리했으면
// 두 번 실행하지 않는다 (세션에 같은 명령이 두 번 가서 토큰을 더 쓰는 일 막기).
const reqIds = new Map();
async function api(path, body, { timeout = body ? 25000 : 12000, raw = false, etag = false } = {}) {
  if (!conf.u) throw new Error('no address');
  const ctl = new AbortController();
  const tm = setTimeout(() => ctl.abort(), timeout);
  const rkey = body ? path + JSON.stringify(body) : '';
  if (body) {
    const old = reqIds.get(rkey);
    if (!old || Date.now() - old[1] > 9 * 60000) reqIds.set(rkey, [crypto.getRandomValues(new Uint32Array(4)).join('-'), Date.now()]);
  }
  try {
    const r = await fetch(conf.u + path, {
      method: body ? 'POST' : 'GET', signal: ctl.signal, cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: Object.assign({ Authorization: 'Bearer ' + conf.k }, body ? { 'Content-Type': 'application/json', 'X-Req-Id': reqIds.get(rkey)[0] } : {},
        etag && etags[path] ? { 'X-If-Same': etags[path][0] } : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
    if (body && r.ok) reqIds.delete(rkey);
    if (r.status === 401) { revoked = true; throw new Error('unauthorized'); }
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).error || msg; } catch (e) { /* 무시 */ }
      throw new Error(String(msg));
    }
    if (raw) return r.blob();
    const data = await r.json();
    if (etag && data.same && etags[path]) return etags[path][1];  // 바뀐 게 없음 (휴대폰 데이터 절약)
    if (etag && r.headers.get('X-Etag')) etags[path] = [r.headers.get('X-Etag'), data];
    return data;
  } finally { clearTimeout(tm); }
}
// ── PC 서명 확인: 링크로 받은 PC 공개 열쇠(conf.s)로 확인한 주소에만 열쇠·암호를 보낸다 ──
const b64uDec = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), c => c.charCodeAt(0));
const b64u = a => btoa(String.fromCharCode(...a)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const pcKeys = {};
async function verifySig(text, sig) {
  try {
    if (!pcKeys[conf.s]) pcKeys[conf.s] = await crypto.subtle.importKey('raw', b64uDec(conf.s), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
    return await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, pcKeys[conf.s], b64uDec(sig), new TextEncoder().encode(text));
  } catch (e) { return false; }
}
async function pcCode() {  // PC 화면의 'PC 확인 코드'와 같은 값
  try {
    const d = new Uint8Array(await crypto.subtle.digest('SHA-256', b64uDec(conf.s)));
    const x = [...d.slice(0, 4)].map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
    return x.slice(0, 4) + '-' + x.slice(4);
  } catch (e) { return ''; }
}
let pcInfo = {};
async function ping(u) {
  try {
    const ctl = new AbortController();
    setTimeout(() => ctl.abort(), 6000);
    const n = b64u(crypto.getRandomValues(new Uint8Array(18)));
    const r = await fetch(u + '/api/ping?n=' + n, { signal: ctl.signal, cache: 'no-store' });
    const d = await r.json();
    if (!r.ok || d.app !== 'claude-office') return false;
    if (conf.s && !(d.sig && await verifySig('co-ping|' + n + '|' + u, d.sig))) return false;  // 가짜 서버
    pcInfo = d;
    return true;
  } catch (e) { return false; }
}
async function findPC() {
  // 저장된 주소 → 안 되면 ntfy의 비밀 채널에서 PC가 마지막으로 알린 주소
  for (const u of [conf.u, conf.alt]) {
    if (u && okU(u) && await ping(u)) { conf.u = u; delete conf.alt; saveConf(); return true; }
  }
  try {
    const r = await fetch('https://ntfy.sh/' + encodeURIComponent(conf.t) + '/json?poll=1&since=12h', { cache: 'no-store' });
    const lines = (await r.text()).trim().split('\n').filter(Boolean);
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = JSON.parse(lines[i]);
      if (m.event !== 'message') continue;
      const d = JSON.parse(m.message || '{}');
      if (conf.s && !(d.sig && await verifySig('co-url|' + (d.u || '') + '|' + d.ts, d.sig))) continue;  // PC가 올린 게 아니면 무시
      if (!d.u) { pcOff = true; return false; }
      if (/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(d.u) && await ping(d.u)) {
        conf.u = d.u; saveConf(); pcOff = false; return true;
      }
      break;
    }
  } catch (e) { /* 아래에서 다시 */ }
  pcOff = true;
  return false;
}

let state = null, stateSig = '', join = '';
// 열쇠 없이 부르는 것 (한 번용 코드·암호를 열쇠로 바꾸기)
async function openPost(path, body) {
  try {
    const r = await fetch(conf.u + path, { method: 'POST', cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let data = {};
    try { data = await r.json(); } catch (e) { /* 무시 */ }
    return { ok: r.ok, status: r.status, data };
  } catch (e) { return { ok: false, status: 0, data: {} }; }
}
async function connect() {
  setConn('connecting');
  if (!conf.k && !conf.t) { render(); return; }
  for (let wait = 2000; ; wait = Math.min(wait * 1.6, 20000)) {
    if (await findPC()) break;
    setConn('off');
    render();
    await sleep(wait);
  }
  if (!conf.k) {
    if (conf.p) {  // QR: 한 번용 코드를 이 기기의 열쇠로 바꾼다
      const r = await openPost('/api/pair', { code: conf.p, name: deviceName() });
      delete conf.p;
      if (r.ok && r.data.k) conf.k = r.data.k; else join = 'expired';
      saveConf();
    }
    if (!conf.k) {  // 복사한 주소: 암호를 묻는다
      if (join !== 'expired') join = pcInfo.pw ? 'pw' : 'nopw';
      setConn('off');
      render();
      return;
    }
  }
  join = '';
  try {
    const hi = await api('/api/hello', { name: deviceName() });
    if (!localStorage.getItem('co.lang') && hi.lang) LANG = hi.lang;
    conf.vapid = hi.vapid;
    if (!conf.s && hi.id) conf.s = hi.id;  // 예전에 연결한 기기: 이제부터 PC 서명을 확인한다
    saveConf();
  } catch (e) { if (revoked) { setConn('off'); render(); return; } }
  online = true;
  setConn('on');
  buildTabs();
  poll();
}
function deviceName() {
  const ua = navigator.userAgent;
  const m = ua.match(/\(([^)]+)\)/);
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? (m && m[1].split(';').map(s => s.trim()).find(s => /^[A-Z]{2}-|SM-|Pixel|Galaxy/.test(s))) || 'Android' : 'Browser';
  const br = /SamsungBrowser/.test(ua) ? 'Samsung' : /CriOS|Chrome/.test(ua) ? 'Chrome' : /Safari/.test(ua) ? 'Safari' : /Firefox/.test(ua) ? 'Firefox' : '';
  return (os + ' ' + br).trim();
}
function setConn(s) {
  const d = $('#conn-dot');
  d.className = 'dot' + (s === 'on' ? ' on' : s === 'off' ? ' off' : '');
  $('#conn-text').textContent = s === 'on' ? t('connected') : s === 'off' ? '' : t('connecting');
}
let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  if (document.hidden) { pollTimer = setTimeout(poll, 4000); return; }
  try {
    const s = await api('/api/state', null, { etag: true });
    online = true; setConn('on');
    const sig = JSON.stringify(s, (k, v) => (k === 'now' ? 0 : v));
    if (sig !== stateSig) { stateSig = sig; state = s; render(true); }
    if (tab === 'talk') refreshChat();
  } catch (e) {
    if (revoked) { online = false; setConn('off'); render(); return; }
    online = false;
    setConn('connecting');
    if (!(await ping(conf.u))) { connect(); return; }
  }
  pollTimer = setTimeout(poll, 3000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && online) poll(); });

// ───────────── NPC 그림 ─────────────
const sprites = new Map();
function sprite(sid, pose, img) {
  const key = sid + '/' + pose;
  img.alt = '';
  if (sprites.has(key)) {
    const v = sprites.get(key);
    if (typeof v === 'string') img.src = v; else v.then(u => { img.src = u; });
    return img;
  }
  const p = api('/api/sprite?sid=' + encodeURIComponent(sid) + '&pose=' + pose + '&s=4', null, { raw: true })
    .then(b => { const u = URL.createObjectURL(b); sprites.set(key, u); return u; })
    .catch(() => { sprites.delete(key); return ''; });
  sprites.set(key, p);
  p.then(u => { if (u) img.src = u; });
  return img;
}
function poseOf(mode) { return mode === 'busy' ? 'type_a' : mode === 'wait' ? 'raise' : mode === 'sleep' ? 'sleep' : 'idle'; }
setInterval(() => {  // 작업 중인 세션은 타자 치는 두 그림을 번갈아
  document.querySelectorAll('img[data-busy]').forEach(img => {
    const b = img.dataset.flip === '1';
    img.dataset.flip = b ? '0' : '1';
    sprite(img.dataset.busy, b ? 'type_a' : 'type_b', img);
  });
}, 420);
function avatar(sid, mode, cls) {
  const img = h('img', { class: 'avatar' + (cls ? ' ' + cls : '') });
  if (mode === 'busy') img.dataset.busy = sid;
  return sprite(sid, poseOf(mode), img);
}
const imgs = new Map();
function meetImg(mid, name, img) {
  const key = mid + '/' + name;
  const set = u => { if (u) img.src = u; };
  if (imgs.has(key)) { Promise.resolve(imgs.get(key)).then(set); return img; }
  const p = api('/api/img?m=' + encodeURIComponent(mid) + '&f=' + encodeURIComponent(name), null, { raw: true, timeout: 30000 })
    .then(b => URL.createObjectURL(b)).catch(() => '');
  imgs.set(key, p);
  p.then(u => { imgs.set(key, u); set(u); });
  return img;
}

// ───────────── 탭 ─────────────
const ICONS = {
  office: '<path d="M3 11 12 4l9 7v9H3z" fill="currentColor"/><rect x="10" y="14" width="4" height="6" fill="var(--card)"/>',
  talk: '<path d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-5 4v-4H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" fill="currentColor"/><circle cx="8" cy="11" r="1.4" fill="var(--card)"/><circle cx="12" cy="11" r="1.4" fill="var(--card)"/><circle cx="16" cy="11" r="1.4" fill="var(--card)"/>',
  sec: '<path d="m12 3 2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4 6.7 19.4l1.2-6L3.4 9.3l6-.7z" fill="currentColor"/>',
  meet: '<circle cx="6" cy="7" r="2" fill="currentColor"/><circle cx="12" cy="5" r="2" fill="currentColor"/><circle cx="18" cy="7" r="2" fill="currentColor"/><rect x="2" y="11" width="20" height="4" rx="1.5" fill="currentColor"/><path d="M6 15v5M18 15v5" stroke="currentColor" stroke-width="2"/>',
  more: '<circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/>',
};
const TABS = ['office', 'talk', 'sec', 'meet', 'more'];
let tab = localStorage.getItem('co.tab') || 'office';
function buildTabs() {
  const nav = $('#tabs');
  nav.textContent = '';
  if (!online) return;
  nav.append(h('i', { class: 'ink' }));
  for (const k of TABS) {
    const b = h('button', { 'data-k': k, onclick: () => go(k) });
    b.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS[k] + '</svg>';  // 고정 아이콘만 (PC 데이터 아님)
    b.append(h('span', { text: t(k) }));
    nav.append(b);
  }
  markTab();
}
function markTab() {
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.k === tab));
  const on = $('#tabs button.on'), ink = $('#tabs .ink');
  if (on && ink) { ink.style.left = on.offsetLeft + on.offsetWidth * 0.3 + 'px'; ink.style.width = on.offsetWidth * 0.4 + 'px'; }
  const n = state ? state.sessions.filter(s => s.mode === 'wait').length : 0;
  badge('office', n);
  badge('sec', state ? state.sec.items.filter(i => !i.done).length : 0);
}
function badge(k, n) {
  const b = $(`#tabs button[data-k="${k}"]`);
  if (!b) return;
  let el = b.querySelector('.badge');
  if (!n) { if (el) el.remove(); return; }
  if (!el) { el = h('span', { class: 'badge' }); b.append(el); }
  el.textContent = n;
}
function go(k, dir) {
  if (tab === k) { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
  dir = dir || (TABS.indexOf(k) > TABS.indexOf(tab) ? 1 : -1);
  tab = k;
  localStorage.setItem('co.tab', k);
  buzz();
  markTab();
  render(false, dir);
  window.scrollTo(0, 0);
  if (k === 'talk') refreshChat(true);
  if (k === 'more') loadPast();
}
window.addEventListener('resize', markTab);

// ───────────── 그리기 ─────────────
function render(quiet, dir) {
  const v = $('#view');
  const keepY = window.scrollY;
  v.textContent = '';
  v.style.transform = ''; v.style.transition = ''; v.style.opacity = '';
  v.className = quiet ? '' : dir < 0 ? 'view-in from-left' : 'view-in';
  document.body.dataset.tab = online && state ? tab : '';
  if (!conf.k && !conf.t) return v.append(hero('🔗', t('notPaired'), t('notPairedSub')));
  if (!conf.k && join) return v.append(joinView());
  if (revoked) return v.append(hero('⛔', t('revoked'), t('notPairedSub')));
  if (!online || !state) {
    return v.append(pcOff ? hero('💤', t('pcOff'), t('pcOffSub')) : h('div', { class: 'hero' }, h('div', { class: 'spin' }), h('p', { class: 'text2', text: t('connecting') })));
  }
  if (quiet) v.classList.add('still');
  ({ office: viewOffice, talk: viewTalk, sec: viewSec, meet: viewMeet, more: viewMore })[tab](v);
  if (quiet) { v.querySelectorAll('.card').forEach(c => { c.style.animation = 'none'; }); window.scrollTo(0, keepY); }
  markTab();
  if (sheetFor) refreshSheet();
  if (!$('#drawer').hidden) { if (tab === 'talk') fillDrawer(); else closeDrawer(); }
}
function joinView() {
  const box = h('div', { class: 'hero join' }, h('div', { class: 'big', text: '🔒' }), h('h2', { text: t('joinTitle') }));
  if (join === 'expired') box.append(h('p', { class: 'text2', text: t('linkUsed') }));
  if (!pcInfo.pw) { box.append(h('p', { class: 'text2', text: t('noPwSet') })); return box; }
  box.append(h('p', { class: 'text2', text: t('joinSub') }));
  const fp = h('div', { class: 'fp' });
  if (conf.s) pcCode().then(x => { if (x) fp.append(h('b', { text: t('pcCode', x) }), h('span', { text: t('pcCodeSub') })); });
  const inp = h('input', { type: 'password', placeholder: t('pwPh'), autocomplete: 'current-password', enterkeyhint: 'go', maxlength: '128' });
  const err = h('div', { class: 'join-err' });
  const btn = h('button', { class: 'btn pri block', text: t('pwBtn') });
  const submit = async () => {
    if (!inp.value || btn.disabled) return;
    btn.disabled = true;
    err.textContent = '';
    if (!await ping(conf.u)) { err.textContent = t('netErr'); btn.disabled = false; return; }  // 보내기 직전에 진짜 PC인지 다시 확인
    const r = await openPost('/api/login', { pw: inp.value, name: deviceName() });
    if (r.ok && r.data.k) {
      inp.value = '';
      conf.k = r.data.k; saveConf();
      revoked = false; join = '';
      buzz(20);
      connect();
      return;
    }
    err.textContent = t(r.status === 429 ? 'pwLocked' : r.status === 403 ? 'noPwSet' : r.status === 401 ? 'pwBad' : 'netErr');
    if (r.status === 401) { inp.select(); buzz(40); }
    btn.disabled = false;
  };
  btn.onclick = submit;
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  box.append(fp, h('div', { class: 'join-form' }, inp, err, btn));
  setTimeout(() => inp.focus(), 300);
  return box;
}
function hero(icon, title, sub) {
  return h('div', { class: 'hero' }, h('div', { class: 'big', text: icon }), h('h2', { text: title }), h('p', { class: 'text2', text: sub }));
}
function chip(mode) { return h('span', { class: 'chip m-' + mode, text: t(mode) }); }

function viewOffice(v) {
  const ss = state.sessions;
  const counts = ['busy', 'wait', 'idle', 'sleep'].map(m => [m, ss.filter(s => s.mode === m).length]);
  v.append(h('div', { class: 'subbar' }, h('div', { class: 'row', style: 'gap:6px;flex-wrap:wrap' }, counts.map(([m, n]) => h('span', { class: 'chip m-' + m, text: t(m) + ' ' + n })))));
  if (!ss.length) return v.append(h('div', { class: 'empty' }, h('b', { text: t('noSessions') })));
  const order = { wait: 0, busy: 1, idle: 2, sleep: 3 };
  [...ss].sort((a, b) => order[a.mode] - order[b.mode]).forEach((s, i) => {
    const card = h('div', { class: 'card tap', style: `animation-delay:${i * 40}ms`, onclick: () => openSession(s.sid) },
      h('div', { class: 'row' }, avatar(s.sid, s.mode),
        h('div', { class: 'grow' },
          h('div', { class: 'row', style: 'gap:8px' }, h('div', { class: 'name grow', text: s.name }), chip(s.mode)),
          h('div', { class: 'small ell', text: s.tool || s.cwd }),
          s.goal ? h('div', { class: 'text2 ell', style: 'font-size:13px', text: s.goal }) : null,
          s.progress != null ? h('div', { class: 'bar' }, h('i', { style: `width:${Math.round(s.progress)}%` })) : null)),
      s.mode === 'wait' ? h('div', { class: 'wait-note', text: '⏳ ' + t('waitNote') + (s.waiting ? ' · ' + s.waiting : '') }) : null);
    v.append(card);
  });
}

// ───────────── 시트 (세션 상세) ─────────────
let sheetFor = null, sheetTab = 'qa', sheetData = null;
function openSheet(build) {
  const sh = $('#sheet'), back = $('#sheet-back');
  sh.textContent = '';
  sh.className = '';
  build(sh);
  sh.hidden = false; back.hidden = false;
  sh.style.transform = ''; sh.style.transition = '';
  back.onclick = closeSheet;
  document.documentElement.style.overflow = 'hidden';
  buzz();
}
function closeSheet() {
  const sh = $('#sheet');
  sh.classList.add('out');
  setTimeout(() => { sh.hidden = true; $('#sheet-back').hidden = true; document.documentElement.style.overflow = ''; }, 200);
  sheetFor = null;
}
// 시트는 맨 위에서(또는 위쪽 손잡이를 잡고) 아래로 끌면 손가락을 따라 내려가고, 충분히 끌거나 빠르게 튕기면 닫힌다
let sheetDrag = null;
$('#sheet').addEventListener('touchstart', e => {
  const sh = $('#sheet');
  sheetDrag = (sh.scrollTop <= 0 || e.target.closest('.sheet-top')) && !e.target.closest('textarea, .composer, .seg')
    ? { y: e.touches[0].clientY, t: Date.now(), dy: 0 } : null;
}, { passive: true });
$('#sheet').addEventListener('touchmove', e => {
  if (!sheetDrag) return;
  sheetDrag.dy = Math.max(0, e.touches[0].clientY - sheetDrag.y);
  if (sheetDrag.dy > 4) {
    e.preventDefault();
    const sh = $('#sheet');
    sh.style.transition = 'none';
    sh.style.transform = `translateY(${sheetDrag.dy}px)`;
  }
}, { passive: false });
$('#sheet').addEventListener('touchend', () => {
  if (!sheetDrag) return;
  const sh = $('#sheet'), d = sheetDrag;
  sheetDrag = null;
  const fast = d.dy / Math.max(1, Date.now() - d.t) > 0.6;
  sh.style.transition = 'transform .22s cubic-bezier(.2,.8,.2,1)';
  if (d.dy > 110 || (fast && d.dy > 30)) { sh.style.transform = 'translateY(100%)'; setTimeout(closeSheet, 200); } else sh.style.transform = '';
});

function openSession(sid) {
  sheetFor = sid;
  openSheet(sh => {
    sh.append(h('div', { class: 'sheet-top' }, h('div', { class: 'grip' }), h('div', { id: 'sess-head' }),
      segBar(['qa', 'log', 'tasks'], sheetTab, k => { sheetTab = k; fillSession(); })), h('div', { id: 'sess-body' }),
      composer(t('cmdPh'), async text => {
        await api('/api/send', { sid, text });
        toast(t('sent'));
        setTimeout(loadSession, 1500);
      }));
    refreshSheet();
    loadSession();
  });
}
function segBar(keys, on, pick) {
  const seg = h('div', { class: 'seg' }, h('i', { class: 'knob' }));
  keys.forEach(k => seg.append(h('button', { 'data-k': k, class: k === on ? 'on' : '', text: t(k), onclick: e => { seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === e.currentTarget)); moveKnob(seg); pick(k); } })));
  requestAnimationFrame(() => moveKnob(seg));
  return seg;
}
function moveKnob(seg) {
  const b = seg.querySelector('button.on'), k = seg.querySelector('.knob');
  if (b && k) { k.style.left = b.offsetLeft + 'px'; k.style.width = b.offsetWidth + 'px'; }
}
function composer(ph, onSend) {
  const ta = h('textarea', { rows: 1, placeholder: ph, enterkeyhint: 'send' });
  ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(140, ta.scrollHeight) + 'px'; });
  const btn = h('button', { class: 'btn pri', text: t('send') });
  btn.onclick = async () => {
    const text = ta.value.trim();
    if (!text) return;
    btn.disabled = true;
    try { await onSend(text); ta.value = ''; ta.style.height = ''; buzz(12); } catch (e) { toast(t('err', e.message === 'session is off' ? t('offSession') : e.message), true); }
    btn.disabled = false;
  };
  return h('div', { class: 'composer' }, ta, btn);
}
function refreshSheet() {
  const s = state && state.sessions.find(x => x.sid === sheetFor);
  const head = $('#sess-head');
  if (!head) return;
  head.textContent = '';
  if (!s) { head.append(h('div', { class: 'empty', text: t('offSession') })); return; }
  head.append(h('div', { class: 'row' }, avatar(s.sid, s.mode),
    h('div', { class: 'grow' }, h('div', { class: 'name', text: s.name }), h('div', { class: 'row', style: 'gap:6px' }, chip(s.mode), h('span', { class: 'small', text: s.model || '' })),
      h('div', { class: 'small ell', text: s.cwd }), h('div', { class: 'small', text: t('today', fmtN(s.tok)) }))),
  s.mode === 'wait' ? h('div', { class: 'wait-note', text: '⏳ ' + t('waitNote') + (s.tool ? ' · ' + s.tool : '') }) : null);
}
async function loadSession() {
  if (!sheetFor) return;
  try { sheetData = await api('/api/session?sid=' + encodeURIComponent(sheetFor)); } catch (e) { sheetData = null; }
  fillSession();
}
function fillSession() {
  const body = $('#sess-body');
  if (!body) return;
  body.textContent = '';
  const s = state && state.sessions.find(x => x.sid === sheetFor);
  if (sheetTab === 'tasks') {
    if (!s || !s.tasks.length) return body.append(h('div', { class: 'empty', text: t('noQa') }));
    if (s.goal) body.append(h('div', { class: 'text2', style: 'margin:4px 2px 8px', text: s.goal }));
    s.tasks.forEach(k => body.append(h('div', { class: 'log' }, h('span', { class: 't', text: k.s === 'completed' ? '✔' : k.s === 'in_progress' ? '▶' : '·' }), k.t, k.p != null && k.s === 'in_progress' ? ` (${k.p}%)` : '')));
    return;
  }
  if (!sheetData) return body.append(h('div', { class: 'empty' }, h('div', { class: 'spin' })));
  if (sheetTab === 'qa') {
    if (!sheetData.qa.length) return body.append(h('div', { class: 'empty', text: t('noQa') }));
    [...sheetData.qa].reverse().forEach(e => body.append(h('div', { class: 'qa' }, h('div', { class: 'small', text: ago(e.qt) }), h('div', { class: 'q', text: e.q }), e.a ? h('div', { class: 'a', text: e.a }) : null)));
  } else {
    const icon = { user: '▶', say: '“', tool: '⚙', err: '⚠', msg: '✉' };
    [...sheetData.events].reverse().forEach(e => body.append(h('div', { class: 'log' }, h('span', { class: 't', text: new Date(e.ts * 1000).toTimeString().slice(0, 5) }), (icon[e.k] || '·') + ' ' + e.t)));
  }
}

// ───────────── 세션톡 ─────────────
let room = localStorage.getItem('co.room') || '*', chatData = null, chatSig = '';
let roomsOpen = localStorage.getItem('co.roomsOpen') !== '0', talkComposer = null, talkComposerRoom = null;
function viewTalk(v) {
  const rooms = h('div', { class: 'rooms' });
  rooms.append(h('button', { class: 'room' + (room === '*' ? ' on' : ''), onclick: () => setRoom('*') }, h('div', { class: 'all', text: '#' }), h('span', { text: t('all') })));
  state.sessions.forEach(s => {
    const img = h('img');
    sprite(s.sid, poseOf(s.mode), img);
    rooms.append(h('button', { class: 'room' + (room === s.sid ? ' on' : ''), onclick: () => setRoom(s.sid) }, img, h('span', { text: s.name })));
  });
  // 방 목록은 위에 고정되고, 버튼으로 접고 편다 (접어도 지금 방은 보인다)
  const cur = state.sessions.find(s => s.sid === room);
  const curImg = h('img', { class: 'cur-img', alt: '' });
  if (cur) sprite(cur.sid, poseOf(cur.mode), curImg);
  const bar = h('div', { class: 'subbar talkbar' + (roomsOpen ? '' : ' closed') });
  const toggle = h('button', { class: 'fold-btn', 'aria-label': 'rooms', onclick: e => {
    e.stopPropagation();
    roomsOpen = !roomsOpen;
    localStorage.setItem('co.roomsOpen', roomsOpen ? '1' : '0');
    bar.classList.toggle('closed', !roomsOpen);
    buzz();
  } }, h('i'));
  bar.append(h('div', { class: 'talk-cur', onclick: () => toggle.click() }, cur ? curImg : h('span', { class: 'all-mini', text: '#' }),
    h('b', { class: 'ell grow', text: cur ? cur.name : t('all') }), toggle), h('div', { class: 'rooms-wrap' }, rooms));
  dragScroll(rooms);
  const on = rooms.querySelector('.room.on');
  if (on) requestAnimationFrame(() => { rooms.scrollLeft = Math.max(0, on.offsetLeft - rooms.clientWidth / 2 + on.offsetWidth / 2); });
  v.append(bar, h('div', { class: 'chat', id: 'chat' }));
  if (cur) {
    if (!talkComposer || talkComposerRoom !== room) {  // 다시 그려도 쓰던 글이 지워지지 않게 같은 입력칸을 쓴다
      talkComposerRoom = room;
      talkComposer = composer(t('talkPh'), async text => { await api('/api/send', { sid: talkComposerRoom, text }); toast(t('sent')); refreshChat(true); });
      talkComposer.classList.add('fixed');
    }
    if (talkComposer.parentNode !== document.body) document.body.append(talkComposer);
  } else {
    if (talkComposer) { talkComposer.remove(); talkComposer = null; }
    v.append(h('div', { class: 'small', style: 'text-align:center;padding:10px', text: t('pickRoom') }));
  }
  drawChat();
}
// ───────────── 그림 크게 보기 ─────────────
// 두 손가락으로 벌리거나 두 번 탭하면 확대, 확대한 채로 끌면 이동, 확대 안 한 채로 좌우로 넘기면 다음 그림,
// 아래로 끌거나 바깥을 누르거나 ✕·뒤로 가기로 닫는다. PC 미리보기 창에서는 휠 확대·끌기·화살표·Esc.
const vw = { mid: '', names: [], i: 0, s: 1, tx: 0, ty: 0 };
function openViewer(mid, names, i) {
  if (!names || !names.length) return;
  Object.assign(vw, { mid, names: names.slice(), i, s: 1, tx: 0, ty: 0 });
  const box = $('#viewer');
  box.textContent = '';
  const img = h('img', { class: 'vw-img', alt: '', draggable: 'false' });
  const count = h('span', { class: 'vw-count' });
  box.append(h('div', { class: 'vw-top' }, count, h('button', { class: 'icon-btn vw-x', 'aria-label': 'close', text: '✕', onclick: closeViewer })),
    h('div', { class: 'vw-stage' }, img),
    names.length > 1 ? h('button', { class: 'vw-nav prev', 'aria-label': 'prev', text: '‹', onclick: e => { e.stopPropagation(); vwGo(-1); } }) : null,
    names.length > 1 ? h('button', { class: 'vw-nav next', 'aria-label': 'next', text: '›', onclick: e => { e.stopPropagation(); vwGo(1); } }) : null,
    h('div', { class: 'vw-hint', text: t('zoomHint') }));
  box.hidden = false;
  box.classList.remove('out');
  document.documentElement.style.overflow = 'hidden';
  history.pushState({ viewer: 1 }, '');
  vwShow();
  buzz();
}
function vwShow() {
  const img = $('#viewer .vw-img');
  if (!img) return;
  vw.s = 1; vw.tx = 0; vw.ty = 0;
  vwApply(false);
  img.removeAttribute('src');
  meetImg(vw.mid, vw.names[vw.i], img);
  $('#viewer .vw-count').textContent = vw.names.length > 1 ? `${vw.i + 1} / ${vw.names.length}` : '';
}
function vwGo(d) {
  if (vw.names.length < 2) return;
  vw.i = (vw.i + d + vw.names.length) % vw.names.length;
  vwShow();
  buzz();
}
function vwClamp() {  // 확대한 그림이 화면 밖으로 다 나가지 않게
  const img = $('#viewer .vw-img'), st = $('#viewer .vw-stage');
  if (!img || !st) return;
  const mx = Math.max(0, (img.offsetWidth * vw.s - st.clientWidth) / 2), my = Math.max(0, (img.offsetHeight * vw.s - st.clientHeight) / 2);
  vw.tx = Math.max(-mx, Math.min(mx, vw.tx));
  vw.ty = Math.max(-my, Math.min(my, vw.ty));
}
function vwApply(anim, dy = 0) {
  const img = $('#viewer .vw-img');
  if (!img) return;
  img.style.transition = anim ? 'transform .22s cubic-bezier(.2,.8,.2,1)' : 'none';
  img.style.transform = `translate(${vw.tx}px, ${vw.ty + dy}px) scale(${vw.s})`;
  $('#viewer').style.background = dy ? `rgba(8,10,14,${Math.max(0.35, 0.96 - Math.abs(dy) / 500)})` : '';
}
function vwZoomAt(s2, px, py) {  // (px,py) = 화면 가운데 기준 위치. 그 점이 손가락 아래에 머물도록
  s2 = Math.max(1, Math.min(5, s2));
  vw.tx = px - (px - vw.tx) * (s2 / vw.s);
  vw.ty = py - (py - vw.ty) * (s2 / vw.s);
  vw.s = s2;
  if (vw.s === 1) { vw.tx = 0; vw.ty = 0; }
  vwClamp();
}
function closeViewer(fromPop) {
  const box = $('#viewer');
  if (box.hidden) return;
  box.classList.add('out');
  setTimeout(() => { box.hidden = true; box.textContent = ''; box.style.background = ''; if ($('#sheet').hidden && $('#drawer').hidden) document.documentElement.style.overflow = ''; }, 180);
  if (fromPop !== true && history.state && history.state.viewer) history.back();
}
window.addEventListener('popstate', () => closeViewer(true));
document.addEventListener('keydown', e => {
  if ($('#viewer').hidden) return;
  if (e.key === 'Escape') closeViewer();
  else if (e.key === 'ArrowLeft') vwGo(-1);
  else if (e.key === 'ArrowRight') vwGo(1);
});
(function viewerGestures() {
  const box = $('#viewer');
  let g = null, lastTap = 0;
  const center = () => { const r = $('#viewer .vw-stage').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  box.addEventListener('touchstart', e => {
    if (e.target.closest('button')) { g = null; return; }
    const t = e.touches;
    if (t.length === 2) {
      const [cx, cy] = center();
      g = { mode: 'pinch', d0: dist(t), s0: vw.s, tx0: vw.tx, ty0: vw.ty, mx: (t[0].clientX + t[1].clientX) / 2 - cx, my: (t[0].clientY + t[1].clientY) / 2 - cy };
    } else if (t.length === 1) {
      g = { mode: null, x: t[0].clientX, y: t[0].clientY, tx0: vw.tx, ty0: vw.ty, t: Date.now(), dx: 0, dy: 0, onImg: !!e.target.closest('.vw-img') };
    }
  }, { passive: true });
  box.addEventListener('touchmove', e => {
    if (!g) return;
    e.preventDefault();
    const t = e.touches;
    if (g.mode === 'pinch' && t.length === 2) {
      vw.s = g.s0; vw.tx = g.tx0; vw.ty = g.ty0;
      vwZoomAt(g.s0 * dist(t) / g.d0, g.mx, g.my);
      vwApply(false);
      return;
    }
    if (t.length !== 1 || g.mode === 'pinch') return;
    g.dx = t[0].clientX - g.x; g.dy = t[0].clientY - g.y;
    if (!g.mode && Math.hypot(g.dx, g.dy) > 8) g.mode = vw.s > 1 ? 'pan' : Math.abs(g.dx) > Math.abs(g.dy) ? 'swipe' : 'close';
    if (g.mode === 'pan') { vw.tx = g.tx0 + g.dx; vw.ty = g.ty0 + g.dy; vwClamp(); vwApply(false); }
    else if (g.mode === 'swipe') { vw.tx = g.dx; vwApply(false); }
    else if (g.mode === 'close') vwApply(false, Math.max(0, g.dy));
  }, { passive: false });
  box.addEventListener('touchend', e => {
    if (!g || e.touches.length) { if (g && g.mode === 'pinch' && e.touches.length === 1) g = null; return; }
    const d = g;
    g = null;
    e.preventDefault();  // 터치 뒤에 브라우저가 흉내 내는 마우스 클릭·더블클릭이 확대를 되돌리지 않게
    if (d.mode === 'swipe') {
      const fast = Math.abs(d.dx) / Math.max(1, Date.now() - d.t) > 0.4;
      vw.tx = 0;
      if (vw.names.length > 1 && (Math.abs(d.dx) > 70 || (fast && Math.abs(d.dx) > 25))) vwGo(d.dx < 0 ? 1 : -1); else vwApply(true);
    } else if (d.mode === 'close') {
      if (d.dy > 110 || (d.dy > 30 && d.dy / Math.max(1, Date.now() - d.t) > 0.5)) closeViewer(); else vwApply(true);
    } else if (!d.mode) {  // 탭
      const now = Date.now();
      if (d.onImg && now - lastTap < 300) {
        const [cx, cy] = center();
        if (vw.s > 1) { vw.s = 1; vw.tx = 0; vw.ty = 0; } else vwZoomAt(2.5, d.x - cx, d.y - cy);
        vwApply(true);
        lastTap = 0;
      } else {
        lastTap = now;
        if (!d.onImg && vw.s === 1) closeViewer();
      }
    }
  });
  // 마우스 (PC 미리보기 창)
  box.addEventListener('wheel', e => {
    if (box.hidden) return;
    e.preventDefault();
    const [cx, cy] = center();
    vwZoomAt(vw.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2), e.clientX - cx, e.clientY - cy);
    vwApply(false);
  }, { passive: false });
  let md = null;
  box.addEventListener('mousedown', e => { if (e.button === 0 && !e.target.closest('button')) md = { x: e.clientX, y: e.clientY, tx0: vw.tx, ty0: vw.ty, moved: false, onImg: !!e.target.closest('.vw-img') }; });
  window.addEventListener('mousemove', e => {
    if (!md || vw.s === 1) return;
    md.moved = md.moved || Math.hypot(e.clientX - md.x, e.clientY - md.y) > 4;
    vw.tx = md.tx0 + e.clientX - md.x; vw.ty = md.ty0 + e.clientY - md.y; vwClamp(); vwApply(false);
  });
  window.addEventListener('mouseup', () => { if (md && !md.moved && !md.onImg && vw.s === 1) closeViewer(); md = null; });
  box.addEventListener('dblclick', e => {
    if (!e.target.closest('.vw-img')) return;
    const [cx, cy] = center();
    if (vw.s > 1) { vw.s = 1; vw.tx = 0; vw.ty = 0; } else vwZoomAt(2.5, e.clientX - cx, e.clientY - cy);
    vwApply(true);
  });
})();

// 마우스로도 끌어서 넘긴다 (손가락은 브라우저가 직접 굴린다). 끌었으면 손을 뗄 때의 클릭은 무시한다.
function dragScroll(el) {
  let d = null;
  el.addEventListener('dragstart', e => e.preventDefault());  // 그림을 잡아 끄는 기본 동작이 굴리기를 끊지 않게
  el.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'mouse' || e.button !== 0) return;
    d = { x: e.clientX, left: el.scrollLeft, moved: false };
  });
  el.addEventListener('pointermove', e => {
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 5) { d.moved = true; el.classList.add('dragging'); el.setPointerCapture(e.pointerId); }
    if (d.moved) el.scrollLeft = d.left - dx;
  });
  const end = () => {
    if (d && d.moved) el.addEventListener('click', ev => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
    d = null;
    el.classList.remove('dragging');
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
}

// 위쪽 막대의 메뉴: 대화상대 전체 목록에서 세션톡 상대를 고른다
let drawerQ = '';
function openDrawer() {
  const dr = $('#drawer'), back = $('#drawer-back');
  dr.textContent = '';
  dr.className = '';
  dr.style.transform = ''; dr.style.transition = '';
  const close = h('button', { class: 'icon-btn', 'aria-label': 'close', text: '✕', onclick: closeDrawer });
  const q = h('input', { type: 'search', placeholder: t('searchName'), enterkeyhint: 'search' });
  q.value = drawerQ;
  q.addEventListener('input', () => { drawerQ = q.value; fillDrawer(); });
  dr.append(h('div', { class: 'dr-head' }, h('b', { class: 'grow', text: t('members') }), h('span', { class: 'cnt', id: 'dr-cnt' }), close),
    h('div', { class: 'dr-search' }, q), h('div', { class: 'dr-list', id: 'dr-list' }));
  fillDrawer();
  dr.hidden = false; back.hidden = false;
  back.onclick = closeDrawer;
  document.documentElement.style.overflow = 'hidden';
  buzz();
}
function closeDrawer() {
  const dr = $('#drawer');
  if (dr.hidden) return;
  dr.classList.add('out');
  setTimeout(() => { dr.hidden = true; $('#drawer-back').hidden = true; if ($('#sheet').hidden) document.documentElement.style.overflow = ''; }, 200);
}
function fillDrawer() {
  const list = $('#dr-list');
  if (!list || !state) return;
  const ss = state.sessions;
  $('#dr-cnt').textContent = ss.length;
  const keep = list.scrollTop;
  list.textContent = '';
  const pick = r => { closeDrawer(); if (r !== room) setRoom(r); };
  const qq = drawerQ.trim().toLowerCase();
  if (!qq) list.append(h('button', { class: 'dr-row' + (room === '*' ? ' on' : ''), onclick: () => pick('*') },
    h('div', { class: 'pf', text: '#' }), h('div', { class: 'grow' }, h('div', { class: 'nm', text: t('allRoom') }), h('div', { class: 'st-msg ell', text: t('allRoomSub') })),
    room === '*' ? h('span', { class: 'chk', text: '✓' }) : null));
  const order = { wait: 0, busy: 1, idle: 2, sleep: 3 };
  const hits = [...ss].filter(s => !qq || (s.name || '').toLowerCase().includes(qq) || (s.goal || '').toLowerCase().includes(qq))
    .sort((a, b) => order[a.mode] - order[b.mode]);
  list.append(h('div', { class: 'dr-sec', text: t('sessionsN', hits.length) }));
  if (!hits.length) list.append(h('div', { class: 'dr-empty', text: t('noMatch') }));
  hits.forEach(s => {
    const pf = avatar(s.sid, s.mode);
    pf.className = 'pf';
    list.append(h('button', { class: 'dr-row' + (room === s.sid ? ' on' : ''), onclick: () => pick(s.sid) }, pf,
      h('div', { class: 'grow' }, h('div', { class: 'row', style: 'gap:6px' }, h('div', { class: 'nm ell grow', text: s.name }), chip(s.mode)),
        h('div', { class: 'st-msg ell', text: s.goal || s.tool || s.cwd || '' })),
      room === s.sid ? h('span', { class: 'chk', text: '✓' }) : null));
  });
  list.scrollTop = keep;
}
// 서랍은 오른쪽으로 끌면 손가락을 따라가다 닫힌다
(function drawerDrag() {
  const dr = $('#drawer');
  let d = null;
  dr.addEventListener('touchstart', e => {
    d = e.target.closest('input') ? null : { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now(), dx: 0, mode: null };
  }, { passive: true });
  dr.addEventListener('touchmove', e => {
    if (!d) return;
    const dx = e.touches[0].clientX - d.x, dy = e.touches[0].clientY - d.y;
    if (!d.mode && Math.hypot(dx, dy) > 10) d.mode = dx > 0 && Math.abs(dx) > Math.abs(dy) * 1.2 ? 'x' : 'y';
    if (d.mode !== 'x') return;
    e.preventDefault();
    d.dx = Math.max(0, dx);
    dr.style.transition = 'none';
    dr.style.transform = `translateX(${d.dx}px)`;
  }, { passive: false });
  dr.addEventListener('touchend', () => {
    if (!d || d.mode !== 'x') { d = null; return; }
    const fast = d.dx / Math.max(1, Date.now() - d.t) > 0.5;
    dr.style.transition = 'transform .22s cubic-bezier(.2,.8,.2,1)';
    if (d.dx > 90 || (fast && d.dx > 30)) { dr.style.transform = 'translateX(100%)'; setTimeout(closeDrawer, 200); } else dr.style.transform = '';
    d = null;
  });
})();
function setRoom(r) { room = r; localStorage.setItem('co.room', r); chatData = null; render(); refreshChat(true); }
async function refreshChat(force) {
  try {
    const d = await api('/api/chat?room=' + encodeURIComponent(room), null, { etag: true });
    const sig = JSON.stringify(d);
    if (sig === chatSig && !force) return;
    chatSig = sig; chatData = d;
    drawChat(true);
  } catch (e) { /* 다음 폴링 때 */ }
}
// ── 마크다운 (Claude Code 터미널처럼): 굵게·기울임·코드·코드 블록·제목·목록·인용·링크·취소선. 글자는 textContent로만 넣는다 ──
const MD_INLINE = /\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__|`([^`\n]+)`|~~(?=\S)(.+?)(?<=\S)~~|\[([^\]\n]+)\]\(([^)\s]+)\)|(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])|(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])/g;
function mdInline(s) {
  const out = [];
  let pos = 0;
  for (const m of s.matchAll(MD_INLINE)) {
    if (m.index > pos) out.push(document.createTextNode(s.slice(pos, m.index)));
    if (m[1] != null || m[2] != null) out.push(h('b', {}, mdInline(m[1] ?? m[2])));
    else if (m[3] != null) out.push(h('code', { text: m[3] }));
    else if (m[4] != null) out.push(h('s', {}, mdInline(m[4])));
    else if (m[5] != null) {
      const ok = /^https?:\/\//i.test(m[6]);  // 웹 주소만 링크로 (javascript: 등은 글자로)
      out.push(ok ? h('a', { href: m[6], target: '_blank', rel: 'noopener noreferrer' }, mdInline(m[5])) : h('span', {}, mdInline(m[5])));
    } else out.push(h('i', {}, mdInline(m[7] ?? m[8])));
    pos = m.index + m[0].length;
  }
  if (pos < s.length) out.push(document.createTextNode(s.slice(pos)));
  return out;
}
function md(text) {
  const frag = document.createDocumentFragment();
  let code = null, list = null;
  const endList = () => { list = null; };
  for (const ln of String(text || '').replace(/\r/g, '').split('\n')) {
    const st = ln.trim();
    if (st.startsWith('```')) {
      if (code) { code = null; } else { endList(); code = h('code', {}); frag.append(h('pre', {}, code)); }
      continue;
    }
    if (code) { code.append((code.textContent ? '\n' : '') + ln); continue; }
    if (!st) { endList(); continue; }
    let m;
    if ((m = st.match(/^(#{1,6})\s+(.*)/))) { endList(); frag.append(h('div', { class: 'md-h' }, mdInline(m[2]))); continue; }
    if (/^((-\s*){3,}|(\*\s*){3,}|(_\s*){3,})$/.test(st)) { endList(); frag.append(h('hr')); continue; }
    if ((m = ln.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)/))) {
      const ordered = !/[-*+]/.test(m[2]);
      const depth = Math.min(3, Math.floor(m[1].replace(/\t/g, '    ').length / 2));
      if (!list) { list = h('div', { class: 'md-list' }); frag.append(list); }
      list.append(h('div', { class: 'md-li', style: `padding-left:${depth * 14 + 14}px` },
        h('span', { class: 'md-mark', text: ordered ? m[2] : '•' }), mdInline(m[3])));
      continue;
    }
    endList();
    if ((m = st.match(/^>\s?(.*)/))) { frag.append(h('div', { class: 'md-q' }, mdInline(m[1]))); continue; }
    if (st.startsWith('|') && st.endsWith('|')) { frag.append(h('div', { class: 'md-table', text: st })); continue; }
    frag.append(h('div', {}, mdInline(ln)));
  }
  return frag;
}
function drawChat(scroll) {
  const box = $('#chat');
  if (!box) return;
  box.textContent = '';
  if (!chatData) { box.append(h('div', { class: 'empty' }, h('div', { class: 'spin' }))); return; }
  if (!chatData.msgs.length) box.append(h('div', { class: 'empty', text: t('noQa') }));
  chatData.msgs.forEach(m => {
    const me = m.from === 'user';
    box.append(h('div', { class: 'msg ' + (me ? 'me' : 'other') },
      me ? null : h('div', { class: 'who', text: (m.fromLabel || '') + (room === '*' && m.toLabel ? ' → ' + (m.to === 'user' ? t('me') : m.toLabel) : '') }),
      h('div', { class: 'b md' }, md(m.text)),
      h('div', { class: 'meta', text: (me && m.read ? t('read') + ' · ' : '') + ago(m.ts) })));
  });
  if (chatData.typing.length && (room === '*' || chatData.typing.includes(room))) box.append(h('div', { class: 'typing' }, h('i'), h('i'), h('i')));
  if (scroll) window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

// ───────────── 비서 ─────────────
let showDone = false;
function viewSec(v) {
  const sec = state.sec;
  const head = h('div', { class: 'card sec-head' },
    h('div', { class: 'row' }, h('div', { class: 'grow' }, h('div', { class: 'name', text: t('sec') }),
      h('div', { class: 'small', text: sec.updated ? ago(sec.updated / 1000) : '' })),
    h('button', { class: 'btn', style: 'background:#fff;color:#1d8f66;border:0', disabled: !sec.sid || sec.briefing, text: sec.briefing ? t('briefing') : t('brief'),
      onclick: async e => { e.currentTarget.disabled = true; try { await api('/api/brief', {}); toast(t('briefing')); poll(); } catch (er) { toast(t('err', er.message), true); } } })),
    h('div', { style: 'margin-top:8px;font-size:14px', text: sec.sid ? (sec.summary || '') : t('noSec') }));
  v.append(head);
  const items = sec.items.filter(i => showDone || !i.done);
  v.append(h('label', { class: 'small', style: 'display:block;margin:0 4px 10px' },
    h('input', { type: 'checkbox', style: 'width:auto;margin-right:6px', checked: showDone, onchange: e => { showDone = e.target.checked; render(); } }), t('showDone')));
  if (!items.length) return v.append(h('div', { class: 'empty', text: t('noSugg') }));
  items.forEach((it, i) => {
    const btns = h('div', { class: 'btns' });
    it.choices.forEach((ch, j) => btns.append(h('button', { class: 'btn ' + (j ? 'line' : 'pri'), disabled: !ch.on, text: ch.label,
      onclick: async e => { e.currentTarget.disabled = true; try { await api('/api/choice', { key: it.key, i: ch.i }); toast(t('sent')); poll(); } catch (er) { toast(t('err', er.message), true); } } })));
    if (it.url) btns.append(h('a', { class: 'btn', href: it.url, target: '_blank', rel: 'noopener noreferrer', style: 'text-decoration:none', text: t('link') }));
    btns.append(h('button', { class: 'btn', style: 'margin-left:auto', text: it.done ? t('undo') : t('markDone'),
      onclick: async () => { await api('/api/secmark', { key: it.key, done: !it.done }).catch(() => {}); poll(); } }));
    v.append(h('div', { class: 'card' + (it.done ? ' done' : ''), style: `animation-delay:${i * 50}ms` },
      h('div', {}, h('span', { class: 'kind k-' + it.kind, text: t('kinds.' + it.kind) }), h('b', { text: it.title })),
      it.target ? h('div', { class: 'small', text: '→ ' + it.target }) : null,
      it.detail ? h('details', {}, h('summary', { text: '…' }), h('div', { class: 'text2', style: 'white-space:pre-wrap;font-size:14px', text: it.detail })) : null,
      btns));
  });
}

// ───────────── 회의실 ─────────────
let meetPick = new Set(JSON.parse(localStorage.getItem('co.meetPick') || '[]')), meetTurns = 3;
let talk = { sid: null, pages: [], pi: 0, n: 0, timer: null, text: '' };
const ST = { done: ['stDone', '#2fb985'], partial: ['stPartial', '#f0a020'], todo: ['stTodo', '#e8505b'] };
// 회의실 장면(무대+대사 창)을 핀으로 위에 고정: 회의록을 내려 읽어도 발표가 보인다 (기기에 기억)
let meetPin = localStorage.getItem('co.meetPin') === '1';
const PIN_SVG = '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M15 3l6 6-3 1-4 4 1 5-2 2-4-4-5 5-1-1 5-5-4-4 2-2 5 1 4-4z" fill="currentColor"/></svg>';
function viewMeet(v) {
  const m = state.meeting;
  const top = h('div', { class: 'meet-top' + (meetPin ? ' pinned' : '') });
  const pin = h('button', { class: 'pin-btn' + (meetPin ? ' on' : ''), 'aria-label': 'pin', title: t('pinMeet'), onclick: e => {
    e.stopPropagation();
    meetPin = !meetPin;
    localStorage.setItem('co.meetPin', meetPin ? '1' : '0');
    top.classList.toggle('pinned', meetPin);
    pin.classList.toggle('on', meetPin);
    toast(t(meetPin ? 'pinnedMeet' : 'unpinnedMeet'));
    buzz();
  } });
  pin.innerHTML = PIN_SVG;  // 고정 아이콘만 (PC 데이터 아님)
  top.append(stage(m), dialog(m), pin);
  v.append(top);
  if (!m) {
    const pick = h('div', { class: 'pick' });
    state.sessions.forEach(s => {
      const img = h('img');
      sprite(s.sid, 'idle', img);
      pick.append(h('button', { class: meetPick.has(s.sid) ? 'on' : '', onclick: e => {
        meetPick.has(s.sid) ? meetPick.delete(s.sid) : meetPick.add(s.sid);
        localStorage.setItem('co.meetPick', JSON.stringify([...meetPick]));
        e.currentTarget.classList.toggle('on');
        drawStage(null);
      } }, img, h('span', { class: 'ell', text: s.name })));
    });
    const turns = h('div', { class: 'chips' });
    [1, 3, 5, 10].forEach(n => turns.append(h('button', { class: n === meetTurns ? 'on' : '', text: n === 1 ? t('turn1') : t('turnN', n), onclick: e => {
      meetTurns = n; turns.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === e.currentTarget));
    } })));
    v.append(h('div', { class: 'card' }, h('b', { text: t('invite') }), h('div', { style: 'margin-top:8px' }, pick),
      h('div', { style: 'margin:12px 0 6px' }, h('b', { text: t('range') })), turns,
      h('button', { class: 'btn pri block', style: 'margin-top:14px', text: t('start'), onclick: async e => {
        const sids = [...meetPick].filter(x => state.sessions.some(s => s.sid === x));
        if (!sids.length) return toast(t('invite'), true);
        e.currentTarget.disabled = true;
        try { await api('/api/meeting/start', { sids, turns: meetTurns }); toast(t('sent')); poll(); } catch (er) { toast(t('err', er.message), true); e.currentTarget.disabled = false; }
      } })));
    return;
  }
  m.roster.filter(r => r.report).forEach((r, i) => {
    const rep = r.report;
    const card = h('div', { class: 'card', style: `animation-delay:${i * 50}ms` },
      h('div', { class: 'row' }, avatar(r.sid, 'idle', 's'), h('div', { class: 'grow' }, h('div', { class: 'name', text: r.name }), h('div', { style: 'color:var(--acc-text);font-weight:700', text: rep.title })),
        h('button', { class: 'btn line', text: t('presentAgain'), onclick: () => { present(r.sid); window.scrollTo({ top: 0, behavior: 'smooth' }); } })),
      rep.summary ? h('div', { class: 'text2', style: 'margin-top:8px;font-size:14px;white-space:pre-wrap', text: rep.summary }) : null);
    rep.items.forEach(it => {
      const st = ST[it.status] || ST.done;
      card.append(h('div', { style: 'border-top:1px solid var(--edge);margin-top:10px;padding-top:8px' },
        h('div', {}, h('span', { class: 'st', style: 'background:' + st[1], text: t(st[0]) }), h('b', { text: it.request })),
        it.result ? h('div', { class: 'text2', style: 'font-size:14px;white-space:pre-wrap;margin-top:4px', text: it.result }) : null,
        it.images.length ? h('div', { class: 'thumbs' }, it.images.map((n, k) => meetImg(m.id, n, h('img', { alt: '', onclick: () => openViewer(m.id, it.images, k) })))) : null));
    });
    if (rep.next) card.append(h('div', { class: 'small', style: 'margin-top:8px', text: t('next') + rep.next }));
    v.append(card);
  });
  v.append(h('button', { class: 'btn block', style: 'margin-top:6px', text: t('end'), onclick: async () => {
    if (!confirm(t('endQ'))) return;
    await api('/api/meeting/end', {}).catch(() => {});
    talk.sid = null; poll();
  } }));
  autoPresent(m);
}
function seatState(r, m) {
  if (!m) return 'guest';
  if (r.report) return r.sid === talk.sid ? 'talk' : (m.presented || []).includes(r.sid) ? 'done' : 'ready';
  return r.mode == null ? 'off' : r.mode === 'busy' ? 'busy' : 'wait';
}
const BADGE = { busy: ['…', '#5b9ee8'], wait: ['?', '#f0a020'], ready: ['!', '#2fb985'], done: ['✓', '#8a96a3'], off: ['z', '#9a86c8'] };
function stage(m) { const st = h('div', { class: 'stage', id: 'stage' }); drawStage(m, st); return st; }
function drawStage(m, st = $('#stage')) {
  if (!st) return;
  const prev = new Set([...st.querySelectorAll('.seat')].map(x => x.dataset.sid));
  st.textContent = '';
  const page = talk.pages[talk.pi];
  const scr = h('div', { class: 'screen' });
  if (page && page.images && page.images.length && m) {
    scr.append(meetImg(m.id, page.images[0], h('img', { alt: '' })), h('span', { class: 'zoom-tag', text: '⤢' }));
    scr.classList.add('tap');
    const ims = page.images.slice();
    scr.onclick = e => { e.stopPropagation(); openViewer(m.id, ims, 0); };
  }
  else scr.textContent = talk.sid && m ? ((m.roster.find(r => r.sid === talk.sid) || {}).report || {}).title || '' : t('meet');
  const roster = m ? m.roster : state.sessions.filter(s => meetPick.has(s.sid)).map(s => ({ sid: s.sid, name: s.name, mode: s.mode }));
  const seats = h('div', { class: 'seats' });
  roster.forEach(r => {
    const s = seatState(r, m);
    const img = h('img', { alt: '' });
    sprite(r.sid, s === 'off' ? 'sleep' : s === 'busy' ? 'type_a' : s === 'ready' ? 'raise' : 'idle', img);
    if (s === 'busy') img.dataset.busy = r.sid;
    if (prev.has(r.sid)) img.style.animation = s === 'talk' ? '' : 'none';
    const b = BADGE[s];
    seats.append(h('div', { class: 'seat ' + s, 'data-sid': r.sid, onclick: () => r.report && present(r.sid) }, img,
      b ? h('span', { class: 'badge', style: 'background:' + b[1], text: b[0] }) : null));
  });
  st.append(scr, seats, h('div', { class: 'table' }));
}
function dialog(m) {
  const box = h('div', { class: 'dialog', id: 'dialog', onclick: () => advance() });
  swipeX(box, dir => (dir > 0 ? back() : advance()));
  box.append(h('span', { class: 'tag sys', id: 'dlg-tag' }), h('div', { class: 'txt', id: 'dlg-txt' }), h('div', { class: 'nav' }, h('span', { id: 'dlg-pg' }), h('span', { class: 'more', id: 'dlg-more', hidden: true, text: '▼' })));
  requestAnimationFrame(() => { if (!talk.sid) sysLine(m); else showPage(true); });
  return box;
}
function sysLine(m) {
  const got = m ? m.roster.filter(r => r.report).length : 0;
  const text = !m ? t('mWelcome') : got && m.roster.every(r => r.report && (m.presented || []).includes(r.sid)) ? t('mDone') : t('mWaiting', got, m.roster.length);
  if (talk.sid === null && talk.text === text && $('#dlg-txt') && $('#dlg-txt').textContent === text) return;
  talk.pages = [{ text, images: [] }]; talk.pi = 0;
  $('#dlg-tag').textContent = t('moderator'); $('#dlg-tag').className = 'tag sys';
  typeOut(text);
}
function pagesOf(rep) {
  const segs = [];
  const head = [rep.title, rep.summary].filter(Boolean).join('\n');
  if (head) segs.push([head, []]);
  rep.items.forEach(it => segs.push([`[${t((ST[it.status] || ST.done)[0])}] ${it.request}` + (it.result ? '\n' + it.result : ''), it.images]));
  if (rep.next) segs.push([t('next') + rep.next, []]);
  const pages = [];
  for (const [text, images] of segs) {  // 대사 창 한 번에 약 120자
    const parts = text.match(/[\s\S]{1,120}(?=\s|$)|[\s\S]{1,120}/g) || [text];
    parts.forEach(p => pages.push({ text: p.trim(), images }));
  }
  return pages;
}
function present(sid) {
  const m = state.meeting;
  const r = m && m.roster.find(x => x.sid === sid);
  if (!r || !r.report) return;
  talk.sid = sid; talk.pages = pagesOf(r.report); talk.pi = 0;
  drawStage(m);
  showPage();
}
function showPage(noType) {
  const m = state.meeting;
  const r = m && m.roster.find(x => x.sid === talk.sid);
  if (!r) { talk.sid = null; return sysLine(m); }
  $('#dlg-tag').textContent = r.name; $('#dlg-tag').className = 'tag';
  $('#dlg-pg').textContent = talk.pages.length > 1 ? `${talk.pi + 1}/${talk.pages.length}` : '';
  drawStage(m);
  if (noType && talk.text === talk.pages[talk.pi].text && talk.n >= talk.text.length) { $('#dlg-txt').textContent = talk.text; $('#dlg-more').hidden = false; return; }
  typeOut(talk.pages[talk.pi].text);
}
function typeOut(text) {
  clearTimeout(talk.timer);
  talk.text = text; talk.n = 0;
  const el = $('#dlg-txt'), more = $('#dlg-more');
  if (!el) return;
  more.hidden = true;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches || typeScale() === 0;
  const step = () => {
    if (!$('#dlg-txt')) return;
    talk.n = reduce ? text.length : talk.n + 1;
    el.textContent = text.slice(0, talk.n);
    if (talk.n >= text.length) { more.hidden = !talk.sid; return; }
    const ch = text[talk.n - 1];
    const base = '.!?。！？\n'.includes(ch) ? 260 : ',，、'.includes(ch) ? 110 : 28;  // 보통 = 초당 약 35자, 문장 끝에서 잠깐
    talk.timer = setTimeout(step, Math.max(1, base * typeScale()));
  };
  step();
}
function back() {
  if (talk.sid && talk.pi > 0) { talk.pi--; showPage(); buzz(); }
}
function advance() {
  if (talk.n < talk.text.length) { clearTimeout(talk.timer); talk.n = talk.text.length; $('#dlg-txt').textContent = talk.text; $('#dlg-more').hidden = !talk.sid; return; }
  if (!talk.sid) return;
  if (talk.pi + 1 < talk.pages.length) { talk.pi++; showPage(); return; }
  const done = talk.sid;
  talk.sid = null;
  api('/api/meeting/presented', { sid: done }).catch(() => {}).then(poll);
  if (state.meeting) { state.meeting.presented = [...(state.meeting.presented || []), done]; autoPresent(state.meeting) || (drawStage(state.meeting), sysLine(state.meeting)); }
}
function autoPresent(m) {
  if (talk.sid) return true;
  const next = m.roster.find(r => r.report && !(m.presented || []).includes(r.sid));
  if (next) { setTimeout(() => { if (!talk.sid && tab === 'meet') present(next.sid); }, 500); return true; }
  return false;
}

const SPEEDS = { slow: 1.6, normal: 1, fast: 0.45, instant: 0 };
function typeSpeed() { const v = localStorage.getItem('co.typeSpeed'); return v in SPEEDS ? v : 'normal'; }
function typeScale() { return SPEEDS[typeSpeed()]; }

// ───────────── 더보기 ─────────────
let past = null;
async function loadPast() {
  try { past = await api('/api/past'); } catch (e) { past = { past: [], cwds: [] }; }
  if (tab === 'more') render(true);
}
function viewMore(v) {
  // 세션 켜기
  const launch = h('div', { class: 'card' }, h('h2', { class: 'title', text: t('launch') }));
  const sel = h('select', {}, (past ? past.cwds : []).map(c => h('option', { value: c, text: c })));
  const nm = h('input', { placeholder: t('sessName'), maxlength: 40 });
  launch.append(h('div', { class: 'small', text: t('newSess') + ' · ' + t('folder') }), sel, h('div', { style: 'height:8px' }), nm,
    h('button', { class: 'btn pri block', style: 'margin-top:10px', text: t('newSess'), disabled: !(past && past.cwds.length), onclick: async e => {
      e.currentTarget.disabled = true;
      try { await api('/api/new', { cwd: sel.value, name: nm.value.trim() }); toast(t('starting')); } catch (er) { toast(t('err', er.message), true); }
      e.currentTarget.disabled = false;
    } }));
  if (!past) launch.append(h('div', { class: 'small', style: 'margin-top:10px', text: t('loading') }));
  else past.past.slice(0, 20).forEach(p => launch.append(h('div', { class: 'row', style: 'border-top:1px solid var(--edge);margin-top:10px;padding-top:10px' },
    h('div', { class: 'grow' }, h('div', { class: 'ell', style: 'font-weight:700', text: p.name }), h('div', { class: 'small ell', text: ago(p.mtime) + ' · ' + p.cwd }),
      p.last ? h('div', { class: 'small ell', text: p.last }) : null),
    h('button', { class: 'btn line', text: t('resume'), onclick: async e => {
      e.currentTarget.disabled = true;
      try { await api('/api/resume', { sid: p.sid }); toast(t('starting')); } catch (er) { toast(t('err', er.message), true); e.currentTarget.disabled = false; }
    } }))));
  v.append(launch);
  // 알림
  const nCard = h('div', { class: 'card' }, h('h2', { class: 'title', text: t('notif') }), h('div', { class: 'text2', style: 'font-size:14px', text: t('notifSub') }));
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const ios = /iPhone|iPad/.test(navigator.userAgent) && !navigator.standalone;
  if (!supported || ios) nCard.append(h('div', { class: 'wait-note', text: t('notifIos') }));
  else if (Notification.permission === 'denied') nCard.append(h('div', { class: 'wait-note', text: t('notifDenied') }));
  else {
    const btns = h('div', { class: 'btns' });
    navigator.serviceWorker.ready.then(reg => reg.pushManager.getSubscription()).then(sub => {
      btns.append(h('button', { class: 'btn ' + (sub ? '' : 'pri'), text: sub ? t('notifOff') : t('notifOn'), onclick: () => (sub ? pushOff() : pushOn()).then(() => render(true)) }));
      if (sub) btns.append(h('button', { class: 'btn line', text: t('notifTest'), onclick: () => api('/api/push', { sub: sub.toJSON(), test: true }).catch(e => toast(t('err', e.message), true)) }));
    });
    nCard.append(btns);
  }
  v.append(nCard);
  // 대사 속도
  const sp = h('div', { class: 'chips' });
  Object.keys(SPEEDS).forEach(k => sp.append(h('button', { class: k === typeSpeed() ? 'on' : '', text: t('sp_' + k), onclick: e => {
    localStorage.setItem('co.typeSpeed', k); sp.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === e.currentTarget)); } })));
  v.append(h('div', { class: 'card' }, h('h2', { class: 'title', text: t('typeSpeed') }), sp));
  // 연결
  v.append(h('div', { class: 'card' }, h('h2', { class: 'title', text: t('conn') }),
    h('div', { class: 'kv' }, h('b', { text: t('pcAddr') }), h('span', { class: 'ell', text: conf.u || '-' })),
    h('div', { class: 'small', style: 'margin-top:8px', text: t('install') }),
    h('button', { class: 'btn block', style: 'margin-top:12px', text: t('forget'), onclick: () => {
      if (!confirm(t('forgetQ'))) return;
      pushOff().finally(() => { localStorage.removeItem('co.conf'); idbPut('conf', null).catch(() => {}); location.reload(); });
    } })));
  v.append(h('div', { class: 'chips', style: 'justify-content:center;margin:6px 0 14px' }, [['ko', '한국어'], ['en', 'English'], ['ja', '日本語'], ['zh', '中文']].map(([c, n]) =>
    h('button', { class: c === LANG ? 'on' : '', text: n, onclick: () => { LANG = c; localStorage.setItem('co.lang', c); buildTabs(); render(); } }))));
}
function urlB64ToBytes(s) {
  const b = atob((s + '='.repeat((4 - s.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(b, c => c.charCodeAt(0));
}
async function pushOn() {
  try {
    if (await Notification.requestPermission() !== 'granted') return toast(t('notifDenied'), true);
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToBytes(conf.vapid) });
    await api('/api/push', { sub: sub.toJSON(), test: true });
  } catch (e) { toast(t('err', e.message), true); }
}
async function pushOff() {
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
    await api('/api/push', { sub: null });
  } catch (e) { /* 연결이 끊겨 있어도 이 기기에서는 끈다 */ }
}

// ───────────── 손가락 조작 ─────────────
function buzz(ms = 8) { try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* 진동이 없는 기기 */ } }
function swipeX(el, onSwipe) {  // 좌우로 넘기기 (dir: 오른쪽으로 넘기면 +1)
  let s = null;
  el.addEventListener('touchstart', e => { s = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() }; }, { passive: true });
  el.addEventListener('touchend', e => {
    if (!s) return;
    const dx = e.changedTouches[0].clientX - s.x, dy = e.changedTouches[0].clientY - s.y;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) { e.preventDefault(); onSwipe(dx > 0 ? 1 : -1); }
    s = null;
  });
}
// 화면을 좌우로 넘기면 옆 탭으로 (손가락을 따라 움직이고, 맨 끝 탭에서는 버티듯 덜 움직인다).
// 맨 위에서 아래로 당기면 새로고침. 가로로 굴리는 목록·입력칸·대사창에서는 끼어들지 않는다.
(function gestures() {
  const view = $('#view'), ptr = $('#ptr');
  let g = null;
  const skip = '.rooms, .thumbs, .chips, .pick, textarea, input, select, .dialog, .seg, .composer';
  document.addEventListener('touchstart', e => {
    if (!online || !state || !$('#sheet').hidden || !$('#drawer').hidden || !$('#viewer').hidden || e.touches.length > 1 || e.target.closest('#tabs, #top') || e.target.closest(skip)) { g = null; return; }
    g = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now(), mode: null, dx: 0, dy: 0, top: window.scrollY <= 0 };
  }, { passive: true });
  document.addEventListener('touchmove', e => {
    if (!g) return;
    g.dx = e.touches[0].clientX - g.x;
    g.dy = e.touches[0].clientY - g.y;
    if (!g.mode && Math.hypot(g.dx, g.dy) > 10) g.mode = Math.abs(g.dx) > Math.abs(g.dy) * 1.2 ? 'x' : (g.top && g.dy > 0 && tab !== 'talk' ? 'pull' : 'y');
    if (g.mode === 'x') {
      e.preventDefault();
      const i = TABS.indexOf(tab), edge = (g.dx > 0 && i === 0) || (g.dx < 0 && i === TABS.length - 1);
      view.style.transition = 'none';
      view.style.transform = `translateX(${g.dx * (edge ? 0.25 : 0.9)}px)`;
      view.style.opacity = String(1 - Math.min(0.35, Math.abs(g.dx) / 900));
    } else if (g.mode === 'pull') {
      e.preventDefault();
      const d = Math.min(90, g.dy * 0.45);
      ptr.style.height = d + 'px';
      ptr.classList.toggle('ready', d > 58);
    }
  }, { passive: false });
  document.addEventListener('touchend', () => {
    if (!g) return;
    const d = g;
    g = null;
    if (d.mode === 'x') {
      const i = TABS.indexOf(tab), next = i + (d.dx < 0 ? 1 : -1);
      const fast = Math.abs(d.dx) / Math.max(1, Date.now() - d.t) > 0.45;
      if ((Math.abs(d.dx) > 80 || (fast && Math.abs(d.dx) > 30)) && next >= 0 && next < TABS.length) {
        view.style.transition = 'transform .16s ease-in, opacity .16s';
        view.style.transform = `translateX(${d.dx < 0 ? -120 : 120}px)`;
        view.style.opacity = '0';
        setTimeout(() => go(TABS[next], d.dx < 0 ? 1 : -1), 140);
      } else {
        view.style.transition = 'transform .25s cubic-bezier(.2,.8,.2,1), opacity .25s';
        view.style.transform = '';
        view.style.opacity = '';
      }
    } else if (d.mode === 'pull') {
      const go_ = ptr.classList.contains('ready');
      ptr.style.transition = 'height .25s';
      ptr.style.height = go_ ? '46px' : '0';
      if (go_) { buzz(); stateSig = ''; poll().finally(() => { ptr.style.height = '0'; ptr.classList.remove('ready'); }); }
      setTimeout(() => { ptr.style.transition = ''; }, 300);
    }
  }, { passive: true });
  // 위쪽·아래쪽 고정 막대 높이를 알려 고정 위치를 맞춘다
  const fit = () => {
    document.documentElement.style.setProperty('--top-h', $('#top').offsetHeight + 'px');
    document.documentElement.style.setProperty('--tabs-h', $('#tabs').offsetHeight + 'px');
  };
  new ResizeObserver(fit).observe($('#top'));
  new ResizeObserver(fit).observe($('#tabs'));
  fit();
})();

// ───────────── 시작 ─────────────
$('#refresh-btn').onclick = () => { stateSig = ''; online ? poll() : connect(); };
$('#menu-btn').onclick = () => { if (online && state) openDrawer(); };
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
if (conf.k) idbPut('conf', conf).catch(() => {});
render();
connect();
