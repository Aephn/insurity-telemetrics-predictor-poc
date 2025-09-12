import { useDashboardData } from './hooks/useDashboardData';
import { Layout } from './components/Layout';
import { PremiumSummary } from './components/PremiumSummary';
import { SafetyFactorBreakdown } from './components/SafetyFactorBreakdown';
import { PremiumHistoryChart } from './components/PremiumHistoryChart';
import { DrivingEventsFeed } from './components/DrivingEventsFeed';
import { GamificationBadges } from './components/GamificationBadges';

export default function App() {
  const { data, loading, error, mode, refresh } = useDashboardData();
  return (
    <Layout>
      {loading && <div style={{ padding:40, textAlign:'center' }}>Loading dashboard…</div>}
      {error && <div style={{ color:'#ff6b6b' }}>{error}</div>}
      {data && (
        <div style={{ display:'flex', flexDirection:'column', gap: '1.25rem' }}>
          <div style={{ display:'flex', alignItems:'flex-end', gap:'1rem', flexWrap:'wrap' }}>
            <div style={{ fontSize: '2.25rem', fontWeight: 600, lineHeight: 1.1 }}>
              Hello {data.profile.name}.
            </div>
            <div style={{ fontSize:12, padding:'4px 8px', borderRadius:6, background:'#1d242c', border:'1px solid #2b343d', display:'flex', gap:6, alignItems:'center' }}>
              <span style={{ opacity:0.6 }}>Data Source:</span>
              <strong style={{ color: mode === 'backend' ? '#5dd39e' : '#ffaa00' }}>{mode}</strong>
              <button style={{ marginLeft:8, background:'#27313b', border:'1px solid #364350', color:'#eee', borderRadius:4, cursor:'pointer', fontSize:11, padding:'2px 6px' }} onClick={refresh}>Refresh</button>
            </div>
          </div>
          <section style={{ display:'flex', flexWrap:'wrap', gap: '1.25rem' }}>
            <PremiumSummary history={data.history} />
            <SafetyFactorBreakdown factors={data.currentFactors || data.history[data.history.length-1].factors} safetyScore={data.history[data.history.length-1].safetyScore} />
          </section>
          <PremiumHistoryChart history={data.history} />
          <section style={{ display:'flex', flexWrap:'wrap', gap:'1.25rem' }}>
            <DrivingEventsFeed events={data.recentEvents} />
            <GamificationBadges />
          </section>
        </div>
      )}
    </Layout>
  );
}
