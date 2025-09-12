import { DashboardData, DrivingEvent, MonthlyScore, PremiumProjectionPoint } from '../types';
import { computeSafetyScore } from '../utils/scoring';

// In a real app replace with fetch calls & auth tokens.

const seedHistory: MonthlyScore[] = (() => {
  const now = new Date();
  const list: MonthlyScore[] = [];
  for (let i = 5; i >= 0; i--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1));
    const month = d.toISOString().slice(0, 7);
    const factors = {
      hardBraking: +(Math.random() * 6 + 2).toFixed(1),
      aggressiveTurning: +(Math.random() * 4 + 1).toFixed(1),
      followingDistance: +(Math.random() * 0.35 + 0.05).toFixed(2),
      excessiveSpeeding: +(Math.random() * 12 + 2).toFixed(1),
      lateNightDriving: +(Math.random() * 8).toFixed(1)
    };
    const safetyScore = computeSafetyScore(factors);
    const base = 120;
    const premium = +(base * (1 + (70 - safetyScore) / 300)).toFixed(2);
    list.push({ month, safetyScore, premium, miles: Math.round(Math.random() * 900 + 400), factors });
  }
  return list;
})();

let recentEvents: DrivingEvent[] = [];

export async function fetchDashboard(): Promise<DashboardData> {
  const hosts = ['localhost', '127.0.0.1'];
  for (const h of hosts) {
    try {
      const url = `http://${h}:8787/api/dashboard`;
      const resp = await fetch(url, { method: 'GET', cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        if (data?.profile?.name) {
          (window as any).__DASHBOARD_MODE__ = 'backend';
          // Ensure events are sorted descending by timestamp (latest first)
          if (Array.isArray(data.recentEvents)) {
            data.recentEvents.sort((a: DrivingEvent, b: DrivingEvent) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
          }
          return data as DashboardData;
        }
      }
    } catch (err) {
      // continue to next host
    }
  }

  // fallback simulation (original logic)
  await new Promise(r => setTimeout(r, 150));
  const profile = {
    id: 'driver-001',
    name: 'Test Driver',
    policyNumber: 'POL-123456',
    basePremium: 120,
    currentMonth: new Date().toISOString().slice(0,7)
  };
  const currentFactors = aggregateCurrentFactors();
  const currentScore = computeSafetyScore(currentFactors);
  const currentPremium = +(profile.basePremium * (1 + (70 - currentScore) / 300)).toFixed(2);
  const extendedHistory: MonthlyScore[] = [...seedHistory.filter(h => h.month !== profile.currentMonth), {
    month: profile.currentMonth,
    safetyScore: currentScore,
    premium: currentPremium,
    miles: 0,
    factors: currentFactors
  }];
  const projections: PremiumProjectionPoint[] = new Array(3).fill(0).map((_, i) => {
    const dt = new Date();
    dt.setMonth(dt.getMonth() + i + 1);
    const simulatedScore = Math.min(100, Math.max(40, currentScore + (Math.random() * 10 - 5)));
    const projectedPremium = +(profile.basePremium * (1 + (70 - simulatedScore) / 300)).toFixed(2);
    return { date: dt.toISOString().slice(0,10), projectedPremium };
  });
  (window as any).__DASHBOARD_MODE__ = 'simulation';
  return { profile, history: extendedHistory, recentEvents, projections };
}

export function startEventStream(callback: (event: DrivingEvent) => void) {
  // Only simulate events in simulation mode
  if ((window as any).__DASHBOARD_MODE__ !== 'simulation') {
    return () => {};
  }
  const interval = setInterval(() => {
    const evt = generateSimulatedEvent();
    recentEvents = [evt, ...recentEvents].slice(0, 100);
    callback(evt);
  }, 4000);
  return () => clearInterval(interval);
}

function aggregateCurrentFactors() {
  if (!recentEvents.length) {
    return {
      hardBraking: 0,
      aggressiveTurning: 0,
      followingDistance: 0.05,
      excessiveSpeeding: 0,
      lateNightDriving: 0
    };
  }
  const counts = {
    hardBraking: 0,
    aggressiveTurning: 0,
    followingDistance: 0,
    excessiveSpeeding: 0,
    lateNightDriving: 0
  };
  recentEvents.forEach(e => { counts[e.type] += e.value; });
  // simple normalization factors (toy)
  return {
    hardBraking: +(counts.hardBraking / 12).toFixed(1),
    aggressiveTurning: +(counts.aggressiveTurning / 14).toFixed(1),
    followingDistance: +(Math.min(1, counts.followingDistance / 300)).toFixed(2),
    excessiveSpeeding: +(counts.excessiveSpeeding / 20).toFixed(1),
    lateNightDriving: +(counts.lateNightDriving / 10).toFixed(1)
  };
}
