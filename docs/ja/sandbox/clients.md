---
search:
  exclude: true
---
# サンドボックスクライアント

このページでは、サンドボックスでの作業を実行する場所を選択します。ほとんどの場合、`SandboxAgent` の定義はそのまま維持し、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] でサンドボックスクライアントとクライアント固有のオプションのみを変更します。

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供までに API の詳細、デフォルト、サポートされる機能が変更される可能性があります。また、今後さらに高度な機能が追加される予定です。

## 選択ガイド {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目的 | 最初の選択 | 理由 |
| --- | --- | --- |
| macOS または Linux での最速のローカルイテレーション | `UnixLocalSandboxClient` | 追加インストールが不要で、ローカルファイルシステムを使用した開発が簡単です。 |
| 基本的なコンテナ分離 | `DockerSandboxClient` | 特定のイメージを使用して、Docker 内で作業を実行します。 |
| ホステッド実行または本番環境に近い分離 | ホステッドサンドボックスクライアント | ワークスペースの境界をプロバイダー管理の環境に移します。 |

</div>

## ローカルクライアント {#local-clients}

ほとんどのユーザーには、次の 2 つのサンドボックスクライアントのいずれかを推奨します。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 選択する場合 | コード例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | なし | macOS または Linux で最速のローカルイテレーションが必要な場合。ローカル開発のデフォルトとして適しています。 | [Unix-local スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | コンテナ分離が必要な場合、または対象環境をローカルで再現するために特定のイメージを使用する場合。 | [Docker スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local は、ローカルファイルシステムを対象とした開発を始める最も簡単な方法です。より強力な環境分離や本番環境に近い整合性が必要になった場合は、Docker またはホステッドプロバイダーに移行してください。

`SandboxPathGrant.host_path` は Docker 専用で、ホストパスをコンテナ内の別の POSIX パスにマッピングします。Unix-local では、同一パスへの許可のみをサポートします。詳細は、[マニフェストのパス許可](guide.md#manifest)を参照してください。

Unix-local から Docker に切り替えるには、エージェント定義をそのまま維持し、実行設定のみを変更します。

```python
from docker import from_env as docker_from_env

from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=DockerSandboxClient(docker_from_env()),
        options=DockerSandboxClientOptions(image="python:3.14-slim"),
    ),
)
```

コンテナ分離が必要な場合、またはサンドボックスイメージを別の環境で使用されるイメージと一致させる場合に使用します。[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) を参照してください。

### Docker ネットワークの無効化 {#disable-docker-networking}

Docker サンドボックスからネットワークにアクセスできないようにする必要がある場合は、`network_mode="none"` を設定します。

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

明示的にサポートされるネットワークモードは `"none"` のみです。Docker のデフォルト動作を維持するには、`network_mode` を省略してください。ネットワークを無効化したサンドボックスはポートを公開できないため、`network_mode="none"` と空ではない `exposed_ports` タプルを組み合わせると、オプションの検証時に失敗します。この設定はサンドボックスのセッション状態に保存され、その状態を再開する際に SDK が代替コンテナを作成する必要がある場合にも再適用されます。

## マウントとリモートストレージ {#mounts-and-remote-storage}

マウントエントリでは公開するストレージを記述し、マウント戦略ではサンドボックスバックエンドがそのストレージを接続する方法を記述します。組み込みのマウントエントリと汎用戦略は `agents.sandbox.entries` からインポートします。ホステッドプロバイダー向けの戦略は、`agents.extensions.sandbox` またはプロバイダー固有の拡張パッケージから利用できます。

一般的なマウントオプションは次のとおりです。

- `mount_path`: サンドボックス内でストレージが表示される場所です。相対パスはマニフェストルートを基準に解決され、絶対パスはそのまま使用されます。
- `read_only`: デフォルトは `True` です。サンドボックスからマウント済みストレージへ書き戻す必要がある場合にのみ、`False` を設定してください。
- `mount_strategy`: 必須です。マウントエントリとサンドボックスバックエンドの両方に適合する戦略を使用してください。

マウントは一時的なワークスペースエントリとして扱われます。スナップショットおよび永続化フローでは、マウントされたリモートストレージを保存済みワークスペースへコピーせず、マウントされたパスを切り離すかスキップします。

汎用のローカル／コンテナ戦略は次のとおりです。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 戦略またはパターン | 使用する場合 | 注記 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | サンドボックスイメージで `rclone` を実行できる場合。 | S3、GCS、R2、Azure Blob、Box をサポートします。`RcloneMountPattern` は `fuse` モードまたは `nfs` モードで実行できます。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | イメージに `mount-s3` があり、Mountpoint 形式の S3 または S3 互換アクセスを使用する場合。 | `S3Mount` と `GCSMount` をサポートします。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | イメージに `blobfuse2` と FUSE サポートがある場合。 | `AzureBlobMount` をサポートします。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | イメージに `mount.s3files` があり、既存の S3 Files マウントターゲットに到達できる場合。 | `S3FilesMount` をサポートします。 |
| `DockerVolumeMountStrategy(driver=...)` | コンテナの起動前に、Docker でボリュームドライバーを使用したマウントを接続する場合。 | Docker 専用です。S3、GCS、R2、Azure Blob、Box は `rclone` を介してマウントできます。S3 と GCS は `mountpoint` を介してマウントすることもできます。 |

</div>

## 対応ホステッドプラットフォーム {#supported-hosted-platforms}

ホステッド環境が必要な場合、通常は同じ `SandboxAgent` 定義を引き継ぎ、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] のサンドボックスクライアントのみを変更します。

このリポジトリのチェックアウトではなく公開済みの SDK を使用している場合は、対応するパッケージの extra を通じてサンドボックスクライアントの依存関係をインストールしてください。

プロバイダー固有の設定に関する注記と、リポジトリに含まれる拡張機能のコード例へのリンクについては、[examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md) を参照してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | コード例 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

### Modal サンドボックスのリソースサイズ {#size-modal-sandboxes}

新しい Modal サンドボックスのリソースを要求するには、`ModalSandboxClientOptions.cpu` と `ModalSandboxClientOptions.memory` を使用します。単一の値では、その量を要求します。2 項目の `(request, limit)` タプルでは、最初の項目を要求値、2 番目の項目を上限値として使用します。メモリ値の単位は MiB です。

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

省略した各リソースに Modal のデフォルトを使用するには、`cpu`、`memory`、またはその両方を `None` のままにします。選択した値はサンドボックスのセッション状態に保持されるため、代替サンドボックスでも同じリソース設定が使用されます。

ホステッドサンドボックスクライアントは、プロバイダー固有のマウント戦略を公開します。ストレージプロバイダーに最適なバックエンドとマウント戦略を選択してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | マウントに関する注記 |
| --- | --- |
| Docker | `InContainerMountStrategy` や `DockerVolumeMountStrategy` などのローカル戦略により、`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount`、`S3FilesMount` をサポートします。 |
| `ModalSandboxClient` | `ModalCloudBucketMountStrategy` を `S3Mount`、`R2Mount`、HMAC 認証を使用する `GCSMount` と組み合わせることで、クラウドバケットのマウントをサポートします。インライン認証情報または名前付き Modal Secret を使用できます。 |
| `CloudflareSandboxClient` | `CloudflareBucketMountStrategy` を `S3Mount`、`R2Mount`、HMAC 認証を使用する `GCSMount` と組み合わせることで、バケットのマウントをサポートします。 |
| `BlaxelSandboxClient` | `BlaxelCloudBucketMountStrategy` と `S3Mount`、`R2Mount`、または `GCSMount` のエントリを組み合わせることで、クラウドバケットのマウントをサポートします。また、`agents.extensions.sandbox.blaxel` から利用できる `BlaxelDriveMount` と `BlaxelDriveMountStrategy` により、永続的な Blaxel Drives もサポートします。 |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy` を使用し、`rclone` を介したクラウドストレージのマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` と組み合わせて使用します。 |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy` を使用し、`rclone` を介したクラウドストレージのマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` と組み合わせて使用します。 |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy` を使用し、`rclone` を介したクラウドストレージのマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` と組み合わせて使用します。 |
| `VercelSandboxClient` | `VercelCloudBucketMountStrategy` と `S3Mount` エントリを組み合わせることで、作成時のみの S3 および S3 互換バケットのマウントをサポートします。マウント済みセッションは再開できず、インライン認証情報には `allow_s3_credential_exposure=True` が必要です。 |

</div>

マウント表は、各バックエンドで実行できるストレージタイプを示します。チェックマークが付いていても、モデルが制御するサンドボックス内で実行されるマウントヘルパーの認証情報境界を迂回できるわけではなく、すべての戦略が認証情報なしで動作できることも意味しません。Agents SDK は、選択したヘルパーが保護対象の権限なしで動作できる場合に限り、承認なしでコンテナ内マウントを受け入れます。保護対象の権限が必要なマウントについては、信頼できるアプリケーションコードが該当する正確なマウントパスへの権限公開を明示的に承認しない限り、サンドボックスまたはマウントヘルパーを起動する前に拒否します。

認証情報を使用しない `rclone` マウントは、S3、GCS、R2、Azure Blob に限定されます。コンテナ内での Box マウントには、非対話型の認証ソースと、そのソースに対応する承認が必要です。インライン認証情報が設定されていない場合でも、`blobfuse2` は環境内に存在する Azure の権限を検出するため、`FuseMountPattern` には広範な承認が必要です。同様に、`mount.s3files` は環境内に存在する IAM 権限を使用するため、`S3FilesMountPattern` にも広範な承認が必要です。これらの要件は、Docker をバックエンドとして使用する場合にも適用されます。以下のチェックマークは、該当する権限境界の要件が満たされた後に Docker でマウントを実行できることを示します。

`"data"` という名前のマウントエントリでは、設定された権限に対応する承認から返された、コピー済みの `Manifest` を保持してください。

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

承認が必要な正確なマウントパスをすべて渡してください。両方の権限クラスを使用するマウントには、両方の承認が必要です。承認は実行時にのみ有効で、シリアライズされません。また、認証情報の使用をマウントパス内に限定することなく、ヘルパーが認証情報を受け取ることを許可します。利用可能な場合は外部戦略またはプロバイダーネイティブ戦略を優先し、それ以外の場合はサンドボックスに限定された短期間有効かつ最小権限の認証情報を使用してください。

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)` は、マウント範囲に限定されたインライン認証情報を使用する、作成時の Vercel S3 マウント向け互換オプションとして引き続き利用できます。広範な認証情報への権限を許可するものではありません。

以下の表は、各バックエンドが直接マウントできるリモートストレージエントリをまとめたものです。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | Box | S3 Files |
| --- | --- | --- | --- | --- | --- | --- |
| Docker | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ModalSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `CloudflareSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `BlaxelSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `DaytonaSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `E2BSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `RunloopSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `VercelSandboxClient` | ✓ | - | - | - | - | - |

</div>

実行可能なコード例については、ローカル、コーディング、メモリ、ハンドオフ、エージェント構成のパターンを扱う [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) と、ホステッドサンドボックスクライアントを扱う [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) を参照してください。