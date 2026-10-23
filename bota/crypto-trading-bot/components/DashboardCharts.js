'use client';

import { Line } from 'react-chartjs-2';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend
} from 'chart.js';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend);

export default function DashboardCharts({ chartData }) {
  if (!chartData || chartData.length === 0) {
    return <div className="text-gray-400 p-4">Brak danych wykresu. Wklej kontrakt tokena.</div>;
  }

  const data = {
    labels: chartData.map(c => new Date(c.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
    datasets: [
      {
        label: 'Cena USD (24h)',
        data: chartData.map(c => c.close),
        borderColor: '#22c55e',
        backgroundColor: 'rgba(34, 197, 94, 0.1)',
        tension: 0.2,
        fill: true,
      },
    ],
  };

  return (
    <div className="bg-gray-900 p-4 rounded-xl border border-gray-800">
      <h3 className="text-white font-bold mb-4">Wykres Ceny (GeckoTerminal 24h)</h3>
      <Line data={data} />
    </div>
  );
}