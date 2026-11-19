import type {
  AccountDetail,
  AccountCreatePayload,
  AccountStats,
  AccountSummary,
  Bootstrap,
  MarketPayload,
  MarketSyncPayload,
  OrderPayload,
  PlanHistory,
  PlanPayload,
  Review,
  ReviewPayload,
  TradingPlan,
  TrainingMutationPayload,
  TrainingSession,
  TrainingSessionCreatePayload,
} from './types';

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(message: string, status: number, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...init, headers });
  } catch (error) {
    throw new ApiError(error instanceof Error ? error.message : '网络连接失败', 0);
  }
  if (!response.ok) {
    let detail = '';
    let code = '';
    try {
      const body = await response.json() as { detail?: unknown; message?: unknown; code?: unknown };
      detail = String(body.detail ?? body.message ?? '');
      code = String(body.code ?? '');
    } catch {
      detail = await response.text().catch(() => '');
    }
    throw new ApiError(detail || `请求失败（${response.status}）`, response.status, code);
  }
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) return response.json() as Promise<T>;
  return response.text() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });

export const api = {
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  market: (symbol: string, end: string, accountId?: number | string) => {
    const query = new URLSearchParams({ end });
    if (accountId !== undefined && accountId !== null) query.set('account_id', String(accountId));
    return request<MarketPayload>(`/market/${encodeURIComponent(symbol)}?${query.toString()}`);
  },
  syncMarket: (body: MarketSyncPayload) => request<MarketPayload>(`/market/sync`, json(body)),
  importMarket: (file: File, symbol: string, source: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('symbol', symbol);
    form.append('source', source);
    return request<MarketPayload>('/market/import', { method: 'POST', body: form });
  },
  accounts: () => request<AccountSummary[]>('/accounts'),
  createAccount: (body: AccountCreatePayload) =>
    request<AccountDetail>('/accounts', json(body)),
  account: (id: number | string) => request<AccountDetail>(`/accounts/${encodeURIComponent(id)}`),
  placeOrder: (id: number | string, body: OrderPayload) => request<AccountDetail>(`/accounts/${encodeURIComponent(id)}/orders`, json(body)),
  cancelOrder: (id: number | string, orderId: number | string) =>
    request<AccountDetail>(`/accounts/${encodeURIComponent(id)}/orders/${encodeURIComponent(orderId)}/cancel`, { method: 'POST' }),
  advanceAccount: (id: number | string, request_id: string, expected_cursor?: string | null) =>
    request<AccountDetail>(`/accounts/${encodeURIComponent(id)}/advance`, json({ request_id, ...(expected_cursor !== undefined ? { expected_cursor } : {}) })),
  accountStats: (id: number | string) => request<AccountStats>(`/accounts/${encodeURIComponent(id)}/stats`),
  trainingSessions: () => request<TrainingSession[]>('/training-sessions'),
  createTrainingSession: (body: TrainingSessionCreatePayload) => request<TrainingSession>('/training-sessions', json(body)),
  trainingSession: (id: number | string) => request<TrainingSession>(`/training-sessions/${encodeURIComponent(id)}`),
  restartTrainingSession: (id: number | string, body: TrainingMutationPayload) => request<TrainingSession>(`/training-sessions/${encodeURIComponent(id)}/restart`, json(body)),
  finishTrainingSession: (id: number | string, body: TrainingMutationPayload) => request<TrainingSession>(`/training-sessions/${encodeURIComponent(id)}/finish`, json(body)),
  reviews: (search = '', accountId?: number | string | null) => {
    const query = new URLSearchParams({ search });
    if (accountId !== undefined && accountId !== null) query.set('account_id', String(accountId));
    return request<Review[]>(`/reviews?${query.toString()}`);
  },
  saveReview: (date: string, body: ReviewPayload, keepalive = false, accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<Review>(`/reviews/${encodeURIComponent(date)}${query}`, { method: 'PUT', body: JSON.stringify(body), keepalive });
  },
  deleteReview: (date: string, accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<void>(`/reviews/${encodeURIComponent(date)}${query}`, { method: 'DELETE' });
  },
  plans: (accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<TradingPlan[]>(`/plans${query}`);
  },
  createPlan: (body: PlanPayload, accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<TradingPlan>(`/plans${query}`, json(body));
  },
  updatePlan: (id: number | string, body: Partial<PlanPayload>, accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<TradingPlan>(`/plans/${encodeURIComponent(id)}${query}`, { method: 'PUT', body: JSON.stringify(body) });
  },
  deletePlan: (id: number | string, accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<void>(`/plans/${encodeURIComponent(id)}${query}`, { method: 'DELETE' });
  },
  planHistory: (id: number | string, accountId?: number | string | null) => {
    const query = accountId === undefined || accountId === null ? '' : `?account_id=${encodeURIComponent(String(accountId))}`;
    return request<PlanHistory[]>(`/plans/${encodeURIComponent(id)}/history${query}`);
  },
  backup: () => fetchDownload('/backup'),
  exportFills: (accountId: number | string) => fetchDownload(`/export/fills.csv?account_id=${encodeURIComponent(accountId)}`),
  restore: (backup: unknown) => request<{ restored?: boolean; message?: string }>('/restore', json({ backup })),
};

async function fetchDownload(path: string): Promise<{ blob: Blob; filename: string }> {
  const response = await fetch(`/api${path}`);
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new ApiError(text || `下载失败（${response.status}）`, response.status);
  }
  const disposition = response.headers.get('content-disposition') ?? '';
  const filename = disposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i)?.[1] ?? '';
  return { blob: await response.blob(), filename: decodeURIComponent(filename) || (path.includes('csv') ? 'fills.csv' : 'trading-lab-backup.json') };
}
