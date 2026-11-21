import { dirname, join, resolve } from "node:path";
import {
  LocalBlobProvider,
  LocalKmsProvider,
  LocalSecretProvider,
  ProviderError,
  type ProviderStatus,
  resolveStorageProviderConfig,
  SqliteStateProvider,
  type BlobProvider,
  type KmsProvider,
  type SecretProvider,
  type StateProvider,
  type StorageProviderConfig,
} from "./providers";
import { OpenBaoKmsProvider } from "./storage-openbao";
import { S3BlobProvider } from "./storage-s3";
import { MysqlStateProvider, PostgresStateProvider } from "./storage-sql";
import { VaultStore } from "./vault";

export interface StorageProviders {
  readonly config: StorageProviderConfig;
  readonly state: StateProvider;
  readonly blob: BlobProvider;
  readonly secret: SecretProvider;
  readonly kms: KmsProvider;
}

export interface StorageFactoryOptions {
  root?: string;
  env?: NodeJS.ProcessEnv;
  state?: StateProvider;
  blob?: BlobProvider;
  secret?: SecretProvider;
  kms?: KmsProvider;
}

/**
 * Selects providers without probing a remote service. Network calls happen only
 * when an operation is invoked, which keeps the default local path offline.
 */
export function createStorageProviders(options: StorageFactoryOptions = {}): StorageProviders {
  const env = options.env ?? process.env;
  const config = resolveStorageProviderConfig(env);
  const root = resolve(options.root ?? env.FACTORY_ROOT ?? ".factory");
  const state = options.state ?? (() => {
    if (config.state === "sqlite") return new SqliteStateProvider(env.BUSINESS_DB_PATH ?? join(root, "factory.db"));
    if (config.state === "postgres") return new PostgresStateProvider(env);
    return new MysqlStateProvider(env);
  })();
  const blob = options.blob ?? (() => {
    if (config.blob === "local") return new LocalBlobProvider(join(root, "cas"));
    return S3BlobProvider.fromEnvironment(env);
  })();
  const secret = options.secret ?? (() => {
    if (config.secret === "local") return new LocalSecretProvider(new VaultStore(
      env.MAO_VAULT_PATH ?? join(root, "vault.json"),
      env.MAO_VAULT_KEY_PATH ?? join(root, "vault.key"),
      undefined,
      env,
    ));
    return OpenBaoKmsProvider.fromEnvironment(env);
  })();
  const kms = options.kms ?? (() => {
    if (config.secret === "openbao") return secret as unknown as KmsProvider;
    const vault = secret instanceof LocalSecretProvider ? secret.vault : undefined;
    if (!vault) throw new ProviderError("PROVIDER_CONFIG_INVALID", "local KMS requires the local SecretProvider");
    return new LocalKmsProvider(vault);
  })();
  return { config, state, blob, secret, kms };
}

export function storageStatus(storage: StorageProviders): Record<string, unknown> {
  return {
    mode: storage.config.mode,
    state: storage.state.status(),
    blob: storage.blob.status(),
    secret: storage.secret.status(),
    kms: storage.kms.status(),
  };
}

export async function probeStorageStatus(storage: StorageProviders): Promise<Record<string, unknown>> {
  const result = storageStatus(storage) as Record<string, any>;
  const secret = storage.secret as SecretProvider & { checkAvailability?: () => Promise<ProviderStatus> };
  if (secret.checkAvailability) {
    const status = await secret.checkAvailability();
    result.secret = status;
    result.kms = status;
  }
  if (storage.config.state !== "sqlite") {
    const problems = storage.state.verify();
    if (problems.length) result.state = { ...result.state, available: false, code: "PROVIDER_UNAVAILABLE", reason: problems[0]?.reason ?? "state provider verification failed" };
  }
  return result;
}

export function closeStorage(storage: StorageProviders): void {
  storage.state.close();
}

export function storageRootFromEnvironment(env: NodeJS.ProcessEnv = process.env): string {
  return resolve(env.FACTORY_ROOT ?? dirname(env.FACTORY_DB_PATH ?? ".factory/factory.db"));
}
