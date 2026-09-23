const assert = require('node:assert/strict');
const { test, mock } = require('node:test');
const crypto = require('node:crypto');
const { parse, stringify } = require('yaml');
const { applyXOut, shuffleProxyNames } = require('../.test-build/modules/subscription/x-out.js');

const pairs = ['♻️ 🇷🇺 Hades', '♻️ __[yt]Nlades', '♻️ Test'];
const options = { randomize: true, mode: 'per-refresh' };
function config(settings = options, proxies = pairs) {
    return {
        dns: { nameserver: ['1.1.1.1'], 'fake-ip-filter': ['*.example.com'] },
        'proxy-providers': {
            direct: { type: 'inline', payload: [{ name: 'node', uuid: 'test-secret' }] },
        },
        'proxy-groups': [
            { name: 'Без рекламы', type: 'fallback', 'x-out': settings, proxies },
            { name: '♻️ 🇷🇺 Hades', type: 'fallback', use: ['direct', 'wl'], filter: '^Hades$' },
            { name: 'Other', type: 'select', proxies: ['DIRECT', 'Без рекламы'] },
        ],
        rules: ['MATCH,Без рекламы'],
    };
}
function run(data) {
    const result = applyXOut(Buffer.from(stringify(data)));
    return { ...result, data: parse(result.body.toString()) };
}

test('Fisher-Yates accepts every possible ordering for three entries without changing its input', () => {
    const permutations = new Set();
    for (let first = 0; first < 3; first++) {
        for (let second = 0; second < 2; second++) {
            const picks = [first, second];
            permutations.add(JSON.stringify(shuffleProxyNames(pairs, () => picks.shift())));
        }
    }
    assert.equal(permutations.size, 6);
    assert.ok(permutations.has(JSON.stringify(pairs)));
    assert.deepEqual(pairs, ['♻️ 🇷🇺 Hades', '♻️ __[yt]Nlades', '♻️ Test']);
});

test('only marked proxy order changes; provider credentials, rules and fallback pairs survive', () => {
    const random = mock.method(crypto, 'randomInt', () => 0);
    try {
        const source = config();
        const result = run(source);
        assert.equal(result.modified, true);
        assert.equal(result.warning, undefined);
        assert.equal(random.mock.callCount(), 2);
        assert.deepEqual(result.data['proxy-groups'][0].proxies, [pairs[1], pairs[2], pairs[0]]);
        assert.equal(Object.hasOwn(result.data['proxy-groups'][0], 'x-out'), false);
        const expected = structuredClone(source);
        delete expected['proxy-groups'][0]['x-out'];
        expected['proxy-groups'][0].proxies = [pairs[1], pairs[2], pairs[0]];
        assert.deepEqual(result.data, expected);
    } finally {
        random.mock.restore();
    }
});

test('each refresh of the same source draws new random indices', () => {
    let index = 0;
    const picks = [0, 0, 2, 1];
    const random = mock.method(crypto, 'randomInt', () => picks[index++]);
    try {
        const body = Buffer.from(stringify(config()));
        const first = applyXOut(body);
        const second = applyXOut(body);
        assert.notDeepEqual(
            parse(first.body.toString())['proxy-groups'][0].proxies,
            parse(second.body.toString())['proxy-groups'][0].proxies,
        );
        assert.equal(random.mock.callCount(), 4);
    } finally {
        random.mock.restore();
    }
});

test('unmarked YAML and other subscription formats are byte-for-byte unchanged', () => {
    for (const text of [
        '# comment\nproxy-groups: [{name: Test, proxies: [DIRECT]}]\n',
        '{"outbounds":[{"tag":"x-out"}]}',
        '<html>x-out</html>',
        'dmxlc3M6Ly90ZXN0',
        'proxy-groups: [{name: x-out, proxies: [DIRECT]}]\n',
    ]) {
        const body = Buffer.from(text);
        const result = applyXOut(body);
        assert.equal(result.modified, false);
        assert.equal(result.body, body);
    }
});

test('disabled markers are removed without using randomness', () => {
    const random = mock.method(crypto, 'randomInt', () => {
        throw new Error('must not shuffle');
    });
    try {
        const result = run(config({ randomize: false }));
        assert.equal(result.modified, true);
        assert.equal(result.warning, undefined);
        assert.deepEqual(result.data['proxy-groups'][0].proxies, pairs);
        assert.equal(Object.hasOwn(result.data['proxy-groups'][0], 'x-out'), false);
        assert.equal(random.mock.callCount(), 0);
    } finally {
        random.mock.restore();
    }
});

test('missing mode defaults to per-refresh', () => {
    const random = mock.method(crypto, 'randomInt', () => 0);
    try {
        assert.notDeepEqual(
            run(config({ randomize: true })).data['proxy-groups'][0].proxies,
            pairs,
        );
    } finally {
        random.mock.restore();
    }
});

test('unsupported modes and invalid flags keep the order and produce a sanitized warning', () => {
    for (const settings of [
        null,
        true,
        {},
        { randomize: 'true' },
        { ...options, mode: 'secret-user-token' },
    ]) {
        const result = run(config(settings));
        assert.equal(result.modified, true);
        assert.deepEqual(result.data['proxy-groups'][0].proxies, pairs);
        assert.equal(Object.hasOwn(result.data['proxy-groups'][0], 'x-out'), false);
        assert.ok(result.warning);
        assert.equal(result.warning.includes('secret-user-token'), false);
    }
});

test('empty and single-item groups preserve their entries', () => {
    for (const proxies of [[], ['REJECT']]) {
        assert.deepEqual(run(config(options, proxies)).data['proxy-groups'][0].proxies, proxies);
    }
});

test('invalid proxy lists are not rewritten', () => {
    for (const proxies of [null, 'node', ['node', 42]]) {
        const result = run(config(options, proxies));
        assert.deepEqual(result.data['proxy-groups'][0].proxies, proxies);
        assert.ok(result.warning);
    }
});

test('a processed subscription has no markers and can safely pass through again', () => {
    const once = run(config());
    const twice = applyXOut(once.body);
    assert.equal(twice.modified, false);
    assert.equal(twice.body, once.body);
});

test('shared YAML proxy lists do not shuffle unmarked groups', () => {
    const body = Buffer.from(`proxy-groups:
  - name: original
    type: select
    proxies: &names [A, B, C]
  - name: shuffled
    type: fallback
    x-out: {randomize: true, mode: per-refresh}
    proxies: *names
`);
    const random = mock.method(crypto, 'randomInt', () => 0);
    try {
        const result = parse(applyXOut(body).body.toString());
        assert.deepEqual(result['proxy-groups'][0].proxies, ['A', 'B', 'C']);
        assert.deepEqual(result['proxy-groups'][1].proxies, ['B', 'C', 'A']);
    } finally {
        random.mock.restore();
    }
});

test('invalid or oversized YAML fails open without logging its contents', () => {
    for (const body of [
        Buffer.from('x-out: [SECRET'),
        Buffer.from('x-out: ' + 'x'.repeat(8 * 1024 * 1024)),
    ]) {
        const result = applyXOut(body);
        assert.equal(result.body, body);
        assert.equal(result.modified, false);
        assert.ok(result.warning);
        assert.equal(result.warning.includes('SECRET'), false);
    }
});
