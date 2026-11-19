import { useEffect, useMemo, useRef, useState, type Dispatch, type FormEvent, type ReactNode, type SetStateAction } from 'react';
import {
  Activity, AlertTriangle, ArrowDownRight, ArrowRight, ArrowUpRight, BookOpenCheck, CalendarDays,
  Check, CheckCircle2, ChevronDown, CircleHelp, Clock3, Database, Download, FileDown, FileText,
  Gauge, History, LayoutDashboard, ListChecks, LoaderCircle, Menu, Moon, PanelLeftClose, Plus,
  RefreshCw, Search, Settings2, ShieldAlert, Sun, Trash2, TrendingUp, Wallet, X,
} from 'lucide-react';
import { api, ApiError } from './api';
import type { EChartsOption } from 'echarts';
import { EChart } from './EChart';
import { KlinePanel } from './KlinePanel';
import { ReplayPanel } from './ReplayPanel';
import type {
  AccountCreatePayload, AccountDetail, AccountStats, AccountSummary, Bootstrap, Fill, LooseRecord, MarketPayload,
  MarketRow, Observation, Order, OrderPayload, PageKey, PlanHistory, PlanPayload, Review, ThemeMode, TradingPlan, TrainingSession,
} from './types';
import {
  REVIEW_QUESTIONS, REVIEW_TAGS, PAGE_TITLES, accountId as getAccountId, curveRows,
  estimateMaxRiskQuantity, fillDate, fillPrice, fillSide, finite, formatDate, formatMoney, formatPercent, formatQuantity, formatUpdatedAt,
  normalizeStatus, objectLabel, orderId, positionName, reviewPayload,
  rowId, statusTone,
} from './utils';

type Toast = { message: string; tone: 'success' | 'error' | 'neutral' };
type ReviewSaveState = 'saved' | 'editing' | 'saving' | 'error';
type LocalReviewDraft = { date: string; answers: string[]; tags: string[]; updatedAt: number; dirty: boolean; completed?: boolean; baseRevision?: number };
type OrderTab = 'positions' | 'orders' | 'fills' | 'cycles';
type PendingAccountMutation = { accountId: string; kind: 'order' | 'advance'; requestId: string; body?: OrderPayload; createdAt: number };
type PendingAccountCreate = { body: AccountCreatePayload; createdAt: number };

const NAV: Array<{ key: PageKey; label: string; icon: typeof LayoutDashboard; group: string }> = [
  { key: 'overview', label: '总览', icon: LayoutDashboard, group: '工作台' },
  { key: 'review', label: '每日复盘', icon: BookOpenCheck, group: '工作台' },
  { key: 'plans', label: '交易计划', icon: ListChecks, group: '工作台' },
  { key: 'simulation', label: '历史模拟', icon: History, group: '研究工具' },
  { key: 'statistics', label: '账户统计', icon: Activity, group: '研究工具' },
  { key: 'data', label: '数据管理', icon: Database, group: '系统' },
  { key: 'settings', label: '设置', icon: Settings2, group: '系统' },
];

function todayISO(): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function reviewDraftStorageKey(accountId: number | string | null | undefined, date: string): string {
  return accountId === undefined || accountId === null ? `tl-review-draft:${date}` : `tl-review-draft:account:${encodeURIComponent(String(accountId))}:${date}`;
}

function reviewDateStorageKey(accountId: number | string | null | undefined): string {
  return accountId === undefined || accountId === null ? 'tl-review-date' : `tl-review-date:account:${encodeURIComponent(String(accountId))}`;
}

function requestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function accountCreateRequestId(): string {
  const value = globalThis.crypto?.randomUUID?.();
  if (!value) throw new Error('当前浏览器无法生成账户创建幂等标识，未发送创建请求。');
  return value;
}

