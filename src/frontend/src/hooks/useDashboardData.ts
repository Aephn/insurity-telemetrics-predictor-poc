import { useEffect, useState } from 'react';
import { DashboardData, DrivingEvent } from '../types';
import { fetchDashboard, startEventStream } from '../services/api';

export function useDashboardData() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    fetchDashboard()
      .then(d => { if (!cancel) { setData(d); setLoading(false); } })
      .catch(e => { if (!cancel) { setError(String(e)); setLoading(false); } });
    return () => { cancel = true; };
  }, []);

  useEffect(() => {
    if (!data) return;
    const stop = startEventStream((evt: DrivingEvent) => {
      setData((prev: DashboardData | null) => prev ? { ...prev, recentEvents: [evt, ...prev.recentEvents].slice(0, 100) } : prev);
    });
    return stop;
  }, [data]);

  return { data, loading, error };
}
