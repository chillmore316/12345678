export type ThemeMode = 'light' | 'dark';
export type PageKey = 'overview' | 'review' | 'plans' | 'simulation' | 'statistics' | 'data' | 'settings';
export type Numeric = number | null | undefined;
export type LooseRecord = Record<string, unknown>;

export interface SymbolItem {
  symbol: string;
  name: string;
  kind?: string;
}

export interface Observation {
  symbol: string;
  quantity: number;
  reference_cost: number;
  note?: string;
  name?: string;
}

export interface AccountSummary {
  id: number | string;
  name: string;
  cursor_date?: string | null;
  initial_cash?: Numeric;
  cash?: Numeric;
  available_cash?: Numeric;
  equity?: Numeric;
  realized_pnl?: Numeric;
  unrealized_pnl?: Numeric;
  positions?: Position[];
  orders?: Order[];
  fills?: Fill[];
  cycles?: LooseRecord[];
  equity_curve?: CurvePoint[];
  [key: string]: unknown;
}

export interface AccountCreatePayload {
  name: string;
  initial_cash: number;
  start_date: string;
  request_id: string;
  commission_rate?: number;
  minimum_commission?: number;
  stock_stamp_duty_rate?: number;
  stock_transfer_fee_rate?: number;
}

export interface TrainingSession {
  id: number | string;
  account_id: number | string;
  name: string;
  symbol: string;
  start_date: string;
  max_date: string;
  cursor_date: string | null;
  status: 'active' | 'finished';
  parent_id?: number | string | null;
  [key: string]: unknown;
}

export interface TrainingSessionCreatePayload {
  name: string;
  symbol: string;
  start_date: string;
  initial_cash: number;
  request_id: string;
}

export interface TrainingMutationPayload {
  request_id: string;
}

export interface Position {
  symbol: string;
  quantity?: Numeric;
  sellable?: Numeric;
  avg_cost?: Numeric;
  market_value?: Numeric;
  unrealized_pnl?: Numeric;
  name?: string;
  [key: string]: unknown;
}

export interface Order {
  id?: number | string;
  order_id?: number | string;
  symbol?: string;
  side?: string;
  quantity?: Numeric;
  limit_price?: Numeric;
  stop_price?: Numeric;
  status?: string;
  created_at?: string;
  filled_quantity?: Numeric;
  plan_id?: number | string | null;
  [key: string]: unknown;
}

export interface Fill {
  id?: number | string;
  symbol?: string;
  side?: string;
  quantity?: Numeric;
  price?: Numeric;
  fill_price?: Numeric;
  filled_at?: string;
  date?: string;
  realized_pnl?: Numeric;
  pnl?: Numeric;
  plan_id?: number | string | null;
  [key: string]: unknown;
}

export interface CurvePoint {
  date?: string;
  timestamp?: number;
  value?: Numeric;
  equity?: Numeric;
  [key: string]: unknown;
}

export interface Bootstrap {
  symbols: SymbolItem[];
  observations: Observation[];
  accounts: AccountSummary[];
  reviews: Review[];
  plans: TradingPlan[];
  strategy?: { name?: string; version?: string; rules?: string[]; [key: string]: unknown };
  today?: string;
}

export interface MarketRow {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: Numeric;
  amount?: Numeric;
  pre_close?: Numeric;
  up_limit?: Numeric;
  down_limit?: Numeric;
}

export interface MarketPayload {
  rows: MarketRow[];
  source?: string;
  adjustment?: string;
  updated_at?: string;
  mode?: string;
  warning?: string;
  dataset_id?: string | number;
}

export interface AccountDetail extends AccountSummary {
  positions: Position[];
  orders: Order[];
  fills: Fill[];
  cycles: LooseRecord[];
  equity_curve: CurvePoint[];
}

export interface Review {
  date: string;
  account_id?: number | string | null;
  answers?: string[];
  tags?: string[];
  completed?: boolean;
  revision?: number;
  updated_at?: string;
  [key: string]: unknown;
}

export interface TradingPlan {
  id?: number | string;
  account_id?: number | string | null;
  symbol: string;
  strategy: string;
  market_trend: string;
  reason: string;
  entry_condition: string;
  entry_price: Numeric;
  stop_price: Numeric;
  target_price: Numeric;
  quantity: Numeric;
  discipline: string;
  status: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface PlanHistory {
  id?: number | string;
  plan_id?: number | string;
  event?: string;
  detail?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface AccountStats {
  totalfills?: Numeric;
  totalclosed?: Numeric;
  win_rate?: Numeric;
  avg_win?: Numeric;
  avg_loss?: Numeric;
  payoff_ratio?: Numeric;
  profit_factor?: Numeric;
  avg_r?: Numeric;
  cumulative_net?: Numeric;
  max_drawdown?: Numeric;
  streaks?: unknown;
  avg_holding_days?: Numeric;
  by_symbol?: Array<LooseRecord>;
  monthly?: Array<LooseRecord>;
  equity_curve?: CurvePoint[];
  [key: string]: unknown;
}

export interface ReviewPayload {
  answers: string[];
  tags: string[];
  completed: boolean;
}

export interface MarketSyncPayload {
  symbol: string;
  start: string;
  end: string;
  provider: 'tushare' | 'akshare';
}

export interface OrderPayload {
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  limit_price: number;
  stop_price: number | null;
  risk_pct: number;
  plan_id?: number | string;
  request_id: string;
}

export interface PlanPayload {
  symbol: string;
  strategy: string;
  market_trend: string;
  reason: string;
  entry_condition: string;
  entry_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  quantity: number | null;
  discipline: string;
  status: string;
}
