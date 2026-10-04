/** Navigation icons: 16px, 1.5px strokes in the text colour, decorative (the label names the place). */
const PATHS: Record<string, string> = {
  home: 'M2.5 7.5l5.5-5 5.5 5M4 6.5v7h8v-7',
  functions: 'M10 2H7L5 14M3 6h8',
  endpoints: 'M6 5H3v6h3M10 5h3v6h-3M6 8h4',
  services: 'M2 3h12v4H2zM2 9h12v4H2z',
  data: 'M3 4c0-3 10-3 10 0v8c0 3-10 3-10 0zM3 4c0 3 10 3 10 0M3 8c0 3 10 3 10 0',
  infrastructure: 'M2 3h12v4H2zM2 9h12v4H2zM5 5h.01M5 11h.01',
  identity: 'M5 4a3 3 0 1 0 6 0a3 3 0 1 0-6 0M2 14c0-7 12-7 12 0',
  projects: 'M2.5 2.5h4.5v4.5h-4.5zM9 2.5h4.5v4.5h-4.5zM2.5 9h4.5v4.5h-4.5zM9 9h4.5v4.5h-4.5z',
  monitor: 'M1.5 8.5h3l2-5 3 9 2-4h3',
  overview: 'M2.5 7.5l5.5-5 5.5 5M4 6.5v7h8v-7',
  runs: 'M5 3l8 5-8 5z',
  pipelines: 'M3 4.5a1.5 1.5 0 1 0 0 .01M13 4.5a1.5 1.5 0 1 0 0 .01M8 11.5a1.5 1.5 0 1 0 0 .01M4.5 4.5h7M4 6l3 4.5M12 6l-3 4.5',
  jobs: 'M2.5 5l5.5-2.5 5.5 2.5v6l-5.5 2.5-5.5-2.5zM2.5 5l5.5 2.5 5.5-2.5M8 7.5v6',
  models: 'M8 1.5l5.5 3.25v6.5l-5.5 3.25-5.5-3.25v-6.5zM8 8v6.5M8 8l5.5-3.25M8 8l-5.5-3.25',
  deployments: 'M2.5 3h11v4h-11zM2.5 9h11v4h-11zM5 5h.01M5 11h.01',
  activity: 'M3 4h10M3 8h10M3 12h6',
  settings: 'M8 5.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
};

export function Icon({ name }: { name: string }) {
  return (
    <svg className="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d={PATHS[name] ?? ''} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
