import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type FormEvent, type SetStateAction } from 'react';
import { AlertTriangle, ArrowRight, CalendarDays, CheckCircle2, Clock3, LoaderCircle, Pause, Play, Plus, RefreshCw, RotateCcw, ShieldAlert, X } from 'lucide-react';
import { api, ApiError } from './api';
import type { AccountDetail, AccountStats, SymbolItem, TrainingSession, TrainingSessionCreatePayload } from './types';
import { finite, formatDate, formatMoney, formatPercent } from './utils';

type ReplaySpeed = 1 | 2 | 5;
type PendingAdvance = { sessionId: string; accountId: string; requestId: string; expectedCursor: string | null; createdAt: number };
type PendingCreate = { body: TrainingSessionCreatePayload; createdAt: number };
type PendingSessionMutation = { action: 'restart' | 'finish'; sessionId: string; accountId: string; requestId: string; createdAt: number };

const CREATE_KEY = 'tl-training-pending-create';
const ADVANCE_KEY = (id: string | number) => `tl-training-pending-advance:${String(id)}`;
const SESSION_MUTATION_KEY = (id: string | number) => `tl-training-pending-mutation:${String(id)}`;
const TRAINING_SYMBOLS = new Set(['603283.SH', '515880.SH']);

function parseStored<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) as T : null;
  } catch { return null; }
}

function makeRequestId(): string {
  const id = globalThis.crypto?.randomUUID?.();
  if (!id) throw new Error('当前浏览器无法生成幂等标识，未发送请求。');
  return id;
}

function day(value?: string | null): string | null {
  return value ? value.slice(0, 10) : null;
}

function upsertSession(items: TrainingSession[], next: TrainingSession): TrainingSession[] {
  return [next, ...items.filter((item) => String(item.id) !== String(next.id))];
}

