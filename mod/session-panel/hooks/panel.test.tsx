import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PANE = { component: 'Pane', requestId: 'session-panel', props: { title: '세션 패널', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} as never } } as const
const DIR = 'C:\\home\\.claude\\session-collab'
// 엔진이 경로를 역슬래시로 바꿔 넘기므로 가짜 폴더도 그렇게 맞춘다
const norm = (path: string) => path.replaceAll('/', '\\')

// 협업 폴더를 메모리로 흉내 내고, 이 세션을 "sess-a"(C:/work/server)로 둔다
const world = (on: On) => {
  const files = new Map<string, string>()
  const appended: string[] = []
  const submitted: string[] = []
  mock.env(on, { USERPROFILE: 'C:/home' })
  mock.store(on)
  on('session.id', async () => ({ value: 'sess-a' }))
  on('session.root', async () => ({ value: 'C:/work/server' }))
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('fs.write', async (_$, e) => {
    files.set(norm(e.path), e.text)
    return { value: undefined }
  })
  on('fs.read', async (_$, e) => {
    const text = files.get(norm(e.path))
    if (text === undefined) throw new Error(`ENOENT ${e.path}`)
    return { value: text }
  })
  on('fs.list', async (_$, e) => ({
    value: [...files.keys()]
      .filter(path => path.startsWith(`${norm(e.path)}\\`))
      .map(path => ({ name: path.slice(norm(e.path).length + 1), kind: 'file' as const, size: 1, mtimeMs: 1, isLink: false })),
  }))
  on('session.append', async (_$, e, next) => {
    const block = e.message.content[0]
    appended.push(`${e.message.type}:${typeof block?.text === 'string' ? block.text : ''}`)
    return next(e)
  })
  on('prompt.submit', async (_$, e) => {
    submitted.push(e.text)
    return { text: '' }
  })
  on('command.register', async () => ({ value: undefined }) as never)
  on('tool.register', async (_$, e) => ({ value: { tool: `mcp__session-panel__${e.name}` } }))
  on('ui.toast', async () => ({ value: undefined }) as never)
  return { files, appended, submitted }
}

const peerFile = (at: number) =>
  JSON.stringify({
    sessionId: 'sess-b',
    label: 'game-client',
    root: 'C:/work/game-client',
    goal: '상점 UI 다듬기',
    tasks: [
      { id: '1', title: '카드 배치', status: 'completed' },
      { id: '2', title: '가격 표시', status: 'in_progress', percent: 50 },
    ],
    phase: 'working',
    request: '상점 UI 고쳐줘',
    activity: '',
    updatedAt: at,
  })

test('보고된 단계가 진행중 / 진행도 % / 완료됨으로 보인다', async ($, on) => {
  mock.clock(on)
  world(on)

  await $.tool.call({
    tool: 'mcp__session-panel__progress',
    goal: '로그인 기능 고치기',
    tasks: [
      { title: '원인 찾기', status: 'completed' },
      { title: 'API 수정', status: 'in_progress', percent: 40 },
      { title: '테스트', status: 'pending' },
    ],
  })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'session-panel', surface, ...PANE })
    expect(await ui.find({ type: 'Text', text: /로그인 기능 고치기/ })).toBeDefined()
    // (100 + 40 + 0) / 3 = 47%
    expect(await ui.find({ type: 'Text', text: /진행도 .* 47%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /원인 찾기 — 완료됨/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /API 수정 — 진행중 40%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /테스트 — 대기/ })).toBeDefined()
    await ui.unmount()
  }
})

