import type { AccountDetail, CurvePoint, Fill, LooseRecord, MarketRow, Numeric, Order, PlanHistory, Position, Review, TradingPlan } from './types';

export const REVIEW_QUESTIONS = [
  '今天大盘是什么趋势？',
  '今天哪些板块比较强？',
  '赛腾股份今天表现如何？',
  '是否符合我的波段策略？',
  '今天有没有交易？为什么？',
  '今天犯了什么错误？',
  '明天重点观察什么？',
];

export const REVIEW_TAGS = ['遵守计划', '情绪稳定', '追涨', '过早止盈', '止损拖延', '错过机会', '等待确认', '空仓观察'];

export const PAGE_TITLES: Record<string, { title: string; eyebrow: string; description: string }> = {
  overview: { title: '总览', eyebrow: 'TRADING LAB / HOME', description: '把数据、计划与复盘放在同一条研究线上。' },
  review: { title: '每日复盘', eyebrow: 'JOURNAL / 01', description: '记录决策过程，让每次交易都留下可追溯的依据。' },
  plans: { title: '交易计划', eyebrow: 'PLANNING / 02', description: '先写清条件和风险，再考虑执行。计划不会自动下单。' },
  simulation: { title: '历史模拟', eyebrow: 'SIMULATION / 03', description: '仅按已验证的历史交易日与开盘数据推进账户。' },
  statistics: { title: '账户统计', eyebrow: 'ANALYTICS / 04', description: '统计只读取当前模拟账户的账本结果。' },
  data: { title: '数据管理', eyebrow: 'MARKET DATA / 05', description: '查看来源、更新时间，并管理历史行情导入与备份。' },
  settings: { title: '设置', eyebrow: 'PREFERENCES / 06', description: '调整本地显示偏好，查看实验室功能阶段。' },
};

export const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export function formatMoney(value: Numeric, digits = 2): string {
  if (!finite(value)) return '—';
  return new Intl.NumberFormat('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

export function formatQuantity(value: Numeric): string {
  if (!finite(value)) return '—';
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value);
}

export function formatPercent(value: Numeric, fraction = true): string {
  if (!finite(value)) return '—';
  const percent = fraction ? value * 100 : value;
  return `${percent.toFixed(2)}%`;
}

export function formatDate(value?: string | null): string {
  if (!value) return '—';
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1]}.${match[2]}.${match[3]}` : value;
}

export function formatUpdatedAt(value?: string | null): string {
  if (!value) return '—';
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const timestamp = new Date(hasTimezone ? value : `${value}Z`);
  if (!Number.isFinite(timestamp.getTime())) return `${value.replace('T', ' ').slice(0, 16)} UTC`;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(timestamp);
  const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day} ${fields.hour}:${fields.minute} 北京时间`;
}

export function dateStamp(date: string): number {
  return Date.parse(`${date}T12:00:00+08:00`);
}

export function normalizeStatus(status?: string): string {
  if (!status) return '未知状态';
  const labels: Record<string, string> = {
    pending: '待撮合', open: '待撮合', submitted: '已提交', accepted: '已受理', filled: '已成交', partial: '部分成交', partially_filled: '部分成交', expired: '未成交已到期', partially_filled_expired: '部分成交，余单到期', cancelled: '已撤销', rejected: '已拒绝',
    planned: '计划中', active: '执行中', done: '已完成', completed: '已完成', canceled: '已取消', draft: '草稿',
  };
  return labels[status.toLowerCase()] ?? status;
}

export function statusTone(status?: string): 'good' | 'warning' | 'bad' | 'neutral' {
  const value = status?.toLowerCase() ?? '';
  if (['filled', 'accepted', 'completed', 'done', 'good', 'success'].includes(value)) return 'good';
  if (['pending', 'open', 'submitted', 'accepted', 'partially_filled', 'partial', 'planned', 'active', 'warning'].includes(value)) return 'warning';
  if (['rejected', 'cancelled', 'canceled', 'failed', 'bad', 'error'].includes(value)) return 'bad';
  return 'neutral';
}

