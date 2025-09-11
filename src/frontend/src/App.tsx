import { useDashboardData } from './hooks/useDashboardData';
import { Layout } from './components/Layout';
import { PremiumSummary } from './components/PremiumSummary';
import { SafetyFactorBreakdown } from './components/SafetyFactorBreakdown';
import { PremiumHistoryChart } from './components/PremiumHistoryChart';
import { DrivingEventsFeed } from './components/DrivingEventsFeed';
import { GamificationBadges } from './components/GamificationBadges';

export default function App() {
  const { data, loading, error } = useDashboardData();
  return (
    <Layout>
      {loading && <div style={{ padding:40, textAlign:'center' }}>Loading dashboard…</div>}
      {error && <div style={{ color:'#ff6b6b' }}>{error}</div>}
      {data && (
        <div style={{ display:'flex', flexDirection:'column', gap: '1.25rem' }}>
          <section style={{ display:'flex', flexWrap:'wrap', gap: '1.25rem' }}>
            <PremiumSummary history={data.history} />
            <SafetyFactorBreakdown factors={data.history[data.history.length-1].factors} safetyScore={data.history[data.history.length-1].safetyScore} />
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
