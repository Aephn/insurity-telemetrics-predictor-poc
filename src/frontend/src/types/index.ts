export interface SafetyFactors {
  hardBraking: number; // events / 100mi
  aggressiveTurning: number; // events / 100mi
  followingDistance: number; // proportion of time tailgating (0-1)
  excessiveSpeeding: number; // minutes over threshold / 100mi
  lateNightDriving: number; // miles between 12-4am / 100mi
}

export interface MonthlyScore {
  month: string; // YYYY-MM
  safetyScore: number; // 0-100
  premium: number; // USD
  miles: number;
  factors: SafetyFactors;
}

export interface DrivingEvent {
  id: string;
  timestamp: string; // ISO
  type: keyof SafetyFactors;
  severity: 'low' | 'moderate' | 'high';
  value: number;
  speedMph: number;
  location?: { lat: number; lon: number };
}

export interface DriverProfile {
  id: string;
  name: string;
  policyNumber: string;
  basePremium: number;
  currentMonth: string;
}

export interface PremiumProjectionPoint {
  date: string; // YYYY-MM-DD
  projectedPremium: number;
}

export interface DashboardData {
  profile: DriverProfile;
  history: MonthlyScore[];
  recentEvents: DrivingEvent[];
  projections: PremiumProjectionPoint[];
}
