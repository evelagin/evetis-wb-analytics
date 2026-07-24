/**
 * Доступ к Secret Manager (PR-Mig0: обёртка). Токены WB читает ТОЛЬКО runtime SA;
 * sa-deployer доступа к значениям НЕ имеет. Значения секретов не логируются.
 */
import { SecretManagerServiceClient } from '@google-cloud/secret-manager';

export class SecretsClient {
  private readonly client: SecretManagerServiceClient;

  constructor(private readonly projectId: string) {
    this.client = new SecretManagerServiceClient();
  }

  async access(secretName: string, version = 'latest'): Promise<string> {
    const name = `projects/${this.projectId}/secrets/${secretName}/versions/${version}`;
    const [res] = await this.client.accessSecretVersion({ name });
    const data = res.payload?.data;
    if (!data) throw new Error(`Секрет ${secretName} пуст или недоступен`);
    return Buffer.from(data as Uint8Array).toString('utf8');
  }
}