function asString(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function numericInput(value: string): number | null {
  if (!value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function safeRows(value: unknown): LooseRecord[] {
  return Array.isArray(value) ? value.filter((row): row is LooseRecord => Boolean(row) && typeof row === 'object') : [];
}

function displayStreaks(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'number' || typeof value === 'string') return String(value);
  if (Array.isArray(value)) return value.map((item) => typeof item === 'object' && item ? Object.values(item as object).join('：') : String(item)).join(' · ') || '—';
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const type = String(record.current_type ?? record.type ?? '').toLowerCase();
    const length = record.current_length ?? record.length;
    if (type && length !== null && length !== undefined) return `${type.includes('win') || type.includes('胜') ? '连胜' : type.includes('loss') || type.includes('亏') ? '连亏' : type} ${String(length)} 笔`;
    const names: Record<string, string> = { win: '连胜', wins: '连胜', max_win: '最大连胜', max_winning: '最大连胜', loss: '连亏', losses: '连亏', max_loss: '最大连亏', max_losing: '最大连亏' };
    return Object.entries(record).map(([key, item]) => `${names[key] ?? key} ${String(item)}`).join(' · ') || '—';
  }
  return '—';
}

function pushNotice(setToast: (toast: Toast | null) => void, toast: Toast) {
  setToast(toast);
  window.setTimeout(() => setToast(null), 4000);
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function PanelTitle({ eyebrow, title, action }: { eyebrow?: string; title: string; action?: ReactNode }) {
  return <div className="panel-head"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h2>{title}</h2></div>{action}</div>;
}

function EmptyState({ icon: Icon = CircleHelp, title, detail, action }: { icon?: typeof CircleHelp; title: string; detail: string; action?: ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><Icon size={19} /></div><strong>{title}</strong><p>{detail}</p>{action}</div>;
}

function PendingAccountCreateBanner({ message, busy, onRetry }: { message: string; busy: boolean; onRetry: () => void }) {
  return <div className="pending-mutation-banner" role="status"><div><AlertTriangle size={17} /><span><b>账户创建结果待确认</b><small>{message || '原始账户信息和请求标识已保存在本机。不会自动重发。'}</small></span></div><button type="button" className="button button-outline button-small" onClick={onRetry} disabled={busy} data-testid="retry-pending-account-create">{busy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}{busy ? '正在确认…' : '重试同一创建请求'}</button></div>;
}

const PENDING_ACCOUNT_MUTATION_KEY = 'tl-pending-account-mutation-v1';
const PENDING_ACCOUNT_CREATE_KEY = 'tl-pending-account-create-v1';

function isAccountCreatePayload(value: unknown): value is AccountCreatePayload {
  if (!value || typeof value !== 'object') return false;
  const payload = value as Partial<AccountCreatePayload>;
  return typeof payload.name === 'string' && payload.name.trim().length > 0
    && typeof payload.initial_cash === 'number' && Number.isFinite(payload.initial_cash) && payload.initial_cash > 0
    && typeof payload.start_date === 'string' && Boolean(payload.start_date)
    && typeof payload.request_id === 'string' && Boolean(payload.request_id)
    && ['commission_rate', 'minimum_commission', 'stock_stamp_duty_rate', 'stock_transfer_fee_rate']
      .every((key) => payload[key as keyof AccountCreatePayload] === undefined || (typeof payload[key as keyof AccountCreatePayload] === 'number' && Number.isFinite(payload[key as keyof AccountCreatePayload] as number)));
}

function readPendingAccountCreate(): PendingAccountCreate | null {
  try {
    const raw = localStorage.getItem(PENDING_ACCOUNT_CREATE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingAccountCreate>;
    if (!isAccountCreatePayload(value.body)) return null;
    return { body: value.body, createdAt: typeof value.createdAt === 'number' && Number.isFinite(value.createdAt) ? value.createdAt : 0 };
  } catch { return null; }
}

function storePendingAccountCreate(envelope: PendingAccountCreate, setState: (value: PendingAccountCreate) => void): boolean {
  try {
    localStorage.setItem(PENDING_ACCOUNT_CREATE_KEY, JSON.stringify(envelope));
    setState(envelope);
    return true;
  } catch { return false; }
}

function clearPendingAccountCreate(setState: (value: PendingAccountCreate | null) => void): boolean {
  try {
    localStorage.removeItem(PENDING_ACCOUNT_CREATE_KEY);
    setState(null);
    return true;
  } catch { return false; }
}

function readPendingAccountMutation(): PendingAccountMutation | null {
  try {
    const raw = localStorage.getItem(PENDING_ACCOUNT_MUTATION_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as PendingAccountMutation;
    if (typeof value.accountId !== 'string' || typeof value.requestId !== 'string' || !['order', 'advance'].includes(value.kind)) return null;
    if (value.kind === 'order' && (!value.body || value.body.request_id !== value.requestId)) return null;
    return value;
  } catch { return null; }
}

function storePendingMutation(envelope: PendingAccountMutation, setState: (value: PendingAccountMutation) => void): boolean {
  try {
    localStorage.setItem(PENDING_ACCOUNT_MUTATION_KEY, JSON.stringify(envelope));
    setState(envelope);
    return true;
  } catch { return false; }
}

function clearPendingMutation(setState: (value: null) => void) {
  localStorage.removeItem(PENDING_ACCOUNT_MUTATION_KEY);
  setState(null);
}

function App() {
  const [theme, setTheme] = useState<ThemeMode>(() => localStorage.getItem('tl-theme') === 'dark' ? 'dark' : 'light');
  const [page, setPage] = useState<PageKey>('overview');
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [bootError, setBootError] = useState('');
  const [selectedSymbol, setSelectedSymbol] = useState(() => localStorage.getItem('tl-symbol') ?? '');
  const [selectedDate, setSelectedDate] = useState(() => localStorage.getItem('tl-date') ?? todayISO());
  const [market, setMarket] = useState<MarketPayload | null>(null);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState('');
  const [marketErrorKey, setMarketErrorKey] = useState('');
  const [marketIdentityKey, setMarketIdentityKey] = useState('');
  const [marketRefreshKey, setMarketRefreshKey] = useState(0);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [activeAccountId, setActiveAccountId] = useState<string>(() => localStorage.getItem('tl-account-id') ?? '');
  const [account, setAccount] = useState<AccountDetail | null>(null);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountError, setAccountError] = useState('');
  const createdAccountIdRef = useRef<string | null>(null);
  const [stats, setStats] = useState<AccountStats | null>(null);
  const [statsAccountId, setStatsAccountId] = useState('');
  const [statsLoading, setStatsLoading] = useState(false);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [plans, setPlans] = useState<TradingPlan[]>([]);
  const [trainingSessions, setTrainingSessions] = useState<TrainingSession[]>([]);
  const [trainingSessionsLoaded, setTrainingSessionsLoaded] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const replayPauseRef = useRef<() => void>(() => undefined);
  const activeTrainingSession = trainingSessions.find((item) => String(item.account_id) === activeAccountId) ?? null;
  const scopedAccountId = activeTrainingSession?.account_id;

  function navigateToPage(target: PageKey) {
    if (target !== 'simulation') replayPauseRef.current();
    setPage(target);
  }

  function acceptCreatedAccount(value: AccountDetail) {
    const id = String(value.id);
    createdAccountIdRef.current = id;
    setAccounts((items) => [value, ...items.filter((item) => String(item.id) !== id)]);
    setAccount(value);
    setAccountError('');
    setAccountLoading(false);
    setActiveAccountId(id);
  }

  useEffect(() => {
    let live = true;
    api.bootstrap().then((value) => {
      if (!live) return;
      const loaded: Bootstrap = {
        ...value,
        symbols: Array.isArray(value.symbols) ? value.symbols : [],
        observations: Array.isArray(value.observations) ? value.observations : [],
        accounts: Array.isArray(value.accounts) ? value.accounts : [],
        reviews: Array.isArray(value.reviews) ? value.reviews : [],
        plans: Array.isArray(value.plans) ? value.plans : [],
      };
      setBootstrap(loaded);
      setAccounts(loaded.accounts);
      setBootError('');
      const defaultSymbol = loaded.symbols.find((item) => item.symbol === '603283.SH')?.symbol ?? loaded.observations.find((item) => item.symbol === '603283.SH')?.symbol ?? loaded.observations[0]?.symbol ?? loaded.symbols[0]?.symbol ?? '';
      setSelectedSymbol((current) => loaded.symbols.some((item) => item.symbol === current) ? current : defaultSymbol);
      setSelectedDate((current) => localStorage.getItem('tl-date') || loaded.today || current);
      if (loaded.accounts.length && (!activeAccountId || !loaded.accounts.some((item) => String(item.id) === activeAccountId))) setActiveAccountId(String(loaded.accounts[0].id));
    }).catch((error: unknown) => {
      if (live) setBootError(error instanceof Error ? error.message : '无法读取实验室数据');
    });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!bootstrap) return;
    let live = true;
    api.accounts().then((items) => { if (live) setAccounts(Array.isArray(items) ? items : []); }).catch(() => undefined);
    api.trainingSessions().then((items) => { if (live) { setTrainingSessions(Array.isArray(items) ? items : []); setTrainingSessionsLoaded(true); } }).catch(() => { if (live) { setTrainingSessions([]); setTrainingSessionsLoaded(true); } });
    return () => { live = false; };
  }, [bootstrap]);

  useEffect(() => {
    if (!bootstrap || !trainingSessionsLoaded) return;
    let live = true;
    setReviews([]);
    setPlans([]);
    api.reviews('', scopedAccountId).then((items) => { if (live) setReviews(Array.isArray(items) ? items : []); }).catch(() => undefined);
    api.plans(scopedAccountId).then((items) => { if (live) setPlans(Array.isArray(items) ? items : []); }).catch(() => undefined);
    return () => { live = false; };
  }, [bootstrap, scopedAccountId, trainingSessionsLoaded]);

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('tl-theme', theme); }, [theme]);
  useEffect(() => { if (selectedSymbol) localStorage.setItem('tl-symbol', selectedSymbol); }, [selectedSymbol]);
  useEffect(() => { if (selectedDate) localStorage.setItem('tl-date', selectedDate); }, [selectedDate]);
  useEffect(() => { if (activeAccountId) localStorage.setItem('tl-account-id', activeAccountId); }, [activeAccountId]);
  useEffect(() => {
    const navigate = (event: Event) => {
      const target = (event as CustomEvent<PageKey>).detail;
      if (target in PAGE_TITLES) navigateToPage(target);
    };
    window.addEventListener('trading-lab:navigate', navigate);
    return () => window.removeEventListener('trading-lab:navigate', navigate);
  }, []);

  useEffect(() => {
    if (!activeAccountId || !bootstrap) { setAccount(null); return; }
    let live = true;
    setAccountLoading(true);
    setAccountError('');
    api.account(activeAccountId).then((value) => {
      if (!live) return;
      setAccount(value);
      setAccounts((items) => [value, ...items.filter((item) => String(item.id) !== activeAccountId)]);
      if (createdAccountIdRef.current === activeAccountId) createdAccountIdRef.current = null;
      setAccountLoading(false);
    }).catch((error: unknown) => {
      if (!live) return;
      if (createdAccountIdRef.current !== activeAccountId) setAccount(null);
      setAccountError(error instanceof Error ? error.message : '读取模拟账户失败');
      setAccountLoading(false);
    });
    return () => { live = false; };
  }, [activeAccountId, bootstrap]);

  const accountMatchesSelection = Boolean(account && activeAccountId && String(account.id) === activeAccountId);
  const visibleAccount = accountMatchesSelection ? account : null;
  const accountErrorDisplay = accountError && createdAccountIdRef.current === activeAccountId
    ? `账户已创建，详情暂不可读：${accountError}`
    : accountError;
  function refreshAccountDetail(id: string | number = activeAccountId) {
    if (!id) return Promise.resolve();
    return api.account(id).then((value) => {
      if (String(id) === activeAccountId) {
        setAccount(value);
        setAccountError('');
        if (createdAccountIdRef.current === activeAccountId) createdAccountIdRef.current = null;
      }
      setAccounts((items) => [value, ...items.filter((item) => String(item.id) !== String(id))]);
    }).catch((error: unknown) => {
      if (String(id) === activeAccountId) setAccountError(error instanceof Error ? error.message : '读取失败');
    });
  }
  const accountCursor = visibleAccount?.cursor_date ? String(visibleAccount.cursor_date).slice(0, 10) : '';
  const isSimulationPage = page === 'simulation';
  const marketEndDate = isSimulationPage ? accountCursor : selectedDate;
  const marketAccountId = isSimulationPage && accountCursor ? activeAccountId : undefined;
  const requestedMarketKey = isSimulationPage ? `${activeAccountId}|${selectedSymbol}|${accountCursor}` : `${selectedSymbol}|${selectedDate}`;
  const marketMatchesRequest = Boolean(requestedMarketKey && marketIdentityKey === requestedMarketKey);
  const currentMarket = marketMatchesRequest ? market : null;
  const currentMarketError = marketErrorKey === requestedMarketKey ? marketError : '';
  const currentMarketLoading = !marketMatchesRequest || marketLoading;
  const accountMarketReady = Boolean(!isSimulationPage || (accountMatchesSelection && marketMatchesRequest && !currentMarketLoading && currentMarket?.rows?.length));
  useEffect(() => {
    if (!selectedSymbol || !bootstrap) return;
    if (isSimulationPage && !accountCursor) {
      setMarket({ rows: [], warning: '账户尚未推进到交易日，模拟页面不会请求或展示未验证日期的行情。' });
      setMarketIdentityKey(requestedMarketKey);
      setMarketLoading(false);
      setMarketError('');
      setMarketErrorKey('');
      return;
    }
    if (!marketEndDate) return;
    let live = true;
    setMarketLoading(true);
    setMarketError('');
    api.market(selectedSymbol, marketEndDate, marketAccountId).then((value) => {
      if (!live) return;
      setMarket(value);
      setMarketIdentityKey(requestedMarketKey);
      setMarketErrorKey('');
      setMarketLoading(false);
    }).catch((error: unknown) => {
      if (!live) return;
      setMarket({ rows: [] });
      setMarketIdentityKey(requestedMarketKey);
      setMarketError(error instanceof Error ? error.message : '历史行情读取失败');
      setMarketErrorKey(requestedMarketKey);
      setMarketLoading(false);
    });
    return () => { live = false; };
  }, [selectedSymbol, marketEndDate, marketAccountId, bootstrap, isSimulationPage, accountCursor, marketRefreshKey, requestedMarketKey]);

  useEffect(() => {
    if (page !== 'statistics' || !activeAccountId) { setStats(null); setStatsAccountId(''); return; }
    let live = true;
    setStats(null); setStatsLoading(true);
    api.accountStats(activeAccountId).then((value) => {
      if (live) { setStats(value); setStatsAccountId(activeAccountId); setStatsLoading(false); }
    }).catch((error: unknown) => {
      if (live) { setStats(null); setStatsAccountId(activeAccountId); setStatsLoading(false); pushNotice(setToast, { message: error instanceof Error ? error.message : '账户统计读取失败', tone: 'error' }); }
    });
    return () => { live = false; };
  }, [page, activeAccountId]);

  const currentSymbol = bootstrap?.symbols.find((item) => item.symbol === selectedSymbol);
  const marketRows = Array.isArray(currentMarket?.rows) ? (currentMarket.rows as MarketRow[]).filter((row) => !isSimulationPage || !accountCursor || row.date.slice(0, 10) <= accountCursor) : [];
  const latest = marketRows.at(-1);
  const today = bootstrap?.today || todayISO();
  const currentHeaderDate = isSimulationPage ? accountCursor : selectedDate;
  const activeTitle = PAGE_TITLES[page];
  const currentStats = statsAccountId === activeAccountId ? stats : null;
  const sourceText = currentMarket?.source || (currentMarketLoading ? '正在读取' : currentMarketError ? '读取失败' : marketRows.length ? '来源待说明' : '暂无数据');

  function refreshMarket() { setMarketRefreshKey((value) => value + 1); }

  return (
    <div className={`app-shell ${collapsed ? 'sidebar-collapsed' : ''}`} data-testid="trading-lab-app">
      {mobileNavOpen && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
      <aside className={`sidebar ${mobileNavOpen ? 'mobile-open' : ''}`} data-testid="sidebar">
        <div className="brand-row">
          <div className="brand-mark"><span /></div>
          <div className="brand-copy"><b>交易实验室</b><small>TRADING LAB</small></div>
          <button className="sidebar-collapse" title="折叠侧栏" onClick={() => setCollapsed((value) => !value)}><PanelLeftClose size={16} /></button>
          <button className="mobile-close" title="关闭导航" onClick={() => setMobileNavOpen(false)}><X size={18} /></button>
        </div>
        <div className="workspace-pill"><div className="workspace-avatar">研</div><div className="workspace-info"><b>研究工作台</b><span>本地私有空间</span></div><ChevronDown size={15} /></div>
        <nav className="primary-nav" aria-label="主导航">
          {(['工作台', '研究工具', '系统'] as const).map((group) => <div className="nav-group" key={group}>
            <div className="nav-group-label">{group}</div>
            {NAV.filter((item) => item.group === group).map((item) => {
              const Icon = item.icon;
              return <button key={item.key} className={`nav-item ${page === item.key ? 'active' : ''}`} onClick={() => { navigateToPage(item.key); setMobileNavOpen(false); }} data-testid={`nav-${item.key}`}>
                <Icon size={17} strokeWidth={1.8} /><span>{item.label}</span>{item.key === 'simulation' && account && <span className="nav-badge">{account.cursor_date ? '历史' : '待建'}</span>}
              </button>;
            })}
          </div>)}
        </nav>
        <div className="sidebar-bottom">
          <div className="phase-card"><div className="phase-card-top"><span className="phase-dot" /> <span>当前阶段</span><b>01</b></div><strong>复盘 · 计划 · 历史模拟</strong><div className="phase-track"><i /></div><small>研究模式运行中</small></div>
          <a className="old-app-link" href="http://127.0.0.1:8060" target="_blank" rel="noreferrer"><div className="old-app-icon"><TrendingUp size={15} /></div><span><b>旧版研究面板</b><small>打开独立页面</small></span><ArrowRight size={15} /></a>
          <div className="sidebar-user"><div className="user-avatar">实</div><div className="user-label"><b>本地账户</b><small>数据存储在本机</small></div><button className="more-button" onClick={() => navigateToPage('settings')} aria-label="打开设置">···</button></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="topbar-left"><button className="mobile-menu" onClick={() => setMobileNavOpen(true)} aria-label="打开导航"><Menu size={20} /></button><div className="breadcrumb"><span>研究工作台</span><span className="slash">/</span><b>{activeTitle.title}</b></div></div>
          <div className="topbar-tools">
            <label className="header-select symbol-select"><span>标的</span><select aria-label="选择标的" value={selectedSymbol} onChange={(event) => { replayPauseRef.current(); setSelectedSymbol(event.target.value); }} disabled={!bootstrap?.symbols.length || (isSimulationPage && Boolean(activeTrainingSession))} data-testid="global-symbol-select">
              {(bootstrap?.symbols ?? []).map((item) => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}
              {!bootstrap?.symbols.length && <option value="">暂无标的</option>}
            </select><ChevronDown className="select-chevron" size={13} /></label>
            <label className={`header-date ${isSimulationPage ? 'locked' : ''}`}><CalendarDays size={15} /><input type="date" aria-label={isSimulationPage ? '模拟账户当前日期' : '行情截止日期'} value={currentHeaderDate || ''} max={today} disabled={isSimulationPage || !selectedSymbol} onChange={(event) => setSelectedDate(event.target.value)} data-testid="global-date-input" /></label>
            <div className={`source-status ${currentMarketError ? 'error' : currentMarketLoading ? 'loading' : marketRows.length ? 'ready' : ''}`} title={currentMarketError || `历史行情来源：${currentMarket?.source ?? '尚无数据'}`} data-testid="market-source-status">
              <span className="status-light" />
              <span className="source-label">{currentMarketLoading ? '行情读取中' : currentMarketError ? '行情异常' : '历史数据'}</span>
              <b>{sourceText}</b>
            </div>
            <button className="theme-toggle" onClick={() => setTheme((value) => value === 'light' ? 'dark' : 'light')} title={theme === 'light' ? '切换深色模式' : '切换浅色模式'} aria-label={theme === 'light' ? '切换深色模式' : '切换浅色模式'} data-testid="theme-toggle">{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button>
          </div>
        </header>

        {bootError && <div className="global-error"><AlertTriangle size={16} /><span>无法连接实验室 API：{bootError}</span><button onClick={() => window.location.reload()}>重新加载</button></div>}
        {accountError && accountMatchesSelection && page === 'simulation' && <div className="global-error"><AlertTriangle size={16} /><span>{accountErrorDisplay}</span></div>}
        {currentMarketError && <div className="global-error market-error"><AlertTriangle size={16} /><span>行情接口没有返回数据：{currentMarketError}</span><button onClick={refreshMarket}>重试</button></div>}

        <div className="page-container">
          <div className="page-heading"><div><div className="eyebrow">{activeTitle.eyebrow}</div><h1>{activeTitle.title}</h1><p>{activeTitle.description}</p></div>{page === 'simulation' && visibleAccount && <div className="heading-status"><span className="status-light" /> HISTORICAL ONLY</div>}</div>
          {page === 'overview' && <OverviewPage bootstrap={bootstrap} plans={plans} accounts={accounts} market={currentMarket} marketRows={marketRows} latest={latest} selectedSymbol={selectedSymbol} symbolName={currentSymbol?.name ?? selectedSymbol} theme={theme} selectedDate={selectedDate} onNavigate={navigateToPage} observations={bootstrap?.observations ?? []} fills={[]} />}
          {page === 'review' && <ReviewPage key={scopedAccountId === undefined ? 'global-review' : `review-${scopedAccountId}`} accountId={scopedAccountId} minDate={activeTrainingSession?.start_date} maxDate={activeTrainingSession ? (accountCursor || activeTrainingSession.start_date) : today} today={today} reviews={reviews} setReviews={setReviews} page={page} onNotice={(message, tone = 'success') => pushNotice(setToast, { message, tone })} />}
          {page === 'plans' && <PlansPage key={scopedAccountId === undefined ? 'global-plans' : `plans-${scopedAccountId}`} accountId={scopedAccountId} plans={plans} setPlans={setPlans} symbols={bootstrap?.symbols ?? []} selectedSymbol={selectedSymbol} strategyName={bootstrap?.strategy?.name ?? ''} onNotice={(message, tone = 'success') => pushNotice(setToast, { message, tone })} />}
          {page === 'simulation' && <SimulationPage accounts={accounts} setAccounts={setAccounts} trainingSessions={trainingSessions} setTrainingSessions={setTrainingSessions} activeAccountId={activeAccountId} setActiveAccountId={setActiveAccountId} account={visibleAccount} setAccount={setAccount} onAccountCreated={acceptCreatedAccount} loading={accountLoading || Boolean(activeAccountId && !accountMatchesSelection)} accountError={accountMatchesSelection ? accountErrorDisplay : ''} onRefresh={refreshAccountDetail} symbols={bootstrap?.symbols ?? []} selectedSymbol={selectedSymbol} setSelectedSymbol={setSelectedSymbol} symbolName={currentSymbol?.name ?? selectedSymbol} market={currentMarket} marketRows={marketRows} theme={theme} plans={plans} marketReady={accountMarketReady} onPauseReady={(pause) => { replayPauseRef.current = pause; }} onNotice={(message, tone = 'success') => pushNotice(setToast, { message, tone })} />}
          {page === 'statistics' && <StatisticsPage stats={currentStats} loading={statsLoading || statsAccountId !== activeAccountId} account={visibleAccount} accounts={accounts} activeAccountId={activeAccountId} theme={theme} />}
          {page === 'data' && <DataPage symbols={bootstrap?.symbols ?? []} selectedSymbol={selectedSymbol} selectedDate={selectedDate} market={currentMarket} marketRows={marketRows} loading={currentMarketLoading} activeAccountId={activeAccountId} onNotice={(message, tone = 'success') => pushNotice(setToast, { message, tone })} onRefresh={refreshMarket} onSetMarket={setMarket} />}
          {page === 'settings' && <SettingsPage theme={theme} setTheme={setTheme} strategy={bootstrap?.strategy} mode={currentMarket?.mode || '历史研究'} />}
        </div>
        <footer className="app-footer"><span>交易实验室</span><span className="dot-separator" /><span>RESEARCH ONLY</span><span className="footer-spacer" /><span>数据状态以本地接口为准</span></footer>
      </main>
      {toast && <div className={`toast ${toast.tone}`} role="status" data-testid="toast"><span>{toast.tone === 'success' ? <CheckCircle2 size={17} /> : toast.tone === 'error' ? <AlertTriangle size={17} /> : <InfoIcon />}</span>{toast.message}<button aria-label="关闭提示" onClick={() => setToast(null)}><X size={15} /></button></div>}
    </div>
  );
}

export default App;

function InfoIcon() { return <CircleHelp size={17} />; }

function OverviewPage({ bootstrap, plans, accounts, market, marketRows, latest, selectedSymbol, symbolName, theme, selectedDate, onNavigate, observations, fills }: {
  bootstrap: Bootstrap | null; plans: TradingPlan[]; accounts: AccountSummary[]; market: MarketPayload | null; marketRows: MarketRow[]; latest?: MarketRow; selectedSymbol: string; symbolName: string; theme: ThemeMode; selectedDate: string; onNavigate: (key: PageKey) => void; observations: Observation[]; fills: Fill[];
}) {
  const [observationSelection, setObservationSelection] = useState('');
  const names = useMemo(() => new Map((bootstrap?.symbols ?? []).map((item) => [item.symbol, item.name])), [bootstrap?.symbols]);
  const current = marketRows.at(-1);
  const previous = marketRows.at(-2);
  const changePct = current && (current.pre_close ?? previous?.close) ? (current.close / Number(current.pre_close ?? previous?.close) - 1) * 100 : null;
  const activePlans = plans.filter((plan) => !['completed', 'done', 'canceled', 'cancelled'].includes(String(plan.status).toLowerCase()));
  const observation = observations.find((item) => item.symbol === selectedSymbol);
  const observationLevels = observation ? [{ label: '真实持仓参考成本 · 非模拟成交', value: observation.reference_cost, color: '#d49e40' }] : [];
  return <div className="dashboard-grid">
    <div className="dashboard-main">
      <section className="welcome-card panel">
        <div className="welcome-copy"><div className="welcome-kicker"><span className="welcome-kicker-dot" /> 本地研究环境 <span className="welcome-divider">·</span> 历史数据</div><h2>先把每个决策，<br /><em>写清楚。</em></h2><p>用真实行情做记录、计划和历史模拟。每一项收益统计都来自账户账本。</p><div className="welcome-actions"><button className="button button-primary" onClick={() => onNavigate('review')} data-testid="start-review"><BookOpenCheck size={16} /> 开始今日复盘 <ArrowRight size={15} /></button><button className="button button-quiet" onClick={() => onNavigate('plans')}>写一份交易计划</button></div></div>
        <div className="welcome-graphic" aria-hidden="true"><div className="graphic-ring ring-one" /><div className="graphic-ring ring-two" /><div className="graphic-axis axis-x" /><div className="graphic-axis axis-y" /><div className="graphic-line"><i /><i /><i /><i /><i /><i /></div><div className="graphic-candle candle-a"><i /><b /></div><div className="graphic-candle candle-b"><i /><b /></div><div className="graphic-candle candle-c"><i /><b /></div><div className="graphic-candle candle-d"><i /><b /></div><div className="graphic-label label-a">观察</div><div className="graphic-label label-b">计划</div><div className="graphic-label label-c">复盘</div><div className="graphic-orbit"><div className="orbit-dot" /></div><div className="graphic-center"><span>复</span></div></div>
        <div className="welcome-footer"><span><span className="micro-check"><Check size={11} /></span>研究模式</span><span><span className="micro-check"><Check size={11} /></span>历史撮合</span><span><span className="micro-check"><Check size={11} /></span>本地账本</span></div>
      </section>

      <div className="overview-metrics">
        <button className="overview-metric panel" onClick={() => onNavigate('review')}><div className="metric-icon journal"><BookOpenCheck size={17} /></div><div className="metric-title">复盘记录</div><strong>{bootstrap?.reviews.length ?? '—'}<small>篇</small></strong><span className="metric-sub">已保存的个人记录 <ArrowRight size={13} /></span></button>
        <button className="overview-metric panel" onClick={() => onNavigate('plans')}><div className="metric-icon plans"><ListChecks size={17} /></div><div className="metric-title">计划进行中</div><strong>{activePlans.length}<small>项</small></strong><span className="metric-sub">已归档计划 {plans.length - activePlans.length} 项 <ArrowRight size={13} /></span></button>
        <button className="overview-metric panel" onClick={() => onNavigate('simulation')}><div className="metric-icon accounts"><Wallet size={17} /></div><div className="metric-title">历史模拟账户</div><strong>{accounts.length}<small>个</small></strong><span className="metric-sub">模拟账本独立记录 <ArrowRight size={13} /></span></button>
      </div>

      <KlinePanel rows={marketRows} symbol={selectedSymbol} symbolName={symbolName} source={market?.source} adjustment={market?.adjustment} updatedAt={market?.updated_at} warning={market?.warning} theme={theme} endDate={selectedDate} fills={fills} levels={observationLevels} cutoffLabel="全周期历史观察 · 不代表模拟账户的当时可见信息" />

      <section className="panel observe-panel">
        <PanelTitle eyebrow="OBSERVATION / RAW DATA" title="当前行情观察" action={<button className="text-link-button" onClick={() => onNavigate('data')}>管理数据 <ArrowRight size={14} /></button>} />
        <div className="observe-summary">
          <div className="observe-instrument"><div className="ticker-tile">{selectedSymbol.slice(0, 2) || '--'}</div><div><b>{symbolName}</b><span className="mono">{selectedSymbol} · 截至 {formatDate(latest?.date)}</span></div></div>
          <div className="observe-price"><span>最新历史收盘</span><strong>{latest ? `¥${formatMoney(latest.close, priceDigits(latest.close))}` : '—'}</strong></div>
          <div className="observe-day"><span>距参考收盘</span><strong className={typeof changePct === 'number' ? changePct >= 0 ? 'positive' : 'negative' : ''}>{typeof changePct === 'number' ? `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%` : '—'}</strong></div>
          <div className="observe-period"><span>数据来源</span><strong>{market?.source ?? '待同步'}</strong></div>
          <button className="square-link" onClick={() => onNavigate('data')} title="查看行情数据"><ArrowRight size={16} /></button>
        </div>
        <div className="observe-note"><InfoIcon /><span>这是历史原始行情的观察值，未经过模拟账户的时间截断，也不构成实时价格。</span><span>截止日 {formatDate(selectedDate)}</span></div>
      </section>
    </div>

    <aside className="dashboard-side">
      <section className="panel side-card practice-card">
        <PanelTitle eyebrow="TODAY / CHECKLIST" title="今日研究节奏" />
        <div className="practice-flow">
          <button className="flow-step" onClick={() => onNavigate('data')}><span className="flow-number">01</span><span className="flow-copy"><b>确认行情来源</b><small>{market?.source ? `${market.source} · ${marketRows.length} 根日K` : '查看导入与同步状态'}</small></span><ArrowRight size={15} /></button>
          <button className="flow-step" onClick={() => onNavigate('plans')}><span className="flow-number">02</span><span className="flow-copy"><b>写下交易计划</b><small>{activePlans.length ? `${activePlans.length} 项计划待跟踪` : '没有计划记录'}</small></span><ArrowRight size={15} /></button>
          <button className="flow-step" onClick={() => onNavigate('review')}><span className="flow-number">03</span><span className="flow-copy"><b>完成每日复盘</b><small>把判断依据和错误记下来</small></span><ArrowRight size={15} /></button>
        </div>
        <div className="practice-footer"><span className="practice-icon"><Gauge size={15} /></span><span>过程优先于短期收益</span><span className="practice-line" /></div>
      </section>

      <section className="panel side-card holdings-card">
        <PanelTitle eyebrow="PRIVATE PROFILE" title="观察持仓" action={<span className="badge badge-soft">来自本地档案</span>} />
        {observations.length ? <div className="holdings-list">{observations.map((item) => {
          const symbolNameFor = names.get(item.symbol) ?? item.name ?? item.symbol;
          return <button className={`holding-row ${observationSelection === item.symbol ? 'selected' : ''}`} key={`${item.symbol}-${item.quantity}`} onClick={() => setObservationSelection(item.symbol)}>
            <span className="holding-symbol">{item.symbol.slice(0, 2)}</span><span className="holding-main"><b>{symbolNameFor}</b><small className="mono">{item.symbol}</small></span><span className="holding-values"><b>{formatQuantity(item.quantity)} {symbolUnit(item.symbol, bootstrap?.symbols ?? [])}</b><small>参考成本 ¥{formatMoney(item.reference_cost, item.reference_cost < 1 ? 4 : 3)}</small></span><ChevronDown size={14} className="holding-chevron" />
            {observationSelection === item.symbol && <span className="holding-detail">{item.note || '买入日期未载入，暂不纳入模拟周期与收益统计。'}</span>}
          </button>;
        })}</div> : <EmptyState icon={Wallet} title="暂无观察持仓" detail="持仓观察项由本地档案提供；不会由界面预填或推算。" />}
        {observations.length > 0 && <div className="holding-disclaimer"><ShieldAlert size={15} /><span>参考成本尚无买入日期，不并入模拟账户的盈亏与周期。</span></div>}
      </section>

      <section className="strategy-card panel"><div className="strategy-top"><span className="strategy-icon"><Activity size={17} /></span><span className="badge badge-success"><i /> SHADOW ONLY</span></div><div className="eyebrow">CURRENT STRATEGY</div><strong>{bootstrap?.strategy?.name || '策略配置待读取'}</strong><p>{bootstrap?.strategy?.version ? `版本 ${bootstrap.strategy.version}` : '当前没有可展示的策略版本信息。'}</p><button className="strategy-action" onClick={() => onNavigate('settings')}>查看阶段与配置 <ArrowRight size={14} /></button></section>
    </aside>
  </div>;
}

function ReviewPage({ accountId, minDate, maxDate, today, reviews, setReviews, page, onNotice }: { accountId?: number | string; minDate?: string; maxDate?: string; today: string; reviews: Review[]; setReviews: (fn: (items: Review[]) => Review[]) => void; page: PageKey; onNotice: (message: string, tone?: 'success' | 'error' | 'neutral') => void }) {
  const [reviewDate, setReviewDate] = useState(() => {
    const saved = localStorage.getItem(reviewDateStorageKey(accountId));
    if (saved && (!minDate || saved >= minDate) && (!maxDate || saved <= maxDate)) return saved;
    return accountId !== undefined && accountId !== null ? maxDate || todayISO() : saved || todayISO();
  });
  const [answers, setAnswers] = useState<string[]>(Array(7).fill(''));
  const [tags, setTags] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saveState, setSaveState] = useState<ReviewSaveState>('saved');
  const [completed, setCompleted] = useState(false);
  const [draftReady, setDraftReady] = useState(false);
  const [hasUnsyncedDraft, setHasUnsyncedDraft] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const localDraftRef = useRef<LocalReviewDraft>({ date: reviewDate, answers: Array(7).fill(''), tags: [], updatedAt: 0, dirty: false });
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const inFlightSavesRef = useRef(new Map<string, { signature: string; promise: Promise<Review> }>());
  const deletedDatesRef = useRef(new Set<string>());
  const [search, setSearch] = useState('');
  const [archiveQuery, setArchiveQuery] = useState('');
  const [searchLoading, setSearchLoading] = useState(false);
  const [archiveError, setArchiveError] = useState('');
  const reviewsRef = useRef(reviews);
  reviewsRef.current = reviews;

  function enqueueReviewSave(date: string, payload: { answers: string[]; tags: string[]; completed: boolean }): Promise<Review> {
    if (deletedDatesRef.current.has(date)) return Promise.reject(new Error('这条复盘正在删除，已阻止排队中的草稿写回。'));
    const signature = JSON.stringify(payload);
    const pending = inFlightSavesRef.current.get(date);
    if (pending?.signature === signature) return pending.promise;
    const queued = saveQueueRef.current.catch(() => undefined).then(() => {
      if (deletedDatesRef.current.has(date)) throw new Error('这条复盘正在删除，已阻止排队中的草稿写回。');
      return api.saveReview(date, payload, true, accountId);
    });
    inFlightSavesRef.current.set(date, { signature, promise: queued });
    saveQueueRef.current = queued.then(() => undefined, () => undefined);
    queued.then(() => {
      if (inFlightSavesRef.current.get(date)?.promise === queued) inFlightSavesRef.current.delete(date);
    }, () => {
      if (inFlightSavesRef.current.get(date)?.promise === queued) inFlightSavesRef.current.delete(date);
    });
    return queued;
  }

  function acceptSavedReview(saved: Review, date: string, payload: { answers: string[]; tags: string[]; completed: boolean }) {
    setReviews((items) => [saved, ...items.filter((item) => item.date !== saved.date)]);
    const current = localDraftRef.current;
    if (current.date !== date) return;
    const sameDraft = JSON.stringify([current.answers, current.tags, Boolean(current.completed)]) === JSON.stringify([payload.answers, payload.tags, payload.completed]);
    const revision = finite(saved.revision) ? saved.revision : current.baseRevision;
    if (sameDraft) {
      localStorage.removeItem(reviewDraftStorageKey(accountId, date));
      localDraftRef.current = { ...current, dirty: false, baseRevision: revision, updatedAt: Date.now() };
      setDirty(false); setHasUnsyncedDraft(false); setCompleted(payload.completed); setSaveState('saved');
    } else {
      const next = { ...current, baseRevision: revision };
      localDraftRef.current = next;
      try { localStorage.setItem(reviewDraftStorageKey(accountId, date), JSON.stringify(next)); } catch { /* Keep the in-memory copy. */ }
    }
  }

  useEffect(() => { localStorage.setItem(reviewDateStorageKey(accountId), reviewDate); }, [accountId, reviewDate]);
  useEffect(() => {
    setDraftReady(false);
    const saved = reviewsRef.current.find((item) => item.date === reviewDate);
    const serverAnswers = saved ? reviewPayload(saved) : Array(7).fill('');
    const serverTags = saved?.tags ?? [];
    const serverUpdatedAt = saved?.updated_at ? Date.parse(String(saved.updated_at)) : 0;
    const serverRevision = finite(saved?.revision) ? saved.revision : 0;
    let local: LocalReviewDraft | null = null;
    try {
      const raw = localStorage.getItem(reviewDraftStorageKey(accountId, reviewDate));
      if (raw) {
        const parsed = JSON.parse(raw) as LocalReviewDraft;
        if (parsed.date === reviewDate && Array.isArray(parsed.answers) && Array.isArray(parsed.tags)) local = parsed;
      }
    } catch { local = null; }
    const localDataDiffers = Boolean(local && JSON.stringify([local.answers, local.tags, Boolean(local.completed)]) !== JSON.stringify([serverAnswers, serverTags, Boolean(saved?.completed)]));
    const localRevision = local?.baseRevision;
    const revisionShowsLocalIsCurrent = localRevision !== undefined && localRevision >= serverRevision;
    const localIsNewer = Boolean(local && localDataDiffers && (revisionShowsLocalIsCurrent || (localRevision === undefined && local.updatedAt > serverUpdatedAt)));
    const nextAnswers = localIsNewer && local ? Array.from({ length: 7 }, (_, index) => String(local.answers[index] ?? '')) : serverAnswers;
    const nextTags = localIsNewer && local ? local.tags : serverTags;
    const nextCompleted = localIsNewer && local ? Boolean(local.completed) : Boolean(saved?.completed);
    setAnswers(nextAnswers); setTags(nextTags); setCompleted(nextCompleted);
    setDirty(localIsNewer); setHasUnsyncedDraft(localIsNewer); setSaveState(localIsNewer ? 'editing' : 'saved');
    localDraftRef.current = { date: reviewDate, answers: nextAnswers, tags: nextTags, updatedAt: localIsNewer && local ? local.updatedAt : serverUpdatedAt, dirty: localIsNewer, completed: nextCompleted, baseRevision: serverRevision };
    setDraftReady(true);
  }, [reviewDate, accountId]);

  useEffect(() => {
    if (page !== 'review' || !dirty || !draftReady || deleting) return;
    const dateAtSchedule = reviewDate;
    const payload = { answers: answers.slice(), tags: tags.slice(), completed };
    setSaveState('saving');
    const timer = window.setTimeout(() => {
      enqueueReviewSave(dateAtSchedule, payload).then((saved) => acceptSavedReview(saved, dateAtSchedule, payload)).catch(() => {
        if (localDraftRef.current.date === dateAtSchedule) setSaveState('error');
      });
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [answers, tags, completed, dirty, draftReady, deleting, reviewDate, page, setReviews]);

  useEffect(() => () => {
    const draft = localDraftRef.current;
    if (!draft.dirty || deletedDatesRef.current.has(draft.date)) return;
    const payload = { answers: draft.answers, tags: draft.tags, completed: Boolean(draft.completed) };
    enqueueReviewSave(draft.date, payload).then(() => {
      const latest = localDraftRef.current;
      if (latest.date === draft.date && JSON.stringify([latest.answers, latest.tags, Boolean(latest.completed)]) === JSON.stringify([draft.answers, draft.tags, payload.completed])) {
        localStorage.removeItem(reviewDraftStorageKey(accountId, draft.date));
        localDraftRef.current = { ...latest, dirty: false };
      }
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!archiveQuery) return;
    let live = true;
    setSearchLoading(true);
    api.reviews(archiveQuery, accountId).then((items) => { if (live) { setReviews((existing) => mergeReviews(existing, items)); setArchiveError(''); setSearchLoading(false); } }).catch((error: unknown) => { if (live) { setArchiveError(error instanceof Error ? error.message : '搜索复盘失败'); setSearchLoading(false); } });
    return () => { live = false; };
  }, [archiveQuery, setReviews, accountId]);

  function persistLocalDraft(nextAnswers: string[], nextTags: string[], nextCompleted = false) {
    const currentReview = reviewsRef.current.find((item) => item.date === reviewDate);
    const draft: LocalReviewDraft = { date: reviewDate, answers: nextAnswers, tags: nextTags, updatedAt: Date.now(), dirty: true, completed: nextCompleted, baseRevision: finite(currentReview?.revision) ? currentReview.revision : localDraftRef.current.baseRevision ?? 0 };
    localDraftRef.current = draft;
    try { localStorage.setItem(reviewDraftStorageKey(accountId, reviewDate), JSON.stringify(draft)); } catch { /* Keep editing even if browser storage is unavailable. */ }
    setHasUnsyncedDraft(true); setDirty(true); setSaveState('editing');
  }

  function mutateAnswer(index: number, value: string) {
    const next = answers.map((item, position) => position === index ? value : item);
    setAnswers(next); setCompleted(false); persistLocalDraft(next, tags, false);
  }

  function toggleTag(tag: string) {
    const next = tags.includes(tag) ? tags.filter((item) => item !== tag) : [...tags, tag];
    setTags(next); setCompleted(false); persistLocalDraft(answers, next, false);
  }

  async function completeReview() {
    const date = reviewDate;
    const payload = { answers: answers.slice(), tags: tags.slice(), completed: true };
    persistLocalDraft(payload.answers, payload.tags, true);
    setCompleted(true); setSaveState('saving');
    try {
      const saved = await enqueueReviewSave(date, payload);
      acceptSavedReview(saved, date, payload);
      onNotice('复盘已标记完成，内容已保存。');
    } catch (error) { setSaveState('error'); onNotice(error instanceof Error ? error.message : '保存失败，请稍后重试。', 'error'); }
  }

  async function saveNow() {
    const date = reviewDate;
    const payload = { answers: answers.slice(), tags: tags.slice(), completed: false };
    setCompleted(false); persistLocalDraft(payload.answers, payload.tags, false); setSaveState('saving');
    try {
      const saved = await enqueueReviewSave(date, payload);
      acceptSavedReview(saved, date, payload);
      onNotice('复盘草稿已保存。');
    } catch (error) { setSaveState('error'); onNotice(error instanceof Error ? error.message : '保存失败。', 'error'); }
  }

  async function deleteCurrent() {
    const date = reviewDate;
    if (!window.confirm(`确定删除 ${date} 的复盘记录吗？`)) return;
    deletedDatesRef.current.add(date);
    setDeleting(true); setDirty(false);
    localDraftRef.current = { ...localDraftRef.current, dirty: false };
    try {
      await saveQueueRef.current;
      await api.deleteReview(date, accountId);
      setReviews((items) => items.filter((item) => item.date !== date));
      setAnswers(Array(7).fill('')); setTags([]); setCompleted(false); setDirty(false); setHasUnsyncedDraft(false); setSaveState('saved');
      localStorage.removeItem(reviewDraftStorageKey(accountId, date));
      localDraftRef.current = { date, answers: Array(7).fill(''), tags: [], updatedAt: Date.now(), dirty: false, completed: false };
      onNotice('复盘记录已删除。');
    } catch (error) {
      deletedDatesRef.current.delete(date);
      const draft = localDraftRef.current;
      if (draft.date === date) {
        const recovered = { ...draft, dirty: true, updatedAt: Date.now() };
        localDraftRef.current = recovered;
        try { localStorage.setItem(reviewDraftStorageKey(accountId, date), JSON.stringify(recovered)); } catch { /* Keep the in-memory draft. */ }
        setDirty(true); setHasUnsyncedDraft(true); setSaveState('error');
      }
      onNotice(error instanceof Error ? error.message : '删除失败。', 'error');
    }
    finally { setDeleting(false); }
  }

  const currentReview = reviews.find((item) => item.date === reviewDate);
  const stateCopy = saveState === 'saving' ? '正在保存' : saveState === 'error' ? '保存失败' : dirty || saveState === 'editing' ? '编辑中' : completed ? '已完成' : currentReview ? '已保存' : '草稿待填写';
  return <div className="review-layout">
    <div className="review-main">
      <section className="panel review-intro"><div className="review-intro-mark"><BookOpenCheck size={21} /></div><div><div className="eyebrow">DAILY JOURNAL</div><h2>把事实写下来，<em>再评价结果。</em></h2><p>按日期保存。编辑内容会自动保存为草稿；点击完成后才会标记为正式复盘。</p></div><div className={`review-save-indicator ${saveState}`}><span className="save-indicator-dot" />{stateCopy}</div></section>
      <section className="panel review-form-panel">
        {hasUnsyncedDraft && <div className="inline-warning draft-recovery"><AlertTriangle size={15} /><span>发现未同步草稿，已在本机恢复。修改后会继续自动保存到数据库。</span><button onClick={() => { localStorage.removeItem(reviewDraftStorageKey(accountId, reviewDate)); setHasUnsyncedDraft(false); }}>知道了</button></div>}
        <div className="review-date-row"><label className="field-label" htmlFor="review-date">复盘日期</label><input id="review-date" className="input-date" type="date" min={minDate} max={maxDate || today} value={reviewDate} disabled={deleting} onChange={(event) => { setReviewDate(event.target.value); setDirty(false); }} data-testid="review-date" /><span className="review-date-note">每个日期有一份独立记录{accountId !== undefined ? ' · 不晚于训练进度' : ''}</span>{currentReview?.revision !== undefined && <span className="revision-pill">版本 {currentReview.revision}</span>}</div>
        <div className="question-list">{REVIEW_QUESTIONS.map((question, index) => <label className="question-card" key={question}><span className="question-number">{String(index + 1).padStart(2, '0')}</span><span className="question-content"><b>{question}</b><textarea value={answers[index] ?? ''} disabled={deleting} onChange={(event) => mutateAnswer(index, event.target.value)} placeholder={index === 0 ? '写下你观察到的事实与依据…' : '用几句话记录你的判断…'} rows={index === 0 || index === 3 ? 3 : 2} data-testid={`review-answer-${index + 1}`} /></span><span className="question-check">{answers[index]?.trim() ? <CheckCircle2 size={17} /> : <span />}</span></label>)}</div>
        <div className="review-tags-section"><div><b>今日标签</b><small>选择能描述今天执行状态的词</small></div><div className="review-tag-list">{REVIEW_TAGS.map((tag) => <button key={tag} type="button" className={`tag-chip ${tags.includes(tag) ? 'active' : ''}`} onClick={() => toggleTag(tag)} disabled={deleting}>{tags.includes(tag) && <Check size={12} />}{tag}</button>)}</div></div>
        <div className="review-form-footer"><button className="button button-danger-quiet" onClick={deleteCurrent} disabled={!currentReview || deleting} data-testid="delete-review"><Trash2 size={15} /> 删除记录</button><div><span className="autosave-note"><Clock3 size={14} />{saveState === 'saving' ? '自动保存中…' : saveState === 'error' ? '自动保存失败，请手动保存' : '编辑后 2 秒自动保存'}</span><button className="button button-outline" onClick={saveNow} disabled={deleting} data-testid="save-review">保存草稿</button><button className="button button-primary" onClick={completeReview} disabled={deleting} data-testid="complete-review"><CheckCircle2 size={15} /> 完成今日复盘</button></div></div>
      </section>
    </div>
    <aside className="review-side">
      <section className="panel review-progress-panel"><div className="eyebrow">TODAY'S NOTES</div><h3>复盘进度</h3><div className="progress-stat"><strong>{answers.filter((answer) => answer.trim()).length}<span> / 7</span></strong><small>已填写问题</small></div><div className="progress-track"><i style={{ width: `${answers.filter((answer) => answer.trim()).length / 7 * 100}%` }} /></div><div className="progress-foot"><span>{completed ? <><CheckCircle2 size={14} /> 已完成</> : '完成全部问题后标记复盘'}</span><span>{formatDate(reviewDate)}</span></div></section>
      <section className="panel archive-panel"><PanelTitle eyebrow="JOURNAL ARCHIVE" title="历史复盘" /><form className="search-field" onSubmit={(event) => { event.preventDefault(); setArchiveQuery(search.trim()); }}><Search size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索日期或关键词" /><button type="submit">搜索</button></form>{searchLoading && <div className="loading-inline"><LoaderCircle size={14} className="spin" />搜索中</div>}{archiveError && <p className="inline-error">{archiveError}</p>}<div className="archive-list">{reviews.filter((review) => !archiveQuery || review.date.includes(archiveQuery) || review.answers?.some((answer) => answer.toLowerCase().includes(archiveQuery.toLowerCase())) || review.tags?.some((tag) => tag.includes(archiveQuery))).slice(0, 8).map((review) => <button key={review.date} className={`archive-row ${review.date === reviewDate ? 'active' : ''}`} onClick={() => setReviewDate(review.date)}><span className="archive-date"><b>{formatDate(review.date)}</b><small>{review.completed ? '已完成' : '草稿'}</small></span><span className="archive-preview">{review.answers?.find((answer) => answer.trim()) || review.tags?.join(' · ') || '暂无正文'}</span><ArrowRight size={14} /></button>)}{!reviews.length && <EmptyState icon={BookOpenCheck} title="还没有复盘记录" detail="保存草稿后，记录会显示在这里。" />}</div></section>
      <div className="review-tip"><span>i</span><p>好的复盘关注决策过程。单次结果可能受行情波动影响，不等于策略质量。</p></div>
    </aside>
  </div>;
}

function mergeReviews(existing: Review[], incoming: Review[]): Review[] {
  const map = new Map(existing.map((item) => [item.date, item]));
  incoming.forEach((item) => map.set(item.date, item));
  return [...map.values()].sort((a, b) => b.date.localeCompare(a.date));
}

function PlansPage({ accountId, plans, setPlans, symbols, selectedSymbol, strategyName, onNotice }: { accountId?: number | string; plans: TradingPlan[]; setPlans: (fn: (items: TradingPlan[]) => TradingPlan[]) => void; symbols: Bootstrap['symbols']; selectedSymbol: string; strategyName: string; onNotice: (message: string, tone?: 'success' | 'error' | 'neutral') => void }) {
  const [form, setForm] = useState({ symbol: selectedSymbol, strategy: strategyName, market_trend: '', reason: '', entry_condition: '', entry_price: '', stop_price: '', target_price: '', quantity: '', discipline: '', status: 'planned' });
  const [editingId, setEditingId] = useState<string | number | null>(null);
  const [saving, setSaving] = useState(false);
  const [historyPlan, setHistoryPlan] = useState<TradingPlan | null>(null);
  const [historyRows, setHistoryRows] = useState<PlanHistory[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [filter, setFilter] = useState('all');
  useEffect(() => { setForm((value) => ({ ...value, symbol: value.symbol || selectedSymbol, strategy: value.strategy || strategyName })); }, [selectedSymbol, strategyName]);
  const entry = numericInput(form.entry_price);
  const stop = numericInput(form.stop_price);
  const target = numericInput(form.target_price);
  const quantity = numericInput(form.quantity);
  const risk = entry !== null && stop !== null && quantity !== null ? (entry - stop) * quantity : null;
  const potential = entry !== null && target !== null && quantity !== null ? (target - entry) * quantity : null;
  const ratio = risk && potential !== null ? potential / risk : null;
  const riskInvalid = entry !== null && stop !== null && entry <= stop;
  const filteredPlans = plans.filter((plan) => filter === 'all' || String(plan.status).toLowerCase() === filter);
  const planPayload: PlanPayload = { symbol: form.symbol, strategy: form.strategy, market_trend: form.market_trend, reason: form.reason, entry_condition: form.entry_condition, entry_price: entry, stop_price: stop, target_price: target, quantity, discipline: form.discipline, status: form.status };

  function setField(field: keyof typeof form, value: string) { setForm((state) => ({ ...state, [field]: value })); }
  function resetForm() { setEditingId(null); setForm({ symbol: selectedSymbol, strategy: strategyName, market_trend: '', reason: '', entry_condition: '', entry_price: '', stop_price: '', target_price: '', quantity: '', discipline: '', status: 'planned' }); }

  async function submitPlan(event: FormEvent) {
    event.preventDefault();
    if (!form.symbol || !form.reason.trim() || !form.entry_condition.trim()) { onNotice('请先填写标的、计划理由和入场条件。', 'error'); return; }
    if ((form.entry_price && entry === null) || (form.stop_price && stop === null) || (form.target_price && target === null) || (form.quantity && quantity === null)) { onNotice('价格和数量需要填写有效数字。', 'error'); return; }
    setSaving(true);
    try {
      if (editingId !== null) {
        const saved = await api.updatePlan(editingId, planPayload, accountId);
        setPlans((items) => [saved, ...items.filter((item) => String(item.id) !== String(editingId))]);
        onNotice('交易计划已更新。');
      } else {
        const saved = await api.createPlan(planPayload, accountId);
        setPlans((items) => [saved, ...items.filter((item) => String(item.id) !== String(saved.id))]);
        onNotice('交易计划已保存。');
      }
      resetForm();
    } catch (error) { onNotice(error instanceof Error ? error.message : '保存计划失败。', 'error'); }
    finally { setSaving(false); }
  }

  function editPlan(plan: TradingPlan) {
    setEditingId(plan.id ?? null);
    setForm({ symbol: plan.symbol, strategy: plan.strategy ?? '', market_trend: plan.market_trend ?? '', reason: plan.reason ?? '', entry_condition: plan.entry_condition ?? '', entry_price: asString(plan.entry_price), stop_price: asString(plan.stop_price), target_price: asString(plan.target_price), quantity: asString(plan.quantity), discipline: plan.discipline ?? '', status: plan.status ?? 'planned' });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function removePlan(plan: TradingPlan) {
    if (!plan.id || !window.confirm(`确定删除 ${plan.symbol} 计划吗？`)) return;
    try { await api.deletePlan(plan.id, accountId); setPlans((items) => items.filter((item) => String(item.id) !== String(plan.id))); onNotice('交易计划已删除。'); }
    catch (error) { onNotice(error instanceof Error ? error.message : '删除计划失败。', 'error'); }
  }

  async function showHistory(plan: TradingPlan) {
    if (!plan.id) return;
    setHistoryPlan(plan); setHistoryLoading(true); setHistoryRows([]);
    try { const rows = await api.planHistory(plan.id, accountId); setHistoryRows(Array.isArray(rows) ? rows : []); }
    catch (error) { onNotice(error instanceof Error ? error.message : '读取计划历史失败。', 'error'); }
    finally { setHistoryLoading(false); }
  }

  return <div className="plans-layout">
    <section className="panel plan-create-panel"><div className="plan-create-header"><div className="plan-form-mark"><ListChecks size={19} /></div><div><div className="eyebrow">NEW PLAN / PRE-TRADE</div><h2>{editingId ? '编辑交易计划' : '先写计划，再等信号'}</h2><p>记录条件、理由与退出位置。此表单只保存研究计划，不会提交模拟订单。</p></div>{editingId !== null && <button className="button button-quiet" onClick={resetForm}>取消编辑</button>}</div>
      <form className="plan-form" onSubmit={submitPlan}>
        <div className="form-grid form-grid-three"><label className="form-field"><span>标的 <i>*</i></span><select value={form.symbol} onChange={(event) => setField('symbol', event.target.value)} required data-testid="plan-symbol">{symbols.map((item) => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}</select></label><label className="form-field"><span>策略</span><input value={form.strategy} onChange={(event) => setField('strategy', event.target.value)} placeholder="策略名称或版本" data-testid="plan-strategy" /></label><label className="form-field"><span>市场趋势</span><select value={form.market_trend} onChange={(event) => setField('market_trend', event.target.value)}><option value="">选择判断</option><option value="上涨">上涨</option><option value="震荡">震荡</option><option value="下跌">下跌</option><option value="不明">暂不判断</option></select></label></div>
        <label className="form-field"><span>计划理由 <i>*</i></span><textarea value={form.reason} onChange={(event) => setField('reason', event.target.value)} placeholder="写下触发本计划的事实、数据或策略规则…" rows={3} required data-testid="plan-reason" /></label>
        <label className="form-field"><span>入场条件 <i>*</i></span><textarea value={form.entry_condition} onChange={(event) => setField('entry_condition', event.target.value)} placeholder="描述可以被检查的入场条件…" rows={2} required data-testid="plan-entry-condition" /></label>
        <div className="form-grid form-grid-four"><label className="form-field"><span>计划入场价</span><div className="input-prefix"><b>¥</b><input type="number" step="0.001" min="0" value={form.entry_price} onChange={(event) => setField('entry_price', event.target.value)} placeholder="选填" /></div></label><label className="form-field"><span>止损价</span><div className="input-prefix"><b>¥</b><input type="number" step="0.001" min="0" value={form.stop_price} onChange={(event) => setField('stop_price', event.target.value)} placeholder="选填" /></div></label><label className="form-field"><span>目标价</span><div className="input-prefix"><b>¥</b><input type="number" step="0.001" min="0" value={form.target_price} onChange={(event) => setField('target_price', event.target.value)} placeholder="选填" /></div></label><label className="form-field"><span>计划数量</span><div className="input-suffix"><input type="number" step="100" min="0" value={form.quantity} onChange={(event) => setField('quantity', event.target.value)} placeholder="选填" /><b>股</b></div></label></div>
        <label className="form-field"><span>纪律要求</span><textarea value={form.discipline} onChange={(event) => setField('discipline', event.target.value)} placeholder="例如：只在收盘确认后评估，单日不追单…" rows={2} /></label>
        <div className="plan-risk-preview"><div className="risk-preview-title"><ShieldAlert size={16} /><span>风险预览</span><small>按做多方向算术估算，不代表结果承诺</small></div><div className="risk-preview-metrics"><div><span>止损价差 × 数量</span><b className={riskInvalid ? 'negative' : ''}>{risk !== null && !riskInvalid ? `¥${formatMoney(risk)}` : riskInvalid ? '请检查止损价' : '—'}</b></div><div><span>目标价差 × 数量</span><b>{potential !== null && potential > 0 ? `¥${formatMoney(potential)}` : '—'}</b></div><div><span>目标 / 风险 比</span><b>{finite(ratio) && ratio > 0 ? `${ratio.toFixed(2)} R` : '—'}</b></div></div></div>
        <div className="plan-form-footer"><label className="form-field status-field"><span>计划状态</span><select value={form.status} onChange={(event) => setField('status', event.target.value)}><option value="planned">计划中</option><option value="active">跟踪中</option><option value="completed">已完成</option><option value="canceled">已取消</option></select></label><button className="button button-primary" type="submit" disabled={saving} data-testid="save-plan">{saving ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{editingId ? '保存修改' : '保存交易计划'}</button></div>
      </form>
    </section>
    <section className="panel plans-list-panel"><div className="panel-head"><div><div className="eyebrow">PLAN REGISTER / {plans.length.toString().padStart(2, '0')}</div><h2>计划台账</h2></div><div className="filter-tabs"><button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部</button><button className={filter === 'planned' ? 'active' : ''} onClick={() => setFilter('planned')}>计划中</button><button className={filter === 'active' ? 'active' : ''} onClick={() => setFilter('active')}>跟踪中</button></div></div>
      {filteredPlans.length ? <div className="plans-table-wrap"><table className="data-table plans-table"><thead><tr><th>标的 / 策略</th><th>判断与条件</th><th>价格区间</th><th>状态</th><th /></tr></thead><tbody>{filteredPlans.map((plan, index) => <tr key={plan.id ?? `${plan.symbol}-${index}`}><td><div className="table-symbol"><b>{symbols.find((item) => item.symbol === plan.symbol)?.name ?? plan.symbol}</b><small className="mono">{plan.symbol} · {plan.strategy || '未指定策略'}</small></div></td><td><div className="table-reason"><b>{plan.market_trend || '未记录趋势'}</b><span>{plan.entry_condition || plan.reason}</span></div></td><td><div className="price-range"><span>入 ¥{formatMoney(plan.entry_price)}</span><span>止 ¥{formatMoney(plan.stop_price)}</span><span>目 ¥{formatMoney(plan.target_price)}</span></div></td><td><span className={`status-pill ${statusTone(String(plan.status))}`}>{normalizeStatus(String(plan.status))}</span></td><td><div className="row-actions"><button className="row-action-button" title="查看计划历史" onClick={() => showHistory(plan)}><Clock3 size={14} /></button><button className="row-action-button" title="编辑计划" onClick={() => editPlan(plan)}><Settings2 size={14} /></button><button className="row-action-button danger" title="删除计划" onClick={() => removePlan(plan)}><Trash2 size={14} /></button></div></td></tr>)}</tbody></table></div> : <EmptyState icon={ListChecks} title="还没有交易计划" detail="先写清理由、条件和风险位置，再等待历史行情验证。" />}
    </section>
    {historyPlan && <div className="modal-backdrop" role="presentation" onClick={() => setHistoryPlan(null)}><section className="modal-card history-modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}><div className="modal-head"><div><div className="eyebrow">PLAN HISTORY</div><h3>{historyPlan.symbol} · 计划变更</h3></div><button className="icon-button" onClick={() => setHistoryPlan(null)}><X size={17} /></button></div>{historyLoading ? <div className="loading-panel"><LoaderCircle className="spin" />读取计划历史…</div> : historyRows.length ? <div className="history-list">{historyRows.map((row, index) => <div className="history-item" key={row.id ?? index}><span className="history-dot" /><div><b>{row.event ?? '计划记录'}</b><p>{row.detail ?? '历史内容由后端记录提供。'}</p><small>{row.created_at ?? '时间未提供'}</small></div></div>)}</div> : <EmptyState icon={Clock3} title="暂无历史变更" detail="后续对计划的修改会显示在这里。" />}</section></div>}
  </div>;
}

function SimulationPage({ accounts, setAccounts, trainingSessions, setTrainingSessions, activeAccountId, setActiveAccountId, account, setAccount, onAccountCreated, loading, accountError, onRefresh, symbols, selectedSymbol, setSelectedSymbol, symbolName, market, marketRows, theme, plans, marketReady, onPauseReady, onNotice }: {
  accounts: AccountSummary[]; setAccounts: Dispatch<SetStateAction<AccountSummary[]>>; trainingSessions: TrainingSession[]; setTrainingSessions: Dispatch<SetStateAction<TrainingSession[]>>; activeAccountId: string; setActiveAccountId: (id: string) => void; account: AccountDetail | null; setAccount: (account: AccountDetail | null) => void; onAccountCreated: (account: AccountDetail) => void; loading: boolean; accountError: string; onRefresh: (accountId?: string | number) => Promise<void> | void; symbols: Bootstrap['symbols']; selectedSymbol: string; setSelectedSymbol: (symbol: string) => void; symbolName: string; market: MarketPayload | null; marketRows: MarketRow[]; theme: ThemeMode; plans: TradingPlan[]; marketReady: boolean; onPauseReady: (pause: () => void) => void; onNotice: (message: string, tone?: 'success' | 'error' | 'neutral') => void;
}) {
  const [tab, setTab] = useState<OrderTab>('positions');
  const [creatingAccount, setCreatingAccount] = useState(false);
  const [accountName, setAccountName] = useState('');
  const [initialCash, setInitialCash] = useState('');
  const [startDate, setStartDate] = useState(todayISO());
  const [advancedFees, setAdvancedFees] = useState(false);
  const [fees, setFees] = useState({ commission_rate: '0.00025', minimum_commission: '5', stock_stamp_duty_rate: '0.0005', stock_transfer_fee_rate: '0.00001' });
  const [creating, setCreating] = useState(false);
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY');
  const [orderQuantity, setOrderQuantity] = useState('');
  const [orderPrice, setOrderPrice] = useState('');
  const [stopPrice, setStopPrice] = useState('');
  const [riskPct, setRiskPct] = useState('1');
  const [orderPlan, setOrderPlan] = useState('');
  const [placingOrder, setPlacingOrder] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [actionError, setActionError] = useState('');
  const [canceling, setCanceling] = useState<string | null>(null);
  const [pendingMutation, setPendingMutation] = useState<PendingAccountMutation | null>(() => readPendingAccountMutation());
  const [pendingMutationBusy, setPendingMutationBusy] = useState(false);
  const [pendingMutationMessage, setPendingMutationMessage] = useState('');
  const [pendingAccountCreate, setPendingAccountCreate] = useState<PendingAccountCreate | null>(() => readPendingAccountCreate());
  const [pendingAccountCreateMessage, setPendingAccountCreateMessage] = useState('');
  const [trainingMutationPending, setTrainingMutationPending] = useState(false);
  const [replayAdvancePending, setReplayAdvancePending] = useState(false);
  const accountCreateInFlightRef = useRef(false);
  const replayPauseRef = useRef<() => void>(() => undefined);
  const trainingSession = trainingSessions.find((item) => String(item.account_id) === activeAccountId) ?? null;
  const trainingReadOnly = trainingSession?.status === 'finished';
  const activeAccountIsListed = accounts.some((item) => String(item.id) === activeAccountId);
  const activeAccountLoadingLabel = account
    ? `${account.name} · 已选`
    : trainingSession
      ? `${trainingSession.name} · ${loading ? '训练账本正在读取' : '训练账本详情暂不可用'}`
      : `账本 ${activeAccountId} · ${loading ? '正在读取' : '当前未出现在列表'}`;

  const selectedPlans = plans.filter((plan) => plan.symbol === selectedSymbol && !['completed', 'done', 'canceled', 'cancelled'].includes(String(plan.status).toLowerCase()));
  const selectedPosition = account?.positions?.find((position) => position.symbol === selectedSymbol);
  const price = numericInput(orderPrice);
  const qty = numericInput(orderQuantity);
  const stop = numericInput(stopPrice);
  const riskPercent = numericInput(riskPct);
  const priceRisk = price !== null && stop !== null && qty !== null ? (side === 'BUY' ? Math.max(price - stop, 0) : Math.max(stop - price, 0)) * qty : null;
  const riskBudget = finite(account?.equity) && finite(account?.cash) && riskPercent !== null && riskPercent > 0 ? Math.min(account.equity, account.cash) * riskPercent / 100 : null;
  const orderValue = price !== null && qty !== null ? price * qty : null;
  const symbolKind = symbols.find((item) => item.symbol === selectedSymbol)?.kind?.toLowerCase() ?? '';
  const tickSize = symbolKind.includes('etf') || symbolKind.includes('基金') ? 0.001 : 0.01;
  const securityIsStock = !symbolKind.includes('etf') && !symbolKind.includes('基金') && !symbolKind.includes('index') && !symbolKind.includes('指数');
  const feeSettings = account ? feeSchedule(account) : { commissionRate: 0.00025, minimumCommission: 5, stampDutyRate: 0.0005, transferFeeRate: 0.00001 };
  const estimatedRisk = priceRisk !== null && price !== null && stop !== null && qty !== null ? priceRisk + estimateRoundTripFees(price, stop, qty, side, feeSettings, securityIsStock) : null;
  const riskBudgetBase = finite(account?.equity) && finite(account?.cash) ? Math.min(account.equity, account.cash) : null;
  const availableCash = finite(account?.available_cash) ? Math.max(0, account.available_cash) : finite(account?.cash) ? Math.max(0, account.cash) : null;
  const maxAffordableBuyQuantity = side === 'BUY' && riskBudgetBase !== null && availableCash !== null && riskPercent !== null && price !== null && stop !== null ? estimateMaxRiskQuantity(price, stop, riskBudgetBase * riskPercent / 100, availableCash, feeSettings, securityIsStock) : null;
  const unit = symbolKind.includes('etf') || symbolKind.includes('基金') ? '份' : '股';
  
  function updateAccountData(value: AccountDetail | null) {
    setAccount(value);
    if (value) setAccounts((items) => [value, ...items.filter((item) => String(item.id) !== String(value.id))]);
  }

  async function runPendingAccountCreate(envelope: PendingAccountCreate) {
    if (accountCreateInFlightRef.current) return;
    accountCreateInFlightRef.current = true;
    setCreating(true);
    setActionError('');
    setPendingAccountCreateMessage('');
    try {
      const created = await api.createAccount(envelope.body);
      onAccountCreated(created);
      const cleared = clearPendingAccountCreate(setPendingAccountCreate);
      setActionError('');
      setPendingAccountCreateMessage(cleared ? '' : '账户已创建，详情已载入；本机原请求记录未能清除，请重试同一请求完成确认。');
      setCreatingAccount(false);
      setAccountName('');
      setInitialCash('');
      setStartDate(todayISO());
      if (cleared) onNotice('历史模拟账户已创建，数据与费率快照已锁定。');
      else onNotice('账户已创建且详情已载入；请重试同一请求清除本机待确认记录。', 'neutral');
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      const message = error instanceof Error ? error.message : '账户创建结果尚未确认。';
      if (status >= 400 && status < 500 && status !== 408 && status !== 409) {
        const cleared = clearPendingAccountCreate(setPendingAccountCreate);
        const rejection = cleared ? message : `${message}；本机原请求记录无法清除，暂时保留原请求。`;
        setPendingAccountCreateMessage(cleared ? '' : '服务器已明确拒绝，但本机请求记录无法清除。请勿填写新参数；重试仍会使用原请求体。');
        setActionError(rejection);
        onNotice(cleared ? message : '服务器已明确拒绝，本机原请求记录仍待清理。', 'error');
      } else {
        setPendingAccountCreateMessage(`结果未确认：${message}。原始账户信息和 UUID 已保存在本机；不会自动重发，请使用下方按钮重试同一请求。`);
        setActionError('');
        onNotice('账户创建结果待确认；原始请求已保留，请重试同一请求。', 'error');
      }
    } finally {
      accountCreateInFlightRef.current = false;
      setCreating(false);
    }
  }

  async function createNewAccount(event: FormEvent) {
    event.preventDefault();
    if (pendingAccountCreate || accountCreateInFlightRef.current) {
      onNotice('已有账户创建请求待确认，请先重试原请求。', 'error');
      return;
    }
    const cash = numericInput(initialCash);
    if (!accountName.trim() || cash === null || cash <= 0 || !startDate) { onNotice('请填写账户名称、正数初始资金和开始日期。', 'error'); return; }
    if (startDate > todayISO()) { onNotice('开始日期不能晚于今天。', 'error'); return; }
    let requestIdForCreate: string;
    try { requestIdForCreate = accountCreateRequestId(); }
    catch (error) {
      const message = error instanceof Error ? error.message : '无法生成账户创建幂等标识，未发送创建请求。';
      setActionError(message); onNotice(message, 'error'); return;
    }
    const payload: AccountCreatePayload = {
      name: accountName.trim(), initial_cash: cash, start_date: startDate,
      request_id: requestIdForCreate,
    };
    if (advancedFees) {
      for (const value of Object.values(fees)) {
        if (numericInput(value) === null || Number(value) < 0) {
          const message = '费率参数必须填写大于等于 0 的数值。';
          setActionError(message); onNotice(message, 'error'); return;
        }
      }
      payload.commission_rate = Number(fees.commission_rate);
      payload.minimum_commission = Number(fees.minimum_commission);
      payload.stock_stamp_duty_rate = Number(fees.stock_stamp_duty_rate);
      payload.stock_transfer_fee_rate = Number(fees.stock_transfer_fee_rate);
    }
    const envelope: PendingAccountCreate = { body: payload, createdAt: Date.now() };
    if (!storePendingAccountCreate(envelope, setPendingAccountCreate)) {
      const message = '无法在本机保存账户创建请求，未发送创建请求。';
      setActionError(message); onNotice(message, 'error'); return;
    }
    setActionError('');
    setPendingAccountCreateMessage('');
    await runPendingAccountCreate(envelope);
  }

  async function submitOrder(event: FormEvent) {
    event.preventDefault();
    replayPauseRef.current();
    if (trainingReadOnly) { onNotice('已结束的训练账户为只读状态。', 'error'); return; }
    if (trainingMutationPending || replayAdvancePending) { onNotice('训练账户正在确认推进或结束操作，暂不能下单。', 'error'); return; }
    if (!account || !marketReady || price === null || price <= 0 || qty === null || qty <= 0 || riskPercent === null || riskPercent <= 0 || riskPercent > 100) { onNotice('请填写有效价格、数量和 0 到 100 之间的风险比例。', 'error'); return; }
    if (side === 'BUY' && stop === null) { onNotice('买入委托需要填写止损价，以便服务器校验账户风险。', 'error'); return; }
    if (stopPrice && stop === null) { onNotice('止损价需要填写有效数字。', 'error'); return; }
    if (side === 'BUY' && stop !== null && stop >= price) { onNotice('买入委托的止损价应低于限价。', 'error'); return; }
    if (pendingMutation) { onNotice('仍有一笔操作结果待确认，请先用原请求标识重试。', 'error'); return; }
    const id = requestId();
    const body: OrderPayload = { symbol: selectedSymbol, side, quantity: qty, limit_price: price, stop_price: stop, risk_pct: riskPercent, ...(orderPlan ? { plan_id: orderPlan } : {}), request_id: id };
    const envelope: PendingAccountMutation = { accountId: String(account.id), kind: 'order', requestId: id, body, createdAt: Date.now() };
    if (!storePendingMutation(envelope, setPendingMutation)) { onNotice('无法在本机保存幂等请求标识，未发送委托。', 'error'); return; }
    setPlacingOrder(true); setActionError(''); setPendingMutationMessage('');
    await runPendingMutation(envelope);
    setPlacingOrder(false);
  }

  async function runPendingMutation(envelope: PendingAccountMutation) {
    if (envelope.accountId !== activeAccountId) {
      setPendingMutationMessage('切换到该操作所属账户后，再重试同一请求。');
      setActiveAccountId(envelope.accountId);
      return;
    }
    setPendingMutationBusy(true);
    try {
      const result = envelope.kind === 'order'
        ? await api.placeOrder(envelope.accountId, envelope.body!)
        : await api.advanceAccount(envelope.accountId, envelope.requestId);
      updateAccountData(result);
      clearPendingMutation(setPendingMutation);
      setPendingMutationMessage('');
      if (envelope.kind === 'order') { setOrderQuantity(''); setOrderPrice(''); setStopPrice(''); }
      onNotice(envelope.kind === 'order' ? '委托已确认写入。仅在下一交易日开盘尝试撮合，未成交余单当日到期。' : `模拟进度已确认更新至 ${formatDate(result.cursor_date ?? '')}。`);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      if (status === 409) {
        try {
          const readback = await api.account(envelope.accountId);
          updateAccountData(readback);
          const requestFound = envelope.kind === 'order'
            ? (readback.orders ?? []).some((order) => String(order.request_id ?? '') === envelope.requestId)
            : String((readback as unknown as Record<string, unknown>).last_advance_request_id ?? '') === envelope.requestId;
          if (requestFound) {
            clearPendingMutation(setPendingMutation);
            setPendingMutationMessage('');
            onNotice('账户读取已确认这笔操作已写入。');
          } else {
            setPendingMutationMessage('服务器返回冲突，账户读取仍无法确认是否写入。保留原请求标识；请重试同一请求，不要重新提交。');
          }
        } catch {
          setPendingMutationMessage('服务器返回冲突，账户暂时无法读取。保留原请求标识；请重试同一请求。');
        }
      } else if (status >= 400 && status < 500 && status !== 408) {
        clearPendingMutation(setPendingMutation);
        const message = error instanceof Error ? error.message : '服务器拒绝了这笔操作。';
        setPendingMutationMessage('服务器已明确拒绝，当前请求标识已解除。');
        setActionError(message); onNotice(message, 'error');
      } else {
        const message = error instanceof Error ? error.message : '操作结果尚未确认。';
        setPendingMutationMessage(`结果未确认：${message}。重试会复用同一个请求标识和原始参数。`);
        setActionError('操作结果未确认。请使用下方按钮重试同一请求。');
        onNotice('操作结果未确认；原请求已保留，请勿重新创建同一操作。', 'error');
      }
    } finally { setPendingMutationBusy(false); }
  }

  async function advanceNextDay() {
    replayPauseRef.current();
    if (trainingSession) { onNotice('训练账户请使用历史回放控制推进。', 'error'); return; }
    if (!account || !marketReady) { onNotice('账户行情尚未对齐或没有可用历史数据，暂不能推进。', 'error'); return; }
    if (pendingMutation) { onNotice('仍有一笔操作结果待确认，请先重试同一请求。', 'error'); return; }
    const cursor = account.cursor_date ?? '尚未开始';
    if (!window.confirm(`当前模拟进度：${formatDate(String(cursor))}\n\n将仅推进到下一个已验证的历史交易日，并按开盘价尝试撮合限价委托。未成交余单当日到期，不模拟盘中触价。不会读取实时行情。是否继续？`)) return;
    const id = requestId();
    const envelope: PendingAccountMutation = { accountId: String(account.id), kind: 'advance', requestId: id, createdAt: Date.now() };
    if (!storePendingMutation(envelope, setPendingMutation)) { onNotice('无法在本机保存幂等请求标识，未推进账户。', 'error'); return; }
    setAdvancing(true); setActionError(''); setPendingMutationMessage('');
    await runPendingMutation(envelope);
    setAdvancing(false);
  }

  async function cancelOrder(order: Order) {
    replayPauseRef.current();
    if (trainingReadOnly) { onNotice('已结束的训练账户为只读状态。', 'error'); return; }
    if (trainingMutationPending || replayAdvancePending) { onNotice('训练账户正在确认推进或结束操作，暂不能撤单。', 'error'); return; }
    if (!account || pendingMutation) { onNotice('先确认待处理操作，再撤销委托。', 'error'); return; }
    const id = orderId(order);
    if (id === undefined) return;
    setCanceling(String(id)); setActionError('');
    try {
      const result = await api.cancelOrder(account.id, id);
      updateAccountData(result);
      onNotice('撤单请求已处理。');
    } catch (error) { const message = error instanceof Error ? error.message : '撤单失败。'; setActionError(message); onNotice(message, 'error'); }
    finally { setCanceling(null); }
  }

  const levels = useMemo(() => {
    const items: Array<{ label: string; value: number | null | undefined; color: string }> = [];
    if (selectedPosition?.avg_cost !== null && selectedPosition?.avg_cost !== undefined) items.push({ label: '持仓成本', value: selectedPosition.avg_cost, color: '#d49e40' });
    const linkedPlanId = orderPlan || String(selectedPosition?.plan_id ?? account?.orders?.find((item) => item.symbol === selectedSymbol && item.plan_id)?.plan_id ?? account?.fills?.find((item) => item.symbol === selectedSymbol && item.plan_id)?.plan_id ?? '');
    const plan = selectedPlans.find((item) => linkedPlanId && String(item.id) === linkedPlanId);
    if (plan?.stop_price !== null && plan?.stop_price !== undefined) items.push({ label: '计划止损', value: plan.stop_price, color: '#db6770' });
    if (plan?.target_price !== null && plan?.target_price !== undefined) items.push({ label: '计划目标', value: plan.target_price, color: '#329c7d' });
    return items;
  }, [selectedPosition, selectedPlans, orderPlan, account?.orders, account?.fills, selectedSymbol]);

  const pendingMutationBanner = pendingMutation && <div className="pending-mutation-banner"><div><AlertTriangle size={17} /><span><b>{pendingMutation.kind === 'order' ? '委托结果待确认' : '推进结果待确认'}</b><small>{pendingMutationMessage || '服务器响应未得到确认；原请求及参数已保存在本机。不会自动重发。'}</small></span></div><button className="button button-outline button-small" onClick={() => pendingMutation.accountId === activeAccountId ? runPendingMutation(pendingMutation) : setActiveAccountId(pendingMutation.accountId)} disabled={pendingMutationBusy} data-testid="retry-pending-mutation">{pendingMutationBusy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}{pendingMutation.accountId === activeAccountId ? '重试同一请求' : '切换至所属账户'}</button></div>;
  const pendingAccountCreateBanner = pendingAccountCreate && !creatingAccount
    ? <PendingAccountCreateBanner message={pendingAccountCreateMessage} busy={creating} onRetry={() => runPendingAccountCreate(pendingAccountCreate)} />
    : null;
  const accountCreateRetryBanner = pendingAccountCreate
    ? <PendingAccountCreateBanner message={pendingAccountCreateMessage} busy={creating} onRetry={() => runPendingAccountCreate(pendingAccountCreate)} />
    : null;
  const accountCreateModal = creatingAccount && <AccountCreateModal accountName={accountName} setAccountName={setAccountName} initialCash={initialCash} setInitialCash={setInitialCash} startDate={startDate} setStartDate={setStartDate} advancedFees={advancedFees} setAdvancedFees={setAdvancedFees} fees={fees} setFees={setFees} saving={creating} error={actionError} pending={Boolean(pendingAccountCreate)} pendingBanner={accountCreateRetryBanner} onSubmit={createNewAccount} onClose={() => setCreatingAccount(false)} />;

  if (!accounts.length && !loading) return <div className="account-empty-layout">{pendingMutationBanner}{pendingAccountCreateBanner}<section className="panel account-onboarding"><div className="account-onboarding-symbol"><Wallet size={22} /></div><div className="eyebrow">HISTORICAL SIMULATION / SETUP</div><h2>建立一份隔离的历史账本</h2><p>账户会绑定创建时可用的行情、日历、执行和费率快照。后续数据更新不会改写已创建账户的历史视图。</p><div className="onboarding-rules"><span><CheckCircle2 size={16} />仅推进已核验交易日</span><span><CheckCircle2 size={16} />委托在历史开盘规则下撮合</span><span><CheckCircle2 size={16} />与其他账户账本隔离</span></div><button className="button button-primary" onClick={() => setCreatingAccount(true)} disabled={Boolean(pendingAccountCreate)} data-testid="create-account-open"><Plus size={16} /> 创建历史模拟账户</button></section><ReplayPanel sessions={trainingSessions} setSessions={setTrainingSessions} activeAccountId={activeAccountId} setActiveAccountId={setActiveAccountId} account={account} selectedSymbol={selectedSymbol} setSelectedSymbol={setSelectedSymbol} symbols={symbols} market={market} accountMutationPending={Boolean(pendingMutation) || pendingMutationBusy || canceling !== null || placingOrder} onAccountUpdated={updateAccountData} onRefreshAccount={onRefresh} onPauseReady={(pause) => { replayPauseRef.current = pause; onPauseReady(pause); }} onAdvancePending={setReplayAdvancePending} onSessionMutationPending={setTrainingMutationPending} onNotice={onNotice} />{accountCreateModal}</div>;

  return <div className="simulation-page">
    <section className="panel simulation-toolbar"><div className="account-picker"><div className="account-picker-icon"><Wallet size={17} /></div><label><span>模拟账户</span><select value={activeAccountId} onChange={(event) => { const nextId = event.target.value; const nextTraining = trainingSessions.find((item) => String(item.account_id) === nextId); replayPauseRef.current(); setTrainingMutationPending(false); if (nextTraining) setSelectedSymbol(nextTraining.symbol); setActiveAccountId(nextId); }} data-testid="account-select">{activeAccountId && !activeAccountIsListed && <option value={activeAccountId}>{account?.id && String(account.id) === activeAccountId ? `${account.name} · 已选` : activeAccountLoadingLabel}</option>}{accounts.map((item) => <option key={item.id} value={String(item.id)}>{item.name} · {item.cursor_date ? formatDate(item.cursor_date) : '待推进'}</option>)}</select></label><ChevronDown size={14} /></div><div className="toolbar-divider" /><div className="sim-date-lock"><span className="status-light" /><span>历史进度</span><b>{account?.cursor_date ? formatDate(account.cursor_date) : '未开始'}</b></div><div className="toolbar-spacer" /><button className="button button-outline button-small" onClick={() => setCreatingAccount(true)} disabled={Boolean(pendingAccountCreate)}><Plus size={14} /> 新建账户</button>{!trainingSession && <button className="button button-primary button-small" onClick={advanceNextDay} disabled={!account || advancing || loading || !marketReady || Boolean(pendingMutation) || pendingMutationBusy} data-testid="advance-day">{advancing ? <LoaderCircle size={15} className="spin" /> : <ArrowRight size={15} />}推进下一交易日</button>}</section>
    <ReplayPanel sessions={trainingSessions} setSessions={setTrainingSessions} activeAccountId={activeAccountId} setActiveAccountId={setActiveAccountId} account={account} selectedSymbol={selectedSymbol} setSelectedSymbol={setSelectedSymbol} symbols={symbols} market={market} accountMutationPending={Boolean(pendingMutation) || pendingMutationBusy || canceling !== null || placingOrder} onAccountUpdated={updateAccountData} onRefreshAccount={onRefresh} onPauseReady={(pause) => { replayPauseRef.current = pause; onPauseReady(pause); }} onAdvancePending={setReplayAdvancePending} onSessionMutationPending={setTrainingMutationPending} onNotice={onNotice} />
    {loading && <div className="loading-panel"><LoaderCircle className="spin" />读取模拟账本…</div>}
    {accountError && !loading && !account && <EmptyState icon={AlertTriangle} title="无法读取该模拟账户" detail={accountError} action={<button className="button button-outline" onClick={() => onRefresh()}>重试读取</button>} />}
    {accountError && !loading && account && <div className="inline-warning"><AlertTriangle size={15} /><span>{accountError}</span><button className="button button-outline button-small" onClick={() => onRefresh()}>重试读取</button></div>}
    {pendingMutationBanner}
    {pendingAccountCreateBanner}
    {account && <>
      <div className="sim-account-heading"><div><span className="account-name-label">{account.name}</span><span className="status-pill neutral">HISTORICAL ONLY</span></div><span>账户 ID <b className="mono">{getAccountId(account)}</b></span></div>
      <div className="sim-metrics">
        <div className="panel sim-metric"><span>总权益</span><strong>¥{formatMoney(account.equity)}</strong><small>以模拟账本估值为准</small></div>
        <div className="panel sim-metric"><span>可用现金</span><strong>¥{formatMoney(account.cash)}</strong><small>初始资金 ¥{formatMoney(account.initial_cash)}</small></div>
        <div className="panel sim-metric"><span>已实现盈亏</span><strong className={finite(account.realized_pnl) ? account.realized_pnl >= 0 ? 'positive' : 'negative' : ''}>{finite(account.realized_pnl) ? `${account.realized_pnl >= 0 ? '+' : ''}¥${formatMoney(account.realized_pnl)}` : '—'}</strong><small>来自账本已平仓记录</small></div>
        <div className="panel sim-metric"><span>持仓浮动盈亏</span><strong className={finite(account.unrealized_pnl) ? account.unrealized_pnl >= 0 ? 'positive' : 'negative' : ''}>{finite(account.unrealized_pnl) ? `${account.unrealized_pnl >= 0 ? '+' : ''}¥${formatMoney(account.unrealized_pnl)}` : '—'}</strong><small>按账户可见历史行情</small></div>
      </div>
      {actionError && <div className="inline-warning error-warning"><AlertTriangle size={15} /><span>{actionError}</span></div>}
      {accountWarnings(account).map((message, index) => <div className="inline-warning error-warning" key={`${message}-${index}`}><AlertTriangle size={15} /><span>{message}</span></div>)}
      {accountDrawdownPct(account) !== null && accountDrawdownPct(account)! >= 5 && <div className="drawdown-warning"><ShieldAlert size={17} /><span><b>账户回撤达到 5%</b><small>建议暂停交易并完成一次复盘，先核对账户估值与决策依据。</small></span><button className="button button-outline button-small" onClick={() => window.dispatchEvent(new CustomEvent('trading-lab:navigate', { detail: 'review' }))}>前往复盘 <ArrowRight size={13} /></button></div>}
      <div className="simulation-grid">
        <div className="simulation-main">
          <KlinePanel rows={marketRows} symbol={selectedSymbol} symbolName={symbolName} source={market?.source} adjustment={market?.adjustment} updatedAt={market?.updated_at} warning={market?.warning} theme={theme} fills={account.fills ?? []} unit={unit} endDate={account.cursor_date ?? undefined} levels={levels} cutoffLabel={`账户历史行情截至 ${formatDate(account.cursor_date ?? '')} · 已按账户 ${market?.dataset_id ? `数据集 ${market.dataset_id}` : '冻结数据快照'} 截断`} emptyLabel="该账户日期范围内没有已验证行情" />
          <section className="panel ledger-panel"><div className="panel-head ledger-head"><div><div className="eyebrow">ACCOUNT LEDGER</div><h2>账户记录</h2></div><div className="ledger-tabs">{([['positions', '持仓'], ['orders', '委托'], ['fills', '成交'], ['cycles', '交易周期']] as Array<[OrderTab, string]>).map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}<small>{key === 'positions' ? account.positions?.length ?? 0 : key === 'orders' ? account.orders?.length ?? 0 : key === 'fills' ? account.fills?.length ?? 0 : account.cycles?.length ?? 0}</small></button>)}</div></div>
            {tab === 'positions' && (account.positions?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>标的</th><th>持仓</th><th>可卖</th><th>成本均价</th><th>市值</th><th>浮动盈亏</th></tr></thead><tbody>{account.positions.map((position, index) => <tr key={`${position.symbol}-${index}`}><td><div className="table-symbol"><b>{positionName(position, new Map(symbols.map((item) => [item.symbol, item.name])))}</b><small className="mono">{position.symbol}</small></div></td><td>{formatQuantity(position.quantity)} {symbolUnit(position.symbol, symbols)}</td><td>{formatQuantity(position.sellable)} {symbolUnit(position.symbol, symbols)}</td><td>¥{formatMoney(position.avg_cost, priceDigits(position.avg_cost))}</td><td>¥{formatMoney(position.market_value)}</td><td className={finite(position.unrealized_pnl) ? position.unrealized_pnl >= 0 ? 'positive' : 'negative' : ''}>{formatMoney(position.unrealized_pnl)}</td></tr>)}</tbody></table></div> : <EmptyState icon={Wallet} title="当前没有持仓" detail="买入委托仅在下一交易日开盘尝试撮合，未成交余单当日到期。" />)}
            {tab === 'orders' && (account.orders?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>提交日期</th><th>标的</th><th>方向</th><th>委托数量</th><th>限价</th><th>成交数量</th><th>状态</th><th /></tr></thead><tbody>{account.orders.map((order, index) => { const id = orderId(order); const status = String(order.status ?? ''); const canCancel = ['pending', 'partially_filled'].includes(status.toLowerCase()); return <tr key={id ?? index}><td>{formatDate(String(order.created_date ?? order.created_at ?? ''))}</td><td className="mono">{order.symbol}</td><td><span className={`side-pill ${(order.side ?? '').toLowerCase()}`}>{order.side === 'BUY' ? '买入' : order.side === 'SELL' ? '卖出' : order.side ?? '—'}</span></td><td>{formatQuantity(order.quantity)}</td><td>¥{formatMoney(order.limit_price, priceDigits(order.limit_price))}</td><td>{formatQuantity(order.filled_quantity)}</td><td><div className="status-stack"><span className={`status-pill ${statusTone(status)}`}>{normalizeStatus(status)}</span>{order.pending_reason !== undefined && order.pending_reason !== null && order.pending_reason !== '' && <small>{translatePendingReason(String(order.pending_reason))}</small>}</div></td><td>{canCancel && id !== undefined && <button className="row-action-button danger" title="撤销委托" disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending || canceling === String(id) || Boolean(pendingMutation)} onClick={() => cancelOrder(order)}>{canceling === String(id) ? <LoaderCircle size={14} className="spin" /> : <X size={14} />}</button>}</td></tr>; })}</tbody></table></div> : <EmptyState icon={ListChecks} title="没有委托记录" detail="新委托会进入队列，不会立即成交。" />)}
            {tab === 'fills' && (account.fills?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>成交时间</th><th>标的</th><th>方向</th><th>成交数量</th><th>成交价格</th><th>已实现盈亏</th><th>关联计划</th></tr></thead><tbody>{account.fills.map((fill, index) => <tr key={fill.id ?? index}><td>{formatDate(fillDate(fill))}</td><td className="mono">{fill.symbol}</td><td><span className={`side-pill ${(fillSide(fill) || '').toLowerCase()}`}>{fillSide(fill) === 'BUY' ? '买入' : fillSide(fill) === 'SELL' ? '卖出' : '—'}</span></td><td>{formatQuantity(fill.quantity)} {symbolUnit(fill.symbol ?? '', symbols)}</td><td>¥{formatMoney(fillPrice(fill), priceDigits(fillPrice(fill)))}</td><td className={finite(fill.realized_pnl ?? fill.pnl) ? Number(fill.realized_pnl ?? fill.pnl) >= 0 ? 'positive' : 'negative' : ''}>{formatMoney(fill.realized_pnl ?? fill.pnl)}</td><td>{fill.plan_id ?? '未关联'}</td></tr>)}</tbody></table></div> : <EmptyState icon={Activity} title="暂无历史成交" detail="委托只在下一历史交易日开盘尝试一次，未成交余单当日到期。" />)}
            {tab === 'cycles' && (account.cycles?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>标的</th><th>建仓日</th><th>平仓日</th><th>状态</th><th>净收益</th><th>R 值</th><th>数量</th></tr></thead><tbody>{account.cycles.slice(0, 50).map((cycle, index) => { const rawStatus = objectLabel(cycle, 'status', 'state'); const status = ['open', 'active', 'holding'].includes(rawStatus.toLowerCase()) ? '持仓中' : ['closed', 'completed', 'done'].includes(rawStatus.toLowerCase()) ? '已平仓' : normalizeStatus(rawStatus); const pnl = numericValue(cycle, 'net_pnl', 'realized_pnl', 'pnl'); return <tr key={rowId(cycle, index)}><td className="mono">{objectLabel(cycle, 'symbol')}</td><td>{formatDate(objectLabel(cycle, 'entry_date', 'opened_at', 'open_date', 'start_date'))}</td><td>{formatDate(objectLabel(cycle, 'exit_date', 'closed_at', 'close_date', 'end_date'))}</td><td>{status}</td><td className={pnl === null ? '' : pnl >= 0 ? 'positive' : 'negative'}>{formatMoney(pnl)}</td><td>{formatMetricR(cycle)}</td><td>{formatQuantity(numericValue(cycle, 'quantity', 'shares'))} {symbolUnit(objectLabel(cycle, 'symbol'), symbols)}</td></tr>; })}</tbody></table></div> : <EmptyState icon={History} title="暂无完整交易周期" detail="周期统计会在仓位完整闭合后由账本形成。" />)}
          </section>
        </div>
        <aside className="simulation-side">
          <section className="panel order-form-panel"><div className="panel-head"><div><div className="eyebrow">ORDER TICKET / HISTORICAL</div><h2>模拟委托</h2></div><span className={`badge ${trainingReadOnly ? 'badge-soft' : 'badge-warning'}`}>{trainingReadOnly ? '训练已结束 · 只读' : trainingMutationPending ? '训练操作确认中' : '不连接实盘'}</span></div><div className="side-switch"><button className={side === 'BUY' ? 'buy active' : ''} onClick={() => setSide('BUY')} disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending} data-testid="order-side-buy"><ArrowUpRight size={15} />买入</button><button className={side === 'SELL' ? 'sell active' : ''} onClick={() => setSide('SELL')} disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending} data-testid="order-side-sell"><ArrowDownRight size={15} />卖出</button></div>
            <form className="order-form" onSubmit={submitOrder}><label className="form-field"><span>交易标的</span><select value={selectedSymbol} onChange={() => undefined} disabled>{symbols.map((item) => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}</select></label><div className="form-grid form-grid-two"><label className="form-field"><span>委托数量</span><div className="input-suffix"><input type="number" min={side === 'BUY' ? 100 : 1} step={side === 'BUY' ? 100 : 1} value={orderQuantity} onChange={(event) => setOrderQuantity(event.target.value)} placeholder="数量" required disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending} data-testid="order-quantity" /><b>{unit}</b></div></label><label className="form-field"><span>限价</span><div className="input-prefix"><b>¥</b><input type="number" min={tickSize} step={tickSize} value={orderPrice} onChange={(event) => setOrderPrice(event.target.value)} placeholder={`¥ ${tickSize === 0.001 ? '0.000' : '0.00'}`} required disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending} data-testid="order-limit-price" /></div></label></div><div className="form-grid form-grid-two"><label className="form-field"><span>止损价 {side === 'BUY' ? <i>* 买入必填</i> : <small>可选</small>}</span><div className="input-prefix"><b>¥</b><input type="number" min={tickSize} step={tickSize} value={stopPrice} onChange={(event) => setStopPrice(event.target.value)} placeholder="仅用于风险校验，不自动止损" required={side === 'BUY'} disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending} data-testid="order-stop-price" /></div></label><label className="form-field"><span>风险比例 <small>占现金与权益较小值</small></span><div className="input-suffix"><input type="number" min="0.01" max="100" step="0.01" value={riskPct} onChange={(event) => setRiskPct(event.target.value)} required disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending} data-testid="order-risk-pct" /><b>%</b></div></label></div><label className="form-field"><span>关联计划 <small>选填</small></span><select value={orderPlan} onChange={(event) => setOrderPlan(event.target.value)} disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending}><option value="">不关联</option>{selectedPlans.map((plan) => <option key={plan.id} value={String(plan.id)}>{plan.strategy || '计划'} · {formatDate(plan.created_at)}</option>)}</select></label>
              <div className="order-risk-box"><div><span>委托金额估算</span><b>{orderValue !== null ? `¥${formatMoney(orderValue)}` : '—'}</b></div><div><span>含费用风险估算</span><b>{estimatedRisk !== null ? `¥${formatMoney(estimatedRisk)}` : '—'}</b></div><div><span>费用后预算参考</span><b>{riskBudget !== null ? `¥${formatMoney(riskBudget)}` : '—'}</b></div><small>{maxAffordableBuyQuantity !== null ? `风险与可用现金估算的最大买入数量：${formatQuantity(maxAffordableBuyQuantity)} ${unit}。` : '风险预算按可用现金与权益的较小值计算。'} 费用仅作估算；最终余额、费率和可买数量以服务端冻结规则校验为准。</small>{side === 'BUY' && maxAffordableBuyQuantity === 0 && <b className="risk-budget-warning">一手的止损风险或现金需求已超过当前预算</b>}</div>
              <button className="button button-primary order-submit" type="submit" disabled={trainingReadOnly || trainingMutationPending || replayAdvancePending || placingOrder || loading || !marketReady || Boolean(pendingMutation) || pendingMutationBusy} data-testid="submit-order">{placingOrder ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}{trainingReadOnly ? '训练已结束' : trainingMutationPending ? '训练操作确认中' : placingOrder ? '提交中…' : `提交${side === 'BUY' ? '买入' : '卖出'}委托`}</button>
            </form><div className="order-note"><InfoIcon /><span>委托不会即时成交。只在下一历史交易日开盘尝试撮合；未成交余单当日到期，不模拟盘中触价。</span></div></section>
          <section className="panel cursor-card"><div className="cursor-card-icon"><CalendarDays size={17} /></div><div className="eyebrow">SIMULATION CURSOR</div><strong>{account.cursor_date ? formatDate(account.cursor_date) : '尚未开始'}</strong><p>{account.cursor_date ? '图表、成本和成交标记都被限制在该日。' : '创建时选定开始日，推进后才会读取账户历史行情。'}</p><div className="cursor-lock"><ShieldAlert size={14} />账户时间边界已锁定</div></section>
        </aside>
      </div>
    </>}
    {accountCreateModal}
  </div>;
}

function AccountCreateModal({ accountName, setAccountName, initialCash, setInitialCash, startDate, setStartDate, advancedFees, setAdvancedFees, fees, setFees, saving, error, pending, pendingBanner, onSubmit, onClose }: {
  accountName: string; setAccountName: (value: string) => void; initialCash: string; setInitialCash: (value: string) => void; startDate: string; setStartDate: (value: string) => void; advancedFees: boolean; setAdvancedFees: (value: boolean) => void; fees: { commission_rate: string; minimum_commission: string; stock_stamp_duty_rate: string; stock_transfer_fee_rate: string }; setFees: (value: { commission_rate: string; minimum_commission: string; stock_stamp_duty_rate: string; stock_transfer_fee_rate: string }) => void; saving: boolean; error: string; pending: boolean; pendingBanner: ReactNode; onSubmit: (event: FormEvent) => void; onClose: () => void;
}) {
  return <div className="modal-backdrop" role="presentation" onClick={onClose}>
    <section className="modal-card account-modal" role="dialog" aria-modal="true" aria-labelledby="account-modal-title" onClick={(event) => event.stopPropagation()}>
      <div className="modal-head"><div><div className="eyebrow">NEW SIMULATION ACCOUNT</div><h3 id="account-modal-title">创建历史模拟账户</h3></div><button type="button" className="icon-button" onClick={onClose} title="关闭"><X size={17} /></button></div>
      {pendingBanner}
      <form className="account-create-form" onSubmit={onSubmit}>
        <label className="form-field"><span>账户名称</span><input value={accountName} onChange={(event) => setAccountName(event.target.value)} placeholder="例如：波段策略 · 观察账户" required maxLength={80} disabled={pending} data-testid="account-name" /></label>
        <div className="form-grid form-grid-two">
          <label className="form-field"><span>初始资金</span><div className="input-prefix"><b>¥</b><input type="number" min="1" step="0.01" value={initialCash} onChange={(event) => setInitialCash(event.target.value)} placeholder="填写资金基数" required disabled={pending} data-testid="account-cash" /></div></label>
          <label className="form-field"><span>开始日期</span><input type="date" max={todayISO()} value={startDate} onChange={(event) => setStartDate(event.target.value)} required disabled={pending} data-testid="account-start-date" /></label>
        </div>
        <label className="advanced-toggle"><input type="checkbox" checked={advancedFees} onChange={(event) => setAdvancedFees(event.target.checked)} disabled={pending} /><span><b>展开费用参数</b><small>创建时的费率会冻结到账户执行规则</small></span><ChevronDown size={15} /></label>
        {advancedFees && <div className="advanced-fees-grid">
          <label className="form-field"><span>佣金费率</span><input type="number" min="0" step="0.00001" value={fees.commission_rate} onChange={(event) => setFees({ ...fees, commission_rate: event.target.value })} disabled={pending} /></label>
          <label className="form-field"><span>最低佣金（元）</span><input type="number" min="0" step="0.1" value={fees.minimum_commission} onChange={(event) => setFees({ ...fees, minimum_commission: event.target.value })} disabled={pending} /></label>
          <label className="form-field"><span>股票印花税率</span><input type="number" min="0" step="0.00001" value={fees.stock_stamp_duty_rate} onChange={(event) => setFees({ ...fees, stock_stamp_duty_rate: event.target.value })} disabled={pending} /></label>
          <label className="form-field"><span>股票过户费率</span><input type="number" min="0" step="0.000001" value={fees.stock_transfer_fee_rate} onChange={(event) => setFees({ ...fees, stock_transfer_fee_rate: event.target.value })} disabled={pending} /></label>
        </div>}
        <div className="snapshot-note"><ShieldAlert size={15} /><span>行情、交易日历、执行规则和费用参数会在创建时绑定为快照。已创建账户不会被后续数据同步改写。</span></div>
        {error && <div className="inline-error">{error}</div>}
        <div className="modal-actions"><button className="button button-quiet" type="button" onClick={onClose}>取消</button><button className="button button-primary" type="submit" disabled={saving || pending} data-testid="account-create-submit">{saving ? <LoaderCircle size={15} className="spin" /> : <Plus size={15} />}{pending ? '先确认原请求' : '创建账户'}</button></div>
      </form>
    </section>
  </div>;
}

function StatisticsPage({ stats, loading, account, accounts, activeAccountId, theme }: { stats: AccountStats | null; loading: boolean; account: AccountDetail | null; accounts: AccountSummary[]; activeAccountId: string; theme: ThemeMode }) {
  const equityRows = curveRows(stats?.equity_curve?.length ? stats.equity_curve : account?.equity_curve ?? []);
  const monthlyRows = safeRows(stats?.monthly).flatMap((item) => {
    const label = objectLabel(item, 'month', 'date', 'period');
    const value = numericValue(item, 'net_pnl', 'pnl', 'cumulative_net', 'value');
    return label !== '—' && value !== null ? [{ label, value }] : [];
  });
  const symbolRows = safeRows(stats?.by_symbol).flatMap((item) => {
    const label = objectLabel(item, 'symbol', 'name', 'code');
    const value = numericValue(item, 'cumulative_net', 'realized_pnl', 'pnl', 'net_pnl', 'value');
    return label !== '—' && value !== null ? [{ label, value }] : [];
  }).slice(0, 10);
  const selectedAccount = accounts.find((item) => String(item.id) === activeAccountId);
  const accent = theme === 'dark' ? '#8fbcaf' : '#367e72';
  const lineOption = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 82, right: 24, top: 24, bottom: 32 },
    tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => `¥${formatMoney(typeof value === 'number' ? value : null)}` },
    xAxis: { type: 'category', data: equityRows.map((item) => item.date), axisLine: { lineStyle: { color: theme === 'dark' ? '#33404e' : '#e2e7ee' } }, axisLabel: { color: theme === 'dark' ? '#8e9aaa' : '#8490a1', fontSize: 10, hideOverlap: true } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: theme === 'dark' ? '#8e9aaa' : '#8490a1', fontSize: 10, formatter: (value: number) => `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` }, splitLine: { lineStyle: { color: theme === 'dark' ? '#26313e' : '#edf0f4' } } },
    series: [{ type: 'line', name: '账户权益', data: equityRows.map((item) => item.value), smooth: false, symbol: 'none', lineStyle: { width: 2.3, color: accent }, areaStyle: { color: theme === 'dark' ? 'rgba(143,188,175,0.11)' : 'rgba(54,126,114,0.10)' } }],
  }), [equityRows, theme, accent]);
  const monthlyOption = useMemo<EChartsOption>(() => ({
    animation: false, grid: { left: 42, right: 20, top: 20, bottom: 35 }, tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => `¥${formatMoney(typeof value === 'number' ? value : null)}` },
    xAxis: { type: 'category', data: monthlyRows.map((item) => item.label), axisLabel: { color: theme === 'dark' ? '#8e9aaa' : '#8490a1', fontSize: 10, rotate: monthlyRows.length > 10 ? 28 : 0 } },
    yAxis: { type: 'value', axisLabel: { color: theme === 'dark' ? '#8e9aaa' : '#8490a1', fontSize: 10 }, splitLine: { lineStyle: { color: theme === 'dark' ? '#26313e' : '#edf0f4' } } },
    series: [{ name: '净收益', type: 'bar', data: monthlyRows.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? '#45a487' : '#d87679' } })), barMaxWidth: 22, borderRadius: [3, 3, 0, 0] }],
  }), [monthlyRows, theme]);
  const symbolOption = useMemo<EChartsOption>(() => ({
    animation: false, grid: { left: 90, right: 24, top: 14, bottom: 25, containLabel: true }, tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => `¥${formatMoney(typeof value === 'number' ? value : null)}` },
    xAxis: { type: 'value', axisLabel: { color: theme === 'dark' ? '#8e9aaa' : '#8490a1', fontSize: 10 }, splitLine: { lineStyle: { color: theme === 'dark' ? '#26313e' : '#edf0f4' } } },
    yAxis: { type: 'category', data: symbolRows.map((item) => item.label).reverse(), axisLabel: { color: theme === 'dark' ? '#d1d7e0' : '#647083', fontSize: 11 } },
    series: [{ name: '已实现盈亏', type: 'bar', data: symbolRows.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? '#45a487' : '#d87679', borderRadius: [0, 3, 3, 0] } })).reverse(), barMaxWidth: 17 }],
  }), [symbolRows, theme]);
  const metrics = [
    { label: '成交笔数', value: stats?.totalfills, format: 'quantity', hint: '账户累计委托成交' },
    { label: '已平仓周期', value: stats?.totalclosed, format: 'quantity', hint: '完整闭合的交易周期' },
    { label: '胜率', value: stats?.win_rate, format: 'percent', hint: '仅使用已平仓周期' },
    { label: '平均盈亏比', value: stats?.payoff_ratio, format: 'ratio', hint: '平均盈利 / 平均亏损' },
    { label: '盈利因子（PF）', value: stats?.profit_factor, format: 'ratio', hint: '总盈利 / 总亏损' },
    { label: '平均 R 值', value: stats?.avg_r, format: 'ratio', hint: '按单笔风险单位计算' },
    { label: '累计净收益', value: stats?.cumulative_net, format: 'money', hint: '费用后账本数值' },
    { label: '最大回撤', value: stats?.max_drawdown, format: 'percent', hint: '统计窗口内的历史回撤' },
    { label: '平均持仓时间（自然日）', value: stats?.avg_holding_days, format: 'days', hint: '自然日口径，含周末与休市日' },
  ];
  const formatMetric = (metric: typeof metrics[number]) => metric.value === null || metric.value === undefined ? metric.format === 'ratio' ? '不可计算' : '—' : metric.format === 'percent' ? formatPercent(metric.value) : metric.format === 'money' ? `¥${formatMoney(metric.value)}` : metric.format === 'quantity' ? formatQuantity(metric.value) : metric.format === 'days' ? `${formatQuantity(metric.value)} 天` : finite(metric.value) ? metric.value.toFixed(2) : '—';

  if (!activeAccountId) return <EmptyState icon={Activity} title="先创建一个模拟账户" detail="账户统计来自历史撮合账本。请在历史模拟页面创建账户并推进数据后查看。" action={<button className="button button-primary" onClick={() => window.dispatchEvent(new CustomEvent('trading-lab:navigate', { detail: 'simulation' }))}>前往历史模拟</button>} />;
  return <div className="stats-page">{loading && <div className="loading-panel"><LoaderCircle className="spin" />读取账户统计…</div>}<div className="stats-account-strip"><div className="stats-account-icon"><Activity size={17} /></div><div><span>当前统计账户</span><b>{selectedAccount?.name ?? account?.name ?? '账户'}</b></div><span className="badge badge-soft">{account?.cursor_date ? `截至 ${formatDate(account.cursor_date)}` : '等待历史推进'}</span><div className="stats-strip-note"><InfoIcon />不展示实时行情或账户外的收益数据</div></div>
    {stats?.insufficient_samples === true && <div className="sample-warning"><AlertTriangle size={16} /><span><b>统计样本不足</b><small>仅 {finite(stats.totalclosed) ? formatQuantity(stats.totalclosed) : '—'} 个已完成周期，不能据此判断策略优势。需要至少 30 个完整周期后再评估。</small></span></div>}
    <section className="stats-metric-grid">{metrics.map((metric) => <div className="panel stat-card" key={metric.label} title={metric.format === 'ratio' && (metric.value === null || metric.value === undefined) ? '当亏损为 0 或有效样本不足时，比例不可计算。' : undefined}><span>{metric.label}</span><strong>{formatMetric(metric)}</strong><small>{metric.hint}</small></div>)}<div className="panel stat-card streak-card"><span>当前连胜 / 连亏</span><strong>{displayStreaks(stats?.streaks)}</strong><small>按账户账本周期序列统计</small></div></section>
    <div className="stats-chart-grid"><section className="panel stat-chart-panel equity-chart-panel"><PanelTitle eyebrow="EQUITY CURVE" title="账户权益曲线" action={account?.cursor_date && <span className="chart-cutoff-note">截至 {formatDate(account.cursor_date)}</span>} />{equityRows.length ? <EChart option={lineOption} theme={theme} className="equity-chart" /> : <EmptyState icon={TrendingUp} title="暂无权益曲线" detail="账户推进并产生账本数据后，这里会展示真实的权益变化。" />}</section><section className="panel stat-chart-panel"><PanelTitle eyebrow="MONTHLY RESULT" title="月度净收益" />{monthlyRows.length ? <EChart option={monthlyOption} theme={theme} className="monthly-chart" /> : <EmptyState icon={Activity} title="暂无月度统计" detail="没有可统计的已实现账本数据。" />}</section></div>
    <section className="panel stat-chart-panel symbol-performance"><PanelTitle eyebrow="BY SYMBOL" title="标的盈亏分布" /><p className="chart-caption">只汇总当前账户已闭合周期的账本结果。</p>{symbolRows.length ? <EChart option={symbolOption} theme={theme} className="symbol-chart" /> : <EmptyState icon={Activity} title="暂无标的统计" detail="完整交易周期形成后，才会出现各标的盈亏数据。" />}</section>
  </div>;
}

