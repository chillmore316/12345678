import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ActionType,
  dispose,
  init,
  registerOverlay,
  type Chart,
  type KLineData,
  type OverlayCreate,
  type OverlayCreateFiguresCallbackParams,
  type OverlayFigure,
} from 'klinecharts';
import { Download, Eye, EyeOff, ImageDown, Info } from 'lucide-react';
import type { Fill, MarketRow, ThemeMode } from './types';
import { dateStamp, fillDate, fillPnl, fillPrice, fillSide, formatDate, formatMoney, formatQuantity, formatUpdatedAt, marketRowsBefore } from './utils';

type TradeMarker = {
  id: string;
  date: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  pnl?: number | null;
  planId?: string | number | null;
};

type PriceLevel = { label: string; value: number | null | undefined; color: string };

interface KlinePanelProps {
  rows: MarketRow[];
  symbol: string;
  symbolName: string;
  source?: string;
  adjustment?: string;
  updatedAt?: string;
  warning?: string;
  theme: ThemeMode;
  fills?: Fill[];
  endDate?: string;
  levels?: PriceLevel[];
  emptyLabel?: string;
  cutoffLabel?: string;
  unit?: string;
}

let overlaysRegistered = false;
function ensureTradingOverlays() {
  if (overlaysRegistered) return;
  registerOverlay({
    name: 'tradingLabTradeMarker',
    totalStep: 1,
    lock: false,
    needDefaultPointFigure: false,
    createPointFigures: ({ coordinates, overlay }: OverlayCreateFiguresCallbackParams) => {
      const point = coordinates[0];
      if (!point) return [];
      const data = (overlay.extendData ?? {}) as { color?: string; text?: string };
      const color = data.color ?? '#60728a';
      const result: OverlayFigure[] = [
        {
          type: 'circle',
          attrs: { x: point.x, y: point.y, r: 6 },
          styles: { style: 'fill', color, borderColor: '#fff', borderSize: 2, borderStyle: 'solid', borderDashedValue: [] },
        },
      ];
      if (data.text) {
        result.push({
          type: 'text',
          attrs: { x: point.x, y: point.y - 11, text: data.text, align: 'center', baseline: 'bottom' },
          styles: { style: 'fill', color, size: 10, family: 'Inter, sans-serif', weight: 700, backgroundColor: 'transparent', borderColor: 'transparent', borderSize: 0, borderRadius: 0, borderStyle: 'solid', borderDashedValue: [], paddingLeft: 2, paddingRight: 2, paddingTop: 1, paddingBottom: 1 },
        });
      }
      return result;
    },
  });
  registerOverlay({
    name: 'tradingLabPriceLevel',
    totalStep: 1,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, bounding, overlay }: OverlayCreateFiguresCallbackParams) => {
      const point = coordinates[0];
      if (!point) return [];
      const data = (overlay.extendData ?? {}) as { color?: string; label?: string };
      const color = data.color ?? '#8a95a5';
      return [
        { type: 'line', attrs: { coordinates: [{ x: bounding.left, y: point.y }, { x: bounding.right, y: point.y }] }, styles: { style: 'dashed', size: 1, color, dashedValue: [4, 4], smooth: false } },
        { type: 'text', attrs: { x: bounding.right - 4, y: point.y - 7, text: data.label ?? '', align: 'right', baseline: 'bottom' }, styles: { style: 'fill', color, size: 10, family: 'Inter, sans-serif', weight: 600, backgroundColor: 'transparent', borderColor: 'transparent', borderSize: 0, borderRadius: 0, borderStyle: 'solid', borderDashedValue: [], paddingLeft: 2, paddingRight: 2, paddingTop: 1, paddingBottom: 1 } },
      ];
    },
  });
  overlaysRegistered = true;
}

function makeMarker(fill: Fill, index: number): TradeMarker | null {
  const side = fillSide(fill);
  const price = fillPrice(fill);
  const quantity = fill.quantity;
  const date = fillDate(fill).slice(0, 10);
  if (!side || typeof price !== 'number' || !Number.isFinite(price) || typeof quantity !== 'number' || !Number.isFinite(quantity) || !date) return null;
  return {
    id: String(fill.id ?? `${date}-${side}-${index}`),
    date,
    symbol: String(fill.symbol ?? ''),
    side,
    quantity,
    price,
    pnl: fillPnl(fill),
    planId: fill.plan_id,
  };
}

