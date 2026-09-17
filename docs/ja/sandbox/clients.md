---
search:
  exclude: true
---
# サンドボックスクライアント

サンドボックスでの作業を実行する場所を選択するには、このページを使用します。ほとんどの場合、`SandboxAgent` の定義はそのままで、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 内のサンドボックスクライアントとクライアント固有のオプションのみを変更します。

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供までに API の詳細、デフォルト、サポートされる機能が変更される可能性があります。また、今後さらに高度な機能が追加される予定です。

## 選択ガイド {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目的 | 最初に使用するもの | 理由 |
| --- | --- | --- |
| macOS または Linux での信頼できるローカル開発 | `UnixLocalSandboxClient` | 追加のインストールは不要です。コマンドはローカルホストのプロセスとして実行されます。 |
| 基本的なコンテナ分離 | `DockerSandboxClient` | 特定のイメージを使用して、Docker 内で作業を実行します。 |
| ホステッド実行または本番環境に近い分離 | ホステッドサンドボックスクライアント | ワークスペースの境界をプロバイダー管理の環境に移します。 |

</div>

!!! warning "Unix ローカル実行の制限"

    `UnixLocalSandboxClient` は、コマンドをローカルホストのプロセスとして実行します。Linux では、このバックエンドによる OS レベルの制限は追加されません。コマンドは、ホストプロセスおよび外部の分離機構によって許可されたファイルやネットワークリソースにアクセスできます。ワークスペースディレクトリ、`HOME`、または `cwd` を使用しても、そのアクセスは制限されません。

    macOS では、このバックエンドは `sandbox-exec` を使用してファイルシステムの制限を適用します。これらの制限では、ネットワークの分離やコンテナと同等の境界は提供されません。

    Unix ローカルは、信頼できるローカル開発、または外部で分離された環境内で使用してください。信頼できない入力の影響を受けるコマンドなど、信頼できないコマンドには、適切に構成された Docker またはホステッドサンドボックスを選択するか、外部の分離機構を用意してください。ワークロードに応じて、選択した環境の権限、マウント、認証情報、ネットワークアクセスを確認してください。

## ローカルクライアント {#local-clients}

