import { MonthlyScore } from '../types';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine } from 'recharts';

interface Props { history: MonthlyScore[]; }

export function PremiumHistoryChart({ history }: Props) {
  return (
    <div style={card}>
      <h2 style={h2}>Premium & Safety History</h2>
      <div style={{ width: '100%', height: 260 }}>
        <ResponsiveContainer>
          <LineChart data={history} margin={{ left: 8, right: 16, top: 10, bottom: 4 }}>
            <CartesianGrid stroke="#1f242b" />
            <XAxis dataKey="month" stroke="#6b7682" />
            <YAxis yAxisId="left" stroke="#6b7682" />
            <YAxis yAxisId="right" orientation="right" stroke="#6b7682" domain={[0,100]} />
            <Tooltip contentStyle={{ background:'#14181d', border:'1px solid #2a323a', borderRadius:8 }} />
            <Line yAxisId="left" type="monotone" dataKey="premium" stroke="#4dabf7" strokeWidth={2} dot={false} name="Premium ($)" />
            <Line yAxisId="right" type="monotone" dataKey="safetyScore" stroke="#5dd39e" strokeWidth={2} dot={false} name="Safety Score" />
            <ReferenceLine yAxisId="right" y={70} stroke="#ffaa00" strokeDasharray="3 3" label={{ value: 'Target 70', position: 'right', fill:'#ffaa00', fontSize:12 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

const card: React.CSSProperties = { background: '#151a21', border: '1px solid #252b33', padding: '1rem 1.25rem', borderRadius: 12, width: '100%' };
const h2: React.CSSProperties = { margin: '0 0 .5rem', fontSize: 18, fontWeight: 500 };
