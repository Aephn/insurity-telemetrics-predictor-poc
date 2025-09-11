interface Badge { id: string; label: string; achieved: boolean; description: string; }

const sampleBadges: Badge[] = [
  { id: 'consistent-score', label: 'Consistent Driver', achieved: true, description: 'Safety score ≥ 70 for 3 consecutive months.' },
  { id: 'night-owl', label: 'Night Reduction', achieved: false, description: 'Reduce late night driving by 20% month-over-month.' },
  { id: 'smooth-operator', label: 'Smooth Operator', achieved: false, description: 'Zero hard braking events in a 7-day span.' }
];

export function GamificationBadges() {
  return (
    <div style={card} id="badges">
      <h2 style={h2}>Badges</h2>
      <div style={{ display:'flex', gap:12, flexWrap:'wrap' }}>
        {sampleBadges.map(b => <Badge key={b.id} badge={b} />)}
      </div>
      <small style={{ display:'block', marginTop:10, opacity:0.6 }}>Badges are simulated; hook into real KPIs in production.</small>
    </div>
  );
}

function Badge({ badge }: { badge: Badge }) {
  const { achieved } = badge;
  return (
    <div style={{ background:'#1b2129', border:'1px solid #272f38', padding:'0.75rem .9rem', borderRadius:12, width:190, position:'relative', opacity: achieved ? 1 : 0.55 }}>
      <div style={{ fontWeight:600, fontSize:14 }}>{badge.label}</div>
      <div style={{ fontSize:11, marginTop:4, lineHeight:1.25, opacity:0.75 }}>{badge.description}</div>
      <div style={{ position:'absolute', top:8, right:8, fontSize:11, color: achieved ? '#5dd39e' : '#888' }}>{achieved ? 'Achieved' : 'Locked'}</div>
    </div>
  );
}

const card: React.CSSProperties = { background: '#151a21', border: '1px solid #252b33', padding: '1rem 1.25rem', borderRadius: 12 };
const h2: React.CSSProperties = { margin: '0 0 .5rem', fontSize: 18, fontWeight: 500 };
