/**
 * КОНТРАКТ РАЗВЁРТЫВАНИЯ: расписание, запускающее Cloud Run Job, обязано иметь
 * привязку roles/run.invoker для ТОГО ЖЕ сервис-аккаунта.
 *
 * Зачем регрессия. 23.09.2026 данные Ozon-Юнитки за 22.09 не появились в книге.
 * Расписание `ozon-unitka-prod` существовало, было ENABLED и срабатывало — но Job'у
 * никогда не выдавался invoker, и вызов получал PERMISSION_DENIED (status.code=7).
 * Дефект не падал: execution не создавался, логов Job'а не возникало, а обе
 * алерт-политики проекта слушают именно `loader_failed` В ЛОГАХ JOB'А. Расписание
 * молча пропускало сутки, retry не было, и заметил это человек — по пустой строке.
 *
 * Отдельно от IAM проверяется НАБЛЮДАЕМОСТЬ: одной привязки мало, потому что она
 * может быть снесена или не применена. Поэтому контракт требует ещё и политику,
 * слушающую сам Cloud Scheduler, — она единственная видит отказ, при котором
 * Job вообще не запускался.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const TF_DIR = join(fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'infra', 'terraform');
const FILES = readdirSync(TF_DIR).filter((f) => f.endsWith('.tf'));
const SRC = new Map(FILES.map((f) => [f, readFileSync(join(TF_DIR, f), 'utf8')]));
const ALL = [...SRC.values()].join('\n');

/** Тело HCL-блока от открывающей скобки, со счётом вложенности. */
function body(src: string, openIdx: number): string {
  let depth = 1;
  let i = openIdx;
  while (depth > 0 && i < src.length) {
    i += 1;
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') depth -= 1;
  }
  return src.slice(openIdx + 1, i);
}

function blocks(type: string): Array<{ file: string; label: string; text: string }> {
  const out: Array<{ file: string; label: string; text: string }> = [];
  for (const [file, src] of SRC) {
    const re = new RegExp(`resource\\s+"${type}"\\s+"([^"]+)"\\s*\\{`, 'g');
    for (let m = re.exec(src); m !== null; m = re.exec(src)) {
      out.push({ file, label: m[1]!, text: body(src, m.index + m[0].length - 1) });
    }
  }
  return out;
}

/** `google_cloud_run_v2_job.NAME` или `google_cloud_run_v2_job.NAME[each.key]` → NAME. */
const jobRef = (s: string): string | null =>
  /google_cloud_run_v2_job\.([A-Za-z0-9_]+)/.exec(s)?.[1] ?? null;

/**
 * Ссылка на SA приводится к сравнимому виду. Обе стороны нормализуются ОДИНАКОВО:
 * `google_service_account.scheduler_prod.email` и
 * `"serviceAccount:${google_service_account.scheduler_prod.email}"` → один и тот же ключ.
 */
function saKey(raw: string): string {
  return raw
    .trim()
    .replace(/^"|"$/g, '')
    .replace(/\$\{|\}/g, '')
    .replace(/^serviceAccount:/, '')
    .replace(/\.email$/, '')
    .trim();
}

interface Binding { job: string; sa: string }
const INVOKERS: Binding[] = blocks('google_cloud_run_v2_job_iam_member')
  .filter((b) => /role\s*=\s*"roles\/run\.invoker"/.test(b.text))
  .map((b) => ({
    job: jobRef(/name\s*=\s*([^\n]+)/.exec(b.text)?.[1] ?? '') ?? '',
    sa: saKey(/member\s*=\s*([^\n]+)/.exec(b.text)?.[1] ?? ''),
  }));

interface Sched { file: string; label: string; job: string; sa: string }
const RUN_SCHEDULERS: Sched[] = blocks('google_cloud_scheduler_job')
  .map((b) => {
    const uri = /uri\s*=\s*([^\n]+)/.exec(b.text)?.[1] ?? '';
    const sa = /service_account_email\s*=\s*([^\n]+)/.exec(b.text)?.[1] ?? '';
    return { file: b.file, label: b.label, uri, job: jobRef(uri) ?? '', sa: saKey(sa) };
  })
  // только те, что бьют в Run Admin API :run — BigQuery-расписания сюда не относятся
  .filter((s) => s.uri.includes(':run') && s.job !== '')
  .map(({ file, label, job, sa }) => ({ file, label, job, sa }));

