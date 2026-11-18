import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
import type { ThemeMode } from './types';

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

export function EChart({ option, theme, className = '' }: { option: EChartsOption; theme: ThemeMode; className?: string }) {
  const host = useRef<HTMLDivElement | null>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!host.current) return;
    chart.current = echarts.init(host.current, theme === 'dark' ? 'dark' : undefined, { renderer: 'canvas' });
    const resize = new ResizeObserver(() => chart.current?.resize());
    resize.observe(host.current);
    return () => {
      resize.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, [theme]);
  useEffect(() => {
    chart.current?.setOption({ backgroundColor: 'transparent', ...option }, { notMerge: true, lazyUpdate: true });
  }, [option, theme]);
  return <div className={`echart ${className}`} ref={host} role="img" aria-label="账户统计图表" />;
}
