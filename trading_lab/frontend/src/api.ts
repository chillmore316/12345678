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
} from './types';

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
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
    try {
      const body = await response.json() as { detail?: unknown; message?: unknown };
      detail = String(body.detail ?? body.message ?? '');
    } catch {
      detail = await response.text().catch(() => '');
    }
    throw new ApiError(detail || `请求失败（${response.status}）`, response.status);
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
  advanceAccount: (id: number | string, request_id: string) =>
    request<AccountDetail>(`/accounts/${encodeURIComponent(id)}/advance`, json({ request_id })),
  accountStats: (id: number | string) => request<AccountStats>(`/accounts/${encodeURIComponent(id)}/stats`),
  reviews: (search = '') => request<Review[]>(`/reviews?search=${encodeURIComponent(search)}`),
  saveReview: (date: string, body: ReviewPayload, keepalive = false) => request<Review>(`/reviews/${encodeURIComponent(date)}`, { method: 'PUT', body: JSON.stringify(body), keepalive }),
  deleteReview: (date: string) => request<void>(`/reviews/${encodeURIComponent(date)}`, { method: 'DELETE' }),
  plans: () => request<TradingPlan[]>('/plans'),
  createPlan: (body: PlanPayload) => request<TradingPlan>('/plans', json(body)),
  updatePlan: (id: number | string, body: Partial<PlanPayload>) => request<TradingPlan>(`/plans/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) }),
  deletePlan: (id: number | string) => request<void>(`/plans/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  planHistory: (id: number | string) => request<PlanHistory[]>(`/plans/${encodeURIComponent(id)}/history`),
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
