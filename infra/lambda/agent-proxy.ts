// ブラウザ（PWA）から受け取った「警報＋端末に保存された場所・家族の情報」で AgentCore Runtime を呼び出し、
// エージェントの SSE をそのままブラウザへ流す。サーバ側には何も保存しない。
// CloudFront（OAC）経由でしか呼べない Function URL の裏で動く。
import { randomUUID } from 'crypto';
import type { Writable } from 'stream';
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from '@aws-sdk/client-bedrock-agentcore';
import type { LambdaFunctionURLEvent } from 'aws-lambda';

declare const awslambda: {
  streamifyResponse(
    handler: (event: LambdaFunctionURLEvent, stream: Writable) => Promise<void>,
  ): unknown;
  HttpResponseStream: {
    from(stream: Writable, meta: { statusCode: number; headers?: Record<string, string> }): Writable;
  };
};

const client = new BedrockAgentCoreClient({});
const RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN!;
const MAX_BODY_BYTES = 20_000;

const LANGUAGES = ['ja', 'vi', 'en', 'zh', 'ko', 'pt', 'tl', 'ne', 'id'];
const MOBILITY = ['自立', '歩行がゆっくり', '車いす', '乳幼児', '寝たきり', '不明'];
const HAZARDS = ['flood', 'landslide'];

class InvalidInput extends Error {}

// ---- 入力の検証：決めた項目だけを、決めた型・長さで取り出して作り直す（それ以外は捨てる）----
function str(v: unknown, max: number, field: string, required = false): string {
  if (v === undefined || v === null || v === '') {
    if (required) throw new InvalidInput(`${field} が必要です`);
    return '';
  }
  if (typeof v !== 'string') throw new InvalidInput(`${field} は文字列で指定してください`);
  // 制御文字（改行・タブ以外）を取り除く
  const s = v.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '').trim();
  if (s.length > max) throw new InvalidInput(`${field} は${max}文字以内にしてください`);
  return s;
}

function num(v: unknown, min: number, max: number, field: string, nullable = false): number | null {
  if ((v === null || v === undefined || v === '') && nullable) return null;
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n) || n < min || n > max) throw new InvalidInput(`${field} の値が正しくありません`);
  return n;
}

function oneOf<T extends string>(v: unknown, allowed: T[], field: string, fallback?: T): T {
  if ((v === undefined || v === null || v === '') && fallback) return fallback;
  if (typeof v !== 'string' || !allowed.includes(v as T)) throw new InvalidInput(`${field} の値が正しくありません`);
  return v as T;
}

function list<T>(v: unknown, max: number, field: string, map: (x: any, i: number) => T): T[] {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw new InvalidInput(`${field} は配列で指定してください`);
  if (v.length > max) throw new InvalidInput(`${field} は${max}件以内にしてください`);
  return v.map((x, i) => {
    if (typeof x !== 'object' || x === null) throw new InvalidInput(`${field} の形式が正しくありません`);
    return map(x, i);
  });
}

function sanitizeHousehold(h: any) {
  if (typeof h !== 'object' || h === null) throw new InvalidInput('household が必要です');
  const lat = num(h.lat, 35.7, 37.0, '緯度')!;
  const lng = num(h.lng, 139.6, 140.9, '経度')!; // 茨城県のおおよその範囲
  const home = typeof h.home === 'object' && h.home !== null ? h.home : {};
  const members = list(h.members, 15, 'members', (m, i) => ({
    relation: str(m.relation, 20, `members[${i}].relation`, true),
    age: num(m.age, 0, 120, `members[${i}].age`, true),
    mobility: oneOf(m.mobility, MOBILITY, `members[${i}].mobility`, '不明'),
    notes: str(m.notes, 200, `members[${i}].notes`),
  }));
  if (!members.length) throw new InvalidInput('members が必要です');
  return {
    id: str(h.id, 40, 'id', true),
    name: str(h.name, 40, 'name', true),
    situation: h.situation === 'current_location' ? 'current_location' : undefined,
    address: str(h.address, 120, 'address'),
    lat,
    lng,
    city: str(h.city, 20, 'city', true),
    language: oneOf(h.language, LANGUAGES, 'language', 'ja'),
    home: { structure: str(home.structure, 40, 'home.structure'), floors: num(home.floors ?? 1, 1, 60, 'home.floors') },
    members,
    pets: list(h.pets, 10, 'pets', (p, i) => ({ type: str(p.type, 20, `pets[${i}].type`), notes: str(p.notes, 100, `pets[${i}].notes`) })),
    vehicles: list(h.vehicles, 10, 'vehicles', (p, i) => ({ type: str(p.type, 20, `vehicles[${i}].type`), notes: str(p.notes, 100, `vehicles[${i}].notes`) })),
    contacts: list(h.contacts, 10, 'contacts', (p, i) => ({ name: str(p.name, 30, `contacts[${i}].name`), relation: str(p.relation, 60, `contacts[${i}].relation`) })),
  };
}

