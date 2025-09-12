import { SafetyFactors } from '../types';
import { factorLabel } from '../utils/scoring';

interface Props { factors: SafetyFactors; safetyScore: number; }

export function SafetyFactorBreakdown({ factors, safetyScore }: Props) {
  return (
    <div style={card} id="safety">
  <h2 style={h2}>Safety Events (Last 14 Days)</h2>
      <div style={{ display: 'grid', gap: '0.6rem', gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))' }}>
        {Object.entries(factors).map(([k, v]) => (
          <FactorPill key={k} label={factorLabel(k as keyof SafetyFactors)} value={v} />
        ))}
      </div>
      <div style={{ marginTop: 14, fontSize: 14 }}>Composite Safety Score: <strong style={{ color: safetyColor(safetyScore) }}>{safetyScore}</strong></div>
      <small style={{ opacity: 0.6 }}>Lower raw factor values generally improve the composite score.</small>
    </div>
  );
}

function FactorPill({ label, value }: { label: string; value: number; }) {
  return (
    <div style={{ background:'#1b2129', border:'1px solid #272f38', padding:'0.55rem 0.7rem', borderRadius: 10 }}>
      <div style={{ fontSize: 12, opacity: 0.7 }}>{label}</div>
      <div style={{ fontWeight:600 }}>{value}</div>
    </div>
  );
}

function safetyColor(score: number) {
  if (score >= 80) return '#5dd39e';
  if (score >= 65) return '#ffaa00';
  return '#ff6b6b';
}

const card: React.CSSProperties = { background: '#151a21', border: '1px solid #252b33', padding: '1rem 1.25rem', borderRadius: 12, flex: 1 };
const h2: React.CSSProperties = { margin: '0 0 .75rem', fontSize: 18, fontWeight: 500 };
