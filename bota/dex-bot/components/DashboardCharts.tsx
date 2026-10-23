"use client";

import type { OHLCVCandle } from "@/lib/geckoterminal";

type DashboardChartsProps = {
  chartData: OHLCVCandle[];
};

const WIDTH = 820;
const HEIGHT = 340;
const PADDING = { top: 18, right: 76, bottom: 34, left: 16 };

export default function DashboardCharts({ chartData }: DashboardChartsProps) {
  if (chartData.length === 0) {
    return (
      <div className="flex h-80 items-center justify-center rounded-2xl border border-white/10 bg-slate-900 text-sm text-slate-400">
        Brak danych świecowych dla tej puli.
      </div>
    );
  }

  const minPrice = Math.min(...chartData.map((candle) => candle.low));
  const maxPrice = Math.max(...chartData.map((candle) => candle.high));
  const priceRange = maxPrice - minPrice || Math.max(maxPrice * 0.02, 1e-12);
  const plotWidth = WIDTH - PADDING.left - PADDING.right;
  const plotHeight = HEIGHT - PADDING.top - PADDING.bottom;
  const candleStep = plotWidth / chartData.length;
  const candleWidth = Math.max(1, Math.min(8, candleStep * 0.68));
  const y = (price: number) =>
    PADDING.top + ((maxPrice - price) / priceRange) * plotHeight;
  const formatPrice = (price: number) =>
    price.toLocaleString("en-US", { maximumSignificantDigits: 5 });
  const firstTime = new Date(chartData[0].timestamp * 1000)
    .toISOString()
    .slice(11, 16);
  const lastTime = new Date(chartData[chartData.length - 1].timestamp * 1000)
    .toISOString()
    .slice(11, 16);

  return (
    <div className="rounded-2xl border border-white/10 bg-slate-900 p-3 sm:p-5">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="font-semibold text-white">Świece 15 min</h3>
          <p className="text-xs text-slate-400">Ostatnie 24 godziny · USD</p>
        </div>
        <span className="text-xs text-slate-500">
          {chartData.length} świec
        </span>
      </div>
      <div className="w-full overflow-hidden">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="h-auto w-full"
          role="img"
          aria-label="Świecowy wykres ceny tokena z ostatnich 24 godzin"
        >
          <title>Świecowy wykres ceny tokena z ostatnich 24 godzin</title>
          {[0, 1, 2, 3].map((step) => {
            const price = maxPrice - (priceRange * step) / 3;
            const lineY = y(price);
            return (
              <g key={step}>
                <line
                  x1={PADDING.left}
                  x2={WIDTH - PADDING.right}
                  y1={lineY}
                  y2={lineY}
                  stroke="#1e293b"
                  strokeDasharray="4 5"
                />
                <text
                  x={WIDTH - PADDING.right + 8}
                  y={lineY + 4}
                  fill="#94a3b8"
                  fontSize="11"
                >
                  {formatPrice(price)}
                </text>
              </g>
            );
          })}
          {chartData.map((candle, index) => {
            const centerX = PADDING.left + candleStep * (index + 0.5);
            const rising = candle.close >= candle.open;
            const color = rising ? "#34d399" : "#fb7185";
            const bodyTop = y(Math.max(candle.open, candle.close));
            const bodyBottom = y(Math.min(candle.open, candle.close));
            return (
              <g key={`${candle.timestamp}-${index}`}>
                <line
                  x1={centerX}
                  x2={centerX}
                  y1={y(candle.high)}
                  y2={y(candle.low)}
                  stroke={color}
                  strokeWidth="1"
                />
                <rect
                  x={centerX - candleWidth / 2}
                  y={bodyTop}
                  width={candleWidth}
                  height={Math.max(bodyBottom - bodyTop, 1)}
                  fill={color}
                  rx="0.5"
                />
              </g>
            );
          })}
          <text
            x={PADDING.left}
            y={HEIGHT - 8}
            fill="#64748b"
            fontSize="11"
            textAnchor="start"
          >
            {firstTime} UTC
          </text>
          <text
            x={WIDTH - PADDING.right}
            y={HEIGHT - 8}
            fill="#64748b"
            fontSize="11"
            textAnchor="end"
          >
            {lastTime} UTC
          </text>
        </svg>
      </div>
    </div>
  );
}
