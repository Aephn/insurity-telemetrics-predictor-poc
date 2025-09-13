import { DrivingEvent } from '../types';
import { factorLabel } from '../utils/scoring';

interface Props { events: DrivingEvent[]; }

export function DrivingEventsFeed({ events }: Props) {
  return (
    <div style={card} id="events">
      <h2 style={h2}>Recent Driving Events</h2>
  <div className="events-scroll" style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 300, overflowY: 'auto', paddingRight: 4 }}>
        {events.length === 0 && <div style={{ fontSize: 13, opacity: 0.6 }}>No recent events.</div>}
        {events.map(e => <Row key={e.id} evt={e} />)}
      </div>
    </div>
  );
}

function Row({ evt }: { evt: DrivingEvent }) {
  const d = new Date(evt.timestamp);
  const ts = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const mmdd = `${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')}`;
  return (
    <div style={{ display:'flex', alignItems:'center', gap:8, background:'#1b2129', border:'1px solid #272f38', padding:'0.45rem 0.6rem', borderRadius:8, fontSize:12 }}>
      <Severity severity={evt.severity} />
      <div style={{ flex:1 }}>
        <strong>{factorLabel(evt.type)}</strong> <span style={{ opacity:0.7 }}>+{evt.value}</span>
        <div style={{ opacity:0.55 }}>{ts} <span style={{ opacity:0.5 }}>({mmdd})</span> • {evt.speedMph} mph</div>
      </div>
      {evt.location && <div style={{ fontSize:11, opacity:0.5 }}>{evt.location.lat.toFixed(3)},{evt.location.lon.toFixed(3)}</div>}
    </div>
  );
}

function Severity({ severity }: { severity: 'low'|'moderate'|'high' }) {
  const color = severity === 'high' ? '#ff6b6b' : severity === 'moderate' ? '#ffaa00' : '#5dd39e';
  return <span style={{ width:10, height:10, borderRadius:'50%', background:color, boxShadow:`0 0 6px ${color}` }} />;
}

const card: React.CSSProperties = { background: '#151a21', border: '1px solid #252b33', padding: '1rem 1.25rem', borderRadius: 12, flex: 1, minWidth: 300 };
const h2: React.CSSProperties = { margin: '0 0 .75rem', fontSize: 18, fontWeight: 500 };