ほとんどのユーザーは、次の 2 つのサンドボックスクライアントのいずれかから始めてください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 選択する場合 | コード例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | なし | macOS または Linux での信頼できるローカル開発、あるいは外部で分離された環境内での実行。 | [Unix ローカルスターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | コンテナ分離が必要な場合、または対象環境をローカルで再現するために特定のイメージを使用する場合。 | [Docker スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix ローカルでは、コンテナを必要とせずにローカルワークスペースを利用できます。バックエンドが提供する分離境界、または別の環境に一致するイメージが必要な場合は、Docker またはホステッドプロバイダーを選択してください。

`SandboxPathGrant.host_path` は Docker 専用であり、ホストパスをコンテナ内の別の POSIX パスにマッピングします。Unix ローカルでは、同一パスの許可のみをサポートします。詳細は、[マニフェストのパス許可](guide.md#manifest)を参照してください。

### Unix ローカルセッションでのホスト環境継承の制限 {#limit-host-environment-inheritance-for-unix-local-sessions}

デフォルトでは、`UnixLocalSandboxClient` はすべてのコマンド環境をホストプロセスの完全な環境から開始します。代わりに、ホスト変数の控えめな許可リストのみを渡すには、`inherit_host_environment=False` を設定します。

```python
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient(
    inherit_host_environment=False,
    host_environment_allowlist={"PATH", "LANG", "SSL_CERT_FILE"},
)
```

`inherit_host_environment=False` の場合に `host_environment_allowlist` を省略すると、SDK は `PATH`、`LANG`、`LC_ALL`、`LC_COLLATE`、`LC_CTYPE`、`LC_MESSAGES`、`LC_MONETARY`、`LC_NUMERIC`、`LC_TIME`、`TZ`、`TERM`、`TMPDIR`、`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`、`UV_PYTHON`、`NO_COLOR`、`FORCE_COLOR`、`CI` を許可します。そのデフォルトの許可リストを置き換えるには、カスタムコレクションを渡します。カスタム許可リストには `inherit_host_environment=False` が必要です。

`Manifest.environment` の値はホストのフィルタリング後に適用され、継承された値を上書きします。Unix ローカルコマンドには、ワークスペースルートが常に `HOME` として渡されます。継承ポリシーはシリアライズされたセッション状態ではなく現在のクライアントに属するため、`create(...)` と `resume(...)` では、その操作を実行するクライアントのポリシーが適用されます。

このオプションは、継承される環境変数のみをフィルタリングします。OS レベルの制限は追加しません。前述の Unix ローカル実行の制限は引き続き適用されます。

Unix ローカルから Docker に切り替えるには、エージェント定義をそのまま維持し、実行設定のみを変更します。

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

コンテナ分離が必要な場合、またはサンドボックスイメージを別の環境で使用されるイメージと一致させる場合に使用してください。[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) を参照してください。

### Docker ネットワークの無効化 {#disable-docker-networking}

Docker サンドボックスのネットワークアクセスを禁止する必要がある場合は、`network_mode="none"` を設定します。

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

明示的にサポートされるネットワークモードは `"none"` のみです。Docker のデフォルト動作を維持するには、`network_mode` を省略してください。ネットワークを無効にしたサンドボックスはポートを公開できないため、`network_mode="none"` と空でない `exposed_ports` タプルを組み合わせると、オプションの検証時に失敗します。この設定はサンドボックスのセッション状態に保存され、その状態の再開中に SDK が代替コンテナを作成する必要がある場合にも再適用されます。

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

SDK はコンテナの作成時にこれらのキーと値のペアを Docker に渡し、[`DockerSandboxSessionState`][agents.sandbox.sandboxes.docker.DockerSandboxSessionState] に保存します。再開されたセッションが既存のコンテナに再接続すると、SDK は保存された各ラベルが引き続き想定どおりの値であることを検証し、ラベルが一致しない場合は `ValueError` を送出します。SDK が保存された状態から代替コンテナを作成する場合、保存されたラベルを再適用します。

## マウントとリモートストレージ {#mounts-and-remote-storage}

マウントエントリは公開するストレージを記述し、マウント戦略はサンドボックスバックエンドがそのストレージを接続する方法を記述します。組み込みのマウントエントリと汎用戦略は `agents.sandbox.entries` からインポートします。ホステッドプロバイダー向けの戦略は、`agents.extensions.sandbox` またはプロバイダー固有の拡張パッケージから利用できます。

一般的なマウントオプションは次のとおりです。

- `mount_path`: サンドボックス内でストレージが表示される場所です。相対パスはマニフェストルートを基準に解決され、絶対パスはそのまま使用されます。
- `read_only`: デフォルトは `True` です。サンドボックスからマウントされたストレージへ書き戻す必要がある場合にのみ、`False` を設定します。
- `mount_strategy`: 必須です。マウントエントリとサンドボックスバックエンドの両方に適合する戦略を使用してください。

マウントは一時的なワークスペースエントリとして扱われます。スナップショットおよび永続化のフローでは、マウントされたリモートストレージを保存済みワークスペースにコピーせず、マウントされたパスをデタッチまたはスキップします。

汎用的なローカル／コンテナ戦略は次のとおりです。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 戦略またはパターン | 使用する場合 | 注記 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | サンドボックスイメージで `rclone` を実行できる場合。 | S3、GCS、R2、Azure Blob、Box をサポートします。`RcloneMountPattern` は `fuse` モードまたは `nfs` モードで実行できます。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | イメージに `mount-s3` があり、Mountpoint 形式の S3 または S3 互換アクセスが必要な場合。 | `S3Mount` と `GCSMount` をサポートします。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | イメージに `blobfuse2` と FUSE サポートがある場合。 | `AzureBlobMount` をサポートします。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | イメージに `mount.s3files` があり、既存の S3 Files マウントターゲットに到達できる場合。 | `S3FilesMount` をサポートします。 |
| `DockerVolumeMountStrategy(driver=...)` | コンテナの起動前に、Docker でボリュームドライバーを利用したマウントを接続する場合。 | Docker 専用です。S3、GCS、R2、Azure Blob、Box は `rclone` を介してマウントできます。S3 と GCS は `mountpoint` を介してマウントすることもできます。 |

</div>

## 対応ホステッドプラットフォーム {#supported-hosted-platforms}

ホステッド環境が必要な場合、通常は同じ `SandboxAgent` 定義を引き継ぎ、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 内のサンドボックスクライアントのみを変更します。

このリポジトリのチェックアウトではなく公開済みの SDK を使用している場合は、対応するパッケージの extra を使用してサンドボックスクライアントの依存関係をインストールしてください。

プロバイダー固有のセットアップに関する注記と、リポジトリに含まれる拡張機能のコード例へのリンクについては、[examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md) を参照してください。

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

新しい Modal サンドボックスにリソースを要求するには、`ModalSandboxClientOptions.cpu` と `ModalSandboxClientOptions.memory` を使用します。単一の値を指定すると、その量が要求されます。2 要素の `(request, limit)` タプルでは、最初の要素を要求値、2 番目の要素を上限値として使用します。メモリ値の単位は MiB です。

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

省略する各リソースに Modal のデフォルトを使用するには、`cpu`、`memory`、またはその両方を `None` のままにします。選択した値はサンドボックスのセッション状態に保存されるため、代替サンドボックスでも同じリソース構成が使用されます。

ホステッドサンドボックスクライアントは、プロバイダー固有のマウント戦略を公開します。ストレージプロバイダーに最適なバックエンドとマウント戦略を選択してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | マウントに関する注記 |
| --- | --- |
| Docker | `InContainerMountStrategy` や `DockerVolumeMountStrategy` などのローカル戦略で、`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount`、`S3FilesMount` をサポートします。 |
| `ModalSandboxClient` | `ModalCloudBucketMountStrategy` を `S3Mount`、`R2Mount`、HMAC 認証された `GCSMount` とともに使用することで、クラウドバケットのマウントをサポートします。インライン認証情報または名前付き Modal Secret を使用できます。 |
| `CloudflareSandboxClient` | `CloudflareBucketMountStrategy` を `S3Mount`、`R2Mount`、HMAC 認証された `GCSMount` とともに使用することで、バケットのマウントをサポートします。 |
| `BlaxelSandboxClient` | `BlaxelCloudBucketMountStrategy` と `S3Mount`、`R2Mount`、または `GCSMount` のエントリを組み合わせることで、クラウドバケットのマウントをサポートします。また、`agents.extensions.sandbox.blaxel` から利用できる `BlaxelDriveMount` と `BlaxelDriveMountStrategy` を使用した永続的な Blaxel Drives もサポートします。 |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy` を使用して `rclone` 経由でクラウドストレージをマウントできます。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` とともに使用します。 |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy` を使用して `rclone` 経由でクラウドストレージをマウントできます。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` とともに使用します。 |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy` を使用して `rclone` 経由でクラウドストレージをマウントできます。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` とともに使用します。 |
| `VercelSandboxClient` | `VercelCloudBucketMountStrategy` と `S3Mount` エントリを組み合わせることで、作成時に限り S3 および S3 互換バケットのマウントをサポートします。マウントされたセッションは再開できず、インライン認証情報には `allow_s3_credential_exposure=True` が必要です。 |

</div>

マウントの表は、各バックエンドで実行できるストレージの種類を示しています。チェックマークが付いていても、モデル制御下のサンドボックス内で動作するマウントヘルパーの認証情報境界を回避できるわけではなく、すべての戦略が認証情報なしで動作できることを意味するものでもありません。Agents SDKは、選択されたヘルパーが保護された権限なしで動作できる場合に限り、承認なしでコンテナ内マウントを受け入れます。保護された権限を必要とするマウントについては、信頼できるアプリケーションコードが対象となる正確なマウントパスへの権限公開を明示的に承認しない限り、サンドボックスまたはマウントヘルパーの起動前に拒否します。

認証情報なしの `rclone` マウントは、S3、GCS、R2、Azure Blob に限定されます。コンテナ内の Box マウントには、非対話型の認証ソースと、そのソースに対応する承認が必要です。インライン認証情報が構成されていない場合でも、`blobfuse2` が環境内の Azure 権限を検出するため、`FuseMountPattern` には広範な承認が必要です。同様に、`mount.s3files` が環境内の IAM 権限を使用するため、`S3FilesMountPattern` にも広範な承認が必要です。これらの要件は、Docker がバックエンドの場合にも適用されます。以下のチェックマークは、該当する権限境界の要件を満たした後に Docker がマウントを実行できることを示しています。

`"data"` という名前のマウントエントリでは、構成された権限に対応する承認によって返された、コピー済みの `Manifest` を保持してください。

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

承認が必要な正確なマウントパスをすべて渡してください。両方の権限クラスを使用するマウントには、両方の承認が必要です。承認は実行時にのみ有効で、シリアライズされません。また、認証情報の使用をマウントされたパスに限定することなく、ヘルパーが認証情報を受け取ることを許可します。利用可能な場合は外部戦略またはプロバイダーネイティブの戦略を優先し、それ以外の場合はサンドボックスに限定された、有効期間が短く、最小権限の認証情報を使用してください。

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)` は、マウント範囲に限定されたインライン認証情報を使用する、作成時の Vercel S3 マウント向け互換性オプションとして引き続き使用できます。広範な認証情報の権限を認めるものではありません。

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

実行可能なコード例については、ローカル、コーディング、メモリ、ハンドオフ、エージェント構成のパターンを示す [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) と、ホステッドサンドボックスクライアントを示す [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) を参照してください。