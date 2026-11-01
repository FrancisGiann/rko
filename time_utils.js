/* Shared UTC formatting for ISO text and Unix-second/Unix-millisecond API values. */
(function attachRkoTime(root) {
  function epochMilliseconds(value) {
    if (value instanceof Date) {
      const ms = value.getTime();
      return Number.isFinite(ms) ? ms : null;
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      // Unix seconds for this app are ~1e9; millisecond epochs are ~1e12.
      const ms = Math.abs(value) < 100_000_000_000 ? value * 1000 : value;
      return Number.isFinite(ms) ? ms : null;
    }
    if (typeof value === 'string' && value.trim()) {
      const ms = Date.parse(value);
      return Number.isFinite(ms) ? ms : null;
    }
    return null;
  }

  function formatUtc(value) {
    const ms = epochMilliseconds(value);
    if (ms === null) return '—';
    const date = new Date(ms);
    if (!Number.isFinite(date.getTime())) return '—';
    return date.toISOString().replace('T', ' ').replace('.000Z', ' UTC').replace('Z', ' UTC');
  }

  const api = Object.freeze({ epochMilliseconds, formatUtc });
  root.RkoTime = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