function priceDigits(value: unknown): number {
  if (!finite(value)) return 2;
  const decimals = String(value).split('.')[1]?.length ?? 0;
  return Math.max(2, Math.min(4, decimals));
}

function accountDrawdownPct(account: AccountDetail): number | null {
  const direct = numericValue(account as unknown as LooseRecord, 'current_drawdown', 'drawdown');
  if (direct !== null) return Math.abs(direct) <= 1 ? Math.abs(direct) * 100 : Math.abs(direct);
  const rows = curveRows(account.equity_curve ?? []);
  let peak = 0;
  let maxDrawdown = 0;
  for (const row of rows) {
    peak = Math.max(peak, row.value);
    if (peak > 0) maxDrawdown = Math.max(maxDrawdown, (peak - row.value) / peak * 100);
  }
  const reportedMax = numericValue(account as unknown as LooseRecord, 'max_drawdown');
  if (reportedMax !== null) maxDrawdown = Math.max(maxDrawdown, Math.abs(reportedMax) <= 1 ? Math.abs(reportedMax) * 100 : Math.abs(reportedMax));
  return rows.length || reportedMax !== null ? maxDrawdown : null;
}

function accountWarnings(account: AccountDetail): string[] {
  const record = account as unknown as LooseRecord;
  const list = Array.isArray(record.warnings) ? record.warnings.map(String) : [];
  const stale = record.stale_mark === true || String(record.valuation_status ?? '').toLowerCase() === 'stale' || list.some((value) => value.toLowerCase().includes('stale_mark'));
  const translated = list.map((value) => value.toLowerCase().includes('stale_mark') ? '估值标记已过期，浮动盈亏可能不是截至进度日的最新可用值。' : value);
  if (stale && !translated.some((value) => value.includes('估值标记已过期'))) translated.push('估值标记已过期，浮动盈亏可能不是截至进度日的最新可用值。');
  return translated;
}