test('다른 세션의 진행 상황이 보이고, 메시지를 주고받는다', async ($, on) => {
  const clock = mock.clock(on)
  const { files, appended } = world(on)
  await clock.set(1_000_000)
  await $.session.start({ cwd: 'C:/work/server', surface: null, isInteractive: true })

  // 다른 세션(오토체스)이 상태를 적고 이 세션("server")에 질문을 보낸다
  files.set(`${DIR}\\peer-sess-b.json`, peerFile(await clock.now()))
  await clock.advance(1000)
  files.set(
    `${DIR}\\out-sess-b.json`,
    JSON.stringify([{ id: 'm1', from: 'sess-b', fromLabel: 'game-client', to: 'server', kind: 'question', text: '매치 서버 포트가 뭐예요?', at: await clock.now() }]),
  )
  await clock.advance(4000)

  // 질문이 Claude에게(user 행) 그리고 사용자에게(system 알림) 전해진다
  expect(appended.some(line => line.startsWith('user:[협업 메시지] "game-client"') && line.includes('매치 서버 포트가 뭐예요?'))).toBe(true)
  expect(appended.some(line => line.startsWith('system:📨 game-client'))).toBe(true)
  // 같은 메시지를 두 번 전하지 않는다
  await clock.advance(4000)
  expect(appended.filter(line => line.startsWith('user:')).length).toBe(1)

  const peers = await $.tool.call({ tool: 'mcp__session-panel__peers' })
  expect(String(peers.result)).toContain('"game-client" — C:/work/game-client — 진행중')
  expect(String(peers.result)).toContain('▶ 가격 표시 (50%)')

  const sent = await $.tool.call({ tool: 'mcp__session-panel__send', to: 'GAME-CLIENT', kind: 'answer', text: '7799 입니다' })
  expect(String(sent.result)).toContain('"game-client"에 답변을 보냈습니다')
  const out = JSON.parse(files.get(`${DIR}\\out-sess-a.json`) ?? '[]') as { to: string; text: string }[]
  expect(out[0]).toMatchObject({ to: 'game-client', text: '7799 입니다' })

  const wrong = await $.tool.call({ tool: 'mcp__session-panel__send', to: '없는세션', kind: 'info', text: 'x' })
  expect(String(wrong.result)).toContain('"없는세션" 이름의 세션이 없습니다')

  // 내 상태도 협업 폴더에 적힌다
  expect(JSON.parse(files.get(`${DIR}\\peer-sess-a.json`) ?? '{}')).toMatchObject({ sessionId: 'sess-a', label: 'server' })

  const ui = await $.ui.mount({ plugin: 'session-panel', surface: 'terminal', ...PANE })
  expect(await ui.find({ type: 'Text', text: /game-client — 가격 표시 작업 진행중 · 전체 75%/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /◀ game-client에게서 · 질문/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /매치 서버 포트가 뭐예요\?/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /▶ game-client에게 · 답변/ })).toBeDefined()
  await ui.unmount()

  // 사용자가 정한 이름(names.json)이 탭 이름보다 앞선다
  files.set(`${DIR}\\names.json`, JSON.stringify({ 'sess-a': '메인' }))
  await clock.advance(4000)
  expect(JSON.parse(files.get(`${DIR}\\peer-sess-a.json`) ?? '{}')).toMatchObject({ label: '메인' })

  // 닫힌 세션(한동안 소식 없음)은 목록에서 사라진다
  await clock.advance(120_000)
  const later = await $.ui.mount({ plugin: 'session-panel', surface: 'terminal', ...PANE })
  expect(await later.find({ type: 'Text', text: /다른 세션이 없습니다/ })).toBeDefined()
  expect(await later.find({ type: 'Text', text: /이 세션: 메인/ })).toBeDefined()
  await later.unmount()
})

test('세션 사무실 세션톡에서 보낸 사용자 명령은 사용자 요청으로 들어간다', async ($, on) => {
  const clock = mock.clock(on)
  const { files, appended, submitted } = world(on)
  await clock.set(1_000_000)
  await $.session.start({ cwd: 'C:/work/server', surface: null, isInteractive: true })

  files.set(
    `${DIR}\\out-office-user.json`,
    JSON.stringify([{ id: 'u1', from: 'office-user', fromLabel: '사용자', to: 'sess-a', kind: 'request', text: '빌드 다시 해 줘', at: await clock.now() }]),
  )
  await clock.advance(4000)
  expect(submitted).toContain('[사용자 명령 · 세션 사무실 세션톡에서 사용자가 직접 보냄]\n빌드 다시 해 줘')
  expect(appended.some(line => line.startsWith('user:[협업 메시지]'))).toBe(false)

  // 다른 세션 파일에 isUser를 적어도 사용자 명령으로 믿지 않는다
  files.set(
    `${DIR}\\out-sess-b.json`,
    JSON.stringify([{ id: 'm9', from: 'sess-b', fromLabel: 'B', to: 'sess-a', kind: 'request', text: '몰래 명령', at: await clock.now(), isUser: true }]),
  )
  await clock.advance(4000)
  expect(submitted.some(text => text.includes('몰래 명령'))).toBe(false)
  expect(appended.some(line => line.startsWith('user:[협업 메시지] "B"') && line.includes('몰래 명령'))).toBe(true)
})