function sourceLabel(source: string): string {
  const value = source.toLowerCase();
  if (value.includes('tushare')) return 'Tushare 兼容中转';
  if (value.includes('akshare')) return 'AkShare';
  if (value.includes('csv') || value.includes('import')) return '本地导入';
  return source;
}

function adjustmentLabel(adjustment: string): string {
  const value = adjustment.toLowerCase();
  if (['raw', 'none', 'unadjusted', '未复权'].includes(value)) return '未复权';
  if (value === 'qfq' || value.includes('前复权')) return '前复权';
  if (value === 'hfq' || value.includes('后复权')) return '后复权';
  return adjustment;
}

function timeLabel(updatedAt?: string): string {
  return updatedAt ? `更新时间 ${formatUpdatedAt(updatedAt)}` : '更新时间待提供';
}

export function KlinePanel({ rows, symbol, symbolName, source, adjustment, updatedAt, warning, theme, fills = [], endDate, levels = [], emptyLabel, cutoffLabel, unit = '股' }: KlinePanelProps) {
  const chartContainer = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const [showMA, setShowMA] = useState(() => localStorage.getItem('tl-chart-ma') !== 'false');
  const [showVOL, setShowVOL] = useState(() => localStorage.getItem('tl-chart-vol') !== 'false');
  const [zoomNotice, setZoomNotice] = useState('');
  const [activeTrade, setActiveTrade] = useState<TradeMarker | null>(null);
  const [hoverBar, setHoverBar] = useState<KLineData | null>(null);
  const visibleRows = useMemo(() => marketRowsBefore(rows, endDate), [rows, endDate]);
  const chartRows: KLineData[] = useMemo(() => visibleRows.map((row) => ({
    timestamp: dateStamp(row.date),
    open: Number(row.open),
    high: Number(row.high),
    low: Number(row.low),
    close: Number(row.close),
    volume: typeof row.volume === 'number' ? row.volume : undefined,
    turnover: typeof row.amount === 'number' ? row.amount : undefined,
  })).filter((row) => Number.isFinite(row.timestamp) && [row.open, row.high, row.low, row.close].every(Number.isFinite)), [visibleRows]);
  const markerData = useMemo(() => fills.map(makeMarker).filter((item): item is TradeMarker => Boolean(item))
    .filter((item) => item.symbol === symbol && (!endDate || item.date <= endDate.slice(0, 10)) && chartRows.some((row) => new Date(row.timestamp).toISOString().slice(0, 10) === item.date)), [fills, symbol, endDate, chartRows]);
  const precision = useMemo(() => {
    const close = chartRows.at(-1)?.close ?? 0;
    const decimals = String(close).split('.')[1]?.length ?? 0;
    return Math.max(2, Math.min(4, decimals));
  }, [chartRows]);
  const lastRow = visibleRows.at(-1);
  const prevClose = lastRow?.pre_close;
  const change = typeof prevClose === 'number' && prevClose > 0 && typeof lastRow?.close === 'number' ? ((lastRow.close / prevClose) - 1) * 100 : null;
  const hoverDate = hoverBar?.timestamp ? new Date(hoverBar.timestamp).toISOString().slice(0, 10) : '';

  useEffect(() => {
    if (!chartContainer.current) return;
    ensureTradingOverlays();
    const chart = init(chartContainer.current, {
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      styles: theme === 'dark' ? 'dark' : 'light',
      decimalFoldThreshold: 0,
      customApi: {
        formatDate: (_dateTimeFormat, timestamp) => {
          const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(timestamp);
          const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
          return `${values.year}-${values.month}-${values.day}`;
        },
      },
    });
    if (!chart) return;
    chartRef.current = chart;
    chart.setPriceVolumePrecision(precision, 0);
    if (showMA) chart.createIndicator({ name: 'MA', calcParams: [5, 10, 20, 60] }, false, { id: 'candle_pane' });
    if (showVOL) chart.createIndicator('VOL', false, { id: 'volume-pane', height: 88, minHeight: 60 });
    const savedBarSpace = Number(localStorage.getItem('tl-chart-bar-space'));
    if (Number.isFinite(savedBarSpace) && savedBarSpace > 0) chart.setBarSpace(Math.max(4, Math.min(18, savedBarSpace)));
    const onCrosshair = (data?: { kLineData?: KLineData | null }) => setHoverBar(data?.kLineData ?? null);
    const onZoom = () => {
      if (!chartRef.current) return;
      localStorage.setItem('tl-chart-bar-space', String(chartRef.current.getBarSpace()));
      setZoomNotice('视图已记住');
      window.setTimeout(() => setZoomNotice(''), 1200);
    };
    chart.subscribeAction(ActionType.OnCrosshairChange, onCrosshair);
    chart.subscribeAction(ActionType.OnZoom, onZoom);
    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(chartContainer.current);
    return () => {
      resizeObserver.disconnect();
      chart.unsubscribeAction(ActionType.OnCrosshairChange, onCrosshair);
      chart.unsubscribeAction(ActionType.OnZoom, onZoom);
      dispose(chart);
      chartRef.current = null;
    };
  }, [theme]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.setPriceVolumePrecision(precision, 0);
    chart.applyNewData(chartRows, false);
    if (showMA) {
      chart.removeIndicator('candle_pane', 'MA');
      chart.createIndicator({ name: 'MA', calcParams: [5, 10, 20, 60] }, false, { id: 'candle_pane' });
    } else chart.removeIndicator('candle_pane', 'MA');
    if (showVOL) {
      chart.removeIndicator('volume-pane');
      chart.createIndicator('VOL', false, { id: 'volume-pane', height: 88, minHeight: 60 });
    } else chart.removeIndicator('volume-pane');
    chart.removeOverlay({ groupId: 'trade-fills' });
    chart.removeOverlay({ groupId: 'trade-levels' });
    for (const trade of markerData) {
      const timestamp = dateStamp(trade.date);
      const marker: OverlayCreate = {
        name: 'tradingLabTradeMarker',
        id: `fill-${trade.id}`,
        groupId: 'trade-fills',
        lock: true,
        points: [{ timestamp, value: trade.price }],
        extendData: { color: trade.side === 'BUY' ? '#219c75' : '#dd6269', text: trade.side === 'BUY' ? '买' : '卖' },
        onMouseEnter: () => { setActiveTrade(trade); return true; },
        onMouseLeave: () => { setActiveTrade((current) => current?.id === trade.id ? null : current); return true; },
        onClick: () => { setActiveTrade(trade); return true; },
      };
      chart.createOverlay(marker, 'candle_pane');
    }
    const firstTimestamp = chartRows[0]?.timestamp;
    if (firstTimestamp) {
      for (const [index, level] of levels.filter((item) => typeof item.value === 'number' && Number.isFinite(item.value)).entries()) {
        chart.createOverlay({
          name: 'tradingLabPriceLevel',
          id: `level-${index}-${symbol}`,
          groupId: 'trade-levels',
          lock: true,
          points: [{ timestamp: firstTimestamp, value: level.value as number }],
          extendData: { color: level.color, label: `${level.label} ${formatMoney(level.value, precision)}` },
        }, 'candle_pane');
      }
    }
    if (chartRows.length) chart.scrollToRealTime(0);
    setActiveTrade(null);
  }, [chartRows, markerData, levels, showMA, showVOL, precision, symbol, theme]);

  useEffect(() => {
    localStorage.setItem('tl-chart-ma', String(showMA));
  }, [showMA]);
  useEffect(() => {
    localStorage.setItem('tl-chart-vol', String(showVOL));
  }, [showVOL]);

  function saveChartImage() {
    const url = chartRef.current?.getConvertPictureUrl(true, 'png', theme === 'dark' ? '#111923' : '#ffffff');
    if (!url) return;
    const link = document.createElement('a');
    link.href = url;
    link.download = `${symbol}-${endDate ?? 'history'}-chart.png`;
    link.click();
  }

  return (
    <section className="panel chart-panel" data-testid="kline-panel">
      <div className="panel-head chart-panel-head">
        <div>
          <div className="eyebrow">PRICE ACTION</div>
          <h2>{symbolName} <span className="muted mono">{symbol}</span></h2>
          <div className="chart-subline">
            <span title={source || ''}>{source ? `${sourceLabel(source)} · ${source}` : '来源待提供'}</span><span className="dot-separator" />
            <span>{adjustment ? adjustmentLabel(adjustment) : '复权口径待提供'}</span><span className="dot-separator" />
            <span>更新 {timeLabel(updatedAt)}</span>
          </div>
        </div>
        <button className="icon-button chart-export" onClick={saveChartImage} title="下载图表 PNG" aria-label="下载图表 PNG" data-testid="download-chart">
          <ImageDown size={17} /><span>导出图表</span>
        </button>
      </div>
      {warning && <div className="inline-warning"><Info size={15} /><span>{warning}</span></div>}
      {cutoffLabel && <div className="chart-cutoff"><span className="cutoff-dot" />{cutoffLabel}</div>}
      <div className="chart-tools">
        <div className="chart-price">
          <div className="price-main">{typeof lastRow?.close === 'number' ? formatMoney(lastRow.close, precision) : '—'}</div>
          <div className="price-meta">最新收盘 {formatDate(lastRow?.date)}
            {typeof change === 'number' && <span className={change >= 0 ? 'change-up' : 'change-down'}> {change >= 0 ? '+' : ''}{change.toFixed(2)}%</span>}
          </div>
        </div>
        <div className="chart-tool-actions">
          <button type="button" className={`chip-button ${showMA ? 'selected' : ''}`} onClick={() => setShowMA((value) => !value)} data-testid="toggle-ma">{showMA ? <Eye size={13} /> : <EyeOff size={13} />} MA 5 / 10 / 20 / 60</button>
          <button type="button" className={`chip-button ${showVOL ? 'selected' : ''}`} onClick={() => setShowVOL((value) => !value)} data-testid="toggle-volume">{showVOL ? <Eye size={13} /> : <EyeOff size={13} />} 成交量</button>
          {zoomNotice && <span className="tiny-saved">{zoomNotice}</span>}
        </div>
      </div>
      <div className="kline-stage">
        <div className="kline-canvas" ref={chartContainer} aria-label={`${symbolName} 日K线图`} data-testid="kline-canvas" />
        {!chartRows.length && <div className="chart-empty" data-testid="chart-empty"><div className="empty-icon"><Download size={19} /></div><strong>{emptyLabel || '暂无可展示的历史行情'}</strong><span>图表只绘制接口返回的真实数据。请到「数据管理」同步或导入行情。</span></div>}
      </div>
      {hoverBar && <div className="ohlc-row" data-testid="crosshair-details">
        <span>{formatDate(hoverDate)}</span><span>开 <b>{formatMoney(hoverBar.open, precision)}</b></span><span>高 <b>{formatMoney(hoverBar.high, precision)}</b></span><span>低 <b>{formatMoney(hoverBar.low, precision)}</b></span><span>收 <b>{formatMoney(hoverBar.close, precision)}</b></span><span>量 <b>{formatQuantity(hoverBar.volume)}</b></span>
      </div>}
      {activeTrade && <div className="trade-tooltip" data-testid="trade-tooltip">
        <div className={`trade-tooltip-marker ${activeTrade.side.toLowerCase()}`} />
        <div className="trade-tooltip-main"><strong>{activeTrade.side === 'BUY' ? '买入' : '卖出'} · {activeTrade.symbol}</strong><span>{formatDate(activeTrade.date)} · {formatQuantity(activeTrade.quantity)} {unit} · ¥{formatMoney(activeTrade.price)}</span></div>
        <div className="trade-tooltip-meta"><span>已实现盈亏</span><b className={typeof activeTrade.pnl === 'number' ? activeTrade.pnl >= 0 ? 'positive' : 'negative' : ''}>{formatMoney(activeTrade.pnl)}</b></div>
        <div className="trade-tooltip-meta"><span>关联计划</span><b>{activeTrade.planId ?? '未关联'}</b></div>
      </div>}
      <div className="chart-legend">
        <span><i className="legend-candle" />日K</span><span><i className="legend-line ma5" />MA5</span><span><i className="legend-line ma10" />MA10</span><span><i className="legend-line ma20" />MA20</span><span><i className="legend-line ma60" />MA60</span><span><i className="legend-bar" />VOL</span>
      </div>
      {markerData.length > 0 && <div className="chart-trades" data-testid="chart-trades">
        <div className="chart-trades-title"><span>账户成交标记</span><small>共 {markerData.length} 笔 · 仅当前账户 · 截至 {formatDate(endDate)}</small></div>
        <div className="trade-chip-list">{markerData.slice(-8).reverse().map((trade) => (
          <button className="trade-chip" key={trade.id} onClick={() => setActiveTrade(trade)} title="查看成交明细">
            <span className={`side-pill ${trade.side.toLowerCase()}`}>{trade.side === 'BUY' ? '买' : '卖'}</span><span>{formatDate(trade.date)}</span><b>¥{formatMoney(trade.price, precision)}</b><span>{formatQuantity(trade.quantity)} {unit}</span>{typeof trade.pnl === 'number' && <strong className={trade.pnl >= 0 ? 'positive' : 'negative'}>{trade.pnl >= 0 ? '+' : ''}{formatMoney(trade.pnl)}</strong>}
          </button>
        ))}</div>
      </div>}
    </section>
  );
}