describe('расписание Cloud Run Job не существует без права запуска', () => {
  it('в конфигурации вообще есть такие расписания (иначе тест бессмысленно зелёный)', () => {
    expect(RUN_SCHEDULERS.length).toBeGreaterThanOrEqual(8);
    expect(INVOKERS.length).toBeGreaterThanOrEqual(8);
  });

  it.each(RUN_SCHEDULERS.map((s) => [`${s.file}: ${s.label}`, s] as const))(
    '%s — у Job есть roles/run.invoker для его же SA',
    (_name, s) => {
      const forJob = INVOKERS.filter((b) => b.job === s.job);
      expect(forJob.length, `нет ни одной привязки run.invoker для google_cloud_run_v2_job.${s.job}`)
        .toBeGreaterThan(0);
      expect(forJob.map((b) => b.sa), `у ${s.job} есть invoker, но не для ${s.sa}`).toContain(s.sa);
    },
  );

  it('ozon-unitka-prod — именно тот случай, ради которого написан контракт', () => {
    const s = RUN_SCHEDULERS.find((x) => x.label === 'ozon_unitka_prod');
    expect(s, 'расписание ozon_unitka_prod исчезло из конфигурации').toBeDefined();
    expect(INVOKERS).toContainEqual({ job: 'ozon_unitka_prod', sa: 'google_service_account.scheduler_prod' });
  });

  it('право выдаётся ПОРЕСУРСНО: никто не получает run.invoker на весь проект', () => {
    const projectWide = /resource\s+"google_project_iam_member"[^{]*\{[^}]*roles\/run\.invoker/s.test(ALL);
    expect(projectWide, 'run.invoker выдан на уровне проекта — это не least privilege').toBe(false);
  });
});

describe('провал ВЫЗОВА по расписанию наблюдаем', () => {
  const policies = blocks('google_monitoring_alert_policy');

  it('есть политика, слушающая cloud_scheduler_job, а не только логи Job’а', () => {
    const p = policies.find((x) => /resource\.type="cloud_scheduler_job"/.test(x.text));
    expect(p, 'все политики слушают логи Job’а — отказ ДО создания execution невидим').toBeDefined();
    expect(p!.text).toMatch(/AttemptFinished/);
    expect(p!.text).toMatch(/severity>=ERROR/);
  });

  /**
   * logging.ts раскрывает ctx ПОСЛЕДНИМ, поэтому logger.error('loader_failed', {code, message})
   * затирает имя события текстом ошибки. Фильтр по jsonPayload.message="loader_failed" был
   * мёртв: за 90 суток нуль совпадений при двадцати реальных падениях. Ловить падение можно
   * только по коду LoaderError.
   */
  it('падение loader’а не ловится по jsonPayload.message — это поле затирается ctx', () => {
    const loaderPolicies = policies.filter((x) => /resource\.type="cloud_run_job"/.test(x.text));
    expect(loaderPolicies.length).toBeGreaterThanOrEqual(2);
    for (const p of loaderPolicies) {
      expect(p.text, `${p.file}: ${p.label} снова ловит по message — фильтр не совпадёт никогда`)
        .not.toMatch(/jsonPayload\.message\s*=\s*"loader_failed"/);
      expect(p.text, `${p.file}: ${p.label} должен ловить по коду LoaderError`)
        .toMatch(/jsonPayload\.code!=""/);
    }
  });

  it('политика не сужена до одного расписания — новые Job’ы покрыты сразу', () => {
    const p = policies.find((x) => /resource\.type="cloud_scheduler_job"/.test(x.text))!;
    const filter = /filter\s*=\s*<<-?EOT([\s\S]*?)EOT/.exec(p.text)?.[1] ?? '';
    expect(filter).not.toMatch(/resource\.labels\.job_id\s*=/);
  });
});
