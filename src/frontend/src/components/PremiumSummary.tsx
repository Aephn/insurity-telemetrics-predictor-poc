import { MonthlyScore } from '../types';

interface Props { history: MonthlyScore[]; }

export function PremiumSummary({ history }: Props) {
  const latest = history[history.length - 1];
  const prev = history[history.length - 2];
  const delta = prev ? latest.premium - prev.premium : 0;
  const pct = prev ? (delta / prev.premium) * 100 : 0;
  return (
    <div style={card} id="premiums">
      <h2 style={h2}>Current Premium</h2>
      <div style={{ fontSize: 42, fontWeight: 600 }}>${latest.premium.toFixed(2)}</div>
      <div style={{ fontSize: 14, opacity: 0.8 }}>Safety Score: <strong>{latest.safetyScore}</strong></div>
      <div style={{ fontSize: 13, marginTop: 8, color: delta <= 0 ? '#5dd39e' : '#ff9f66' }}>
        {delta === 0 ? 'No change from last month' : delta > 0 ? `+${delta.toFixed(2)} (${pct.toFixed(1)}%) higher than last month` : `${delta.toFixed(2)} (${pct.toFixed(1)}%) lower than last month`}
      </div>
      <small style={{ display: 'block', marginTop: 12, opacity: 0.6 }}>Updated with simulated data.</small>
    </div>
  );
}

const card: React.CSSProperties = { background: '#151a21', border: '1px solid #252b33', padding: '1rem 1.25rem', borderRadius: 12, flex: 1, minWidth: 260 };
const h2: React.CSSProperties = { margin: '0 0 .5rem', fontSize: 18, fontWeight: 500 };
