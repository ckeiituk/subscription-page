import { randomInt } from 'node:crypto';
import { parseDocument, stringify } from 'yaml';

export interface XOutResult {
    body: Buffer;
    modified: boolean;
    warning?: string;
}

const MAX_YAML_BYTES = 8 * 1024 * 1024;

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Fisher-Yates: every permutation is possible, including the original order. */
export function shuffleProxyNames(
    names: readonly string[],
    chooseIndex: (max: number) => number = randomInt,
): string[] {
    const result = [...names];
    for (let index = result.length - 1; index > 0; index--) {
        const selected = chooseIndex(index + 1);
        [result[index], result[selected]] = [result[selected], result[index]];
    }
    return result;
}

/** Process only explicit proxy-group markers; all other response formats pass through. */
export function applyXOut(body: Buffer): XOutResult {
    const unchanged: XOutResult = { body, modified: false };
    if (!body.includes('x-out')) return unchanged;
    if (body.length > MAX_YAML_BYTES) {
        return { ...unchanged, warning: 'x-out: response exceeds the YAML processing limit' };
    }

    try {
        const document = parseDocument(body.toString('utf8'), {
            prettyErrors: false,
            merge: true,
            logLevel: 'silent',
        });
        if (document.errors.length || document.warnings.length) {
            return {
                ...unchanged,
                warning: 'x-out: unsupported or invalid YAML; response unchanged',
            };
        }
        const data: unknown = document.toJS({ maxAliasCount: 100 });
        if (!isRecord(data) || !Array.isArray(data['proxy-groups'])) return unchanged;

        let modified = false;
        let warning: string | undefined;
        const groups: unknown[] = data['proxy-groups'];
        const updatedGroups = groups.map((group: unknown): unknown => {
            if (!isRecord(group) || !Object.hasOwn(group, 'x-out')) return group;
            modified = true;
            // Copy the group so YAML aliases cannot change an unmarked group's order.
            const { 'x-out': settings, ...updated } = group;
            if (isRecord(settings) && settings.randomize === false) return updated;
            if (!isRecord(settings) || settings.randomize !== true) {
                warning = 'x-out: invalid options; marker removed without changing proxy order';
                return updated;
            }
            if (settings.mode !== undefined && settings.mode !== 'per-refresh') {
                warning = 'x-out: unsupported mode; marker removed without changing proxy order';
                return updated;
            }
            const proxies: unknown = group.proxies;
            if (
                !Array.isArray(proxies) ||
                !proxies.every((name: unknown): name is string => typeof name === 'string')
            ) {
                warning =
                    'x-out: proxies must be a list of names; marker removed without changing proxy order';
                return updated;
            }
            updated.proxies = shuffleProxyNames(proxies);
            return updated;
        });
        if (!modified) return unchanged;
        data['proxy-groups'] = updatedGroups;
        return {
            body: Buffer.from(stringify(data, { lineWidth: 0 }), 'utf8'),
            modified: true,
            ...(warning ? { warning } : {}),
        };
    } catch {
        // Never log parser errors: they may contain subscription URLs or credentials.
        return { ...unchanged, warning: 'x-out: YAML processing failed; response unchanged' };
    }
}