function translatePendingReason(value: unknown): string {
  if (value === null || value === undefined || value === '') return '';
  const raw = String(value);
  if (/[\u3400-\u9fff]/.test(raw)) return raw;
  const labels: Record<string, string> = {
    no_open_bar: '下一交易日没有可用开盘价', no_market_data: '该日历史行情缺失', insufficient_cash: '可用现金不足', insufficient_position: '可卖持仓不足',
    price_not_reached: '开盘价未满足限价条件', suspended: '标的当日停牌', limit_up: '开盘价触及涨停限制', limit_down: '开盘价触及跌停限制',
    expired: '未成交余单当日到期', partial_fill: '已部分成交，剩余数量当日到期', rejected: '后端风控拒绝', risk_limit: '超出账户风险比例限制',
  };
  return labels[raw.toLowerCase()] ?? raw;
}

function symbolUnit(symbol: string, symbols: Bootstrap['symbols']): string {
  const kind = symbols.find((item) => item.symbol === symbol)?.kind?.toLowerCase() ?? '';
  return kind.includes('etf') || kind.includes('基金') ? '份' : '股';
}

type FeeSchedule = { commissionRate: number; minimumCommission: number; stampDutyRate: number; transferFeeRate: number };

function feeSchedule(account: AccountDetail): FeeSchedule {
  const nested = account.fee_schedule ?? account.fees ?? account.settings ?? {};
  const values = nested && typeof nested === 'object' ? nested as Record<string, unknown> : {};
  const read = (key: string, alias: string, fallback: number) => finite(account[key]) ? account[key] as number : finite(values[key]) ? values[key] as number : finite(values[alias]) ? values[alias] as number : fallback;
  return {
    commissionRate: read('commission_rate', 'commission_rate', 0.00025),
    minimumCommission: read('minimum_commission', 'minimum_commission', 5),
    stampDutyRate: read('stock_stamp_duty_rate', 'stamp_duty_rate', 0.0005),
    transferFeeRate: read('stock_transfer_fee_rate', 'transfer_fee_rate', 0.00001),
  };
}

