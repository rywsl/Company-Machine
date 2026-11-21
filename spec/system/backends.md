# 生产存储与 KMS provider 隔离

本规范定义 Bun 控制面使用的状态、对象、密钥和 KMS provider 边界。默认路径保持本地 SQLite、本地 CAS 和本地 AES-256-GCM Vault。生产 provider 只有在显式启用时才会被构造。

实现约束如下。

- provider 由 TypeScript 接口隔离，业务代码不包含 PostgreSQL、MySQL、S3 或 OpenBao 方言。
- 不新增 Harness、KMS 或模型服务。OpenBao 通过 HTTP API 使用，不能嵌入 Bun 进程。
- 未设置生产环境变量时不读取云端配置，不解析云端凭据，也不发起外部网络请求。
- 密钥值只能在 provider 的运行时边界读取。日志、数据库、事件、错误、审计引用和 UI 中不得出现令牌、明文密钥、DSN 或签名 URL。
- provider 不可用必须返回 `PROVIDER_UNAVAILABLE`。禁止静默退回另一种 provider。

## 1. 运行模式

运行模式由环境变量选择。

| 变量 | 允许值 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `MAO_STORAGE_MODE` | `local`、`production` | `local` | 选择默认 provider 集合 |
| `MAO_STATE_PROVIDER` | `sqlite`、`postgres`、`mysql` | 由模式决定 | 覆盖状态 provider |
| `MAO_BLOB_PROVIDER` | `local`、`s3` | 由模式决定 | 覆盖对象 provider |
| `MAO_SECRET_PROVIDER` | `local`、`openbao` | 由模式决定 | 覆盖密钥 provider |

`MAO_STORAGE_MODE` 只能取上述值。未知值和未知 provider 名称返回 `PROVIDER_CONFIG_INVALID`，进程不得启动半配置状态。

`local` 模式的默认集合如下。

- `StateProvider` 使用当前 Bun SQLite 实现和 `FACTORY_DB_PATH` 或 `FACTORY_ROOT` 路径。
- `BlobProvider` 使用当前本地 CAS 布局 `cas/sha256/<前两位>/<其余摘要>`。
- `SecretProvider` 与 `KmsProvider` 使用当前本地 AES-256-GCM Vault。
- 本地模式只读取 `MAO_VAULT_PATH`、`MAO_VAULT_KEY_PATH` 和 `MAO_VAULT_KEY`。即使存在 `MAO_OPENBAO_*`、`MAO_S3_*`、`MAO_POSTGRES_*` 或 `MAO_MYSQL_*`，也不能读取和连接。

`production` 模式的默认集合如下。

- `StateProvider` 默认使用 PostgreSQL。
- `BlobProvider` 默认使用 S3 或 MinIO。
- `SecretProvider` 与 `KmsProvider` 默认使用 OpenBao。
- 生产模式中 OpenBao、SQL 或 S3 配置缺失或不可用时，返回 `PROVIDER_UNAVAILABLE` 或 `PROVIDER_CONFIG_INVALID`。系统不能改用本地 Vault、SQLite 或 CAS 继续运行。

显式设置 `MAO_STATE_PROVIDER=sqlite`、`MAO_BLOB_PROVIDER=local` 或 `MAO_SECRET_PROVIDER=local` 可以在生产模式中覆盖对应 provider。`MAO_SECRET_PROVIDER=local` 只在用户明确配置时生效。

## 2. TypeScript 接口

接口位于 `backend/src/storage.ts`、`backend/src/storage-openbao.ts`、`backend/src/storage-s3.ts` 和 SQL provider 模块。接口只暴露业务需要的操作，连接、事务、缓存、重试和方言实现放在 provider 内。

```ts
export interface StateProvider {
  migrate(): Promise<number> | number;
  transaction<T>(work: (tx: StateTransaction) => Promise<T> | T): Promise<T> | T;
  query<T = unknown>(statement: StateStatement): Promise<T[]> | T[];
  status(): Promise<ProviderStatus> | ProviderStatus;
  verify(): Promise<VerificationReport> | VerificationReport;
  close(): Promise<void> | void;
}

export interface BlobProvider {
  put(input: BlobInput): Promise<BlobRecord>;
  get(digest: string): Promise<Uint8Array>;
  head(digest: string): Promise<BlobRecord | undefined>;
  exists(digest: string): Promise<boolean>;
  verify(digests?: string[]): Promise<VerificationReport>;
  status(): Promise<ProviderStatus> | ProviderStatus;
}

export interface SecretProvider {
  put(ref: string, value: string): Promise<void> | void;
  get(ref: string): Promise<string | undefined> | string | undefined;
  delete(ref: string): Promise<boolean> | boolean;
  has(ref: string): Promise<boolean> | boolean;
  audit(ref: string, action: string): Promise<string> | string;
  status(): Promise<ProviderStatus> | ProviderStatus;
}

export interface KmsProvider {
  encrypt(key: string, plaintext: Uint8Array, context?: Record<string, string>): Promise<KmsCiphertext>;
  decrypt(key: string, ciphertext: KmsCiphertext, context?: Record<string, string>): Promise<Uint8Array>;
  sign(key: string, payload: Uint8Array, algorithm?: string): Promise<Uint8Array>;
  hmac(key: string, payload: Uint8Array, algorithm?: string): Promise<Uint8Array>;
  rotate(key: string): Promise<KmsKeyVersion>;
  audit(key: string, action: string): Promise<string>;
  status(): Promise<ProviderStatus> | ProviderStatus;
}
```