export function objectLabel(record: LooseRecord, ...keys: string[]): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'string' && value) return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  }
  return '—';
}

export function rowId(row: LooseRecord, fallback: number): string {
  return objectLabel(row, 'id', 'order_id', 'fill_id', 'date') === '—' ? String(fallback) : objectLabel(row, 'id', 'order_id', 'fill_id', 'date');
}

export function orderId(order: Order): string | number | undefined {
  return order.id ?? order.order_id;
}

export function fillDate(fill: Fill): string {
  return String(fill.date ?? fill.filled_at ?? '');
}

export function fillPrice(fill: Fill): Numeric {
  return fill.price ?? fill.fill_price;
}

export function fillPnl(fill: Fill): Numeric {
  return fill.realized_pnl ?? fill.pnl;
}

export function fillSide(fill: Fill): 'BUY' | 'SELL' | '' {
  const side = String(fill.side ?? '').toUpperCase();
  return side === 'BUY' || side === 'SELL' ? side : '';
}

export function accountId(account: AccountDetail): string {
  return String(account.id);
}

export function curveRows(rows: CurvePoint[] = []): Array<{ date: string; value: number }> {
  return rows.flatMap((row) => {
    const value = row.equity ?? row.value;
    const date = String(row.date ?? (finite(row.timestamp) ? new Date(row.timestamp).toISOString().slice(0, 10) : ''));
    return finite(value) && date ? [{ date, value }] : [];
  });
}

export function planId(plan: TradingPlan): string | number | undefined {
  return plan.id;
}

export function historyLabel(row: PlanHistory): string {
  return String(row.created_at ?? row.event ?? row.detail ?? '计划历史记录');
}

export function positionName(position: Position, names: Map<string, string>): string {
  return position.name ?? names.get(position.symbol) ?? position.symbol;
}

export function reviewPayload(review: Review): string[] {
  return Array.isArray(review.answers) ? Array.from({ length: 7 }, (_, index) => String(review.answers?.[index] ?? '')) : Array.from({ length: 7 }, (_, index) => String(review[`q${index + 1}`] ?? ''));
}

export function marketRowsBefore(rows: MarketRow[], endDate?: string): MarketRow[] {
  if (!endDate) return rows;
  return rows.filter((row) => row.date.slice(0, 10) <= endDate.slice(0, 10));
}

export interface RiskFeeSchedule {
  commissionRate: number;
  minimumCommission: number;
  stampDutyRate: number;
  transferFeeRate: number;
}

/** Maximum whole 100-share lots within both the stop-risk budget and available cash. */
export function estimateMaxRiskQuantity(
  entryPrice: number,
  stopPrice: number,
  riskBudget: number,
  availableCash: number,
  fees: RiskFeeSchedule,
  isStock: boolean,
): number {
  if (![entryPrice, stopPrice, riskBudget, availableCash, fees.commissionRate, fees.minimumCommission, fees.stampDutyRate, fees.transferFeeRate].every(Number.isFinite)
    || entryPrice <= 0 || stopPrice < 0 || stopPrice >= entryPrice || riskBudget <= 0 || availableCash <= 0) return 0;

  const commission = (notional: number) => Math.max(fees.minimumCommission, notional * fees.commissionRate);
  const maxLotsByCash = Math.floor(availableCash / (entryPrice * 100));
  let low = 0;
  let high = maxLotsByCash;
  while (low < high) {
    const lots = Math.ceil((low + high) / 2);
    const quantity = lots * 100;
    const entryNotional = entryPrice * quantity;
    const stopNotional = stopPrice * quantity;
    const risk = (entryPrice - stopPrice) * quantity
      + commission(entryNotional)
      + commission(stopNotional)
      + (isStock ? stopNotional * fees.stampDutyRate + (entryNotional + stopNotional) * fees.transferFeeRate : 0);
    const cashRequired = entryNotional + commission(entryNotional) + (isStock ? entryNotional * fees.transferFeeRate : 0);
    if (risk <= riskBudget + 1e-9 && cashRequired <= availableCash + 1e-9) low = lots;
    else high = lots - 1;
  }
  return low * 100;
}