function estimateRoundTripFees(entry: number, stop: number, quantity: number, side: 'BUY' | 'SELL', fee: FeeSchedule, isStock: boolean): number {
  const entryValue = entry * quantity;
  const stopValue = stop * quantity;
  const entryCommission = Math.max(fee.minimumCommission, entryValue * fee.commissionRate);
  const exitCommission = Math.max(fee.minimumCommission, stopValue * fee.commissionRate);
  const stampDuty = isStock && (side === 'BUY' || side === 'SELL') ? stopValue * fee.stampDutyRate : 0;
  const transferFee = isStock ? (entryValue + stopValue) * fee.transferFeeRate : 0;
  return entryCommission + exitCommission + stampDuty + transferFee;
}

function formatMetricR(record: LooseRecord): string {
  const value = numericValue(record, 'r_multiple', 'avg_r', 'realized_r', 'r_value');
  return value === null ? '—' : `${value.toFixed(2)} R`;
}

function numericValue(record: LooseRecord, ...keys: string[]): number | null {
  for (const key of keys) if (finite(record[key])) return record[key] as number;
  return null;
}

function DataPage({ symbols, selectedSymbol, selectedDate, market, marketRows, loading, activeAccountId, onNotice, onRefresh, onSetMarket }: {
  symbols: Bootstrap['symbols']; selectedSymbol: string; selectedDate: string; market: MarketPayload | null; marketRows: MarketRow[]; loading: boolean; activeAccountId: string; onNotice: (message: string, tone?: 'success' | 'error' | 'neutral') => void; onRefresh: () => void; onSetMarket: (market: MarketPayload) => void;
}) {
  const [syncStart, setSyncStart] = useState(() => localStorage.getItem('tl-sync-start') || '');
  const [syncEnd, setSyncEnd] = useState(selectedDate);
  const [provider, setProvider] = useState<'tushare' | 'akshare'>('tushare');
  const [syncing, setSyncing] = useState(false);
  const [importSource, setImportSource] = useState('本地 CSV');
  const [importing, setImporting] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [dataActionError, setDataActionError] = useState('');
  useEffect(() => { setSyncEnd(selectedDate); }, [selectedDate]);

  async function syncData(event: FormEvent) {
    event.preventDefault();
    if (!selectedSymbol || !syncStart || !syncEnd) { onNotice('请填写标的、开始日期和结束日期。', 'error'); return; }
    if (syncStart > syncEnd) { onNotice('开始日期不能晚于结束日期。', 'error'); return; }
    setSyncing(true); setDataActionError('');
    try {
      const payload = await api.syncMarket({ symbol: selectedSymbol, start: syncStart, end: syncEnd, provider });
      onSetMarket(payload); onRefresh();
      onNotice(`行情同步请求已完成，接口返回 ${payload.rows?.length ?? 0} 根数据。`);
    } catch (error) { const message = error instanceof Error ? error.message : '行情同步失败。'; setDataActionError(message); onNotice(message, 'error'); }
    finally { setSyncing(false); }
  }

  async function importCsv(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.querySelector<HTMLInputElement>('input[type=file]');
    const file = input?.files?.[0];
    if (!file) { onNotice('请先选择 CSV 文件。', 'error'); return; }
    setImporting(true); setDataActionError('');
    try {
      const payload = await api.importMarket(file, selectedSymbol, importSource);
      onSetMarket(payload); onRefresh();
      if (input) input.value = '';
      onNotice(`已提交导入，接口返回 ${payload.rows?.length ?? 0} 根有效数据。`);
    } catch (error) { const message = error instanceof Error ? error.message : 'CSV 导入失败。'; setDataActionError(message); onNotice(message, 'error'); }
    finally { setImporting(false); }
  }

  async function downloadBackup() {
    setDownloadBusy(true); setDataActionError('');
    try { const item = await api.backup(); downloadBlob(item.blob, item.filename); onNotice('备份文件已下载。'); }
    catch (error) { const message = error instanceof Error ? error.message : '备份下载失败。'; setDataActionError(message); onNotice(message, 'error'); }
    finally { setDownloadBusy(false); }
  }

  async function restoreBackup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.querySelector<HTMLInputElement>('input[type=file]');
    const file = input?.files?.[0];
    if (!file) { onNotice('请选择 JSON 备份文件。', 'error'); return; }
    try {
      const backup = JSON.parse(await file.text()) as unknown;
      if (!window.confirm('恢复备份会写入本地实验室数据库。请确认文件来自可信导出。继续恢复？')) return;
      setRestoreBusy(true); setDataActionError('');
      const result = await api.restore(backup);
      if (input) input.value = '';
      onNotice(result.message || '备份恢复请求已完成。');
      window.setTimeout(() => window.location.reload(), 650);
    } catch (error) { const message = error instanceof Error ? error.message : '备份文件无效或恢复失败。'; setDataActionError(message); onNotice(message, 'error'); }
    finally { setRestoreBusy(false); }
  }

  async function exportFills() {
    if (!activeAccountId) { onNotice('请先创建模拟账户。', 'error'); return; }
    setDownloadBusy(true);
    try { const item = await api.exportFills(activeAccountId); downloadBlob(item.blob, item.filename); onNotice('成交 CSV 已下载。'); }
    catch (error) { const message = error instanceof Error ? error.message : '成交导出失败。'; setDataActionError(message); onNotice(message, 'error'); }
    finally { setDownloadBusy(false); }
  }

  const selectedName = symbols.find((item) => item.symbol === selectedSymbol)?.name ?? selectedSymbol;
  const last = marketRows.at(-1);
  const pricePrecision = priceDigits(last?.close);
  const sourceLabel = market?.source ?? '尚无已载入行情';
  const adjustmentLabel = market?.adjustment ?? '未提供复权口径';
  return <div className="data-page">
    <section className="data-hero panel"><div className="data-hero-copy"><div className="data-hero-icon"><Database size={20} /></div><div><div className="eyebrow">MARKET DATA / LOCAL FIRST</div><h2>数据来源透明，账户边界清晰。</h2><p>行情更新会写入可追溯的数据集。已创建模拟账户使用各自冻结的行情快照，不随公共数据刷新而改变。</p></div></div><button className="button button-outline" onClick={onRefresh} disabled={loading} data-testid="refresh-market">{loading ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}刷新当前行情</button></section>
    <div className="data-grid"><section className="panel dataset-panel"><PanelTitle eyebrow="CURRENT DATASET" title={`${selectedName} · ${selectedSymbol}`} action={<span className={`status-pill ${marketRows.length ? 'good' : 'neutral'}`}>{loading ? '读取中' : marketRows.length ? `${marketRows.length} 根日K` : '暂无行情'}</span>} /><div className="dataset-metadata"><div><span>来源</span><b>{sourceLabel}</b></div><div><span>复权</span><b>{adjustmentLabel}</b></div><div><span>最近更新时间</span><b>{formatUpdatedAt(market?.updated_at)}</b></div><div><span>数据模式</span><b>{market?.mode ?? '—'}</b></div><div><span>数据集标识</span><b className="mono">{market?.dataset_id ?? '—'}</b></div><div><span>最后一根日K</span><b>{formatDate(last?.date)}</b></div></div>{market?.warning && <div className="inline-warning"><InfoIcon /><span>{market.warning}</span></div>}{dataActionError && <div className="inline-warning error-warning"><AlertTriangle size={15} /><span>{dataActionError}</span></div>}<div className="dataset-latest"><div><span>最新收盘</span><strong>{last ? `¥${formatMoney(last.close, pricePrecision)}` : '—'}</strong></div><div><span>最新成交量</span><strong>{last ? formatQuantity(last.volume) : '—'}</strong></div><div><span>最新成交额</span><strong>{last ? `¥${formatMoney(last.amount)}` : '—'}</strong></div></div></section>
    <section className="panel sync-panel"><PanelTitle eyebrow="DATA INGESTION" title="同步历史行情" /><p className="section-description">选择公开数据适配器和时间范围。同步结果会回显提供方返回行数与错误信息。</p><form className="sync-form" onSubmit={syncData}><label className="form-field"><span>数据提供方</span><select value={provider} onChange={(event) => setProvider(event.target.value as 'tushare' | 'akshare')}><option value="tushare">Tushare 适配器</option><option value="akshare">AkShare 适配器</option></select></label><div className="form-grid form-grid-two"><label className="form-field"><span>开始日期</span><input type="date" value={syncStart} onChange={(event) => { setSyncStart(event.target.value); localStorage.setItem('tl-sync-start', event.target.value); }} required /></label><label className="form-field"><span>结束日期</span><input type="date" value={syncEnd} onChange={(event) => setSyncEnd(event.target.value)} required /></label></div><button className="button button-primary" type="submit" disabled={syncing} data-testid="sync-market">{syncing ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}{syncing ? '同步中…' : '同步选定范围'}</button></form><div className="provider-notice"><ShieldAlert size={15} /><span>接口无权限、覆盖不足或返回空数据时，会明确显示为失败或缺失，不会补造行情。</span></div></section>
    <section className="panel import-panel"><PanelTitle eyebrow="LOCAL IMPORT" title="导入 CSV 行情" /><form className="import-form" onSubmit={importCsv}><label className="form-field"><span>关联标的</span><select value={selectedSymbol} disabled>{symbols.map((item) => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}</select></label><label className="form-field"><span>来源说明</span><input value={importSource} onChange={(event) => setImportSource(event.target.value)} placeholder="例如：券商导出、JoinQuant" maxLength={120} /></label><label className="file-drop"><input type="file" accept=".csv,text/csv" /><span className="file-drop-icon"><FileText size={18} /></span><span><b>选择 CSV 文件</b><small>需包含日期、开高低收等行情字段；由服务器进行完整校验</small></span><span className="file-extension">.CSV</span></label><button className="button button-outline" type="submit" disabled={importing} data-testid="import-market">{importing ? <LoaderCircle size={15} className="spin" /> : <FileDown size={15} />}导入并校验</button></form></section>
    <section className="panel exports-panel"><PanelTitle eyebrow="BACKUP & EXPORT" title="备份与导出" /><div className="export-row"><span className="export-icon backup"><Download size={16} /></span><span><b>完整数据库备份</b><small>导出本地实验室记录 JSON</small></span><button className="button button-outline button-small" onClick={downloadBackup} disabled={downloadBusy} data-testid="download-backup">下载备份</button></div><div className="export-row"><span className="export-icon csv"><FileDown size={16} /></span><span><b>成交记录 CSV</b><small>{activeAccountId ? '导出当前选中模拟账户成交' : '创建模拟账户后可导出'}</small></span><button className="button button-outline button-small" onClick={exportFills} disabled={downloadBusy || !activeAccountId} data-testid="export-fills">导出 CSV</button></div><form className="restore-row" onSubmit={restoreBackup}><label><span><b>恢复数据库备份</b><small>选择此前导出的 JSON 文件；恢复前会再次确认</small></span><input type="file" accept=".json,application/json" /></label><button className="button button-danger-quiet" type="submit" disabled={restoreBusy} data-testid="restore-backup">{restoreBusy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}恢复备份</button></form></section></div>
    <div className="data-footnote"><InfoIcon /><span>市场数据、观察持仓和模拟账户属于不同数据域。刷新行情只更新公开行情页；模拟账户严格读取自己创建时锁定的历史数据快照。</span></div>
  </div>;
}