function sanitizeAlert(a: any) {
  if (typeof a !== 'object' || a === null) throw new InvalidInput('alert が必要です');
  const area = typeof a.area === 'object' && a.area !== null ? a.area : {};
  return {
    alertId: str(a.alertId, 64, 'alertId', true),
    demo: true,
    hazard: oneOf(a.hazard, HAZARDS, 'hazard'),
    hazardLabel: str(a.hazardLabel, 20, 'hazardLabel'),
    infoKind: str(a.infoKind, 40, 'infoKind'),
    reportDatetime: str(a.reportDatetime, 40, 'reportDatetime'),
    area: {
      prefecture: '茨城県',
      cities: list((area.cities ?? []).map((c: unknown) => ({ c })), 50, 'area.cities', (x, i) => str(x.c, 20, `area.cities[${i}]`, true)),
    },
    requestedArea: str(a.requestedArea, 20, 'requestedArea'),
    river: str(a.river, 20, 'river'),
    level: num(a.level, 3, 5, 'level'),
    title: str(a.title, 80, 'title', true),
    alertLevel: str(a.alertLevel, 40, 'alertLevel'),
    headline: str(a.headline, 200, 'headline'),
    expectedPeakAt: str(a.expectedPeakAt, 40, 'expectedPeakAt'),
  };
}

function reply(stream: Writable, statusCode: number, body: object) {
  const out = awslambda.HttpResponseStream.from(stream, {
    statusCode,
    headers: { 'content-type': 'application/json' },
  });
  out.write(JSON.stringify(body));
  out.end();
}

export const handler = awslambda.streamifyResponse(async (event, stream) => {
  if (event.requestContext.http.method !== 'POST') return reply(stream, 405, { error: 'method not allowed' });
  const raw = event.isBase64Encoded ? Buffer.from(event.body ?? '', 'base64').toString() : event.body ?? '';
  if (Buffer.byteLength(raw) > MAX_BODY_BYTES) return reply(stream, 413, { error: 'payload too large' });

  let household: ReturnType<typeof sanitizeHousehold>;
  let alert: ReturnType<typeof sanitizeAlert>;
  try {
    const body = JSON.parse(raw);
    household = sanitizeHousehold(body?.household);
    alert = sanitizeAlert(body?.alert);
  } catch (err) {
    const message = err instanceof InvalidInput ? err.message : '入力の形式が正しくありません';
    return reply(stream, 400, { error: message });
  }

  // AgentCore の呼び出しに失敗したら、理由を JSON で返す（混雑による拒否など）
  let res;
  try {
    res = await client.send(
      new InvokeAgentRuntimeCommand({
        agentRuntimeArn: RUNTIME_ARN,
        runtimeSessionId: `${alert.alertId}-${household.id}-${randomUUID()}`, // 33文字以上が必要
        contentType: 'application/json',
        accept: 'text/event-stream',
        payload: Buffer.from(JSON.stringify({ household, alert })),
      }),
    );
  } catch (err: any) {
    console.error('InvokeAgentRuntime failed', err?.name, err?.message); // 入力内容はログに出さない
    const busy = err?.name === 'ThrottlingException' || err?.name === 'ServiceQuotaExceededException';
    return reply(stream, busy ? 503 : 502, {
      error: busy ? '混み合っているため、計画を作れませんでした。少し待ってからやり直してください。' : 'エージェントを呼び出せませんでした。',
    });
  }

  const out = awslambda.HttpResponseStream.from(stream, {
    statusCode: 200,
    headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
  });
  try {
    for await (const chunk of res.response as AsyncIterable<Uint8Array>) {
      out.write(chunk);
    }
  } catch (err: any) {
    // 途中で切れたら、画面がエラーとして表示できる形で知らせる
    console.error('stream interrupted', err?.name, err?.message);
    const ev = { kind: 'status', title: 'エラー', body: { state: 'error', error: 'エージェントとの通信が途中で切れました。' }, ts: Date.now() };
    out.write(`data: ${JSON.stringify(ev)}\n\n`);
  }
  out.end();
});