`StateStatement` 只包含规范化 SQL、参数和业务操作名。业务层不能拼接方言 SQL。`ProviderStatus` 只返回 provider 名称、模式、可用状态、版本和非敏感原因。`VerificationReport` 只返回摘要、计数和不一致位置。

当前 `SqliteStore`、`Warehouse` 和 `VaultStore` 通过适配器实现这些接口。适配器必须保持已有调用方的行为和本地文件布局。

## 3. OpenBao provider

OpenBao provider 通过 `fetch` 调用 OpenBao HTTP API。Bun 进程不嵌入 OpenBao，也不启动 OpenBao 子进程。

地址和认证配置来自环境变量。

- `MAO_OPENBAO_ADDR` 是 `http` 或 `https` 地址。
- `MAO_OPENBAO_TOKEN` 可以直接提供运行时令牌。
- 未提供令牌时，provider 可以使用 `MAO_OPENBAO_ROLE_ID` 与 `MAO_OPENBAO_SECRET_ID`，或 `MAO_OPENBAO_JWT` 与对应 role 环境变量完成登录。
- 令牌只保存在内存，不能写入配置文件、数据库、事件、日志或异常文本。

provider 至少实现 KV Secret、Transit 加密和解密、签名、HMAC、轮换和审计引用。审计引用是不可逆的随机 ID 或 OpenBao 返回的审计 ID，不能包含密钥值。

OpenBao 返回非成功 HTTP 状态、超时、证书错误或响应格式错误时，provider 返回 `PROVIDER_UNAVAILABLE`。生产模式不得创建本地 Vault 作为回退。OpenBao 的明文只在调用边界短暂存在，响应 DTO 和事件只能携带 secret reference、状态和审计引用。

## 4. SQL provider

SQLite、PostgreSQL 和 MySQL 都实现 `StateProvider`。事务、查询、迁移、连接池、占位符和错误边界对业务层保持一致。

- SQLite 使用 Bun `bun:sqlite` 和现有数据库文件。
- PostgreSQL 通过标准 PostgreSQL 客户端协议或受控官方命令行连接，连接串只从 `MAO_POSTGRES_DSN` 或等价环境变量读取。
- MySQL 通过标准 MySQL 客户端协议或受控官方命令行连接，连接串只从 `MAO_MYSQL_DSN` 或等价环境变量读取。
- PostgreSQL 和 MySQL 的占位符、事务隔离、分页、布尔值和迁移方言只在 provider 内处理。
- provider 不可用、连接池耗尽或迁移失败返回 `PROVIDER_UNAVAILABLE`，不能创建 SQLite 伪装成功。

迁移器以规范迁移描述为输入，为每个 SQL provider 生成对应方言。迁移过程在单个 provider 事务边界内执行，失败时回滚。业务层只能调用 `migrate()`、`transaction()`、`query()` 和 `verify()`。

## 5. S3 或 MinIO Blob provider

S3 provider 通过标准 HTTP 实现对象读写，不要求 `boto3`、MinIO SDK 或其他第三方依赖。MinIO 使用同一 provider，通过 endpoint 和 addressing 选择路径式或虚拟主机式 URL。

环境变量如下。

- `MAO_S3_ENDPOINT`
- `MAO_S3_BUCKET`
- `MAO_S3_REGION`
- `MAO_S3_ACCESS_KEY`
- `MAO_S3_SECRET_KEY`
- 可选 `MAO_S3_PREFIX`、`MAO_S3_ADDRESSING` 和超时配置

对象键与本地 CAS 的摘要保持一致。写入前计算 SHA-256，使用 HEAD 检查幂等性。相同摘要和长度的对象不重复上传，摘要冲突返回 `CAS_CONFLICT`。读取后校验摘要和长度，失败返回 `CAS_MISSING` 或 `CAS_INTEGRITY_FAILED`。本地缓存只能作为只读缓存，不能成为数据真相。