function SettingsPage({ theme, setTheme, strategy, mode }: { theme: ThemeMode; setTheme: (theme: ThemeMode) => void; strategy?: Bootstrap['strategy']; mode: string }) {
  return <div className="settings-page"><section className="panel settings-intro"><div className="settings-icon"><Settings2 size={20} /></div><div><div className="eyebrow">WORKSPACE PREFERENCES</div><h2>按你的节奏整理研究台</h2><p>显示偏好保存在当前浏览器。本地研究数据仍由服务端数据库管理。</p></div><span className="badge badge-success"><i /> RESEARCH ONLY</span></section><div className="settings-grid"><section className="panel settings-card"><PanelTitle eyebrow="APPEARANCE" title="外观" /><div className="theme-choice-list"><button className={`theme-choice ${theme === 'light' ? 'active' : ''}`} onClick={() => setTheme('light')}><div className="theme-preview light-preview"><i /><i /><i /></div><span><b>浅色</b><small>明亮背景</small></span>{theme === 'light' && <CheckCircle2 size={17} />}</button><button className={`theme-choice ${theme === 'dark' ? 'active' : ''}`} onClick={() => setTheme('dark')}><div className="theme-preview dark-preview"><i /><i /><i /></div><span><b>深色</b><small>低亮度环境</small></span>{theme === 'dark' && <CheckCircle2 size={17} />}</button></div><div className="settings-local-note"><InfoIcon /><span>图表指标开关、缩放宽度与主题会保存在本机浏览器。</span></div></section><section className="panel settings-card"><PanelTitle eyebrow="STRATEGY SNAPSHOT" title="当前策略配置" /><div className="settings-strategy"><span>名称</span><b>{strategy?.name ?? '待配置'}</b><span>版本</span><b>{strategy?.version ?? '—'}</b><span>数据模式</span><b>{mode}</b><span>规则条目</span><b>{strategy?.rules?.length ?? '—'}</b></div>{strategy?.rules?.length ? <ul className="rule-list">{strategy.rules.map((rule, index) => <li key={index}>{rule}</li>)}</ul> : <p className="muted-small">暂无策略规则可展示，界面不会推断或补写规则。</p>}</section></div><section className="panel roadmap-panel"><PanelTitle eyebrow="PRODUCT PHASES" title="实验室功能阶段" /><div className="roadmap-grid"><div className="roadmap-phase current"><div className="roadmap-phase-top"><span>PHASE 01</span><b>当前阶段</b></div><h3>复盘与历史模拟</h3><p>多标的历史行情、七项日常复盘、计划风险预览、隔离账本、费用规则与统计。</p><ul><li><CheckCircle2 size={14} />本地历史数据</li><li><CheckCircle2 size={14} />历史交易日撮合</li><li><CheckCircle2 size={14} />账本统计与导出</li></ul></div><div className="roadmap-phase upcoming"><div className="roadmap-phase-top"><span>PHASE 02</span><b>规划中 · 尚未上线</b></div><h3>12周学习与回放训练</h3><p>下一步优先围绕12周学习计划展开，先完成4周日线训练；回放控制、交易结束复盘和截图管理仍在规划中，当前版本尚未提供。</p><ul><li><Clock3 size={14} />逐根回放速度、暂停与重置</li><li><Clock3 size={14} />完成交易后的结构化复盘</li><li><Clock3 size={14} />交易截图的管理与关联</li></ul></div><div className="roadmap-phase upcoming"><div className="roadmap-phase-top"><span>PHASE 03</span><b>远期探索</b></div><h3>扩展研究工作台</h3><p>继续保持研究和执行隔离，后续是否扩展需经过独立数据与风险验证。</p><ul><li><CircleHelp size={14} />功能范围未定</li><li><CircleHelp size={14} />准入条件未确认</li><li><CircleHelp size={14} />不代表未来已完成</li></ul></div></div></section></div>;
}