export function ReplayPanel({ sessions, setSessions, activeAccountId, setActiveAccountId, account, selectedSymbol, setSelectedSymbol, symbols, market, accountMutationPending, onAccountUpdated, onRefreshAccount, onPauseReady, onAdvancePending, onSessionMutationPending, onNotice }: {
  sessions: TrainingSession[];
  setSessions: Dispatch<SetStateAction<TrainingSession[]>>;
  activeAccountId: string;
  setActiveAccountId: (id: string) => void;
  account: AccountDetail | null;
  selectedSymbol: string;
  setSelectedSymbol: (symbol: string) => void;
  symbols: SymbolItem[];
  market: { source?: string; adjustment?: string } | null;
  accountMutationPending: boolean;
  onAccountUpdated: (value: AccountDetail) => void;
  onRefreshAccount: (accountId?: string | number) => Promise<void> | void;
  onPauseReady: (pause: () => void) => void;
  onAdvancePending: (pending: boolean) => void;
  onSessionMutationPending: (pending: boolean) => void;
  onNotice: (message: string, tone?: 'success' | 'error' | 'neutral') => void;
}) {
  const session = useMemo(() => sessions.find((item) => String(item.account_id) === activeAccountId) ?? null, [sessions, activeAccountId]);
  const trainingSymbols = useMemo(() => symbols.filter((item) => TRAINING_SYMBOLS.has(item.symbol) && (item.kind === 'stock' || item.kind === 'etf')), [symbols]);
  const defaultCreateSymbol = trainingSymbols.some((item) => item.symbol === selectedSymbol)
    ? selectedSymbol
    : trainingSymbols.find((item) => item.symbol === '603283.SH')?.symbol ?? trainingSymbols[0]?.symbol ?? '';
  const finished = session?.status === 'finished';
  const cursor = day(account?.cursor_date) ?? day(session?.cursor_date);
  const atEnd = Boolean(cursor && session?.max_date && cursor >= day(session.max_date)!);
  const [speed, setSpeed] = useState<ReplaySpeed>(1);
  const [playing, setPlaying] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createSymbol, setCreateSymbol] = useState(defaultCreateSymbol);
  const [createStart, setCreateStart] = useState('');
  const [createCash, setCreateCash] = useState('20000');
  const [createBusy, setCreateBusy] = useState(false);
  const [pendingCreate, setPendingCreate] = useState<PendingCreate | null>(() => parseStored<PendingCreate>(CREATE_KEY));
  const [pendingAdvance, setPendingAdvance] = useState<PendingAdvance | null>(null);
  const [pendingMutation, setPendingMutation] = useState<PendingSessionMutation | null>(null);
  const [sessionMutationBusy, setSessionMutationBusy] = useState(false);
  const [results, setResults] = useState<AccountStats | null>(null);
  const [resultsOpen, setResultsOpen] = useState(false);
  const [resultsBusy, setResultsBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [messageTone, setMessageTone] = useState<'error' | 'neutral' | 'success'>('neutral');
  const [createMessage, setCreateMessage] = useState('');

  const playingRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const inFlightRef = useRef(false);
  const pendingAdvanceRef = useRef<PendingAdvance | null>(null);
  const cursorRef = useRef<string | null>(cursor);
  const sessionRef = useRef<TrainingSession | null>(session);
  const accountIdRef = useRef(activeAccountId);
  const scheduleRef = useRef<() => void>(() => undefined);
  const advanceRef = useRef<(pending?: PendingAdvance) => Promise<boolean>>(async () => false);
  const mountedRef = useRef(true);

  sessionRef.current = session;
  accountIdRef.current = activeAccountId;
  cursorRef.current = cursor;
  pendingAdvanceRef.current = pendingAdvance;

  const pause = useCallback(() => {
    playingRef.current = false;
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
    setPlaying(false);
  }, []);

  useEffect(() => {
    onPauseReady(pause);
    return () => onPauseReady(() => undefined);
  }, [onPauseReady, pause]);

  useEffect(() => {
    onSessionMutationPending(Boolean(session && pendingMutation?.sessionId === String(session.id)));
  }, [onSessionMutationPending, pendingMutation, session?.id]);

  useEffect(() => {
    onAdvancePending(Boolean(session && (advancing || pendingAdvance?.sessionId === String(session.id))));
  }, [onAdvancePending, advancing, pendingAdvance, session?.id]);

  useEffect(() => () => onAdvancePending(false), [onAdvancePending]);

  useEffect(() => {
    mountedRef.current = true;
    const onVisibility = () => { if (document.hidden) pause(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      mountedRef.current = false;
      pause();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [pause]);

  useEffect(() => {
    pause();
  }, [activeAccountId, selectedSymbol, session?.status, pause]);

  useEffect(() => {
    if (session && selectedSymbol !== session.symbol) {
      pause();
      setSelectedSymbol(session.symbol);
    }
  }, [session?.id, session?.symbol, selectedSymbol, setSelectedSymbol, pause]);

  useEffect(() => {
    if (!session) {
      setPendingAdvance(null);
      setPendingMutation(null);
      return;
    }
    const advance = parseStored<PendingAdvance>(ADVANCE_KEY(session.id));
    setPendingAdvance(advance?.sessionId === String(session.id) ? advance : null);
    const mutation = parseStored<PendingSessionMutation>(SESSION_MUTATION_KEY(session.id));
    setPendingMutation(mutation?.sessionId === String(session.id) ? mutation : null);
  }, [session?.id]);

  useEffect(() => {
    if ((pendingAdvance && !inFlightRef.current) || finished || atEnd || accountMutationPending) pause();
  }, [pendingAdvance, finished, atEnd, accountMutationPending, pause]);

  function updateMessage(text: string, tone: 'error' | 'neutral' | 'success' = 'neutral') {
    setMessage(text);
    setMessageTone(tone);
  }

  function savePendingAdvance(value: PendingAdvance): boolean {
    try { localStorage.setItem(ADVANCE_KEY(value.sessionId), JSON.stringify(value)); }
    catch {
      pause();
      updateMessage('无法在本机保存推进请求，未发送这一步。', 'error');
      onNotice('无法在本机保存推进请求，未发送这一步。', 'error');
      return false;
    }
    pendingAdvanceRef.current = value;
    setPendingAdvance(value);
    return true;
  }

  function clearPendingAdvance(sessionId: string | number) {
    try { localStorage.removeItem(ADVANCE_KEY(sessionId)); } catch { /* The in-memory guard still prevents another request. */ }
    if (String(sessionRef.current?.id) === String(sessionId)) {
      pendingAdvanceRef.current = null;
      setPendingAdvance(null);
    }
  }

  async function refreshSession(id: string | number) {
    const fresh = await api.trainingSession(id);
    if (mountedRef.current) setSessions((items) => upsertSession(items, fresh));
    return fresh;
  }

  async function runAdvance(existing?: PendingAdvance): Promise<boolean> {
    const activeSession = sessionRef.current;
    if (!activeSession || inFlightRef.current) return false;
    if (accountMutationPending || pendingMutation?.sessionId === String(activeSession.id)) { pause(); return false; }
    const currentPending = existing ?? pendingAdvanceRef.current;
    if (currentPending) {
      if (String(currentPending.sessionId) !== String(activeSession.id) || String(currentPending.accountId) !== String(activeSession.account_id)) {
        pause();
        updateMessage('待确认请求属于另一份训练，请切换到对应训练后用原请求确认。', 'error');
        return false;
      }
    } else {
      const currentCursor = cursorRef.current;
      if (activeSession.status !== 'active' || (currentCursor && day(activeSession.max_date) && currentCursor >= day(activeSession.max_date)!)) {
        pause();
        return false;
      }
      let id: string;
      try { id = makeRequestId(); }
      catch (error) {
        pause();
        updateMessage(error instanceof Error ? error.message : '无法生成推进幂等标识，未发送请求。', 'error');
        return false;
      }
      const created: PendingAdvance = { sessionId: String(activeSession.id), accountId: String(activeSession.account_id), requestId: id, expectedCursor: currentCursor, createdAt: Date.now() };
      if (!savePendingAdvance(created)) return false;
    }
    const envelope = currentPending ?? pendingAdvanceRef.current;
    if (!envelope || !savePendingAdvance(envelope)) return false;
    inFlightRef.current = true;
    setAdvancing(true);
    updateMessage('正在确认本根结果…');
    try {
      const result = await api.advanceAccount(envelope.accountId, envelope.requestId, envelope.expectedCursor);
      const nextCursor = day(result.cursor_date);
      const previousCursor = day(envelope.expectedCursor);
      if (!nextCursor || (previousCursor && nextCursor <= previousCursor)) {
        clearPendingAdvance(envelope.sessionId);
        pause();
        updateMessage('推进响应没有返回更晚的训练日期，已暂停；请刷新训练进度核对。', 'error');
        await onRefreshAccount(envelope.accountId);
        return false;
      }
      if (mountedRef.current && accountIdRef.current === envelope.accountId) onAccountUpdated(result);
      cursorRef.current = nextCursor;
      if (mountedRef.current) setSessions((items) => items.map((item) => String(item.id) === envelope.sessionId ? { ...item, cursor_date: nextCursor } : item));
      clearPendingAdvance(envelope.sessionId);
      setMessage('');
      const latestSession = sessionRef.current;
      const reachedEnd = latestSession?.max_date ? nextCursor >= day(latestSession.max_date)! : false;
      if (reachedEnd) {
        pause();
        updateMessage('已到训练终点。可结束训练并查看结果。', 'success');
        return false;
      }
      return true;
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      const trainingFinished = status === 409 && error instanceof ApiError && error.code === 'training_finished';
      const isCurrentEnvelope = () => mountedRef.current
        && String(sessionRef.current?.id) === envelope.sessionId
        && String(sessionRef.current?.account_id) === envelope.accountId
        && accountIdRef.current === envelope.accountId;
      if (trainingFinished) {
        if (isCurrentEnvelope()) pause();
        clearPendingAdvance(envelope.sessionId);
        let refreshedSession: TrainingSession | null = null;
        try { refreshedSession = await refreshSession(envelope.sessionId); } catch { /* The server rejection remains definitive; the user can refresh this training again. */ }
        try { await onRefreshAccount(envelope.accountId); } catch { /* Account refresh is best effort after the definitive rejection. */ }
        if (isCurrentEnvelope()) {
          updateMessage(refreshedSession?.status === 'finished'
            ? '训练已在其他页面结束；只读状态已刷新，可以查看结果。'
            : '服务器确认训练已结束；状态刷新失败，请重新选择训练后查看。', refreshedSession?.status === 'finished' ? 'success' : 'error');
        }
        return false;
      }
      const staleCursor = status === 409 && error instanceof ApiError && (error.code === 'training_stale_cursor' || error.message.startsWith('本次推进未提交'));
      if (staleCursor) {
        pause();
        clearPendingAdvance(envelope.sessionId);
        const staleMessage = '本次推进未提交，请刷新训练进度';
        updateMessage(staleMessage, 'error');
        onNotice(staleMessage, 'error');
        try { await refreshSession(envelope.sessionId); } catch { /* Account refresh below still gives a second read path. */ }
        try { await onRefreshAccount(envelope.accountId); } catch { /* The stale state is reported without guessing the new cursor. */ }
        return false;
      }
      if (status >= 400 && status < 500 && status !== 408 && status !== 409) {
        pause();
        clearPendingAdvance(envelope.sessionId);
        const detail = error instanceof Error ? error.message : '服务器已拒绝本次推进。';
        updateMessage(detail, 'error');
        onNotice(detail, 'error');
        return false;
      }
      pause();
      updateMessage(`结果未确认：${error instanceof Error ? error.message : '连接暂时不可用'}。原 UUID 和预期日期已保留；请手动重试同一请求。`, 'error');
      onNotice('推进结果待确认；播放已暂停，请手动重试同一请求。', 'error');
      return false;
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setAdvancing(false);
    }
  }

  advanceRef.current = runAdvance;

  const scheduleNext = useCallback(() => {
    if (!playingRef.current || timerRef.current !== null) return;
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      if (!playingRef.current) return;
      advanceRef.current().then((continuePlayback) => {
        if (continuePlayback && playingRef.current) scheduleRef.current();
        else if (!continuePlayback) pause();
      }).catch(() => pause());
    }, speed * 1000);
  }, [speed, pause]);
  scheduleRef.current = scheduleNext;

  function startPlayback() {
    if (!session || session.status !== 'active' || atEnd || pendingAdvanceRef.current || accountMutationPending || pendingMutation?.sessionId === String(session.id) || inFlightRef.current) return;
    updateMessage('');
    playingRef.current = true;
    setPlaying(true);
    scheduleRef.current();
  }

  function selectSession(id: string) {
    pause();
    onSessionMutationPending(false);
    updateMessage('');
    const next = sessions.find((item) => String(item.id) === id);
    if (!next) return;
    setSelectedSymbol(next.symbol);
    setActiveAccountId(String(next.account_id));
    void refreshSession(next.id).then((fresh) => {
      if (accountIdRef.current === String(fresh.account_id)) updateMessage('训练进度已刷新，播放保持暂停。', 'success');
    }).catch((error: unknown) => {
      if (accountIdRef.current === String(next.account_id)) updateMessage(error instanceof Error ? `无法刷新训练进度：${error.message}` : '无法刷新训练进度。', 'error');
    });
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault();
    if (!createName.trim() || !createSymbol || !createStart || !Number.isFinite(Number(createCash)) || Number(createCash) <= 0) {
      setCreateMessage('请填写训练名称、标的、开始日期和正数初始资金。');
      return;
    }
    if (createStart > new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())) {
      setCreateMessage('开始日期不能晚于今天。');
      return;
    }
    let request_id: string;
    try { request_id = makeRequestId(); }
    catch (error) { setCreateMessage(error instanceof Error ? error.message : '无法生成创建幂等标识。'); return; }
    const body: TrainingSessionCreatePayload = { name: createName.trim(), symbol: createSymbol, start_date: createStart, initial_cash: Number(createCash), request_id };
    const envelope: PendingCreate = { body, createdAt: Date.now() };
    try { localStorage.setItem(CREATE_KEY, JSON.stringify(envelope)); }
    catch { setCreateMessage('无法在本机保存创建请求，未发送。'); return; }
    setPendingCreate(envelope);
    await runCreate(envelope);
  }

  async function runCreate(envelope: PendingCreate | null = pendingCreate) {
    if (!envelope || createBusy) return;
    setCreateBusy(true);
    setCreateMessage('');
    try {
      const created = await api.createTrainingSession(envelope.body);
      setSessions((items) => upsertSession(items, created));
      setSelectedSymbol(created.symbol);
      setActiveAccountId(String(created.account_id));
      try { localStorage.removeItem(CREATE_KEY); } catch { /* The idempotent request remains safe to retry. */ }
      setPendingCreate(null);
      setCreateOpen(false);
      setCreateName(''); setCreateStart(''); setCreateCash('20000');
      onNotice('历史回放训练已创建，训练行情与账户条件由服务器固定。');
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      const detail = error instanceof Error ? error.message : '创建结果尚未确认。';
      if (status >= 400 && status < 500 && status !== 408 && status !== 409) {
        try { localStorage.removeItem(CREATE_KEY); } catch { /* Keep the original UUID in memory. */ }
        setPendingCreate(null);
        setCreateMessage(detail);
      } else setCreateMessage(`创建结果未确认：${detail}。请重试同一创建请求。`);
    } finally { setCreateBusy(false); }
  }

  async function runSessionMutation(envelope: PendingSessionMutation | null = pendingMutation) {
    if (!envelope || sessionMutationBusy) return;
    setSessionMutationBusy(true);
    const activeMutation = () => String(sessionRef.current?.id) === envelope.sessionId;
    if (activeMutation()) onSessionMutationPending(true);
    pause();
    try {
      const result = envelope.action === 'restart'
        ? await api.restartTrainingSession(envelope.sessionId, { request_id: envelope.requestId })
        : await api.finishTrainingSession(envelope.sessionId, { request_id: envelope.requestId });
      setSessions((items) => upsertSession(items, result));
      if (envelope.action === 'restart') {
        if (accountIdRef.current === envelope.accountId) {
          setSelectedSymbol(result.symbol);
          setActiveAccountId(String(result.account_id));
        }
        try { await onRefreshAccount(result.account_id); } catch { /* The session is created; account details reload on selection. */ }
        onNotice('已按原训练条件创建新的独立账本，旧训练保留供复盘。');
      } else onNotice('训练已结束；未平仓持仓保留，结果中的浮动盈亏单独显示。');
      try { localStorage.removeItem(SESSION_MUTATION_KEY(envelope.sessionId)); } catch { /* Keep the same request id available. */ }
      if (String(sessionRef.current?.id) === envelope.sessionId) {
        setPendingMutation(null);
        setMessage('');
      }
      if (activeMutation()) onSessionMutationPending(false);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      const detail = error instanceof Error ? error.message : '训练操作结果尚未确认。';
      const pendingOrders = envelope.action === 'finish' && status === 409 && error instanceof ApiError && (error.code === 'training_pending_orders' || /仍有未处理委托/.test(detail));
      if (pendingOrders) {
        try { localStorage.removeItem(SESSION_MUTATION_KEY(envelope.sessionId)); } catch { /* In-memory release still allows the user to handle orders. */ }
        if (String(sessionRef.current?.id) === envelope.sessionId) {
          setPendingMutation(null);
          updateMessage(detail, 'error');
        }
        if (activeMutation()) onSessionMutationPending(false);
        return;
      }
      if (status >= 400 && status < 500 && status !== 408 && status !== 409) {
        try { localStorage.removeItem(SESSION_MUTATION_KEY(envelope.sessionId)); } catch { /* Keep in-memory retry state. */ }
        if (String(sessionRef.current?.id) === envelope.sessionId) {
          setPendingMutation(null);
          updateMessage(detail, 'error');
        }
        if (activeMutation()) onSessionMutationPending(false);
      } else if (String(sessionRef.current?.id) === envelope.sessionId) updateMessage(`训练操作结果未确认：${detail}。请手动重试相同 UUID。`, 'error');
    } finally { setSessionMutationBusy(false); }
  }

  function beginSessionMutation(action: 'restart' | 'finish') {
    if (!session || pendingMutation) return;
    if (accountMutationPending || advancing || inFlightRef.current || pendingAdvanceRef.current) {
      updateMessage('先确认当前推进结果，再结束或重新训练。', 'error');
      return;
    }
    if (action === 'restart' && !window.confirm('将按原快照、本金、日期和费率创建一个新的独立训练账户；当前账本会保留。继续吗？')) return;
    if (action === 'finish' && !window.confirm('结束后训练账户只读。未平仓持仓会保留，不自动平仓。继续吗？')) return;
    let request_id: string;
    try { request_id = makeRequestId(); }
    catch (error) { updateMessage(error instanceof Error ? error.message : '无法生成幂等标识。', 'error'); return; }
    const envelope: PendingSessionMutation = { action, sessionId: String(session.id), accountId: String(session.account_id), requestId: request_id, createdAt: Date.now() };
    try { localStorage.setItem(SESSION_MUTATION_KEY(session.id), JSON.stringify(envelope)); }
    catch { updateMessage('无法在本机保存训练操作 UUID，未发送请求。', 'error'); return; }
    setPendingMutation(envelope);
    void runSessionMutation(envelope);
  }

  async function loadResults() {
    if (!session || session.status !== 'finished') return;
    setResultsBusy(true);
    try {
      setResults(await api.accountStats(session.account_id));
      setResultsOpen(true);
    } catch (error) { onNotice(error instanceof Error ? error.message : '读取训练结果失败。', 'error'); }
    finally { setResultsBusy(false); }
  }

  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
  const hasPendingMutation = Boolean(pendingMutation);
  const scopeLabel = session ? `${formatDate(session.start_date)} 至 ${formatDate(session.max_date)}` : '选择或创建一份隔离的历史训练';

  return <>
    <section className="panel replay-panel" data-testid="replay-panel">
      <div className="replay-panel-heading">
        <div><div className="eyebrow">HISTORICAL PLAYBACK / TRAINING</div><h2>历史回放训练</h2><p>播放只按顺序提交账户下一交易日；请求结束后才等待下一步，刷新页面不会自动播放。</p></div>
        <button type="button" className="button button-outline button-small" onClick={() => { pause(); setCreateSymbol(defaultCreateSymbol); setCreateOpen(true); }} data-testid="replay-create-open"><Plus size={14} />新建训练</button>
      </div>
      <div className="replay-topline">
        <label className="form-field replay-session-select"><span>训练记录</span><select value={session ? String(session.id) : ''} onChange={(event) => selectSession(event.target.value)} data-testid="replay-session-select"><option value="">选择训练</option>{sessions.map((item) => <option key={item.id} value={String(item.id)}>{item.name} · {item.symbol} · {item.status === 'finished' ? '已结束' : '进行中'}</option>)}</select></label>
        <div className="replay-scope"><CalendarDays size={15} /><span>训练范围</span><b>{scopeLabel}</b></div>
        {session && <div className="replay-scope replay-cursor"><Clock3 size={15} /><span>当前进度</span><b>{cursor ? formatDate(cursor) : '尚未开始'}</b></div>}
      </div>
      {session ? <>
        <div className="replay-session-meta"><span><b>{symbols.find((item) => item.symbol === session.symbol)?.name ?? session.symbol}</b> · <code>{session.symbol}</code></span><span>行情来源：{market?.source ?? '账户冻结快照'}</span><span>复权：{market?.adjustment ?? '以冻结快照为准'}</span><span>终点：{formatDate(session.max_date)}</span><span className={`status-pill ${finished ? 'neutral' : 'good'}`}>{finished ? '只读训练' : '训练中'}</span></div>
        <div className="replay-controls">
          <button type="button" className="button button-outline" onClick={() => void runAdvance()} disabled={!account || advancing || playing || finished || atEnd || Boolean(pendingAdvance) || hasPendingMutation || accountMutationPending} data-testid="replay-next">{advancing ? <LoaderCircle size={15} className="spin" /> : <ArrowRight size={15} />}下一根K线</button>
          <button type="button" className="button button-primary" onClick={startPlayback} disabled={!account || advancing || playing || finished || atEnd || Boolean(pendingAdvance) || hasPendingMutation || accountMutationPending} data-testid="replay-play"><Play size={14} />播放</button>
          <button type="button" className="button button-outline" onClick={pause} disabled={!playing && !advancing} data-testid="replay-pause"><Pause size={14} />暂停</button>
          <label className="replay-speed"><span>速度</span><select value={speed} onChange={(event) => setSpeed(Number(event.target.value) as ReplaySpeed)} aria-label="回放速度" data-testid="replay-speed"><option value="1">1 秒</option><option value="2">2 秒</option><option value="5">5 秒</option></select></label>
          <span className="replay-control-spacer" />
          <button type="button" className="button button-outline button-small" onClick={() => beginSessionMutation('restart')} disabled={sessionMutationBusy || hasPendingMutation || accountMutationPending || advancing || Boolean(pendingAdvance)} data-testid="replay-restart"><RotateCcw size={14} />按原条件重新训练</button>
          {session.status === 'active' && <button type="button" className="button button-danger-quiet button-small" onClick={() => beginSessionMutation('finish')} disabled={sessionMutationBusy || hasPendingMutation || accountMutationPending || advancing || Boolean(pendingAdvance)} data-testid="replay-finish"><CheckCircle2 size={14} />结束训练</button>}
          {finished && <button type="button" className="button button-primary button-small" onClick={loadResults} disabled={resultsBusy} data-testid="replay-results">{resultsBusy ? <LoaderCircle size={14} className="spin" /> : <ArrowRight size={14} />}训练结果</button>}
        </div>
        {playing && <div className="replay-state"><span className="status-light" />按每步响应完成后等待 {speed} 秒</div>}
        {atEnd && !finished && <div className="replay-state replay-state-success"><CheckCircle2 size={14} />已到训练终点，可以结束训练并查看结果。</div>}
      </> : <div className="replay-empty"><ShieldAlert size={16} /><span>训练账本与普通历史模拟账户相互隔离。选择已有训练以继续，或创建新训练后开始回放。</span></div>}
      {pendingAdvance && session && <div className="replay-pending" role="status"><AlertTriangle size={16} /><span><b>推进结果待确认</b><small>预期日期 {pendingAdvance.expectedCursor ? formatDate(pendingAdvance.expectedCursor) : '尚未开始'} · UUID {pendingAdvance.requestId}。播放已停止，重试会复用同一 UUID 和预期日期。</small></span><button type="button" className="button button-outline button-small" onClick={() => void runAdvance(pendingAdvance)} disabled={advancing || hasPendingMutation || accountMutationPending || inFlightRef.current} data-testid="replay-retry-advance">{advancing ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}重试同一请求</button></div>}
      {pendingMutation && session && <div className="replay-pending" role="status"><AlertTriangle size={16} /><span><b>训练操作结果待确认</b><small>原操作和 UUID 已保留；不会自动重发。</small></span><button type="button" className="button button-outline button-small" onClick={() => void runSessionMutation(pendingMutation)} disabled={sessionMutationBusy}>{sessionMutationBusy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}重试同一操作</button></div>}
      {pendingCreate && <div className="replay-pending" role="status"><AlertTriangle size={16} /><span><b>训练创建结果待确认</b><small>{createMessage || '原创建参数和 UUID 已保留；重试会复用原请求。'}</small></span><button type="button" className="button button-outline button-small" onClick={() => void runCreate(pendingCreate)} disabled={createBusy}>{createBusy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}重试原创建</button></div>}
      {message && <div className={`inline-warning ${messageTone === 'error' ? 'error-warning' : ''}`} role="status"><AlertTriangle size={15} /><span>{message}</span></div>}
    </section>

    {createOpen && <div className="modal-backdrop" role="presentation" onClick={() => !createBusy && setCreateOpen(false)}><section className="modal-card replay-create-modal" role="dialog" aria-modal="true" aria-labelledby="replay-create-title" onClick={(event) => event.stopPropagation()}>
      <div className="modal-head"><div><div className="eyebrow">NEW HISTORICAL TRAINING</div><h3 id="replay-create-title">创建历史回放训练</h3></div><button type="button" className="icon-button" onClick={() => setCreateOpen(false)} disabled={createBusy} aria-label="关闭"><X size={17} /></button></div>
      <form className="account-create-form" onSubmit={submitCreate}>
        <label className="form-field"><span>训练名称</span><input value={createName} onChange={(event) => setCreateName(event.target.value)} placeholder="例如：赛腾股份趋势训练" required maxLength={80} disabled={Boolean(pendingCreate)} data-testid="replay-name" /></label>
        <label className="form-field"><span>训练标的</span><select value={createSymbol} onChange={(event) => setCreateSymbol(event.target.value)} required disabled={Boolean(pendingCreate)} data-testid="replay-symbol">{trainingSymbols.map((item) => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}</select></label>
        <div className="form-grid form-grid-two"><label className="form-field"><span>开始日期</span><input type="date" max={today} value={createStart} onChange={(event) => setCreateStart(event.target.value)} required disabled={Boolean(pendingCreate)} data-testid="replay-start" /></label><label className="form-field"><span>初始资金</span><div className="input-prefix"><b>¥</b><input type="number" min="1" step="0.01" value={createCash} onChange={(event) => setCreateCash(event.target.value)} required disabled={Boolean(pendingCreate)} data-testid="replay-cash" /></div></label></div>
        <div className="snapshot-note"><ShieldAlert size={15} /><span>服务器会从已有冻结行情创建独立账本，并记录可用终点。新训练不会读取当前未来行情。</span></div>
        {createMessage && <div className="inline-warning error-warning"><AlertTriangle size={15} /><span>{createMessage}</span></div>}
        <div className="modal-actions"><button type="button" className="button button-quiet" onClick={() => setCreateOpen(false)} disabled={createBusy}>取消</button><button type="submit" className="button button-primary" disabled={createBusy || Boolean(pendingCreate)} data-testid="replay-create-submit">{createBusy ? <LoaderCircle size={15} className="spin" /> : <Plus size={15} />}创建训练</button></div>
      </form>
    </section></div>}

    {resultsOpen && results && session && <div className="modal-backdrop" role="presentation" onClick={() => setResultsOpen(false)}><section className="modal-card replay-results-modal" role="dialog" aria-modal="true" aria-labelledby="replay-results-title" onClick={(event) => event.stopPropagation()}>
      <div className="modal-head"><div><div className="eyebrow">TRAINING RESULTS / CLOSED CYCLES</div><h3 id="replay-results-title">{session.name} · 训练结果</h3></div><button type="button" className="icon-button" onClick={() => setResultsOpen(false)} aria-label="关闭"><X size={17} /></button></div>
      <p className="muted-small">统计仅属于当前训练账户。未平仓持仓保留为浮动盈亏，不计入完整周期胜负。</p>
      <div className="replay-result-grid"><div><span>成交笔数</span><b>{finite(results.totalfills) ? results.totalfills : '—'}</b></div><div><span>完整周期</span><b>{finite(results.totalclosed) ? results.totalclosed : '—'}</b></div><div><span>胜率</span><b>{formatPercent(results.win_rate)}</b></div><div><span>累计净收益</span><b className={finite(results.cumulative_net) ? results.cumulative_net >= 0 ? 'positive' : 'negative' : ''}>{finite(results.cumulative_net) ? `¥${formatMoney(results.cumulative_net)}` : '—'}</b></div><div><span>最大回撤</span><b>{formatPercent(results.max_drawdown)}</b></div><div><span>收益因子</span><b>{finite(results.profit_factor) ? results.profit_factor.toFixed(2) : '—'}</b></div><div><span>未平仓浮动盈亏</span><b className={finite(account?.unrealized_pnl) ? Number(account?.unrealized_pnl) >= 0 ? 'positive' : 'negative' : ''}>{finite(account?.unrealized_pnl) ? `¥${formatMoney(account?.unrealized_pnl)}` : '—'}</b></div></div>
      <div className="modal-actions"><button type="button" className="button button-outline" onClick={() => setResultsOpen(false)}>关闭</button></div>
    </section></div>}
  </>;
}