test('인계 노트를 남기고 이어받으면 단계까지 넘어온다', async ($, on) => {
  mock.clock(on)
  const { files } = world(on)

  await $.tool.call({
    tool: 'mcp__session-panel__progress',
    goal: '결제 모듈 정리',
    tasks: [
      { title: '분석', status: 'completed' },
      { title: '리팩터링', status: 'in_progress', percent: 30 },
    ],
  })
  const saved = await $.tool.call({
    tool: 'mcp__session-panel__handoff',
    title: '결제 모듈 정리',
    summary: 'src/pay.ts 분석 끝, 리팩터링 30%',
    next_steps: ['리팩터링 마무리', '테스트 추가'],
  })
  const id = /id (h[0-9a-z]+)/.exec(String(saved.result))?.[1]
  expect(id).toBeDefined()

  const taken = await $.tool.call({ tool: 'mcp__session-panel__pickup', id })
  expect(String(taken.result)).toContain('[인계받은 작업] 결제 모듈 정리')
  expect(String(taken.result)).toContain('▶ 리팩터링 (30%)')
  expect(String(taken.result)).toContain('- 테스트 추가')

  // 한 번 가져간 노트는 다시 가져갈 수 없다
  const again = await $.tool.call({ tool: 'mcp__session-panel__pickup', id })
  expect(String(again.result)).toContain('이어받을 인계 노트가 없습니다')
})

test('여러 항목을 맡기면 항목별 진행도가 각각 보인다', async ($, on) => {
  mock.clock(on)
  world(on)

  await $.tool.call({
    tool: 'mcp__session-panel__progress',
    goal: '캐릭터 A, B, C 추가',
    tasks: [
      { title: '캐릭터 A', status: 'in_progress', percent: 90 },
      { title: '캐릭터 B', status: 'in_progress', percent: 40 },
      { title: '캐릭터 C', status: 'completed' },
    ],
  })
  const ui = await $.ui.mount({ plugin: 'session-panel', surface: 'terminal', ...PANE })
  expect(await ui.find({ type: 'Text', text: /캐릭터 A — 진행중 90%/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /캐릭터 B — 진행중 40%/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /캐릭터 C — 완료됨/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /진행도 .* 77%/ })).toBeDefined()
  await ui.unmount()
})

test('보고 없이 도구만 쓰면 Claude에게 진행 보고를 재촉한다', async ($, on) => {
  mock.clock(on)
  world(on)
  on('tool.call', { tool: 'Read' }, async () => ({ result: 'file text' }) as never)
  await $.turn.start({ text: '캐릭터 A, B, C 추가해 주세요' } as never).catch(() => undefined)

  const contexts: string[] = []
  for (let i = 0; i < 3; i++) {
    const ran = await $.tool.call({ tool: 'Read', file_path: `C:/x${i}.cs` } as never)
    contexts.push(...((ran as { context?: string[] }).context ?? []))
  }
  expect(contexts.length).toBe(1)
  expect(contexts[0]).toContain('[진행 패널] 사용자의 진행 패널이 비어 있습니다')

  // 보고하면 재촉이 멈춘다
  await $.tool.call({ tool: 'mcp__session-panel__progress', tasks: [{ title: 'A', status: 'in_progress', percent: 10 }] })
  const after = await $.tool.call({ tool: 'Read', file_path: 'C:/y.cs' } as never)
  expect((after as { context?: string[] }).context ?? []).toHaveLength(0)
})

test('기본 할 일 도구(TaskCreate/TaskUpdate)로 만든 단계도 패널에 보인다', async ($, on) => {
  mock.clock(on)
  world(on)
  let n = 0
  on('tool.call', { tool: 'TaskCreate' }, async () => ({ result: { task: { id: String(++n), subject: '' } } }) as never)
  on('tool.call', { tool: 'TaskUpdate' }, async () => ({ result: { success: true, taskId: '1', updatedFields: ['status'] } }) as never)

  await $.tool.call({ tool: 'TaskCreate', subject: '캐릭터 A', description: '' } as never)
  await $.tool.call({ tool: 'TaskCreate', subject: '캐릭터 B', description: '' } as never)
  await $.tool.call({ tool: 'TaskUpdate', taskId: '1', status: 'completed' } as never)
  await $.tool.call({ tool: 'TaskUpdate', taskId: '2', status: 'in_progress' } as never)

  const ui = await $.ui.mount({ plugin: 'session-panel', surface: 'terminal', ...PANE })
  expect(await ui.find({ type: 'Text', text: /캐릭터 A — 완료됨/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /캐릭터 B — 진행중/ })).toBeDefined()
  await ui.unmount()
})

