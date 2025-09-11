import { SafetyFactors } from '../types';

// Toy scoring function: starts at 100 and subtracts weighted penalties.
export function computeSafetyScore(f: SafetyFactors): number {
  const penalties =
    f.hardBraking * 1.5 +
    f.aggressiveTurning * 1.2 +
    f.followingDistance * 40 +
    f.excessiveSpeeding * 1.1 +
    f.lateNightDriving * 0.9;
  return Math.round(Math.max(0, 100 - penalties));
}

export function factorLabel(k: keyof SafetyFactors): string {
  switch (k) {
    case 'hardBraking': return 'Hard Braking';
    case 'aggressiveTurning': return 'Aggressive Turning';
    case 'followingDistance': return 'Following Distance';
    case 'excessiveSpeeding': return 'Excessive Speeding';
    case 'lateNightDriving': return 'Late-night Driving';
  }
}
