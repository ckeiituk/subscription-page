const assert = require('node:assert/strict');
const path = require('node:path');
const { registerHooks } = require('node:module');
const { test, mock } = require('node:test');
const crypto = require('node:crypto');
const { parse, stringify } = require('yaml');

registerHooks({
    resolve(specifier, context, nextResolve) {
        if (specifier.startsWith('@common/')) {
            return nextResolve(
                path.resolve(__dirname, '../.test-build/common', specifier.slice(8)),
                context,
            );
        }
        return nextResolve(specifier, context);
    },
});
const {
    SubscriptionService,
} = require('../.test-build/modules/subscription/subscription.service.js');

function response() {
    return {
        headers: {},
        code: null,
        body: null,
        set(name, value) {
            const values = typeof name === 'string' ? { [name]: value } : name;
            for (const [key, val] of Object.entries(values)) this.headers[key.toLowerCase()] = val;
            return this;
        },
        removeHeader(name) {
            delete this.headers[name.toLowerCase()];
        },
        status(code) {
            this.code = code;
            return this;
        },
        send(body) {
            this.body = body;
            return this;
        },
    };
}

test('real subscription handler transforms marked responses, removes stale headers and retains user headers', async () => {
    const input = Buffer.from(
        stringify({
            'proxy-groups': [
                {
                    name: 'ads',
                    type: 'fallback',
                    'x-out': { randomize: true, mode: 'per-refresh' },
                    proxies: ['A', 'B'],
                },
            ],
            rules: ['MATCH,ads'],
        }),
    );
    const upstream = {
        subscription: input,
        headers: {
            'Content-Type': 'text/yaml',
            'Content-Length': String(input.length),
            'Content-Encoding': 'gzip',
            ETag: 'old',
            'Last-Modified': 'yesterday',
            'Content-MD5': 'old',
            Digest: 'old',
            'Cache-Control': 'public,max-age=600',
            'subscription-userinfo': 'upload=0; download=0; total=10',
            'profile-title': 'test',
        },
    };
    const service = new SubscriptionService({ getSubscription: async () => upstream });
    const res = response();
    const random = mock.method(crypto, 'randomInt', () => 0);
    try {
        await service.serveSubscriptionPage('127.0.0.1', { headers: {} }, res, 'synthetic-user');
    } finally {
        random.mock.restore();
    }
    assert.equal(res.code, 200);
    assert.deepEqual(parse(res.body.toString())['proxy-groups'][0].proxies, ['B', 'A']);
    assert.equal(res.body.includes('x-out'), false);
    for (const header of [
        'content-length',
        'content-encoding',
        'etag',
        'last-modified',
        'content-md5',
        'digest',
    ]) {
        assert.equal(res.headers[header], undefined);
    }
    assert.equal(res.headers['cache-control'], 'private, no-store');
    assert.equal(res.headers['subscription-userinfo'], upstream.headers['subscription-userinfo']);
    assert.equal(res.headers['profile-title'], 'test');
    assert.equal(res.headers['content-type'], 'text/yaml');
    assert.equal(upstream.subscription, input);
    assert.equal(upstream.headers['Content-Encoding'], 'gzip');
});

test('ordinary responses keep their original bytes and headers', async () => {
    const input = Buffer.from('dmxlc3M6Ly9leGFtcGxl');
    const service = new SubscriptionService({
        getSubscription: async () => ({
            subscription: input,
            headers: { 'content-type': 'text/plain', 'profile-title': 'test' },
        }),
    });
    const res = response();
    await service.serveSubscriptionPage('127.0.0.1', { headers: {} }, res, 'synthetic-user');
    assert.equal(res.body, input);
    assert.deepEqual(res.headers, { 'content-type': 'text/plain', 'profile-title': 'test' });
});