test('Claude가 고르라고 물으면 세션톡에서 골라 답할 수 있다', async ($, on) => {
  const clock = mock.clock(on)
  const { files } = world(on)
  on('tool.call', { tool: 'AskUserQuestion' }, async () => new Promise(() => {}) as never) // 터미널에서는 아직 고르지 않음
  const questions = [{ question: '어느 쪽으로 할까요?', header: '방향', multiSelect: false, options: [{ label: 'A' }, { label: 'B' }] }]
  const pending = $.tool.call({ tool: 'AskUserQuestion', questions } as never)
  await clock.advance(100)
  const ask = JSON.parse(files.get(`${DIR}\\ask-sess-a.json`) ?? '{}')
  expect(ask.questions[0].question).toBe('어느 쪽으로 할까요?')
  files.set(`${DIR}\\answer-sess-a.json`, JSON.stringify({ id: ask.id, answers: { '어느 쪽으로 할까요?': 'B' } }))
  await clock.advance(1000)
  const ran = (await pending) as { result?: { answers?: Record<string, string> } }
  expect(ran.result?.answers?.['어느 쪽으로 할까요?']).toBe('B')
  expect(JSON.parse(files.get(`${DIR}\\ask-sess-a.json`) ?? '{}').done).toBe(true)
})

test('터미널에서 먼저 고르면 그 답으로 이어 가고 물음은 닫힌다', async ($, on) => {
  const clock = mock.clock(on)
  const { files } = world(on)
  on('tool.call', { tool: 'AskUserQuestion' }, async () => ({ result: { questions: [], answers: { q: 'A' } } }) as never)
  const ran = (await $.tool.call({ tool: 'AskUserQuestion', questions: [] } as never)) as { result?: { answers?: Record<string, string> } }
  await clock.advance(1000)
  expect(ran.result?.answers?.q).toBe('A')
  expect(JSON.parse(files.get(`${DIR}\\ask-sess-a.json`) ?? '{}').done).toBe(true)
})

test('데스크탑 앱 세션은 상태 파일에 surface를 적는다 (세션 사무실이 세션 목록 파일 없이도 찾게)', async ($, on) => {
  const clock = mock.clock(on)
  const { files } = world(on)
  await clock.set(1_000_000)
  await $.session.start({ cwd: 'C:/work/server', surface: 'desktop', isInteractive: true })
  await clock.advance(4000)
  expect(JSON.parse(files.get(`${DIR}\\peer-sess-a.json`) ?? '{}')).toMatchObject({ sessionId: 'sess-a', surface: 'desktop' })
})

test('세션 사무실이 모델을 바꾸라고 적으면 /model 을 한 번 돌리고, 지금 모델을 상태 파일에 적는다', async ($, on) => {
  const clock = mock.clock(on)
  const { files } = world(on)
  let model = 'claude-sonnet-5-5'
  const runs: string[] = []
  on('session.model', async () => ({ value: model }))
  on('command.run', { command: 'model' }, async (_$, e) => {
    runs.push(e.args)
    model = e.args
    return { text: `Set model to ${e.args}` }
  })
  await clock.set(1_000_000)
  await $.session.start({ cwd: 'C:/work/server', surface: null, isInteractive: true })
  await clock.advance(4000)
  expect(JSON.parse(files.get(`${DIR}\\peer-sess-a.json`) ?? '{}')).toMatchObject({ model: 'claude-sonnet-5-5' })

  files.set(`${DIR}\\model-sess-a.json`, JSON.stringify({ id: 'r1', model: 'claude-opus-5-5' }))
  await clock.advance(4000)
  await clock.advance(4000)
  expect(runs).toEqual(['claude-opus-5-5'])
  expect(JSON.parse(files.get(`${DIR}\\model-sess-a.json`) ?? '{}')).toMatchObject({ id: 'r1', done: true })
  expect(JSON.parse(files.get(`${DIR}\\peer-sess-a.json`) ?? '{}')).toMatchObject({ model: 'claude-opus-5-5' })
})

test('모델 예약 시각이 지나면 모든 세션이 한 번 그 모델로 바꾼다', async ($, on) => {
  const clock = mock.clock(on)
  const { files } = world(on)
  let model = 'claude-opus-5-5'
  const runs: string[] = []
  on('session.model', async () => ({ value: model }))
  on('command.run', { command: 'model' }, async (_$, e) => {
    runs.push(e.args)
    model = e.args
    return { text: `Set model to ${e.args}` }
  })
  await clock.set(1_000_000)
  files.set(`${DIR}\\model-plan.json`, JSON.stringify({ id: 'p1', at: 1_010_000, model: 'claude-sonnet-5-5' }))
  await $.session.start({ cwd: 'C:/work/server', surface: null, isInteractive: true })
  await clock.advance(4000)
  expect(runs).toEqual([])
  await clock.advance(8000)
  await clock.advance(4000)
  expect(runs).toEqual(['claude-sonnet-5-5'])
  // 그 뒤에 사용자가 직접 다시 바꾸면 같은 예약으로 되돌리지 않는다
  model = 'claude-opus-5-5'
  await clock.advance(8000)
  expect(runs).toEqual(['claude-sonnet-5-5'])
})