S3 凭据只在签名请求时从环境读取。请求头、签名字符串、异常、事件和数据库中不得持久化凭据。S3 不可用时返回 `PROVIDER_UNAVAILABLE`，不能回退到本地 CAS。

## 6. 迁移、状态和校验

控制面提供 provider status、migrate 和 verify 能力。CLI 和 HTTP 管理面使用同一 provider registry。

- `status` 返回当前模式、各 provider 名称、配置完整性、连接状态和非敏感原因。
- `migrate` 从当前活动 provider 复制状态行、对象和密钥引用。密钥明文不能进入迁移报告，OpenBao 迁移只复制引用或通过 provider 内部轮换复制密文。
- 迁移完成后计算状态行数、规范摘要、对象数量和对象摘要。所有摘要一致后才写入活动 provider 指针。
- `verify` 对当前活动 provider 重算上述摘要并报告差异。校验失败退出失败状态，不修改数据和活动指针。
- 迁移失败保留本地 provider、原数据和旧指针。目标 provider 的部分数据可以保留用于诊断，但不能自动切换。
- 默认本地模式不会读取迁移配置，也不会执行迁移代码中的外部连接。

活动 provider 指针只包含 provider 名称、模式、规范配置摘要和切换时间。不得包含 DSN、访问密钥、OpenBao token 或签名 URL。

## 7. 错误语义

| 错误码 | 语义 |
| --- | --- |
| `PROVIDER_CONFIG_INVALID` | 模式、provider 名称、地址、桶或必需环境变量非法 |
| `PROVIDER_UNAVAILABLE` | provider 无法连接、超时、认证失败或连接池不可用 |
| `CAS_CONFLICT` | 已有对象与期望摘要或长度不一致 |
| `CAS_MISSING` | 索引存在但对象或本地缓存缺失 |
| `CAS_INTEGRITY_FAILED` | 读取对象的摘要或长度校验失败 |
| `MIGRATE_VERIFY_FAILED` | 迁移后的行数、摘要或对象集合不一致 |
| `MIGRATE_IN_PROGRESS` | 已有未完成迁移锁 |
| `KMS_OPERATION_FAILED` | OpenBao 或本地 KMS 的加密、签名、HMAC 或轮换失败 |

错误 detail 可以包含 provider 名称、主机名、桶名、摘要和审计引用。禁止包含 DSN、token、访问密钥、明文 secret 或带查询串的签名 URL。

## 8. 分阶段交付边界

provider 改造按以下阶段提交。每一阶段都要同步更新本规范和对应 Bun 回归测试。

1. 本地适配层和 provider registry。接入 SQLite、CAS、AES-256-GCM Vault，验证未设置环境变量时的零外部连接。
2. OpenBao KMS 和 Secret provider。完成 KV、Transit、签名、HMAC、轮换、审计引用及失败语义。
3. PostgreSQL 和 MySQL State provider。完成连接池、事务、迁移和方言边界。未安装或不可用时返回 provider 错误。
4. S3 或 MinIO Blob provider。完成摘要校验、幂等写入、HEAD、读写和只读缓存。
5. 迁移、status、verify 和活动 provider 切换。只有摘要、行数和对象数量校验通过才能切换。

每阶段都保留默认本地路径。后续阶段失败不能改变已经可用的本地 provider。

## 9. 验收要求

验收使用 Bun 测试和仓库标准入口。

- 未设置 `MAO_STORAGE_MODE` 时，测试设置任意云端环境变量也不会发起 HTTP、TCP、psql、MySQL 或 OpenBao 连接。
- `local` 模式使用 SQLite、本地 CAS 和本地 AES-256-GCM Vault，并保持现有 `MAO_VAULT_*` 行为。
- `production` 模式默认选择 PostgreSQL、S3 和 OpenBao。缺少配置或服务不可用时返回 `PROVIDER_CONFIG_INVALID` 或 `PROVIDER_UNAVAILABLE`，不发生静默回退。
- 显式 provider 覆盖只影响对应 provider。Operator、Foreman 和业务服务共用 provider registry，但每个调用者的 profile 和密钥引用保持独立。
- OpenBao 测试覆盖 KV、Transit、签名、HMAC、轮换、审计引用和 token 脱敏。
- S3 测试覆盖 HEAD、幂等 PUT、摘要冲突、读取校验、缓存和不可用错误。
- PostgreSQL 和 MySQL 测试覆盖 provider 选择、事务边界、占位符翻译、迁移和不可用错误。测试使用本地 fake server 或受控 stub，不要求真实云服务。
- 迁移测试覆盖摘要一致后切换、摘要不一致时保留原指针和失败后保留原数据。

提交或推送前运行以下命令。

```text
bun run verify:bun
bun run build:web
```

所有 provider 测试必须通过。默认本地测试不得因机器上存在云端环境变量而改变行为。
