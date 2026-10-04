import { Table } from '../components/bits';
import { useCrumbs } from '../lib/chrome';
import { formatUnit, usePlatformHealth, SEVERITY, type Health } from '../lib/health';
import { QueryView } from '../lib/query';
import { StatusChip } from './Monitor';

// Only components with an existing provider telemetry contract are discoverable here.
const SERVICES = [
  { name: 'MLflow', scope: 'experiments', purpose: 'Experiment tracking and model registry' },
  { name: 'Argo Workflows', scope: 'workflow', purpose: 'Pipeline execution' },
  { name: 'KServe', scope: 'serving', purpose: 'Model serving' },
  { name: 'Prometheus', scope: 'metrics', purpose: 'Metrics collection and queries' },
];

export function ServicesPage() {
  useCrumbs([{ label: 'Services' }]);
  const query = usePlatformHealth(60);
  return <QueryView query={query}>{(h) => <>
    <div className="page-head"><h1>Services</h1></div>
    <p className="sub">Monitored platform components. Health reflects the control plane's recent calls to each component.</p>
    {!h.available && <div className="alert">Service telemetry is unavailable: {h.error}</div>}
    <Table head={['Service', 'Purpose', 'Status', 'Error rate', 'Latency p95']} testid="services">{SERVICES.map((service) => {
      const checks = h.signals.filter((s) => s.key === 'provider_errors' || s.key === 'provider_latency')
        .flatMap((s) => s.series.filter((series) => series.name === service.scope));
      const status: Health = checks.length ? checks.reduce<Health>((worst, s) => SEVERITY[s.status] > SEVERITY[worst] ? s.status : worst, 'ok') : 'no_data';
      const current = (key: string) => h.signals.find((s) => s.key === key)?.series.find((s) => s.name === service.scope)?.current;
      return <tr key={service.name}><td>{service.name}</td><td>{service.purpose}</td><td><StatusChip status={status} /></td><td className="num">{formatUnit('ratio', current('provider_errors'))}</td><td className="num">{formatUnit('ms', current('provider_latency'))}</td></tr>;
    })}</Table>
    <p className="section"><a href="#/monitor">Open detailed platform checks</a></p>
  </>}</QueryView>;
}
