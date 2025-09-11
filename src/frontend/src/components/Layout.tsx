import { PropsWithChildren } from 'react';

export function Layout({ children }: PropsWithChildren) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1 }}>
      <Header />
      <main style={{ width: '100%', maxWidth: 1440, margin: '0 auto', padding: '1.25rem', flex: 1 }}>{children}</main>
      <footer style={{ textAlign: 'center', fontSize: 12, padding: '0.75rem', opacity: 0.6 }}>
        Telematics Insurance Dashboard (Simulation) &copy; {new Date().getFullYear()}
      </footer>
    </div>
  );
}

function Header() {
  return (
    <header style={{ backdropFilter: 'blur(8px)', background: 'rgba(255,255,255,0.04)', borderBottom: '1px solid #222', padding: '0.75rem 1.25rem', display: 'flex', alignItems: 'center' }}>
      <span style={{ fontWeight: 600, letterSpacing: '.5px' }}>Usage-Based Insurance Dashboard</span>
    </header>
  );
}

// Navigation links removed per user request (all content visible on single page)
