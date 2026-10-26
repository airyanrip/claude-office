export type TaskStatus = 'pending' | 'in_progress' | 'completed'
export type Task = {
  id: string
  title: string
  status: TaskStatus
  // 진행중인 단계의 진행률 (0-100)
  percent?: number
}

// 패널 위쪽에 보이는 작업판: 목표와 단계들
export type Board = {
  goal: string
  tasks: Task[]
  note: string
}

// 세션 전체의 상태
export type Phase = 'idle' | 'working' | 'done' | 'paused' | 'aborted' | 'error'

export type AgentRun = {
  // Agent 도구 호출 id
  id: string
  // 서브에이전트 루프 id (시작된 뒤에 알게 된다)
  agentId?: string
  description: string
  type: string
  status: 'run' | 'ok' | 'err'
}

// 협업 폴더의 peer-<세션>.json: 각 세션이 자기 상태를 주기적으로 적는다
export type Peer = {
  sessionId: string
  label: string
  root: string
  goal: string
  tasks: Task[]
  phase: Phase
  request: string
  activity: string
  updatedAt: number
  // 세션이 그려지는 곳 (terminal · desktop · vscode · mobile). 세션 사무실은 세션 목록 파일이 없는 데스크탑 앱 세션을 이것으로 찾는다
  surface?: string
  // 지금 쓰는 모델 (/model 이 보여 주는 이름). 세션 사무실이 보여 준다
  model?: string
}

// 협업 폴더의 out-<세션>.json: 각 세션이 보낸 메시지 (그 세션만 쓴다)
export type Message = {
  id: string
  from: string
  fromLabel: string
  // 'all' 이거나 받는 세션의 이름
  to: string
  kind: 'info' | 'request' | 'question' | 'answer'
  text: string
  at: number
  // 세션 사무실(Claude Office) 대화창에서 사용자가 직접 보낸 명령 (out-office-user.json 에서 읽은 것만)
  isUser?: boolean
}

// 패널에 보이는 주고받은 메시지 한 줄
export type MessageLine = {
  id: string
  isIncoming: boolean
  who: string
  kind: Message['kind']
  text: string
  at: number
}

// 세션 사이에 넘기는 인계 노트 (협업 폴더의 handoff-<id>.json)
export type Handoff = {
  id: string
  title: string
  summary: string
  nextSteps: string[]
  goal: string
  tasks: Task[]
  fromSession: string
  fromLabel: string
  cwd: string
  createdAt: number
  status: 'open' | 'taken'
  takenBy?: string
}

declare module 'claude-code' {
  interface PluginState {
    'session-panel': {
      board: Board
      phase: Phase
      request: string
      activity: string
      agents: AgentRun[]
      agentNotes: Record<string, string>
      inbox: Handoff[]
      sessionId: string
      label: string
      tabTitle: string
      root: string
      startedAt: number
      modelPlans: string[]
      now: number
      peers: Peer[]
      messages: MessageLine[]
      outbox: Message[]
      seen: string[]
      isAutoReply: boolean
      autoReplies: number[]
      source: 'none' | 'progress' | 'tasks'
      taskMap: { id: string; title: string; status: TaskStatus }[]
      sinceReport: number
      nudges: number
    }
  }
}
