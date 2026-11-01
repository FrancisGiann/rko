const assert = require('node:assert/strict');
const { epochMilliseconds, formatUtc } = require('./time_utils.js');

const epochSeconds = Date.parse('2026-12-01T00:00:00.000Z') / 1000;
const epochMillis = epochSeconds * 1000;
assert.equal(epochMilliseconds(epochSeconds), epochMillis);
assert.equal(epochMilliseconds(epochMillis), epochMillis);
assert.equal(epochMilliseconds('2026-12-01T00:00:00.000Z'), epochMillis);
assert.equal(formatUtc(epochSeconds), '2026-12-01 00:00:00 UTC');
assert.equal(formatUtc(epochMillis), '2026-12-01 00:00:00 UTC');
assert.equal(formatUtc('invalid'), '—');
assert.equal(formatUtc(1e30), '—');
assert.equal(epochMilliseconds(Number.NaN), null);
process.stdout.write('time_utils.js: 8 assertions passed\n');
