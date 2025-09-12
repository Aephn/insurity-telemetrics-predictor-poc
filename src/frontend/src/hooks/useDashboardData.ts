import { useEffect, useState } from 'react';
import { DashboardData, DrivingEvent } from '../types';
import { fetchDashboard, startEventStream } from '../services/api';

export function useDashboardData() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<string>('unknown');

  useEffect(() => {
    let cancel = false;
    const load = () => {
      fetchDashboard()
        .then(d => {
          if (!cancel) {
            const m = (window as any).__DASHBOARD_MODE__;
            console.log('[dashboard] fetched', m, d.profile?.name);
            // Ensure descending order of events
            if (Array.isArray(d.recentEvents)) {
              d.recentEvents = [...d.recentEvents].sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
            }
            setData(d); setMode(m); setLoading(false);
          }
        })
        .catch(e => { if (!cancel) { setError(String(e)); setLoading(false); } });
    };
    load();
    const interval = setInterval(load, 10000); // refresh every 10s
    return () => { cancel = true; clearInterval(interval); };
  }, []);

  useEffect(() => {
    if (!data) return;
    const m = (window as any).__DASHBOARD_MODE__;
    const stop = startEventStream((evt: DrivingEvent) => {
      setData((prev: DashboardData | null) => prev ? {
        ...prev,
        recentEvents: [evt, ...prev.recentEvents].slice(0, 100)
      } : prev);
    });
    return () => { if (m === 'simulation') stop(); };
  }, [data]);

  const refresh = () => { setLoading(true); fetchDashboard().then(d => { const m = (window as any).__DASHBOARD_MODE__; setData(d); setMode(m); setLoading(false); }); };
  return { data, loading, error, mode, refresh };
}
