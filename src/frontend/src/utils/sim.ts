import { DrivingEvent, SafetyFactors } from '../types';

const factorKeys: (keyof SafetyFactors)[] = [
  'hardBraking', 'aggressiveTurning', 'followingDistance', 'excessiveSpeeding', 'lateNightDriving'
];

export function generateSimulatedEvent(): DrivingEvent {
  const type = factorKeys[Math.floor(Math.random() * factorKeys.length)];
  const severityRand = Math.random();
  const severity: DrivingEvent['severity'] = severityRand > 0.85 ? 'high' : severityRand > 0.55 ? 'moderate' : 'low';
  const baseValue = type === 'followingDistance' ? Math.random() * 5 + 1 : Math.random() * 2 + 1;
  const multiplier = severity === 'high' ? 3 : severity === 'moderate' ? 1.8 : 1;
  return {
    id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    type,
    severity,
    value: +(baseValue * multiplier).toFixed(2),
    speedMph: Math.round(Math.random() * 50 + 25),
    location: { lat: +(37 + Math.random() * 0.2).toFixed(5), lon: +(-122 + Math.random() * 0.2).toFixed(5) }
  };
}
