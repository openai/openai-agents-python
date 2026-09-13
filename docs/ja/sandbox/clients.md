---
search:
  exclude: true
---
# サンドボックスクライアント

このページでは、サンドボックスでの処理を実行する場所を選択します。ほとんどの場合、`SandboxAgent` の定義はそのままにし、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] のサンドボックスクライアントとクライアント固有のオプションのみを変更します。

!!! warning "ベータ版機能"

    サンドボックスエージェントはベータ版です。一般提供までに API の詳細、デフォルト、サポートされる機能が変更される可能性があります。また、今後より高度な機能が追加される予定です。

## 選択ガイド {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目的 | 最初に選ぶもの | 理由 |
| --- | --- | --- |
| macOS または Linux で最も高速なローカルイテレーション | `UnixLocalSandboxClient` | 追加のインストールが不要で、ローカルファイルシステムを使用した開発が容易です。 |
| 基本的なコンテナ分離 | `DockerSandboxClient` | 特定のイメージを使用して Docker 内で処理を実行します。 |
| ホステッド実行または本番環境相当の分離 | ホステッドサンドボックスクライアント | ワークスペースの境界をプロバイダー管理の環境へ移します。 |

</div>

## ローカルクライアント {#local-clients}

ほとんどのユーザーには、次の 2 つのサンドボックスクライアントのいずれかを推奨します。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 適している場合 | コード例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | なし | macOS または Linux で最も高速なローカルイテレーションが必要な場合。ローカル開発のデフォルトとして適しています。 | [Unix ローカルのスターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | コンテナ分離が必要な場合、または特定のイメージを使用してターゲット環境をローカルで再現したい場合。 | [Docker スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix ローカルは、ローカルファイルシステムを対象とした開発を開始する最も簡単な方法です。より強力な環境分離や本番環境相当の再現性が必要になった場合は、Docker またはホステッドプロバイダーへ移行してください。

`SandboxPathGrant.host_path` は Docker 専用であり、ホスト上のパスをコンテナ内の別の POSIX パスへマッピングします。Unix ローカルでは、同一パスへの許可のみがサポートされます。詳細については、[マニフェストのパス許可](guide.md#manifest)を参照してください。

### Unix ローカルセッションでのホスト環境継承の制限 {#limit-host-environment-inheritance-for-unix-local-sessions}

デフォルトでは、`UnixLocalSandboxClient` はホストプロセスの完全な環境を基に、各コマンドの環境を開始します。代わりに、ホスト変数の保守的な許可リストのみを渡すには、`inherit_host_environment=False` を設定します。

```python
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient(
    inherit_host_environment=False,
    host_environment_allowlist={"PATH", "LANG", "SSL_CERT_FILE"},
)
```

`inherit_host_environment=False` が設定され、`host_environment_allowlist` が省略されている場合、SDK は `PATH`、`LANG`、`LC_ALL`、`LC_COLLATE`、`LC_CTYPE`、`LC_MESSAGES`、`LC_MONETARY`、`LC_NUMERIC`、`LC_TIME`、`TZ`、`TERM`、`TMPDIR`、`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`、`UV_PYTHON`、`NO_COLOR`、`FORCE_COLOR`、`CI` を許可します。カスタムコレクションを渡すと、このデフォルトの許可リストが置き換えられます。カスタム許可リストを使用するには、`inherit_host_environment=False` が必要です。

`Manifest.environment` の値はホストのフィルタリング後に適用され、継承された値を上書きします。Unix ローカルコマンドには、常にワークスペースルートが `HOME` として渡されます。継承ポリシーは、シリアライズされたセッション状態ではなく現在のクライアントに属するため、`create(...)` と `resume(...)` には、その操作を実行するクライアントのポリシーが適用されます。

このオプションは、継承される環境変数のみをフィルタリングします。Unix ローカルコマンドは引き続き、ローカルファイルシステムとネットワークへアクセスできるローカルホストプロセスとして実行されます。ワークロードにより強力な分離が必要な場合は、Docker またはホステッドサンドボックスを使用してください。

Unix ローカルから Docker へ切り替えるには、エージェント定義を変更せず、実行設定のみを変更します。

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

コンテナ分離が必要な場合、または別の環境で使用されるイメージとサンドボックスイメージを一致させたい場合に使用してください。[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) を参照してください。

### Docker ネットワークの無効化 {#disable-docker-networking}

Docker サンドボックスからのネットワークアクセスを禁止する必要がある場合は、`network_mode="none"` を設定します。

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

明示的にサポートされるネットワークモードは `"none"` のみです。Docker のデフォルト動作を維持するには、`network_mode` を省略します。ネットワークが無効なサンドボックスではポートを公開できないため、`network_mode="none"` と空でない `exposed_ports` タプルを組み合わせると、オプションの検証時に失敗します。この設定はサンドボックスのセッション状態に保存され、その状態を再開する際に SDK が代替コンテナを作成する必要がある場合にも再適用されます。

### Docker コンテナのラベル付け {#label-docker-containers}

アプリケーションがサンドボックスセッション用に作成された Docker コンテナを識別または管理する必要がある場合は、`labels` を設定します。

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    labels={
        "com.example.owner": "agents-sdk",
        "com.example.environment": "development",
    },
)
```

SDK はコンテナの作成時に、これらのキーと値のペアを Docker へ渡し、[`DockerSandboxSessionState`][agents.sandbox.sandboxes.docker.DockerSandboxSessionState] に保存します。再開されたセッションが既存のコンテナへ再接続するとき、SDK は保存された各ラベルが引き続き想定どおりの値であることを検証し、ラベルが一致しない場合は `ValueError` を発生させます。SDK が保存済みの状態から代替コンテナを作成する場合、保存されたラベルが再適用されます。

## マウントとリモートストレージ {#mounts-and-remote-storage}

マウントエントリは公開するストレージを記述し、マウント戦略はサンドボックスバックエンドがそのストレージを接続する方法を記述します。組み込みのマウントエントリと汎用戦略は `agents.sandbox.entries` からインポートします。ホステッドプロバイダー用の戦略は、`agents.extensions.sandbox` またはプロバイダー固有の拡張パッケージから利用できます。

一般的なマウントオプションは次のとおりです。

- `mount_path`: サンドボックス内でストレージが配置される場所です。相対パスはマニフェストルート配下として解決され、絶対パスはそのまま使用されます。
- `read_only`: デフォルトは `True` です。サンドボックスからマウント済みストレージへ書き戻す必要がある場合にのみ、`False` を設定します。
- `mount_strategy`: 必須です。マウントエントリとサンドボックスバックエンドの両方に適合する戦略を使用してください。

マウントは、一時的なワークスペースエントリとして扱われます。スナップショットと永続化の処理では、マウントされたリモートストレージを保存対象のワークスペースへコピーする代わりに、マウント済みパスを切り離すかスキップします。

汎用のローカル／コンテナ戦略は次のとおりです。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 戦略またはパターン | 適している場合 | 備考 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | サンドボックスイメージで `rclone` を実行できる場合。 | S3、GCS、R2、Azure Blob、Box をサポートします。`RcloneMountPattern` は `fuse` モードまたは `nfs` モードで実行できます。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | イメージに `mount-s3` が含まれ、Mountpoint 方式の S3 または S3 互換アクセスを使用したい場合。 | `S3Mount` と `GCSMount` をサポートします。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | イメージに `blobfuse2` が含まれ、FUSE がサポートされている場合。 | `AzureBlobMount` をサポートします。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | イメージに `mount.s3files` が含まれ、既存の S3 Files マウントターゲットへ到達できる場合。 | `S3FilesMount` をサポートします。 |
| `DockerVolumeMountStrategy(driver=...)` | コンテナの起動前に、Docker でボリュームドライバーを利用したマウントを接続する必要がある場合。 | Docker 専用です。S3、GCS、R2、Azure Blob、Box は `rclone` を介してマウントでき、S3 と GCS は `mountpoint` を介してマウントすることもできます。 |

</div>

## サポート対象のホステッドプラットフォーム {#supported-hosted-platforms}

ホステッド環境が必要な場合、通常は同じ `SandboxAgent` 定義を引き継ぎ、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] のサンドボックスクライアントのみを変更します。

このリポジトリのチェックアウトではなく公開版 SDK を使用している場合は、対応するパッケージの extra を介してサンドボックスクライアントの依存関係をインストールします。

リポジトリに含まれる拡張機能のコード例について、プロバイダー固有の設定上の注意事項とリンクを確認するには、[examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md) を参照してください。

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

### Modal サンドボックスのサイズ指定 {#size-modal-sandboxes}

新しい Modal サンドボックス用のリソースをリクエストするには、`ModalSandboxClientOptions.cpu` と `ModalSandboxClientOptions.memory` を使用します。単一の値を指定すると、その量がリクエストされます。2 要素の `(request, limit)` タプルでは、最初の要素がリクエスト値、2 番目の要素が上限値として使用されます。メモリ値の単位は MiB です。

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

`cpu`、`memory`、またはその両方を `None` のままにすると、省略された各リソースに Modal のデフォルトが使用されます。選択した値はサンドボックスのセッション状態に保持されるため、代替サンドボックスでも同じリソース設定が使用されます。

ホステッドサンドボックスクライアントは、プロバイダー固有のマウント戦略を公開します。ストレージプロバイダーに最適なバックエンドとマウント戦略を選択してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | マウントに関する注意事項 |
| --- | --- |
| Docker | `InContainerMountStrategy` や `DockerVolumeMountStrategy` などのローカル戦略とともに、`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount`、`S3FilesMount` をサポートします。 |
| `ModalSandboxClient` | `S3Mount`、`R2Mount`、HMAC 認証を使用する `GCSMount` とともに `ModalCloudBucketMountStrategy` を使用することで、クラウドバケットのマウントをサポートします。インライン認証情報または名前付き Modal Secret を使用できます。 |
| `CloudflareSandboxClient` | `S3Mount`、`R2Mount`、HMAC 認証を使用する `GCSMount` とともに `CloudflareBucketMountStrategy` を使用することで、バケットのマウントをサポートします。 |
| `BlaxelSandboxClient` | `BlaxelCloudBucketMountStrategy` と `S3Mount`、`R2Mount`、`GCSMount` のいずれかのエントリを組み合わせることで、クラウドバケットのマウントをサポートします。また、`agents.extensions.sandbox.blaxel` から利用できる `BlaxelDriveMount` と `BlaxelDriveMountStrategy` を使用した、永続的な Blaxel Drives もサポートします。 |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy` を使用し、`rclone` を介したクラウドストレージのマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` とともに使用してください。 |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy` を使用し、`rclone` を介したクラウドストレージのマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` とともに使用してください。 |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy` を使用し、`rclone` を介したクラウドストレージのマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` とともに使用してください。 |
| `VercelSandboxClient` | `VercelCloudBucketMountStrategy` と `S3Mount` エントリを組み合わせることで、作成時に限り S3 および S3 互換バケットのマウントをサポートします。マウントされたセッションは再開できず、インライン認証情報を使用するには `allow_s3_credential_exposure=True` が必要です。 |

</div>

マウントの表は、各バックエンドで実行できるストレージタイプを示しています。チェックマークがあっても、モデルが制御するサンドボックス内で実行されるマウントヘルパーの認証情報境界が回避されるわけではなく、すべての戦略が認証情報なしで動作できることを意味するものでもありません。Agents SDK は、選択したヘルパーが保護された権限なしで動作できる場合に限り、承認なしのコンテナ内マウントを受け入れます。保護された権限が必要なマウントについては、信頼されたアプリケーションコードが対象となる正確なマウントパスへの権限公開を明示的に承認しない限り、サンドボックスまたはマウントヘルパーを開始する前に拒否します。

認証情報不要の `rclone` マウントは、S3、GCS、R2、Azure Blob に限定されます。コンテナ内の Box マウントには、非対話型の認証ソースと、そのソースに対応する承認が必要です。`FuseMountPattern` では、インライン認証情報が設定されていない場合でも `blobfuse2` が環境内の Azure 権限を検出するため、広範な承認が必要です。同様に、`S3FilesMountPattern` では `mount.s3files` が環境内の IAM 権限を使用するため、広範な承認が必要です。これらの要件は Docker がバックエンドの場合にも適用されます。以下のチェックマークは、該当する権限境界の要件を満たした後に Docker がマウントを実行できることを示します。

`"data"` という名前のマウントエントリでは、設定された権限に対応する承認によって返された、コピー済みの `Manifest` を保持してください。

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

承認を必要とする正確なマウントパスをすべて渡してください。両方の権限クラスを使用するマウントには、両方の承認が必要です。承認は実行時にのみ有効で、シリアライズされません。また、認証情報の使用をマウントされたパスに限定することなく、ヘルパーが認証情報を受け取ることを許可します。可能な場合は外部戦略またはプロバイダーネイティブ戦略を優先し、それ以外の場合はサンドボックスにスコープを限定した、短期間有効かつ最小権限の認証情報を使用してください。

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)` は、マウントにスコープを限定したインライン認証情報を使用する、作成時の Vercel S3 マウント向け互換性オプションとして引き続き利用できます。広範な認証情報の権限を許可するものではありません。

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